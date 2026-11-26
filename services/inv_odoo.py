"""Lecturas de Odoo para Inventario y Operaciones. SOLO LECTURA: aqui no hay create/write/unlink.
Port de Odoo.gs (Apps Script). Timeout largo porque las existencias son miles de quants."""
import os, re, httpx
from datetime import datetime, timezone
from services import odoo as _o

TIMEOUT = float(os.getenv("INV_ODOO_TIMEOUT", "120"))


def etiqueta_filtro() -> str:
    """Etiqueta de producto para limitar las existencias. Vacia / TODOS = sin filtro."""
    v = os.getenv("INVENTARIO_ETIQUETA", "Maquinaria")
    limpio = re.sub(r"[\"'“”\s]", "", v or "")
    if re.fullmatch(r"(todos|todas|ninguna|no|-)?", limpio, re.I):
        return ""
    return v.strip().strip("\"'“” ")


async def rpc(model, method, args, kwargs=None):
    """Mismo canal que services.odoo._rpc (reautenticacion, omision de campos que la version no tiene)."""
    return await _o._rpc(model, method, args, kwargs, timeout=TIMEOUT)


def clave_nombre(display: str):
    m = re.match(r"^\[(.+?)\]\s*(.*)$", display or "")
    return (m.group(1), m.group(2)) if m else ("", display or "")


def fecha_odoo(d: datetime) -> str:
    if d.tzinfo:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def parse_odoo(s):
    s = (s or "").strip()
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S") if s else None


