"""Analisis de conteos: reporte, validacion contra movimientos de Odoo, mal surtido. Port de Conteo.gs.
Trabaja con filas ORM (InvConteoUbic / InvConteoLinea) ya cargadas; solo analizarMovimientos consulta Odoo (lectura)."""
import json
from datetime import datetime
from services.inv_core import *
from services import inv_odoo as O


def calcular_reporte(ubs, lineas, cm, cat):
    contadas = {txt(x.codigo): x for x in ubs if x.estado == "contada"}
    dif, top, sku, ub_dif, otras = [], {}, {}, set(), []
    t_esp = t_con = falt = sobr = v_falt = v_sobr = 0.0
    for l in lineas:
        cod = txt(l.codigo)
        if cod not in contadas or l.contado is None:
            continue
        if es_otra_un(bool(l.inesperado), l.origen, l.producto, cat):
            otras.append({"producto": l.producto, "producto_desc": l.producto_desc, "codigo": l.codigo, "contado": l.contado}); continue
        esp, con = num(l.esperado), num(l.contado); d = con - esp; cs = cm.get(txt(l.producto), 0)
        t_esp += esp; t_con += con
        if d < 0: v_falt += -d * cs
        if d > 0: v_sobr += d * cs
        ps = txt(l.producto)
        k = sku.setdefault(ps, {"producto": ps, "descripcion": txt(l.producto_desc), "contado": 0, "esperado": 0, "ubicaciones": 0, "con_diferencia": 0})
        k["contado"] += con; k["esperado"] += esp; k["ubicaciones"] += 1
        if d != 0: k["con_diferencia"] += 1
        if d < 0: falt += -d
        if d > 0: sobr += d
        if d != 0:
            ub_dif.add(cod)
            t = top.setdefault(ps, {"producto": ps, "descripcion": txt(l.producto_desc), "abs": 0, "neto": 0, "lineas": 0})
            t["abs"] += abs(d); t["neto"] += d; t["lineas"] += 1
            dif.append({"codigo": cod, "bodega": txt(contadas[cod].bodega), "zona": txt(contadas[cod].zona), "producto": ps, "descripcion": txt(l.producto_desc),
                        "esperado": esp, "contado": con, "diferencia": d, "costo": cs, "valor": r2(d * cs), "pct": r1(d * 100 / esp) if esp else None,
                        "inesperado": bool(l.inesperado), "contado_por": txt(l.contado_por), "veces": l.veces or 0})
    grupos, por_contador = {"bodega": {}, "zona": {}}, {}
    for cod, x in contadas.items():
        for par, val in (("bodega", txt(x.bodega) or "—"), ("zona", txt(x.zona) or "—")):
            g = grupos[par].setdefault(val, {"nombre": val, "contadas": 0, "con_diferencia": 0})
            g["contadas"] += 1
            if cod in ub_dif: g["con_diferencia"] += 1
        k = txt(x.contada_por) or "—"; por_contador[k] = por_contador.get(k, 0) + 1

    def lista(o):
        out = []
        for g in o.values():
            g["exactitud"] = r1((g["contadas"] - g["con_diferencia"]) * 100 / g["contadas"]) if g["contadas"] else None
            out.append(g)
        return sorted(out, key=lambda g: g["nombre"])
    por_aud = {}
    for x in ubs:
        k = txt(x.asignado_a) or "(sin asignar)"
        g = por_aud.setdefault(k, {"usuario": k, "asignadas": 0, "contadas": 0, "con_diferencia": 0}); g["asignadas"] += 1
        if x.estado == "contada":
            g["contadas"] += 1
            if txt(x.codigo) in ub_dif: g["con_diferencia"] += 1
    n_cont, n_dif = len(contadas), len(ub_dif)
    dif.sort(key=lambda d: -abs(d["diferencia"]))
    return {
        "resumen": {"ubicaciones": len(ubs), "contadas": n_cont, "pendientes": len(ubs) - n_cont, "en_reconteo": sum(1 for x in ubs if x.estado == "reconteo"),
                    "avance_pct": r1(n_cont * 100 / len(ubs)) if ubs else 0, "ubicaciones_con_diferencia": n_dif,
                    "exactitud_ubicacion_pct": r1((n_cont - n_dif) * 100 / n_cont) if n_cont else None,
                    "esperado_total": t_esp, "contado_total": t_con, "faltante_unidades": falt, "sobrante_unidades": sobr,
                    "exactitud_pieza_pct": r1(max(0, 1 - (falt + sobr) / t_esp) * 100) if t_esp else None,
                    "valor_faltante": r2(v_falt), "valor_sobrante": r2(v_sobr), "con_costos": len(cm) > 0, "otras_un": resumen_otras_un(otras, cat),
                    "lineas_con_diferencia": len(dif), "productos_inesperados": sum(1 for d in dif if d["inesperado"])},
        "por_bodega": lista(grupos["bodega"]), "por_zona": lista(grupos["zona"]),
        "top_productos": sorted(top.values(), key=lambda t: -t["abs"])[:10],
        "por_sku": sorted(({**v, "diferencia": v["contado"] - v["esperado"]} for v in sku.values()), key=lambda v: v["producto"]),
        "por_asignado": sorted(por_aud.values(), key=lambda g: -g["asignadas"]),
        "por_contador": sorted(({"usuario": k, "ubicaciones": v} for k, v in por_contador.items()), key=lambda o: -o["ubicaciones"]),
        "diferencias": dif[:2000], "_todas": dif}


