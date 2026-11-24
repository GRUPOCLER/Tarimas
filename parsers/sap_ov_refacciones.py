"""
Lectores de PDF de SAP Business One para el flujo de refacciones.

  parsear_ov(bytes)      -> "Confirmacion de pedido" (OV): partidas a surtir
  parsear_entrega(bytes) -> "Informe de lotes en documento" (Entrega): partidas y lotes
  parsear_factura(bytes) -> Factura CFDI: folio, UUID y OV / Entrega relacionadas

Notas de formato (aprendidas de los ejemplos reales):
  * En la OV la descripcion se encima con la columna de cantidad. Se lee con
    use_text_flow=True (orden del flujo del PDF) y la cantidad se valida con total/precio.
  * El Item Code impreso puede traer espacio ("HYWP1500G 110") mientras que el codigo de
    barras impreso usa guion bajo ("HYWP1500G_110"). Para comparar se usa norm_codigo().
  * El "lote" de la entrega es el pedimento (aa aa pppp nnnnnnn).
"""
import io
import re

import pdfplumber


def norm_codigo(c: str) -> str:
    """Normaliza un codigo de articulo / lectura de codigo de barras para compararlo."""
    c = (c or "").strip().upper().strip("*")
    return re.sub(r"[\s_]+", "", c)


def _num(s: str) -> float:
    return float(str(s).replace(",", "").strip())


def _f(s: str) -> str:
    """dd/mm/aaaa -> aaaa-mm-dd"""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _abrir(contenido):
    return pdfplumber.open(io.BytesIO(contenido) if isinstance(contenido, (bytes, bytearray)) else contenido)