def norm_sku(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").strip().upper())


def _trozos(lista, n):
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


async def _leer_paginado(model, dom, fields, lote=2000, order="id"):
    out, off = [], 0
    while True:
        pag = await rpc(model, "search_read", [dom], {"fields": fields, "limit": lote, "offset": off, "order": order})
        out += pag
        off += lote
        if len(pag) < lote:
            return out


async def existencias_odoo():
    """stock.quant internos con codigo de barras de ubicacion. [{barcode, clave, nombre, cantidad, reservado}]"""
    etiqueta = etiqueta_filtro()
    dom = [["location_id.usage", "=", "internal"], ["quantity", ">", 0], ["location_id.barcode", "!=", False]]
    if etiqueta:
        dom.append(["product_id.product_tag_ids.name", "=", etiqueta])
    quants = await _leer_paginado("stock.quant", dom, ["product_id", "location_id", "quantity", "reserved_quantity"])
    loc_ids = sorted({q["location_id"][0] for q in quants if q.get("location_id")})
    prod_ids = sorted({q["product_id"][0] for q in quants if q.get("product_id")})
    barcodes, prods = {}, {}
    for t in _trozos(loc_ids, 500):
        for l in await rpc("stock.location", "search_read", [[["id", "in", t]]], {"fields": ["barcode"]}):
            barcodes[l["id"]] = l.get("barcode")
    for t in _trozos(prod_ids, 500):
        for p in await rpc("product.product", "search_read", [[["id", "in", t]]], {"fields": ["default_code", "name"]}):
            prods[p["id"]] = (p.get("default_code") or "", p.get("name") or "")
    out = []
    for q in quants:
        bc = barcodes.get(q["location_id"][0]) if q.get("location_id") else None
        if not bc:
            continue
        p = prods.get(q["product_id"][0]) or clave_nombre(q["product_id"][1])
        out.append({"barcode": str(bc), "clave": p[0] or p[1], "nombre": p[1],
                    "cantidad": q.get("quantity") or 0, "reservado": q.get("reserved_quantity") or 0})
    return out


_CAMPOS_LINEA = ["date", "product_id", "location_id", "location_dest_id", "quantity", "picking_id", "reference"]


async def _formatear_lineas(lineas):
    """stock.move.line -> movimientos legibles (ubicacion, producto, tipo, OV, cliente)."""
    if not lineas:
        return []
    locs, prod_ids, pk_ids = set(), set(), set()
    for l in lineas:
        if l.get("location_id"): locs.add(l["location_id"][0])
        if l.get("location_dest_id"): locs.add(l["location_dest_id"][0])
        if l.get("product_id"): prod_ids.add(l["product_id"][0])
        if l.get("picking_id"): pk_ids.add(l["picking_id"][0])
    nombres, prods, pickings = {}, {}, {}
    for t in _trozos(sorted(locs), 500):
        for l in await rpc("stock.location", "search_read", [[["id", "in", t]]], {"fields": ["barcode", "complete_name"]}):
            nombres[l["id"]] = l.get("barcode") or l.get("complete_name") or str(l["id"])
    for t in _trozos(sorted(prod_ids), 500):
        for p in await rpc("product.product", "search_read", [[["id", "in", t]]], {"fields": ["default_code", "name"]}):
            prods[p["id"]] = (p.get("default_code") or "", p.get("name") or "")
    for t in _trozos(sorted(pk_ids), 500):
        dom = [["id", "in", t]]
        try:
            rows = await rpc("stock.picking", "search_read", [dom], {"fields": ["name", "origin", "picking_type_code", "sale_id", "partner_id"]})
        except Exception:
            rows = await rpc("stock.picking", "search_read", [dom], {"fields": ["name", "origin", "picking_type_code", "partner_id"]})
        for p in rows:
            pickings[p["id"]] = p
    out = []
    for l in lineas:
        p = pickings.get(l["picking_id"][0]) if l.get("picking_id") else None
        t = p.get("picking_type_code") if p else ""
        tipo = "OV" if t == "outgoing" else "traslado" if t == "internal" else "entrada" if t == "incoming" else "ajuste"
        ov = ((p.get("sale_id") or [0, ""])[1] if p and p.get("sale_id") else (p.get("origin") if p else "")) or ""
        pr = prods.get(l["product_id"][0], ("", "")) if l.get("product_id") else ("", "")
        out.append({"id": l["id"], "fecha": l.get("date"), "producto": pr[0] or pr[1], "descripcion": pr[1],
                    "cantidad": l.get("quantity") or 0,
                    "origen": nombres.get(l["location_id"][0], "") if l.get("location_id") else "",
                    "destino": nombres.get(l["location_dest_id"][0], "") if l.get("location_dest_id") else "",
                    "tipo": tipo, "picking": p["name"] if p else (l.get("reference") or ""), "ov": ov,
                    "cliente": p["partner_id"][1] if p and p.get("partner_id") else ""})
    return out


async def _lineas_hechas(dom_extra, desde: datetime, hasta: datetime = None):
    dom = [["state", "=", "done"], ["date", ">=", fecha_odoo(desde)]] + dom_extra
    if hasta:
        dom.insert(2, ["date", "<=", fecha_odoo(hasta)])
    return await _leer_paginado("stock.move.line", dom, _CAMPOS_LINEA, order="date")


async def movimientos_odoo(codigos, desde: datetime, hasta: datetime):
    """Movimientos hechos entre desde y hasta que tocan alguna de las ubicaciones (por codigo de barras)."""
    cods = sorted({c for c in codigos if c})
    if not cods:
        return []
    ids = []
    for t in _trozos(cods, 500):
        for l in await rpc("stock.location", "search_read", [[["barcode", "in", t]]], {"fields": ["barcode"]}):
            ids.append(l["id"])
    por_id = {}
    for t in _trozos(ids, 400):
        for l in await _lineas_hechas(["|", ["location_id", "in", t], ["location_dest_id", "in", t]], desde, hasta):
            por_id[l["id"]] = l
    return await _formatear_lineas(list(por_id.values()))


async def almacenes_de_ubicaciones(codigos):
    """Almacen (stock.warehouse) de cada ubicacion: {porCodigo, whIds}. Si Odoo no lo informa, 1er segmento del nombre."""
    cods = sorted({c for c in codigos if c})
    por_codigo, wh_ids = {}, {}
    for t in _trozos(cods, 500):
        dom = [[["barcode", "in", t]]]
        try:
            locs = await rpc("stock.location", "search_read", dom, {"fields": ["barcode", "warehouse_id", "complete_name"]})
            for l in locs:
                if l.get("warehouse_id"):
                    por_codigo[l["barcode"]] = l["warehouse_id"][1]
                    wh_ids[l["warehouse_id"][0]] = l["warehouse_id"][1]
                else:
                    por_codigo[l["barcode"]] = (l.get("complete_name") or "").split("/")[0] or None
        except Exception:
            for l in await rpc("stock.location", "search_read", dom, {"fields": ["barcode", "complete_name"]}):
                por_codigo[l["barcode"]] = (l.get("complete_name") or "").split("/")[0] or None
    return {"porCodigo": por_codigo, "whIds": wh_ids}


async def salidas_almacen(wh_ids, productos, desde, hasta, excluir_codigos):
    """Movimientos hechos que SALEN de ubicaciones internas de esos almacenes y que NO estan en el conteo (mal surtido)."""
    prods = sorted({p for p in productos if p})
    out = []
    if not wh_ids or not prods:
        return out
    excl = []
    for t in _trozos(sorted({c for c in excluir_codigos if c}), 500):
        for l in await rpc("stock.location", "search_read", [[["barcode", "in", t]]], {"fields": ["id"]}):
            excl.append(l["id"])
    for wid, wnombre in wh_ids.items():
        por_id = {}
        for t in _trozos(prods, 200):
            extra = [["location_id.warehouse_id", "=", int(wid)], ["location_id.usage", "=", "internal"], ["product_id.default_code", "in", t]]
            if excl:
                extra.append(["location_id", "not in", excl[:5000]])
            for l in await _lineas_hechas(extra, desde, hasta):
                por_id[l["id"]] = l
        for m in await _formatear_lineas(list(por_id.values())):
            m["almacen"] = wnombre
            out.append(m)
    return out


async def salidas_pendientes(claves, almacenes=None):
    """Salidas (outgoing) aun no hechas: {CLAVE_NORM: {demanda, reservado, docs}}. Odoo 17+: stock.move.quantity = reservado."""
    out = {}
    lista = sorted({c for c in claves if c})
    for t in _trozos(lista, 300):
        dom = [["picking_type_id.code", "=", "outgoing"], ["state", "in", ["confirmed", "waiting", "partially_available", "assigned"]],
               ["product_id.default_code", "in", t]]
        if almacenes:
            dom.append(["picking_type_id.warehouse_id.name", "in", almacenes])
        for m in await rpc("stock.move", "search_read", [dom], {"fields": ["product_id", "product_uom_qty", "quantity", "state", "picking_id", "reference"]}):
            cd = clave_nombre(m["product_id"][1] if m.get("product_id") else "")[0]
            if not cd:
                continue
            o = out.setdefault(norm_sku(cd), {"demanda": 0, "reservado": 0, "docs": []})
            d, r = m.get("product_uom_qty") or 0, m.get("quantity") or 0
            o["demanda"] += d
            o["reservado"] += r
            o["docs"].append({"doc": (m["picking_id"][1] if m.get("picking_id") else "") or m.get("reference") or "", "estado": m.get("state"), "demanda": d, "reservado": r})
    return out


async def salidas_hechas(codigos, claves, dias=15):
    """Para SOBRANTES: movimientos hechos en los ultimos dias que SALEN de las ubicaciones dadas hacia fuera de ellas."""
    cods = sorted({c for c in codigos if c})
    prods = sorted({c for c in claves if c})
    out = {}
    if not cods or not prods:
        return out
    from datetime import timedelta
    desde = datetime.utcnow() - timedelta(days=dias or 15)
    por_id, conjunto = {}, set(cods)
    for tc in _trozos(cods, 500):
        for tp in _trozos(prods, 200):
            for l in await _lineas_hechas([["location_id.barcode", "in", tc], ["product_id.default_code", "in", tp]], desde):
                por_id[l["id"]] = l
    for m in await _formatear_lineas(list(por_id.values())):
        if m["destino"] in conjunto:
            continue
        o = out.setdefault(norm_sku(m["producto"]), {"cantidad": 0, "docs": []})
        o["cantidad"] += m["cantidad"]
        o["docs"].append({"doc": m["picking"], "tipo": m["tipo"], "fecha": m["fecha"], "origen": m["origen"], "destino": m["destino"], "cantidad": m["cantidad"]})
    return out


async def almacenes_odoo():
    return [{"codigo": w.get("code") or "", "nombre": w.get("name") or ""}
            for w in await rpc("stock.warehouse", "search_read", [[]], {"fields": ["name", "code"]})]


async def costos_odoo(claves):
    """standard_price por clave (default_code; si no, codigo de barras). {clave: costo}"""
    encontrados = {}
    for t in _trozos(list(claves), 300):
        for p in await rpc("product.product", "search_read", [[["default_code", "in", t]]], {"fields": ["default_code", "standard_price"], "context": {"active_test": False}}):
            k, c = (p.get("default_code") or "").strip(), p.get("standard_price") or 0
            if not k or (encontrados.get(k, 0) > 0):
                continue
            encontrados[k] = c
    resto = [k for k in claves if k not in encontrados]
    for t in _trozos(resto, 300):
        for p in await rpc("product.product", "search_read", [[["barcode", "in", t]]], {"fields": ["barcode", "standard_price"], "context": {"active_test": False}}):
            k = (p.get("barcode") or "").strip()
            if k and not (encontrados.get(k, 0) > 0):
                encontrados[k] = p.get("standard_price") or 0
    return encontrados
