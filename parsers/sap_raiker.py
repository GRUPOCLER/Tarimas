import re

SUCURSALES = ['ACAYUCAN','APIZACO','ATLIXCO','BOCA','BOTICARIA','BOULEVARD',
    'CANCUN','CARDEL','CARDENAS','COATZA','COSAMALOAPAN','DIAZ MIRON',
    'EMILIANO ZAPATA','GUADALAJARA','IZUCAR','LAS CHOAPAS','LOMA BONITA',
    'MALIBRAN','MARTINEZ','MERIDA','OAXACA','ORIZABA','PACHUCA','PAPANTLA',
    'PEROTE','PUEBLA','SALINA CRUZ','SAN ANDRES','TECAMACHALCO','TEHUACAN',
    'TEJERIA','TENOSIQUE','TEXMELUCAN','TIERRA BLANCA','TIZAYUCA',
    'TLALNEPANTLA','TUXPAN','TUXTEPEC','VER NORTE','VILLAHERMOSA','XALAPA']

def parsear_sap_raiker(texto: str, nombre_archivo: str = '') -> dict:
    lineas = [l.replace('\u25a0','').replace('\u258c','').strip() for l in texto.split('\n') if l.strip()]

    fecha = ''
    for l in lineas:
        m = re.search(r'(\d{2})/(\d{2})/(\d{4})', l)
        if m: fecha = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"; break

    folio = ''
    m2 = re.search(r'TL(\d+)', nombre_archivo or '', re.I)
    if m2: folio = f"TL{m2.group(1)}"
    if not folio:
        for l in lineas:
            mn = re.match(r'^([\d,]{3,8})$', l)
            if mn: folio = f"TL-{mn.group(1).replace(',','')}"; break
    if not folio: folio = f"TL-{__import__('time').time_ns()}"

    sucursal = ''
    archivo_up = (nombre_archivo or '').upper().replace('_',' ').replace('-',' ')
    for s in SUCURSALES:
        if s in archivo_up: sucursal = s; break
    if not sucursal:
        texto_up = texto.upper()
        for s in SUCURSALES:
            if s in texto_up: sucursal = s; break

    # Productos: intenta primero el formato de "una fila = una linea completa"
    # (numero, clave, descripcion, unidad, cantidad todo junto) — con la
    # descripcion pudiendo continuar en la siguiente linea si es larga.
    RE_FILA = re.compile(
        r'^(\d+)\s+([A-Z0-9][A-Z0-9\-\.\/]{1,19})\s+(.+?)\s+(PZA|PZ|EA|UN)\s+(\d+)$'
    )
    STOP_CONT = re.compile(r'^(Total:|P[aá]gina|Impreso)', re.I)

    productos = []
    idx = 0
    n = len(lineas)
    while idx < n:
        m = RE_FILA.match(lineas[idx])
        if m:
            clave = m.group(2)
            desc  = m.group(3).strip()
            cant  = int(m.group(5))
            idx += 1
            # La descripcion puede seguir en la siguiente linea si era larga
            while idx < n and not RE_FILA.match(lineas[idx]) and not STOP_CONT.match(lineas[idx]):
                desc += ' ' + lineas[idx]
                idx += 1
            productos.append({
                'clave': clave, 'descripcion': desc.strip(),
                'cantidad_total': cant, 'unidad': 'PZA'
            })
        else:
            idx += 1

    # Respaldo: formato viejo de "un dato por linea" (columnar), por si
    # algun documento SAP distinto sigue viniendo asi
    if not productos:
        SKU_RE = re.compile(r'^[A-Z][A-Z0-9\-\.\/]{2,19}$')
        NUM_RE = re.compile(r'^\d+$')

        inicio = 0
        for i, l in enumerate(lineas):
            if re.search(r'[Nn]umero de articulo|[Nn]mero de art', l):
                inicio = i + 1; break

        i = inicio
        while i < len(lineas):
            l = lineas[i]
            if NUM_RE.match(l) and 1 <= int(l) <= 500:
                i += 1
                if i >= len(lineas): break
                if not SKU_RE.match(lineas[i]): continue
                sku = lineas[i]; desc = []; i += 1
                while i < len(lineas):
                    l2 = lineas[i]
                    if l2 in ('PZA','PZ','EA','UN'): i += 1; break
                    if re.search(r'^Total:|^Pagina|SAP Business', l2): break
                    desc.append(l2); i += 1
                cant = 1
                if i < len(lineas) and NUM_RE.match(lineas[i]):
                    cant = int(lineas[i]); i += 1
                productos.append({
                    'clave': sku, 'descripcion': ' '.join(desc).strip(),
                    'cantidad_total': cant, 'unidad': 'PZA'
                })
            else:
                i += 1

    return {
        'num_entrega':    folio,
        'nombre_cliente': 'AGROINDUSTRIAS RAIKER',
        'rfc_cliente':    '',
        'direccion':      f"Sucursal {sucursal}" if sucursal else 'AGROINDUSTRIAS RAIKER',
        'orden':          '',
        'fecha_entrega':  fecha,
        'comercializador':'Raiker',
        'sucursal':       sucursal,
        'productos':      productos
    }