def foto_cierre(ubs, lineas, cm, cat, inc_por_tipo):
    r = calcular_reporte(ubs, lineas, cm, cat)["resumen"]
    return json.dumps({"cierre": iso(ahora()), "ubicaciones": r["ubicaciones"], "contadas": r["contadas"], "ubicaciones_con_diferencia": r["ubicaciones_con_diferencia"],
                       "exactitud_ubicacion_pct": r["exactitud_ubicacion_pct"], "exactitud_pieza_pct": r["exactitud_pieza_pct"],
                       "faltante_unidades": r["faltante_unidades"], "sobrante_unidades": r["sobrante_unidades"], "valor_faltante": r["valor_faltante"],
                       "valor_sobrante": r["valor_sobrante"], "con_costos": r["con_costos"], "incidencias": inc_por_tipo})


def foto_de(c):
    try:
        return json.loads(c.resumen) if c.resumen else None
    except Exception:
        return None


def compensar_mal_surtido(difs, almacen_de, pool):
    """Pistas de mal surtido dentro del mismo almacen: (1) cruce sobrante/faltante del mismo producto, (2) OV salida de ubicacion NO contada."""
    grupos = {}
    for p in pool:
        p["_rest"] = num(p.get("cantidad"))
    for d in difs:
        d["_res"] = d["diferencia_sin_explicar"]; d["posible_origen"] = []
    for d in difs:
        if d["estado"] == "explicada" or abs(d["_res"]) < EPS:
            continue
        alm = almacen_de.get(d["codigo"])
        if not alm:
            continue
        grupos.setdefault((alm, d["producto"]), {"alm": alm, "prod": d["producto"], "ls": []})["ls"].append(d)
    for g in grupos.values():
        pos = sorted([d for d in g["ls"] if d["_res"] > EPS], key=lambda d: -d["_res"])
        neg = sorted([d for d in g["ls"] if d["_res"] < -EPS], key=lambda d: d["_res"])
        for n in neg:
            for p in pos:
                if p["_res"] <= EPS or n["_res"] >= -EPS:
                    continue
                t = min(p["_res"], -n["_res"]); n["_res"] += t; p["_res"] -= t
                n["posible_origen"].append({"tipo": "cruce", "ubicacion": p["codigo"], "cantidad": -t})
                p["posible_origen"].append({"tipo": "cruce", "ubicacion": n["codigo"], "cantidad": t})
        for n in neg:
            for m in pool:
                if n["_res"] >= -EPS or m.get("almacen") != g["alm"] or m["producto"] != g["prod"] or m["_rest"] <= EPS:
                    continue
                if m.get("_dt") and n.get("_fin") and m["_dt"] > n["_fin"]:
                    continue
                t = min(-n["_res"], m["_rest"]); m["_rest"] -= t; n["_res"] += t
                n["posible_origen"].append({"tipo": "ov_fuera", "ubicacion": m.get("origen") or "", "cantidad": -t, "documento": m.get("ov") or m.get("picking") or "",
                                            "picking": m.get("picking") or "", "fecha": m.get("fecha") or ""})
    for d in difs:
        if d["posible_origen"]:
            d["estado"] = "mal_surtido" if abs(d["_res"]) < EPS else "mal_surtido_parcial"
            d["diferencia_sin_explicar"] = d["_res"]
        d.pop("_res", None)
    return difs


