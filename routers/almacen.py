from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, exists, and_
from pydantic import BaseModel
from typing import Optional
import json, io, csv

from database import get_db
from models.models import UbicacionAlmacen, ExistenciaUbicacion, AlmacenSync, LogAcceso
from services.auth import verificar_token
from services import odoo as odoo_svc

router = APIRouter()

async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin autorizacion")
    payload = verificar_token(authorization.split(" ")[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    return payload

# Las ubicaciones y existencias viven en Odoo: aqui solo se leen.
def _ser(u: UbicacionAlmacen, prods: list = None) -> dict:
    prods = prods or []
    total = sum(p["cantidad"] for p in prods)
    return {
        "codigo": u.codigo, "bodega": u.bodega, "rack": u.rack, "lado": u.lado,
        "tramo": u.tramo, "nivel": u.nivel, "zona": u.zona, "capacidad": u.capacidad,
        "distancia_embarques": u.distancia_embarques,
        "x": u.x, "y": u.y, "ancho": u.ancho, "alto": u.alto,
        "productos": prods, "stock": total, "ocupada": len(prods) > 0,
        # compatibilidad: primer producto
        "producto": prods[0]["clave"] if prods else None,
        "producto_desc": prods[0]["descripcion"] if prods else None,
    }

async def _existencias_por_codigo(db: AsyncSession, codigos=None, claves=None) -> dict:
    q = select(ExistenciaUbicacion).order_by(ExistenciaUbicacion.producto)
    if codigos is not None:
        q = q.where(ExistenciaUbicacion.codigo.in_(codigos))
    res = await db.execute(q)
    mapa = {}
    for e in res.scalars():
        mapa.setdefault(e.codigo, []).append({
            "clave": e.producto, "descripcion": e.producto_desc,
            "cantidad": e.cantidad or 0, "reservado": e.reservado or 0})
    return mapa

def _filtro_texto_producto(texto: str):
    """Cada palabra debe aparecer en la clave o en la descripcion."""
    partes = []
    for palabra in (texto or "").split():
        patron = f"%{palabra}%"
        partes.append(ExistenciaUbicacion.producto.ilike(patron) | ExistenciaUbicacion.producto_desc.ilike(patron))
    return and_(*partes) if partes else None

def _ubic_con_producto(texto: str = None, clave: str = None):
    cond = [ExistenciaUbicacion.codigo == UbicacionAlmacen.codigo]
    if clave:
        cond.append(ExistenciaUbicacion.producto == clave)
    elif texto:
        f = _filtro_texto_producto(texto)
        if f is not None: cond.append(f)
    return exists().where(and_(*cond))

# ── UBICACIONES ──────────────────────────────────────────────
@router.get("/ubicaciones")
async def listar_ubicaciones(
    buscar: Optional[str] = None,
    producto: Optional[str] = None,     # clave exacta (viene del buscador de producto)
    bodega: Optional[str] = None,
    zona:   Optional[str] = None,
    solo_libres: bool = False,
    solo_ocupadas: bool = False,
    solo_con_stock: bool = False,
    limite: int = 300,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    q = select(UbicacionAlmacen).order_by(UbicacionAlmacen.codigo)
    if producto:
        q = q.where(_ubic_con_producto(clave=producto))
    if buscar:
        patron = f"%{buscar.strip()}%"
        q = q.where(UbicacionAlmacen.codigo.ilike(patron) | _ubic_con_producto(texto=buscar))
    if bodega:
        q = q.where(UbicacionAlmacen.bodega == bodega)
    if zona:
        q = q.where(UbicacionAlmacen.zona == zona)
    ocupada = exists().where(ExistenciaUbicacion.codigo == UbicacionAlmacen.codigo)
    if solo_libres:
        q = q.where(~ocupada)
    if solo_ocupadas or solo_con_stock:
        q = q.where(ocupada)
    res = await db.execute(q.limit(limite))
    ubics = res.scalars().all()
    ex = await _existencias_por_codigo(db, [u.codigo for u in ubics])
    return [_ser(u, ex.get(u.codigo)) for u in ubics]

# ── MAPA: todas las ubicaciones con geometria y existencias ──
@router.get("/mapa")
async def mapa_almacen(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    res = await db.execute(select(UbicacionAlmacen))
    ex = await _existencias_por_codigo(db)
    return [_ser(u, ex.get(u.codigo)) for u in res.scalars()]

# ── BUSCAR UN PRODUCTO: donde esta y como esta distribuido ───
@router.get("/producto")
async def buscar_producto(q: str = "", db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    filtro = _filtro_texto_producto(q)
    if filtro is None or len(q.strip()) < 2:
        return []
    res = await db.execute(
        select(ExistenciaUbicacion.producto, func.max(ExistenciaUbicacion.producto_desc),
               func.sum(ExistenciaUbicacion.cantidad), func.count(ExistenciaUbicacion.codigo))
        .where(filtro).group_by(ExistenciaUbicacion.producto).order_by(ExistenciaUbicacion.producto).limit(15)
    )
    productos = res.all()
    salida = []
    for clave, desc, total, n in productos:
        r2 = await db.execute(
            select(ExistenciaUbicacion, UbicacionAlmacen)
            .join(UbicacionAlmacen, UbicacionAlmacen.codigo == ExistenciaUbicacion.codigo, isouter=True)
            .where(ExistenciaUbicacion.producto == clave).order_by(ExistenciaUbicacion.cantidad.desc()))
        dist = []
        for e, u in r2.all():
            dist.append({"codigo": e.codigo, "cantidad": e.cantidad or 0, "reservado": e.reservado or 0,
                         "bodega": u.bodega if u else None, "zona": u.zona if u else None,
                         "rack": u.rack if u else None, "en_mapa": u is not None})
        salida.append({"clave": clave, "descripcion": desc, "total": total or 0, "ubicaciones": n, "distribucion": dist})
    return salida

# ── SINCRONIZAR DESDE ODOO (solo lectura en Odoo) ────────────
@router.post("/sincronizar")
async def sincronizar(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("rol") not in ("admin", "gerente"):
        raise HTTPException(status_code=403, detail="Solo Gerente o Administrador pueden sincronizar")
    try:
        filas = await odoo_svc.listar_existencias_ubicaciones()
    except Exception as e:
        raise HTTPException(status_code=502, detail="No se pudo leer Odoo: " + str(e))
    codigos_mapa = {c for (c,) in (await db.execute(select(UbicacionAlmacen.codigo))).all()}
    await db.execute(delete(ExistenciaUbicacion))
    sin_mapa, usados, prods = set(), set(), set()
    for f in filas:
        if f["barcode"] not in codigos_mapa:
            sin_mapa.add(f["barcode"]); continue
        usados.add(f["barcode"]); prods.add(f["clave"])
        db.add(ExistenciaUbicacion(codigo=f["barcode"], producto=f["clave"][:60], producto_desc=(f["nombre"] or "")[:250],
                                   cantidad=f["cantidad"], reservado=f["reservado"]))
    ejemplos = ", ".join(sorted(sin_mapa)[:30])
    db.add(AlmacenSync(usuario=user["sub"], filas=len(filas) - len([1 for f in filas if f["barcode"] in sin_mapa]),
                       ubicaciones=len(usados), productos=len(prods), sin_mapa=len(sin_mapa), ejemplos_sin_mapa=ejemplos))
    db.add(LogAcceso(usuario=user["sub"], accion="SYNC_ALMACEN",
                     detalle=f"{len(usados)} ubicaciones, {len(prods)} productos, {len(sin_mapa)} sin mapa", exito=True))
    await db.commit()
    return {"ok": True, "ubicaciones": len(usados), "productos": len(prods), "sin_mapa": len(sin_mapa), "ejemplos_sin_mapa": ejemplos}

@router.get("/estado-sync")
async def estado_sync(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    r = await db.execute(select(AlmacenSync).order_by(AlmacenSync.id.desc()).limit(1))
    s = r.scalar_one_or_none()
    if not s:
        return {"sincronizado": False}
    return {"sincronizado": True, "fecha": str(s.fecha), "usuario": s.usuario, "ubicaciones": s.ubicaciones,
            "productos": s.productos, "sin_mapa": s.sin_mapa, "ejemplos_sin_mapa": s.ejemplos_sin_mapa or ""}

# ── EXPORTAR A CSV: existencias por ubicacion ────────────────
@router.get("/exportar-csv")
async def exportar_csv(
    bodega: Optional[str] = None, zona: Optional[str] = None, producto: Optional[str] = None,
    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)
):
    q = (select(ExistenciaUbicacion, UbicacionAlmacen)
         .join(UbicacionAlmacen, UbicacionAlmacen.codigo == ExistenciaUbicacion.codigo)
         .order_by(ExistenciaUbicacion.codigo, ExistenciaUbicacion.producto))
    if bodega: q = q.where(UbicacionAlmacen.bodega == bodega)
    if zona: q = q.where(UbicacionAlmacen.zona == zona)
    if producto: q = q.where(ExistenciaUbicacion.producto == producto)
    res = await db.execute(q)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Codigo", "Bodega", "Rack", "Lado", "Tramo", "Nivel", "Zona", "Clave producto", "Descripcion", "Existencia", "Reservado"])
    for e, u in res.all():
        writer.writerow([u.codigo, u.bodega or "", u.rack or "", u.lado or "", u.tramo or "", u.nivel or "",
                         u.zona or "", e.producto or "", e.producto_desc or "", e.cantidad or 0, e.reservado or 0])
    return Response(content="\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=existencias_almacen.csv"})

@router.get("/ubicaciones/{codigo}")
async def obtener_ubicacion(codigo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    result = await db.execute(select(UbicacionAlmacen).where(UbicacionAlmacen.codigo == codigo))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Ubicacion no encontrada")
    ex = await _existencias_por_codigo(db, [codigo])
    return _ser(u, ex.get(codigo))

@router.get("/contar")
async def contar(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    total = await db.execute(select(func.count(UbicacionAlmacen.codigo)))
    ocupadas = await db.execute(select(func.count(func.distinct(ExistenciaUbicacion.codigo))))
    return {"total": total.scalar() or 0, "ocupadas": ocupadas.scalar() or 0}

# La asignacion manual ya no existe: ubicaciones y existencias se administran en Odoo.
@router.post("/ubicaciones/{codigo}/asignar")
async def asignar_deshabilitado(codigo: str, user: dict = Depends(get_current_user)):
    raise HTTPException(status_code=410, detail="La asignacion se hace en Odoo; aqui solo se consulta.")

@router.post("/importar-layout")
async def importar_layout(
    reemplazar: bool = False,   # True = borra las ubicaciones que ya no vienen en el archivo (renombrado de codigos)
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)
):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede importar el mapa")

    contenido = await archivo.read()
    try:
        data = json.loads(contenido)
    except Exception:
        raise HTTPException(status_code=422, detail="El archivo no es un JSON valido")

    elementos = data.get("elements", [])
    if not elementos:
        raise HTTPException(status_code=422, detail="El JSON no trae 'elements'")

    insertados, actualizados = 0, 0
    for el in elementos:
        codigo = str(el.get("id") or "").strip()
        if not codigo:
            continue
        result = await db.execute(select(UbicacionAlmacen).where(UbicacionAlmacen.codigo == codigo))
        u = result.scalar_one_or_none()
        es_nuevo = u is None
        if es_nuevo:
            u = UbicacionAlmacen(codigo=codigo)
            db.add(u)

        u.bodega              = el.get("bodega")
        u.rack                = str(el.get("rack")) if el.get("rack") is not None else None
        u.lado                = el.get("lado")
        u.tramo                = el.get("tramo")
        u.nivel                = el.get("nivel")
        u.zona                 = el.get("zona")
        u.capacidad            = el.get("espacios") or 1
        u.distancia_embarques  = el.get("distancia_embarques") or 0
        u.x      = el.get("x") or 0
        u.y      = el.get("y") or 0
        u.ancho  = el.get("width") or 1
        u.alto   = el.get("height") or 1

        if es_nuevo: insertados += 1
        else: actualizados += 1

    eliminados = 0
    if reemplazar:
        ids_nuevos = {str(el.get("id") or "").strip() for el in elementos}
        viejos = await db.execute(select(UbicacionAlmacen.codigo).where(UbicacionAlmacen.codigo.not_in(ids_nuevos)))
        sobrantes = [c for (c,) in viejos.all()]
        if sobrantes:
            await db.execute(delete(UbicacionAlmacen).where(UbicacionAlmacen.codigo.in_(sobrantes)))
        eliminados = len(sobrantes)
    await db.commit()
    return {"ok": True, "insertados": insertados, "actualizados": actualizados, "eliminados": eliminados, "total": len(elementos)}
