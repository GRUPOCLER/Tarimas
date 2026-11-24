"""
Control de OV (refacciones)
  OV de SAP (PDF) -> surtido por escaneo -> preparacion en cajas mixtas -> entrega SAP -> factura

Estatus:
  recibida -> en_surtido -> surtida -> en_preparacion -> preparada -> entregada_sap -> facturada
  (cancelada en cualquier punto antes de facturar; "paqueteria" queda para la etapa 2)
Cada cambio de estatus se guarda en ov_eventos: de ahi salen el tiempo en el estatus actual
y la duracion de cada etapa.
"""
import io
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import (OrdenVenta, OVLinea, OVEvento, OVEscaneo, OVCaja, OVCajaItem,
                           CatalogoRefacciones, CatalogoItem)
from parsers.sap_ov_refacciones import (parsear_ov, parsear_entrega, parsear_factura, norm_codigo)
from services.permisos import acceso_modulo
get_current_user = acceso_modulo("refacciones")

router = APIRouter()

ESTATUS = [
    {"id": "recibida",       "nombre": "Recibida",          "umbral_horas": 8},
    {"id": "en_surtido",     "nombre": "En surtido",        "umbral_horas": 4},
    {"id": "surtida",        "nombre": "Surtida",           "umbral_horas": 8},
    {"id": "en_preparacion", "nombre": "En preparación",    "umbral_horas": 4},
    {"id": "preparada",      "nombre": "Preparada",         "umbral_horas": 4},
    {"id": "entregada_sap",  "nombre": "Entrega generada",  "umbral_horas": 24},
    {"id": "facturada",      "nombre": "Facturada",         "umbral_horas": None},
    {"id": "cancelada",      "nombre": "Cancelada",         "umbral_horas": None},
]
_UMBRAL = {e["id"]: e["umbral_horas"] for e in ESTATUS}
_FINALES = ("facturada", "cancelada")
ROLES_CARGA = ("admin", "gerente", "sap")
ROLES_FACTURA = ("admin", "gerente", "facturacion")
ROLES_JEFE = ("admin", "gerente")


def _iso(dt):
    return (dt.isoformat() + "Z") if dt else None


def _exigir(user, *roles):
    if user.get("rol") not in roles:
        raise HTTPException(403, "Tu usuario no tiene permiso para esta acción.")


# ───────────────────────── helpers ─────────────────────────
def _l(l: OVLinea) -> dict:
    return {
        "id": l.id, "linea": l.linea, "codigo": l.codigo, "descripcion": l.descripcion,
        "cantidad": l.cantidad, "surtida": l.surtida, "empacada": l.empacada,
        "tipo": l.tipo, "marca": l.marca, "division": l.division, "grupo": l.grupo,
        "en_mezanine": bool(l.en_mezanine), "motivo_faltante": l.motivo_faltante,
        "entrega_cantidad": l.entrega_cantidad,
        "entrega_lotes": json.loads(l.entrega_lotes) if l.entrega_lotes else [],
    }


def _prog(lineas) -> dict:
    tot = sum(l.cantidad or 0 for l in lineas)
    sur = sum(min(l.surtida or 0, l.cantidad or 0) for l in lineas)
    emp = sum(l.empacada or 0 for l in lineas)
    return {
        "piezas": tot, "surtidas": sur, "empacadas": emp,
        "lineas": len(lineas), "lineas_surtidas": sum(1 for l in lineas if (l.surtida or 0) >= (l.cantidad or 0)),
        "pct_surtido": round(100 * sur / tot, 1) if tot else 0,
        "pct_preparado": round(100 * emp / sur, 1) if sur else 0,
    }


def _ov_ser(ov: OrdenVenta, lineas, ahora=None, ncajas=0) -> dict:
    ahora = ahora or datetime.utcnow()
    en_est = (ahora - ov.estatus_desde).total_seconds() if ov.estatus_desde else 0
    um = _UMBRAL.get(ov.estatus)
    return {
        "id": ov.id, "num_ov": ov.num_ov, "fecha_ov": ov.fecha_ov, "emisor": ov.emisor,
        "cliente": ov.cliente, "cliente_rfc": ov.cliente_rfc, "destino": ov.destino,
        "destino_ciudad": ov.destino_ciudad, "fecha_entrega": ov.fecha_entrega,
        "fecha_cancelacion": ov.fecha_cancelacion, "referencia": ov.referencia,
        "notas": json.loads(ov.notas_json) if ov.notas_json else [],
        "permite_parcial": bool(ov.permite_parcial), "almacen": ov.almacen, "total": ov.total,
        "estatus": ov.estatus, "estatus_desde": _iso(ov.estatus_desde),
        "en_estatus_seg": int(en_est),
        "atrasada": bool(um and en_est > um * 3600),
        "umbral_horas": um,
        "edad_seg": int((ahora - ov.creado_en).total_seconds()) if ov.creado_en else 0,
        "creado_en": _iso(ov.creado_en), "creado_por": ov.creado_por,
        "nota_faltantes": ov.nota_faltantes,
        "entrega_folio": ov.entrega_folio, "entrega_fecha": ov.entrega_fecha,
        "entrega_en": _iso(ov.entrega_en), "entrega_por": ov.entrega_por,
        "factura_folio": ov.factura_folio, "factura_uuid": ov.factura_uuid,
        "factura_fecha": ov.factura_fecha, "factura_en": _iso(ov.factura_en), "factura_por": ov.factura_por,
        "cajas": ncajas,
        "progreso": _prog(lineas),
    }


