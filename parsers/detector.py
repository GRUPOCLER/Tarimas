import re

def detectar_tipo(texto: str) -> str:
    muestra = texto[:800].upper()
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
