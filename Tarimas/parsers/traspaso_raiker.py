import re, time

# ============================================================
#  PARSER — TRASPASOS RAIKER (sistema RetailOne)
#  Formatos: "Salida por Traspaso" / "Entrada por traspaso"
#  El texto extraido por pdfplumber sale desordenado (layout en
#  columnas), asi que se trabaja con regex tolerantes en vez de
#  posiciones de linea fijas.
# ============================================================

RE_FOLIO   = re.compile(r'(\d{6}TM\d{3,5})')
RE_FECHA   = re.compile(r'(\d{1,2})/(\d{2})/(\d{4})')
RE_SUC     = re.compile(r'(?<![A-Z0-9])(\d{2,3})\s*-?\s*([A-ZÑÁÉÍÓÚ][A-ZÑÁÉÍÓÚ .]{3,45})')
RE_SKU     = re.compile(r'^([A-Z]+[0-9]+[A-Z0-9]*)\s*(.*)$')
RE_QTY_END = re.compile(r'(\d+\.\d{2})\s*$')

STOP_WORDS = {'TOTALES', 'COMENTARIOS', 'CANTIDAD', 'PAGINA', 'PÁGINA', 'RESPONSABLE'}


def detectar_traspaso_raiker(texto: str) -> bool:
    t = texto.upper()
    tiene_folio_retailone = 'RETAILONE' in t.replace(' ', '')
    tiene_tipo = ('SALIDA POR TRASPASO' in t) or ('ENTRADA POR TRASPASO' in t)
    return tiene_folio_retailone and tiene_tipo


def parsear_traspaso_raiker(texto: str) -> dict:
    lineas = [l.strip() for l in texto.split('\n') if l.strip()]

    if re.search(r'salida\s+por\s+traspaso', texto, re.I):
        tipo = 'salida'
    elif re.search(r'entrada\s+por\s+traspaso', texto, re.I):
        tipo = 'entrada'
    else:
        tipo = 'traspaso'

    m_folio = RE_FOLIO.search(texto)
    folio = m_folio.group(1) if m_folio else f"TRASPASO-{int(time.time())}"

    m_fecha = RE_FECHA.search(texto)
    fecha = f"{m_fecha.group(3)}-{m_fecha.group(2)}-{m_fecha.group(1)}" if m_fecha else ''

    # Sucursales mencionadas — normalmente aparecen origen y destino
    sucursales = []
    vistos = set()
    for m in RE_SUC.finditer(texto):
        etiqueta = f"{m.group(1)} - {m.group(2).strip()}"
        if etiqueta in vistos:
            continue
        vistos.add(etiqueta)
        sucursales.append(etiqueta)

    origen  = sucursales[0] if len(sucursales) >= 1 else ''
    destino = sucursales[1] if len(sucursales) >= 2 else (sucursales[0] if sucursales else '')

    # Comentarios (suele traer referencia cruzada al documento origen)
    m_com = re.search(r'Comentarios[:\s]*([\s\S]{0,200}?)(?:P[aá]gina|\Z)', texto, re.I)
    comentario = re.sub(r'\s+', ' ', m_com.group(1)).strip() if m_com else ''

    # ── PRODUCTOS: escaneo por bloques (SKU puede traer descripcion
    #    multilinea y la cantidad puede venir varias lineas despues) ──
    productos = []
    i = 0
    while i < len(lineas):
        l = lineas[i]
        m = RE_SKU.match(l)
        if m and m.group(1) not in STOP_WORDS and any(c.isdigit() for c in m.group(1)):
            clave = m.group(1)
            resto = m.group(2)
            bloque = [resto] if resto else []
            cantidad = None

            mqty = RE_QTY_END.search(resto)
            if mqty:
                cantidad = float(mqty.group(1))
                bloque[-1] = resto[:mqty.start()].strip()
            else:
                j = i + 1
                saltos = 0
                while j < len(lineas) and saltos < 4:
                    lj = lineas[j]
                    primera_palabra = lj.upper().split()[0] if lj else ''
                    if primera_palabra in STOP_WORDS:
                        break
                    mqty2 = RE_QTY_END.search(lj)
                    if mqty2:
                        pre = lj[:mqty2.start()].strip()
                        cantidad = float(mqty2.group(1))
                        # texto corto y todo en mayusculas antes de la cantidad
                        # suele ser el almacen (ej. "BOCA"), se descarta
                        if pre and not (pre.isupper() and len(pre) <= 15):
                            bloque.append(pre)
                        j += 1
                        break
                    bloque.append(lj)
                    j += 1
                    saltos += 1
                i = j - 1

            descripcion = re.sub(r'\s+', ' ', ' '.join(b for b in bloque if b)).strip()
            if cantidad and cantidad > 0 and descripcion:
                productos.append({
                    'clave':          clave,
                    'descripcion':    descripcion[:150],
                    'cantidad_total': int(round(cantidad)),
                    'unidad':         'PZA'
                })
        i += 1

    nombre_cliente = destino or origen or 'Traspaso Raiker'
    sucursal_ref   = origen if tipo == 'entrada' else destino

    return {
        'num_entrega':     f"TRASPASO-{folio}",
        'nombre_cliente':  nombre_cliente,
        'rfc_cliente':     '',
        'direccion':       comentario[:200] if comentario else sucursal_ref,
        'orden':           f"Traspaso {tipo.capitalize()} {folio}",
        'fecha_entrega':   fecha,
        'comercializador': 'Raiker',
        'sucursal':        sucursal_ref,
        'productos':       productos
    }
