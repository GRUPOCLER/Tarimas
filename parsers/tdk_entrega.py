import re, time

# ============================================================
#  PARSER — NOTA DE ENTREGA TDK INTERNATIONAL
#
#  AVISO IMPORTANTE: en este formato, cuando la descripcion del
#  producto es larga y se envuelve a 2 lineas, el generador del
#  reporte (SAP/Crystal Reports) posiciona el renglon de
#  Cantidad/Unidad/Precio/Impuesto ENCIMA de esa segunda linea
#  de texto — quedan literalmente sobrepuestos en el PDF, no es
#  un problema de esta extraccion. Por eso:
#    - La DESCRIPCION se extrae solo hasta donde el texto viene
#      limpio (antes de la zona de sobreposicion); puede quedar
#      truncada en documentos con descripciones largas.
#    - La CANTIDAD se obtiene de la tabla de Lote/Serie (que si
#      sale limpia), no de la fila principal del producto.
#  Si un documento de este tipo trae MAS de un producto o
#  cantidades mayores a 1, hay que volver a validar el patron.
# ============================================================

RE_FOLIO_FECHA = re.compile(r'\b(\d{2,6})\s+(\d{2}/\d{2}/\d{4})\s+\d+/\d+')
RE_RFC         = re.compile(r'\b([A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3})\b')
RE_ALMACEN     = re.compile(r'Almac[eé]n:\s*(.+)')
RE_ITEM_CODE   = re.compile(r'Item Code:\s*(\S+)')
RE_COMENTARIOS = re.compile(r'Basado en[^\n]*')
RE_FECHA_DOC   = re.compile(r'Fecha de\s+(\d{2}/\d{2}/\d{4})')
RE_LOTE_FILA   = re.compile(r'#\s*Lote.*?\n.*?\n(\d+)\s+([\d\s]+?)\s+(\d+)\s*\n', re.S)
RE_DESC_LIMPIA = re.compile(r'^\d{3}\s+([A-Z0-9ÁÉÍÓÚÑ/%.\- ]+?)(?=\s+[A-Z]\s+/\d|\s+[A-Z]{1,2}\s+\d)', re.M)


def detectar_tdk_entrega(texto: str) -> bool:
    t = texto.upper()
    return 'TDK INTERNATIONAL' in t and 'NOTA DE' in t and 'ENTREGA' in t


def parsear_tdk_entrega(texto: str) -> dict:
    m_ff = RE_FOLIO_FECHA.search(texto)
    folio = m_ff.group(1) if m_ff else str(int(time.time()))
    fecha_raw = m_ff.group(2) if m_ff else ''
    if not fecha_raw:
        m_fd = RE_FECHA_DOC.search(texto)
        fecha_raw = m_fd.group(1) if m_fd else ''
    if fecha_raw:
        dd, mm, yyyy = fecha_raw.split('/')
        fecha = f"{yyyy}-{mm}-{dd}"
    else:
        fecha = ''

    m_rfc = RE_RFC.search(texto)
    rfc_cliente = m_rfc.group(1) if m_rfc else ''

    # Nombre y direccion del contacto que recibe (aparecen justo antes de
    # "Su contacto" en el texto)
    m_nombre = re.search(r'\n([A-ZÑÁÉÍÓÚ ]{4,50})\n(AV |CALLE |BLVD |PROL )', texto)
    nombre_cliente = m_nombre.group(1).strip() if m_nombre else ''

    m_dir = re.search(r'\n((?:AV|CALLE|BLVD|PROL)[^\n]+\n[^\n]+)\nSu contacto', texto)
    direccion = re.sub(r'\s+', ' ', m_dir.group(1)).strip() if m_dir else ''

    m_alm = RE_ALMACEN.search(texto)
    almacen = m_alm.group(1).strip() if m_alm else ''

    m_item = RE_ITEM_CODE.search(texto)
    clave = m_item.group(1).strip() if m_item else ''

    m_com = RE_COMENTARIOS.search(texto)
    comentario = re.sub(r'\s+', ' ', m_com.group(0)).strip() if m_com else ''

    # Numero de referencia para el campo "orden" — mejor un numero corto y
    # buscable (Pedido de cliente / Oferta de venta) que el comentario entero
    m_pedido = re.search(r'Pedidos? de cliente\s+(\d+)', comentario, re.I)
    m_oferta = re.search(r'Ofertas? de ventas?\s+(\d+)', comentario, re.I)
    orden = m_pedido.group(1) if m_pedido else (m_oferta.group(1) if m_oferta else folio)

    # Descripcion: solo la parte que sale limpia (ver aviso arriba)
    m_desc = RE_DESC_LIMPIA.search(texto)
    descripcion = re.sub(r'\s+', ' ', m_desc.group(1)).strip() if m_desc else clave

    # Cantidad real desde la tabla de lote (esa si sale limpia)
    m_lote = RE_LOTE_FILA.search(texto)
    cantidad = int(m_lote.group(3)) if m_lote else 1

    productos = []
    if clave:
        productos.append({
            'clave':          clave,
            'descripcion':    descripcion[:150],
            'cantidad_total': cantidad,
            'unidad':         'PZA'
        })

    return {
        'num_entrega':     f"TDK-{folio}",
        'nombre_cliente':  nombre_cliente,
        'rfc_cliente':     rfc_cliente,
        'direccion':       direccion,
        'orden':           orden,
        'fecha_entrega':   fecha,
        'comercializador': 'TDK',
        'sucursal':        almacen,
        'productos':       productos
    }
