# ============================================================
#  CONTEO DE INVENTARIO
#  - Un conteo congela la existencia del sistema (Odoo) de las ubicaciones elegidas.
#  - Los contadores capturan lo que hay fisicamente, ubicacion por ubicacion.
#  - La diferencia (contado - esperado) se anota aqui. Odoo NO se modifica:
#    los ajustes se hacen en Odoo a partir del reporte.
# ============================================================
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import io, csv, uuid
from services import odoo as odoo_svc

from database import get_db
from models.models import (UbicacionAlmacen, ExistenciaUbicacion, Conteo, ConteoUbicacion,
                           ConteoLinea, LogAcceso, AlmacenSync)
from routers.almacen import get_current_user

router = APIRouter()
MAX_UBICACIONES = 5000

def _es_gestor(user) -> bool:
    return user.get("rol") in ("admin", "gerente")

def _exige_gestor(user):
    if not _es_gestor(user):
        raise HTTPException(status_code=403, detail="Solo Gerente o Administrador")

async def _conteo_o_404(db, id_conteo) -> Conteo:
    c = (await db.execute(select(Conteo).where(Conteo.id == id_conteo))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Conteo no encontrado")
    return c

async def _progreso(db, id_conteo) -> dict:
    total = (await db.execute(select(func.count(ConteoUbicacion.id)).where(ConteoUbicacion.id_conteo == id_conteo))).scalar() or 0
    hechas = (await db.execute(select(func.count(ConteoUbicacion.id)).where(
        ConteoUbicacion.id_conteo == id_conteo, ConteoUbicacion.estado == "contada"))).scalar() or 0
    return {"ubicaciones": total, "contadas": hechas, "pendientes": total - hechas,
            "pct": round(hechas * 100 / total, 1) if total else 0}

def _ser_conteo(c: Conteo, prog: dict) -> dict:
    return {"id": c.id, "nombre": c.nombre, "alcance": c.alcance, "ciego": bool(c.ciego), "estatus": c.estatus,
            "creado_por": c.creado_por, "fecha": str(c.fecha or ""), "cerrado_por": c.cerrado_por,
            "fecha_cierre": str(c.fecha_cierre or ""), "progreso": prog}

# ── LISTAR / CREAR ────────────────────────────────────────────
@router.get("/")
async def listar_conteos(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    q = select(Conteo).order_by(Conteo.fecha.desc()).limit(100)
    if not _es_gestor(user):
        q = q.where(Conteo.estatus == "abierto")
    salida = []
    for c in (await db.execute(q)).scalars():
        salida.append(_ser_conteo(c, await _progreso(db, c.id)))
    return salida

class NuevoConteoIn(BaseModel):
    nombre: str
    ciego: bool = True
    bodega: Optional[str] = None
    zona: Optional[str] = None
    prefijo: Optional[str] = None            # ej. ADISR1  (todas las ubicaciones que empiecen asi)
    solo_con_existencia: bool = False        # True = solo ubicaciones donde el sistema dice que hay producto

@router.post("/")
async def crear_conteo(body: NuevoConteoIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    if len(body.nombre.strip()) < 3:
        raise HTTPException(status_code=400, detail="Escribe un nombre para el conteo")
    q = select(UbicacionAlmacen).where(UbicacionAlmacen.bodega.is_not(None))   # excluye muros y etiquetas del plano
    partes = []
    if body.bodega: q = q.where(UbicacionAlmacen.bodega == body.bodega); partes.append(body.bodega)
    if body.zona: q = q.where(UbicacionAlmacen.zona == body.zona); partes.append("Zona " + body.zona)
    if body.prefijo and body.prefijo.strip():
        q = q.where(UbicacionAlmacen.codigo.ilike(body.prefijo.strip() + "%")); partes.append("prefijo " + body.prefijo.strip())
    ubics = (await db.execute(q.order_by(UbicacionAlmacen.codigo))).scalars().all()
    ex_rows = (await db.execute(select(ExistenciaUbicacion))).scalars().all()
    ex_por_cod = {}
    for e in ex_rows:
        ex_por_cod.setdefault(e.codigo, []).append(e)
    if body.solo_con_existencia:
        ubics = [u for u in ubics if u.codigo in ex_por_cod]
        partes.append("solo con existencia")
    if not ubics:
        raise HTTPException(status_code=400, detail="Ninguna ubicacion cumple ese filtro")
    if len(ubics) > MAX_UBICACIONES:
        raise HTTPException(status_code=400, detail=f"El filtro trae {len(ubics)} ubicaciones; el maximo por conteo es {MAX_UBICACIONES}. Afina el filtro.")

    c = Conteo(id="CNT-" + uuid.uuid4().hex[:8].upper(), nombre=body.nombre.strip(),
               alcance=(", ".join(partes) or "Todo el almacen")[:250], ciego=body.ciego,
               estatus="abierto", creado_por=user["sub"])
    db.add(c)
    for u in ubics:
        db.add(ConteoUbicacion(id_conteo=c.id, codigo=u.codigo, bodega=u.bodega, zona=u.zona))
        for e in ex_por_cod.get(u.codigo, []):
            db.add(ConteoLinea(id_conteo=c.id, codigo=u.codigo, producto=e.producto,
                               producto_desc=e.producto_desc, esperado=e.cantidad or 0))
    db.add(LogAcceso(usuario=user["sub"], accion="CONTEO_CREADO", detalle=f"{c.id} {c.nombre} ({len(ubics)} ubicaciones)", exito=True))
    await db.commit()
    return _ser_conteo(c, await _progreso(db, c.id))

@router.get("/{id_conteo}")
async def detalle_conteo(id_conteo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    c = await _conteo_o_404(db, id_conteo)
    return _ser_conteo(c, await _progreso(db, id_conteo))

# ── UBICACIONES DEL CONTEO ────────────────────────────────────
@router.get("/{id_conteo}/ubicaciones")
async def ubicaciones_conteo(id_conteo: str, estado: Optional[str] = None, buscar: Optional[str] = None,
                             limite: int = 100, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    await _conteo_o_404(db, id_conteo)
    q = select(ConteoUbicacion).where(ConteoUbicacion.id_conteo == id_conteo).order_by(ConteoUbicacion.codigo)
    if estado: q = q.where(ConteoUbicacion.estado == estado)
    if buscar: q = q.where(ConteoUbicacion.codigo.ilike(f"%{buscar.strip()}%"))
    rows = (await db.execute(q.limit(min(limite, 500)))).scalars().all()
    return [{"codigo": r.codigo, "bodega": r.bodega, "zona": r.zona, "estado": r.estado,
             "contada_por": r.contada_por, "fecha_conteo": str(r.fecha_conteo or "")} for r in rows]

@router.get("/{id_conteo}/ubicacion/{codigo}")
async def ver_ubicacion(id_conteo: str, codigo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    c = await _conteo_o_404(db, id_conteo)
    codigo = codigo.strip()
    ub = (await db.execute(select(ConteoUbicacion).where(
        ConteoUbicacion.id_conteo == id_conteo, ConteoUbicacion.codigo == codigo))).scalar_one_or_none()
    if not ub:
        raise HTTPException(status_code=404, detail=f"La ubicacion {codigo} no forma parte de este conteo")
    lineas = (await db.execute(select(ConteoLinea).where(
        ConteoLinea.id_conteo == id_conteo, ConteoLinea.codigo == codigo).order_by(ConteoLinea.producto))).scalars().all()
    ocultar = bool(c.ciego) and c.estatus == "abierto"
    return {"codigo": ub.codigo, "bodega": ub.bodega, "zona": ub.zona, "estado": ub.estado, "nota": ub.nota,
            "ciego": ocultar,
            "lineas": [{"producto": l.producto, "descripcion": l.producto_desc,
                        "esperado": None if ocultar else l.esperado, "contado": l.contado,
                        "inesperado": bool(l.inesperado)} for l in lineas]}

class LineaIn(BaseModel):
    producto: str
    descripcion: Optional[str] = None
    contado: float

class CapturaIn(BaseModel):
    lineas: List[LineaIn] = []
    vacia: bool = False                 # "no hay nada en esta ubicacion"
    nota: Optional[str] = None

@router.post("/{id_conteo}/ubicacion/{codigo}")
async def capturar_ubicacion(id_conteo: str, codigo: str, body: CapturaIn,
                             db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    c = await _conteo_o_404(db, id_conteo)
    if c.estatus != "abierto":
        raise HTTPException(status_code=400, detail="Este conteo ya esta cerrado")
    codigo = codigo.strip()
    ub = (await db.execute(select(ConteoUbicacion).where(
        ConteoUbicacion.id_conteo == id_conteo, ConteoUbicacion.codigo == codigo))).scalar_one_or_none()
    if not ub:
        raise HTTPException(status_code=404, detail=f"La ubicacion {codigo} no forma parte de este conteo")
    for l in body.lineas:
        if l.contado < 0:
            raise HTTPException(status_code=400, detail="Las cantidades no pueden ser negativas")
    existentes = {l.producto: l for l in (await db.execute(select(ConteoLinea).where(
        ConteoLinea.id_conteo == id_conteo, ConteoLinea.codigo == codigo))).scalars()}
    enviados = {l.producto: l for l in body.lineas}
    ahora = datetime.utcnow()

    if body.vacia:
        # ubicacion vacia: todo lo que el sistema esperaba se cuenta en cero
        for l in existentes.values():
            l.contado, l.veces, l.contado_por, l.fecha_conteo = 0, (l.veces or 0) + 1, user["sub"], ahora
    else:
        faltan = [p for p, l in existentes.items() if not l.inesperado and p not in enviados]
        if faltan:
            raise HTTPException(status_code=400,
                detail="Falta capturar: " + ", ".join(faltan) + ". Si no hay nada, captura 0 o usa 'Ubicacion vacia'.")
        for prod, l_in in enviados.items():
            fila = existentes.get(prod)
            if fila is None:
                fila = ConteoLinea(id_conteo=id_conteo, codigo=codigo, producto=prod[:60],
                                   producto_desc=(l_in.descripcion or "")[:250], esperado=0, inesperado=True, veces=0)
                db.add(fila)
            fila.contado = l_in.contado
            fila.veces = (fila.veces or 0) + 1
            fila.contado_por, fila.fecha_conteo = user["sub"], ahora
    ub.estado, ub.contada_por, ub.fecha_conteo = "contada", user["sub"], ahora
    if body.nota is not None:
        ub.nota = body.nota.strip() or None
    await db.commit()
    return {"ok": True, "progreso": await _progreso(db, id_conteo)}

@router.post("/{id_conteo}/cerrar")
async def cerrar_conteo(id_conteo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    c = await _conteo_o_404(db, id_conteo)
    if c.estatus == "cerrado":
        raise HTTPException(status_code=400, detail="Ya esta cerrado")
    c.estatus, c.cerrado_por, c.fecha_cierre = "cerrado", user["sub"], datetime.utcnow()
    db.add(LogAcceso(usuario=user["sub"], accion="CONTEO_CERRADO", detalle=c.id, exito=True))
    await db.commit()
    return {"ok": True}

# ── REPORTE Y ESTADISTICAS ────────────────────────────────────
async def _calcular_reporte(db, id_conteo) -> dict:
    ubs = (await db.execute(select(ConteoUbicacion).where(ConteoUbicacion.id_conteo == id_conteo))).scalars().all()
    lineas = (await db.execute(select(ConteoLinea).where(ConteoLinea.id_conteo == id_conteo))).scalars().all()
    contadas = {u.codigo: u for u in ubs if u.estado == "contada"}
    por_ub = {}
    for l in lineas:
        if l.codigo in contadas and l.contado is not None:
            por_ub.setdefault(l.codigo, []).append(l)

    dif_lineas, top = [], {}
    total_esp = total_cont = faltante = sobrante = 0.0
    ub_con_dif = set()
    por_grupo = {"bodega": {}, "zona": {}}
    for cod, ls in por_ub.items():
        for l in ls:
            d = (l.contado or 0) - (l.esperado or 0)
            total_esp += l.esperado or 0
            total_cont += l.contado or 0
            if d < 0: faltante += -d
            if d > 0: sobrante += d
            if d != 0:
                ub_con_dif.add(cod)
                top.setdefault(l.producto, {"producto": l.producto, "descripcion": l.producto_desc, "abs": 0.0, "neto": 0.0, "lineas": 0})
                top[l.producto]["abs"] += abs(d); top[l.producto]["neto"] += d; top[l.producto]["lineas"] += 1
                dif_lineas.append({"codigo": cod, "bodega": contadas[cod].bodega, "zona": contadas[cod].zona,
                                   "producto": l.producto, "descripcion": l.producto_desc,
                                   "esperado": l.esperado or 0, "contado": l.contado or 0, "diferencia": d,
                                   "pct": round(d * 100 / l.esperado, 1) if l.esperado else None,
                                   "inesperado": bool(l.inesperado), "contado_por": l.contado_por,
                                   "veces": l.veces or 0})
    for u in contadas.values():
        for clave, valor in (("bodega", u.bodega or "—"), ("zona", u.zona or "—")):
            g = por_grupo[clave].setdefault(valor, {"nombre": valor, "contadas": 0, "con_diferencia": 0})
            g["contadas"] += 1
            if u.codigo in ub_con_dif: g["con_diferencia"] += 1
    for clave in por_grupo:
        for g in por_grupo[clave].values():
            g["exactitud"] = round((g["contadas"] - g["con_diferencia"]) * 100 / g["contadas"], 1) if g["contadas"] else None
    por_contador = {}
    for u in contadas.values():
        por_contador[u.contada_por or "—"] = por_contador.get(u.contada_por or "—", 0) + 1
    n_cont = len(contadas)
    dif_lineas.sort(key=lambda x: -abs(x["diferencia"]))
    return {
        "resumen": {
            "ubicaciones": len(ubs), "contadas": n_cont, "pendientes": len(ubs) - n_cont,
            "avance_pct": round(n_cont * 100 / len(ubs), 1) if ubs else 0,
            "ubicaciones_con_diferencia": len(ub_con_dif),
            "exactitud_ubicacion_pct": round((n_cont - len(ub_con_dif)) * 100 / n_cont, 1) if n_cont else None,
            "esperado_total": total_esp, "contado_total": total_cont,
            "faltante_unidades": faltante, "sobrante_unidades": sobrante,
            "exactitud_pieza_pct": round(max(0.0, 1 - (faltante + sobrante) / total_esp) * 100, 1) if total_esp else None,
            "lineas_con_diferencia": len(dif_lineas),
            "productos_inesperados": len([d for d in dif_lineas if d["inesperado"]]),
        },
        "por_bodega": sorted(por_grupo["bodega"].values(), key=lambda g: g["nombre"]),
        "por_zona": sorted(por_grupo["zona"].values(), key=lambda g: g["nombre"]),
        "top_productos": sorted(top.values(), key=lambda t: -t["abs"])[:10],
        "por_contador": [{"usuario": k, "ubicaciones": v} for k, v in sorted(por_contador.items(), key=lambda kv: -kv[1])],
        "diferencias": dif_lineas[:2000],
        "_todas": dif_lineas,
    }

@router.get("/{id_conteo}/reporte")
async def reporte(id_conteo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    c = await _conteo_o_404(db, id_conteo)
    r = await _calcular_reporte(db, id_conteo)
    r.pop("_todas", None)
    r["conteo"] = _ser_conteo(c, await _progreso(db, id_conteo))
    return r

@router.get("/{id_conteo}/reporte.csv")
async def reporte_csv(id_conteo: str, todas: bool = False, db: AsyncSession = Depends(get_db),
                      user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    c = await _conteo_o_404(db, id_conteo)
    buffer = io.StringIO()
    w = csv.writer(buffer)
    w.writerow(["Conteo", "Ubicacion", "Bodega", "Zona", "Clave", "Descripcion", "Esperado (sistema)", "Contado", "Diferencia", "% dif.", "Producto no esperado", "Contado por", "Veces capturado"])
    if todas:
        ubs = {u.codigo: u for u in (await db.execute(select(ConteoUbicacion).where(
            ConteoUbicacion.id_conteo == id_conteo, ConteoUbicacion.estado == "contada"))).scalars()}
        ls = (await db.execute(select(ConteoLinea).where(ConteoLinea.id_conteo == id_conteo).order_by(
            ConteoLinea.codigo, ConteoLinea.producto))).scalars().all()
        for l in ls:
            if l.codigo in ubs and l.contado is not None:
                d = (l.contado or 0) - (l.esperado or 0)
                w.writerow([c.id, l.codigo, ubs[l.codigo].bodega or "", ubs[l.codigo].zona or "", l.producto, l.producto_desc or "",
                            l.esperado or 0, l.contado or 0, d, round(d * 100 / l.esperado, 1) if l.esperado else "",
                            "SI" if l.inesperado else "", l.contado_por or "", l.veces or 0])
    else:
        r = await _calcular_reporte(db, id_conteo)
        for d in r["_todas"]:
            w.writerow([c.id, d["codigo"], d["bodega"] or "", d["zona"] or "", d["producto"], d["descripcion"] or "",
                        d["esperado"], d["contado"], d["diferencia"], d["pct"] if d["pct"] is not None else "",
                        "SI" if d["inesperado"] else "", d["contado_por"] or "", d["veces"]])
    return Response(content="﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={c.id}_{'completo' if todas else 'diferencias'}.csv"})


# ── VALIDACION CONTRA MOVIMIENTOS DE ODOO (OVs surtidas, traslados) ──
# Idea: una diferencia entre lo contado y lo "esperado" puede deberse a que, entre el momento en que
# se tomo la existencia del sistema y el momento en que se conto la ubicacion, salio mercancia por una
# OV o se traslado a otra ubicacion. Aqui se leen esos movimientos en Odoo (solo lectura) y se comparan.
def _fecha_odoo(txt):
    try:
        return datetime.strptime(txt[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

async def _analizar_movimientos(db, c: Conteo) -> dict:
    ubs = (await db.execute(select(ConteoUbicacion).where(ConteoUbicacion.id_conteo == c.id))).scalars().all()
    lineas = (await db.execute(select(ConteoLinea).where(ConteoLinea.id_conteo == c.id))).scalars().all()
    contadas = {u.codigo: u for u in ubs if u.estado == "contada"}
    codigos = {u.codigo for u in ubs}
    # el "esperado" salio de la ultima sincronizacion con Odoo, que puede ser anterior a crear el conteo
    ult_sync = (await db.execute(select(func.max(AlmacenSync.fecha)))).scalar()
    inicio_conteo = c.fecha or datetime.utcnow()
    desde = min(inicio_conteo, ult_sync) if ult_sync else inicio_conteo
    hasta = c.fecha_cierre or datetime.utcnow()
    try:
        movs = await odoo_svc.movimientos_ubicaciones(sorted(codigos), desde, hasta)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudieron leer los movimientos de Odoo: {e}")
    for m in movs:
        m["_dt"] = _fecha_odoo(m.get("fecha") or "")
    movs = [m for m in movs if m["_dt"] and desde <= m["_dt"] <= hasta]     # solo lo ocurrido en la ventana
    idx = {}
    for m in movs:
        cant = m["cantidad"] or 0
        if m["destino"] in codigos:
            idx.setdefault((m["destino"], m["producto"]), []).append((m, +cant))
        if m["origen"] in codigos:
            idx.setdefault((m["origen"], m["producto"]), []).append((m, -cant))

    difs, cuenta = [], {"explicada": 0, "parcial": 0, "sin_explicar": 0}
    for l in lineas:
        if l.codigo not in contadas or l.contado is None:
            continue
        dif = (l.contado or 0) - (l.esperado or 0)
        if abs(dif) < 1e-9:
            continue
        fin = l.fecha_conteo or hasta
        usados = [(m, d) for (m, d) in idx.get((l.codigo, l.producto), []) if m["_dt"] and desde <= m["_dt"] <= fin]
        neto = sum(d for _, d in usados)
        ajustada = (l.contado or 0) - ((l.esperado or 0) + neto)
        if abs(ajustada) < 1e-9:
            estado = "explicada"
        elif abs(ajustada) < abs(dif) - 1e-9:
            estado = "parcial"
        else:
            estado = "sin_explicar"
        cuenta[estado] += 1
        difs.append({
            "codigo": l.codigo, "producto": l.producto, "descripcion": l.producto_desc,
            "esperado": l.esperado or 0, "contado": l.contado or 0, "diferencia": dif,
            "movimiento_neto": neto, "diferencia_sin_explicar": ajustada, "estado": estado,
            "movimientos": [{"tipo": m["tipo"], "documento": m["ov"] or m["picking"], "picking": m["picking"], "fecha": m["fecha"],
                             "cantidad": d, "origen": m["origen"], "destino": m["destino"]} for m, d in usados[:10]],
            "contado_por": l.contado_por,
        })
    orden = {"sin_explicar": 0, "parcial": 1, "explicada": 2}
    difs.sort(key=lambda x: (orden[x["estado"]], -abs(x["diferencia_sin_explicar"])))

    ovs = {}
    for m in movs:
        if m["tipo"] == "OV" and m["origen"] in codigos:
            k = m["picking"] or m["ov"]
            o = ovs.setdefault(k, {"ov": m["ov"], "picking": m["picking"], "cliente": m["cliente"], "fecha": m["fecha"], "unidades": 0, "lineas": []})
            o["unidades"] += m["cantidad"] or 0
            o["fecha"] = max(o["fecha"] or "", m["fecha"] or "")
            o["lineas"].append({"producto": m["producto"], "descripcion": m["descripcion"], "cantidad": m["cantidad"], "ubicacion": m["origen"], "fecha": m["fecha"]})
    traslados = [{"picking": m["picking"], "fecha": m["fecha"], "producto": m["producto"], "descripcion": m["descripcion"],
                  "cantidad": m["cantidad"], "origen": m["origen"], "destino": m["destino"]}
                 for m in movs if m["tipo"] == "traslado"]
    otros = [{"tipo": m["tipo"], "documento": m["ov"] or m["picking"], "fecha": m["fecha"], "producto": m["producto"],
              "cantidad": m["cantidad"], "origen": m["origen"], "destino": m["destino"]}
             for m in movs if m["tipo"] in ("entrada", "ajuste")]
    lista_ovs = sorted(ovs.values(), key=lambda o: o["fecha"] or "")
    return {
        "ventana": {"desde": str(desde)[:19], "hasta": str(hasta)[:19], "base_existencias": str(ult_sync)[:19] if ult_sync else None},
        "resumen": {
            "diferencias": len(difs), **cuenta,
            "ovs": len(lista_ovs), "unidades_ov": sum(o["unidades"] for o in lista_ovs),
            "traslados": len(traslados), "otros_movimientos": len(otros), "movimientos_total": len(movs),
        },
        "diferencias": difs[:2000], "ovs": lista_ovs[:500], "traslados": traslados[:1000], "otros": otros[:500],
        "_difs": difs, "_ovs": lista_ovs, "_traslados": traslados, "_otros": otros,
    }

@router.get("/{id_conteo}/movimientos")
async def movimientos_conteo(id_conteo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    c = await _conteo_o_404(db, id_conteo)
    r = await _analizar_movimientos(db, c)
    for k in ("_difs", "_ovs", "_traslados", "_otros"):
        r.pop(k, None)
    return r

@router.get("/{id_conteo}/movimientos.csv")
async def movimientos_conteo_csv(id_conteo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exige_gestor(user)
    c = await _conteo_o_404(db, id_conteo)
    r = await _analizar_movimientos(db, c)
    buffer = io.StringIO()
    w = csv.writer(buffer)
    w.writerow(["Seccion", "Ubicacion", "Clave", "Descripcion", "Esperado", "Contado", "Diferencia", "Movimiento neto Odoo",
                "Diferencia sin explicar", "Estado", "Tipo mov.", "Documento (OV/picking)", "Fecha", "Origen", "Destino", "Cantidad"])
    for d in r["_difs"]:
        movs = d["movimientos"] or [None]
        for m in movs:
            w.writerow(["DIFERENCIA", d["codigo"], d["producto"], d["descripcion"] or "", d["esperado"], d["contado"], d["diferencia"],
                        d["movimiento_neto"], d["diferencia_sin_explicar"], d["estado"],
                        m["tipo"] if m else "", m["documento"] if m else "", m["fecha"] if m else "",
                        m["origen"] if m else "", m["destino"] if m else "", m["cantidad"] if m else ""])
    for o in r["_ovs"]:
        for l in o["lineas"]:
            w.writerow(["OV SURTIDA", l["ubicacion"], l["producto"], l["descripcion"] or "", "", "", "", "", "", "", "OV",
                        o["ov"] or o["picking"], l["fecha"], l["ubicacion"], o["cliente"], l["cantidad"]])
    for t in r["_traslados"]:
        w.writerow(["TRASLADO", "", t["producto"], t["descripcion"] or "", "", "", "", "", "", "", "traslado",
                    t["picking"], t["fecha"], t["origen"], t["destino"], t["cantidad"]])
    for t in r["_otros"]:
        w.writerow(["OTRO MOV.", "", t["producto"], "", "", "", "", "", "", "", t["tipo"], t["documento"], t["fecha"], t["origen"], t["destino"], t["cantidad"]])
    return Response(content="\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={c.id}_validacion_movimientos.csv"})
