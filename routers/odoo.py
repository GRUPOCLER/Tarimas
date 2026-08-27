from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import os
from database import get_db
from models.models import Entrega, AlmacenTraspaso
from routers.entregas import get_current_user
from services import odoo as odoo_svc

router = APIRouter()

@router.get("/ovs")
async def listar_ovs(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    try:
        ovs = await odoo_svc.listar_ovs_pendientes()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Cruzar contra entregas ya existentes en el sistema (por num de OV o folio)
    nombres = [ov["num_ov"].strip().upper() for ov in ovs if ov.get("num_ov")]
    existentes = {}
    if nombres:
        result = await db.execute(
            select(Entrega.orden, Entrega.num_entrega, Entrega.id_entrega, Entrega.sistema).where(
                func.upper(Entrega.orden).in_(nombres) | func.upper(Entrega.num_entrega).in_(nombres)
            )
        )
        for orden, num_entrega, id_entrega, sistema in result.all():
            for clave in (orden, num_entrega):
                if clave:
                    existentes[clave.strip().upper()] = {"id_entrega": id_entrega, "sistema": sistema}

    for ov in ovs:
        match = existentes.get((ov.get("num_ov") or "").strip().upper())
        ov["ya_importada"] = match is not None
        ov["id_entrega_existente"] = match["id_entrega"] if match else None
        ov["sistema_existente"] = match["sistema"] if match else None

    return ovs

@router.post("/entrega")
async def cargar_entrega(picking_ids: list[int], user: dict = Depends(get_current_user)):
    try:
        return await odoo_svc.cargar_entrega(picking_ids)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/traspasos")
async def listar_traspasos(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    result = await db.execute(select(AlmacenTraspaso.odoo_location_id).where(AlmacenTraspaso.activo == True))
    ubicaciones = [row[0] for row in result.all()]

    try:
        # Si no hay nada configurado en la BD todavia, cae al valor por defecto del .env
        traspasos = await odoo_svc.listar_traspasos_pendientes(ubicaciones or None)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    folios = [t["folio"].strip().upper() for t in traspasos if t.get("folio")]
    existentes = {}
    if folios:
        result2 = await db.execute(
            select(Entrega.orden, Entrega.id_entrega, Entrega.sistema).where(func.upper(Entrega.orden).in_(folios))
        )
        for orden, id_entrega, sistema in result2.all():
            if orden:
                existentes[orden.strip().upper()] = {"id_entrega": id_entrega, "sistema": sistema}

    for t in traspasos:
        match = existentes.get((t.get("folio") or "").strip().upper())
        t["ya_importada"] = match is not None
        t["id_entrega_existente"] = match["id_entrega"] if match else None
        t["sistema_existente"] = match["sistema"] if match else None

    return traspasos

@router.post("/traspaso")
async def cargar_traspaso(picking_id: int, user: dict = Depends(get_current_user)):
    try:
        return await odoo_svc.cargar_traspaso(picking_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

# ── ADMINISTRACION DE ALMACENES DE TRASPASO (solo Admin) ────────────
@router.get("/almacenes-disponibles")
async def buscar_almacenes_odoo(nombre: str = "", user: dict = Depends(get_current_user)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede configurar esto")
    try:
        return await odoo_svc.buscar_almacenes(nombre)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/almacenes-traspaso")
async def listar_almacenes_configurados(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede configurar esto")
    result = await db.execute(select(AlmacenTraspaso).order_by(AlmacenTraspaso.fecha_agregado.desc()))
    return [{
        "id": a.id, "odoo_warehouse_id": a.odoo_warehouse_id, "odoo_location_id": a.odoo_location_id,
        "nombre": a.nombre, "codigo": a.codigo, "activo": a.activo,
        "agregado_por": a.agregado_por, "fecha_agregado": str(a.fecha_agregado or "")
    } for a in result.scalars()]

@router.post("/almacenes-traspaso")
async def agregar_almacen(body: dict, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede configurar esto")
    existe = await db.execute(select(AlmacenTraspaso).where(AlmacenTraspaso.odoo_warehouse_id == body["odoo_warehouse_id"]))
    if existe.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Ese almacen ya esta agregado")
    nuevo = AlmacenTraspaso(
        odoo_warehouse_id=body["odoo_warehouse_id"], odoo_location_id=body["odoo_location_id"],
        nombre=body.get("nombre", ""), codigo=body.get("codigo", ""), activo=True, agregado_por=user["sub"]
    )
    db.add(nuevo)
    await db.commit()
    return {"ok": True}

@router.patch("/almacenes-traspaso/{id_almacen}")
async def editar_almacen(id_almacen: int, body: dict, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede configurar esto")
    result = await db.execute(select(AlmacenTraspaso).where(AlmacenTraspaso.id == id_almacen))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="No encontrado")
    if "activo" in body:
        a.activo = body["activo"]
    await db.commit()
    return {"ok": True}

@router.delete("/almacenes-traspaso/{id_almacen}")
async def quitar_almacen(id_almacen: int, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede configurar esto")
    result = await db.execute(select(AlmacenTraspaso).where(AlmacenTraspaso.id == id_almacen))
    a = result.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="No encontrado")
    await db.delete(a)
    await db.commit()
    return {"ok": True}

@router.get("/diag/env")
async def diag_env(user: dict = Depends(get_current_user)):
    """Muestra el valor RAW que el proceso ve ahora mismo para ODOO_DB."""
    valor = os.getenv("ODOO_DB", "NO_DEFINIDA")
    return {
        "ODOO_DB_raw": repr(valor),
        "longitud": len(valor),
        "ODOO_DB_desde_modulo": repr(odoo_svc.ODOO_DB),
    }

@router.get("/diag/buscar-partner")
async def diag_buscar_partner(nombre: str, user: dict = Depends(get_current_user)):
    """Busca partners por nombre en la base de Odoo conectada ahora mismo.
    Util al cambiar de Odoo de pruebas a productivo: cada base numera sus
    registros distinto, esto encuentra el ID real de Raiker/Korei/etc
    para poner en ODOO_PARTNER_RAIKER_ID / ODOO_PARTNER_KOREI_ID."""
    if user.get("rol") not in ("admin", "gerente"):
        raise HTTPException(status_code=403, detail="Requiere admin o gerente")
    try:
        resultados = await odoo_svc._rpc(
            "res.partner", "search_read",
            [[["name", "ilike", nombre]]],
            {"fields": ["id", "name"], "limit": 15}
        )
        return {"db_actual": odoo_svc.ODOO_DB, "resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
