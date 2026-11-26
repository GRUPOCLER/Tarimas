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
import math
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import (OVAdjunto, OVConfig, Usuario, UsuarioModulo, OrdenVenta, OVLinea, OVEvento, OVEscaneo, OVCaja, OVCajaItem,
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
    {"id": "preparada",      "nombre": "Entrega en sistema pendiente", "umbral_horas": 4},
    {"id": "entregada_sap",  "nombre": "Entrega generada, en espera de facturación", "umbral_horas": 24},
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


# ───────────────────────── responsables por etapa ─────────────────────────
ETAPAS_ASIG = {
    "surtido": ("recibida", "en_surtido"),
    "prep": ("recibida", "en_surtido", "surtida", "en_preparacion"),
}
_ART = {"surtido": "el surtido", "prep": "la preparación"}
_NOM = {"surtido": "surtido", "prep": "preparación"}


def _es_jefe(user) -> bool:
    return user.get("rol") in ROLES_JEFE


async def _exigir_asignacion(db) -> bool:
    c = await db.get(OVConfig, "exigir_asignacion")
    return bool(c and c.valor == "1")


async def _nombres(db) -> dict:
    r = await db.execute(select(Usuario.usuario, Usuario.nombre_display))
    return {u.lower(): (n or u) for u, n in r.all()}


async def _operadores(db) -> list:
    """Usuarios activos con rol operador en Refacciones (a quienes se les puede asignar trabajo)."""
    r = await db.execute(
        select(Usuario.usuario, Usuario.nombre_display)
        .join(UsuarioModulo, UsuarioModulo.usuario == Usuario.usuario)
        .where(UsuarioModulo.modulo == "refacciones", UsuarioModulo.rol == "operador", Usuario.activo == True)  # noqa: E712
        .order_by(Usuario.nombre_display, Usuario.usuario))
    return [{"email": u.lower(), "usuario": u.lower(), "nombre": n or u} for u, n in r.all()]


async def _puede_operar(db, ov, etapa: str, user) -> dict:
    """Los jefes siempre; el resto solo si es el responsable (o si la OV no tiene y no se exige asignacion)."""
    if _es_jefe(user):
        return {"ok": True}
    asig = (getattr(ov, "asig_" + etapa) or "").lower()
    yo = (user.get("sub") or "").lower()
    if asig:
        if asig == yo:
            return {"ok": True}
        nom = (await _nombres(db)).get(asig, asig)
        return {"ok": False, "motivo": f"Esta OV está asignada a {nom} para {_ART[etapa]}. Pide a un gerente que la reasigne."}
    if await _exigir_asignacion(db):
        return {"ok": False, "motivo": f"Esta OV todavía no tiene responsable de {_NOM[etapa]}. Un gerente debe asignarla."}
    return {"ok": True}


async def _exigir_operar(db, ov, etapa: str, user):
    r = await _puede_operar(db, ov, etapa, user)
    if not r["ok"]:
        raise HTTPException(403, r["motivo"])


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
        "asig_surtido": ov.asig_surtido, "asig_surtido_en": _iso(ov.asig_surtido_en), "asig_surtido_por": ov.asig_surtido_por,
        "asig_prep": ov.asig_prep, "asig_prep_en": _iso(ov.asig_prep_en), "asig_prep_por": ov.asig_prep_por,
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
        for tabla in (OVAdjunto, OVEscaneo, OVEvento, OVLinea):
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
        "nombres": await _nombres(db),
    }


async def _detalle(db, ov: OrdenVenta, user=None) -> dict:
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
    d["nombres"] = await _nombres(db)
    d["exigir_asignacion"] = await _exigir_asignacion(db)
    if user is not None:
        if _es_jefe(user):
            d["operadores"] = await _operadores(db)
        d["operar"] = {"surtido": await _puede_operar(db, ov, "surtido", user), "prep": await _puede_operar(db, ov, "prep", user)}
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
    return await _detalle(db, await _get_ov(db, ov_id), user)


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
    await _exigir_operar(db, ov, "surtido", user)
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
    await _exigir_operar(db, ov, "surtido", user)
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
    await _exigir_operar(db, ov, "surtido", user)
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
    if ov.estatus in ("surtida", "en_preparacion"):
        await _exigir_operar(db, ov, "prep", user)
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
    await _exigir_operar(db, ov, "prep", user)
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
    return await _detalle(db, ov, user)


