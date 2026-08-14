from fastapi import APIRouter, Depends, HTTPException
import httpx, os, re
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

@router.get("/diag/probar-db")
async def diag_probar_db(user: dict = Depends(get_current_user)):
    """Prueba login contra Odoo con varios nombres de DB candidatos y reporta cual funciona."""
    url = os.getenv("ODOO_URL", "https://ecor-b2b-35977843.dev.odoo.com")
    login = os.getenv("ODOO_LOGIN", "")
    password = os.getenv("ODOO_API_KEY", "")

    candidatos = [
        "",
        "ecor-b2b-35977843",
        "ecor_b2b_35977843",
        "p_ecor_b2b_35977843",
        "35977843",
    ]

    resultados = []
    async with httpx.AsyncClient(timeout=15) as client:
        for db in candidatos:
            payload = {
                "jsonrpc": "2.0", "method": "call",
                "params": {"service": "common", "method": "login", "args": [db, login, password]}
            }
            try:
                r = await client.post(url + "/jsonrpc", json=payload)
                data = r.json()
                if "error" in data:
                    msg = data["error"].get("data", {}).get("message", data["error"].get("message", ""))
                    resultados.append({"db_probada": db or "(vacio)", "ok": False, "error": msg[:200]})
                else:
                    resultados.append({"db_probada": db or "(vacio)", "ok": True, "uid": data.get("result")})
            except Exception as e:
                resultados.append({"db_probada": db or "(vacio)", "ok": False, "error": str(e)})
    return {"login_usado": login, "resultados": resultados}