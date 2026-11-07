"""Reporteria: agregados de solo lectura para la pestana Analisis.
Todo sale de lo que el sistema ya captura (entregas, productos, tarimas, impresiones,
solicitudes) + un bloque aparte que consulta Odoo (solo lectura) y no bloquea al resto."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, date
from statistics import median
from zoneinfo import ZoneInfo
import re, json, time

from database import get_db
from models.models import Entrega, Producto, Tarima, DetalleTarima, CatalogoItem, SolicitudReimpresion, UsuarioAlmacen
from routers.entregas import get_current_user
from services import odoo as odoo_svc

router = APIRouter()


async def solo_jefatura(user: dict = Depends(get_current_user)):
    """Reporteria: solo admin y gerente."""
    if (user or {}).get("rol") not in ("admin", "gerente"):
        raise HTTPException(status_code=403, detail="El análisis es solo para administradores y gerentes.")
    return user

MX = ZoneInfo("America/Mexico_City")
UTC = ZoneInfo("UTC")
_RE_EXT = re.compile(r"\s*\(EXT\d+\)\s*$", re.I)
DIAS = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]

def _base(clave): return _RE_EXT.sub("", (clave or "").strip()).upper()

def _local(dt):
    """datetime UTC ingenuo (como sale de la BD) -> hora de Mexico."""
    return dt.replace(tzinfo=UTC).astimezone(MX) if dt else None

def _a_utc(d: date) -> datetime:
    """Medianoche local de `d` -> UTC ingenuo, para filtrar en la BD."""
    return datetime(d.year, d.month, d.day, tzinfo=MX).astimezone(UTC).replace(tzinfo=None)

def _pct(a, b): return round(100 * a / b, 1) if b else 0.0
def _delta(act, ant):
    return None if not ant else round(100 * (act - ant) / ant, 1)

async def _cargar(db, d0: date, d1: date, empresa, sistema):
    """Datos crudos del periodo [d0, d1] (ambos inclusive, hora local)."""
    q = select(Entrega).where(Entrega.fecha_creacion >= _a_utc(d0), Entrega.fecha_creacion < _a_utc(d1 + timedelta(days=1)))
    if empresa: q = q.where(Entrega.comercializador == empresa)
    if sistema: q = q.where(Entrega.sistema == sistema)
    ents = list((await db.execute(q)).scalars())
    ids = [e.id_entrega for e in ents]
    prods, tars, dets = [], [], []
    if ids:
        prods = list((await db.execute(select(Producto).where(Producto.id_entrega.in_(ids)))).scalars())
        tars = list((await db.execute(select(Tarima).where(Tarima.id_entrega.in_(ids)))).scalars())
        tids = [t.id_tarima for t in tars]
        if tids:
            dets = list((await db.execute(select(DetalleTarima).where(DetalleTarima.id_tarima.in_(tids)))).scalars())
    return ents, prods, tars, dets

async def _pesos(db, prods):
    skus = list({_base(p.clave) for p in prods if p.clave})
    m = {}
    if skus:
        for c in (await db.execute(select(CatalogoItem).where(CatalogoItem.sku.in_(skus)))).scalars():
            m[c.sku] = c.peso_unit or 0
    return m

def _kg_prod(p, pesos):
    # La extension es un empaque extra: no suma peso del producto (igual que en etiquetas)
    if p.es_extension or re.search(r"\(EXT\d+\)\s*$", p.clave or "", re.I): return 0.0
    return (p.cantidad_total or 0) * pesos.get(_base(p.clave), 0)

def _kpis(ents, prods, tars, pesos):
    cerr = [t for t in tars if t.estatus == "cerrada"]
    piezas = sum(p.cantidad_total or 0 for p in prods)
    con_peso = sum(p.cantidad_total or 0 for p in prods if pesos.get(_base(p.clave), 0) > 0)
    kg = sum(_kg_prod(p, pesos) for p in prods) + sum(t.peso_palet_kg or 0 for t in cerr)
    return {
        "entregas": len(ents),
        "completadas": sum(1 for e in ents if e.estatus == "completada"),
        "pendientes": sum(1 for e in ents if e.estatus != "completada"),
        "piezas": piezas, "bultos": len(cerr),
        "tarimas": sum(1 for t in cerr if (t.tipo_bulto or "tarima") != "caja"),
        "cajas": sum(1 for t in cerr if t.tipo_bulto == "caja"),
        "kg": round(kg, 1),
        "cobertura_peso": _pct(con_peso, piezas),
    }

@router.get("/resumen")
async def resumen(
    desde: str = Query(None), hasta: str = Query(None),
    empresa: str = Query(None), sistema: str = Query(None),
    db: AsyncSession = Depends(get_db), user: dict = Depends(solo_jefatura)
):
    hoy = datetime.now(MX).date()
    try:
        d1 = date.fromisoformat(hasta) if hasta else hoy
        d0 = date.fromisoformat(desde) if desde else d1 - timedelta(days=29)
    except ValueError:
        raise HTTPException(status_code=422, detail="Fechas invalidas. Usa el formato AAAA-MM-DD.")
    if d0 > d1: d0, d1 = d1, d0
    dias = (d1 - d0).days + 1
    if dias > 400:
        raise HTTPException(status_code=422, detail="El periodo maximo es de 400 dias. Acorta el rango.")
    empresa, sistema = (empresa or None), (sistema or None)

    ents, prods, tars, dets = await _cargar(db, d0, d1, empresa, sistema)
    pesos = await _pesos(db, prods)
    k = _kpis(ents, prods, tars, pesos)

    # Periodo anterior de igual duracion, para saber si "vamos mejor o peor"
    p1 = d0 - timedelta(days=1); p0 = p1 - timedelta(days=dias - 1)
    e2, pr2, t2, _ = await _cargar(db, p0, p1, empresa, sistema)
    k2 = _kpis(e2, pr2, t2, await _pesos(db, pr2))
    k["anterior"] = {x: k2[x] for x in ("entregas", "piezas", "bultos", "kg")}
    k["delta"] = {x: _delta(k[x], k2[x]) for x in ("entregas", "piezas", "bultos", "kg")}

    ent_por_id = {e.id_entrega: e for e in ents}
    prods_por_ent = {}
    for p in prods: prods_por_ent.setdefault(p.id_entrega, []).append(p)
    tars_por_ent = {}
    for t in tars: tars_por_ent.setdefault(t.id_entrega, []).append(t)

    # ── serie diaria (incluye dias sin movimiento para que la linea no engañe)
    ser = {(d0 + timedelta(days=i)).isoformat(): {"fecha": (d0 + timedelta(days=i)).isoformat(), "entregas": 0, "piezas": 0, "bultos": 0}
           for i in range(dias)}
    for e in ents:
        f = _local(e.fecha_creacion).date().isoformat()
        if f in ser:
            ser[f]["entregas"] += 1
            ser[f]["piezas"] += sum(p.cantidad_total or 0 for p in prods_por_ent.get(e.id_entrega, []))
    for t in tars:
        if t.estatus == "cerrada" and t.fecha_cierre:
            f = _local(t.fecha_cierre).date().isoformat()
            if f in ser: ser[f]["bultos"] += 1

    # ── cortes por dimension
    def corte(clave_fn):
        acc = {}
        for e in ents:
            n = clave_fn(e) or "Sin dato"
            a = acc.setdefault(n, {"nombre": n, "entregas": 0, "piezas": 0, "bultos": 0, "kg": 0.0})
            a["entregas"] += 1
            ps = prods_por_ent.get(e.id_entrega, [])
            a["piezas"] += sum(p.cantidad_total or 0 for p in ps)
            a["kg"] += sum(_kg_prod(p, pesos) for p in ps)
            ts = [t for t in tars_por_ent.get(e.id_entrega, []) if t.estatus == "cerrada"]
            a["bultos"] += len(ts); a["kg"] += sum(t.peso_palet_kg or 0 for t in ts)
        out = sorted(acc.values(), key=lambda r: (-r["piezas"], -r["entregas"]))
        for r in out: r["kg"] = round(r["kg"], 1)
        return out
    por_empresa = corte(lambda e: (e.comercializador or "").strip().title())
    por_sistema = corte(lambda e: e.sistema)
    por_fuente = corte(lambda e: e.fuente)
    top_clientes = corte(lambda e: (e.nombre_cliente or "").strip())[:8]

    # Flujo empresa -> sistema -> cliente (para el diagrama de Sankey)
    fl = {}
    for e in ents:
        kf = ((e.comercializador or "").strip().title() or "Sin dato", e.sistema or "Sin dato", (e.nombre_cliente or "").strip() or "Sin dato")
        f = fl.setdefault(kf, {"empresa": kf[0], "sistema": kf[1], "cliente": kf[2], "entregas": 0, "piezas": 0, "bultos": 0, "kg": 0.0})
        f["entregas"] += 1
        ps = prods_por_ent.get(e.id_entrega, [])
        f["piezas"] += sum(p.cantidad_total or 0 for p in ps)
        f["kg"] += sum(_kg_prod(p, pesos) for p in ps)
        ts = [t for t in tars_por_ent.get(e.id_entrega, []) if t.estatus == "cerrada"]
        f["bultos"] += len(ts); f["kg"] += sum(t.peso_palet_kg or 0 for t in ts)
    flujo = sorted(fl.values(), key=lambda r: -r["piezas"])
    for r in flujo: r["kg"] = round(r["kg"], 1)

    sk = {}
    for p in prods:
        b = _base(p.clave)
        if not b: continue
        r = sk.setdefault(b, {"clave": b, "descripcion": p.descripcion or "", "piezas": 0})
        r["piezas"] += p.cantidad_total or 0
    top_skus = sorted(sk.values(), key=lambda r: -r["piezas"])[:8]

    # ── ciclo: de la captura de la entrega al cierre del ultimo bulto (solo completadas)
    horas = []
    for e in ents:
        if e.estatus != "completada": continue
        cierres = [t.fecha_cierre for t in tars_por_ent.get(e.id_entrega, []) if t.fecha_cierre]
        if cierres and e.fecha_creacion:
            h = (max(cierres) - e.fecha_creacion).total_seconds() / 3600
            if h >= 0: horas.append(h)
    lim = [("Menos de 2 h", 2), ("2 a 8 h", 8), ("8 a 24 h", 24), ("1 a 3 dias", 72), ("Mas de 3 dias", 1e9)]
    b = [{"etq": n, "n": 0} for n, _ in lim]
    for h in horas:
        for i, (_, tope) in enumerate(lim):
            if h < tope: b[i]["n"] += 1; break
    ciclo = {"muestra": len(horas), "mediana_horas": round(median(horas), 1) if horas else None,
             "p90_horas": round(sorted(horas)[int(0.9 * (len(horas) - 1))], 1) if horas else None, "buckets": b}

    # ── pendientes: antiguedad y los mas viejos
    ahora = datetime.utcnow(); pend = []
    for e in ents:
        if e.estatus == "completada": continue
        ps = prods_por_ent.get(e.id_entrega, [])
        tot = sum(p.cantidad_total or 0 for p in ps); asig = sum(p.cantidad_asignada or 0 for p in ps)
        pend.append({"id_entrega": e.id_entrega, "num_entrega": e.num_entrega, "cliente": e.nombre_cliente,
                     "empresa": (e.comercializador or "").title(), "sistema": e.sistema,
                     "dias": round((ahora - e.fecha_creacion).total_seconds() / 86400, 1) if e.fecha_creacion else 0,
                     "avance_pct": _pct(asig, tot) if e.sistema != "CS" else 0.0, "piezas": tot})
    ab = [("Hoy", 1), ("1 a 3 dias", 3), ("3 a 7 dias", 7), ("Mas de 7 dias", 1e9)]
    antig = [{"etq": n, "n": 0} for n, _ in ab]
    for r in pend:
        for i, (_, tope) in enumerate(ab):
            if r["dias"] < tope: antig[i]["n"] += 1; break
    pendientes = {"total": len(pend), "antiguedad": antig, "lista": sorted(pend, key=lambda r: -r["dias"])[:8]}

    # ── mapa de calor: cuando se cierran los bultos (dia x hora, hora local)
    hm = {}
    for t in tars:
        if t.estatus == "cerrada" and t.fecha_cierre:
            l = _local(t.fecha_cierre); hm[(l.weekday(), l.hour)] = hm.get((l.weekday(), l.hour), 0) + 1
    heatmap = [{"dia": d, "dia_txt": DIAS[d], "hora": h, "bultos": n} for (d, h), n in sorted(hm.items())]

    # ── quien cierra bultos
    us = {}
    for t in tars:
        if t.estatus == "cerrada" and t.cerrado_por:
            u = us.setdefault(t.cerrado_por, {"usuario": t.cerrado_por, "bultos": 0})
            u["bultos"] += 1
    usuarios = sorted(us.values(), key=lambda r: -r["bultos"])[:8]

    # ── calidad: reimpresiones, solicitudes, parciales, extensiones
    cerr = [t for t in tars if t.estatus == "cerrada"]
    t_imp = sum(1 for t in cerr if (t.impresa_veces or 0) >= 1)
    t_re = sum(1 for t in cerr if (t.impresa_veces or 0) > 1)
    s_imp = sum(1 for e in ents if (e.etiquetas_sueltas_impresas_veces or 0) >= 1)
    s_re = sum(1 for e in ents if (e.etiquetas_sueltas_impresas_veces or 0) > 1)
    p_imp = sum(1 for e in ents if (e.packing_impreso_veces or 0) >= 1)
    p_re = sum(1 for e in ents if (e.packing_impreso_veces or 0) > 1)
    sol = list((await db.execute(select(SolicitudReimpresion).where(
        SolicitudReimpresion.fecha_solicitud >= _a_utc(d0), SolicitudReimpresion.fecha_solicitud < _a_utc(d1 + timedelta(days=1))))).scalars())
    sol_pack = [s for s in sol if s.tipo == "PACKING_PARCIAL"]
    sol_re = [s for s in sol if s.tipo != "PACKING_PARCIAL"]
    res_h = [(s.fecha_resolucion - s.fecha_solicitud).total_seconds() / 3600 for s in sol if s.fecha_resolucion and s.fecha_solicitud]
    calidad = {
        "documentos": [
            {"nombre": "Etiquetas de tarima", "impresos": t_imp, "reimpresos": t_re},
            {"nombre": "Etiquetas sueltas", "impresos": s_imp, "reimpresos": s_re},
            {"nombre": "Lista de empaque", "impresos": p_imp, "reimpresos": p_re},
        ],
        "tasa_reimpresion": _pct(t_re + s_re + p_re, t_imp + s_imp + p_imp),
        "solicitudes_reimpresion": {"total": len(sol_re), "aprobadas": sum(1 for s in sol_re if s.estatus in ("aprobada", "usada")),
                                    "rechazadas": sum(1 for s in sol_re if s.estatus == "rechazada"),
                                    "pendientes": sum(1 for s in sol_re if s.estatus == "pendiente")},
        "mediana_resolucion_horas": round(median(res_h), 1) if res_h else None,
        "listas_parciales": {"total": len(sol_pack), "pct_de_entregas": _pct(len({s.id_entrega for s in sol_pack}), len(ents))},
        "extensiones": sum(1 for p in prods if p.es_extension or re.search(r"\(EXT\d+\)\s*$", p.clave or "", re.I)),
        "avance_asignacion_pct": _pct(sum(p.cantidad_asignada or 0 for p in prods), sum(p.cantidad_total or 0 for p in prods)),
    }

    return {"periodo": {"desde": d0.isoformat(), "hasta": d1.isoformat(), "dias": dias,
                        "anterior": {"desde": p0.isoformat(), "hasta": p1.isoformat()}},
            "filtros": {"empresa": empresa, "sistema": sistema},
            "kpis": k, "serie_diaria": list(ser.values()),
            "por_empresa": por_empresa, "por_sistema": por_sistema, "por_fuente": por_fuente,
            "top_clientes": top_clientes, "flujo": flujo, "top_skus": top_skus, "ciclo": ciclo, "pendientes": pendientes,
            "heatmap": heatmap, "usuarios": usuarios, "calidad": calidad,
            "empresas_disponibles": sorted({(e.comercializador or "").title() for e in ents if e.comercializador})}

# ── ODOO (solo lectura): lo que viene, aparte para no frenar el resto ─────────
_cache_odoo = {}

@router.get("/odoo")
async def odoo_demanda(db: AsyncSession = Depends(get_db), user: dict = Depends(solo_jefatura)):
    """OV confirmadas de los ultimos 45 dias en Odoo, cruzadas contra lo ya trabajado en este sistema.
    Responde 'cuanta demanda hay y que tanto hemos tomado'. Cache de 5 min."""
    llave = (user.get("sub"), user.get("rol"))
    c = _cache_odoo.get(llave)
    if c and time.time() - c[0] < 300: return c[1]

    wh = None
    if user.get("rol") != "admin":
        asignados = [r[0] for r in (await db.execute(select(UsuarioAlmacen.odoo_warehouse_id).where(UsuarioAlmacen.usuario == user["sub"]))).all()]
        wh = asignados or None
    try:
        ovs = await odoo_svc.listar_ovs_pendientes(wh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar Odoo ({e}). Intenta de nuevo en un momento.")

    nombres = [o["num_ov"].strip().upper() for o in ovs if o.get("num_ov")]
    ya = set()
    if nombres:
        from sqlalchemy import func
        r = await db.execute(select(Entrega.orden, Entrega.num_entrega).where(
            func.upper(Entrega.orden).in_(nombres) | func.upper(Entrega.num_entrega).in_(nombres)))
        for a, b in r.all():
            for x in (a, b):
                if x: ya.add(x.strip().upper())

    def grp(fn):
        acc = {}
        for o in ovs:
            n = fn(o) or "Sin dato"
            a = acc.setdefault(n, {"nombre": n, "ovs": 0, "tomadas": 0})
            a["ovs"] += 1; a["tomadas"] += 1 if o["num_ov"].strip().upper() in ya else 0
        return sorted(acc.values(), key=lambda r: -r["ovs"])
    por_dia = {}
    for o in ovs:
        if o.get("fecha"): por_dia[o["fecha"]] = por_dia.get(o["fecha"], 0) + 1
    tomadas = sum(1 for o in ovs if o["num_ov"].strip().upper() in ya)
    out = {"total_ovs": len(ovs), "tomadas": tomadas, "sin_tomar": len(ovs) - tomadas,
           "cobertura_pct": _pct(tomadas, len(ovs)),
           "por_almacen": grp(lambda o: o.get("almacen"))[:8], "por_empresa": grp(lambda o: (o.get("comercializador") or "").title())[:8],
           "por_dia": [{"fecha": f, "ovs": n} for f, n in sorted(por_dia.items())],
           "ventana_dias": 45}
    _cache_odoo[llave] = (time.time(), out)
    return out