async def _get_ov(db, ov_id) -> OrdenVenta:
    ov = await db.get(OrdenVenta, ov_id)
    if not ov:
        raise HTTPException(404, "No existe esa OV.")
    return ov


async def _lineas(db, ov_id):
    r = await db.execute(select(OVLinea).where(OVLinea.ov_id == ov_id).order_by(OVLinea.linea))
    return list(r.scalars())


async def _cambiar(db, ov: OrdenVenta, nuevo: str, user: dict, nota: str = None):
    ahora = datetime.utcnow()
    ov.estatus = nuevo
    ov.estatus_desde = ahora
    db.add(OVEvento(ov_id=ov.id, estatus=nuevo, en=ahora, por=user.get("sub"), nota=nota))


def _cod_linea(lineas, codigo, filtro=None):
    """Busca la partida por codigo leido. Devuelve (partida_elegida, existe_alguna)."""
    n = norm_codigo(codigo)
    cands = [l for l in lineas if l.codigo_norm == n]
    if not cands:
        return None, False
    for l in cands:
        if filtro is None or filtro(l):
            return l, True
    return None, True


# ───────────────────────── catalogo de categorias ─────────────────────────
@router.post("/catalogo/importar")
async def importar_catalogo(archivo: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    import openpyxl
    data = await archivo.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb["CATEGORIAS"] if "CATEGORIAS" in wb.sheetnames else wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    enc = [str(c or "").strip().upper() for c in next(it)]
    def ix(n):
        return enc.index(n) if n in enc else None
    ic, idd = ix("CODIGO"), ix("DESCRIPCION")
    if ic is None:
        raise HTTPException(400, "El archivo no tiene la columna CODIGO.")
    campos = {k: ix(k.upper()) for k in ("tipo", "marca", "division", "grupo", "catalogo", "mezanine")}
    def v(r, i):
        return (str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else "")
    reg = {}
    for r in it:
        cod = v(r, ic)
        if not cod:
            continue
        n = norm_codigo(cod)
        reg[n] = {
            "codigo_norm": n, "codigo": cod, "descripcion": v(r, idd),
            "tipo": v(r, campos["tipo"]), "marca": v(r, campos["marca"]),
            "division": v(r, campos["division"]), "grupo": v(r, campos["grupo"]),
            "en_catalogo": v(r, campos["catalogo"]).upper() == "CATALOGO",
            "en_mezanine": v(r, campos["mezanine"]).upper().startswith("MEZZ"),
        }
    filas = list(reg.values())
    for i in range(0, len(filas), 2500):
        lote = filas[i:i + 2500]
        st = pg_insert(CatalogoRefacciones).values(lote)
        st = st.on_conflict_do_update(index_elements=["codigo_norm"], set_={c: getattr(st.excluded, c) for c in lote[0] if c != "codigo_norm"})
        await db.execute(st)
    await db.commit()
    return {"ok": True, "productos": len(filas)}


@router.get("/catalogo/estado")
async def estado_catalogo(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    n = (await db.execute(select(func.count()).select_from(CatalogoRefacciones))).scalar() or 0
    return {"productos": n}


async def _enriquecer(db, partidas):
    ns = list({p["codigo_norm"] for p in partidas})
    m = {}
    if ns:
        r = await db.execute(select(CatalogoRefacciones).where(CatalogoRefacciones.codigo_norm.in_(ns)))
        m = {c.codigo_norm: c for c in r.scalars()}
    for p in partidas:
        c = m.get(p["codigo_norm"])
        p["tipo"], p["marca"], p["division"], p["grupo"] = (c.tipo, c.marca, c.division, c.grupo) if c else ("", "", "", "")
        p["en_mezanine"] = bool(c.en_mezanine) if c else False
        p["en_catalogo_categorias"] = bool(c)
    return partidas


# ───────────────────────── cargar OV ─────────────────────────
@router.post("/leer")
async def leer_ov(archivo: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lee el PDF de la OV y lo devuelve para revisar. No guarda nada."""
    _exigir(user, *ROLES_CARGA)
    data = await archivo.read()
    try:
        ov = parsear_ov(data)
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer el PDF: {e}")
    if not ov["num_ov"] or not ov["partidas"]:
        raise HTTPException(400, "El PDF no parece una confirmación de pedido (OV) de SAP.")
    await _enriquecer(db, ov["partidas"])
    ya = await db.get(OrdenVenta, ov["num_ov"])
    ov["ya_existe"] = bool(ya)
    ov["ya_existe_estatus"] = ya.estatus if ya else None
    ov["sin_categoria"] = [p["codigo"] for p in ov["partidas"] if not p["en_catalogo_categorias"]]
    return ov


class OVIn(BaseModel):
    ov: dict


@router.post("")
async def crear_ov(body: OVIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_CARGA)
    o = body.ov
    num = str(o.get("num_ov") or "").strip()
    partidas = o.get("partidas") or []
    if not num or not partidas:
        raise HTTPException(400, "Falta el número de OV o las partidas.")
    ya = await db.get(OrdenVenta, num)
    if ya:
        if ya.estatus not in ("recibida", "cancelada"):
            raise HTTPException(409, f"La OV {num} ya está en el sistema ({ya.estatus.replace('_', ' ')}). No se puede cargar de nuevo; si cambió en SAP, cancélala y vuelve a cargarla.")
        for tabla in (OVEscaneo, OVEvento, OVLinea):
            await db.execute(delete(tabla).where(tabla.ov_id == num))
        await db.execute(delete(OrdenVenta).where(OrdenVenta.id == num))
        await db.flush()
    await _enriquecer(db, partidas)
    ahora = datetime.utcnow()
    ov = OrdenVenta(
        id=num, num_ov=num, fecha_ov=o.get("fecha_ov"), emisor=o.get("emisor"), cliente=o.get("cliente"),
        cliente_codigo=o.get("cliente_codigo"), cliente_rfc=o.get("cliente_rfc"), destino=o.get("destino"),
        destino_ciudad=o.get("destino_ciudad"), fecha_entrega=o.get("fecha_entrega"),
        fecha_cancelacion=o.get("fecha_cancelacion"), referencia=o.get("referencia"),
        notas_json=json.dumps(o.get("notas") or [], ensure_ascii=False), permite_parcial=bool(o.get("permite_parcial")),
        almacen=(partidas[0].get("almacen") or ""), moneda=o.get("moneda"), total=o.get("total") or 0,
        estatus="recibida", estatus_desde=ahora, creado_en=ahora, creado_por=user["sub"],
    )
    db.add(ov)
    await db.flush()
    for p in partidas:
        db.add(OVLinea(
            ov_id=num, linea=p["linea"], codigo=p["codigo"], codigo_norm=norm_codigo(p["codigo"]),
            descripcion=p.get("descripcion"), cantidad=int(p.get("cantidad") or 0), precio=p.get("precio") or 0,
            tipo=p.get("tipo"), marca=p.get("marca"), division=p.get("division"), grupo=p.get("grupo"),
            en_mezanine=bool(p.get("en_mezanine")),
        ))
    db.add(OVEvento(ov_id=num, estatus="recibida", en=ahora, por=user["sub"], nota="OV cargada desde SAP"))
    await db.commit()
    return {"ok": True, "id": num}


# ───────────────────────── tablero y detalle ─────────────────────────
@router.get("")
async def listar(ver: str = "activas", q: str = "", db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    st = select(OrdenVenta).order_by(OrdenVenta.creado_en.desc())
    if ver == "activas":
        st = st.where(OrdenVenta.estatus.notin_(_FINALES))
    elif ver in {e["id"] for e in ESTATUS}:
        st = st.where(OrdenVenta.estatus == ver)
    if q.strip():
        like = f"%{q.strip()}%"
        st = st.where((OrdenVenta.num_ov.ilike(like)) | (OrdenVenta.cliente.ilike(like)) | (OrdenVenta.referencia.ilike(like)) | (OrdenVenta.entrega_folio.ilike(like)))
    ovs = list((await db.execute(st.limit(400))).scalars())
    ids = [o.id for o in ovs]
    por = {}
    if ids:
        for l in (await db.execute(select(OVLinea).where(OVLinea.ov_id.in_(ids)))).scalars():
            por.setdefault(l.ov_id, []).append(l)
    ncaj = {}
    if ids:
        for oid, n in (await db.execute(select(OVCaja.ov_id, func.count()).where(OVCaja.ov_id.in_(ids)).group_by(OVCaja.ov_id))).all():
            ncaj[oid] = n
    ahora = datetime.utcnow()
    conteo = {e["id"]: 0 for e in ESTATUS}
    for est, n in (await db.execute(select(OrdenVenta.estatus, func.count()).group_by(OrdenVenta.estatus))).all():
        conteo[est] = n
    return {
        "estatus": ESTATUS,
        "conteo": conteo,
        "ovs": [_ov_ser(o, por.get(o.id, []), ahora, ncaj.get(o.id, 0)) for o in ovs],
    }


async def _detalle(db, ov: OrdenVenta) -> dict:
    ls = await _lineas(db, ov.id)
    cajas = list((await db.execute(select(OVCaja).where(OVCaja.ov_id == ov.id).order_by(OVCaja.numero))).scalars())
    items = []
    if cajas:
        items = list((await db.execute(select(OVCajaItem).join(OVCaja, OVCaja.id == OVCajaItem.caja_id).where(OVCaja.ov_id == ov.id))).scalars())
    por_linea = {l.id: l for l in ls}
    pesos = await _pesos(db, ls)
    cajas_ser = []
    for c in cajas:
        its = [i for i in items if i.caja_id == c.id and i.cantidad > 0]
        calc, completo = 0.0, True
        for i in its:
            l = por_linea.get(i.linea_id)
            p = pesos.get(l.codigo_norm) if l else None
            if p:
                calc += p * i.cantidad
            else:
                completo = False
        cajas_ser.append({
            "id": c.id, "numero": c.numero, "estatus": c.estatus, "peso_kg": c.peso_kg,
            "peso_calc_kg": round(calc, 2) if its else 0, "peso_calc_completo": completo and bool(its),
            "creada_en": _iso(c.creada_en), "creada_por": c.creada_por,
            "cerrada_en": _iso(c.cerrada_en), "cerrada_por": c.cerrada_por,
            "impresa_veces": c.impresa_veces or 0,
            "primera_impresion_en": _iso(c.primera_impresion_en),
            "ultima_impresion_en": _iso(c.ultima_impresion_en), "ultima_impresion_por": c.ultima_impresion_por,
            "piezas": sum(i.cantidad for i in its),
            "items": [{"linea_id": i.linea_id, "cantidad": i.cantidad,
                       "codigo": por_linea[i.linea_id].codigo if i.linea_id in por_linea else "",
                       "descripcion": por_linea[i.linea_id].descripcion if i.linea_id in por_linea else "",
                       "marca": por_linea[i.linea_id].marca if i.linea_id in por_linea else ""} for i in its],
        })
    evs = list((await db.execute(select(OVEvento).where(OVEvento.ov_id == ov.id).order_by(OVEvento.en, OVEvento.id))).scalars())
    ahora = datetime.utcnow()
    etapas = []
    for i, e in enumerate(evs):
        fin = evs[i + 1].en if i + 1 < len(evs) else (None if e.estatus in _FINALES else ahora)
        etapas.append({"estatus": e.estatus, "desde": _iso(e.en), "hasta": _iso(fin) if i + 1 < len(evs) else None,
                       "segundos": int((fin - e.en).total_seconds()) if fin else None, "por": e.por, "nota": e.nota,
                       "en_curso": i + 1 == len(evs) and e.estatus not in _FINALES})
    d = _ov_ser(ov, ls, ahora, len(cajas))
    d["lineas"] = [_l(l) for l in ls]
    d["cajas_detalle"] = cajas_ser
    d["eventos"] = etapas
    d["categorias"] = _categorias(ls)
    return d


def _categorias(ls):
    c = {}
    for l in ls:
        k = l.division or "Sin categoría"
        a = c.setdefault(k, {"nombre": k, "piezas": 0, "lineas": 0})
        a["piezas"] += l.cantidad or 0
        a["lineas"] += 1
    return sorted(c.values(), key=lambda a: -a["piezas"])


async def _pesos(db, ls):
    cods = list({l.codigo for l in ls} | {l.codigo_norm for l in ls})
    m = {}
    if cods:
        r = await db.execute(select(CatalogoItem).where(CatalogoItem.sku.in_(cods)))
        for c in r.scalars():
            if c.peso_unit:
                m[norm_codigo(c.sku)] = c.peso_unit
    return m


@router.get("/{ov_id}")
async def detalle(ov_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    return await _detalle(db, await _get_ov(db, ov_id))


# ───────────────────────── surtido ─────────────────────────
class EscaneoIn(BaseModel):
    codigo: str
    cantidad: int = 1


class CantidadIn(BaseModel):
    cantidad: int
    motivo: Optional[str] = None


async def _auto_surtida(db, ov, ls, user):
    if all((l.surtida or 0) >= (l.cantidad or 0) for l in ls) and ov.estatus == "en_surtido":
        await _cambiar(db, ov, "surtida", user, "Surtido completo")


@router.post("/{ov_id}/surtido/escanear")
async def surtir_escanear(ov_id: str, body: EscaneoIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    if ov.estatus not in ("recibida", "en_surtido"):
        raise HTTPException(409, "Esta OV ya no está en surtido.")
    if body.cantidad < 1:
        raise HTTPException(400, "La cantidad debe ser al menos 1.")
    ls = await _lineas(db, ov_id)
    l, existe = _cod_linea(ls, body.codigo, lambda x: (x.surtida or 0) < (x.cantidad or 0))
    if not existe:
        raise HTTPException(404, f"El código {body.codigo.strip()} no está en esta OV.")
    if not l:
        raise HTTPException(409, "Ese producto ya está surtido completo.")
    q = min(body.cantidad, (l.cantidad or 0) - (l.surtida or 0))
    if body.cantidad > q:
        raise HTTPException(409, f"Solo faltan {q} pieza(s) de ese producto; escaneaste {body.cantidad}.")
    if ov.estatus == "recibida":
        await _cambiar(db, ov, "en_surtido", user, "Primer escaneo")
    l.surtida = (l.surtida or 0) + q
    db.add(OVEscaneo(ov_id=ov_id, linea_id=l.id, etapa="surtido", cantidad=q, codigo=body.codigo.strip(), metodo="scan", usuario=user["sub"]))
    await _auto_surtida(db, ov, ls, user)
    await db.commit()
    return {"linea": _l(l), "ov": _ov_ser(ov, ls)}


@router.post("/{ov_id}/surtido/linea/{lid}")
async def surtir_linea(ov_id: str, lid: int, body: CantidadIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Fija la cantidad surtida de una partida (confirmar el total o corregir)."""
    ov = await _get_ov(db, ov_id)
    if ov.estatus not in ("recibida", "en_surtido"):
        raise HTTPException(409, "Esta OV ya no está en surtido.")
    ls = await _lineas(db, ov_id)
    l = next((x for x in ls if x.id == lid), None)
    if not l:
        raise HTTPException(404, "No existe esa partida.")
    if body.cantidad < 0 or body.cantidad > (l.cantidad or 0):
        raise HTTPException(400, f"La cantidad debe estar entre 0 y {l.cantidad}.")
    if (l.empacada or 0) > body.cantidad:
        raise HTTPException(409, "Ya hay piezas empacadas de esta partida.")
    dif = body.cantidad - (l.surtida or 0)
    if dif == 0:
        return {"linea": _l(l), "ov": _ov_ser(ov, ls)}
    if ov.estatus == "recibida":
        await _cambiar(db, ov, "en_surtido", user, "Primer movimiento")
    l.surtida = body.cantidad
    db.add(OVEscaneo(ov_id=ov_id, linea_id=l.id, etapa="surtido", cantidad=dif, codigo=l.codigo,
                     metodo="total" if body.cantidad == l.cantidad else "manual", usuario=user["sub"]))
    await _auto_surtida(db, ov, ls, user)
    await db.commit()
    return {"linea": _l(l), "ov": _ov_ser(ov, ls)}


@router.post("/{ov_id}/surtido/terminar")
async def surtido_terminar(ov_id: str, body: CantidadIn = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Cierra el surtido. Con faltantes: solo gerente/admin y con motivo."""
    ov = await _get_ov(db, ov_id)
    if ov.estatus not in ("recibida", "en_surtido"):
        raise HTTPException(409, "Esta OV ya no está en surtido.")
    ls = await _lineas(db, ov_id)
    falt = [l for l in ls if (l.surtida or 0) < (l.cantidad or 0)]
    if falt:
        _exigir(user, *ROLES_JEFE)
        motivo = ((body.motivo if body else "") or "").strip()
        if len(motivo) < 3:
            raise HTTPException(400, "Escribe el motivo de los faltantes.")
        if not any((l.surtida or 0) > 0 for l in ls):
            raise HTTPException(400, "No hay nada surtido; cancela la OV si no se va a surtir.")
        ov.nota_faltantes = motivo
        for l in falt:
            l.motivo_faltante = motivo[:200]
        await _cambiar(db, ov, "surtida", user, f"Surtido con faltantes: {motivo}")
    else:
        await _cambiar(db, ov, "surtida", user, "Surtido completo")
    await db.commit()
    return {"ov": _ov_ser(ov, ls)}


# ───────────────────────── preparacion ─────────────────────────
async def _caja(db, ov_id, cid) -> OVCaja:
    c = await db.get(OVCaja, cid)
    if not c or c.ov_id != ov_id:
        raise HTTPException(404, "No existe esa caja.")
    return c


async def _asegurar_prep(db, ov, user):
    if ov.estatus == "surtida":
        await _cambiar(db, ov, "en_preparacion", user, "Inicio de preparación")
    elif ov.estatus != "en_preparacion":
        raise HTTPException(409, "La OV no está en preparación (primero debe estar surtida).")


@router.post("/{ov_id}/preparacion/iniciar")
async def prep_iniciar(ov_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    await _asegurar_prep(db, ov, user)
    await db.commit()
    return {"ov": _ov_ser(ov, await _lineas(db, ov_id))}


@router.post("/{ov_id}/preparacion/cajas")
async def caja_nueva(ov_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    await _asegurar_prep(db, ov, user)
    abiertas = (await db.execute(select(func.count()).select_from(OVCaja).where(OVCaja.ov_id == ov_id, OVCaja.estatus == "abierta"))).scalar() or 0
    if abiertas:
        raise HTTPException(409, "Ya hay una caja abierta; ciérrala antes de abrir otra.")
    n = ((await db.execute(select(func.max(OVCaja.numero)).where(OVCaja.ov_id == ov_id))).scalar() or 0) + 1
    c = OVCaja(ov_id=ov_id, numero=n, estatus="abierta", creada_por=user["sub"], creada_en=datetime.utcnow())
    db.add(c)
    await db.commit()
    return {"caja_id": c.id, "numero": n}


async def _meter(db, ov, c, l, q, user, metodo):
    libre = (l.surtida or 0) - (l.empacada or 0)
    if q > libre:
        raise HTTPException(409, f"De ese producto solo quedan {libre} pieza(s) por empacar.")
    it = (await db.execute(select(OVCajaItem).where(OVCajaItem.caja_id == c.id, OVCajaItem.linea_id == l.id))).scalars().first()
    if it:
        it.cantidad += q
    else:
        db.add(OVCajaItem(caja_id=c.id, linea_id=l.id, cantidad=q))
    l.empacada = (l.empacada or 0) + q
    db.add(OVEscaneo(ov_id=ov.id, linea_id=l.id, caja_id=c.id, etapa="preparacion", cantidad=q, codigo=l.codigo, metodo=metodo, usuario=user["sub"]))


@router.post("/{ov_id}/preparacion/cajas/{cid}/escanear")
async def caja_escanear(ov_id: str, cid: int, body: EscaneoIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "en_preparacion":
        raise HTTPException(409, "La OV no está en preparación.")
    c = await _caja(db, ov_id, cid)
    if c.estatus != "abierta":
        raise HTTPException(409, "Esa caja ya está cerrada.")
    if body.cantidad < 1:
        raise HTTPException(400, "La cantidad debe ser al menos 1.")
    ls = await _lineas(db, ov_id)
    l, existe = _cod_linea(ls, body.codigo, lambda x: (x.surtida or 0) - (x.empacada or 0) > 0)
    if not existe:
        raise HTTPException(404, f"El código {body.codigo.strip()} no está en esta OV.")
    if not l:
        raise HTTPException(409, "De ese producto ya no quedan piezas por empacar.")
    await _meter(db, ov, c, l, body.cantidad, user, "scan")
    await db.commit()
    return await _detalle(db, ov)


@router.post("/{ov_id}/preparacion/cajas/{cid}/quitar")
async def caja_quitar(ov_id: str, cid: int, linea_id: int, cantidad: int = 1, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    c = await _caja(db, ov_id, cid)
    if c.estatus != "abierta":
        raise HTTPException(409, "Esa caja ya está cerrada.")
    it = (await db.execute(select(OVCajaItem).where(OVCajaItem.caja_id == cid, OVCajaItem.linea_id == linea_id))).scalars().first()
    if not it or it.cantidad < cantidad or cantidad < 1:
        raise HTTPException(400, "No hay esa cantidad en la caja.")
    l = await db.get(OVLinea, linea_id)
    it.cantidad -= cantidad
    l.empacada = max(0, (l.empacada or 0) - cantidad)
    db.add(OVEscaneo(ov_id=ov_id, linea_id=linea_id, caja_id=cid, etapa="preparacion", cantidad=-cantidad, codigo=l.codigo, metodo="manual", usuario=user["sub"]))
    await db.commit()
    return await _detalle(db, ov)


class CerrarCajaIn(BaseModel):
    peso_kg: Optional[float] = None


@router.post("/{ov_id}/preparacion/cajas/{cid}/cerrar")
async def caja_cerrar(ov_id: str, cid: int, body: CerrarCajaIn = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    c = await _caja(db, ov_id, cid)
    if c.estatus != "abierta":
        raise HTTPException(409, "Esa caja ya está cerrada.")
    n = (await db.execute(select(func.coalesce(func.sum(OVCajaItem.cantidad), 0)).where(OVCajaItem.caja_id == cid))).scalar() or 0
    if n < 1:
        raise HTTPException(400, "La caja está vacía.")
    c.estatus = "cerrada"
    c.cerrada_en = datetime.utcnow()
    c.cerrada_por = user["sub"]
    if body and body.peso_kg and body.peso_kg > 0:
        c.peso_kg = float(body.peso_kg)
    ls = await _lineas(db, ov_id)
    if all((l.empacada or 0) >= (l.surtida or 0) for l in ls):
        await _cambiar(db, ov, "preparada", user, "Todo lo surtido quedó en cajas")
    await db.commit()
    return await _detalle(db, ov)


@router.post("/{ov_id}/preparacion/cajas/{cid}/reabrir")
async def caja_reabrir(ov_id: str, cid: int, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    ov = await _get_ov(db, ov_id)
    if ov.estatus not in ("en_preparacion", "preparada"):
        raise HTTPException(409, "Ya no se puede reabrir: la OV avanzó de etapa.")
    c = await _caja(db, ov_id, cid)
    if (await db.execute(select(func.count()).select_from(OVCaja).where(OVCaja.ov_id == ov_id, OVCaja.estatus == "abierta"))).scalar():
        raise HTTPException(409, "Hay otra caja abierta; ciérrala primero.")
    c.estatus = "abierta"
    c.cerrada_en = None
    if ov.estatus == "preparada":
        await _cambiar(db, ov, "en_preparacion", user, f"Reapertura de la caja {c.numero}")
    await db.commit()
    return await _detalle(db, ov)


@router.post("/{ov_id}/preparacion/terminar")
async def prep_terminar(ov_id: str, body: CantidadIn = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Termina la preparacion aunque queden piezas surtidas sin empacar (solo gerente/admin, con motivo)."""
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "en_preparacion":
        raise HTTPException(409, "La OV no está en preparación.")
    if (await db.execute(select(func.count()).select_from(OVCaja).where(OVCaja.ov_id == ov_id, OVCaja.estatus == "abierta"))).scalar():
        raise HTTPException(409, "Cierra la caja abierta antes de terminar.")
    ls = await _lineas(db, ov_id)
    if not any((l.empacada or 0) > 0 for l in ls):
        raise HTTPException(400, "Todavía no hay piezas en cajas.")
    if any((l.empacada or 0) < (l.surtida or 0) for l in ls):
        _exigir(user, *ROLES_JEFE)
        motivo = ((body.motivo if body else "") or "").strip()
        if len(motivo) < 3:
            raise HTTPException(400, "Escribe el motivo de las piezas que no se empacaron.")
        await _cambiar(db, ov, "preparada", user, f"Preparación con piezas sin empacar: {motivo}")
    else:
        await _cambiar(db, ov, "preparada", user, "Preparación completa")
    await db.commit()
    return {"ov": _ov_ser(ov, ls)}


# ───────────────────────── etiquetas de caja ─────────────────────────
def _barcode_url(data: str, height: int = 50) -> str:
    import urllib.parse
    return ("https://barcode.tec-it.com/barcode.ashx?data=" + urllib.parse.quote(data)
            + f"&code=Code128&dpi=200&unit=Fit&width=280&height={height}&quiet=0&color=%23000000")


def _etq(d: dict, caja: dict, total: int) -> dict:
    """Misma forma que la etiqueta de carga agrupada (Etiquetas.jsx)."""
    peso = caja.get("peso_kg") or (caja["peso_calc_kg"] if caja.get("peso_calc_completo") else 0) or 0
    ov = d["num_ov"]
    cod_caja = f"{ov}-C{caja['numero']}"
    prods = [{"clave": i["codigo"], "descripcion": i["descripcion"], "cantidad_total": i["cantidad"]} for i in caja["items"]]
    ciudad = d.get("destino_ciudad") or ""
    return {
        "id_tarima": f"{ov}-C{caja['numero']}", "id_caja": caja["id"], "tipo_bulto": "caja",
        "numero_tarima": str(caja["numero"]), "total_tarimas": total, "total_bultos": total, "es_mix": False,
        "remitente": d.get("emisor") or "", "nombre_cliente": d.get("cliente"), "sucursal": ciudad,
        "direccion": d.get("destino") or "", "num_entrega": ov, "orden": "", "fecha_entrega": d.get("fecha_entrega") or "",
        "doc_principal": "OV", "sin_palet": True, "referencia": d.get("referencia") or "",
        "peso_neto_kg": peso, "peso_palet_kg": 0, "peso_bruto_kg": peso,
        "largo_cm": 0, "ancho_cm": 0, "alto_cm": 0,
        "productos": prods,
        "barcode_entrega": ov, "barcode_entrega_url": _barcode_url(ov),
        "barcode_tarima": cod_caja, "barcode_tarima_url": _barcode_url(cod_caja),
        "impresa_veces": caja.get("impresa_veces") or 0,
        "primera_impresion_en": caja.get("primera_impresion_en"),
        "ultima_impresion_en": caja.get("ultima_impresion_en"),
        "ultima_impresion_por": caja.get("ultima_impresion_por"),
    }


@router.get("/{ov_id}/etiquetas")
async def etiquetas(ov_id: str, caja_id: Optional[int] = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    d = await _detalle(db, ov)
    cerradas = [c for c in d["cajas_detalle"] if c["estatus"] == "cerrada"]
    if caja_id is not None:
        cerradas = [c for c in cerradas if c["id"] == caja_id]
        if not cerradas:
            raise HTTPException(409, "Cierra la caja para imprimir su etiqueta.")
    if not cerradas:
        raise HTTPException(409, "Todavía no hay cajas cerradas.")
    total = len([c for c in d["cajas_detalle"] if c["estatus"] == "cerrada"])
    return [_etq(d, c, total) for c in cerradas]


class ImpresionIn(BaseModel):
    caja_ids: list[int]
    motivo: Optional[str] = None


@router.post("/{ov_id}/impresion")
async def cajas_impresion(ov_id: str, body: ImpresionIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Registra la impresion de una o varias cajas. La primera vez es libre; reimprimir solo gerente/admin."""
    await _get_ov(db, ov_id)
    cajas = [await _caja(db, ov_id, cid) for cid in body.caja_ids]
    if any((c.impresa_veces or 0) >= 1 for c in cajas):
        _exigir(user, *ROLES_JEFE)
    ahora = datetime.utcnow()
    for c in cajas:
        c.impresa_veces = (c.impresa_veces or 0) + 1
        c.primera_impresion_en = c.primera_impresion_en or ahora
        c.ultima_impresion_en = ahora
        c.ultima_impresion_por = user["sub"]
    await db.commit()
    return {"ok": True, "cajas": len(cajas)}


# ───────────────────────── entrega SAP ─────────────────────────
def _conciliar(ls, en: dict) -> dict:
    por = {}
    for p in en["partidas"]:
        a = por.setdefault(p["codigo_norm"], {"cantidad": 0, "lotes": [], "descripcion": p["descripcion"], "codigo": p["codigo"]})
        a["cantidad"] += p["cantidad"]
        a["lotes"] += p["lotes"]
    dif, ok, sin_ent = [], 0, []
    vistos = set()
    for l in ls:
        if l.codigo_norm in vistos:
            continue
        vistos.add(l.codigo_norm)
        emp = sum(x.empacada or 0 for x in ls if x.codigo_norm == l.codigo_norm)
        ped = sum(x.cantidad or 0 for x in ls if x.codigo_norm == l.codigo_norm)
        ent = por.get(l.codigo_norm, {}).get("cantidad", 0)
        if ent == emp:
            if emp:
                ok += 1
            continue
        fila = {"codigo": l.codigo, "descripcion": l.descripcion, "pedida": ped, "empacada": emp, "entrega": ent}
        (dif if ent else sin_ent).append(fila)
    ov_codes = {l.codigo_norm for l in ls}
    extra = [{"codigo": a["codigo"], "descripcion": a["descripcion"], "entrega": a["cantidad"]} for n, a in por.items() if n not in ov_codes]
    return {"coinciden": ok, "diferencias": dif + [{**x, "nota": "Se preparó pero no está en la entrega"} for x in sin_ent if x["empacada"]], "no_en_ov": extra,
            "lineas_entrega": len(en["partidas"]), "limpia": not (dif or extra or any(x["empacada"] for x in sin_ent))}


@router.post("/{ov_id}/entrega/validar")
async def entrega_validar(ov_id: str, archivo: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_CARGA)
    ov = await _get_ov(db, ov_id)
    try:
        en = parsear_entrega(await archivo.read())
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer el PDF de la entrega: {e}")
    if not en["folio"] or not en["partidas"]:
        raise HTTPException(400, "El PDF no parece un informe de lotes de entrega de SAP.")
    ls = await _lineas(db, ov_id)
    out = {"folio": en["folio"], "fecha": en["fecha"], "cliente": en["cliente"], "autor": en["autor"], "avisos": en["avisos"],
           "conciliacion": _conciliar(ls, en)}
    if ov.cliente and en["cliente"] and norm_codigo(ov.cliente) != norm_codigo(en["cliente"]):
        out["avisos"].append(f"El cliente de la entrega ({en['cliente']}) no coincide con el de la OV ({ov.cliente}).")
    dup = (await db.execute(select(OrdenVenta.num_ov).where(OrdenVenta.entrega_folio == en["folio"], OrdenVenta.id != ov_id))).scalars().first()
    if dup:
        out["avisos"].append(f"El folio {en['folio']} ya está registrado en la OV {dup}.")
    return out


@router.post("/{ov_id}/entrega")
async def entrega_registrar(ov_id: str, folio: str = Form(""), confirmar_diferencias: bool = Form(False),
                            archivo: Optional[UploadFile] = File(None),
                            db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_CARGA)
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "preparada":
        raise HTTPException(409, "La entrega se registra cuando la OV está preparada.")
    ls = await _lineas(db, ov_id)
    en = None
    if archivo is not None and archivo.filename:
        try:
            en = parsear_entrega(await archivo.read())
        except Exception as e:
            raise HTTPException(400, f"No se pudo leer el PDF de la entrega: {e}")
        if not en["folio"]:
            raise HTTPException(400, "No se encontró el folio de entrega en el PDF.")
        if folio.strip() and folio.strip() != en["folio"]:
            raise HTTPException(400, f"El folio escrito ({folio.strip()}) no coincide con el del PDF ({en['folio']}).")
        folio = en["folio"]
    folio = folio.strip()
    if not folio:
        raise HTTPException(400, "Escribe el folio de la entrega o adjunta el PDF.")
    dup = (await db.execute(select(OrdenVenta.num_ov).where(OrdenVenta.entrega_folio == folio, OrdenVenta.id != ov_id))).scalars().first()
    if dup:
        raise HTTPException(409, f"El folio {folio} ya está registrado en la OV {dup}.")
    if en:
        con = _conciliar(ls, en)
        if not con["limpia"] and not confirmar_diferencias:
            raise HTTPException(409, "La entrega no coincide con lo preparado. Revisa las diferencias y confirma si es correcto.")
        por = {}
        for p in en["partidas"]:
            a = por.setdefault(p["codigo_norm"], {"cantidad": 0, "lotes": []})
            a["cantidad"] += p["cantidad"]
            a["lotes"] += p["lotes"]
        for l in ls:
            a = por.get(l.codigo_norm)
            l.entrega_cantidad = a["cantidad"] if a else 0
            l.entrega_lotes = json.dumps(a["lotes"]) if a else None
        ov.entrega_fecha = en["fecha"]
    ov.entrega_folio = folio
    ov.entrega_en = datetime.utcnow()
    ov.entrega_por = user["sub"]
    await _cambiar(db, ov, "entregada_sap", user, f"Entrega SAP {folio}")
    await db.commit()
    return {"ok": True, "ov": _ov_ser(ov, ls)}


# ───────────────────────── factura ─────────────────────────
@router.post("/{ov_id}/factura")
async def factura_registrar(ov_id: str, folio: str = Form(""), confirmar: bool = Form(False),
                            archivo: Optional[UploadFile] = File(None),
                            db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_FACTURA)
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "entregada_sap":
        raise HTTPException(409, "La factura se registra cuando la entrega de SAP ya está capturada.")
    uuid_, fecha = None, None
    if archivo is not None and archivo.filename:
        try:
            fa = parsear_factura(await archivo.read())
        except Exception as e:
            raise HTTPException(400, f"No se pudo leer el PDF de la factura: {e}")
        if not fa["folio"]:
            raise HTTPException(400, "No se encontró el folio en el PDF de la factura.")
        if folio.strip() and folio.strip() != fa["folio"]:
            raise HTTPException(400, f"El folio escrito ({folio.strip()}) no coincide con el del PDF ({fa['folio']}).")
        probl = []
        if fa["num_ov"] and fa["num_ov"] != ov.num_ov:
            probl.append(f"la factura dice OV {fa['num_ov']} y esta es la {ov.num_ov}")
        if fa["num_entrega"] and fa["num_entrega"] != ov.entrega_folio:
            probl.append(f"la factura dice entrega {fa['num_entrega']} y la registrada es {ov.entrega_folio}")
        if probl and not confirmar:
            raise HTTPException(409, "La factura no coincide: " + "; ".join(probl) + ".")
        folio, uuid_, fecha = fa["folio"], fa["uuid"], fa["fecha"]
    folio = folio.strip()
    if not folio:
        raise HTTPException(400, "Escribe el folio de la factura o adjunta el PDF.")
    ov.factura_folio, ov.factura_uuid, ov.factura_fecha = folio, uuid_, fecha
    ov.factura_en = datetime.utcnow()
    ov.factura_por = user["sub"]
    await _cambiar(db, ov, "facturada", user, f"Factura {folio}")
    await db.commit()
    return {"ok": True, "ov": _ov_ser(ov, await _lineas(db, ov_id))}


# ───────────────────────── cancelar ─────────────────────────
@router.post("/{ov_id}/cancelar")
async def cancelar(ov_id: str, body: CantidadIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    ov = await _get_ov(db, ov_id)
    if ov.estatus in _FINALES:
        raise HTTPException(409, "Esa OV ya está cerrada.")
    motivo = (body.motivo or "").strip()
    if len(motivo) < 3:
        raise HTTPException(400, "Escribe el motivo de la cancelación.")
    await _cambiar(db, ov, "cancelada", user, motivo)
    await db.commit()
    return {"ok": True}
