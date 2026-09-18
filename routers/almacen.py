from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional
import json

from database import get_db
from models.models import UbicacionAlmacen, LogAcceso
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

def _ser(u: UbicacionAlmacen) -> dict:
    return {
        "codigo": u.codigo, "bodega": u.bodega, "rack": u.rack, "lado": u.lado,
        "tramo": u.tramo, "nivel": u.nivel, "zona": u.zona, "capacidad": u.capacidad,
        "distancia_embarques": u.distancia_embarques,
        "x": u.x, "y": u.y, "ancho": u.ancho, "alto": u.alto,
        "producto": u.producto, "producto_desc": u.producto_desc,
        "producto2": u.producto2, "producto2_desc": u.producto2_desc,
        "stock": u.stock, "notas": u.notas,
        "asignado_por": u.asignado_por, "actualizado": str(u.actualizado or ""),
    }

# ── UBICACIONES ──────────────────────────────────────────────
@router.get("/ubicaciones")
async def listar_ubicaciones(
    buscar: Optional[str] = None,
    bodega: Optional[str] = None,
    zona:   Optional[str] = None,
    solo_libres: bool = False,
    solo_ocupadas: bool = False,
    solo_con_stock: bool = False,
    limite: int = 200,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    q = select(UbicacionAlmacen).order_by(UbicacionAlmacen.codigo).limit(limite)
    if buscar:
        patron = f"%{buscar.strip()}%"
        q = q.where(
            UbicacionAlmacen.codigo.ilike(patron) |
            UbicacionAlmacen.producto.ilike(patron) |
            UbicacionAlmacen.producto_desc.ilike(patron)
        )
    if bodega:
        q = q.where(UbicacionAlmacen.bodega == bodega)
    if zona:
        q = q.where(UbicacionAlmacen.zona == zona)
    if solo_libres:
        q = q.where(UbicacionAlmacen.producto.is_(None))
    if solo_ocupadas:
        q = q.where(UbicacionAlmacen.producto.is_not(None))
    if solo_con_stock:
        q = q.where(UbicacionAlmacen.stock > 0)
    result = await db.execute(q)
    return [_ser(u) for u in result.scalars()]

# ── MAPA: todas las ubicaciones con su geometria, para dibujar
# el plano visual del CEDIS completo de una sola vez ─────────
@router.get("/mapa")
async def mapa_almacen(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    result = await db.execute(select(UbicacionAlmacen))
    return [_ser(u) for u in result.scalars()]

@router.get("/ubicaciones/{codigo}")
async def obtener_ubicacion(codigo: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    result = await db.execute(select(UbicacionAlmacen).where(UbicacionAlmacen.codigo == codigo))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Ubicacion no encontrada")
    return _ser(u)

@router.get("/contar")
async def contar(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    total = await db.execute(select(func.count(UbicacionAlmacen.codigo)))
    ocupadas = await db.execute(select(func.count(UbicacionAlmacen.codigo)).where(UbicacionAlmacen.producto.is_not(None)))
    return {"total": total.scalar() or 0, "ocupadas": ocupadas.scalar() or 0}

# ── ASIGNAR PRODUCTO A UNA UBICACION ─────────────────────────
class AsignarIn(BaseModel):
    producto:       Optional[str] = None
    producto_desc:  Optional[str] = None
    producto2:      Optional[str] = None
    producto2_desc: Optional[str] = None
    stock:          Optional[int] = 0
    notas:          Optional[str] = None

@router.post("/ubicaciones/{codigo}/asignar")
async def asignar_producto(
    codigo: str, body: AsignarIn,
    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)
):
    # Igual que en la version anterior del mapa (admin + editor podian escribir
    # el inventario): cualquier usuario autenticado puede asignar/editar aqui,
    # no solo admin/gerente.
    if user.get("rol") not in ("admin", "gerente", "operador"):
        raise HTTPException(status_code=403, detail="Requiere admin o gerente")
    result = await db.execute(select(UbicacionAlmacen).where(UbicacionAlmacen.codigo == codigo))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Ubicacion no encontrada")

    u.producto        = (body.producto or "").strip().upper() or None
    u.producto_desc   = body.producto_desc
    u.producto2       = (body.producto2 or "").strip().upper() or None
    u.producto2_desc  = body.producto2_desc
    u.stock           = body.stock or 0
    u.notas           = body.notas
    u.asignado_por    = user["sub"]

    from datetime import datetime
    u.actualizado = datetime.utcnow()

    db.add(LogAcceso(
        usuario=user["sub"], accion="asignar_ubicacion",
        detalle=f"Asigno {u.producto or '(vacio)'} a la ubicacion {codigo} (stock: {u.stock})",
        exito=True
    ))
    await db.commit()
    return {"ok": True}

# ── PRODUCTOS DE ODOO (para elegir cual asignar) ─────────────
@router.get("/productos-odoo")
async def buscar_productos_odoo(buscar: str = "", user: dict = Depends(get_current_user)):
    try:
        return await odoo_svc.buscar_productos_venta(buscar)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

# ── IMPORTAR EL MAPA (una sola vez, desde layout_cedis.json) ─
@router.post("/importar-layout")
async def importar_layout(
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

    await db.commit()
    return {"ok": True, "insertados": insertados, "actualizados": actualizados, "total": len(elementos)}
