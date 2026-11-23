import re
from parsers.sap_raiker import SUCURSALES as SUCURSALES_RAIKER

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

    # Productos - enfoque por bloques [SKU]
    # Cada bloque va de un [SKU] al siguiente. Dentro del bloque, la cantidad es la
    # PRIMERA cifra con 2 a 4 decimales despues de la descripcion:
    #   Odoo 17: "[SKU] Desc 123456 1.0000 Unidades"          (lotes enteros, cantidad 4 dec)
    #   Odoo 19: "[SKU] Desc 1.0000 914.0000 Unidades"        (cantidad + otra columna numerica)
    #   Odoo 19: "[SKU] Desc 914 1.00 Unidades"               (serie + cantidad con 2 dec)
    # Antes se tomaba la ULTIMA cifra, y en Odoo 19 esa es otra columna (existencias),
    # no lo que se entrega. Los numeros de serie/lote son enteros, por eso no se confunden.
    # Si el PDF trae folio de Odoo, entregas.py ademas valida contra Odoo y Odoo manda.
    EXCLUIR = {'Pagina', 'pagina', 'Producto', 'serie', 'lote', 'Entregado'}
    CANT = re.compile(r'(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2,4})(?![\d])(?!\s*(?:HP|hp|Hp|MM|mm|CM|cm|KG|kg|L\b|LT|lt|ML|ml|PULG|pulg|"))')
    bloques = list(re.finditer(r'\[([^\]]+)\]', texto))
    acumulado = {}

    for bi, match in enumerate(bloques):
        clave = match.group(1).strip()
        if clave in EXCLUIR: continue
        inicio = match.end()
        fin    = bloques[bi+1].start() if bi+1 < len(bloques) else len(texto)
        bloque = texto[inicio:fin]

        idx_u = bloque.find('Unidades')
        if idx_u == -1:
            idx_u = len(bloque)  # Odoo 19 puede omitir la unidad (depende de grupos de UdM)
        antes = bloque[:idx_u]

        cantidades = list(CANT.finditer(antes))
        if not cantidades: continue
        valor = lambda m: float(m.group(1).replace(',', '') + '.' + m.group(2))
        primera = cantidades[0]
        # Orden de venta (trae "precio unitario" e "$ importe"): se confirma con
        # cantidad x precio = importe, para no confundir la cantidad con el precio.
        resto = ''
        m_imp = re.search(r'\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})', antes)
        if m_imp:
            importe = float(m_imp.group(1).replace(',', ''))
            previas = [m for m in cantidades if m.start() < m_imp.start()]
            for k in range(len(previas) - 1):
                if abs(valor(previas[k]) * valor(previas[k + 1]) - importe) <= max(0.05, importe * 0.005):
                    primera = previas[k]
                    break
            resto = antes[m_imp.end():]  # 2a linea de la descripcion (va despues del importe)
        cant = valor(primera)
        if cant <= 0 or cant > 100000: continue

        desc = antes[:primera.start()].strip()
        if resto.strip() and not re.search(r'\d+\.\d{2}', resto):
            desc = desc + ' ' + resto.strip()
        desc = re.sub(r'\s+\d{3,8}(\s*,?\s*\d{3,8})*\s*,?\s*$', '', desc).strip()  # series/lotes al final
        desc = re.sub(r'\s+', ' ', desc).strip()

        if clave in acumulado:
            acumulado[clave]['cantidad_total'] += int(round(cant))
        else:
            acumulado[clave] = {
                'clave': clave, 'descripcion': desc[:120],
                'cantidad_total': int(round(cant)), 'unidad': 'Unidades'
            }

    # Sucursal Raiker desde "AGROINDUSTRIAS RAIKER, PAPANTLA"
    sucursal = ''
    if 'RAIKER' in cliente.upper() and ',' in cliente:
        cand = cliente.split(',')[-1].strip().upper()
        sucursal = next((s for s in SUCURSALES_RAIKER if s == cand), '')

    return {
        # Orden de venta sin folio de entrega: se identifica por su numero de OV
        'num_entrega':    folio or orden or f'EC-{__import__("time").time_ns()}',
        'nombre_cliente': cliente,
        'rfc_cliente':    '',
        'direccion':      dir_txt,
        'orden':          orden,
        'fecha_entrega':  fecha,
        'comercializador':'ECOR',
        'sucursal':       sucursal,
        'productos':      list(acumulado.values())
    }
