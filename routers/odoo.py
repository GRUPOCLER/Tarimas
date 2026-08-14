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

@router.get("/diag/session-info")
async def diag_session_info(user: dict = Depends(get_current_user)):
    """Diagnostico: busca el nombre real de la BD en el HTML publico de login."""
    url = os.getenv("ODOO_URL", "https://ecor-b2b-35977843.dev.odoo.com")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(url + "/web/login")
            texto = r.text
            candidatos = set()
            for pat in [
                r'name=[\'"]db[\'"]\s+value=[\'"]([^\'"]+)[\'"]',
                r'data-db=[\'"]([^\'"]+)[\'"]',
                r'"db"\s*:\s*[\'"]([^\'"]+)[\'"]',
            ]:
                for m in re.finditer(pat, texto):
                    candidatos.add(m.group(1))
            return {
                "status": r.status_code,
                "candidatos_db": list(candidatos),
                "largo_html": len(texto),
                "primeros_3000": texto[:3000]
            }
        except Exception as e:
            return {"error": str(e)}