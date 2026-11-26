"""Inventario: costos, otras unidades de negocio, reconteo externo por SKU, comprometido, comparar conteos, reporte final y firmas.
Port de Final.gs, Comprometido.gs, RecoSKU.gs, Comparar.gs y Firmas.gs. Odoo NUNCA se modifica (solo lectura)."""
import csv, io, json
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import (InvConteo, InvConteoUbic, InvConteoRonda, InvCosto, InvOtraUN, InvRecontoSKU, InvComprometido, InvExistencia, InvIncidencia)
from routers.inventario import (inv_user, exige_gestor, log, conteo_o_404, ubs_de, lineas_de, cat_otras_un, costos_mapa, archivo_csv, ser_conteo, progreso,
                                incidencias_de, ser_inc, movimientos, TIPOS_INC)
from services.inv_core import *
from services import inv_odoo as O
from services import inv_analisis as A

router = APIRouter()
NS = O.norm_sku


# ───────────────────────── COSTOS ─────────────────────────
async def actualizar_costos(db, claves):
    claves = sorted({txt(k) for k in claves if txt(k)})
    enc = await O.costos_odoo(claves)
    t = ahora()
    cache = {r.producto: r for r in (await db.execute(select(InvCosto))).scalars()}
    for k, v in enc.items():
        if k in cache: cache[k].costo = v; cache[k].fecha = t
        else: db.add(InvCosto(producto=k[:60], costo=v, fecha=t))
    await db.commit()
    return {"leidos": len(enc), "sin_odoo": [k for k in claves if k not in enc]}