# ────────────────────────── OV ──────────────────────────
def parsear_ov(contenido) -> dict:
    avisos = []
    partidas = []
    with _abrir(contenido) as pdf:
        t1 = pdf.pages[0].extract_text() or ""
        t_ult = pdf.pages[-1].extract_text() or ""
        for pg in pdf.pages:
            ws = pg.extract_words(use_text_flow=True, keep_blank_chars=False)
            # encabezado de la tabla (fila "Descripcion ... Total") -> las partidas empiezan debajo
            enc = [w["top"] for w in ws if w["text"] == "Descripción" and w["x0"] < 120]
            if not enc:
                continue
            y0 = enc[0] + 8
            cuerpo = [w for w in ws if w["top"] > y0 and not w["text"].startswith("▌")]
            # numero de partida: 3 digitos pegados al margen izquierdo
            ancla = sorted([w for w in cuerpo if w["x0"] < 70 and re.fullmatch(r"\d{3}", w["text"])], key=lambda w: w["top"])
            fechas = sorted([w for w in cuerpo if w["text"] == "Fecha" and w["x0"] < 100], key=lambda w: w["top"])
            for a in ancla:
                fin = next((f["top"] for f in fechas if f["top"] > a["top"]), a["top"] + 60)
                bl = [w for w in cuerpo if a["top"] - 3 <= w["top"] <= fin + 2 and not (w["text"] == "Fecha" and w["top"] >= fin - 1)]
                # solo lo que esta antes de la fila "Fecha de entrega"
                bl = [w for w in bl if w["top"] < fin - 1]
                # fila de Item Code
                code_w = next((w for w in bl if w["text"] == "Code:" and w["x0"] < 120), None)
                item = ""
                alm = ""
                fila_item = None
                if code_w:
                    fila_item = code_w["top"]
                    alm_w = next((w for w in bl if w["text"] == "Almacen:" and abs(w["top"] - fila_item) < 5), None)
                    xmax = alm_w["x0"] if alm_w else 9999
                    item = " ".join(w["text"] for w in sorted(
                        [w for w in bl if abs(w["top"] - fila_item) < 5 and code_w["x0"] < w["x0"] < xmax], key=lambda w: w["x0"]))
                    if alm_w:
                        alm = next((w["text"] for w in sorted(bl, key=lambda w: w["x0"]) if abs(w["top"] - fila_item) < 5 and w["x0"] > alm_w["x0"] and not w["text"].startswith("*")), "")
                nums4 = [w for w in bl if re.fullmatch(r"[\d,]*\d\.\d{4}", w["text"])]
                precio = next((w for w in nums4 if w["x0"] < 450), None)
                total = next((w for w in sorted(nums4, key=lambda w: -w["x0"]) if w["x0"] >= 450 and w is not precio), None)
                # cantidad: entero en la banda de la columna "Cantidad"
                cand = [w for w in bl if 292 <= w["x0"] <= 325 and re.fullmatch(r"\d+", w["text"])]
                qty = None
                pv = _num(precio["text"]) if precio else 0
                tv = _num(total["text"]) if total else 0
                calc = round(tv / pv) if pv else None
                if cand:
                    qty = int(cand[0]["text"])
                    if calc is not None and abs(calc * pv - tv) < 0.01 and qty != calc:
                        avisos.append(f"Partida {a['text']}: cantidad leida {qty} no cuadra con total/precio ({calc}); se usa {calc}.")
                        qty = calc
                elif calc is not None:
                    qty = calc
                if qty is None:
                    avisos.append(f"Partida {a['text']}: no se pudo leer la cantidad.")
                    qty = 0
                excluir = {id(a)}
                if cand:
                    excluir.add(id(cand[0]))
                desc = []
                for w in bl:
                    if id(w) in excluir:
                        continue
                    if fila_item is not None and abs(w["top"] - fila_item) < 6:
                        continue
                    if w["text"].startswith("*") and w["text"].endswith("*"):
                        continue
                    if w in nums4 or (w["text"] == "16.00" and w["x0"] > 440) or (w["text"] == "PZA" and 325 <= w["x0"] <= 345):
                        continue
                    if re.fullmatch(r"[\d,]*\d\.\d{2}", w["text"]) and w["x0"] > 440:
                        continue
                    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", w["text"]):
                        continue
                    desc.append(w["text"])
                partidas.append({
                    "linea": int(a["text"]),
                    "codigo": item,
                    "codigo_norm": norm_codigo(item),
                    "descripcion": " ".join(desc).strip(),
                    "cantidad": qty,
                    "unidad": "PZA",
                    "precio": pv,
                    "total": tv,
                    "almacen": alm,
                })
    # encabezado
    def _r(p, t=t1, g=1, flags=0):
        m = re.search(p, t, flags)
        return m.group(g).strip() if m else ""
    doc = re.search(r"\n(\d{7,10})\s+(\d{2}/\d{2}/\d{4})\s+1/(\d+)", "\n" + t1)
    sub = re.search(r"Total antes del impuesto:\s+MXP\s+([\d,\.]+)", t_ult)
    imp = re.search(r"Importe total:\s+MXP\s+([\d,\.]+)", t_ult)
    pie = t_ult.split("Importe total:")[-1] if "Importe total:" in t_ult else ""
    notas = [l.strip() for l in pie.split("\n")[1:] if l.strip() and not l.startswith("***") and not l.startswith("Confirmación") and not l.startswith("Firma")]
    # un cliente por debajo de la razon social emisora
    emisor = _r(r"\n([A-Z0-9 ,\.]+SA DE CV)\s+Número de cliente")
    cliente = ""
    m = re.search(r"SA DE CV\s+Número de cliente.*?\n.*?\n([A-ZÁÉÍÓÚÑ0-9 \.,&]+)\n", t1, re.S)
    if m:
        cliente = m.group(1).strip()
    dest, ciudad = "", ""
    try:
        with _abrir(contenido) as pdf2:
            pg = pdf2.pages[0]
            der = (pg.crop((pg.width * 0.55, 0, pg.width, pg.height * 0.42)).extract_text() or "")
        m = re.search(r"Dirección de entrega\s*\n(.+?)\nMX\b", der, re.S)
        if m:
            lin = [l.strip() for l in m.group(1).split("\n") if l.strip() and l.strip() != "MX"]
            dest = lin[0] if lin else ""
            ciudad = lin[-1] if len(lin) > 1 else ""
            ciudad = re.sub(r",\s*\d{5},.*$", "", ciudad).strip()
    except Exception:
        pass
    out = {
        "num_ov": doc.group(1) if doc else "",
        "fecha_ov": _f(doc.group(2)) if doc else "",
        "emisor": emisor,
        "cliente_codigo": _r(r"Número de cliente\s+RFC\s*\n(\S+)\s+(\S+)", g=1),
        "cliente_rfc": _r(r"Número de cliente\s+RFC\s*\n(\S+)\s+(\S+)", g=2),
        "cliente": cliente,
        "destino": dest,
        "destino_ciudad": ciudad,
        "fecha_entrega": _f(_r(r"Fecha de entrega:\s+(\d{2}/\d{2}/\d{4})")),
        "fecha_cancelacion": _f(_r(r"Fecha de cancelación\s+(\d{2}/\d{2}/\d{4})")),
        "moneda": _r(r"Moneda:\s+(\w+)"),
        "subtotal": _num(sub.group(1)) if sub else 0,
        "total": _num(imp.group(1)) if imp else 0,
        "notas": notas,
        "referencia": notas[0] if notas and not notas[0].lower().startswith("permitir") else "",
        "permite_parcial": any("permitir entrega parcial" in n.lower() for n in notas),
        "partidas": sorted(partidas, key=lambda p: p["linea"]),
        "avisos": avisos,
    }
    suma = round(sum(p["total"] for p in out["partidas"]), 2)
    if out["subtotal"] and abs(suma - out["subtotal"]) > 0.05:
        out["avisos"].append(f"La suma de partidas ({suma:,.2f}) no cuadra con el subtotal del documento ({out['subtotal']:,.2f}). Revisa que el PDF este completo.")
    n = [p["linea"] for p in out["partidas"]]
    if n and n != list(range(1, max(n) + 1)):
        out["avisos"].append("Faltan numeros de partida en la secuencia; revisa que el PDF este completo.")
    return out


