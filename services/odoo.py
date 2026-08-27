import os, re, httpx

ODOO_URL      = os.getenv("ODOO_URL", "")
ODOO_DB       = os.getenv("ODOO_DB", "")
ODOO_LOGIN    = os.getenv("ODOO_LOGIN", "")
ODOO_PASSWORD = os.getenv("ODOO_API_KEY", "")

# Ubicaciones destino de traspasos internos que interesan al almacen —
# via variables de entorno para poder ajustarlas sin tocar codigo.
def _destinos_traspaso():
    raw = os.getenv("ODOO_UBICACIONES_TRASPASO", "29,69,117,125,133,165")
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return ids

DESTINOS_TRASPASO = _destinos_traspaso()

# IDs de partner via variables de entorno — cada base de Odoo (prueba vs
# productivo) numera sus registros distinto, asi que el cambio entre
# ambientes NO requiere tocar codigo, solo actualizar estas 2 variables.
def _partner_map():
    mapa = {}
    raiker_id = os.getenv("ODOO_PARTNER_RAIKER_ID", "11088")
    korei_id  = os.getenv("ODOO_PARTNER_KOREI_ID", "12449")
    if raiker_id.strip().isdigit():
        mapa[int(raiker_id)] = "Raiker"
    if korei_id.strip().isdigit():
        mapa[int(korei_id)] = "Korei"
    return mapa

PARTNER_MAP = _partner_map()

async def _rpc(model: str, method: str, args: list, kwargs: dict = None):
    if not ODOO_URL:
        raise Exception(
            "ODOO_URL no esta configurada en las variables de Railway. "
            "Revisa el servicio cler-backend -> Variables."
        )
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
    ovs = await _rpc("sale.order", "search_read",
        [[["picking_ids", "!=", False], ["state", "in", ["sale", "done"]]]],
        {"fields": ["name", "partner_id", "state", "picking_ids", "date_order"], "order": "id desc", "limit": 60}
    )
    return [{
        "num_ov": ov["name"],
        "cliente": ov["partner_id"][1] if ov.get("partner_id") else "",
        "comercializador": PARTNER_MAP.get(ov["partner_id"][0] if ov.get("partner_id") else None, ""),
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

# ── TRASPASOS INTERNOS (CEDIS, FULL MELI, Eventos y Expo) ────────
async def listar_traspasos_pendientes(ubicaciones: list = None):
    ubicaciones = ubicaciones if ubicaciones is not None else DESTINOS_TRASPASO
    if not ubicaciones:
        return []
    pickings = await _rpc("stock.picking", "search_read",
        [[
            ["picking_type_id.code", "=", "internal"],
            ["location_dest_id", "child_of", ubicaciones],
            ["state", "not in", ["done", "cancel"]]
        ]],
        {
            "fields": ["name", "location_id", "location_dest_id", "state", "origin", "move_ids", "scheduled_date"],
            "order": "id desc", "limit": 60
        }
    )
    return [{
        "id":         p["id"],
        "folio":      p["name"],
        "origen":     p["location_id"][1] if p.get("location_id") else "",
        "destino":    p["location_dest_id"][1] if p.get("location_dest_id") else "",
        "estado":     p["state"],
        "referencia": p.get("origin") or "",
        "fecha":      (p.get("scheduled_date") or "")[:10],
        "move_ids":   p["move_ids"]
    } for p in pickings]

async def buscar_almacenes(nombre: str = ""):
    """Busca almacenes reales en Odoo para que el admin elija cuales vigilar."""
    dominio = [["name", "ilike", nombre]] if nombre else []
    almacenes = await _rpc("stock.warehouse", "search_read",
        [dominio],
        {"fields": ["id", "name", "code", "view_location_id", "lot_stock_id"], "limit": 50}
    )
    return [{
        "id":          a["id"],
        "nombre":      a["name"],
        "codigo":      a["code"],
        "location_id": a["view_location_id"][0] if a.get("view_location_id") else None,
    } for a in almacenes]

async def cargar_traspaso(picking_id: int):
    picks = await _rpc("stock.picking", "search_read",
        [[["id", "=", picking_id]]],
        {"fields": ["name", "location_id", "location_dest_id", "move_ids"]}
    )
    if not picks:
        raise Exception("Traspaso no encontrado")
    p = picks[0]
    moves = await _rpc("stock.move", "search_read",
        [[["id", "in", p["move_ids"]]]],
        {"fields": ["product_id", "product_uom_qty", "name"]}
    )
    origen  = p["location_id"][1] if p.get("location_id") else ""
    destino = p["location_dest_id"][1] if p.get("location_dest_id") else ""

    productos = []
    for m in moves:
        if m["product_uom_qty"] <= 0:
            continue
        nombre = m["product_id"][1] if m.get("product_id") else m["name"]
        match = re.match(r"^\[([^\]]+)\]", nombre)
        clave = match.group(1).strip() if match else nombre.split(" ")[0]
        desc  = nombre.replace(match.group(0), "").strip() if match else nombre
        productos.append({
            "clave": clave, "descripcion": desc,
            "cantidad_total": round(m["product_uom_qty"]), "unidad": "PZA"
        })

    folio_limpio = re.sub(r"[^A-Z0-9]+", "-", p["name"].upper()).strip("-")
    return {
        "num_entrega":     f"TRASPASO-{folio_limpio}",
        "orden":           p["name"],
        "nombre_cliente":  f"TRASPASO A {destino}",
        "direccion":       f"Origen: {origen}  ->  Destino: {destino}",
        "sucursal":        destino,
        "comercializador": "ECOR",
        "fuente":          "odoo",
        "productos":       productos
    }
