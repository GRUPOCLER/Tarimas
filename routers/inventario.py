"""Inventario y Operaciones: mapa del almacen, existencias (Odoo, solo lectura) y conteos de auditoria.
Port de la version Apps Script (Almacen.gs, Conteo.gs, Capturas.gs). Odoo NUNCA se modifica."""
import asyncio, io, csv, json, re, uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select, delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import (Usuario, UsuarioModulo, InvUbicacion, InvExistencia, InvSync, InvConteo, InvConteoUbic,
                           InvConteoLinea, InvConteoRonda, InvConteoPausa, InvCosto, InvOtraUN, InvLog)
from services.permisos import usuario_actual
from services import inv_odoo as O
from services.inv_core import *

router = APIRouter()
_lock_sync = asyncio.Lock()
NIVEL = {"admin": 3, "gerente": 3, "auditor": 2, "operador": 1}


async def inv_user(user: dict = Depends(usuario_actual)):
    rol = user["modulos"].get("inventario")
    if not rol:
        raise HTTPException(403, "No tienes acceso a este modulo")
    return {**user, "rol": rol, "nivel": NIVEL.get(rol, 1), "email": txt(user["sub"]).lower(), "modulo": "inventario"}


def exige(u, minimo):
    if u["nivel"] < minimo:
        raise HTTPException(403, "No tienes permiso para esta seccion")


def exige_gestor(u):
    if u["nivel"] < 3:
        raise HTTPException(403, "Solo quien administra el modulo Conteo (gerente o administrador)")


def es_gestor(u) -> bool:
    return u["nivel"] >= 3


async def log(db, usuario, accion, detalle=""):
    db.add(InvLog(fecha=ahora(), usuario=usuario, accion=accion, detalle=txt(detalle)[:500]))


def archivo_csv(nombre, filas):
    return {"nombre": nombre, "csv": csv_texto(filas)}


# ───────────────────────── datos base ─────────────────────────
async def conteo_o_404(db, id_) -> InvConteo:
    c = await db.get(InvConteo, txt(id_))
    if not c:
        raise HTTPException(404, "Conteo no encontrado")
    return c


async def ubs_de(db, id_):
    return (await db.execute(select(InvConteoUbic).where(InvConteoUbic.id_conteo == id_))).scalars().all()


async def lineas_de(db, id_):
    return (await db.execute(select(InvConteoLinea).where(InvConteoLinea.id_conteo == id_))).scalars().all()


async def exist_por_codigo(db):
    m = {}
    for e in (await db.execute(select(InvExistencia))).scalars():
        m.setdefault(txt(e.codigo), []).append({"clave": txt(e.producto), "descripcion": txt(e.producto_desc), "cantidad": num(e.cantidad), "reservado": num(e.reservado)})
    return m


async def cat_otras_un(db):
    return {txt(r.producto).upper(): (txt(r.unidad) or "Sin asignar") for r in (await db.execute(select(InvOtraUN))).scalars() if txt(r.producto)}


async def costos_mapa(db):
    return {txt(r.producto): num(r.costo) for r in (await db.execute(select(InvCosto))).scalars()}


def es_plano(u: InvUbicacion) -> bool:
    return not txt(u.bodega)


def pos(v):
    n = num(v)
    return 0 if n < -5 or n > 1000 else n


async def estado_sync(db):
    s = (await db.execute(select(InvSync).order_by(InvSync.id.desc()).limit(1))).scalar_one_or_none()
    if not s:
        return None
    return {"fecha": iso(s.fecha), "usuario": txt(s.usuario), "quants": s.quants or 0, "ubicaciones": s.ubicaciones or 0, "sin_mapa": s.sin_mapa or 0, "detalle": txt(s.detalle)}


@router.get("/yo")
async def yo(u: dict = Depends(inv_user)):
    return {"usuario": u["email"], "rol": u["rol"], "nivel": u["nivel"], "gestor": es_gestor(u)}


