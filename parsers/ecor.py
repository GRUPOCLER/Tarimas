import re, time

def _limpiar(texto: str) -> str:
    reemplazos = {
        'a':'a','e':'e','i':'i','o':'o','u':'u','n':'n',
        'A':'A','E':'E','I':'I','O':'O','U':'U','N':'N'
    }
    for src, dst in reemplazos.items():
        texto = texto.replace(src, dst)
    return re.sub(r'[^\x00-\x7F\n\r\t ]', ' ', texto)

def parsear_ecor(texto: str) -> dict:
    texto  = _limpiar(texto)
    lineas = texto.split('\n')

    folio = ''
    m = re.search(r'\b([A-Z]{2,6}-[A-Z]{2,6}/OUT/\d+)\b', texto)
    if m: folio = m.group(1)

    orden = ''
    m2 = re.search(r'\b(S\d{5,8})\b', texto)
    if m2: orden = m2.group(1)

    fecha = ''
    m3 = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
    if m3:
        p = m3.group(1).split('/')
        fecha = f"{p[2]}-{p[1]}-{p[0]}"

    cliente = ''
    dir_txt = ''
    for i, l in enumerate(lineas):
        if re.search(r'direcci[oo]n de env[ii]o', l, re.I):
            for j in range(i+1, min(i+5, len(lineas))):
                if lineas[j].strip() and not re.search(r'[aA]venida|[cC]alle|\d{5}', lineas[j]):
                    cliente = lineas[j].strip()
                    break
            dir_parts = [lineas[k].strip() for k in range(i+1, min(i+6, len(lineas))) if lineas[k].strip()]
            dir_txt = ', '.join(dir_parts[:4])
            break

    EXCLUIR = {'Pagina', 'pagina', 'Producto', 'serie', 'lote', 'Entregado'}
    bloques  = list(re.finditer(r'\[([^\]]+)\]', texto))
    acumulado = {}

    for bi, match in enumerate(bloques):
        clave = match.group(1).strip()
        if clave in EXCLUIR: continue
        inicio = match.end()
        fin    = bloques[bi+1].start() if bi+1 < len(bloques) else len(texto)
        bloque = texto[inicio:fin]

        idx_u = bloque.find('Unidades')
        if idx_u == -1: continue
        antes = bloque[:idx_u]

        cantidades = list(re.finditer(r'(\d+\.\d{3,4})', antes))
        if not cantidades: continue
        cant = float(cantidades[-1].group(1))
        if cant <= 0 or cant > 100000: continue

        desc = antes[:cantidades[-1].start()].strip()
        desc = re.sub(r'\s+\d{4,8}(\s+\d{4,8})*\s*$', '', desc).strip()
        desc = re.sub(r'\s+', ' ', desc).strip()

        if clave in acumulado:
            acumulado[clave]['cantidad_total'] += int(cant)
        else:
            acumulado[clave] = {
                'clave': clave, 'descripcion': desc[:120],
                'cantidad_total': int(cant), 'unidad': 'Unidades'
            }

    return {
        'num_entrega':    folio or f'EC-{int(time.time())}',
        'nombre_cliente': cliente,
        'rfc_cliente':    '',
        'direccion':      dir_txt,
        'orden':          orden,
        'fecha_entrega':  fecha,
        'comercializador':'ECOR',
        'sucursal':       '',
        'productos':      list(acumulado.values())
    }
