"""Inventario: reconteo, validacion/fusion de capturas y completar esperado. Port de Reconteo.gs, Capturas.gs, Esperado.gs.
Odoo NUNCA se modifica (solo lectura de movimientos)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import InvConteoUbic, InvConteoLinea, InvConteoRonda, InvSync, InvLog
from routers.inventario import (inv_user, exige_gestor, log, conteo_o_404, ubs_de, lineas_de, cat_otras_un, costos_mapa, archivo_csv, ser_conteo,
                                progreso, ubic_conteo, contadores_previos, valida_contador, movimientos, claves_conocidas)
from services.inv_core import *
from services import inv_odoo as O

router = APIRouter()


async def rondas_de(db, id_, con_anuladas=False):
    q = select(InvConteoRonda).where(InvConteoRonda.id_conteo == id_)
    rs = (await db.execute(q)).scalars().all()
    return rs if con_anuladas else [r for r in rs if not r.anulada]


def ronda_num(r):
    return txt(r.ronda).isdigit()


def _g(por, cod):
    return por.setdefault(cod, {"esperado": 0.0, "abs": 0.0, "maxl": 0.0, "lineas": []})


def _fila_cand(cod, x, g, lineas_dif):
    return {"codigo": cod, "grupo": txt(x.grupo), "ronda": x.ronda or 1, "estado": "reconteo" if x.estado == "reconteo" else "contada", "asignado_a": txt(x.asignado_a),
            "contada_por": txt(x.contada_por), "en_reconteo": x.estado == "reconteo", "lineas_dif": lineas_dif, "unidades_dif": g["abs"], "esperado": g["esperado"],
            "pct": r1(g["abs"] * 100 / g["esperado"]) if g["esperado"] else None, "max_linea": g["maxl"], "lineas": g["lineas"][:5]}


@router.get("/conteos/{id_}/reconteo/candidatos")
async def api_candidatos(id_: str, producto: str = "", db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    ubs = {txt(x.codigo): x for x in await ubs_de(db, id_)}
    cat = await cat_otras_un(db); lineas = [l for l in await lineas_de(db, id_) if not es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)]
    q = norm(producto)
    por = {}
    for l in lineas:
        cod = txt(l.codigo); x = ubs.get(cod)
        if not x: continue
        if q:
            if x.estado not in ("contada", "reconteo") or (l.contado is None and x.estado == "contada"): continue
            if q not in norm(l.producto) and q not in norm(l.producto_desc): continue
        elif x.estado != "contada" or l.contado is None:
            continue
        g = _g(por, cod); esp = num(l.esperado); con = None if l.contado is None else num(l.contado); d = 0 if con is None else con - esp
        g["esperado"] += esp
        if q or abs(d) > EPS:
            g["abs"] += abs(d); g["maxl"] = max(g["maxl"], abs(d))
            g["lineas"].append({"producto": txt(l.producto), "descripcion": txt(l.producto_desc), "esperado": esp, "contado": con, "diferencia": d})
    out = []
    for cod, g in por.items():
        x = ubs[cod]; n_dif = sum(1 for l in g["lineas"] if abs(l["diferencia"]) > EPS)
        if not q and not n_dif: continue
        out.append(_fila_cand(cod, x, g, n_dif))
    if not q:   # las que ya estan en reconteo siempre se muestran
        for cod, x in ubs.items():
            if x.estado == "reconteo" and cod not in por:
                out.append(_fila_cand(cod, x, {"esperado": 0, "abs": 0, "maxl": 0, "lineas": []}, 0))
    out.sort(key=lambda o: (-o["unidades_dif"], o["codigo"]))
    return {"conteo": ser_conteo(c, await progreso(db, id_)), "ubicaciones": out, "por_producto": bool(q)}


@router.post("/conteos/{id_}/reconteo")
async def api_crear_reconteo(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    codigos = [txt(x) for x in (body.get("codigos") or []) if txt(x)]; email = txt(body.get("email")).lower()
    if not codigos: raise HTTPException(422, "Elige al menos una ubicacion")
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo esta cerrado: reabrelo para hacer reconteos")
    if email: await valida_contador(db, email)
    sel = set(codigos)
    ubs = [x for x in await ubs_de(db, id_) if txt(x.codigo) in sel]
    if len(ubs) != len(sel): raise HTTPException(422, "Alguna ubicacion no pertenece a este conteo")
    mal = [x for x in ubs if x.estado != "contada"]
    if mal: raise HTTPException(409, "Solo se recuentan ubicaciones ya contadas (y no en reconteo): " + ", ".join(txt(x.codigo) for x in mal[:5]))
    if email and not body.get("forzar"):
        rep = [x for x in ubs if email in await contadores_previos(db, id_, txt(x.codigo), x)]
        if rep:
            raise HTTPException(409, f"REPETIDO: {email} ya conto " + ", ".join(txt(x.codigo) for x in rep[:5]) + (f" y {len(rep) - 5} mas" if len(rep) > 5 else "") + ". El reconteo debe hacerlo otra persona; confirma si quieres forzarlo.")
    ronda_de = {txt(x.codigo): x.ronda or 1 for x in ubs}
    ya = {(txt(r.codigo), txt(r.producto), txt(r.ronda)) for r in await rondas_de(db, id_)}
    for l in await lineas_de(db, id_):
        cod = txt(l.codigo)
        if cod not in sel or l.contado is None or (cod, txt(l.producto), str(ronda_de[cod])) in ya: continue
        db.add(InvConteoRonda(id_conteo=id_, ronda=str(ronda_de[cod]), codigo=cod, producto=txt(l.producto), contado=l.contado, contado_por=txt(l.contado_por), fecha_conteo=l.fecha_conteo))
    for r in await rondas_de(db, id_):   # una decision anterior queda sin efecto al recontar
        if txt(r.ronda) == "final" and txt(r.codigo) in sel: r.ronda = "anulado"
    modo = "previo" if txt(body.get("modo")) == "previo" else "ciego"
    for x in ubs:
        x.estado = "reconteo"; x.ronda = ronda_de[txt(x.codigo)] + 1; x.asignado_a = email; x.modo_reco = modo
    await log(db, u["email"], "RECONTEO_CREADO", f"{id_} {len(ubs)} ubicaciones ({modo}) -> {email or 'sin asignar'}" + (" (forzado)" if body.get("forzar") else ""))
    await db.commit()
    return {"ok": True, "ubicaciones": len(ubs)}


@router.post("/conteos/{id_}/reconteo/reasignar")
async def api_reasignar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    codigos = {txt(x) for x in (body.get("codigos") or []) if txt(x)}; email = txt(body.get("email")).lower()
    if not codigos: raise HTTPException(422, "Elige al menos una ubicacion")
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo esta cerrado")
    if email: await valida_contador(db, email)
    ubs = [x for x in await ubs_de(db, id_) if txt(x.codigo) in codigos]
    if len(ubs) != len(codigos): raise HTTPException(422, "Alguna ubicacion no pertenece a este conteo")
    if any(x.estado == "contada" for x in ubs): raise HTTPException(409, "Solo se reasignan ubicaciones pendientes o en reconteo")
    if email and not body.get("forzar"):
        rep = [x for x in ubs if x.estado == "reconteo" and email in await contadores_previos(db, id_, txt(x.codigo), x)]
        if rep: raise HTTPException(409, f"REPETIDO: {email} ya conto " + ", ".join(txt(x.codigo) for x in rep[:5]) + ". Confirma si quieres forzarlo.")
    for x in ubs: x.asignado_a = email
    await log(db, u["email"], "RECONTEO_REASIGNA", f"{id_} {len(ubs)} -> {email or 'nadie'}" + (" (forzado)" if body.get("forzar") else ""))
    await db.commit()
    return {"ok": True, "cambiadas": len(ubs)}


ETIQ_MOV = {"OV": "OV", "traslado": "traslado", "entrada": "entrada", "ajuste": "ajuste"}


def causas_txt(d):
    p = [f"{ETIQ_MOV.get(k, k)} {sg(v)}" for k, v in (d.get("mov_tipos") or {}).items() if abs(v) > EPS]
    for o in d.get("posible_origen") or []:
        p.append(f"cruce con {o['ubicacion']} {sg(o['cantidad'])}" if o["tipo"] == "cruce" else f"OV de otra ubicacion {sg(o['cantidad'])}")
    return ", ".join(p) or "movimientos del sistema"


def anulados_de(rs):
    g, orden = {}, []
    for r in rs:
        if not r.anulada: continue
        k = (txt(r.codigo), iso(r.fecha_anulacion))
        if k not in g:
            g[k] = {"codigo": txt(r.codigo), "fecha": iso(r.fecha_anulacion), "por": txt(r.anulada_por), "motivo": txt(r.motivo_anulacion), "prod": {}}; orden.append(k)
        pr = g[k]["prod"].setdefault(txt(r.producto), {"producto": txt(r.producto), "rondas": [], "final": None})
        if txt(r.ronda) == "final": pr["final"] = {"valor": num(r.contado), "por": txt(r.contado_por), "nota": txt(r.nota)}
        elif ronda_num(r): pr["rondas"].append({"ronda": int(r.ronda), "contado": num(r.contado), "contado_por": txt(r.contado_por)})
    out = []
    for k in orden:
        x = g[k]
        out.append({"codigo": x["codigo"], "fecha": x["fecha"], "por": x["por"], "motivo": x["motivo"],
                    "lineas": [{**v, "rondas": sorted(v["rondas"], key=lambda r: r["ronda"])} for v in x["prod"].values()]})
    return sorted(out, key=lambda a: (a["fecha"], a["codigo"]), reverse=True)


@router.get("/conteos/{id_}/reconteo/seguimiento")
async def api_seguimiento(id_: str, sin_mov: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    ubs = [x for x in await ubs_de(db, id_) if (x.ronda or 1) >= 2 or x.estado == "reconteo"]
    por = {txt(x.codigo): x for x in ubs}
    hist, fin = {}, {}
    rs_all = await rondas_de(db, id_)
    for r in rs_all:
        cod = txt(r.codigo)
        if cod not in por: continue
        k = (cod, txt(r.producto))
        if txt(r.ronda) == "final": fin[k] = {"valor": num(r.contado), "por": txt(r.contado_por), "nota": txt(r.nota), "fecha": iso(r.fecha_conteo)}
        elif ronda_num(r): hist.setdefault(k, []).append({"ronda": int(r.ronda), "contado": num(r.contado), "contado_por": txt(r.contado_por)})
    cat = await cat_otras_un(db)
    mov_de, mov_ok, aviso = {}, False, None
    if not sin_mov and any(x.estado == "contada" for x in ubs):
        try:
            for d in (await movimientos(db, c))["_difs"]: mov_de[(d["codigo"], d["producto"])] = d
            mov_ok = True
        except Exception as e:
            aviso = f"No se pudieron consultar los movimientos de Odoo ({str(e)[:120]}): solo se compara contra el sistema."
    res = {}
    for l in await lineas_de(db, id_):
        cod = txt(l.codigo)
        if cod not in por or es_otra_un(bool(l.inesperado), l.origen, l.producto, cat): continue
        x = por[cod]; k = (cod, txt(l.producto)); rs = list(hist.get(k, [])); rn = x.ronda or 1
        if x.estado == "contada" and l.contado is not None and k not in fin and not any(r["ronda"] == rn for r in rs):
            rs.append({"ronda": rn, "contado": num(l.contado), "contado_por": txt(l.contado_por)})
        rs.sort(key=lambda r: r["ronda"])
        esp = num(l.esperado); nota = ""; md = mov_de.get(k); dif_u = None
        if x.estado == "reconteo": resultado = "pendiente"
        elif k in fin: resultado = "decidida"
        elif len(rs) < 2: resultado = "sin_reconteo"
        else:
            last, prev = rs[-1]["contado"], rs[-2]["contado"]; dif_u = last - esp
            explicada = abs(dif_u) >= EPS and md and md["estado"] in ("explicada", "mal_surtido")
            if abs(dif_u) < EPS: resultado = "cuadra" if abs(last - prev) < EPS else "error_conteo"
            elif explicada: resultado = "cuadra_mov"; nota = f"Dif. {sg(dif_u)} explicada por: {causas_txt(md)}"
            else:
                resultado = "confirmada" if abs(last - prev) < EPS else ("discrepancia" if len(rs) >= MAX_RONDAS else "requiere_reconteo")
                nota = f"Dif. {sg(dif_u)}" + (f" · movimientos explican {sg(md['movimiento_neto'])} · sin explicar {sg(md['diferencia_sin_explicar'])}" if md else ("" if mov_ok else " · movimientos no consultados"))
                if resultado == "requiere_reconteo": nota += f" · cuenta {len(rs)} de {MAX_RONDAS}: pide otro reconteo"
        res.setdefault(cod, []).append({"producto": txt(l.producto), "descripcion": txt(l.producto_desc), "esperado": esp, "rondas": rs, "final": fin.get(k), "resultado": resultado,
                                        "inesperado": bool(l.inesperado), "dif": dif_u, "nota": nota})
    orden = {"requiere_reconteo": 0, "discrepancia": 1, "pendiente": 2, "confirmada": 3, "error_conteo": 4, "decidida": 5, "cuadra_mov": 6, "cuadra": 7, "sin_reconteo": 8}
    out = []
    for cod, ls in res.items():
        x = por[cod]; ls.sort(key=lambda a: orden[a["resultado"]])
        out.append({"codigo": cod, "grupo": txt(x.grupo), "ronda": x.ronda or 1, "estado": txt(x.estado), "asignado_a": txt(x.asignado_a), "modo": txt(x.modo_reco) or "ciego", "peor": ls[0]["resultado"], "lineas": ls})
    out.sort(key=lambda o: (orden[o["peor"]], o["codigo"]))
    cuenta = {}
    for o in out: cuenta[o["peor"]] = cuenta.get(o["peor"], 0) + 1
    return {"conteo": ser_conteo(c, await progreso(db, id_)), "ubicaciones": out, "cuenta": cuenta, "anulados": anulados_de(await rondas_de(db, id_, True)),
            "requieren": [o["codigo"] for o in out if o["peor"] == "requiere_reconteo" and o["estado"] == "contada"], "max_rondas": MAX_RONDAS, "mov_ok": mov_ok, "aviso_mov": aviso}


@router.get("/conteos/{id_}/reconteo/historial/csv")
async def api_historial_csv(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    f = [["Conteo", "Ubicacion", "Producto", "Ronda / decision", "Contado", "Contado por", "Fecha", "Nota", "Estado", "Anulada por", "Fecha anulacion", "Motivo anulacion"]]
    for r in await rondas_de(db, id_, True):
        f.append([id_, txt(r.codigo), txt(r.producto), "decision supervisor" if txt(r.ronda) == "final" else txt(r.ronda), r.contado, txt(r.contado_por), iso(r.fecha_conteo), txt(r.nota),
                  "ANULADA" if r.anulada else "vigente", txt(r.anulada_por) if r.anulada else "", iso(r.fecha_anulacion) if r.anulada else "", txt(r.motivo_anulacion) if r.anulada else ""])
    return archivo_csv(f"{id_}_historial_reconteos.csv", f)


@router.post("/conteos/{id_}/reconteo/decidir")
async def api_decidir(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    try:
        valor = float(body.get("valor")); assert valor >= 0
    except Exception:
        raise HTTPException(422, "El valor final debe ser un numero mayor o igual a 0")
    await conteo_o_404(db, id_)
    cod, prod = txt(body.get("codigo")), txt(body.get("producto"))
    ub = await ubic_conteo(db, id_, cod)
    if ub.estado == "reconteo": raise HTTPException(409, "La ubicacion sigue pendiente de reconteo")
    if ub.estado != "contada": raise HTTPException(409, "La ubicacion aun no se ha contado")
    l = next((x for x in await lineas_de(db, id_) if x.codigo == cod and txt(x.producto) == prod), None)
    if not l: raise HTTPException(404, "Renglon no encontrado")
    rs = [r for r in await rondas_de(db, id_) if txt(r.codigo) == cod and txt(r.producto) == prod and txt(r.ronda) != "anulado"]
    rn = ub.ronda or 1; t = ahora(); nota = txt(body.get("nota"))[:250]
    ya_final = next((r for r in rs if txt(r.ronda) == "final"), None)
    if not ya_final and not any(ronda_num(r) and int(r.ronda) == rn for r in rs) and l.contado is not None:
        db.add(InvConteoRonda(id_conteo=id_, ronda=str(rn), codigo=cod, producto=prod, contado=l.contado, contado_por=txt(l.contado_por), fecha_conteo=l.fecha_conteo))
    if ya_final:
        ya_final.contado = valor; ya_final.contado_por = u["email"]; ya_final.fecha_conteo = t; ya_final.nota = nota
    else:
        db.add(InvConteoRonda(id_conteo=id_, ronda="final", codigo=cod, producto=prod, contado=valor, contado_por=u["email"], fecha_conteo=t, nota=nota))
    l.contado = valor
    await log(db, u["email"], "RECONTEO_DECIDE", f"{id_} {cod} {prod} = {valor}" + (f" ({nota})" if nota else ""))
    await db.commit()
    return {"ok": True}


@router.post("/conteos/{id_}/reconteo/cancelar")
async def api_cancelar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    motivo = txt(body.get("motivo"))[:250]
    if len(motivo) < 3: raise HTTPException(422, "Escribe el motivo de la cancelacion (queda en el registro)")
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo esta cerrado: reabrelo para cancelar reconteos")
    ubs = await ubs_de(db, id_)
    sel = {txt(x.codigo) for x in ubs if x.estado == "reconteo" or (x.ronda or 1) > 1} if body.get("todos") else {txt(k) for k in (body.get("codigos") or [])}
    obj = {txt(x.codigo): x for x in ubs if txt(x.codigo) in sel and (x.estado == "reconteo" or (x.ronda or 1) > 1)}
    if not obj: raise HTTPException(422, "No hay reconteos que cancelar en esa seleccion")
    rs = await rondas_de(db, id_)
    ya = {(txt(r.codigo), txt(r.producto), txt(r.ronda)) for r in rs}
    hay_final = {(txt(r.codigo), txt(r.producto)) for r in rs if txt(r.ronda) == "final"}
    lineas = [l for l in await lineas_de(db, id_) if txt(l.codigo) in obj]
    for l in lineas:   # guardar como ronda la cuenta vigente de un reconteo ya contado
        cod = txt(l.codigo); x = obj[cod]; rn = x.ronda or 1
        if x.estado != "contada" or l.contado is None or rn < 2 or (cod, txt(l.producto)) in hay_final or (cod, txt(l.producto), str(rn)) in ya: continue
        db.add(InvConteoRonda(id_conteo=id_, ronda=str(rn), codigo=cod, producto=txt(l.producto), contado=l.contado, contado_por=txt(l.contado_por), fecha_conteo=l.fecha_conteo))
    r1_ = {(txt(r.codigo), txt(r.producto)): r for r in rs if txt(r.codigo) in obj and txt(r.ronda) == "1"}
    for l in lineas:   # restaurar la ronda 1
        s1 = r1_.get((txt(l.codigo), txt(l.producto)))
        if s1: l.contado = s1.contado; l.contado_por = s1.contado_por; l.fecha_conteo = s1.fecha_conteo; l.veces = 1
        else: l.contado = None; l.contado_por = ""; l.fecha_conteo = None; l.veces = 0
    t = ahora()
    await db.flush()
    for r in await rondas_de(db, id_):
        if txt(r.codigo) in obj:
            r.anulada = True; r.anulada_por = u["email"]; r.fecha_anulacion = t; r.motivo_anulacion = motivo
    for cod, x in obj.items():
        p = r1_ and next((r for k, r in r1_.items() if k[0] == cod), None)
        x.estado = "contada"; x.ronda = 1; x.asignado_a = ""; x.modo_reco = ""
        if p: x.contada_por = txt(p.contado_por); x.fecha_conteo = p.fecha_conteo
    await log(db, u["email"], "RECONTEO_CANCELADO", f"{id_} {len(obj)} ubicaciones" + (" (todos)" if body.get("todos") else "") + f" · motivo: {motivo}")
    await db.commit()
    return {"ok": True, "ubicaciones": len(obj)}


# ───────────────────────── CAPTURAS: fusion, validacion, resolucion ─────────────────────────
def fusion_candidatos(ubs, lineas, cat):
    cont = {txt(x.codigo) for x in ubs if x.estado == "contada"}
    por = {}
    for l in lineas:
        if txt(l.codigo) in cont and l.contado is not None: por.setdefault(txt(l.codigo), []).append(l)
    out = []
    for cod, ls in por.items():
        inesp = [l for l in ls if l.inesperado and not es_otra_un(True, l.origen, l.producto, cat) and num(l.contado) > 0 and not txt(l.origen).startswith("fusion")]
        esp = [l for l in ls if not l.inesperado and num(l.contado) < num(l.esperado)]
        for a in inesp:
            ka, mejor = norm_clave(a.producto), None
            for b in esp:
                kb = norm_clave(b.producto)
                if not ka or not kb or ka == kb: continue
                d = dist_edicion(ka, kb)
                if d <= limite_dist(ka, kb) and (mejor is None or d < mejor[0]): mejor = (d, b)
            if mejor:
                d, b = mejor
                out.append({"codigo": cod, "de": txt(a.producto), "de_desc": txt(a.producto_desc), "de_contado": num(a.contado), "a": txt(b.producto), "a_desc": txt(b.producto_desc),
                            "a_esperado": num(b.esperado), "a_contado": num(b.contado), "distancia": d, "dif_antes": num(b.contado) - num(b.esperado),
                            "dif_despues": num(b.contado) + num(a.contado) - num(b.esperado)})
    out.sort(key=lambda x: (x["distancia"], -abs(x["de_contado"])))
    return out


async def confirmadas(db, id_):
    rows = (await db.execute(select(InvLog.detalle).where(InvLog.accion == "CAPTURA_CONFIRMADA"))).all()
    return {txt(d).split(" ·")[0].strip() for (d,) in rows if txt(d).startswith(id_ + "|")}


def clave_confirm(id_, cod, prod, con):
    return f"{id_}|{cod}|{prod}|{r2(con):g}"


@router.get("/conteos/{id_}/capturas/validar")
async def api_validar(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    cm, cat, conocidas, conf = await costos_mapa(db), await cat_otras_un(db), await claves_conocidas(db), await confirmadas(db, id_)
    ubs = await ubs_de(db, id_); cont = {txt(x.codigo) for x in ubs if x.estado == "contada"}; lineas = await lineas_de(db, id_)
    hall = []
    for l in lineas:
        cod, prod = txt(l.codigo), txt(l.producto)
        if cod not in cont or l.contado is None or es_otra_un(bool(l.inesperado), l.origen, prod, cat) or txt(l.origen).startswith("fusion"): continue
        esp, con, cs, por = num(l.esperado), num(l.contado), cm.get(prod, 0), txt(l.contado_por)
        if l.inesperado:
            if con > 0 and norm_clave(prod) not in conocidas:
                s = sugerencia_clave(prod, conocidas)
                hall.append({"tipo": "clave_inexistente", "codigo": cod, "producto": prod, "contado": con, "esperado": 0, "valor": r2(con * cs), "contado_por": por, "sugerencia": s,
                             "explicacion": f"La clave «{prod}» no existe en el catálogo del sistema" + (f" y se parece a «{s}». Lo más probable es un error de escritura." if s else ". Puede ser un error de escritura o mercancía de otra unidad de negocio."),
                             "accion": f"Revisa con el auditor y fusiona con «{s}» si es el mismo producto." if s else "Pregunta al auditor qué producto es; si es de otra UN, márcalo en «Mercancía no esperada»."})
        else:
            m = motivo_cantidad(esp, con)
            if m and clave_confirm(id_, cod, prod, con) not in conf:
                hall.append({"tipo": "cantidad_atipica", "codigo": cod, "producto": prod, "contado": con, "esperado": esp, "valor": r2((con - esp) * cs), "contado_por": por, "sugerencia": "",
                             "explicacion": f"{m} Diferencia de {sg(con - esp)} pieza(s).", "accion": "Recontar la ubicación, corregir la cifra si fue un error de captura, o confirmarla si es correcta."})
    fus = []
    for x in fusion_candidatos(ubs, lineas, cat):
        cs = cm.get(x["a"], 0)
        fus.append({"tipo": "posible_fusion", "codigo": x["codigo"], "producto": x["de"], "contado": x["de_contado"], "esperado": 0, "valor": r2(abs(x["dif_antes"]) * cs), "contado_por": "",
                    "sugerencia": x["a"], "de": x["de"], "a": x["a"],
                    "explicacion": f"Se contaron {x['de_contado']:g} de «{x['de']}» (no esperado) y «{x['a']}», que sí debía estar en esta ubicación, quedó con {sg(x['dif_antes'])}. Las claves son casi iguales: probable error de escritura.",
                    "accion": "Fusionar si es el mismo producto."})
    fus_de = {(f["codigo"], f["producto"]) for f in fus}
    hall = fus + [h for h in hall if not (h["tipo"] == "clave_inexistente" and (h["codigo"], h["producto"]) in fus_de)]
    hall.sort(key=lambda h: (-abs(h["valor"]), -abs(h["contado"] - h["esperado"])))
    cuenta = {}
    for h in hall: cuenta[h["tipo"]] = cuenta.get(h["tipo"], 0) + 1
    return {"hallazgos": hall[:500], "cuenta": cuenta, "total": len(hall),
            "nota": "Son pistas para revisar, no correcciones automáticas. El catálogo conocido sale de las existencias sincronizadas y de los costos leídos de Odoo."}


@router.post("/conteos/{id_}/capturas/fusionar")
async def api_fusionar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo está cerrado")
    items = body.get("items") or []
    if not items: raise HTTPException(422, "Elige al menos una fusión")
    ls = await lineas_de(db, id_); t = ahora(); n = 0
    for it in items:
        cod = txt(it.get("codigo"))
        de = next((l for l in ls if l.codigo == cod and txt(l.producto) == txt(it.get("de"))), None)
        a = next((l for l in ls if l.codigo == cod and txt(l.producto) == txt(it.get("a"))), None)
        if not de or not a: raise HTTPException(404, f"No encontré {txt(it.get('de'))} o {txt(it.get('a'))} en {cod}")
        if not de.inesperado: raise HTTPException(422, f"{txt(de.producto)} no es un producto no esperado")
        mover = num(de.contado); antes = 0 if a.contado is None else num(a.contado)
        a.contado = antes + mover; a.veces = (a.veces or 0) + 1; a.contado_por = u["email"]; a.fecha_conteo = t
        de.contado = 0; de.inesperado = False; de.origen = "fusion→" + txt(a.producto)
        await log(db, u["email"], "FUSION_CAPTURA", f"{id_} {cod}: {txt(de.producto)} ({mover:g}) → {txt(a.producto)} (contado {antes:g} → {antes + mover:g})")
        n += 1
    await db.commit()
    return {"fusionadas": n}


@router.post("/conteos/{id_}/capturas/resolver")
async def api_resolver(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo está cerrado")
    items = body.get("items") or []
    if not items: raise HTTPException(422, "Elige al menos un renglón")
    ubs = {txt(x.codigo): x for x in await ubs_de(db, id_)}; ls = await lineas_de(db, id_); nc = nk = 0
    for it in items:
        cod, prod = txt(it.get("codigo")), txt(it.get("producto"))
        l = next((x for x in ls if x.codigo == cod and txt(x.producto) == prod), None)
        if not l: raise HTTPException(404, f"No encontré {prod} en {cod}")
        if cod not in ubs or ubs[cod].estado != "contada": raise HTTPException(409, f"{cod} no está en estado «contada» (si está en reconteo, espera a que termine)")
        antes = num(l.contado)
        if it.get("accion") == "corregir":
            try:
                nuevo = float(it.get("nuevo")); assert nuevo >= 0
            except Exception:
                raise HTTPException(422, f"Indica la cantidad correcta de {prod} en {cod}")
            motivo = txt(it.get("motivo"))
            if len(motivo) < 3: raise HTTPException(422, "Escribe el motivo de la corrección")
            l.contado = nuevo; l.veces = (l.veces or 0) + 1
            await log(db, u["email"], "CAPTURA_CORREGIDA", f"{id_} {cod} {prod}: contado {antes:g} → {nuevo:g} (capturó {txt(l.contado_por)}) · {motivo[:200]}")
            nc += 1
        else:
            await log(db, u["email"], "CAPTURA_CONFIRMADA", f"{clave_confirm(id_, cod, prod, antes)} · {txt(it.get('motivo'))[:200]}")
            nk += 1
    await db.commit()
    return {"corregidas": nc, "confirmadas": nk}


# ───────────────────────── COMPLETAR ESPERADO ─────────────────────────
@router.post("/conteos/{id_}/completar-esperado")
async def api_completar_esperado(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo está cerrado")
    syncs = sorted(s.fecha for s in (await db.execute(select(InvSync))).scalars() if s.fecha)
    if not syncs: raise HTTPException(409, "Aún no hay sincronización con Odoo. Sincroniza primero.")
    ini = c.fecha; desde = ini
    for d in syncs:
        if d <= ini: desde = d
    hasta = syncs[-1]
    if hasta < ini: raise HTTPException(409, "La última sincronización con Odoo es anterior al inicio del conteo. Sincroniza de nuevo (con la etiqueta vacía) y reintenta.")
    from models.models import InvExistencia
    ubs = await ubs_de(db, id_); contadas = {txt(x.codigo) for x in ubs if x.estado == "contada"}
    cat = await cat_otras_un(db); lineas = await lineas_de(db, id_)
    ex, desc = {}, {}
    for e in (await db.execute(select(InvExistencia))).scalars():
        k = (txt(e.codigo), txt(e.producto)); ex[k] = ex.get(k, 0) + num(e.cantidad); desc[k] = txt(e.producto_desc)
    tiene = {(txt(l.codigo), txt(l.producto)): l for l in lineas}
    cand = []
    for l in lineas:
        k = (txt(l.codigo), txt(l.producto))
        if txt(l.codigo) in contadas and l.inesperado and not es_otra_un(True, l.origen, l.producto, cat) and l.contado is not None and ex.get(k, 0) > 0:
            cand.append({"tipo": "corrige", "linea": l, "codigo": k[0], "producto": k[1], "descripcion": txt(l.producto_desc), "actual": ex[k]})
    if body.get("incluir_no_contados"):
        for k, q in ex.items():
            if k[0] in contadas and k not in tiene and q > 0:
                cand.append({"tipo": "agrega", "codigo": k[0], "producto": k[1], "descripcion": desc.get(k, ""), "actual": q})
    vent = {"desde": iso(desde), "hasta": iso(hasta)}
    if not cand: return {"aplicado": False, "candidatos": 0, "lineas": [], "resumen": {"corrige": 0, "agrega": 0}, "ventana": vent}
    try:
        movs = await O.movimientos_odoo(sorted({x["codigo"] for x in cand}), desde, hasta)
    except Exception as e:
        raise HTTPException(502, f"No se pudieron leer los movimientos de Odoo ({str(e)[:150]}). No se cambió nada.")
    neto = {}
    for m in movs:
        q = num(m["cantidad"])
        neto[(m["destino"], m["producto"])] = neto.get((m["destino"], m["producto"]), 0) + q
        neto[(m["origen"], m["producto"])] = neto.get((m["origen"], m["producto"]), 0) - q
    salida, t = [], ahora()
    for x in cand:
        n = neto.get((x["codigo"], x["producto"]), 0); esp = max(0, x["actual"] - n)
        if esp <= 0: continue
        con = num(x["linea"].contado) if x["tipo"] == "corrige" else 0
        s = {"tipo": x["tipo"], "codigo": x["codigo"], "producto": x["producto"], "descripcion": x["descripcion"], "existencia_actual": x["actual"], "movimientos_netos": n,
             "esperado_inicio": esp, "contado": con, "diferencia_antes": con if x["tipo"] == "corrige" else None, "diferencia_despues": con - esp}
        salida.append(s)
        if not body.get("aplicar"): continue
        if x["tipo"] == "corrige":
            l = x["linea"]; s["antes"] = num(l.esperado); l.esperado = esp; l.inesperado = False; l.origen = "esp_corregido"
        else:
            db.add(InvConteoLinea(id_conteo=id_, codigo=x["codigo"], producto=x["producto"][:60], producto_desc=x["descripcion"][:250], esperado=esp, contado=0, inesperado=False, veces=1,
                                  contado_por=u["email"], fecha_conteo=t, origen="esp_corregido"))
        await log(db, u["email"], "CORR_ESPERADO", f"{id_} {x['codigo']} {x['producto']} esperado {s.get('antes', 'sin linea')} → {esp:g}")
    if body.get("aplicar"): await db.commit()
    resumen = {"corrige": sum(1 for s in salida if s["tipo"] == "corrige"), "agrega": sum(1 for s in salida if s["tipo"] == "agrega")}
    salida.sort(key=lambda s: -abs(s["diferencia_despues"]))
    return {"aplicado": bool(body.get("aplicar")), "candidatos": len(cand), "resumen": resumen, "lineas": salida[:500], "ventana": vent}
