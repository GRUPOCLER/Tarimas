import os, re, httpx
from datetime import datetime, timedelta
from parsers.sap_raiker import SUCURSALES as SUCURSALES_RAIKER

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

async def listar_ovs_pendientes(warehouse_ids: list = None):
    fecha_limite = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    dominio = [["picking_ids", "!=", False], ["state", "in", ["sale", "done"]], ["date_order", ">=", fecha_limite]]
    if warehouse_ids:
        dominio.append(["warehouse_id", "in", warehouse_ids])
    ovs = await _rpc("sale.order", "search_read",
        [dominio],
        {"fields": ["name", "partner_id", "state", "picking_ids", "date_order", "warehouse_id"], "order": "id desc", "limit": 1500}
    )
    return [{
        "num_ov": ov["name"],
        "cliente": ov["partner_id"][1] if ov.get("partner_id") else "",
        "comercializador": PARTNER_MAP.get(ov["partner_id"][0] if ov.get("partner_id") else None, ""),
        "fecha": (ov.get("date_order") or "")[:10],
        "almacen": ov["warehouse_id"][1] if ov.get("warehouse_id") else "",
        "picking_ids": ov["picking_ids"]
    } for ov in ovs]

def _detectar_sucursal_raiker(nombre_contacto: str) -> str:
    """Detecta la sucursal Raiker (Boca, Boticaria, Tejeria, etc.) a partir
    del nombre del contacto de envio en Odoo. El contacto normalmente viene
    formateado como 'AGROINDUSTRIAS RAIKER, BOCA' — el nombre de la
    sucursal despues de la coma."""
    if not nombre_contacto:
        return ""
    texto = nombre_contacto.upper()
    if "," in texto:
        candidato = texto.split(",")[-1].strip()
        for s in SUCURSALES_RAIKER:
            if candidato == s or candidato.startswith(s):
                return s
    # Respaldo: busca cualquier sucursal conocida mencionada en el nombre completo
    for s in SUCURSALES_RAIKER:
        if s in texto:
            return s
    return ""


async def cargar_entrega(picking_ids: list):
    picks = await _rpc("stock.picking", "search_read",
        [[["id", "in", picking_ids]]],
        {"fields": ["name", "partner_id", "sale_id", "move_ids", "state"]}
    )
    if not picks:
        raise Exception("La OV no tiene entregas")
    p = picks[0]
    validada = p.get("state") == "done"
    moves = await _rpc("stock.move", "search_read",
        [[["id", "in", p["move_ids"]]]],
        {"fields": ["product_id", "product_uom_qty", "quantity", "name"]}
    )
    productos = []
    for m in moves:
        # Si la entrega ya esta validada ("Hecho"), usamos la cantidad REAL
        # que se surtio (campo "quantity") — no lo que se pidio originalmente
        # ("product_uom_qty"/Demanda). Asi los productos que no se pudieron
        # surtir (faltante de stock, etc.) no salen en las etiquetas.
        # Si todavia no esta validada, no hay cantidad real aun, usamos la
        # demanda como respaldo (comportamiento anterior).
        cantidad = m.get("quantity", 0) if validada else m.get("product_uom_qty", 0)
        if cantidad <= 0:
            continue
        nombre = m["product_id"][1] if m.get("product_id") else m["name"]
        match = re.match(r"^\[([^\]]+)\]", nombre)
        clave = match.group(1).strip() if match else nombre.split(" ")[0]
        desc  = nombre.replace(match.group(0), "").strip() if match else nombre
        productos.append({
            "clave": clave, "descripcion": desc,
            "cantidad_total": round(cantidad), "unidad": "PZA"
        })

    # Direccion de entrega real: viene del campo "partner_shipping_id" de la
    # orden de venta (Odoo estandar), no del cliente general de la OV.
    direccion = p["partner_id"][1] if p.get("partner_id") else ""
    sucursal_detectada = ""
    if p.get("sale_id"):
        try:
            ov = await _rpc("sale.order", "search_read",
                [[["id", "=", p["sale_id"][0]]]],
                {"fields": ["partner_shipping_id"]}
            )
            if ov and ov[0].get("partner_shipping_id"):
                id_envio = ov[0]["partner_shipping_id"][0]
                contactos = await _rpc("res.partner", "search_read",
                    [[["id", "=", id_envio]]],
                    {"fields": ["name", "street", "street2", "city", "state_id", "zip", "country_id"]}
                )
                if contactos:
                    c = contactos[0]
                    partes = [
                        c.get("street"), c.get("street2"), c.get("city"),
                        c["state_id"][1] if c.get("state_id") else None,
                        c.get("zip"),
                        c["country_id"][1] if c.get("country_id") else None,
                    ]
                    direccion_armada = ", ".join(x for x in partes if x)
                    if direccion_armada:
                        direccion = direccion_armada
                    sucursal_detectada = _detectar_sucursal_raiker(c.get("name", ""))
        except Exception:
            pass  # si algo falla, se queda con el nombre del cliente como respaldo

    return {
        "num_entrega": p["name"],
        "orden": p["sale_id"][1] if p.get("sale_id") else "",
        "nombre_cliente": p["partner_id"][1] if p.get("partner_id") else "",
        "direccion": direccion,
        "sucursal": sucursal_detectada,
        "comercializador": PARTNER_MAP.get(p["partner_id"][0] if p.get("partner_id") else None, ""),
        "fuente": "odoo",
        "productos": productos
    }

