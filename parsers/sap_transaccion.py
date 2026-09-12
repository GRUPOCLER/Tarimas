import re, time

# ============================================================
#  PARSER — SAP BUSINESS ONE: TRANSACCION ENTRE ALMACENES
#  Distinto del parser SAP_RAIKER existente (ese usa el formato
#  "Solicitud de traslado" con ALMACEN DE SALIDA/ENTRADA). Este
#  usa el formato de reporte "Numero de transaccion entre
#  almacenes", con columna "A almacen" por renglon.
#
#  OJO: la descripcion del producto sale partida en 2 lineas por
#  pdfplumber, con el renglon de datos (clave/almacen/cantidad)
#  metido en medio — asi es como SAP B1 imprime celdas que
#  envuelven texto. Ejemplo real:
#    DESMALEZADORA RAIKER 52CC
#    1 RKD52 CLES PZA 4 MXP 633.8085 4
#    MOTOR 2.5 HP
# ============================================================

RE_FOLIO   = re.compile(r'Número de transacci[oó]n entre almacenes\s+(\d+)', re.I)
RE_FECHA   = re.compile(r'Fecha\s+(\d{1,2})/(\d{2})/(\d{4})')
RE_ORIGEN  = re.compile(r'De almac[eé]n\s+([A-Z0-9\-]+)', re.I)
RE_COMENT  = re.compile(r'Comentarios\s+(.+?)(?:\n|$)')
RE_FILA    = re.compile(
    r'^(\d+)\s+([A-Z0-9\-]+)\s+(.*?)\s*([A-Z][A-Z0-9\-]{1,14})\s*'
    r'(PZA|PZAS|PZ|KG|LTS?|UND|CAJA)\s+([\d,]+(?:\.\d+)?)\s*MXP\s+([\d,]+\.\d+)\s+([\d,]+(?:\.\d+)?)\s*$'
)

STOP_LINEA = ('REPRESENTANTE', 'COMENTARIOS', 'PÁGINA', 'PAGINA', 'DE ALMACÉN', 'DE ALMACEN',
              'FECHA', 'HORA', 'NÚMERO', 'NUMERO', '#', 'UNIDAD', 'CTD')


def detectar_sap_transaccion(texto: str) -> bool:
    t = texto.upper()
    return 'TRANSACCIÓN ENTRE ALMACENES' in t or 'TRANSACCION ENTRE ALMACENES' in t


def _es_stop(linea: str) -> bool:
    l = linea.strip().upper()
    return (not l) or any(l.startswith(s) for s in STOP_LINEA)


def parsear_sap_transaccion(texto: str) -> dict:
    lineas = [l.strip() for l in texto.split('\n')]
    lineas_no_vacias = [l for l in lineas if l.strip()]

    m_folio = RE_FOLIO.search(texto)
    folio = m_folio.group(1) if m_folio else str(int(time.time()))

    m_fecha = RE_FECHA.search(texto)
    fecha = f"{m_fecha.group(3)}-{m_fecha.group(2)}-{m_fecha.group(1)}" if m_fecha else ''

    m_origen = RE_ORIGEN.search(texto)
    almacen_origen = m_origen.group(1).strip() if m_origen else ''

    m_com = RE_COMENT.search(texto)
    comentario = re.sub(r'\s+', ' ', m_com.group(1)).strip() if m_com else ''

    productos = []
    almacenes_destino = []
    for i, linea in enumerate(lineas_no_vacias):
        m = RE_FILA.match(linea)
        if not m:
            continue
        clave      = m.group(2).strip()
        desc_medio = m.group(3).strip()  # texto de descripcion metido en medio del renglon (si lo hay)
        almacen    = m.group(4).strip()
        cantidad   = float(m.group(6).replace(',', ''))
        if cantidad <= 0:
            continue
        almacenes_destino.append(almacen)

        # Descripcion: linea de arriba + texto de en medio (si lo hay) + linea de abajo
        partes = []
        if i - 1 >= 0 and not _es_stop(lineas_no_vacias[i - 1]) and not RE_FILA.match(lineas_no_vacias[i - 1]):
            partes.append(lineas_no_vacias[i - 1])
        if desc_medio:
            partes.append(desc_medio)
        if i + 1 < len(lineas_no_vacias) and not _es_stop(lineas_no_vacias[i + 1]) and not RE_FILA.match(lineas_no_vacias[i + 1]):
            partes.append(lineas_no_vacias[i + 1])
        descripcion = re.sub(r'\s+', ' ', ' '.join(partes)).strip() or clave

        productos.append({
            'clave':          clave,
            'descripcion':    descripcion[:150],
            'cantidad_total': int(round(cantidad)),
            'unidad':         'PZA'
        })

    almacen_destino = almacenes_destino[0] if almacenes_destino else ''
    nombre_cliente = f"Traslado a {almacen_destino}" if almacen_destino else f"Traslado desde {almacen_origen}"
    direccion = comentario[:200] if comentario else f"{almacen_origen} -> {almacen_destino}"

    return {
        'num_entrega':     f"SAP-{folio}",
        'nombre_cliente':  nombre_cliente,
        'rfc_cliente':     '',
        'direccion':       direccion,
        'orden':           folio,
        'fecha_entrega':   fecha,
        'comercializador': 'Raiker',
        'sucursal':        almacen_destino,
        'productos':       productos
    }
