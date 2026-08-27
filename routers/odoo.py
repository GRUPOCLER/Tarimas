from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import os
from database import get_db
from models.models import Entrega
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

@router.get("/diag/env")
async def diag_env(user: dict = Depends(get_current_user)):
    """Muestra el valor RAW que el proceso ve ahora mismo para ODOO_DB."""
    valor = os.getenv("ODOO_DB", "NO_DEFINIDA")
    return {
        "ODOO_DB_raw": repr(valor),
        "longitud": len(valor),
        "ODOO_DB_desde_modulo": repr(odoo_svc.ODOO_DB),
    }