# ── TRASPASOS INTERNOS (CEDIS, FULL MELI, Eventos y Expo) ────────
def _frag(x):
    """Un 'fragmento' de dominio de Odoo: si ya es un compuesto (empieza con
    &/|/!) se pasa tal cual; si es una sola condicion, ocupa un solo lugar."""
    if isinstance(x[0], str) and x[0] in ("&", "|", "!"):
        return x
    return [x]

def _and(*partes):
    slots = []
    for p in partes:
        slots += _frag(p)
    return ["&"] * (len(partes) - 1) + slots

def _or(*partes):
    slots = []
    for p in partes:
        slots += _frag(p)
    return ["|"] * (len(partes) - 1) + slots

async def listar_traspasos_pendientes(destinos: list = None, origenes: list = None):
    # Compatibilidad: si nadie pasa nada, usa el valor viejo por variable de entorno
    if destinos is None and origenes is None:
        destinos = DESTINOS_TRASPASO
    destinos = destinos or []
    origenes = origenes or []
    if not destinos and not origenes:
        return []

    fecha_limite = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    leaf_estado = ["state", "not in", ["done", "cancel"]]
    leaf_fecha  = ["scheduled_date", ">=", fecha_limite]

    rama_destino = _and(["location_dest_id", "child_of", destinos], ["location_id.usage", "in", ["internal", "transit"]])
    rama_origen  = _and(["location_id", "child_of", origenes], ["location_dest_id.usage", "in", ["internal", "transit"]])

    if destinos and origenes:
        dominio = _and(leaf_estado, leaf_fecha, _or(rama_destino, rama_origen))
    elif destinos:
        dominio = _and(leaf_estado, leaf_fecha, rama_destino)
    else:
        dominio = _and(leaf_estado, leaf_fecha, rama_origen)

    pickings = await _rpc("stock.picking", "search_read",
        [dominio],
        {
            "fields": ["name", "location_id", "location_dest_id", "state", "origin", "move_ids", "scheduled_date"],
            "order": "id desc", "limit": 400
        }
    )

    # Cuando el traslado es de varios pasos, el "OUT" que encontramos apunta
    # a una ubicacion de transito generica (ej. "Warehouse Transfer"), no al
    # almacen destino real. Ese destino real esta en el registro hermano
    # ".../IN/#####" que comparte la misma referencia ("origin"). Lo buscamos.
    origenes_ref = list(set(p["origin"] for p in pickings if p.get("origin") and "/OUT/" in p.get("name", "")))
    destinos_reales = {}
    if origenes_ref:
        hermanos = await _rpc("stock.picking", "search_read",
            [[["origin", "in", origenes_ref], ["name", "like", "/IN/"]]],
            {"fields": ["origin", "location_dest_id"]}
        )
        for h in hermanos:
            if h.get("origin") and h.get("location_dest_id"):
                destinos_reales[h["origin"]] = h["location_dest_id"][1]

    return [{
        "id":         p["id"],
        "folio":      p["name"],
        "origen":     p["location_id"][1] if p.get("location_id") else "",
        "destino":    destinos_reales.get(p.get("origin")) or (
            f"{p['location_dest_id'][1]} (tentativo, sin confirmar)" if p.get("location_dest_id") else "(sin destino)"
        ),
        "estado":     p["state"],
        "referencia": p.get("origin") or "",
        "fecha":      (p.get("scheduled_date") or "")[:10],
        "move_ids":   p["move_ids"]
    } for p in pickings]

async def diag_traspasos(ubicaciones: list = None):
    """Sin filtro de tipo de operacion ni de estatus — para ver que hay
    realmente en esas ubicaciones antes de decidir el filtro final."""
    ubicaciones = ubicaciones if ubicaciones is not None else DESTINOS_TRASPASO
    if not ubicaciones:
        return []
    pickings = await _rpc("stock.picking", "search_read",
        [[["location_dest_id", "child_of", ubicaciones]]],
        {
            "fields": ["name", "location_id", "location_dest_id", "state", "picking_type_id", "origin", "scheduled_date"],
            "order": "id desc", "limit": 40
        }
    )
    return [{
        "folio":         p["name"],
        "origen":        p["location_id"][1] if p.get("location_id") else "",
        "destino":       p["location_dest_id"][1] if p.get("location_dest_id") else "",
        "estado":        p["state"],
        "tipo_operacion":p["picking_type_id"][1] if p.get("picking_type_id") else "",
        "referencia":    p.get("origin") or "",
        "fecha":         (p.get("scheduled_date") or "")[:10],
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
