import re

def detectar_tipo(texto: str) -> str:
    muestra = texto[:800].upper()
    # Traspasos Raiker (RetailOne) — chequeo en texto completo, layout desordenado
    t_completo = texto.upper()
    if 'RETAILONE' in t_completo.replace(' ', '') and \
       ('SALIDA POR TRASPASO' in t_completo or 'ENTRADA POR TRASPASO' in t_completo):
        return 'TRASPASO_RAIKER'
    # Factura CFDI de ECOR — chequear ANTES de la nota de entrega generica,
    # ya que ambas comparten "EQUIPOS COREANOS" en el encabezado
    if 'EQUIPOS COREANOS' in t_completo and 'FOLIO FISCAL' in t_completo and 'CONCEPTOS' in t_completo:
        return 'FACTURA_ECOR'
    # ECOR siempre primero
    if re.search(r'[A-Z]{2,6}-[A-Z]{2,6}/OUT/\d+', muestra) or \
       'EQUIPOS COREANOS' in muestra or 'ECOR.MX' in muestra:
        return 'ECOR_ODOO'
    if 'SOLICITUD DE TRASLADO' in muestra and \
       ('ALMACEN DE SALIDA' in muestra or 'ALMACEN DE ENTRADA' in muestra):
        return 'SAP_RAIKER'
    if 'TDK INTERNATIONAL' in muestra or 'TDK DE MEXICO' in muestra:
        return 'TDK'
    if 'KOREI' in muestra and 'EQUIPOS COREANOS' not in muestra:
        return 'KOREI'
    return 'ECOR_ODOO'