async def analizar_movimientos(c, ubs, lineas, cat, syncs):
    """Una diferencia puede deberse a que salio mercancia por una OV o se traslado entre la ultima sincronizacion y el conteo. Odoo solo se LEE."""
    codigos, contadas = set(), {}
    for x in ubs:
        codigos.add(txt(x.codigo))
        if x.estado == "contada":
            contadas[txt(x.codigo)] = x
    lineas = [l for l in lineas if not es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)]
    inicio = c.fecha
    ult = syncs[-1].fecha if syncs else None
    desde = ult if ult and ult < inicio else inicio
    hasta = c.fecha_cierre or ahora()
    movs = []
    for m in await O.movimientos_odoo(list(codigos), desde, hasta):
        m["_dt"] = O.parse_odoo(m["fecha"])
        if m["_dt"] and desde <= m["_dt"] <= hasta:
            movs.append(m)
    idx = {}
    for m in movs:
        q = num(m["cantidad"])
        if m["destino"] in codigos: idx.setdefault((m["destino"], m["producto"]), []).append((m, q))
        if m["origen"] in codigos: idx.setdefault((m["origen"], m["producto"]), []).append((m, -q))
    difs = []
    for l in lineas:
        cod = txt(l.codigo)
        if cod not in contadas or l.contado is None:
            continue
        esp, con = num(l.esperado), num(l.contado); dif = con - esp
        fin = l.fecha_conteo or hasta
        usados = [(m, d) for (m, d) in idx.get((cod, txt(l.producto)), []) if desde <= m["_dt"] <= fin]
        neto = sum(d for _, d in usados)
        aj = con - (esp + neto)
        if abs(dif) < EPS and abs(aj) < EPS:
            continue
        estado = "explicada" if abs(aj) < EPS else ("parcial" if abs(aj) < abs(dif) - EPS else "sin_explicar")
        tipos = {}
        for m, d in usados: tipos[m["tipo"]] = tipos.get(m["tipo"], 0) + d
        difs.append({"_fin": fin, "posible_origen": [], "codigo": cod, "producto": txt(l.producto), "descripcion": txt(l.producto_desc), "esperado": esp, "contado": con,
                     "diferencia": dif, "movimiento_neto": neto, "diferencia_sin_explicar": aj, "estado": estado, "contado_por": txt(l.contado_por), "mov_tipos": tipos,
                     "docs": [{"tipo": m["tipo"], "documento": m["ov"] or m["picking"], "picking": m["picking"], "cliente": m.get("cliente") or "", "fecha": m["fecha"], "cantidad": d, "origen": m["origen"], "destino": m["destino"]} for m, d in usados[:30]],
                     "movimientos": [{"tipo": m["tipo"], "documento": m["ov"] or m["picking"], "picking": m["picking"], "fecha": m["fecha"], "cantidad": d, "origen": m["origen"], "destino": m["destino"]} for m, d in usados[:10]]})
    aviso = None
    pend = [d for d in difs if d["estado"] != "explicada"]
    if pend:
        try:
            al = await O.almacenes_de_ubicaciones(list(codigos)); pool = []
            prod_falt = [d["producto"] for d in pend if d["diferencia_sin_explicar"] < 0]
            if prod_falt and al["whIds"]:
                for m in await O.salidas_almacen(al["whIds"], prod_falt, desde, hasta, list(codigos)):
                    if m["tipo"] == "OV":
                        m["_dt"] = O.parse_odoo(m["fecha"]); pool.append(m)
            elif prod_falt:
                aviso = "Odoo no informo el almacen de las ubicaciones: solo se buscaron cruces, no OVs de otras ubicaciones."
            compensar_mal_surtido(difs, al["porCodigo"], pool)
        except Exception as e:
            aviso = f"No se pudo revisar mal surtido: {e}"
    for d in difs: d.pop("_fin", None)
    cuenta = {k: sum(1 for d in difs if d["estado"] == k) for k in ("explicada", "parcial", "mal_surtido", "mal_surtido_parcial", "sin_explicar")}
    orden = {"sin_explicar": 0, "mal_surtido_parcial": 1, "parcial": 2, "mal_surtido": 3, "explicada": 4}
    difs.sort(key=lambda d: (orden[d["estado"]], -abs(d["diferencia_sin_explicar"])))
    ovs = {}
    for m in movs:
        if m["tipo"] != "OV" or m["origen"] not in codigos:
            continue
        k = m["picking"] or m["ov"]
        o = ovs.setdefault(k, {"ov": m["ov"], "picking": m["picking"], "cliente": m["cliente"], "fecha": m["fecha"], "unidades": 0, "lineas": []})
        o["unidades"] += num(m["cantidad"])
        if (m["fecha"] or "") > (o["fecha"] or ""): o["fecha"] = m["fecha"]
        o["lineas"].append({"producto": m["producto"], "descripcion": m["descripcion"], "cantidad": m["cantidad"], "ubicacion": m["origen"], "fecha": m["fecha"]})
    lista_ovs = sorted(ovs.values(), key=lambda o: o["fecha"] or "")
    traslados = [{"picking": m["picking"], "fecha": m["fecha"], "producto": m["producto"], "descripcion": m["descripcion"], "cantidad": m["cantidad"], "origen": m["origen"], "destino": m["destino"]}
                 for m in movs if m["tipo"] == "traslado"]
    otros = [{"tipo": m["tipo"], "documento": m["ov"] or m["picking"], "fecha": m["fecha"], "producto": m["producto"], "cantidad": m["cantidad"], "origen": m["origen"], "destino": m["destino"]}
             for m in movs if m["tipo"] in ("entrada", "ajuste")]
    return {"ventana": {"desde": O.fecha_odoo(desde), "hasta": O.fecha_odoo(hasta), "base_existencias": O.fecha_odoo(ult) if ult else None, "aviso_mal_surtido": aviso},
            "resumen": {"diferencias": len(difs), **cuenta, "ovs": len(lista_ovs), "unidades_ov": sum(o["unidades"] for o in lista_ovs),
                        "traslados": len(traslados), "otros_movimientos": len(otros), "movimientos_total": len(movs)},
            "diferencias": difs[:2000], "ovs": lista_ovs[:500], "traslados": traslados[:1000], "otros": otros[:500],
            "_difs": difs, "_ovs": lista_ovs, "_traslados": traslados, "_otros": otros}