class AsignarItemIn(BaseModel):
    linea_id: int
    cantidad: Optional[int] = None      # None = todo lo pendiente de esa partida


class AsignarCajaIn(BaseModel):
    items: list[AsignarItemIn] = []
    todo: bool = False                  # True = todo lo surtido y no empacado de la OV


@router.post("/{ov_id}/preparacion/cajas/{cid}/asignar")
async def caja_asignar(ov_id: str, cid: int, body: AsignarCajaIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Asigna a la caja abierta varias partidas a la vez (total, parcial o mezcla). Es atomico: si una falla, no se mueve nada."""
    ov = await _get_ov(db, ov_id)
    await _exigir_operar(db, ov, "prep", user)
    if ov.estatus != "en_preparacion":
        raise HTTPException(409, "La OV no está en preparación.")
    c = await _caja(db, ov_id, cid)
    if c.estatus != "abierta":
        raise HTTPException(409, "Esa caja ya está cerrada.")
    ls = {l.id: l for l in await _lineas(db, ov_id)}
    pedido = []
    if body.todo:
        pedido = [(l, (l.surtida or 0) - (l.empacada or 0)) for l in ls.values()]
    else:
        if not body.items:
            raise HTTPException(400, "Elige al menos una partida.")
        vistos = set()
        for it in body.items:
            l = ls.get(it.linea_id)
            if not l:
                raise HTTPException(404, "Una de las partidas no pertenece a esta OV.")
            if it.linea_id in vistos:
                raise HTTPException(400, f"La partida {l.codigo} viene repetida.")
            vistos.add(it.linea_id)
            libre = (l.surtida or 0) - (l.empacada or 0)
            q = libre if it.cantidad is None else it.cantidad
            if q < 1:
                raise HTTPException(400, f"{l.codigo}: la cantidad debe ser al menos 1.")
            pedido.append((l, q))
    pedido = [(l, q) for l, q in pedido if q > 0]
    if not pedido:
        raise HTTPException(409, "No hay piezas por empacar.")
    for l, q in pedido:                 # _meter valida contra lo pendiente; cualquier error aborta todo
        await _meter(db, ov, c, l, q, user, "lote")
    await db.commit()
    return await _detalle(db, ov, user)


@router.post("/{ov_id}/preparacion/cajas/{cid}/vaciar")
async def caja_vaciar(ov_id: str, cid: int, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Saca todo lo que tiene la caja abierta (regresa las piezas a 'por empacar')."""
    ov = await _get_ov(db, ov_id)
    await _exigir_operar(db, ov, "prep", user)
    c = await _caja(db, ov_id, cid)
    if c.estatus != "abierta":
        raise HTTPException(409, "Esa caja ya está cerrada.")
    for it in (await db.execute(select(OVCajaItem).where(OVCajaItem.caja_id == cid, OVCajaItem.cantidad > 0))).scalars():
        l = await db.get(OVLinea, it.linea_id)
        l.empacada = max(0, (l.empacada or 0) - it.cantidad)
        db.add(OVEscaneo(ov_id=ov_id, linea_id=it.linea_id, caja_id=cid, etapa="preparacion", cantidad=-it.cantidad, codigo=l.codigo, metodo="manual", usuario=user["sub"]))
        it.cantidad = 0
    await db.commit()
    return await _detalle(db, ov, user)


@router.post("/{ov_id}/preparacion/cajas/{cid}/quitar")
async def caja_quitar(ov_id: str, cid: int, linea_id: int, cantidad: int = 1, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    await _exigir_operar(db, ov, "prep", user)
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
    return await _detalle(db, ov, user)


class CerrarCajaIn(BaseModel):
    peso_kg: Optional[float] = None


@router.post("/{ov_id}/preparacion/cajas/{cid}/cerrar")
async def caja_cerrar(ov_id: str, cid: int, body: CerrarCajaIn = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ov = await _get_ov(db, ov_id)
    await _exigir_operar(db, ov, "prep", user)
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
    return await _detalle(db, ov, user)


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
    return await _detalle(db, ov, user)


@router.post("/{ov_id}/preparacion/terminar")
async def prep_terminar(ov_id: str, body: CantidadIn = None, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Termina la preparacion aunque queden piezas surtidas sin empacar (solo gerente/admin, con motivo)."""
    ov = await _get_ov(db, ov_id)
    await _exigir_operar(db, ov, "prep", user)
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
    out["cliente_coincide"] = not (ov.cliente and en["cliente"] and norm_codigo(ov.cliente) != norm_codigo(en["cliente"]))
    out["folio_duplicado"] = bool(dup)
    out["corresponde"] = bool(out["conciliacion"]["limpia"] and out["cliente_coincide"] and not dup)
    out["puede_autorizar"] = user.get("rol") in ROLES_JEFE
    return out


@router.post("/{ov_id}/entrega")
async def entrega_registrar(ov_id: str, folio: str = Form(""), confirmar_diferencias: bool = Form(False), motivo: str = Form(""),
                            archivo: Optional[UploadFile] = File(None),
                            db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """La entrega solo se da por generada con el PDF de SAP, y solo si corresponde a lo preparado (mismas partidas y cantidades,
    mismo cliente, folio no usado). Una diferencia o un registro sin PDF solo los autoriza un gerente/admin, con motivo."""
    _exigir(user, *ROLES_CARGA)
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "preparada":
        raise HTTPException(409, "La entrega se registra cuando la OV está preparada.")
    es_jefe = user.get("rol") in ROLES_JEFE
    motivo = (motivo or "").strip()
    ls = await _lineas(db, ov_id)
    en, datos = None, None
    if archivo is not None and archivo.filename:
        datos = await archivo.read()
        if not datos.lstrip()[:5].startswith(b"%PDF"):
            raise HTTPException(400, "El archivo no es un PDF.")
        if len(datos) > MAX_ADJUNTO:
            raise HTTPException(413, "El archivo pesa más de 9 MB.")
        try:
            en = parsear_entrega(datos)
        except Exception as e:
            raise HTTPException(400, f"No se pudo leer el PDF de la entrega: {e}")
        if not en["folio"]:
            raise HTTPException(400, "No se encontró el folio de entrega en el PDF.")
        if folio.strip() and folio.strip() != en["folio"]:
            raise HTTPException(400, f"El folio escrito ({folio.strip()}) no coincide con el del PDF ({en['folio']}).")
        folio = en["folio"]
    folio = folio.strip()
    if not en:
        if not (es_jefe and len(motivo) >= 5):
            raise HTTPException(409, "Falta el PDF de la entrega generada en SAP. Sin él no se puede cerrar la entrega ni pasar a facturación. "
                                     "Solo un gerente puede registrarla sin PDF, con motivo.")
        if not folio:
            raise HTTPException(400, "Escribe el folio de la entrega.")
    dup = (await db.execute(select(OrdenVenta.num_ov).where(OrdenVenta.entrega_folio == folio, OrdenVenta.id != ov_id))).scalars().first()
    if dup:
        raise HTTPException(409, f"El folio {folio} ya está registrado en la OV {dup}.")
    nota = f"Entrega SAP {folio}"
    if en:
        con = _conciliar(ls, en)
        problemas = []
        if not con["limpia"]:
            problemas.append("las partidas o cantidades no coinciden con lo preparado")
        if ov.cliente and en["cliente"] and norm_codigo(ov.cliente) != norm_codigo(en["cliente"]):
            problemas.append(f"el cliente de la entrega ({en['cliente']}) no es el de la OV ({ov.cliente})")
        if problemas:
            if not es_jefe:
                raise HTTPException(409, "La entrega no coincide con la OV: " + "; ".join(problemas) + ". Solo un gerente puede autorizarla; avísale para que la valide.")
            if not (confirmar_diferencias and len(motivo) >= 5):
                raise HTTPException(409, "La entrega no coincide con la OV: " + "; ".join(problemas) + ". Para autorizarla confirma y escribe el motivo.")
            nota += f" (autorizada con diferencias por {user['sub']}: {motivo[:150]})"
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
        # El PDF validado queda guardado junto con la OV en la misma operacion (no depende de otra llamada).
        await db.execute(delete(OVAdjunto).where(OVAdjunto.ov_id == ov_id, OVAdjunto.tipo == "entrega_pdf"))
        nombre = re.sub(r'[\\/:*?"<>|]+', "_", archivo.filename or "entrega.pdf")[:120]
        db.add(OVAdjunto(ov_id=ov_id, tipo="entrega_pdf", nombre=nombre, mime="application/pdf", tam=len(datos), subido_por=user["sub"], contenido=datos))
    else:
        nota += f" (SIN PDF, autorizada por {user['sub']}: {motivo[:150]})"
    ov.entrega_folio = folio
    ov.entrega_en = datetime.utcnow()
    ov.entrega_por = user["sub"]
    await _cambiar(db, ov, "entregada_sap", user, nota)
    await db.commit()
    return {"ok": True, "ov": _ov_ser(ov, ls)}


# ───────────────────────── factura ─────────────────────────
@router.post("/{ov_id}/factura")
async def factura_registrar(ov_id: str, folio: str = Form(""), confirmar: bool = Form(False),
                            archivo: Optional[UploadFile] = File(None),
                            xml: Optional[UploadFile] = File(None),
                            db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_FACTURA)
    ov = await _get_ov(db, ov_id)
    if ov.estatus != "entregada_sap":
        raise HTTPException(409, "La factura se registra cuando la entrega de SAP ya está capturada.")
    tiene_pdf = (await db.execute(select(func.count()).select_from(OVAdjunto).where(OVAdjunto.ov_id == ov_id, OVAdjunto.tipo == "entrega_pdf"))).scalar()
    excepcion = (await db.execute(select(func.count()).select_from(OVEvento).where(OVEvento.ov_id == ov_id, OVEvento.nota.like("%(SIN PDF%")))).scalar()
    if not tiene_pdf and not excepcion:
        raise HTTPException(409, "Falta el PDF de la entrega generada en SAP. Pídele al encargado de SAP que lo adjunte en Archivos.")
    uuid_, fecha = None, None
    cf = None
    if xml is not None and xml.filename:
        cf = _leer_cfdi(await xml.read())
        if not cf["uuid"]:
            raise HTTPException(400, "El XML no trae el folio fiscal (UUID); revisa que sea una factura timbrada.")
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
        if cf and fa.get("uuid") and str(fa["uuid"]).upper() != cf["uuid"]:
            probl.append(f"el folio fiscal del XML ({cf['uuid']}) no es el del PDF ({str(fa['uuid']).upper()})")
        if fa["num_ov"] and fa["num_ov"] != ov.num_ov:
            probl.append(f"la factura dice OV {fa['num_ov']} y esta es la {ov.num_ov}")
        if fa["num_entrega"] and fa["num_entrega"] != ov.entrega_folio:
            probl.append(f"la factura dice entrega {fa['num_entrega']} y la registrada es {ov.entrega_folio}")
        if probl and not confirmar:
            raise HTTPException(409, "La factura no coincide: " + "; ".join(probl) + ".")
        folio, uuid_, fecha = fa["folio"], fa["uuid"], fa["fecha"]
    elif cf and not folio.strip():
        folio = (cf["serie"] + cf["folio"]).strip()
    if cf:
        uuid_ = cf["uuid"]
        fecha = fecha or (cf["fecha"] or "")[:10]
        dup = (await db.execute(select(OrdenVenta.num_ov).where(OrdenVenta.factura_uuid == uuid_, OrdenVenta.id != ov.id))).scalars().first()
        if dup:
            raise HTTPException(409, f"Ese folio fiscal ya está registrado en la OV {dup}.")
    folio = folio.strip()
    if not folio:
        raise HTTPException(400, "Escribe el folio de la factura o adjunta el PDF o el XML.")
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


# ───────────────────────── CFDI (XML de factura) ─────────────────────────
def _leer_cfdi(datos: bytes) -> dict:
    import xml.etree.ElementTree as ET
    txt = datos.decode("utf-8-sig", errors="replace")
    if "<!DOCTYPE" in txt.upper() or "<!ENTITY" in txt.upper():
        raise HTTPException(400, "El XML no es válido.")
    try:
        raiz = ET.fromstring(txt)
    except ET.ParseError:
        raise HTTPException(400, "El XML está dañado o no es un XML válido.")

    def busca(nombre):
        for el in raiz.iter():
            if el.tag.split("}")[-1] == nombre:
                return el
        return None
    comp = raiz if raiz.tag.split("}")[-1] == "Comprobante" else busca("Comprobante")
    if comp is None:
        raise HTTPException(400, "El archivo no parece un XML de factura (CFDI).")
    tim, em, re_ = busca("TimbreFiscalDigital"), busca("Emisor"), busca("Receptor")
    a = lambda el, k: (el.get(k) or "").strip() if el is not None else ""
    try:
        total = float(a(comp, "Total") or 0)
    except ValueError:
        total = 0.0
    return {"uuid": a(tim, "UUID").upper(), "serie": a(comp, "Serie"), "folio": a(comp, "Folio"), "fecha": a(comp, "Fecha"),
            "total": total, "moneda": a(comp, "Moneda"), "rfc_emisor": a(em, "Rfc"), "nombre_emisor": a(em, "Nombre"),
            "rfc_receptor": a(re_, "Rfc"), "nombre_receptor": a(re_, "Nombre")}


# ───────────────────────── asignacion de responsables ─────────────────────────
class AsignarIn(BaseModel):
    etapa: str
    usuario: Optional[str] = ""


class ExigirIn(BaseModel):
    exigir: bool


@router.post("/{ov_id}/asignar")
async def asignar(ov_id: str, body: AsignarIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    if body.etapa not in ETAPAS_ASIG:
        raise HTTPException(400, "Etapa inválida.")
    ov = await _get_ov(db, ov_id)
    if ov.estatus not in ETAPAS_ASIG[body.etapa]:
        raise HTTPException(409, f'Ya no se puede cambiar el responsable de {_NOM[body.etapa]}: la OV está en "{ov.estatus.replace("_", " ")}".')
    u = (body.usuario or "").strip().lower()
    if u:
        if u not in {o["usuario"] for o in await _operadores(db)}:
            raise HTTPException(400, "Ese usuario no está activo o no es operador de Refacciones.")
    e = body.etapa
    setattr(ov, f"asig_{e}", u or None)
    setattr(ov, f"asig_{e}_en", datetime.utcnow() if u else None)
    setattr(ov, f"asig_{e}_por", user["sub"] if u else None)
    await db.commit()
    return {"ok": True, "ov": _ov_ser(ov, await _lineas(db, ov_id))}


@router.get("/asignacion/tablero")
async def asig_tablero(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    ovs = list((await db.execute(select(OrdenVenta).where(OrdenVenta.estatus.in_(ETAPAS_ASIG["prep"])).order_by(OrdenVenta.creado_en))).scalars())
    ids = [o.id for o in ovs]
    por = {}
    if ids:
        for l in (await db.execute(select(OVLinea).where(OVLinea.ov_id.in_(ids)))).scalars():
            por.setdefault(l.ov_id, []).append(l)
    ahora = datetime.utcnow()
    return {"operadores": await _operadores(db), "ovs": [_ov_ser(o, por.get(o.id, []), ahora) for o in ovs],
            "exigir": await _exigir_asignacion(db), "nombres": await _nombres(db)}


@router.post("/asignacion/config")
async def asig_config(body: ExigirIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, "admin")
    c = await db.get(OVConfig, "exigir_asignacion")
    if c:
        c.valor = "1" if body.exigir else "0"
    else:
        db.add(OVConfig(clave="exigir_asignacion", valor="1" if body.exigir else "0"))
    await db.commit()
    return {"ok": True, "exigir": body.exigir}


# ───────────────────────── KPI de tiempos ─────────────────────────
KPI_ETAPAS = [
    ("recibida", "Espera de surtido"), ("en_surtido", "Surtido"), ("surtida", "Espera de preparación"),
    ("en_preparacion", "Preparación"), ("preparada", "Espera de entrega SAP"), ("entregada_sap", "Espera de factura"),
]


def _pct(v, p):
    if not v:
        return None
    a = sorted(v)
    return a[min(len(a) - 1, max(0, math.ceil(p * len(a)) - 1))]


def _prom(v):
    return round(sum(v) / len(v)) if v else None


@router.get("/kpi/tiempos")
async def kpi_tiempos(desde: str = "", hasta: str = "", db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Tiempos por etapa y por responsable de las OV creadas entre desde y hasta (aaaa-mm-dd, hora de Ciudad de Mexico)."""
    _exigir(user, *ROLES_JEFE)
    import re
    from zoneinfo import ZoneInfo
    mx, utc = ZoneInfo("America/Mexico_City"), ZoneInfo("UTC")
    hoy = datetime.now(mx).date()
    ok = lambda x: bool(re.match(r"^\d{4}-\d{2}-\d{2}$", x or ""))
    desde = desde if ok(desde) else (hoy - timedelta(days=30)).isoformat()
    hasta = hasta if ok(hasta) else hoy.isoformat()
    try:
        t0 = datetime.fromisoformat(desde + "T00:00:00").replace(tzinfo=mx).astimezone(utc).replace(tzinfo=None)
        t1 = datetime.fromisoformat(hasta + "T23:59:59").replace(tzinfo=mx).astimezone(utc).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(400, "Fechas inválidas.")
    ovs = list((await db.execute(select(OrdenVenta).where(OrdenVenta.creado_en >= t0, OrdenVenta.creado_en <= t1, OrdenVenta.estatus != "cancelada"))).scalars())
    ids = [o.id for o in ovs]
    ev_por, ls_por, ncaj = {}, {}, {}
    if ids:
        for e in (await db.execute(select(OVEvento).where(OVEvento.ov_id.in_(ids)).order_by(OVEvento.en, OVEvento.id))).scalars():
            ev_por.setdefault(e.ov_id, []).append(e)
        for l in (await db.execute(select(OVLinea).where(OVLinea.ov_id.in_(ids)))).scalars():
            ls_por.setdefault(l.ov_id, []).append(l)
        for oid, n in (await db.execute(select(OVCaja.ov_id, func.count()).where(OVCaja.ov_id.in_(ids)).group_by(OVCaja.ov_id))).all():
            ncaj[oid] = n
    nombres = await _nombres(db)
    por_etapa = {k: [] for k, _ in KPI_ETAPAS}
    ciclos, detalle = [], []
    resp = {"surtido": {}, "prep": {}}
    for o in ovs:
        evs = ev_por.get(o.id, [])
        seg, primero = {}, {}
        for i, e in enumerate(evs):
            primero.setdefault(e.estatus, e)
            if i + 1 >= len(evs):
                continue                      # intervalo aun abierto: no cuenta
            d = (evs[i + 1].en - e.en).total_seconds()
            if d >= 0:
                seg[e.estatus] = seg.get(e.estatus, 0) + d
        prog = _prog(ls_por.get(o.id, []))
        fila = {"id": o.id, "num_ov": o.num_ov, "cliente": o.cliente, "estatus": o.estatus, "piezas": prog["piezas"],
                "surtidas": prog["surtidas"], "empacadas": prog["empacadas"], "cajas": ncaj.get(o.id, 0), "seg": {}}
        for k, _ in KPI_ETAPAS:
            if k not in seg:
                fila["seg"][k] = None
                continue
            fila["seg"][k] = round(seg[k])
            por_etapa[k].append(seg[k])
        fin = primero.get("facturada")
        fila["ciclo_seg"] = round((fin.en - o.creado_en).total_seconds()) if fin and o.creado_en else None
        if fila["ciclo_seg"] is not None:
            ciclos.append(fila["ciclo_seg"])
        for etapa, est, campo in (("surtido", "en_surtido", "surtidas"), ("prep", "en_preparacion", "empacadas")):
            ini = primero.get(est)
            asig = (getattr(o, "asig_" + etapa) or "").lower()
            ejec = (ini.por or "").lower() if ini else ""
            quien = asig or ejec
            asig_en = getattr(o, f"asig_{etapa}_en")
            arr = None
            if asig and ini and asig_en:
                a = (ini.en - asig_en).total_seconds()
                arr = round(a) if a >= 0 else None
            fila[etapa] = {"asignado": asig or None, "ejecuto": ejec or None, "responsable": quien or None, "arranque_seg": arr}
            if not quien or est not in seg:
                continue
            r = resp[etapa].setdefault(quien, {"email": quien, "nombre": nombres.get(quien, quien), "ovs": 0, "piezas": 0, "seg": 0, "en_tiempo": 0, "arranques": []})
            r["ovs"] += 1
            r["piezas"] += prog[campo]
            r["seg"] += seg[est]
            if seg[est] <= (_UMBRAL.get(est) or 1e9) * 3600:
                r["en_tiempo"] += 1
            if arr is not None:
                r["arranques"].append(arr)
        detalle.append(fila)

    etapas = []
    for k, nom in KPI_ETAPAS:
        v, um = por_etapa[k], _UMBRAL.get(k)
        etapas.append({"id": k, "nombre": nom, "n": len(v), "prom_seg": _prom(v), "mediana_seg": _pct(v, 0.5), "p90_seg": _pct(v, 0.9),
                       "max_seg": round(max(v)) if v else None, "umbral_horas": um,
                       "pct_en_tiempo": round(100 * sum(1 for x in v if x <= um * 3600) / len(v)) if v and um else None})

    def lista(o):
        out = []
        for r in o.values():
            out.append({"email": r["email"], "nombre": r["nombre"], "ovs": r["ovs"], "piezas": r["piezas"], "seg_total": round(r["seg"]),
                        "prom_seg": round(r["seg"] / r["ovs"]) if r["ovs"] else None,
                        "piezas_hora": round(10 * r["piezas"] / (r["seg"] / 3600)) / 10 if r["seg"] > 0 else None,
                        "pct_en_tiempo": round(100 * r["en_tiempo"] / r["ovs"]) if r["ovs"] else None,
                        "arranque_prom_seg": _prom(r["arranques"])})
        return sorted(out, key=lambda x: -x["ovs"])
    detalle.sort(key=lambda f: str(f["id"]), reverse=True)
    return {"desde": desde, "hasta": hasta, "total_ovs": len(ovs), "facturadas": len(ciclos), "ciclo_prom_seg": _prom(ciclos),
            "ciclo_mediana_seg": _pct(ciclos, 0.5), "etapas": etapas,
            "responsables": {"surtido": lista(resp["surtido"]), "prep": lista(resp["prep"])}, "detalle": detalle[:300], "nombres": nombres}


# ───────────────────────── adjuntos (PDF / XML) ─────────────────────────
ADJUNTOS = {
    "ov_pdf": ("OV (PDF)", ROLES_CARGA),
    "entrega_pdf": ("Entrega SAP (PDF)", ROLES_CARGA),
    "factura_pdf": ("Factura (PDF)", ROLES_FACTURA),
    "factura_xml": ("Factura (XML)", ROLES_FACTURA),
}
MAX_ADJUNTO = 9 * 1024 * 1024


def _adj_ser(a: OVAdjunto) -> dict:
    return {"id": a.id, "ov_id": a.ov_id, "tipo": a.tipo, "tipo_nombre": ADJUNTOS.get(a.tipo, (a.tipo,))[0], "nombre": a.nombre,
            "mime": a.mime, "tam": a.tam or 0, "subido_en": _iso(a.subido_en), "subido_por": a.subido_por}


@router.get("/{ov_id}/adjuntos")
async def adj_listar(ov_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    await _get_ov(db, ov_id)
    r = await db.execute(select(OVAdjunto).where(OVAdjunto.ov_id == ov_id).order_by(OVAdjunto.subido_en.desc()))
    return [_adj_ser(a) for a in r.scalars()]


@router.post("/{ov_id}/adjuntos")
async def adj_subir(ov_id: str, tipo: str = Form(...), archivo: UploadFile = File(...),
                    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Un archivo nuevo del mismo tipo reemplaza al anterior."""
    cfg = ADJUNTOS.get(tipo)
    if not cfg:
        raise HTTPException(400, "Tipo de archivo inválido.")
    _exigir(user, *cfg[1])
    ov = await _get_ov(db, ov_id)
    if tipo.startswith("factura") and ov.estatus not in ("entregada_sap", "facturada"):
        raise HTTPException(409, "La factura se adjunta cuando la entrega de SAP ya está capturada.")
    datos = await archivo.read()
    if tipo == "entrega_pdf":
        # El PDF de la entrega se valida y se guarda al registrar la entrega. Aqui solo se completa una OV ya entregada
        # (por ejemplo una registrada antes de este control) y debe ser la MISMA entrega.
        if ov.estatus not in ("entregada_sap", "facturada"):
            raise HTTPException(409, "El PDF de la entrega se adjunta desde el panel Entrega en SAP, donde se valida contra lo preparado.")
        ya = (await db.execute(select(func.count()).select_from(OVAdjunto).where(OVAdjunto.ov_id == ov_id, OVAdjunto.tipo == "entrega_pdf"))).scalar()
        if ya and user.get("rol") not in ROLES_JEFE:
            raise HTTPException(403, "Solo un gerente puede reemplazar el PDF de una entrega ya registrada.")
        try:
            en_ = parsear_entrega(datos)
        except Exception as e:
            raise HTTPException(400, f"No se pudo leer el PDF de la entrega: {e}")
        if en_["folio"] != (ov.entrega_folio or ""):
            raise HTTPException(409, f"El PDF es de la entrega {en_['folio'] or '(sin folio)'} y la registrada en esta OV es {ov.entrega_folio}.")
    if not datos:
        raise HTTPException(400, "El archivo está vacío.")
    if len(datos) > MAX_ADJUNTO:
        raise HTTPException(413, "El archivo pesa más de 9 MB.")
    es_pdf = tipo.endswith("_pdf")
    if es_pdf:
        if not datos.lstrip()[:5].startswith(b"%PDF"):
            raise HTTPException(400, "El archivo no es un PDF.")
        mime = "application/pdf"
    else:
        cf = _leer_cfdi(datos)
        mime = "text/xml"
        if tipo == "factura_xml" and not ov.factura_uuid and ov.estatus == "facturada" and cf["uuid"]:
            ov.factura_uuid = cf["uuid"]
    nombre = re.sub(r'[\\/:*?"<>|]+', "_", archivo.filename or (tipo + (".pdf" if es_pdf else ".xml")))[:120]
    await db.execute(delete(OVAdjunto).where(OVAdjunto.ov_id == ov_id, OVAdjunto.tipo == tipo))
    a = OVAdjunto(ov_id=ov_id, tipo=tipo, nombre=nombre, mime=mime, tam=len(datos), subido_por=user["sub"], contenido=datos)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return _adj_ser(a)


@router.get("/adjuntos/{adj_id}/contenido")
async def adj_leer(adj_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    a = await db.get(OVAdjunto, adj_id)
    if not a:
        raise HTTPException(404, "No existe ese archivo.")
    return Response(content=a.contenido or b"", media_type=a.mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=300"})


@router.delete("/adjuntos/{adj_id}")
async def adj_borrar(adj_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir(user, *ROLES_JEFE)
    a = await db.get(OVAdjunto, adj_id)
    if not a:
        raise HTTPException(404, "No existe ese archivo.")
    await db.delete(a)
    await db.commit()
    return {"ok": True}