# ────────────────────────── ENTREGA ──────────────────────────
_PED = re.compile(r"^\d{2} \d{2} \d{4} \d{7}$")


def parsear_entrega(contenido) -> dict:
    items = {}
    cab = {"folio": "", "fecha": "", "hora": "", "cliente": "", "rfc": "", "autor": ""}
    with _abrir(contenido) as pdf:
        t1 = pdf.pages[0].extract_text() or ""
        m = re.search(r"Entrega\s+(\d{4,})", t1)
        cab["folio"] = m.group(1) if m else ""
        m = re.search(r"Fecha\s+(\d{2}/\d{2}/\d{4})", t1)
        cab["fecha"] = _f(m.group(1)) if m else ""
        m = re.search(r"Tiempo\s+(\d{1,2}:\d{2})", t1)
        cab["hora"] = m.group(1) if m else ""
        m = re.search(r"Hasta\s+(.+?)\s+Fecha", t1)
        cab["cliente"] = m.group(1).strip() if m else ""
        m = re.search(r"\b([A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3})\b", t1)
        cab["rfc"] = m.group(1) if m else ""
        m = re.search(r"Autor:\s*(.+)", t1)
        cab["autor"] = m.group(1).strip() if m else ""
        actual = None
        for pg in pdf.pages:
            ws = pg.extract_words(use_text_flow=False)
            filas = {}
            for w in ws:
                filas.setdefault(round(w["top"] / 3), []).append(w)
            claves = sorted(filas)
            tops = {k: min(w["top"] for w in filas[k]) for k in claves}
            # filas de partida: numero a la izquierda + entero en columna Cantidad (x~468-515) + sin diagonal
            fila_item = []
            for k in claves:
                fw = sorted(filas[k], key=lambda w: w["x0"])
                izq = [w for w in fw if w["x0"] < 45 and re.fullmatch(r"\d{1,3}", w["text"])]
                cant = [w for w in fw if 470 <= w["x0"] <= 520 and re.fullmatch(r"\d+", w["text"])]
                cod = [w for w in fw if 50 <= w["x0"] < 190]
                if izq and cant and cod:
                    fila_item.append((k, izq[0], cant[0], cod))
            # limite: footer
            limite = min([w["top"] for w in ws if w["text"] == "Página"] or [9999])
            for i, (k, izq, cant, cod) in enumerate(fila_item):
                n = int(izq["text"])
                top = tops[k]
                sig = tops[fila_item[i + 1][0]] - 25 if i + 1 < len(fila_item) else limite
                it = items.setdefault(n, {"linea": n, "codigo": "", "descripcion": "", "cantidad": 0, "total": 0.0, "lotes": []})
                it["codigo"] = it["codigo"] or " ".join(w["text"] for w in cod)
                it["codigo_norm"] = norm_codigo(it["codigo"])
                it["cantidad"] = int(cant["text"])
                desc = " ".join(w["text"] for w in sorted([w for w in ws if abs(w["top"] - top) < 14 and 190 <= w["x0"] < 460 and w["text"] != "MXP"], key=lambda w: (round(w["top"]), w["x0"])))
                it["descripcion"] = it["descripcion"] or desc
                tot = [w for w in ws if top - 2 <= w["top"] <= top + 14 and w["x0"] >= 500 and re.fullmatch(r"[\d,]*\d\.\d{4}", w["text"])]
                if tot:
                    it["total"] = _num(tot[0]["text"])
                # lotes: de ahi hasta la siguiente partida
                zona = [w for w in ws if top + 14 < w["top"] < sig]
                inicios = sorted([w for w in zona if w["text"] == "ALM-R" and w["x0"] < 100], key=lambda w: w["top"])
                for j, ini in enumerate(inicios):
                    ymax = inicios[j + 1]["top"] - 10 if j + 1 < len(inicios) else sig
                    # no mezclar el encabezado "# / Numero de articulo" de la siguiente partida
                    hdr = [w["top"] for w in zona if w["text"] == "#" and w["x0"] < 45 and w["top"] > ini["top"]]
                    if hdr:
                        ymax = min(ymax, min(hdr) - 2)
                    bloque = [w for w in zona if ini["top"] - 6 <= w["top"] < ymax]
                    lote_t = sorted([w for w in bloque if w["x0"] < 80], key=lambda w: (round(w["top"]), w["x0"]))
                    lote = " ".join(w["text"] for w in lote_t)
                    if not _PED.match(lote):
                        lote = "".join(w["text"] for w in lote_t)
                    q = next((w for w in bloque if 115 <= w["x0"] <= 180 and re.fullmatch(r"[\d,]*\d\.\d{2}", w["text"])), None)
                    d = next((w for w in bloque if w["x0"] >= 420 and re.fullmatch(r"\d{2}/\d{2}/\d{4}", w["text"])), None)
                    if q:
                        it["lotes"].append({"lote": lote, "cantidad": _num(q["text"]), "admision": _f(d["text"]) if d else ""})
    lineas = sorted(items.values(), key=lambda x: x["linea"])
    avisos = []
    for it in lineas:
        s = sum(l["cantidad"] for l in it["lotes"])
        if it["lotes"] and abs(s - it["cantidad"]) > 0.001:
            avisos.append(f"Partida {it['linea']} ({it['codigo']}): lotes suman {s:g} y la cantidad es {it['cantidad']}.")
    cab["partidas"] = lineas
    cab["total"] = round(sum(x["total"] for x in lineas), 2)
    cab["avisos"] = avisos
    return cab