@router.get("/usuarios")
async def usuarios_asignables(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    """Quien puede contar: auditores y gerentes de Inventario y administradores activos."""
    exige_gestor(u)
    mods = (await db.execute(select(UsuarioModulo).where(UsuarioModulo.modulo == "inventario", UsuarioModulo.rol.in_(["auditor", "gerente"])))).scalars().all()
    por_rol = {m.usuario: m.rol for m in mods}
    us = (await db.execute(select(Usuario).where(Usuario.activo == True))).scalars().all()  # noqa: E712
    out = []
    for x in us:
        rol = "admin" if x.rol == "admin" else por_rol.get(x.usuario)
        if rol:
            out.append({"usuario": x.usuario.lower(), "nombre": getattr(x, "nombre_display", None) or x.usuario, "rol": rol})
    return sorted(out, key=lambda o: o["usuario"])


async def valida_contador(db, email):
    """El usuario debe estar activo y poder contar (auditor/gerente/admin)."""
    x = (await db.execute(select(Usuario).where(func.lower(Usuario.usuario) == email))).scalar_one_or_none()
    if not x or not x.activo:
        raise HTTPException(400, f"El usuario {email} no esta dado de alta/activo")
    if x.rol == "admin":
        return
    m = (await db.execute(select(UsuarioModulo).where(UsuarioModulo.usuario == x.usuario, UsuarioModulo.modulo == "inventario"))).scalar_one_or_none()
    if not m or NIVEL.get(m.rol, 0) < 2:
        raise HTTPException(400, f"{email} no tiene permiso para contar (rol auditor o gerente de Inventario)")


# ───────────────────────── ALMACEN: mapa, listado, buscador ─────────────────────────
@router.get("/mapa")
async def api_mapa(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    ex = await exist_por_codigo(db)
    items = []
    for r in (await db.execute(select(InvUbicacion))).scalars():
        prods = ex.get(txt(r.codigo), [])
        items.append({"c": txt(r.codigo), "b": txt(r.bodega), "z": txt(r.zona), "x": pos(r.x), "y": pos(r.y), "w": pos(r.ancho), "h": pos(r.alto),
                      "t": txt(r.tipo), "s": sum(p["cantidad"] for p in prods), "n": len(prods)})
    return {"items": items, "sync": await estado_sync(db)}


async def _listar(db, bodega="", zona="", codigo="", producto="", estado="todas"):
    ex = await exist_por_codigo(db)
    q, pq = norm(codigo), norm(producto)
    rows = []
    for r in (await db.execute(select(InvUbicacion))).scalars():
        if es_plano(r):
            continue
        c = txt(r.codigo); prods = ex.get(c, [])
        if bodega and txt(r.bodega) != bodega: continue
        if zona and txt(r.zona) != zona: continue
        if q and q not in norm(c): continue
        if estado == "con" and not prods: continue
        if estado == "vacias" and prods: continue
        if pq and not any(pq in norm(x["clave"] + " " + x["descripcion"]) for x in prods): continue
        rows.append({"codigo": c, "bodega": txt(r.bodega), "zona": txt(r.zona), "rack": rack_de_codigo(c), "nivel": txt(r.nivel),
                     "stock": sum(x["cantidad"] for x in prods), "productos": prods})
    rows.sort(key=lambda r: r["codigo"])
    return rows


@router.get("/listar")
async def api_listar(bodega: str = "", zona: str = "", codigo: str = "", producto: str = "", estado: str = "todas", pagina: int = 1, limite: int = 100,
                     db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    rows = await _listar(db, bodega, zona, codigo, producto, estado)
    pagina, lim = max(1, pagina), min(max(limite, 1), 5000)
    return {"total": len(rows), "pagina": pagina, "limite": lim, "filas": rows[(pagina - 1) * lim: pagina * lim]}


@router.get("/listar/csv")
async def api_listar_csv(bodega: str = "", zona: str = "", codigo: str = "", producto: str = "", estado: str = "todas",
                         db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    out = [["Ubicacion", "Bodega", "Zona", "Clave", "Descripcion", "Cantidad", "Reservado"]]
    for f in await _listar(db, bodega, zona, codigo, producto, estado):
        if not f["productos"]:
            out.append([f["codigo"], f["bodega"], f["zona"], "", "", 0, 0])
        for x in f["productos"]:
            out.append([f["codigo"], f["bodega"], f["zona"], x["clave"], x["descripcion"], x["cantidad"], x["reservado"]])
    return archivo_csv("existencias_por_ubicacion.csv", out)


@router.get("/filtros")
async def api_filtros(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    us = (await db.execute(select(InvUbicacion.bodega, InvUbicacion.zona))).all()
    return {"bodegas": sorted({txt(b) for b, _ in us if txt(b)}), "zonas": sorted({txt(z) for _, z in us if txt(z)})}


@router.get("/ubicacion/{codigo}")
async def api_ubicacion(codigo: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    ub = await db.get(InvUbicacion, txt(codigo))
    if not ub:
        raise HTTPException(404, f"No existe la ubicacion {codigo}")
    return {"codigo": ub.codigo, "bodega": txt(ub.bodega), "zona": txt(ub.zona), "rack": rack_de_codigo(ub.codigo), "nivel": txt(ub.nivel),
            "capacidad": ub.capacidad, "productos": (await exist_por_codigo(db)).get(ub.codigo, [])}


@router.get("/producto")
async def api_buscar_producto(q: str = "", db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    """Donde esta un producto y como esta distribuido."""
    qq = norm(q)
    if len(qq) < 2:
        return []
    ub = {txt(r.codigo): r for r in (await db.execute(select(InvUbicacion))).scalars()}
    por, orden = {}, []
    for e in (await db.execute(select(InvExistencia))).scalars():
        clave = txt(e.producto)
        if qq not in norm(clave + " " + txt(e.producto_desc)):
            continue
        if clave not in por:
            por[clave] = {"clave": clave, "descripcion": txt(e.producto_desc), "total": 0, "distribucion": []}; orden.append(clave)
        r = ub.get(txt(e.codigo))
        por[clave]["total"] += num(e.cantidad)
        por[clave]["distribucion"].append({"codigo": txt(e.codigo), "cantidad": num(e.cantidad), "reservado": num(e.reservado),
                                           "bodega": txt(r.bodega) if r else "", "zona": txt(r.zona) if r else "", "en_mapa": bool(r)})
    out = []
    for k in sorted(orden)[:15]:
        o = por[k]; o["ubicaciones"] = len(o["distribucion"]); o["distribucion"].sort(key=lambda d: -d["cantidad"])
        pb = {}
        for d in o["distribucion"]:
            pb[d["bodega"] or "Sin mapa"] = pb.get(d["bodega"] or "Sin mapa", 0) + d["cantidad"]
        o["por_bodega"] = [{"bodega": b, "cantidad": c} for b, c in pb.items()]
        out.append(o)
    return out


@router.get("/sync")
async def api_estado_sync(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    return await estado_sync(db)


async def sincronizar(db, usuario):
    async with _lock_sync:
        quants = await O.existencias_odoo()
        mapa = {txt(c) for (c,) in (await db.execute(select(InvUbicacion.codigo))).all()}
        acum, sin_mapa = {}, set()
        for q in quants:
            k = (q["barcode"], q["clave"])
            a = acum.setdefault(k, {"codigo": q["barcode"], "producto": q["clave"][:60], "producto_desc": q["nombre"][:250], "cantidad": 0, "reservado": 0})
            a["cantidad"] += q["cantidad"]; a["reservado"] += q["reservado"]
            if q["barcode"] not in mapa:
                sin_mapa.add(q["barcode"])
        filas = list(acum.values())
        await db.execute(delete(InvExistencia))
        db.add_all([InvExistencia(**f) for f in filas])
        db.add(InvSync(fecha=ahora(), usuario=usuario, quants=len(quants), ubicaciones=len({f["codigo"] for f in filas}),
                       sin_mapa=len(sin_mapa), detalle=", ".join(sorted(sin_mapa)[:20])))
        await log(db, usuario, "SYNC_ODOO", f"{len(quants)} quants")
        await db.commit()
    return await estado_sync(db)


@router.post("/sincronizar")
async def api_sincronizar(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    try:
        return await sincronizar(db, u["email"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"No se pudo leer Odoo: {str(e)[:200]}")


@router.post("/ubicaciones/importar")
async def importar_ubicaciones(archivo: UploadFile = File(...), db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    """Carga el plano (CSV: codigo,bodega,rack,lado,tramo,nivel,zona,capacidad,x,y,ancho,alto,tipo). Reemplaza el plano completo."""
    exige_gestor(u)
    crudo = (await archivo.read()).decode("utf-8-sig", errors="replace")
    lector = csv.DictReader(io.StringIO(crudo))
    if not lector.fieldnames or "codigo" not in [txt(c).lower() for c in lector.fieldnames]:
        raise HTTPException(422, "El CSV debe traer la columna 'codigo'.")
    vistos, filas = set(), []
    for r in lector:
        r = {txt(k).lower(): v for k, v in r.items() if k}
        cod = txt(r.get("codigo"))
        if not cod or cod in vistos:
            continue
        vistos.add(cod)
        def f(k): return num(str(r.get(k) or "").replace(",", "."))
        filas.append(InvUbicacion(codigo=cod[:40], bodega=txt(r.get("bodega"))[:40], rack=txt(r.get("rack"))[:20], lado=txt(r.get("lado"))[:10], tramo=txt(r.get("tramo"))[:10],
                                  nivel=txt(r.get("nivel"))[:10], zona=txt(r.get("zona"))[:20], capacidad=f("capacidad"), x=f("x"), y=f("y"), ancho=f("ancho"), alto=f("alto"), tipo=txt(r.get("tipo"))[:20]))
    if not filas:
        raise HTTPException(422, "El CSV no trae ubicaciones.")
    await db.execute(delete(InvUbicacion))
    db.add_all(filas)
    await log(db, u["email"], "UBICACIONES_IMPORTADAS", f"{len(filas)} ubicaciones")
    await db.commit()
    return {"ubicaciones": len(filas)}


# ───────────────────────── CONTEOS ─────────────────────────
async def progreso(db, id_):
    ubs = await ubs_de(db, id_)
    hechas = sum(1 for x in ubs if x.estado == "contada")
    return {"ubicaciones": len(ubs), "contadas": hechas, "pendientes": len(ubs) - hechas, "pct": r1(hechas * 100 / len(ubs)) if ubs else 0}


def ser_conteo(c, prog):
    return {"id": c.id, "nombre": txt(c.nombre), "alcance": txt(c.alcance), "ciego": bool(c.ciego), "estatus": txt(c.estatus), "creado_por": txt(c.creado_por),
            "fecha": iso(c.fecha), "cerrado_por": txt(c.cerrado_por), "fecha_cierre": iso(c.fecha_cierre), "tipo": txt(c.tipo) or "ubicacion",
            "productos": txt(c.productos), "progreso": prog}


async def pausas_de(db, id_):
    out = []
    for x in (await db.execute(select(InvConteoPausa).where(InvConteoPausa.id_conteo == id_).order_by(InvConteoPausa.id))).scalars():
        fin = x.fin or ahora()
        out.append({"inicio": iso(x.inicio), "pausado_por": txt(x.pausado_por), "motivo": txt(x.motivo), "fin": iso(x.fin), "reanudado_por": txt(x.reanudado_por),
                    "minutos": round((fin - x.inicio).total_seconds() / 60) if x.inicio else 0})
    return out


async def cierra_pausa_abierta(db, id_, quien):
    p = (await db.execute(select(InvConteoPausa).where(InvConteoPausa.id_conteo == id_, InvConteoPausa.fin == None).order_by(InvConteoPausa.id.desc()))).scalars().first()  # noqa: E711
    if p:
        p.fin = ahora(); p.reanudado_por = quien


@router.post("/conteos/{id_}/pausar")
async def api_pausar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo ya esta cerrado")
    if c.estatus == "pausado": raise HTTPException(409, "El conteo ya esta en pausa")
    c.estatus = "pausado"
    db.add(InvConteoPausa(id_conteo=id_, inicio=ahora(), pausado_por=u["email"], motivo=txt(body.get("motivo"))[:250]))
    await log(db, u["email"], "CONTEO_PAUSADO", f"{id_} {txt(body.get('motivo'))}")
    await db.commit()
    return {"ok": True}


@router.post("/conteos/{id_}/reabrir")
async def api_reabrir(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    """Corrige un cierre por error: el conteo vuelve en PAUSA conservando todo lo capturado."""
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus != "cerrado": raise HTTPException(409, "El conteo no esta cerrado")
    cerro = txt(c.cerrado_por)
    c.estatus = "pausado"; c.cerrado_por = None; c.fecha_cierre = None
    motivo = ("Reabierto tras cierre" + (f" por {cerro}" if cerro else "") + (f": {txt(body.get('motivo'))}" if txt(body.get("motivo")) else ""))[:250]
    db.add(InvConteoPausa(id_conteo=id_, inicio=ahora(), pausado_por=u["email"], motivo=motivo))
    await log(db, u["email"], "CONTEO_REABIERTO", f"{id_} (cerrado por {cerro})")
    await db.commit()
    return {"ok": True}


@router.post("/conteos/{id_}/reanudar")
async def api_reanudar(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus != "pausado": raise HTTPException(409, "El conteo no esta en pausa")
    c.estatus = "abierto"
    await cierra_pausa_abierta(db, id_, u["email"])
    await log(db, u["email"], "CONTEO_REANUDADO", id_)
    await db.commit()
    return {"ok": True}


@router.get("/conteos")
async def api_listar_conteos(db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    todos = (await db.execute(select(InvConteo).order_by(InvConteo.fecha.desc()).limit(100))).scalars().all()
    tot, ok, mias = {}, {}, {}
    for x in (await db.execute(select(InvConteoUbic))).scalars():
        i = x.id_conteo; tot[i] = tot.get(i, 0) + 1
        if x.estado == "contada": ok[i] = ok.get(i, 0) + 1
        if txt(x.asignado_a).lower() == u["email"]:
            m = mias.setdefault(i, {"total": 0, "contadas": 0}); m["total"] += 1
            if x.estado == "contada": m["contadas"] += 1
    pausa_ab, n_pausas = {}, {}
    for p in (await db.execute(select(InvConteoPausa))).scalars():
        n_pausas[p.id_conteo] = n_pausas.get(p.id_conteo, 0) + 1
        if not p.fin:
            pausa_ab[p.id_conteo] = {"inicio": iso(p.inicio), "pausado_por": txt(p.pausado_por), "motivo": txt(p.motivo)}
    out = []
    for c in todos:
        if not (es_gestor(u) or c.estatus in ("abierto", "pausado")):
            continue
        t, h = tot.get(c.id, 0), ok.get(c.id, 0)
        o = ser_conteo(c, {"ubicaciones": t, "contadas": h, "pendientes": t - h, "pct": r1(h * 100 / t) if t else 0})
        o["mias"] = mias.get(c.id, {"total": 0, "contadas": 0}); o["pausa"] = pausa_ab.get(c.id); o["n_pausas"] = n_pausas.get(c.id, 0)
        out.append(o)
    return out


@router.post("/conteos")
async def api_crear_conteo(body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    nombre = txt(body.get("nombre"))
    if len(nombre) < 3:
        raise HTTPException(422, "Escribe un nombre para el conteo")
    pref, bodega, zona = txt(body.get("prefijo")).upper(), txt(body.get("bodega")), txt(body.get("zona"))
    solo_ex = bool(body.get("solo_con_existencia"))
    ex = await exist_por_codigo(db)
    skus, sku_set = [], None
    crudo = body.get("productos")
    if crudo not in (None, "") and txt(crudo if isinstance(crudo, str) else " ".join(map(str, crudo))):
        lista = crudo if isinstance(crudo, list) else re.split(r"[\s,;]+", str(crudo))
        vistos = set()
        for x in lista:
            x = txt(x)
            if x and x.upper() not in vistos:
                vistos.add(x.upper()); skus.append(x)
        if not skus: raise HTTPException(422, "Escribe al menos una clave de producto")
        if len(skus) > 200: raise HTTPException(422, "Maximo 200 productos por conteo")
        sku_set = {x.upper() for x in skus}
    en_lista = lambda e: not sku_set or txt(e["clave"]).upper() in sku_set
    ubics = []
    for r in (await db.execute(select(InvUbicacion))).scalars():
        c = txt(r.codigo)
        if es_plano(r): continue
        if bodega and txt(r.bodega) != bodega: continue
        if zona and txt(r.zona) != zona: continue
        if pref and not c.upper().startswith(pref): continue
        if solo_ex and c not in ex: continue
        if sku_set and not any(en_lista(e) for e in ex.get(c, [])): continue
        ubics.append(r)
    ubics.sort(key=lambda r: txt(r.codigo))
    partes = []
    if bodega: partes.append(bodega)
    if zona: partes.append("Zona " + zona)
    if pref: partes.append("prefijo " + pref)
    if solo_ex: partes.append("solo con existencia")
    if sku_set:
        hallados = {txt(e["clave"]).upper() for r in ubics for e in ex.get(txt(r.codigo), []) if en_lista(e)}
        nohay = [x for x in skus if x.upper() not in hallados]
        if len(nohay) == len(skus):
            raise HTTPException(422, "Ninguno de esos productos tiene existencia en ubicaciones del filtro: " + ", ".join(nohay[:10]))
        partes.insert(0, "Por producto: " + ", ".join(skus[:8]) + (f" +{len(skus) - 8}" if len(skus) > 8 else ""))
        if nohay: partes.append("sin existencia en sistema: " + ", ".join(nohay[:10]))
    if not ubics: raise HTTPException(422, "Ninguna ubicacion cumple ese filtro")
    if len(ubics) > MAX_UBICACIONES_CONTEO:
        raise HTTPException(422, f"El filtro trae {len(ubics)} ubicaciones; el maximo por conteo es {MAX_UBICACIONES_CONTEO}. Afina el filtro.")
    id_ = "CNT-" + uuid.uuid4().hex[:8].upper()
    c = InvConteo(id=id_, nombre=nombre, alcance=(", ".join(partes) or "Todo el almacen")[:250], ciego=body.get("ciego") is not False, estatus="abierto",
                  creado_por=u["email"], fecha=ahora(), tipo="producto" if sku_set else "ubicacion", productos=(", ".join(skus)[:2000] if sku_set else ""))
    db.add(c)
    for r in ubics:
        db.add(InvConteoUbic(id_conteo=id_, codigo=txt(r.codigo), bodega=txt(r.bodega), zona=txt(r.zona), estado="pendiente", grupo=grupo_de(r.codigo, r.bodega), ronda=1))
        for e in ex.get(txt(r.codigo), []):
            if en_lista(e):
                db.add(InvConteoLinea(id_conteo=id_, codigo=txt(r.codigo), producto=e["clave"], producto_desc=e["descripcion"], esperado=e["cantidad"], contado=None, inesperado=False, veces=0))
    await log(db, u["email"], "CONTEO_CREADO", f"{id_} {nombre} ({len(ubics)} ubicaciones)")
    await db.commit()
    return ser_conteo(c, await progreso(db, id_))


@router.get("/conteos/{id_}")
async def api_detalle_conteo(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    c = await conteo_o_404(db, id_)
    o = ser_conteo(c, await progreso(db, id_)); ps = await pausas_de(db, id_)
    o["pausas"] = ps; o["pausa"] = next((x for x in ps if not x["fin"]), None)
    return o


@router.get("/conteos/{id_}/grupos")
async def api_grupos(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    g = {}
    for x in await ubs_de(db, id_):
        k = txt(x.grupo) or "Sin grupo"
        o = g.setdefault(k, {"grupo": k, "total": 0, "contadas": 0, "asignados": set()})
        o["total"] += 1; o["contadas"] += x.estado == "contada"; o["asignados"].add(txt(x.asignado_a))
    out = []
    for k in sorted(g, key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]):
        as_ = [a for a in g[k]["asignados"] if a]
        out.append({"grupo": k, "total": g[k]["total"], "contadas": g[k]["contadas"],
                    "asignado_a": as_[0] if len(as_) == 1 and len(g[k]["asignados"]) == 1 else ("varios" if as_ else "")})
    return out


@router.post("/conteos/{id_}/asignar")
async def api_asignar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    email = txt(body.get("email")).lower()
    if email: await valida_contador(db, email)
    sel = set(body.get("grupos") or [])
    n = 0
    for x in await ubs_de(db, id_):
        if txt(x.grupo) in sel:
            x.asignado_a = email; n += 1
    await log(db, u["email"], "CONTEO_ASIGNA", f"{id_} {'; '.join(sel)} -> {email or 'nadie'}")
    await db.commit()
    return {"ok": True, "cambiadas": n}


@router.get("/conteos/{id_}/ubicaciones")
async def api_ubicaciones_conteo(id_: str, estado: str = "", buscar: str = "", mias: bool = False, grupo: str = "", limite: int = 100,
                                 db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    await conteo_o_404(db, id_)
    b, lim = norm(buscar), min(max(limite, 1), 500)
    out = []
    for x in await ubs_de(db, id_):
        if estado and not (x.estado == estado or (estado == "pendiente" and x.estado == "reconteo")): continue
        if b and b not in norm(x.codigo): continue
        if mias and txt(x.asignado_a).lower() != u["email"]: continue
        if grupo and txt(x.grupo) != grupo: continue
        out.append(x)
    out.sort(key=lambda x: txt(x.codigo))
    return [{"codigo": txt(x.codigo), "bodega": txt(x.bodega), "zona": txt(x.zona), "estado": x.estado, "contada_por": txt(x.contada_por), "fecha_conteo": iso(x.fecha_conteo),
             "grupo": txt(x.grupo), "asignado_a": txt(x.asignado_a), "ronda": x.ronda or 1} for x in out[:lim]]


async def ubic_conteo(db, id_, codigo) -> InvConteoUbic:
    ub = (await db.execute(select(InvConteoUbic).where(InvConteoUbic.id_conteo == id_, InvConteoUbic.codigo == codigo))).scalar_one_or_none()
    if not ub:
        raise HTTPException(404, f"La ubicacion {codigo} no forma parte de este conteo")
    return ub


@router.get("/conteos/{id_}/ubicacion/{codigo}")
async def api_ver_ubicacion(id_: str, codigo: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige(u, 2)
    cat = await cat_otras_un(db)
    c = await conteo_o_404(db, id_); codigo = txt(codigo); ub = await ubic_conteo(db, id_, codigo)
    rec_todo = ub.estado == "reconteo"; previo = rec_todo and txt(ub.modo_reco) == "previo"; rec = rec_todo and not previo
    ciego = (rec or bool(c.ciego)) and c.estatus != "cerrado"
    ls = [l for l in await lineas_de(db, id_) if l.codigo == codigo and not (rec and l.inesperado)]
    ls.sort(key=lambda l: txt(l.producto))
    return {"codigo": codigo, "bodega": txt(ub.bodega), "zona": txt(ub.zona), "grupo": txt(ub.grupo), "asignado_a": txt(ub.asignado_a), "estado": ub.estado,
            "ronda": ub.ronda or 1, "modo_reco": ("previo" if previo else "ciego") if rec_todo else "", "nota": "" if rec else txt(ub.nota), "ciego": ciego,
            "lineas": [{"producto": txt(l.producto), "descripcion": txt(l.producto_desc), "esperado": None if ciego else num(l.esperado),
                        "contado": None if (rec or l.contado is None) else num(l.contado), "inesperado": bool(l.inesperado),
                        "otra_un": es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)} for l in ls]}


async def contadores_previos(db, id_, codigo, ub):
    s = set()
    if txt(ub.contada_por): s.add(txt(ub.contada_por).lower())
    for r in (await db.execute(select(InvConteoRonda).where(InvConteoRonda.id_conteo == id_, InvConteoRonda.codigo == codigo, InvConteoRonda.anulada != True))).scalars():  # noqa: E712
        if re.fullmatch(r"\d+", txt(r.ronda)) and txt(r.contado_por): s.add(txt(r.contado_por).lower())
    return s


async def claves_conocidas(db):
    k = {}
    for (p,) in (await db.execute(select(InvExistencia.producto).distinct())).all():
        k[norm_clave(p)] = txt(p)
    for (p,) in (await db.execute(select(InvCosto.producto))).all():
        k[norm_clave(p)] = txt(p)
    return k


def valida_captura_previa(body, env, existentes, conocidas_fn):
    """Si algo luce raro lanza 409 'CONFIRMAR:...' para que el auditor confirme o corrija."""
    if body.get("confirmar") or body.get("vacia"):
        return
    avisos, conocidas = [], None
    for k, e in env.items():
        l, con = existentes.get(k), num(e.get("contado"))
        if l is None:
            if e.get("otra_un"): continue
            conocidas = conocidas if conocidas is not None else conocidas_fn()
            if norm_clave(k) in conocidas: continue
            s = sugerencia_clave(k, conocidas)
            avisos.append(f"«{k}» no existe; ¿quisiste decir «{s}»?" if s else f"«{k}» no existe en el catálogo del sistema (¿está bien escrita o es de otra unidad de negocio?)")
        elif not l.inesperado:
            m = motivo_cantidad(num(l.esperado), con)
            if m: avisos.append(f"«{k}»: {m}")
    if avisos:
        raise HTTPException(409, "CONFIRMAR:" + "\n".join(avisos) + "\n\n¿Guardar tal cual? (Aceptar = sí, es correcto · Cancelar = corregir)")


@router.post("/conteos/{id_}/capturar")
async def api_capturar(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige(u, 2)
    codigo = txt(body.get("codigo")); enviadas = body.get("lineas") or []
    for l in enviadas:
        try:
            ok = float(l.get("contado")) >= 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise HTTPException(422, "Las cantidades deben ser numeros y no pueden ser negativas")
    c = await conteo_o_404(db, id_)
    if c.estatus == "pausado": raise HTTPException(409, "El conteo esta en PAUSA. Un gestor debe reanudarlo para seguir capturando.")
    if c.estatus != "abierto": raise HTTPException(409, "Este conteo ya esta cerrado")
    ub = await ubic_conteo(db, id_, codigo)
    asig = txt(ub.asignado_a).lower()
    if asig and asig != u["email"] and not es_gestor(u):
        raise HTTPException(403, f"La ubicacion {codigo} ({txt(ub.grupo)}) esta asignada a {asig}")
    rec_todo = ub.estado == "reconteo"; rec = rec_todo and txt(ub.modo_reco) != "previo"
    if rec_todo and not es_gestor(u) and asig != u["email"] and u["email"] in await contadores_previos(db, id_, codigo, ub):
        raise HTTPException(409, f"El reconteo de {codigo} debe hacerlo otra persona (tu ya la contaste). Pide a un supervisor que la reasigne.")
    existentes = {txt(l.producto): l for l in await lineas_de(db, id_) if l.codigo == codigo}
    env = {txt(l["producto"]): l for l in enviadas}
    if txt(c.tipo) == "producto":
        ok = {x.upper() for x in re.split(r"[\s,;]+", txt(c.productos)) if x}
        fuera = [k for k in env if k not in existentes and k.upper() not in ok]
        if fuera:
            raise HTTPException(422, f"Este conteo es por producto: solo se cuentan {txt(c.productos)[:120]}. No va: {', '.join(fuera[:5])}. Si encontraste otro producto, reportalo como incidencia.")
    conocidas_cache = {}
    async def _cc():
        return await claves_conocidas(db)
    if not (body.get("confirmar") or body.get("vacia")) and any(k not in existentes for k in env if not env[k].get("otra_un")):
        conocidas_cache = await _cc()
    valida_captura_previa(body, env, existentes, lambda: conocidas_cache)
    t = ahora()
    if body.get("vacia"):
        for l in existentes.values():
            l.contado = 0; l.veces = (l.veces or 0) + 1; l.contado_por = u["email"]; l.fecha_conteo = t
    else:
        faltan = [k for k, l in existentes.items() if not l.inesperado and k not in env and not txt(l.origen).startswith("fusion")]
        if faltan:
            raise HTTPException(422, "Falta capturar: " + ", ".join(faltan) + ". Si no hay nada, captura 0 o usa 'Ubicacion vacia'.")
        if rec:
            for k, l in existentes.items():   # "no esperado" de la ronda anterior que ahora no aparece: ya no hay
                if l.inesperado and k not in env:
                    l.contado = 0; l.veces = (l.veces or 0) + 1; l.contado_por = u["email"]; l.fecha_conteo = t
        for k, e in env.items():
            l, cant = existentes.get(k), float(e["contado"])
            if l:
                l.contado = cant; l.veces = (l.veces or 0) + 1; l.contado_por = u["email"]; l.fecha_conteo = t
                if e.get("otra_un") and l.inesperado: l.origen = "otra_un"
            else:
                db.add(InvConteoLinea(id_conteo=id_, codigo=codigo, producto=k[:60], producto_desc=txt(e.get("descripcion"))[:250], esperado=0, contado=cant,
                                      inesperado=True, veces=1, contado_por=u["email"], fecha_conteo=t, origen="otra_un" if e.get("otra_un") else ""))
    ub.estado = "contada"; ub.contada_por = u["email"]; ub.fecha_conteo = t
    if body.get("nota") is not None: ub.nota = txt(body.get("nota"))
    await db.commit()
    return {"ok": True, "progreso": await progreso(db, id_)}


# ───────────────────────── CIERRE, REPORTE, CSV ─────────────────────────
from models.models import InvIncidencia
from services import inv_analisis as A

TIPOS_INC = {"sin_registro": "Producto sin registro en sistema", "ubicacion_equivocada": "Producto en ubicación equivocada", "no_localizado": "Producto no localizado",
             "danado": "Producto dañado o golpeado", "defectuoso": "Producto defectuoso (falla)", "empaque": "Empaque abierto o kit incompleto",
             "sobrante": "Sobrante en ubicación", "codigo_barras": "Código de barras no lee o no coincide", "diferencia": "Código con diferencia", "otro": "Otro"}
IMPACTOS_INC = ["Vendible — sin afectación", "No vendible", "Requiere revisión técnica", "No aplica"]
ACCIONES_INC = ["Se dejó en sitio", "Se reportó a Operaciones para reubicar", "Pendiente de definir"]


async def incidencias_de(db, id_):
    return (await db.execute(select(InvIncidencia).where((InvIncidencia.id_conteo == id_) | (InvIncidencia.id_conteo == None) | (InvIncidencia.id_conteo == "")))).scalars().all()  # noqa: E711


def ser_inc(r):
    return {"id": txt(r.id), "id_conteo": txt(r.id_conteo), "fecha": iso(r.fecha), "reportado_por": txt(r.reportado_por), "auditor": txt(r.auditor) or txt(r.reportado_por),
            "codigo": txt(r.codigo), "producto": txt(r.producto), "descripcion": txt(r.producto_desc), "tipo": txt(r.tipo), "tipo_txt": TIPOS_INC.get(txt(r.tipo), txt(r.tipo)),
            "cantidad": num(r.cantidad), "impacto": txt(r.impacto), "accion": txt(r.accion), "detalle": txt(r.detalle), "evidencia": txt(r.evidencia),
            "estado": txt(r.estado) or "abierta", "resuelta_por": txt(r.resuelta_por), "fecha_resolucion": iso(r.fecha_resolucion), "nota_resolucion": txt(r.nota_resolucion)}


@router.post("/conteos/{id_}/cerrar")
async def api_cerrar(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "Ya esta cerrado")
    estaba_pausado = c.estatus == "pausado"
    try:
        inc = {}
        for r in await incidencias_de(db, id_):
            if txt(r.id_conteo) == id_: inc[txt(r.tipo) or "otro"] = inc.get(txt(r.tipo) or "otro", 0) + 1
        c.resumen = A.foto_cierre(await ubs_de(db, id_), await lineas_de(db, id_), await costos_mapa(db), await cat_otras_un(db), inc)
    except Exception:
        pass  # la foto es un extra: si falla, el conteo se cierra igual
    c.estatus = "cerrado"; c.cerrado_por = u["email"]; c.fecha_cierre = ahora()
    if estaba_pausado: await cierra_pausa_abierta(db, id_, u["email"] + " (cierre)")
    await log(db, u["email"], "CONTEO_CERRADO", id_)
    await db.commit()
    return {"ok": True}


async def reporte(db, id_):
    return A.calcular_reporte(await ubs_de(db, id_), await lineas_de(db, id_), await costos_mapa(db), await cat_otras_un(db))


@router.get("/conteos/{id_}/reporte")
async def api_reporte(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    c = await conteo_o_404(db, id_); r = await reporte(db, id_); r.pop("_todas", None)
    r["conteo"] = ser_conteo(c, await progreso(db, id_)); r["pausas"] = await pausas_de(db, id_)
    r["minutos_pausado"] = sum(x["minutos"] for x in r["pausas"])
    return r


@router.get("/conteos/{id_}/totales-sku/csv")
async def api_totales_sku_csv(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    f = [["Producto", "Descripcion", "Contado total", "Sistema (en ubicaciones contadas)", "Diferencia", "Ubicaciones", "Ubicaciones con diferencia"]]
    for k in (await reporte(db, id_))["por_sku"]:
        f.append([k["producto"], k["descripcion"], k["contado"], k["esperado"], k["diferencia"], k["ubicaciones"], k["con_diferencia"]])
    return archivo_csv(f"{id_}_totales_sku.csv", f)


@router.get("/conteos/{id_}/reporte/csv")
async def api_reporte_csv(id_: str, todas: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    cm, cat = await costos_mapa(db), await cat_otras_un(db)
    f = [["Conteo", "Ubicacion", "Bodega", "Zona", "Clave", "Descripcion", "Esperado (sistema)", "Contado", "Diferencia", "% dif.", "Costo unitario", "Valor diferencia", "Producto no esperado", "Contado por", "Veces capturado", "Otra unidad de negocio"]]
    if todas:
        ub = {txt(x.codigo): x for x in await ubs_de(db, id_) if x.estado == "contada"}
        for l in sorted([l for l in await lineas_de(db, id_) if l.codigo in ub and l.contado is not None], key=lambda l: (txt(l.codigo), txt(l.producto))):
            e, con = num(l.esperado), num(l.contado); d = con - e; x = ub[l.codigo]; cs = cm.get(txt(l.producto), 0)
            otra = es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)
            f.append([id_, l.codigo, txt(x.bodega), txt(x.zona), txt(l.producto), txt(l.producto_desc), e, con, d, r1(d * 100 / e) if e else "", cs or "", r2(d * cs),
                      "SI" if l.inesperado else "", txt(l.contado_por), l.veces or 0, (cat.get(txt(l.producto).upper()) or "Sin asignar") if otra else ""])
    else:
        for d in (await reporte(db, id_))["_todas"]:
            f.append([id_, d["codigo"], d["bodega"], d["zona"], d["producto"], d["descripcion"], d["esperado"], d["contado"], d["diferencia"], "" if d["pct"] is None else d["pct"],
                      d["costo"] or "", d["valor"], "SI" if d["inesperado"] else "", d["contado_por"], d["veces"]])
    return archivo_csv(f"{id_}_{'completo' if todas else 'diferencias'}.csv", f)


# ───────────────────────── VALIDACION CONTRA MOVIMIENTOS DE ODOO ─────────────────────────
async def movimientos(db, c):
    syncs = (await db.execute(select(InvSync).order_by(InvSync.id))).scalars().all()
    return await A.analizar_movimientos(c, await ubs_de(db, c.id), await lineas_de(db, c.id), await cat_otras_un(db), syncs)


@router.get("/conteos/{id_}/movimientos")
async def api_movimientos(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    try:
        r = await movimientos(db, await conteo_o_404(db, id_))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"No se pudieron leer los movimientos de Odoo: {str(e)[:200]}")
    for k in ("_difs", "_ovs", "_traslados", "_otros"): r.pop(k, None)
    return r


@router.get("/conteos/{id_}/movimientos/csv")
async def api_movimientos_csv(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await movimientos(db, await conteo_o_404(db, id_))
    f = [["Seccion", "Ubicacion", "Clave", "Descripcion", "Esperado", "Contado", "Diferencia", "Movimiento neto Odoo", "Diferencia sin explicar", "Estado", "Tipo mov.", "Documento (OV/picking)", "Fecha", "Origen", "Destino", "Cantidad", "Posible mal surtido"]]
    for d in r["_difs"]:
        po = "; ".join((f"cruce con {x['ubicacion']} ({x['cantidad']})" if x["tipo"] == "cruce" else f"OV {x.get('documento')} desde {x['ubicacion']} ({x['cantidad']})") for x in d["posible_origen"])
        for m in (d["movimientos"] or [None]):
            f.append(["DIFERENCIA", d["codigo"], d["producto"], d["descripcion"], d["esperado"], d["contado"], d["diferencia"], d["movimiento_neto"], d["diferencia_sin_explicar"], d["estado"],
                      m["tipo"] if m else "", m["documento"] if m else "", m["fecha"] if m else "", m["origen"] if m else "", m["destino"] if m else "", m["cantidad"] if m else "", po])
    for o in r["_ovs"]:
        for l in o["lineas"]:
            f.append(["OV SURTIDA", l["ubicacion"], l["producto"], l["descripcion"], "", "", "", "", "", "", "OV", o["ov"] or o["picking"], l["fecha"], l["ubicacion"], o["cliente"], l["cantidad"]])
    for t in r["_traslados"]:
        f.append(["TRASLADO", "", t["producto"], t["descripcion"], "", "", "", "", "", "", "traslado", t["picking"], t["fecha"], t["origen"], t["destino"], t["cantidad"]])
    for t in r["_otros"]:
        f.append(["OTRO MOV.", "", t["producto"], "", "", "", "", "", "", "", t["tipo"], t["documento"], t["fecha"], t["origen"], t["destino"], t["cantidad"]])
    return archivo_csv(f"{id_}_validacion_movimientos.csv", f)


# ───────────────────────── MAPA DE AVANCE ─────────────────────────
@router.get("/conteos/{id_}/mapa")
async def api_mapa_conteo(id_: str, movimientos_: bool = False, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    """Colorea el almacen segun el estado de cada ubicacion. Estados: fuera|pendiente|asignada|reconteo|contada|ok|dif|explicada.
    Quien no es gestor no recibe diferencias (ve 'contada')."""
    c = await conteo_o_404(db, id_); gestor = es_gestor(u)
    ubs = {txt(x.codigo): x for x in await ubs_de(db, id_)}
    dif_cod, est_mov = set(), {}
    if gestor:
        cat = await cat_otras_un(db)
        for l in await lineas_de(db, id_):
            x = ubs.get(txt(l.codigo))
            if l.contado is None or not x or x.estado != "contada" or es_otra_un(bool(l.inesperado), l.origen, l.producto, cat): continue
            if abs(num(l.contado) - num(l.esperado)) > EPS: dif_cod.add(txt(l.codigo))
        if movimientos_:
            for d in (await movimientos(db, c))["_difs"]:
                e = est_mov.get(d["codigo"], "explicada")
                if d["estado"] not in ("explicada", "mal_surtido"): e = "dif"
                est_mov[d["codigo"]] = e
    cuenta = {k: 0 for k in ("pendiente", "asignada", "reconteo", "contada", "ok", "dif", "explicada")}
    items = []
    for r in (await db.execute(select(InvUbicacion))).scalars():
        cod = txt(r.codigo); x = ubs.get(cod); e = "fuera"
        if x:
            if x.estado == "reconteo": e = "reconteo"
            elif x.estado != "contada": e = "asignada" if txt(x.asignado_a) else "pendiente"
            elif not gestor: e = "contada"
            else: e = ("explicada" if movimientos_ and est_mov.get(cod) == "explicada" else "dif") if cod in dif_cod else "ok"
            cuenta[e] += 1
        items.append({"c": cod, "b": txt(r.bodega), "x": pos(r.x), "y": pos(r.y), "w": pos(r.ancho), "h": pos(r.alto), "t": txt(r.tipo), "e": e,
                      "a": txt(x.asignado_a) if x else "", "g": txt(x.grupo) if x else "", "mia": bool(x and txt(x.asignado_a).lower() == u["email"])})
    ps = await pausas_de(db, id_)
    return {"items": items, "cuenta": cuenta, "estatus": txt(c.estatus), "progreso": await progreso(db, id_), "gestor": gestor,
            "pausa": next((p for p in ps if not p["fin"]), None), "actualizado": iso(ahora())}


@router.get("/conteos/{id_}/mapa/{codigo}")
async def api_detalle_ubic_mapa(id_: str, codigo: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    c = await conteo_o_404(db, id_); gestor = es_gestor(u); codigo = txt(codigo)
    ub = (await db.execute(select(InvConteoUbic).where(InvConteoUbic.id_conteo == id_, InvConteoUbic.codigo == codigo))).scalar_one_or_none()
    ubi = await db.get(InvUbicacion, codigo)
    o = {"codigo": codigo, "en_conteo": bool(ub), "bodega": txt(ubi.bodega) if ubi else "", "zona": txt(ubi.zona) if ubi else "", "estado": ub.estado if ub else "fuera",
         "ronda": (ub.ronda or 1) if ub else 0, "asignado_a": txt(ub.asignado_a) if ub else "", "contada_por": txt(ub.contada_por) if ub else "",
         "fecha_conteo": iso(ub.fecha_conteo) if ub else "", "grupo": txt(ub.grupo) if ub else "", "nota": txt(ub.nota) if ub and gestor else ""}
    if gestor and ub:
        cm, cat = await costos_mapa(db), await cat_otras_un(db); tot = {"sistema": 0, "contado": 0, "valor_dif": 0}; ls = []
        for l in await lineas_de(db, id_):
            if l.codigo != codigo: continue
            esp = num(l.esperado); con = None if l.contado is None else num(l.contado); cs = cm.get(txt(l.producto), 0); d = None if con is None else con - esp
            otra = es_otra_un(bool(l.inesperado), l.origen, l.producto, cat)
            if con is not None and not otra:
                tot["sistema"] += esp; tot["contado"] += con; tot["valor_dif"] += d * cs
            ls.append({"producto": txt(l.producto), "descripcion": txt(l.producto_desc), "esperado": esp, "contado": con, "diferencia": d, "costo": cs,
                       "valor": None if d is None else r2(d * cs), "inesperado": bool(l.inesperado), "otra_un": otra, "contado_por": txt(l.contado_por)})
        tot["valor_dif"] = r2(tot["valor_dif"]); o["lineas"] = ls; o["totales"] = tot
        o["incidencias"] = [ser_inc(r) for r in await incidencias_de(db, id_) if txt(r.codigo).upper() == codigo.upper()]
    return o


# ───────────────────────── INCIDENCIAS ─────────────────────────
@router.get("/incidencias/opciones")
async def api_opciones_inc(u: dict = Depends(inv_user)):
    return {"tipos": TIPOS_INC, "impactos": IMPACTOS_INC, "acciones": ACCIONES_INC}


@router.post("/conteos/{id_}/incidencias")
async def api_crear_inc(id_: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige(u, 2)
    c = await conteo_o_404(db, id_)
    if c.estatus == "cerrado": raise HTTPException(409, "El conteo esta cerrado")
    tipo = txt(body.get("tipo"))
    if tipo not in TIPOS_INC: raise HTTPException(422, "Elige el tipo de incidencia")
    prod, cod = txt(body.get("producto"))[:120], txt(body.get("codigo")).upper()
    if not cod: raise HTTPException(422, "Indica la ubicacion")
    if not prod: raise HTTPException(422, "Indica el numero de parte o descripcion")
    try: cant = float(body.get("cantidad"))
    except (TypeError, ValueError): cant = 0
    if not cant > 0: raise HTTPException(422, "Indica la cantidad afectada (mayor a 0)")
    impacto, accion = txt(body.get("impacto")) or "No aplica", txt(body.get("accion")) or "Pendiente de definir"
    if impacto not in IMPACTOS_INC: raise HTTPException(422, "Impacto no valido")
    if accion not in ACCIONES_INC: raise HTTPException(422, "Accion no valida")
    desc = next((txt(l.producto_desc) for l in await lineas_de(db, id_) if txt(l.producto).upper() == prod.upper()), "")
    evid = txt(body.get("evidencia"))[:500]
    foto = body.get("foto") or {}
    if foto.get("data"):   # la foto se guarda en la base (el cliente la reduce antes de enviarla)
        if len(foto["data"]) > 1_500_000: raise HTTPException(413, "La foto es muy pesada")
        evid = foto["data"] if str(foto["data"]).startswith("data:") else f"data:{foto.get('tipo') or 'image/jpeg'};base64,{foto['data']}"
    r = InvIncidencia(id="INC-" + uuid.uuid4().hex[:8].upper(), id_conteo=id_, fecha=ahora(), reportado_por=u["email"], auditor=txt(body.get("auditor"))[:80] or u["email"],
                      codigo=cod, producto=prod, producto_desc=desc[:250], tipo=tipo, cantidad=cant, impacto=impacto, accion=accion, detalle=txt(body.get("detalle"))[:1000],
                      evidencia=evid, estado="abierta")
    db.add(r); await log(db, u["email"], "INCIDENCIA", f"{r.id} {tipo} {prod}"); await db.commit()
    return ser_inc(r)


@router.get("/conteos/{id_}/incidencias")
async def api_listar_inc(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige(u, 2); await conteo_o_404(db, id_)
    rs = [ser_inc(r) for r in await incidencias_de(db, id_) if es_gestor(u) or txt(r.reportado_por).lower() == u["email"]]
    return sorted(rs, key=lambda r: r["fecha"], reverse=True)


@router.post("/incidencias/{iid}/resolver")
async def api_resolver_inc(iid: str, body: dict, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u)
    r = await db.get(InvIncidencia, txt(iid))
    if not r: raise HTTPException(404, "No existe esa incidencia")
    abrir = body.get("reabrir") is True
    r.estado = "abierta" if abrir else "resuelta"; r.resuelta_por = None if abrir else u["email"]; r.fecha_resolucion = None if abrir else ahora()
    if body.get("nota") is not None: r.nota_resolucion = txt(body.get("nota"))[:500]
    await db.commit()
    return ser_inc(r)


@router.get("/conteos/{id_}/incidencias/csv")
async def api_inc_csv(id_: str, db: AsyncSession = Depends(get_db), u: dict = Depends(inv_user)):
    exige_gestor(u); await conteo_o_404(db, id_)
    f = [["Incidencia", "Fecha", "Auditor", "Ubicacion", "Numero de parte o descripcion", "Tipo de incidencia", "Cantidad afectada", "Impacto", "Accion tomada", "Detalle", "Evidencia", "Estado", "Resolvio", "Fecha resolucion", "Nota resolucion"]]
    for r in (ser_inc(x) for x in await incidencias_de(db, id_)):
        f.append([r["id"], r["fecha"], r["auditor"], r["codigo"], r["producto"], r["tipo_txt"], r["cantidad"], r["impacto"], r["accion"], r["detalle"], "(foto adjunta)" if r["evidencia"].startswith("data:") else r["evidencia"], r["estado"], r["resuelta_por"], r["fecha_resolucion"], r["nota_resolucion"]])
    return archivo_csv(f"{id_}_incidencias.csv", f)
