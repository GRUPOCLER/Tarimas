from fastapi import APIRouter, Depends, HTTPException
import httpx, os
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

@router.get("/diag/db-list")
async def diag_db_list(user: dict = Depends(get_current_user)):
    """Diagnostico: pide a Odoo la lista real de bases de datos disponibles."""
    url = os.getenv("ODOO_URL", "https://ecor-b2b-35977843.dev.odoo.com")
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            payload = {
                "jsonrpc": "2.0", "method": "call",
                "params": {"service": "db", "method": "list", "args": []}
            }
            r = await client.post(url + "/jsonrpc", json=payload)
            return {"status": r.status_code, "body": r.json()}
        except Exception as e:
            return {"error": str(e)}