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
        {"fields": ["name", "partner_id", "state", "picking_ids", "date_order", "warehouse_id"], "order": "date_order desc", "limit": 5000}
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
        {"fields": ["product_id", "product_uom_qty", "quantity"]}
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
        nombre = m["product_id"][1] if m.get("product_id") else (m.get("name") or "")
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
        {"fields": ["product_id", "product_uom_qty"]}
    )
    origen  = p["location_id"][1] if p.get("location_id") else ""
    destino = p["location_dest_id"][1] if p.get("location_dest_id") else ""

    productos = []
    for m in moves:
        if m["product_uom_qty"] <= 0:
            continue
        nombre = m["product_id"][1] if m.get("product_id") else (m.get("name") or "")
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

# ── PRODUCTOS COMERCIALIZABLES (para asignar ubicacion en el almacen) ──
async def buscar_productos_venta(termino: str = "", limite: int = 50):
    """Busca productos que se pueden vender (sale_ok=True) en Odoo, para
    poder asignarles despues una ubicacion fisica en el mapa del CEDIS.
    Solo se muestran productos con la etiqueta "Maquinaria" (product_tag_ids),
    ya que este almacen es especificamente para ese tipo de producto."""
    dominio = [["sale_ok", "=", True], ["product_tag_ids.name", "=", "Maquinaria"]]
    if termino:
        t = termino.strip()
        dominio.append("|")
        dominio.append(["default_code", "ilike", t])
        dominio.append(["name", "ilike", t])
    productos = await _rpc("product.product", "search_read",
        [dominio],
        {"fields": ["default_code", "name", "qty_available"], "limit": limite, "order": "name"}
    )
    return [{
        "clave":           p.get("default_code") or "",
        "nombre":          p.get("name") or "",
        "existencia_odoo": p.get("qty_available", 0),
    } for p in productos if p.get("default_code")]


# ── DATOS DE PRODUCTO PARA EL CATALOGO (solo lectura) ──────────────────────
# Odoo cambia nombres de campos y modelos entre versiones (v17 -> v19), asi que se consulta con fields_get qué
# existe realmente en esta base y se lee solo eso. Nada se escribe en Odoo.
_fields_cache = {}


async def _fields(model: str) -> dict:
    if model in _fields_cache:
        return _fields_cache[model]
    try:
        r = await _rpc(model, "fields_get", [], {"attributes": ["type", "string", "relation"]})
    except Exception:
        r = {}
    _fields_cache[model] = r or {}
    return _fields_cache[model]


def reset_cache_campos():
    _fields_cache.clear()


async def _modelo_existe(nombre: str) -> bool:
    try:
        r = await _rpc("ir.model", "search_read", [[["model", "=", nombre]]], {"fields": ["model"], "limit": 1})
        return bool(r)
    except Exception:
        return False


# unidad de longitud -> factor para pasar a cm
_A_CM = {"mm": 0.1, "millimeter": 0.1, "millimeters": 0.1, "cm": 1.0, "centimeter": 1.0, "centimeters": 1.0, "m": 100.0,
         "meter": 100.0, "meters": 100.0, "in": 2.54, "inch": 2.54, "inches": 2.54, "ft": 30.48, "foot": 30.48, "feet": 30.48}


def _factor_cm(nombre) -> float:
    n = str(nombre or "").strip().lower()
    return _A_CM.get(n, 1.0)


_DIM_PROD = ("product_length", "product_width", "product_height")      # medidas en el producto (v18+) [VERIFICAR en v19]
_DIM_PAQ = ("packaging_length", "width", "height")                       # medidas del tipo de paquete


async def diagnostico_catalogo(muestra: str = "") -> dict:
    """Qué campos y modelos relacionados con peso / medidas / empaques existen en ESTE Odoo."""
    pf = await _fields("product.product")
    campos_prod = {c: (c in pf) for c in ("weight", "volume", *_DIM_PROD, "dimensions_uom_id", "packaging_ids", "uom_ids")}
    modelos = {m: await _modelo_existe(m) for m in ("product.packaging", "product.uom", "stock.package.type", "uom.uom")}
    out = {"campos_producto": campos_prod, "modelos": modelos}
    if modelos["product.packaging"]:
        qf = await _fields("product.packaging")
        out["campos_empaque"] = sorted(qf.keys())
    if modelos["stock.package.type"]:
        tf = await _fields("stock.package.type")
        out["campos_tipo_paquete"] = sorted(c for c in tf if any(x in c for x in ("length", "width", "height", "weight", "uom", "name")))
    if modelos["product.uom"]:
        uf = await _fields("product.uom")
        out["campos_product_uom"] = sorted(uf.keys())
    if muestra.strip():
        d = await leer_catalogo_odoo([muestra.strip().upper()])
        out["muestra"] = d.get(muestra.strip().upper())
    return out


