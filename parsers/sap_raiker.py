import re, time

SUCURSALES = [
    'ACAYUCAN','APIZACO','ATLIXCO','BOCA','BOTICARIA','BOULEVARD',
    'CANCUN','CARDEL','CARDENAS','COATZA','COSAMALOAPAN','DIAZ MIRON',
    'EMILIANO ZAPATA','GUADALAJARA','IZUCAR','LAS CHOAPAS','LOMA BONITA',
    'MALIBRAN','MARTINEZ','MERIDA','OAXACA','ORIZABA','PACHUCA','PAPANTLA',
    'PEROTE','PUEBLA','SALINA CRUZ','SAN ANDRES','TECAMACHALCO','TEHUACAN',
    'TEJERIA','TENOSIQUE','TEXMELUCAN','TIERRA BLANCA','TIZAYUCA',
    'TLALNEPANTLA','TUXPAN','TUXTEPEC','VER NORTE','VILLAHERMOSA','XALAPA'
]

def parsear_sap_raiker(texto: str, nombre_archivo: str = '') -> dict:
    lineas = [l.replace('\u25a0','').strip() for l in texto.split('\n') if l.strip()]

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
    if not folio: folio = f"TL-{int(time.time())}"

    sucursal   = ''
    archivo_up = (nombre_archivo or '').upper().replace('_',' ').replace('-',' ')
    for s in SUCURSALES:
        if s in archivo_up: sucursal = s; break
    if not sucursal:
        texto_up = texto.upper()
        for s in SUCURSALES:
            if s in texto_up: sucursal = s; break

    SKU_RE = re.compile(r'^[A-Z][A-Z0-9\-\.\/]{2,19}$')
    NUM_RE = re.compile(r'^\d+$')

    inicio = 0
    for i, l in enumerate(lineas):
        if re.search(r'[Nn]umero de articulo', l):
            inicio = i + 1; break

    productos = []
    i = inicio
    while i < len(lineas):
        l = lineas[i]
        if NUM_RE.match(l) and 1 <= int(l) <= 500:
            i += 1
            if i >= len(lineas): break
            if not SKU_RE.match(lineas[i]): continue
            sku  = lineas[i]
            desc = []
            i   += 1
            while i < len(lineas):
                l2 = lineas[i]
                if l2 in ('PZA','PZ','EA','UN'): i += 1; break
                if re.search(r'^Total:|^Pagina|SAP Business', l2): break
                desc.append(l2)
                i += 1
            cant = 1
            if i < len(lineas) and NUM_RE.match(lineas[i]):
                cant = int(lineas[i]); i += 1
            productos.append({
                'clave':          sku,
                'descripcion':    ' '.join(desc).strip(),
                'cantidad_total': cant,
                'unidad':         'PZA'
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
