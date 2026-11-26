"""Funciones puras de Inventario y Operaciones (sin base de datos ni Odoo), portadas de Datos.gs / Conteo.gs / Capturas.gs."""
import re, csv, io, unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

MX = ZoneInfo("America/Mexico_City")
UTC = ZoneInfo("UTC")
EPS = 1e-9
MAX_UBICACIONES_CONTEO = 5000
MAX_RONDAS = 3


def iso(d) -> str:
    """Fecha naive UTC -> 'yyyy-MM-dd HH:mm' en hora de Ciudad de Mexico."""
    if not d:
        return ""
    if isinstance(d, str):
        return d
    return d.replace(tzinfo=UTC).astimezone(MX).strftime("%Y-%m-%d %H:%M")


def ahora() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def txt(v) -> str:
    return "" if v is None else str(v).strip()


def num(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def norm(s) -> str:
    """minusculas sin acentos"""
    t = unicodedata.normalize("NFD", txt(s).lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def norm_clave(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", txt(s).upper())


def r1(x): return round(x * 10) / 10
def r2(x): return round(x * 100) / 100


def sg(n) -> str:
    n = round(num(n) * 1000) / 1000
    n = int(n) if n == int(n) else n
    return ("+" if n > 0 else "") + str(n)


def dist_edicion(a, b) -> int:
    """Damerau-Levenshtein (con letras intercambiadas)."""
    a, b = str(a), str(b)
    m, n = len(a), len(b)
    d = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1): d[i][0] = i
    for j in range(n + 1): d[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[m][n]


def limite_dist(a, b) -> int:
    return max(1, min(2, max(len(a), len(b)) // 4))


def sugerencia_clave(clave, conocidas: dict) -> str:
    kc, mejor = norm_clave(clave), None
    for kn, orig in conocidas.items():
        if not kn or kn == kc:
            continue
        d = dist_edicion(kc, kn)
        if d <= limite_dist(kc, kn) and (mejor is None or d < mejor[0]):
            mejor = (d, orig)
    return mejor[1] if mejor else ""


def motivo_cantidad(esp, con) -> str:
    """Motivo si lo contado es sospechoso frente al sistema, o ''."""
    if not (esp > 0) or not (con >= 0) or con == esp:
        return ""
    se = "".join(sorted(str(round(esp)))); sc = "".join(sorted(str(round(con))))
    if se == sc and str(round(esp)) != str(round(con)) and esp >= 10:
        return f"Los dígitos de lo contado ({_f(con)}) son los mismos que los del sistema ({_f(esp)}) en otro orden: posible dígitos cambiados."
    if con > 0 and (abs(con / esp - 10) < 1e-9 or abs(con / esp - 0.1) < 1e-9 or abs(con / esp - 100) < 1e-9):
        return f"Lo contado ({_f(con)}) es exactamente {'10' if con > esp else '1/10'} veces el sistema ({_f(esp)}): posible cero de más o de menos."
    if con >= esp * 3 and con - esp >= 20:
        return f"Lo contado ({_f(con)}) es más de 3 veces lo que dice el sistema ({_f(esp)}): confirmar que no se capturó otra cifra (piezas por caja, total de otra ubicación)."
    return ""


def _f(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def rack_de_codigo(cod) -> str:
    m = re.match(r"^([A-Z]+R\d+)", txt(cod).upper())
    return m.group(1) if m else ""


def grupo_de(codigo, bodega) -> str:
    """Grupo de conteo = rack del CODIGO (ADISR14AC05N04 -> ADISR14); pisos por su prefijo de letras."""
    cod, bod = txt(codigo).upper(), txt(bodega)
    m = re.match(r"^([A-Z]+\d?R\d+)", cod)
    if m:
        return f"{m.group(1)} · {bod}"
    m = re.match(r"^([A-Z]+\d(?=B\d\d)|[A-Z]+)", cod)
    if m:
        return f"Piso {m.group(1)} · {bod}"
    return f"Sin grupo · {bod}"


def csv_texto(filas) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for f in filas:
        w.writerow(["" if v is None else v for v in f])
    return "﻿" + buf.getvalue()


def es_otra_un(inesperado, origen, producto, cat: dict) -> bool:
    if not inesperado:
        return False
    return txt(origen) == "otra_un" or txt(producto).upper() in cat


def resumen_otras_un(lineas, cat):
    """lineas: dicts con producto, producto_desc, codigo, contado"""
    por, u = {}, 0
    for l in lineas:
        k = txt(l["producto"]); uni = cat.get(k.upper()) or "Sin asignar"; q = num(l.get("contado"))
        o = por.setdefault(k, {"producto": k, "descripcion": txt(l.get("producto_desc")), "unidad": uni, "unidades": 0, "ubicaciones": []})
        o["unidades"] += q
        if q > 0 and txt(l["codigo"]) not in o["ubicaciones"]:
            o["ubicaciones"].append(txt(l["codigo"]))
        u += q
    det = sorted(por.values(), key=lambda d: -d["unidades"])
    pu = {}
    for d in det:
        x = pu.setdefault(d["unidad"], {"unidad": d["unidad"], "productos": 0, "unidades": 0})
        x["productos"] += 1; x["unidades"] += d["unidades"]
    out = []
    for d in det[:500]:
        d["n_ubicaciones"] = len(d["ubicaciones"]); d["ubicaciones"] = d["ubicaciones"][:6]; out.append(d)
    return {"unidades": u, "productos": len(det), "por_unidad": sorted(pu.values(), key=lambda x: -x["unidades"]), "detalle": out}