async def leer_catalogo_odoo(skus: list) -> dict:
    """Devuelve {sku: {encontrado, odoo_id, peso_unit, m3_unit, lr_unit, an_unit, al_unit, cm_cant, cm_lr, cm_an, cm_al, empaques, ...}}
    con medidas en cm, peso en kg y volumen en m3. Lo que Odoo no tiene viene en 0."""
    skus = [s for s in dict.fromkeys(x.strip().upper() for x in skus if x) if s]
    if not skus:
        return {}
    pf = await _fields("product.product")
    campos = ["default_code", "name", "weight", "volume"] + [c for c in _DIM_PROD if c in pf]
    if "dimensions_uom_id" in pf:
        campos.append("dimensions_uom_id")
    if "packaging_ids" in pf:
        campos.append("packaging_ids")
    # Odoo compara default_code sin distinguir si esta en mayusculas; se pide con ilike exacto via "in" sobre el valor original
    productos = await _rpc("product.product", "search_read",
                           [[["default_code", "in", skus + [s.lower() for s in skus]]]],
                           {"fields": campos, "context": {"active_test": False}, "limit": len(skus) * 3})
    # factor de unidad de medidas del producto
    uom_ids = {p["dimensions_uom_id"][0] for p in productos if p.get("dimensions_uom_id")}
    fact_uom = {}
    if uom_ids:
        try:
            for u in await _rpc("uom.uom", "read", [list(uom_ids)], {"fields": ["name"]}):
                fact_uom[u["id"]] = _factor_cm(u.get("name"))
        except Exception:
            pass

    # empaques
    paq_por_prod = {}
    if await _modelo_existe("product.packaging"):
        qf = await _fields("product.packaging")
        ids = [i for p in productos for i in (p.get("packaging_ids") or [])]
        if ids:
            cq = [c for c in ("name", "qty", "product_id", "package_type_id") if c in qf]
            paqs = await _rpc("product.packaging", "read", [ids], {"fields": cq, "context": {"active_test": False}})
            tipo_ids = {q["package_type_id"][0] for q in paqs if q.get("package_type_id")}
            tipos = {}
            if tipo_ids and await _modelo_existe("stock.package.type"):
                tf = await _fields("stock.package.type")
                ct = ["name"] + [c for c in _DIM_PAQ if c in tf] + [c for c in ("length_uom_name", "max_weight") if c in tf]
                for t in await _rpc("stock.package.type", "read", [list(tipo_ids)], {"fields": ct}):
                    tipos[t["id"]] = t
            for q in paqs:
                t = tipos.get((q.get("package_type_id") or [None])[0], {}) if q.get("package_type_id") else {}
                f = _factor_cm(t.get("length_uom_name"))
                paq_por_prod.setdefault(q["product_id"][0], []).append({
                    "id": q["id"], "nombre": q.get("name") or "", "qty": float(q.get("qty") or 0),
                    "tipo": (t.get("name") if t else None),
                    "lr": round((t.get("packaging_length") or 0) * f, 3), "an": round((t.get("width") or 0) * f, 3), "al": round((t.get("height") or 0) * f, 3),
                })

    marca = os.getenv("ODOO_EMPAQUE_MASTER", "master").strip().lower()
    out = {}
    for p in productos:
        sku = (p.get("default_code") or "").strip().upper()
        if not sku or sku in out:
            continue
        f = fact_uom.get((p.get("dimensions_uom_id") or [None])[0], 1.0)
        d = {"encontrado": True, "odoo_id": p["id"], "nombre": p.get("name") or "",
             "peso_unit": float(p.get("weight") or 0), "m3_unit": float(p.get("volume") or 0),
             "lr_unit": round(float(p.get("product_length") or 0) * f, 3), "an_unit": round(float(p.get("product_width") or 0) * f, 3),
             "al_unit": round(float(p.get("product_height") or 0) * f, 3),
             "cm_cant": 0, "cm_lr": 0, "cm_an": 0, "cm_al": 0,
             "dim_factor": f, "tiene_campos_dim": any(c in pf for c in _DIM_PROD)}
        paqs = sorted([q for q in paq_por_prod.get(p["id"], []) if q["qty"] > 1], key=lambda q: q["qty"])
        d["empaques"] = paq_por_prod.get(p["id"], [])
        elegido = None
        por_nombre = [q for q in paqs if marca and marca in (q["nombre"] or "").lower()]
        if por_nombre:
            elegido = por_nombre[0]
        elif paqs:
            elegido = paqs[0]
            d["empaque_ambiguo"] = len(paqs) > 1
        if elegido:
            d.update({"cm_cant": int(round(elegido["qty"])), "cm_lr": elegido["lr"], "cm_an": elegido["an"], "cm_al": elegido["al"], "empaque_usado": elegido["nombre"]})
        out[sku] = d
    for s in skus:
        out.setdefault(s, {"encontrado": False})
    return out
