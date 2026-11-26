import re

def _limpiar(texto: str) -> str:
    reemplazos = {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ñ':'n',
                  'Á':'A','É':'E','Í':'I','Ó':'O','Ú':'U','Ñ':'N'}
    for src, dst in reemplazos.items():
        texto = texto.replace(src, dst)
    return re.sub(r'[^\x00-\x7F\n\r\t ]', ' ', texto)

def parsear_ecor(texto: str) -> dict:
    texto = _limpiar(texto)
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

    # Cliente desde "Direccion de envio:"
    cliente = ''
    dir_txt = ''
    for i, l in enumerate(lineas):
        if re.search(r'direcci[oó]n de env[íi]o', l, re.I):
            for j in range(i+1, min(i+5, len(lineas))):
                if lineas[j].strip() and not re.search(r'[aA]venida|[cC]alle|\d{5}', lineas[j]):
                    cliente = lineas[j].strip()
                    break
            dir_parts = [lineas[k].strip() for k in range(i+1, min(i+6, len(lineas))) if lineas[k].strip()]
            dir_txt = ', '.join(dir_parts[:4])
            break

    # Productos — enfoque por bloques [SKU]. Cada bloque va del "[SKU]" al siguiente "[SKU]" o al pie de pagina.
    EXCLUIR = {'Pagina', 'pagina', 'Producto', 'serie', 'lote', 'Entregado'}
    avisos = []
    # Odoo imprime cantidades con N decimales (4 en esta base; 2 si cambian la precision). N se deduce del propio documento
    # para no confundir "1.25 KW" o "14.5" de una descripcion con una cantidad.
    bloques = [b for b in re.finditer(r'\[([^\]]+)\]', texto) if b.group(1).strip() not in EXCLUIR]
    cuerpos = []
    for bi, match in enumerate(bloques):
        fin = bloques[bi + 1].start() if bi + 1 < len(bloques) else len(texto)
        b = texto[match.end():fin]
        corte = re.search(r'P[aA]gina\s*:|odoo@|https?://|\n\s*Producto\b', b)
        cuerpos.append(b[:corte.start()] if corte else b)
    # Fin de la fila de datos: la unidad de medida (Unidades, Piezas, Docena, Kg...). Si no aparece, toda la cola del bloque.
    RE_UOM = re.compile(r'\b(?:Unidades?|Piezas?|Pzas?|PZA|Docenas?|Cajas?|Pares?|Juegos?|Kits?|Kgs?|Litros?|Metros?|Rollos?|Paquetes?|Bolsas?|Servicios?)\b')
    RE_NUM = re.compile(r'(?<![\w.,/-])(\d{1,7}\.(\d{2,4}))(?![\w.,/])')
    decs = []
    for c in cuerpos:
        m = RE_UOM.search(c)
        toks = RE_NUM.findall(c[:m.start()] if m else c)
        if toks:
            decs.append(len(toks[-1][1]))
    P = max(set(decs), key=decs.count) if decs else 4
    dos_cols = bool(re.search(r'Ordenado\s+Entregado', texto))   # "Recibo de entrega" con demanda y entregado
    filas = []
    for match, c in zip(bloques, cuerpos):
        clave = match.group(1).strip()
        m = RE_UOM.search(c)
        zona = c[:m.start()] if m else c
        nums = [float(n) for n, d in RE_NUM.findall(zona) if len(d) == P]
        desc_fin = RE_NUM.search(zona)
        desc = zona[:desc_fin.start()].strip() if desc_fin else zona.strip()
        desc = re.sub(r'\s+\d{4,8}(\s+\d{4,8})*\s*$', '', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()
        if not nums:
            avisos.append(f'No se pudo leer la cantidad de {clave}: revisa el PDF.')
            continue
        if dos_cols and len(nums) >= 2:
            ordenado, entregado = nums[0], nums[1]
        elif dos_cols:
            ordenado, entregado = nums[0], None
        else:
            ordenado, entregado = nums[-1], nums[-1]
        filas.append((clave, desc, ordenado, entregado, m.group(0) if m else 'Unidades'))
    # Con dos columnas: si NADA aparece entregado (entrega sin validar), se usa lo ordenado; si hay entregas, cuenta lo entregado.
    hay_entregado = any((e or 0) > 0 for _, _, _, e, _ in filas)
    if dos_cols and not hay_entregado and filas:
        avisos.append('La columna Entregado viene en 0 (entrega sin validar): se usaron las cantidades Ordenadas.')
    acumulado = {}
    for clave, desc, ordenado, entregado, uom in filas:
        cant = (entregado or 0) if (not dos_cols or hay_entregado) else ordenado
        if cant <= 0 or cant > 1000000:
            continue
        if abs(cant - round(cant)) > 0.001:
            avisos.append(f'{clave}: cantidad fraccionaria ({cant:g}); se redondeo.')
        if clave in acumulado:
            acumulado[clave]['cantidad_total'] += int(round(cant))
        else:
            acumulado[clave] = {'clave': clave, 'descripcion': desc[:120], 'cantidad_total': int(round(cant)), 'unidad': 'Unidades'}
        if not re.match(r'(?i)unidad', uom):
            avisos.append(f'{clave}: la unidad es "{uom}", no piezas; verifica la cantidad.')

    return {
        'num_entrega':    folio or f'EC-{__import__("time").time_ns()}',
        'nombre_cliente': cliente,
        'rfc_cliente':    '',
        'direccion':      dir_txt,
        'orden':          orden,
        'fecha_entrega':  fecha,
        'comercializador':'ECOR',
        'sucursal':       '',
        'productos':      list(acumulado.values()),
        'avisos':         avisos
    }