# ────────────────────────── FACTURA ──────────────────────────
def parsear_factura(contenido) -> dict:
    with _abrir(contenido) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    def r(p, g=1, flags=0):
        m = re.search(p, texto, flags)
        return m.group(g).strip() if m else ""
    obs = r(r"Observaciones\s+(.+?)(?:\nSerie del Certificado|\nFolio Fiscal)", flags=re.S).replace("\n", " ")
    tot = re.findall(r"Total\s*:?\s*\$?\s*([\d,]+\.\d{2})", texto)
    return {
        "folio": r(r"Factura\s+([A-Z]{1,4}\d{3,})"),
        "fecha": r(r"Fecha\s+(\d{4}-\d{2}-\d{2})"),
        "uuid": r(r"Folio Fiscal\s+([0-9A-F\-]{36})"),
        "cliente": r(r"Cliente\s*:\s*(.+)"),
        "rfc_cliente": r(r"R\.F\.C\.\s*:\s*(\S+)"),
        "observaciones": obs,
        "num_ov": r(r"Pedidos de cliente\s+(\d+)"),
        "num_entrega": r(r"Basado en Entregas\s+(\d+)"),
        "referencia": re.split(r"\s+Basado en", obs)[0].strip() if obs else "",
        "total": _num(tot[-1]) if tot else 0,
    }


def tipo_documento(contenido) -> str:
    """'ov' | 'entrega' | 'factura' | ''  (por el contenido de la primera pagina)"""
    with _abrir(contenido) as pdf:
        t = (pdf.pages[0].extract_text() or "")
    if "Confirmación de pedido" in t or "Confirmaci" in t and "pedido" in t:
        return "ov"
    if "Informe de lotes en documento" in t:
        return "entrega"
    if "Tipo de Comprobante" in t or "Folio Fiscal" in t or re.search(r"Factura\s+[A-Z]{1,4}\d{3,}", t):
        return "factura"
    return ""
