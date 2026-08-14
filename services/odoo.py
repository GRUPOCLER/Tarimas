import os, httpx

ODOO_URL = os.getenv("ODOO_URL", "https://ecor-b2b-35977843.dev.odoo.com")
ODOO_DB       = os.getenv("ODOO_DB", "")
ODOO_LOGIN    = os.getenv("ODOO_LOGIN", "")
ODOO_PASSWORD = os.getenv("ODOO_API_KEY", "")

PARTNER_MAP = {11088: "Raiker", 12449: "Korei"}

async def _rpc(model: str, method: str, args: list, kwargs: dict = None):
    async with httpx.AsyncClient(timeout=15) as client:
        payload = {
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "service": "object", "method": "execute_kw",
                "args": [ODOO_DB, await _uid(client), ODOO_PASSWORD, model, method, args, kwargs or {}]
            }
        }
        r = await client.post(ODOO_URL + "/jsonrpc", json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise Exception(data["error"].get("data", {}).get("message", "Error Odoo"))
        return data["result"]

_uid_cache = {}
async def _uid(client: httpx.AsyncClient):
    if "uid" in _uid_cache:
        return _uid_cache["uid"]
    payload = {
        "jsonrpc": "2.0", "method": "call",
        "params": {
            "service": "common", "method": "login",
            "args": [ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD]
        }
    }
    r = await client.post(ODOO_URL + "/jsonrpc", json=payload)
    r.raise_for_status()
    data = r.json()
    uid = data.get("result")
    if not uid:
        raise Exception("No se pudo autenticar en Odoo: " + str(data.get("error", "")))
    _uid_cache["uid"] = uid
    return uid

async def listar_ovs_pendientes():
    partner_ids = list(PARTNER_MAP.keys())
    ovs = await _rpc("sale.order", "search_read",
        [[["partner_id", "in", partner_ids], ["picking_ids", "!=", False], ["state", "in", ["sale", "done"]]]],
        {"fields": ["name", "partner_id", "state", "picking_ids", "date_order"], "order": "id desc", "limit": 30}
    )
    return [{
        "num_ov": ov["name"],
        "cliente": ov["partner_id"][1],
        "comercializador": PARTNER_MAP.get(ov["partner_id"][0], ""),
        "fecha": (ov.get("date_order") or "")[:10],
        "picking_ids": ov["picking_ids"]
    } for ov in ovs]

async def cargar_entrega(picking_ids: list):
    picks = await _rpc("stock.picking", "search_read",
        [[["id", "in", picking_ids]]],
        {"fields": ["name", "partner_id", "sale_id", "move_ids"]}
    )
    if not picks:
        raise Exception("La OV no tiene entregas")
    p = picks[0]
    moves = await _rpc("stock.move", "search_read",
        [[["id", "in", p["move_ids"]]]],
        {"fields": ["product_id", "product_uom_qty", "name"]}
    )
    productos = []
    for m in moves:
        if m["product_uom_qty"] <= 0:
            continue
        nombre = m["product_id"][1] if m.get("product_id") else m["name"]
        import re
        match = re.match(r"^\[([^\]]+)\]", nombre)
        clave = match.group(1).strip() if match else nombre.split(" ")[0]
        desc  = nombre.replace(match.group(0), "").strip() if match else nombre
        productos.append({
            "clave": clave, "descripcion": desc,
            "cantidad_total": round(m["product_uom_qty"]), "unidad": "PZA"
        })
    return {
        "num_entrega": p["name"],
        "orden": p["sale_id"][1] if p.get("sale_id") else "",
        "nombre_cliente": p["partner_id"][1] if p.get("partner_id") else "",
        "direccion": p["partner_id"][1] if p.get("partner_id") else "",
        "comercializador": PARTNER_MAP.get(p["partner_id"][0] if p.get("partner_id") else None, ""),
        "fuente": "odoo",
        "productos": productos
    }