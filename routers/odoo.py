from fastapi import APIRouter, Depends, HTTPException
import os
from routers.entregas import get_current_user
from services import odoo as odoo_svc

router = APIRouter()

@router.get("/ovs")
async def listar_ovs(user: dict = Depends(get_current_user)):
    try:
        return await odoo_svc.listar_ovs_pendientes()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

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