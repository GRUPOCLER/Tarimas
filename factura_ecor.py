import re, time

# ============================================================
#  PARSER — FACTURA CFDI ECOR
#  Documento fiscal (no nota de entrega). Trae el campo "Origen"
#  con el numero de OV de Odoo — esto es lo que activa el candado
#  de duplicados si esa OV ya se importo directo desde Odoo.
# ============================================================

RE_ORIGEN           = re.compile(r'Origen:\s*(\S+)')
RE_FOLIO            = re.compile(r'Folio\s+(INV/\d+/\d+)')
RE_FECHA            = re.compile(r'(\d{2})/(\d{2})/(\d{4})')
RE_RFC_RECEPTOR     = re.compile(r'RFC receptor:\s*(\S+)')
RE_NOMBRE_RECEPTOR  = re.compile(r'Nombre receptor:\s*(.+?)\s+No\. de serie')
RE_DIRECCION        = re.compile(r'Direcci[oó]n:\s*(.+?)C[oó]digo postal del receptor', re.S)
RE_PRODUCTO         = re.compile(r'^(\d{6,10})\s+([A-Z0-9\-]{2,15})\s+([\d,]+\.\d{2,6})\s+H87\s+Unidades\s+')


def detectar_factura_ecor(texto: str) -> bool:
    t = texto.upper()
    return 'EQUIPOS COREANOS' in t and 'FOLIO FISCAL' in t and 'CONCEPTOS' in t


def parsear_factura_ecor(texto: str) -> dict:
    lineas = texto.split('\n')

    m_origen = RE_ORIGEN.search(texto)
    orden = m_origen.group(1).strip() if m_origen else ''

    m_folio = RE_FOLIO.search(texto)
    folio = m_folio.group(1).replace('/', '-') if m_folio else f"FAC-{int(time.time())}"

    m_fecha = RE_FECHA.search(texto)
    fecha = f"{m_fecha.group(3)}-{m_fecha.group(2)}-{m_fecha.group(1)}" if m_fecha else ''

    m_rfc = RE_RFC_RECEPTOR.search(texto)
    rfc_cliente = m_rfc.group(1).strip() if m_rfc else ''

    m_nombre = RE_NOMBRE_RECEPTOR.search(texto)
    nombre_cliente = m_nombre.group(1).strip() if m_nombre else ''

    m_dir = RE_DIRECCION.search(texto)
    direccion = re.sub(r'\s+', ' ', m_dir.group(1)).strip() if m_dir else ''
    direccion = re.sub(r'C[oó]digo postal,?\s*fecha y hora\s*\d+.*?\d{2}:\d{2}:\d{2}', '', direccion)
    direccion = re.sub(r'de emisi[oó]n:\s*', '', direccion)
    direccion = re.sub(r'\s{2,}', ' ', direccion).strip(' ,')

    productos = []
    for i, linea in enumerate(lineas):
        mp = RE_PRODUCTO.match(linea)
        if not mp:
            continue
        sku = mp.group(2)
        cantidad = float(mp.group(3).replace(',', ''))
        if cantidad <= 0:
            continue

        descripcion = ''
        if i + 1 < len(lineas) and 'Descripci' in lineas[i + 1]:
            md = re.search(r'\[' + re.escape(sku) + r'\]\s*(.+?)\s+Impuesto\s+Tipo\s+Base', lineas[i + 1])
            if md:
                descripcion = md.group(1).strip()
            if i + 2 < len(lineas):
                mc = re.match(r'^(.+?)\s+Traslado\b', lineas[i + 2])
                if mc:
                    cont = mc.group(1).strip()
                    if cont and not re.match(r'^[\d.,]+$', cont):
                        descripcion = (descripcion + ' ' + cont).strip()

        productos.append({
            'clave':          sku,
            'descripcion':    (descripcion or sku)[:150],
            'cantidad_total': int(round(cantidad)),
            'unidad':         'PZA'
        })

    return {
        'num_entrega':     folio,
        'nombre_cliente':  nombre_cliente,
        'rfc_cliente':     rfc_cliente,
        'direccion':       direccion,
        'orden':           orden,
        'fecha_entrega':   fecha,
        'comercializador': 'ECOR',
        'sucursal':        '',
        'productos':       productos
    }