@router.post("/conteos/{id_}/costos/actualizar")
async def api_actualizar_costos(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    claves = {txt(l.producto) for l in await lineas_de(db, id_)}
    try:
        r = await actualizar_costos(db, claves)
    except Exception as e:
        raise HTTPException(502, f"No se pudieron leer costos de Odoo: {str(e)[:200]}")
    await log(db, u["email"], "COSTOS_ACTUALIZADOS", f"{id_} {r['leidos']} costos de Odoo"); await db.commit()
    return r


# ───────────────────────── OTRAS UNIDADES DE NEGOCIO ─────────────────────────
UNIDADES_NEGOCIO = ["Raiker", "ECOR", "Korei", "TDK Machinery", "Hyundai Energy", "Hyundai Potencia", "Surtidora del Pastelero", "Reliant", "Hotel Fragata", "Otra"]


@router.get("/conteos/{id_}/no-esperados")
async def api_no_esperados(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    cat = await cat_otras_un(db); ubs = {txt(x.codigo): x for x in await ubs_de(db, id_)}
    por = {}
    for l in await lineas_de(db, id_):
        if not l.inesperado or l.contado is None or ubs.get(txt(l.codigo)) is None or ubs[txt(l.codigo)].estado != "contada": continue
        k = txt(l.producto); o = por.setdefault(k, {"producto": k, "descripcion": txt(l.producto_desc), "unidades": 0, "ubicaciones": [], "unidad": cat.get(k.upper(), "")})
        o["unidades"] += num(l.contado)
        if txt(l.codigo) not in o["ubicaciones"]: o["ubicaciones"].append(txt(l.codigo))
    return {"productos": sorted(por.values(), key=lambda o: -o["unidades"]), "unidades_negocio": UNIDADES_NEGOCIO}


@router.post("/conteos/{id_}/otras-un")
async def api_clasificar_otra_un(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    items = body.get("items") or []
    if not items: raise HTTPException(422, "Elige al menos un producto")
    cat = {r.producto.upper(): r for r in (await db.execute(select(InvOtraUN))).scalars()}
    for it in items:
        k = txt(it.get("producto"))
        if not k: continue
        if it.get("quitar"):
            if k.upper() in cat: await db.delete(cat[k.upper()])
            continue
        un = txt(it.get("unidad"))[:60] or "Sin asignar"
        if k.upper() in cat: cat[k.upper()].unidad = un
        else: db.add(InvOtraUN(producto=k[:60], unidad=un, nota=txt(it.get("nota"))[:250], creado_por=u["email"], fecha=ahora()))
    await log(db, u["email"], "OTRAS_UN", f"{id_} {len(items)} productos"); await db.commit()
    return {"ok": True}


# ───────────────────────── RECONTEO EXTERNO POR SKU ─────────────────────────
def parse_tabla(texto):
    t = (texto or "").replace("\r", "")
    if "\t" in t: return [l.split("\t") for l in t.split("\n") if l.strip()]
    l1 = t.split("\n")[0] if t else ""
    sep = ";" if l1.count(";") > l1.count(",") else ","
    return [r for r in csv.reader(io.StringIO(t), delimiter=sep) if any(x.strip() for x in r)]


def _num(s):
    try: return float(str(s).replace(" ", "").replace(",", ""))
    except ValueError: return None


def sin_encabezado(filas):
    if filas and len(filas[0]) > 1 and txt(filas[0][1]) != "" and _num(filas[0][1]) is None: return filas[1:]
    return filas


@router.post("/conteos/{id_}/reconteo-sku")
async def api_importar_reco_sku(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    avisos, vistos, nuevas, omit = [], {}, [], 0
    for i, f in enumerate(sin_encabezado(parse_tabla(body.get("texto")))):
        prod = txt(f[0]) if f else ""; crudo = txt(f[1]).replace(" ", "") if len(f) > 1 else ""
        if not prod: continue
        if crudo == "": omit += 1; continue
        n = _num(crudo)
        if n is None or n < 0: avisos.append(f"Fila {i + 1} ({prod}): cantidad no válida «{f[1]}», se omitió."); continue
        k = NS(prod)
        if k in vistos:
            avisos.append(f"{prod} viene repetido en el archivo: se usa la última fila ({n:g}, antes {vistos[k]:g}). Revisa que no sean dos productos distintos.")
            nuevas = [x for x in nuevas if NS(x["producto"]) != k]
        vistos[k] = n
        nuevas.append({"producto": prod, "recontado": n, "observacion": txt(f[2])[:500] if len(f) > 2 else "", "personas": txt(f[3])[:250] if len(f) > 3 else ""})
    if not nuevas: raise HTTPException(422, "No encontré filas con producto y cantidad. Pega columnas: Producto, Recontado (y opcional Observación).")
    t = ahora()
    for x in nuevas: db.add(InvRecontoSKU(id_conteo=id_, cargado_por=u["email"], fecha=t, **x))
    await log(db, u["email"], "importar_reconteo_sku", f"{id_} · {len(nuevas)} productos"); await db.commit()
    return {"importados": len(nuevas), "sin_cifra": omit, "avisos": avisos}


async def reco_vigente(db, id_):
    m = {}
    for r in (await db.execute(select(InvRecontoSKU).where(InvRecontoSKU.id_conteo == id_).order_by(InvRecontoSKU.id))).scalars(): m[NS(r.producto)] = r
    return m


# ───────────────────────── REPORTE FINAL ─────────────────────────
CAUSAS = [("ov", "Salidas por OV (ventas / surtido)"), ("traslado", "Traslados internos"), ("entrada", "Entradas / recepciones"), ("ajuste", "Ajustes de inventario"),
          ("cruce", "Compensación entre ubicaciones (mismo SKU y almacén)"), ("ov_fuera", "OV surtida desde otra ubicación (mal surtido)"), ("sin", "Sin explicar")]
DEF_TIPOS = ("danado", "defectuoso", "empaque")


async def calcular_final(db, c, actualizar=False, con_mov=False):
    id_ = c.id; aviso = []
    ubs = await ubs_de(db, id_); contadas = {txt(x.codigo): x for x in ubs if x.estado == "contada"}
    cat = await cat_otras_un(db)
    todas = [l for l in await lineas_de(db, id_) if txt(l.codigo) in contadas and l.contado is not None]
    lineas = [l for l in todas if not es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)]
    incs = await incidencias_de(db, id_)
    claves = {txt(l.producto) for l in lineas} | {txt(r.producto) for r in incs if txt(r.producto) and len(txt(r.producto)) <= 40}
    cm = await costos_mapa(db); faltan = [k for k in claves if k not in cm]
    if (actualizar or faltan) and claves:
        try:
            await actualizar_costos(db, claves if actualizar else faltan); cm = await costos_mapa(db)
        except Exception as e:
            await db.rollback(); aviso.append(f"No se pudieron leer costos de Odoo ({str(e)[:120]}). Se usan los costos guardados.")
            await db.refresh(c)
            # el rollback expira los objetos ORM ya cargados: se recargan
            ubs = await ubs_de(db, id_); contadas = {txt(x.codigo): x for x in ubs if x.estado == "contada"}
            todas = [l for l in await lineas_de(db, id_) if txt(l.codigo) in contadas and l.contado is not None]
            lineas = [l for l in todas if not es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)]
            incs = await incidencias_de(db, id_); cm = await costos_mapa(db)
    costo = lambda k: cm.get(k, 0)
    sin_costo = sorted({txt(l.producto) for l in lineas if not (cm.get(txt(l.producto), 0) > 0)})
    sku, dif, tot, gb = {}, [], {"sistema": 0.0, "contado": 0.0, "falt": 0.0, "sobr": 0.0, "us": 0.0, "uc": 0.0}, {"bodega": {}, "zona": {}}
    for l in lineas:
        k, cod = txt(l.producto), txt(l.codigo); x = contadas[cod]; esp, con = num(l.esperado), num(l.contado); d = con - esp; cs = costo(k)
        s = sku.setdefault(k, {"producto": k, "descripcion": txt(l.producto_desc), "costo": cs, "sistema": 0.0, "contado": 0.0, "falt": 0.0, "sobr": 0.0, "ubicaciones": 0, "falta_en": [], "sobra_en": []})
        s["sistema"] += esp; s["contado"] += con; s["ubicaciones"] += 1
        if d < 0: s["falt"] += -d; s["falta_en"].append(f"{cod}: {-d:g}")
        elif d > 0: s["sobr"] += d; s["sobra_en"].append(f"{cod}: {d:g}")
        tot["us"] += esp; tot["uc"] += con; tot["sistema"] += esp * cs; tot["contado"] += con * cs
        if d < 0: tot["falt"] += -d * cs
        if d > 0: tot["sobr"] += d * cs
        for par, val in (("bodega", txt(x.bodega) or "—"), ("zona", txt(x.zona) or "—")):
            g = gb[par].setdefault(val, {"nombre": val, "sistema": 0.0, "contado": 0.0}); g["sistema"] += esp * cs; g["contado"] += con * cs
        if abs(d) > EPS:
            dif.append({"codigo": cod, "bodega": txt(x.bodega), "zona": txt(x.zona), "producto": k, "descripcion": txt(l.producto_desc), "esperado": esp, "contado": con, "diferencia": d,
                        "costo": cs, "valor": r2(d * cs), "tipo": "faltante" if d < 0 else "sobrante", "inesperado": bool(l.inesperado)})
    dif.sort(key=lambda d: (-abs(d["valor"]), -abs(d["diferencia"])))
    skus = []
    for k in sorted(sku):
        s = sku[k]; s["diferencia"] = s["contado"] - s["sistema"]; cs = s["costo"]
        s["valor_sistema"] = r2(s["sistema"] * cs); s["valor_contado"] = r2(s["contado"] * cs); s["valor_dif"] = r2(s["diferencia"] * cs)
        s["compensado"] = min(s["falt"], s["sobr"]); s["falt_neto"] = s["falt"] - s["compensado"]; s["sobr_neto"] = s["sobr"] - s["compensado"]
        s["valor_compensado"] = r2(s["compensado"] * cs); s["valor_falt_neto"] = r2(s["falt_neto"] * cs); s["valor_sobr_neto"] = r2(s["sobr_neto"] * cs)
        skus.append(s)
    comp = sorted([s for s in skus if s["compensado"] > EPS], key=lambda s: -s["valor_compensado"])
    neto = {"falt": sum(s["valor_falt_neto"] for s in skus), "sobr": sum(s["valor_sobr_neto"] for s in skus), "comp": sum(s["valor_compensado"] for s in skus)}
    inc = [ser_inc(r) for r in incs]
    deff = []
    for r in inc:
        if r["tipo"] in DEF_TIPOS or r["impacto"] in ("No vendible", "Requiere revisión técnica"):
            cs = costo(r["producto"]); r["costo"] = cs; r["valor"] = r2(r["cantidad"] * cs); deff.append(r)
    por_tipo = {}
    for r in inc:
        t = por_tipo.setdefault(r["tipo"], {"tipo": r["tipo"], "tipo_txt": r["tipo_txt"], "incidencias": 0, "abiertas": 0}); t["incidencias"] += 1
        if r["estado"] != "resuelta": t["abiertas"] += 1
    mov = None
    if con_mov:
        try:
            m = await movimientos(db, c); est, cat_c, por_sku, lin_c = {}, {k: {"clave": k, "nombre": n, "unidades": 0.0, "bruto": 0.0, "valor": 0.0} for k, n in CAUSAS}, {}, []
            for d in m["_difs"]:
                cs = costo(d["producto"]); e = est.setdefault(d["estado"], {"estado": d["estado"], "lineas": 0, "valor_dif": 0.0, "valor_sin_explicar": 0.0})
                e["lineas"] += 1; e["valor_dif"] += d["diferencia"] * cs; e["valor_sin_explicar"] += d["diferencia_sin_explicar"] * cs
                t, po = d.get("mov_tipos") or {}, d.get("posible_origen") or []
                q = {"ov": t.get("OV", 0), "traslado": t.get("traslado", 0), "entrada": t.get("entrada", 0), "ajuste": t.get("ajuste", 0),
                     "cruce": sum(num(x["cantidad"]) for x in po if x["tipo"] == "cruce"), "ov_fuera": sum(num(x["cantidad"]) for x in po if x["tipo"] == "ov_fuera"), "sin": d["diferencia_sin_explicar"]}
                sk = por_sku.setdefault(d["producto"], {"producto": d["producto"], "descripcion": d["descripcion"], "costo": cs, "diferencia": 0.0, **{k: 0.0 for k, _ in CAUSAS}})
                sk["diferencia"] += d["diferencia"]
                for k, _ in CAUSAS:
                    cat_c[k]["unidades"] += q[k]; cat_c[k]["bruto"] += abs(q[k]); cat_c[k]["valor"] += q[k] * cs; sk[k] += q[k]
                lin_c.append({"codigo": d["codigo"], "producto": d["producto"], "descripcion": d["descripcion"], "esperado": d["esperado"], "contado": d["contado"], "diferencia": d["diferencia"],
                              "ov": q["ov"], "traslado": q["traslado"], "entrada": q["entrada"], "ajuste": q["ajuste"], "cruce": q["cruce"], "ov_fuera": q["ov_fuera"], "sin_explicar": q["sin"],
                              "estado": d["estado"], "costo": cs, "valor_dif": r2(d["diferencia"] * cs), "valor_sin_explicar": r2(q["sin"] * cs),
                              "docs": (d.get("docs") or []) + [{"tipo": "OV otra ubicación", "documento": x.get("documento"), "picking": x.get("picking"), "fecha": x.get("fecha"), "cantidad": x["cantidad"], "origen": x.get("ubicacion"), "destino": ""} for x in po if x["tipo"] == "ov_fuera"]})
            cats = []
            for k, _ in CAUSAS: cat_c[k]["valor"] = r2(cat_c[k]["valor"]); cats.append(cat_c[k])
            skus2 = []
            for x in por_sku.values(): x["valor_dif"] = r2(x["diferencia"] * x["costo"]); x["valor_sin"] = r2(x["sin"] * x["costo"]); skus2.append(x)
            skus2.sort(key=lambda a: (-abs(a["valor_sin"]), -abs(a["valor_dif"]))); lin_c.sort(key=lambda a: (-abs(a["valor_sin_explicar"]), -abs(a["valor_dif"])))
            for e in est.values(): e["valor_dif"] = r2(e["valor_dif"]); e["valor_sin_explicar"] = r2(e["valor_sin_explicar"])
            mov = {"ventana": m["ventana"], "resumen": m["resumen"], "por_estado": list(est.values()),
                   "causas": {"categorias": cats, "valor_diferencia": r2(sum(x["valor"] for x in cats)), "valor_explicado": r2(sum(x["valor"] for x in cats if x["clave"] != "sin"))},
                   "causas_sku": skus2[:1000], "diferencias": lin_c[:2000], "_lineas": lin_c, "_skus": skus2}
            if m["ventana"].get("aviso_mal_surtido"): aviso.append(m["ventana"]["aviso_mal_surtido"])
        except Exception as e:
            aviso.append(f"No se pudieron revisar los movimientos de Odoo: {str(e)[:150]}")
    lista = lambda o: [{**g, "sistema": r2(g["sistema"]), "contado": r2(g["contado"]), "diferencia": r2(g["contado"] - g["sistema"])} for _, g in sorted(o.items())]
    return {"conteo": {"id": id_, "nombre": txt(c.nombre), "alcance": txt(c.alcance), "estatus": txt(c.estatus), "tipo": txt(c.tipo) or "ubicacion", "fecha": iso(c.fecha), "fecha_cierre": iso(c.fecha_cierre)},
            "avance": {"ubicaciones": len(ubs), "contadas": len(contadas), "completo": len(contadas) == len(ubs)}, "avisos": aviso, "sin_costo": sin_costo,
            "valor": {"valor_sistema": r2(tot["sistema"]), "valor_contado": r2(tot["contado"]), "diferencia": r2(tot["contado"] - tot["sistema"]), "faltante": r2(tot["falt"]), "sobrante": r2(tot["sobr"]),
                      "unidades_sistema": tot["us"], "unidades_contado": tot["uc"], "por_bodega": lista(gb["bodega"]), "por_zona": lista(gb["zona"])},
            "faltantes_sobrantes": {"faltante_valor": r2(tot["falt"]), "sobrante_valor": r2(tot["sobr"]), "neto_valor": r2(tot["sobr"] - tot["falt"]), "lineas": len(dif), "detalle": dif[:3000]},
            "compensados": {"valor_compensado": r2(neto["comp"]), "faltante_neto_valor": r2(neto["falt"]), "sobrante_neto_valor": r2(neto["sobr"]), "productos": len(comp), "detalle": comp},
            "inventario": skus,
            "defectuosos": {"piezas": sum(r["cantidad"] for r in deff), "valor": r2(sum(r["valor"] for r in deff)), "detalle": deff},
            "incidencias": {"total": len(inc), "abiertas": sum(1 for r in inc if r["estado"] != "resuelta"), "por_tipo": list(por_tipo.values()), "detalle": inc},
            "movimientos": mov,
            "otras_un": resumen_otras_un([{"producto": l.producto, "producto_desc": l.producto_desc, "codigo": l.codigo, "contado": l.contado} for l in todas if es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)], cat),
            "_dif": dif, "_skus": skus, "_comp": comp, "_def": deff, "_inc": inc, "_mov": mov}


# ───────────────────────── COMPROMETIDO ─────────────────────────
async def com_vigente(db, id_):
    m = {"manual": {}, "decision": {}, "config": None}
    for r in (await db.execute(select(InvComprometido).where(InvComprometido.id_conteo == id_).order_by(InvComprometido.id))).scalars():
        f = txt(r.fuente)
        if f == "config":
            try: m["config"] = json.loads(txt(r.motivo))
            except Exception: pass
        elif f in m: m[f][NS(r.producto)] = r
    return m


async def config_com(db, id_, vig):
    cfg = vig["config"] or {}; defecto = []
    if not cfg.get("almacenes"):
        try:
            al = await O.almacenes_de_ubicaciones([txt(x.codigo) for x in await ubs_de(db, id_)])
            defecto = sorted({v for v in al["porCodigo"].values() if v})
        except Exception: pass
    modo = cfg.get("modo") or (("manual" if cfg.get("auto") is False else "exacto") if vig["config"] else "tope")
    return {"almacenes": cfg.get("almacenes") or defecto, "modo": modo, "auto": modo != "manual", "guardada": bool(vig["config"])}


N2 = lambda x: f"{round(x * 100) / 100:g}"


async def calcular_comprometido(db, c, dias=15, fin=None):
    id_ = c.id; dias = max(1, min(60, int(dias or 15)))
    fin = fin or await calcular_final(db, c)
    vig = await com_vigente(db, id_); cfg = await config_com(db, id_, vig); rec = await reco_vigente(db, id_)
    cods = {txt(x.codigo) for x in await ubs_de(db, id_)}
    od = {}
    for e in (await db.execute(select(InvExistencia))).scalars():
        if txt(e.codigo) in cods and num(e.reservado) > 0: od[NS(e.producto)] = od.get(NS(e.producto), 0) + num(e.reservado)
    cf = [s["producto"] for s in fin["_skus"] if s["sistema"] > s["contado"]]; cs_ = [s["producto"] for s in fin["_skus"] if s["contado"] > s["sistema"]]
    salidas, hechas, avisos = {}, {}, []
    try: salidas = await O.salidas_pendientes(cf, cfg["almacenes"])
    except Exception as e: avisos.append(f"No pude leer las salidas pendientes de Odoo ({str(e)[:120]}). Para faltantes se muestra solo reservado y captura manual.")
    try: hechas = await O.salidas_hechas(list(cods), cs_, dias)
    except Exception as e: avisos.append(f"No pude leer las salidas ya hechas de Odoo ({str(e)[:120]}). Para sobrantes se muestra solo la captura manual.")
    filas, usados = [], set()
    def agrega(prod, desc, sis, con, costo):
        k = NS(prod); usados.add(k)
        tipo = "faltante" if sis > con + EPS else ("sobrante" if con > sis + EPS else "ninguno"); base = abs(con - sis)
        sl, hh, mv = salidas.get(k), hechas.get(k), vig["manual"].get(k); man = num(mv.cantidad) if mv else 0; o = od.get(k, 0); sal = tra = 0
        if tipo == "faltante": sal = sl["demanda"] if sl else 0; comp = max(o, sal) + man
        elif tipo == "sobrante": tra = hh["cantidad"] if hh else 0; comp = tra + man
        else: comp = max(o, 0) + man
        if comp <= EPS: return
        dv = vig["decision"].get(k); sug = min(comp, base)
        e = "sin_faltante" if tipo == "ninguno" else ("cuadra" if abs(comp - base) < EPS else ("parcial" if comp < base else "excede"))
        auto = not dv and ((cfg["modo"] == "tope" and e in ("cuadra", "parcial", "excede")) or (cfg["modo"] == "exacto" and e == "cuadra"))
        desc_ = min(num(dv.cantidad), base, comp) if dv else (sug if auto else 0)
        T = "sobrante" if tipo == "sobrante" else "faltante"
        que = "ya movido en Odoo (salida hecha / en tránsito)" if tipo == "sobrante" else "apartado o con salida pendiente"
        expl = {"cuadra": f"Lo comprometido ({N2(comp)}) es exactamente el {T} ({N2(base)}). Si confirmas que ese material está {que}, puedes descontarlo completo.",
                "parcial": f"Lo comprometido ({N2(comp)}) cubre solo una parte del {T} ({N2(base)}). Quedarían {N2(base - comp)} sin explicar.",
                "excede": f"Lo comprometido ({N2(comp)}) es mayor que el {T} ({N2(base)}). Solo se puede descontar hasta el {T}; revisa si está bien capturado o si parte sigue su curso normal.",
                "sin_faltante": f"Hay {N2(comp)} comprometido pero este producto no tiene diferencia (sistema {N2(sis)}, contado {N2(con)}); no hay nada que descontar."}[e]
        if sl and tipo == "faltante": expl += " Salida pendiente en Odoo: " + ", ".join(f"{d['doc']} ({N2(d['demanda'])})" for d in sl["docs"]) + ". Reservado y salida se toman como el mismo material (se usa el mayor)."
        if hh and tipo == "sobrante": expl += f" Salida hecha en Odoo (últimos {dias} días): " + ", ".join(f"{d['doc']} ({N2(d['cantidad'])} → {d['destino']})" for d in hh["docs"][:5]) + ". El sistema ya la descontó pero el producto sigue físicamente aquí."
        if k in rec: expl += " Ojo: este producto ya tiene reconteo externo; revisa que no estés descontando dos veces la misma explicación."
        docs = ", ".join(d["doc"] for d in sl["docs"]) if sl and tipo == "faltante" else (", ".join(d["doc"] for d in hh["docs"][:5]) if hh and tipo == "sobrante" else "")
        ajuste = -desc_ if tipo == "sobrante" else desc_
        filas.append({"producto": prod, "descripcion": desc, "tipo": tipo, "sistema": sis, "contado": con, "faltante": base, "odoo": o, "salida": sal, "transito": tra, "docs_salida": docs,
                      "manual": man, "motivo": txt(mv.motivo) if mv else "", "comprometido": comp, "sugerido": sug, "descontar": desc_, "auto": auto, "dif_original": con - sis, "dif_ajustada": con - sis + ajuste,
                      "estado": e, "explicacion": expl, "costo": costo, "valor_descontado": r2(desc_ * costo), "decidido_por": txt(dv.usuario) if dv else "", "fecha_decision": iso(dv.fecha) if dv else ""})
    for s in fin["_skus"]: agrega(s["producto"], s["descripcion"], s["sistema"], s["contado"], s["costo"])
    cm = await costos_mapa(db)
    for k, v in vig["manual"].items():
        if k in usados or num(v.cantidad) <= EPS: continue
        filas.append({"producto": txt(v.producto), "descripcion": "", "tipo": "ninguno", "sistema": 0, "contado": 0, "faltante": 0, "odoo": 0, "salida": 0, "transito": 0, "docs_salida": "", "manual": num(v.cantidad),
                      "motivo": txt(v.motivo), "comprometido": num(v.cantidad), "sugerido": 0, "descontar": 0, "auto": False, "dif_original": 0, "dif_ajustada": 0, "estado": "fuera_conteo",
                      "explicacion": "Este producto no forma parte del conteo; revisa la clave.", "costo": cm.get(txt(v.producto), 0), "valor_descontado": 0})
    orden = {"cuadra": 0, "excede": 1, "parcial": 2, "sin_faltante": 3, "fuera_conteo": 4}
    filas.sort(key=lambda f: (orden[f["estado"]], -f["faltante"] * f["costo"]))
    cuenta = {}; tot = {k: 0.0 for k in ("falt_u0", "falt_v0", "falt_u1", "falt_v1", "sobr_u0", "sobr_v0", "sobr_u1", "sobr_v1", "desc_u", "desc_v", "desc_falt_u", "desc_falt_v", "desc_sobr_u", "desc_sobr_v")}
    for s in fin["_skus"]:
        d = s["contado"] - s["sistema"]
        if d < 0: tot["falt_u0"] += -d; tot["falt_v0"] += -d * s["costo"]
        elif d > 0: tot["sobr_u0"] += d; tot["sobr_v0"] += d * s["costo"]
    for f in filas:
        cuenta[f["estado"]] = cuenta.get(f["estado"], 0) + 1; tot["desc_u"] += f["descontar"]; tot["desc_v"] += f["descontar"] * f["costo"]
        if f["tipo"] == "faltante": tot["desc_falt_u"] += f["descontar"]; tot["desc_falt_v"] += f["descontar"] * f["costo"]
        if f["tipo"] == "sobrante": tot["desc_sobr_u"] += f["descontar"]; tot["desc_sobr_v"] += f["descontar"] * f["costo"]
    tot["falt_u1"] = tot["falt_u0"] - tot["desc_falt_u"]; tot["falt_v1"] = tot["falt_v0"] - tot["desc_falt_v"]; tot["sobr_u1"] = tot["sobr_u0"] - tot["desc_sobr_u"]; tot["sobr_v1"] = tot["sobr_v0"] - tot["desc_sobr_v"]
    for k in list(tot):
        if "_v" in k: tot[k] = r2(tot[k])
    return {"conteo": {"id": id_, "nombre": txt(c.nombre)}, "avisos": fin["avisos"] + avisos, "filas": filas, "cuenta": cuenta, "totales": tot, "dias": dias,
            "decididos": sum(1 for f in filas if f["descontar"] > 0), "automaticos": sum(1 for f in filas if f["auto"]), "config": cfg,
            "nota": f"FALTANTES: lo comprometido es lo reservado en Odoo o con salida pendiente (apartado, en surtido). SOBRANTES: lo comprometido es producto que sigue físicamente en la ubicación pero Odoo ya lo movió (salida hecha en los últimos {dias} días / en tránsito)."}


@router.get("/conteos/{id_}/comprometido")
async def api_comprometido(id_: str, dias: int = 15, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_comprometido(db, await conteo_o_404(db, id_), dias)
    try: r["almacenes_disponibles"] = await O.almacenes_odoo()
    except Exception: r["almacenes_disponibles"] = []; r["avisos"].append("No pude leer la lista de almacenes de Odoo.")
    return r


@router.post("/conteos/{id_}/comprometido/config")
async def api_config_com(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    alm = [txt(x) for x in (body.get("almacenes") or []) if txt(x)]
    if not alm: raise HTTPException(422, "Elige al menos un almacén (los de donde estás trabajando).")
    modo = body.get("modo") if body.get("modo") in ("exacto", "tope", "manual") else "exacto"
    db.add(InvComprometido(id_conteo=id_, producto="__CONFIG__", fuente="config", cantidad=0, motivo=json.dumps({"almacenes": alm, "modo": modo}), usuario=u["email"], fecha=ahora()))
    await log(db, u["email"], "COMPROMETIDO_CONFIG", f"{id_} · {', '.join(alm)} · modo={modo}"); await db.commit()
    return {"ok": True}


@router.post("/conteos/{id_}/comprometido/cargar")
async def api_cargar_com(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    avisos, vistos, nuevas = [], set(), []
    for i, f in enumerate(sin_encabezado(parse_tabla(body.get("texto")))):
        prod = txt(f[0]) if f else ""; crudo = txt(f[1]).replace(" ", "") if len(f) > 1 else ""
        if not prod or crudo == "": continue
        n = _num(crudo)
        if n is None or n < 0: avisos.append(f"Fila {i + 1} ({prod}): cantidad no válida «{f[1]}», se omitió."); continue
        k = NS(prod)
        if k in vistos: avisos.append(f"{prod} viene repetido: se usa la última fila."); nuevas = [x for x in nuevas if NS(x[0]) != k]
        vistos.add(k); nuevas.append((prod, n, txt(f[2]) if len(f) > 2 else ""))
    if not nuevas: raise HTTPException(422, "No encontré filas con producto y cantidad. Pega columnas: Producto, Cantidad, Motivo.")
    t = ahora()
    for p, n, m in nuevas: db.add(InvComprometido(id_conteo=id_, producto=p[:60], fuente="manual", cantidad=n, motivo=m[:250], usuario=u["email"], fecha=t))
    await log(db, u["email"], "COMPROMETIDO_CARGA", f"{id_} · {len(nuevas)} productos"); await db.commit()
    return {"importados": len(nuevas), "avisos": avisos}


@router.post("/conteos/{id_}/comprometido/descontar")
async def api_descontar_com(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_comprometido(db, await conteo_o_404(db, id_), body.get("dias") or 15)
    por = {NS(f["producto"]): f for f in r["filas"]}; nuevas = []
    for it in body.get("items") or []:
        f = por.get(NS(it.get("producto")))
        if not f: raise HTTPException(404, f"El producto {it.get('producto')} no tiene comprometido.")
        try: n = float(it.get("cantidad")); assert n >= 0
        except Exception: raise HTTPException(422, f"Cantidad no válida para {f['producto']}")
        if n > f["faltante"] + 1e-9: raise HTTPException(422, f"{f['producto']}: no puedes descontar más que el faltante ({f['faltante']:g}).")
        if n > f["comprometido"] + 1e-9: raise HTTPException(422, f"{f['producto']}: no puedes descontar más que lo comprometido ({f['comprometido']:g}).")
        nuevas.append((f["producto"], n, txt(it.get("motivo"))))
    if not nuevas: raise HTTPException(422, "Nada que guardar.")
    t = ahora()
    for p, n, m in nuevas: db.add(InvComprometido(id_conteo=id_, producto=p[:60], fuente="decision", cantidad=n, motivo=m[:250], usuario=u["email"], fecha=t))
    await log(db, u["email"], "COMPROMETIDO_DESCUENTO", f"{id_} · " + ", ".join(f"{p}={n:g}" for p, n, _ in nuevas)); await db.commit()
    return {"guardados": len(nuevas)}


ETQ_COM = {"cuadra": "Cuadra con el faltante", "parcial": "Cubre parte del faltante", "excede": "Mayor que el faltante", "sin_faltante": "Sin faltante", "fuera_conteo": "Fuera del conteo"}


@router.get("/conteos/{id_}/comprometido/csv")
async def api_com_csv(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_comprometido(db, await conteo_o_404(db, id_))
    f = [["Producto", "Descripcion", "Tipo", "Sistema", "Contado", "Diferencia (abs)", "Reservado Odoo", "Salida pendiente Odoo", "Salida hecha / en transito Odoo", "Documentos", "Comprometido manual", "Motivo",
          "Comprometido total", "Sugerido descontar", "Descontado", "Dif. original", "Dif. ajustada", "Resultado", "Explicacion"]]
    for x in r["filas"]:
        f.append([x["producto"], x["descripcion"], x["tipo"], x["sistema"], x["contado"], x["faltante"], x["odoo"], x["salida"], x["transito"], x["docs_salida"], x["manual"], x["motivo"], x["comprometido"],
                  x["sugerido"], x["descontar"], x["dif_original"], x["dif_ajustada"], ETQ_COM[x["estado"]], x["explicacion"]])
    return archivo_csv(f"{id_}_comprometido.csv", f)


@router.get("/conteos/{id_}/comprometido/diagnostico")
async def api_diag_com(id_: str, producto: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    prod = txt(producto); k = NS(prod)
    if not prod: raise HTTPException(422, "Escribe la clave del producto.")
    c = await conteo_o_404(db, id_); vig = await com_vigente(db, id_); cfg = await config_com(db, id_, vig); fin = await calcular_final(db, c)
    s = next((x for x in fin["_skus"] if NS(x["producto"]) == k), None)
    out = {"producto": prod, "almacenes": cfg["almacenes"], "modo": cfg["modo"], "dias": 60, "conteo": {"sistema": s["sistema"], "contado": s["contado"], "diferencia": s["contado"] - s["sistema"]} if s else None}
    cods = {txt(x.codigo) for x in await ubs_de(db, id_)}
    out["reservado_odoo"] = sum(num(e.reservado) for e in (await db.execute(select(InvExistencia))).scalars() if txt(e.codigo) in cods and NS(e.producto) == k)
    try: out["pendientes"] = (await O.salidas_pendientes([prod], None)).get(k, {}).get("docs", [])
    except Exception as e: out["pendientes"] = []; out["error_pendientes"] = str(e)[:150]
    try: out["hechas"] = (await O.salidas_hechas(list(cods), [prod], 60)).get(k, {}).get("docs", [])
    except Exception as e: out["hechas"] = []; out["error_hechas"] = str(e)[:150]
    return out


# ───────────────────────── CIFRA REAL y RECONTEO SKU (calculo) ─────────────────────────
def cifra_real(r, cm, rec):
    mvk = {NS(s["producto"]): s for s in (r["_mov"] or {}).get("_skus", [])} if r["_mov"] else {}
    cmk = {NS(f["producto"]): f for f in cm["filas"]} if cm else {}
    T = {k: 0.0 for k in ("sis", "con", "falt", "sobr", "fu", "su", "d0", "rc", "mv", "cp", "abs")}; top = []; n = {"total": 0, "con_dif": 0, "cuadran": 0}; nrec = 0
    for s in r["_skus"]:
        k = NS(s["producto"]); cs = s["costo"] or 0; S, C = s["sistema"], s["contado"]; rv = rec.get(k) if rec else None; has = rv is not None and rv.recontado is not None
        C1 = num(rv.recontado) if has else C; m = mvk.get(k); mv = (num(m["ov"]) + num(m["traslado"]) + num(m["entrada"]) + num(m["ajuste"])) if m else 0
        d0, d1 = C - S, C1 - S; d2 = d1 - mv; f = cmk.get(k); dF = dS = 0
        if f and f["descontar"] > 0:
            if f["tipo"] == "faltante": dF = min(f["descontar"], -d2) if d2 < 0 else 0
            else: dS = min(f["descontar"], d2) if d2 > 0 else 0
        d3 = d2 + dF - dS; S2 = S + mv - dF; C2 = C1 - dS
        if has: nrec += 1
        T["sis"] += S2 * cs; T["con"] += C2 * cs; T["d0"] += d0 * cs; T["rc"] += (d1 - d0) * cs; T["mv"] += (d2 - d1) * cs; T["cp"] += (d3 - d2) * cs; T["abs"] += abs(d3 * cs)
        if d3 < -EPS: T["falt"] += -d3 * cs; T["fu"] += -d3
        elif d3 > EPS: T["sobr"] += d3 * cs; T["su"] += d3
        n["total"] += 1
        if abs(d3) < EPS: n["cuadran"] += 1
        else: n["con_dif"] += 1; top.append({"producto": s["producto"], "descripcion": s["descripcion"], "unidades": r2(d3), "valor": r2(d3 * cs)})
    top.sort(key=lambda a: -abs(a["valor"])); dif = T["con"] - T["sis"]; w = r["valor"]
    b = lambda clave, et, v, det: {"clave": clave, "etiqueta": et, "valor": r2(v), "detalle": det}
    pasos = [b("original", "Diferencia según conteo", T["d0"], "Contado menos sistema, a costo, sin ningún ajuste."),
             b("reconteo", "Reconteo externo", T["rc"], f"{nrec} producto(s) recontados: se toma el reconteo en lugar del conteo original." if nrec else "No se cargó reconteo externo."),
             b("movimientos", "Movimientos de Odoo durante el conteo", T["mv"], "Ventas, traslados, entradas y ajustes que movieron el sistema mientras se contaba." if r["_mov"] else "No incluidos (no se consultaron los movimientos de Odoo)."),
             b("comprometido", "Comprometido / en tránsito", T["cp"], f"{cm.get('decididos', 0)} producto(s) con descuento: apartado o ya movido en Odoo." if cm else "No calculado.")]
    ou = r["otras_un"] or {}
    return {"hay_ajustes": abs(T["rc"]) + abs(T["mv"]) + abs(T["cp"]) > 0.005, "completos": {"movimientos": bool(r["_mov"]), "reconteo": nrec > 0, "comprometido": bool(cm)},
            "original": {"valor_sistema": w["valor_sistema"], "valor_contado": w["valor_contado"], "diferencia": w["diferencia"]},
            "valor_sistema": r2(T["sis"]), "valor_contado": r2(T["con"]), "diferencia": r2(dif), "faltante": r2(T["falt"]), "sobrante": r2(T["sobr"]), "faltante_u": r2(T["fu"]), "sobrante_u": r2(T["su"]),
            "pct": r2(dif / T["sis"] * 100) if T["sis"] else 0, "puente": pasos + [b("real", "Diferencia real", dif, "Lo que realmente falta o sobra después de todo lo anterior.")],
            "top": top[:15], "abs_total": r2(T["abs"]), "productos": n, "otras_un": {"productos": ou.get("productos", 0), "unidades": ou.get("unidades", 0)},
            "no_vendible": r["defectuosos"]["valor"], "no_vendible_piezas": r["defectuosos"]["piezas"], "sin_costo": len(r["sin_costo"])}


def _limpia(r):
    for k in ("_dif", "_skus", "_comp", "_def", "_inc", "_mov"): r.pop(k, None)
    if r.get("movimientos"): r["movimientos"].pop("_lineas", None); r["movimientos"].pop("_skus", None)
    return r


@router.get("/conteos/{id_}/final")
async def api_final(id_: str, actualizar_costos: bool = False, movimientos_: bool = False, ajustado: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_); r = await calcular_final(db, c, actualizar_costos, movimientos_)
    if ajustado:
        try:
            cm = await calcular_comprometido(db, c, 15, r); t = cm["totales"]
            try: r["real"] = cifra_real(r, cm, await reco_vigente(db, id_))
            except Exception as e2: r["real"] = None; r["avisos"].append(f"No se pudo consolidar la cifra real: {str(e2)[:100]}")
            v = r["valor"]
            r["ajustado"] = {"totales": t, "decididos": cm["decididos"], "automaticos": cm["automaticos"], "almacenes": cm["config"]["almacenes"], "modo": cm["config"]["modo"],
                             "diferencia_original": v["diferencia"], "diferencia_ajustada": r2(v["diferencia"] + t["desc_falt_v"] - t["desc_sobr_v"]),
                             "valor_sistema": r2(v["valor_sistema"] - t["desc_falt_v"]), "valor_contado": r2(v["valor_contado"] - t["desc_sobr_v"]),
                             "faltante": r2(v["faltante"] - t["desc_falt_v"]), "sobrante": r2(v["sobrante"] - t["desc_sobr_v"])}
        except Exception as e:
            r["ajustado"] = None; r["avisos"].append(f"No se pudo calcular el ajuste por comprometido: {str(e)[:100]}")
    r["firmas"] = parse_firmas(c.firmas)
    return _limpia(r)


@router.get("/conteos/{id_}/final/csv")
async def api_final_csv(id_: str, seccion: str = "faltantes", db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_); sec = seccion or "faltantes"
    r = await calcular_final(db, c, False, sec in ("movimientos", "causas"))
    if sec == "valor":
        v = r["valor"]
        f = [["Concepto", "Valor"], ["Valor del sistema (ubicaciones contadas)", v["valor_sistema"]], ["Valor contado", v["valor_contado"]], ["Diferencia", v["diferencia"]], ["Faltantes (valor)", v["faltante"]],
             ["Sobrantes (valor)", v["sobrante"]], ["Faltantes compensados (valor)", r["compensados"]["valor_compensado"]], ["Faltante neto (valor)", r["compensados"]["faltante_neto_valor"]],
             ["Sobrante neto (valor)", r["compensados"]["sobrante_neto_valor"]], ["Inventario defectuoso (valor)", r["defectuosos"]["valor"]], [], ["Bodega", "Sistema", "Contado", "Diferencia"]]
        f += [[g["nombre"], g["sistema"], g["contado"], g["diferencia"]] for g in v["por_bodega"]] + [[], ["Zona", "Sistema", "Contado", "Diferencia"]] + [[g["nombre"], g["sistema"], g["contado"], g["diferencia"]] for g in v["por_zona"]]
    elif sec == "otras_un":
        f = [["Clave", "Descripcion", "Unidad de negocio", "Unidades", "Ubicaciones (hasta 6)"]] + [[d["producto"], d["descripcion"], d["unidad"], d["unidades"], " | ".join(d["ubicaciones"])] for d in r["otras_un"]["detalle"]]
    elif sec == "inventario":
        f = [["Clave", "Descripcion", "Costo unitario", "Sistema", "Contado", "Diferencia", "Valor sistema", "Valor contado", "Valor diferencia", "Ubicaciones"]] + [[s["producto"], s["descripcion"], s["costo"], s["sistema"], s["contado"], s["diferencia"], s["valor_sistema"], s["valor_contado"], s["valor_dif"], s["ubicaciones"]] for s in r["_skus"]]
    elif sec == "compensados":
        f = [["Clave", "Descripcion", "Costo unitario", "Faltante total", "Sobrante total", "Compensado (uds)", "Valor compensado", "Faltante neto", "Sobrante neto", "Valor faltante neto", "Valor sobrante neto", "Falta en", "Sobra en"]] + \
            [[s["producto"], s["descripcion"], s["costo"], s["falt"], s["sobr"], s["compensado"], s["valor_compensado"], s["falt_neto"], s["sobr_neto"], s["valor_falt_neto"], s["valor_sobr_neto"], " | ".join(s["falta_en"]), " | ".join(s["sobra_en"])] for s in r["_comp"]]
    elif sec == "defectuosos":
        f = [["Incidencia", "Fecha", "Ubicacion", "Numero de parte o descripcion", "Tipo", "Cantidad", "Impacto", "Accion tomada", "Costo unitario", "Valor", "Detalle", "Evidencia", "Estado", "Auditor"]] + \
            [[d["id"], d["fecha"], d["codigo"], d["producto"], d["tipo_txt"], d["cantidad"], d["impacto"], d["accion"], d["costo"], d["valor"], d["detalle"], "(foto adjunta)" if d["evidencia"].startswith("data:") else d["evidencia"], d["estado"], d["auditor"]] for d in r["_def"]]
    elif sec == "causas":
        mv = r["_mov"]; f = [["Clave", "Descripcion", "Costo unitario", "Diferencia", "OV (salidas)", "Traslados", "Entradas", "Ajustes", "Compensacion entre ubicaciones", "OV de otra ubicacion", "Sin explicar", "Valor diferencia", "Valor sin explicar"]]
        if mv: f += [[x["producto"], x["descripcion"], x["costo"], x["diferencia"], x["ov"], x["traslado"], x["entrada"], x["ajuste"], x["cruce"], x["ov_fuera"], x["sin"], x["valor_dif"], x["valor_sin"]] for x in mv["_skus"]]
    elif sec == "movimientos":
        mv = r["_mov"]; f = [["Ubicacion", "Clave", "Descripcion", "Sistema", "Contado", "Diferencia", "OV (salidas)", "Traslados", "Entradas", "Ajustes", "Compensacion entre ubicaciones", "OV de otra ubicacion", "Sin explicar", "Estado", "Valor diferencia", "Valor sin explicar", "Documentos"]]
        if mv:
            for d in mv["_lineas"]:
                f.append([d["codigo"], d["producto"], d["descripcion"], d["esperado"], d["contado"], d["diferencia"], d["ov"], d["traslado"], d["entrada"], d["ajuste"], d["cruce"], d["ov_fuera"], d["sin_explicar"], d["estado"], d["valor_dif"], d["valor_sin_explicar"],
                          " | ".join(f"{x['tipo']} {x.get('documento') or x.get('picking') or ''} {x.get('fecha') or ''} {x['cantidad']:g} ({x.get('origen') or ''}→{x.get('destino') or ''})" for x in d["docs"])])
    else:
        sec = "faltantes"
        f = [["Ubicacion", "Bodega", "Zona", "Clave", "Descripcion", "Sistema", "Contado", "Diferencia", "Tipo", "Costo unitario", "Valor", "No esperado"]] + \
            [[d["codigo"], d["bodega"], d["zona"], d["producto"], d["descripcion"], d["esperado"], d["contado"], d["diferencia"], d["tipo"], d["costo"], d["valor"], "SI" if d["inesperado"] else ""] for d in r["_dif"]]
    return archivo_csv(f"{id_}_final_{sec}.csv", f)


# ───────────────────────── RECONTEO EXTERNO: resultado ─────────────────────────
async def calcular_reco_sku(db, c, con_mov=False):
    id_ = c.id; vig = await reco_vigente(db, id_); fin = await calcular_final(db, c, False, con_mov); aviso = list(fin["avisos"])
    mov_sku = {NS(s["producto"]): s for s in (fin["_mov"] or {}).get("_skus", [])} if fin["_mov"] else {}
    costos = await costos_mapa(db); usados, filas = set(), []
    def cls(sis, con, rec, esp_mov, tiene_mov):
        if rec is None: return "cuadrado" if abs(con - sis) < EPS else "sin_reconteo"
        if abs(rec - sis) < EPS: return "cuadrado" if abs(con - sis) < EPS else "cuadra"
        if tiene_mov and abs(rec - esp_mov) < EPS: return "cuadra_mov"
        if sis == 0: return "sin_sistema"
        if abs(rec - con) < EPS: return "confirma"
        return "se_acerca" if abs(rec - sis) < abs(con - sis) else "se_aleja"
    def agrega(k, producto, desc, sis, con, costo):
        v = vig.get(NS(k)); rec = num(v.recontado) if v else None; ms = mov_sku.get(NS(k))
        neto = (ms["ov"] + ms["traslado"] + ms["entrada"] + ms["ajuste"]) if ms else 0; esp_mov = sis + neto
        e = cls(sis, con, rec, esp_mov, bool(ms) and abs(neto) > EPS); usados.add(NS(k)); dr = None if rec is None else rec - sis; R = "" if rec is None else N2(rec)
        expl = {"cuadra": f"El reconteo ({R}) coincide con el sistema ({N2(sis)}). El conteo original traía {N2(con)}: el error estaba en el conteo original.",
                "cuadra_mov": f"El sistema decía {N2(sis)} pero hubo movimientos de Odoo por {N2(neto)} desde el inicio; se esperaba {N2(esp_mov)} y el reconteo dio {R}. La diferencia se explica por esos movimientos.",
                "confirma": f"El reconteo repite el conteo original ({R}) y difiere del sistema ({N2(sis)}) en {'' if dr is None else N2(dr)}. Dos conteos coinciden: la diferencia probablemente es real.",
                "se_acerca": f"El reconteo ({R}) quedó más cerca del sistema que el conteo original ({N2(con)}) pero aún difiere en {'' if dr is None else N2(dr)}. Revisa las ubicaciones de la observación.",
                "se_aleja": f"El reconteo ({R}) se alejó del sistema ({N2(sis)}) más que el conteo original ({N2(con)}). Las tres cifras no concuerdan: conviene recontar por ubicación.",
                "sin_sistema": f"El sistema no tiene este producto en las ubicaciones contadas (existencia 0) pero se contaron {R}. Puede ser otra unidad de negocio, producto no descargado o una clave distinta.",
                "sin_reconteo": f"Tiene diferencia de {N2(con - sis)} en el conteo original y no aparece en el reconteo externo.",
                "fuera_conteo": "El reconteo trae este producto pero no forma parte de este conteo. Revisa que la clave esté bien escrita.", "cuadrado": "Sin diferencia."}[e]
        filas.append({"explicacion": expl, "producto": producto, "descripcion": desc, "sistema": sis, "contado": con, "recontado": rec, "dif_conteo": con - sis, "dif_reco": dr, "mov_neto": neto,
                      "esperado_mov": esp_mov if ms else None, "estado": e, "costo": costo, "valor_dif_reco": None if rec is None else r2((rec - sis) * costo), "valor_dif_conteo": r2((con - sis) * costo),
                      "observacion": txt(v.observacion) if v else "", "personas": txt(v.personas) if v else "", "cargado_por": txt(v.cargado_por) if v else "", "fecha_carga": iso(v.fecha) if v else ""})
    for s in fin["_skus"]: agrega(s["producto"], s["producto"], s["descripcion"], s["sistema"], s["contado"], s["costo"])
    for k, v in vig.items():
        if k in usados: continue
        agrega(v.producto, txt(v.producto), "", 0, 0, costos.get(txt(v.producto), 0)); filas[-1]["estado"] = "fuera_conteo"
    orden = {"se_aleja": 0, "confirma": 1, "se_acerca": 2, "sin_sistema": 3, "sin_reconteo": 4, "fuera_conteo": 5, "cuadra_mov": 6, "cuadra": 7, "cuadrado": 8}
    filas = [f for f in filas if not (f["estado"] == "cuadrado" and f["recontado"] is None)]
    filas.sort(key=lambda f: (orden[f["estado"]], -abs(f["valor_dif_conteo"] if f["valor_dif_reco"] is None else f["valor_dif_reco"])))
    cuenta, uds, val = {}, {"antes": 0.0, "despues": 0.0}, {"antes": 0.0, "despues": 0.0}
    for f in filas:
        cuenta[f["estado"]] = cuenta.get(f["estado"], 0) + 1
        if f["recontado"] is not None: uds["antes"] += abs(f["dif_conteo"]); uds["despues"] += abs(f["dif_reco"]); val["antes"] += abs(f["valor_dif_conteo"]); val["despues"] += abs(f["valor_dif_reco"])
    f2 = {"original": {"falt_u": 0.0, "sobr_u": 0.0, "falt_v": 0.0, "sobr_v": 0.0}, "final": {"falt_u": 0.0, "sobr_u": 0.0, "falt_v": 0.0, "sobr_v": 0.0}, "aparte": {"productos": 0, "unidades": 0.0, "valor": 0.0}}
    for f in filas:
        if f["estado"] in ("sin_sistema", "fuera_conteo"): f2["aparte"]["productos"] += 1; f2["aparte"]["unidades"] += f["recontado"] or 0; f2["aparte"]["valor"] += (f["recontado"] or 0) * f["costo"]; continue
        d0 = f["dif_conteo"]; d1 = d0 if f["recontado"] is None else f["dif_reco"]; cs = f["costo"]
        for key, d in (("original", d0), ("final", d1)):
            if d < 0: f2[key]["falt_u"] += -d; f2[key]["falt_v"] += -d * cs
            elif d > 0: f2[key]["sobr_u"] += d; f2[key]["sobr_v"] += d * cs
    for key in ("original", "final"):
        o = f2[key]; o["falt_v"] = r2(o["falt_v"]); o["sobr_v"] = r2(o["sobr_v"]); o["neto_v"] = r2(o["sobr_v"] - o["falt_v"])
    f2["aparte"]["valor"] = r2(f2["aparte"]["valor"])
    if not con_mov: aviso.append("Sin movimientos de Odoo: «cuadra con movimientos» no se evalúa. Marca la casilla para incluirlos.")
    return {"conteo": {"id": id_, "nombre": txt(c.nombre)}, "avisos": aviso, "filas": filas, "cuenta": cuenta, "recontados": sum(1 for f in filas if f["recontado"] is not None), "con_reconteo_cargado": len(vig),
            "unidades_dif": uds, "final": f2, "valor_dif": {"antes": r2(val["antes"]), "despues": r2(val["despues"])},
            "nota_final": "Diferencia final por producto (neta): usa el reconteo externo donde existe y el conteo original donde no. Es neta por producto, por eso no coincide con faltantes/sobrantes por ubicación del reporte final.",
            "nota": "El reconteo externo es por producto: no dice en qué ubicación está la diferencia. Se compara contra el total del sistema en las ubicaciones contadas."}


@router.get("/conteos/{id_}/reconteo-sku")
async def api_reco_sku(id_: str, movimientos_: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    return await calcular_reco_sku(db, await conteo_o_404(db, id_), movimientos_)


@router.get("/conteos/{id_}/reconteo-sku/csv")
async def api_reco_sku_csv(id_: str, movimientos_: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_reco_sku(db, await conteo_o_404(db, id_), movimientos_)
    f = [["Producto", "Descripcion", "Sistema", "Contado original", "Reconteo externo", "Dif. original", "Dif. tras reconteo", "Movimientos netos", "Esperado con movimientos", "Resultado", "Costo", "Valor dif. tras reconteo", "Observacion", "Explicacion"]]
    for x in r["filas"]:
        f.append([x["producto"], x["descripcion"], x["sistema"], x["contado"], "" if x["recontado"] is None else x["recontado"], x["dif_conteo"], "" if x["dif_reco"] is None else x["dif_reco"], x["mov_neto"],
                  "" if x["esperado_mov"] is None else x["esperado_mov"], x["estado"], x["costo"], "" if x["valor_dif_reco"] is None else x["valor_dif_reco"], x["observacion"], x["explicacion"]])
    return archivo_csv(f"{id_}_reconteo_externo_sku.csv", f)


# ───────────────────────── FIRMAS ─────────────────────────
MAX_FIRMAS = 6


def parse_firmas(s):
    try:
        a = json.loads(s or "[]")
        return [{"nombre": txt(x.get("nombre")), "cargo": txt(x.get("cargo"))} for x in (a if isinstance(a, list) else [])[:MAX_FIRMAS] if txt(x.get("nombre"))]
    except Exception:
        return []


@router.get("/conteos/{id_}/firmas")
async def api_firmas(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    return {"firmas": parse_firmas((await conteo_o_404(db, id_)).firmas), "max": MAX_FIRMAS}


@router.post("/conteos/{id_}/firmas")
async def api_guardar_firmas(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    lista = [{"nombre": txt(x.get("nombre"))[:80], "cargo": txt(x.get("cargo"))[:80]} for x in (body.get("firmas") or []) if txt(x.get("nombre"))]
    if len(lista) > MAX_FIRMAS: raise HTTPException(422, f"Máximo {MAX_FIRMAS} personas")
    c.firmas = json.dumps(lista, ensure_ascii=False)
    await log(db, u["email"], "FIRMAS_INFORME", f"{id_} · " + ", ".join(x["nombre"] for x in lista)); await db.commit()
    return {"firmas": lista}


# ───────────────────────── COMPARAR CONTEOS ─────────────────────────
def _datos_cmp(c, ubs, lineas, cat):
    d = {}
    for x in ubs:
        if x.estado == "contada": d[txt(x.codigo)] = {"codigo": txt(x.codigo), "zona": txt(x.zona), "bodega": txt(x.bodega), "esp": 0.0, "con": 0.0, "abs": 0.0, "falt": 0.0, "sobr": 0.0, "lineas": {}, "n_dif": 0}
    for l in lineas:
        u = d.get(txt(l.codigo))
        if u is None or l.contado is None or es_otra_un(bool(l.inesperado), l.origen, l.producto, cat): continue
        esp, con = num(l.esperado), num(l.contado); df = con - esp; pr = txt(l.producto)
        u["esp"] += esp; u["con"] += con; u["abs"] += abs(df); u["falt"] += max(0, -df); u["sobr"] += max(0, df)
        if abs(df) > EPS: u["n_dif"] += 1
        u["lineas"][pr] = {"producto": pr, "descripcion": txt(l.producto_desc), "esp": esp, "con": con, "d": df}
    return {"id": c.id, "nombre": txt(c.nombre), "fecha": iso(c.fecha), "estatus": txt(c.estatus), "ubs": d}


def _kpi_cmp(datos, conj, cm):
    n = nd = 0; falt = sobr = esp = vf = vs = 0.0
    for cod in conj:
        u = datos["ubs"][cod]; n += 1
        if u["abs"] > EPS: nd += 1
        falt += u["falt"]; sobr += u["sobr"]; esp += u["esp"]
        for pr, l in u["lineas"].items():
            cs = cm.get(pr, 0)
            if l["d"] < 0: vf += -l["d"] * cs
            if l["d"] > 0: vs += l["d"] * cs
    return {"ubicaciones": n, "con_diferencia": nd, "exactitud_ubicacion_pct": r1((n - nd) * 100 / n) if n else None, "faltante_unidades": r2(falt), "sobrante_unidades": r2(sobr),
            "exactitud_pieza_pct": r1(max(0, 1 - (falt + sobr) / esp) * 100) if esp else None, "valor_faltante": r2(vf), "valor_sobrante": r2(vs)}


async def calcular_comparacion(db, a_id, b_id, zona="", bodega="", causas=False):
    ca, cb = await conteo_o_404(db, a_id), await conteo_o_404(db, b_id)
    if ca.id == cb.id: raise HTTPException(422, "Elige dos conteos distintos")
    if ca.fecha and cb.fecha and ca.fecha > cb.fecha: ca, cb = cb, ca
    cat, cm = await cat_otras_un(db), await costos_mapa(db)
    A_ = _datos_cmp(ca, await ubs_de(db, ca.id), await lineas_de(db, ca.id), cat); B_ = _datos_cmp(cb, await ubs_de(db, cb.id), await lineas_de(db, cb.id), cat)
    okf = lambda u: (not zona or u["zona"] == zona) and (not bodega or u["bodega"] == bodega)
    comunes = sorted(k for k in A_["ubs"] if k in B_["ubs"] and okf(A_["ubs"][k]))
    solo_a = sum(1 for k in A_["ubs"] if k not in B_["ubs"] and okf(A_["ubs"][k])); solo_b = sum(1 for k in B_["ubs"] if k not in A_["ubs"] and okf(B_["ubs"][k]))
    filas = []
    for cod in comunes:
        a, b = A_["ubs"][cod], B_["ubs"][cod]; da, db_ = a["abs"] > EPS, b["abs"] > EPS
        if not da and not db_: e = "sin_dif"
        elif da and not db_: e = "mejoro"
        elif not da and db_: e = "empeoro"
        else: e = "empeoro" if b["abs"] > a["abs"] + EPS else ("mejoro" if b["abs"] < a["abs"] - EPS else "persiste")
        filas.append({"codigo": cod, "zona": b["zona"] or a["zona"], "bodega": b["bodega"] or a["bodega"], "dif_a": a["abs"], "dif_b": b["abs"], "lineas_dif_a": a["n_dif"], "lineas_dif_b": b["n_dif"], "estado": e, "nueva": not da and db_})
    oe = {"empeoro": 0, "persiste": 1, "mejoro": 2, "sin_dif": 3}
    filas.sort(key=lambda f: (oe[f["estado"]], -(f["dif_b"] - f["dif_a"])))
    cuenta = {"empeoro": 0, "persiste": 0, "mejoro": 0, "sin_dif": 0}
    for f in filas: cuenta[f["estado"]] += 1
    reinc, prod_re = [], {}
    for cod in comunes:
        a, b = A_["ubs"][cod], B_["ubs"][cod]
        for pr, lb in b["lineas"].items():
            la = a["lineas"].get(pr)
            if not la or abs(la["d"]) < EPS or abs(lb["d"]) < EPS: continue
            cs = cm.get(pr, 0)
            reinc.append({"codigo": cod, "producto": pr, "descripcion": lb["descripcion"] or la["descripcion"], "dif_a": la["d"], "dif_b": lb["d"], "costo": cs, "valor_b": r2(lb["d"] * cs)})
            k = prod_re.setdefault(pr, {"producto": pr, "descripcion": lb["descripcion"] or la["descripcion"], "ubicaciones": 0, "dif_a": 0.0, "dif_b": 0.0}); k["ubicaciones"] += 1; k["dif_a"] += la["d"]; k["dif_b"] += lb["d"]
    reinc.sort(key=lambda x: -abs(x["dif_b"])); productos = sorted(prod_re.values(), key=lambda x: (-x["ubicaciones"], -abs(x["dif_b"])))
    zonas = {}
    for f in filas:
        g = zonas.setdefault(f["zona"] or "—", {"zona": f["zona"] or "—", "ubicaciones": 0, "dif_a": 0, "dif_b": 0}); g["ubicaciones"] += 1; g["dif_a"] += f["dif_a"] > EPS; g["dif_b"] += f["dif_b"] > EPS
    por_zona = [{**g, "exactitud_a": r1((g["ubicaciones"] - g["dif_a"]) * 100 / g["ubicaciones"]) if g["ubicaciones"] else None, "exactitud_b": r1((g["ubicaciones"] - g["dif_b"]) * 100 / g["ubicaciones"]) if g["ubicaciones"] else None} for _, g in sorted(zonas.items())]
    setc = {k.upper() for k in comunes}; filtrando = bool(zona or bodega)
    async def cont_inc(i):
        o = {}
        for r in (await db.execute(select(InvIncidencia).where(InvIncidencia.id_conteo == i))).scalars():
            if filtrando and txt(r.codigo).upper() not in setc: continue
            t = txt(r.tipo) or "otro"; o[t] = o.get(t, 0) + 1
        return o
    ia, ib = await cont_inc(ca.id), await cont_inc(cb.id)
    incid = sorted(({"tipo": k, "tipo_txt": TIPOS_INC.get(k, k), "a": ia.get(k, 0), "b": ib.get(k, 0), "cambio": ib.get(k, 0) - ia.get(k, 0)} for k in set(ia) | set(ib)), key=lambda x: (-x["cambio"], -x["b"]))
    out = {"a": {"id": ca.id, "nombre": A_["nombre"], "fecha": A_["fecha"], "estatus": A_["estatus"], "foto": A.foto_de(ca)}, "b": {"id": cb.id, "nombre": B_["nombre"], "fecha": B_["fecha"], "estatus": B_["estatus"], "foto": A.foto_de(cb)},
           "filtro": {"zona": zona, "bodega": bodega}, "alcance": {"comunes": len(comunes), "solo_a": solo_a, "solo_b": solo_b}, "kpi_a": _kpi_cmp(A_, comunes, cm), "kpi_b": _kpi_cmp(B_, comunes, cm),
           "cuenta": cuenta, "zonas": por_zona, "incidencias": incid, "incidencias_total": {"a": sum(x["a"] for x in incid), "b": sum(x["b"] for x in incid)}, "ubicaciones": filas, "reincidentes": reinc, "productos_reincidentes": productos,
           "costos_nota": "Los valores usan el costo actual de Odoo en ambos conteos." if cm else "Sin costos cargados: los valores aparecen en 0 (actualiza costos en el Reporte)."}
    if causas:
        out["causas"] = {}
        for par, cc in (("a", ca), ("b", cb)):
            try:
                r = (await movimientos(db, cc))["resumen"]
                out["causas"][par] = {"diferencias": r["diferencias"], "explicada": r["explicada"], "parcial": r["parcial"], "mal_surtido": r["mal_surtido"] + r["mal_surtido_parcial"], "sin_explicar": r["sin_explicar"]}
            except Exception as e:
                out["causas"][par] = None; out["aviso_causas"] = f"No se pudieron consultar los movimientos de Odoo de uno de los conteos: {str(e)[:120]}"
    return out


@router.get("/comparar")
async def api_comparar(a: str, b: str, zona: str = "", bodega: str = "", causas: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_comparacion(db, a, b, zona, bodega, causas); r["reincidentes"] = r["reincidentes"][:500]; r["ubicaciones"] = r["ubicaciones"][:3000]
    return r


@router.get("/comparar/csv")
async def api_comparar_csv(a: str, b: str, zona: str = "", bodega: str = "", seccion: str = "", db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await calcular_comparacion(db, a, b, zona, bodega)
    if seccion == "productos":
        f = [["Conteo anterior", "Conteo reciente", "Ubicacion", "Clave", "Descripcion", "Diferencia anterior", "Diferencia reciente", "Costo unitario", "Valor diferencia reciente"]] + \
            [[r["a"]["id"], r["b"]["id"], x["codigo"], x["producto"], x["descripcion"], x["dif_a"], x["dif_b"], x["costo"], x["valor_b"]] for x in r["reincidentes"]]
        return archivo_csv(f"{r['b']['id']}_vs_{r['a']['id']}_reincidentes.csv", f)
    E = {"empeoro": "EMPEORO", "persiste": "PERSISTE", "mejoro": "MEJORO", "sin_dif": "sin diferencia"}
    f = [["Conteo anterior", "Conteo reciente", "Ubicacion", "Bodega", "Zona", "Unidades dif. (abs) anterior", "Unidades dif. (abs) reciente", "Renglones con dif. anterior", "Renglones con dif. reciente", "Resultado", "Diferencia nueva"]] + \
        [[r["a"]["id"], r["b"]["id"], x["codigo"], x["bodega"], x["zona"], x["dif_a"], x["dif_b"], x["lineas_dif_a"], x["lineas_dif_b"], E[x["estado"]], "si" if x["nueva"] else ""] for x in r["ubicaciones"]]
    return archivo_csv(f"{r['b']['id']}_vs_{r['a']['id']}_comparacion.csv", f)


@router.get("/conteos/{id_}/anterior")
async def api_anterior(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_); ref = c.fecha_cierre or c.fecha or ahora()
    cand = [x for x in (await db.execute(select(InvConteo))).scalars() if x.id != id_ and x.estatus == "cerrado" and x.fecha_cierre and x.fecha_cierre <= ref and A.foto_de(x)]
    mismo = [x for x in cand if txt(x.alcance) == txt(c.alcance)]
    cand = sorted(mismo or cand, key=lambda x: x.fecha_cierre, reverse=True)
    if not cand: return {"anterior": None}
    a = cand[0]
    return {"anterior": {"id": a.id, "nombre": txt(a.nombre), "alcance": txt(a.alcance), "fecha_cierre": iso(a.fecha_cierre), "mismo_alcance": txt(a.alcance) == txt(c.alcance), "foto": A.foto_de(a)}}
