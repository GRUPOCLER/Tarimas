from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_
from pydantic import BaseModel
from typing import Optional
from urllib.parse import quote
import io, os, csv, re, json, httpx

from database import get_db
from models.models import CatalogoItem, CatalogoHistorial, CatalogoAdicional
from services.auth import verificar_token

router = APIRouter()

# Hoja de Google Sheets con el catalogo — via variables de entorno para
# poder cambiarla sin tocar codigo. Requiere que este compartida como
# "Cualquier persona con el enlace" (solo lectura), sin eso Google
# regresa una pagina de login en vez del CSV.
GOOGLE_SHEET_CATALOGO_ID   = os.getenv("GOOGLE_SHEET_CATALOGO_ID", "12Ewr2SvJXRt-g5HqGo-JrnJ07mv3vXboHU7Y2Gfkedw")
GOOGLE_SHEET_CATALOGO_HOJA = os.getenv("GOOGLE_SHEET_CATALOGO_HOJA", "CATALOGO_V2")

from services.permisos import acceso_modulo
get_current_user = acceso_modulo("embarques")

# Nombres de columna aceptados en el Excel/CSV — flexible por si el
# archivo real trae variantes de mayusculas/espacios/acentos.
_ALIAS = {
    "sku": "sku", "clave": "sku", "codigo de articulo": "sku",
    "descripcion": "descripcion", "descripción": "descripcion",
    "peso_unit": "peso_unit", "peso unit": "peso_unit", "peso unitario": "peso_unit", "peso": "peso_unit",
    "lr_unit": "lr_unit", "lr unit": "lr_unit", "largo": "lr_unit", "largo unitario": "lr_unit",
    "an_unit": "an_unit", "an unit": "an_unit", "ancho": "an_unit", "ancho unitario": "an_unit",
    "al_unit": "al_unit", "al unit": "al_unit", "alto": "al_unit", "alto unitario": "al_unit",
    "m3_unit": "m3_unit", "m3 unit": "m3_unit", "m3": "m3_unit",
    "cm_cant": "cm_cant", "cm cant": "cm_cant", "cantidad caja master": "cm_cant", "pzas por caja": "cm_cant", "piezas por caja": "cm_cant",
    "cm_peso": "cm_peso", "cm peso": "cm_peso", "peso caja master": "cm_peso",
    "cm_lr": "cm_lr", "cm lr": "cm_lr", "largo caja master": "cm_lr",
    "cm_an": "cm_an", "cm an": "cm_an", "ancho caja master": "cm_an",
    "cm_al": "cm_al", "cm al": "cm_al", "alto caja master": "cm_al",
    "cm_m3": "cm_m3", "cm m3": "cm_m3", "m3 caja master": "cm_m3",
    "sat_codigo": "sat_codigo", "sat codigo": "sat_codigo", "codigo sat": "sat_codigo", "código sat": "sat_codigo",
    "sat_desc": "sat_desc", "sat descripcion": "sat_desc", "descripcion sat": "sat_desc",
}

def _normalizar_col(nombre: str) -> str:
    n = str(nombre).strip().lower()
    n = re.sub(r"\([^)]*\)", "", n).strip()  # quita "(kg)", "(cm)", etc.
    n = re.sub(r"\s+", " ", n)
    return _ALIAS.get(n, n.replace(" ", "_"))

def _num(v, es_entero=False):
    """Convierte a numero sin confundir separadores. Los valores ya numericos
    (float/int) pasan tal cual. En texto: '1,250.5' -> 1250.5, '0,205' -> 0.205
    (coma decimal), '1,250' -> 1250 (coma de miles), '12.5 kg' -> 12.5."""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        t = re.sub(r"[^\d,.\-]", "", str(v))
        if not t or t in "-.,":
            return 0
        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):          # 1.250,5 -> coma decimal
                t = t.replace(".", "").replace(",", ".")
            else:                                     # 1,250.5 -> coma de miles
                t = t.replace(",", "")
        elif "," in t:
            entero, _, dec = t.rpartition(",")
            if t.count(",") == 1 and (len(dec) != 3 or entero.lstrip("-") == "0"):
                t = entero + "." + dec                # 0,205 / 12,5 -> decimal
            else:
                t = t.replace(",", "")                # 1,250 / 1,250,000 -> miles
        try:
            n = float(t)
        except ValueError:
            return 0
    return int(round(n)) if es_entero else n

def _filas_desde_gviz_json(texto: str) -> list:
    """Parsea la respuesta de gviz (out:json) a dicts con encabezados normalizados.
    Usa el valor crudo 'v'; si viene null usa el texto formateado 'f'."""
    import json
    try:
        ini, fin = texto.index("("), texto.rindex(")")
        tabla = json.loads(texto[ini + 1:fin])["table"]
    except Exception:
        return []
    etiquetas = [(c.get("label") or c.get("id") or "") for c in tabla.get("cols", [])]
    if not any(etiquetas) or not any(_normalizar_col(e) == "sku" for e in etiquetas):
        return []   # gviz no detecto encabezados -> usar CSV
    enc = [_normalizar_col(e) for e in etiquetas]
    filas = []
    for row in tabla.get("rows", []):
        celdas = row.get("c") or []
        vals = []
        for c in celdas:
            vals.append(None if c is None else (c.get("v") if c.get("v") is not None else c.get("f")))
        vals += [None] * (len(enc) - len(vals))
        filas.append(dict(zip(enc, vals)))
    return filas

CAMPOS_TXT = ("descripcion", "sat_codigo", "sat_desc")
CAMPOS_FLOAT = ("peso_unit", "lr_unit", "an_unit", "al_unit", "m3_unit", "cm_peso", "cm_lr", "cm_an", "cm_al", "cm_m3")
CAMPOS_INT = ("cm_cant",)
CAMPOS_EDITABLES = CAMPOS_TXT + CAMPOS_FLOAT + CAMPOS_INT


def _manuales(item) -> set:
    try:
        return set(json.loads(item.campos_manuales or "[]"))
    except Exception:
        return set()


def _txt(v):
    return "" if v is None else (str(round(v, 6)).rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def _igual(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a or 0) - float(b or 0)) < 1e-9
    return (a or "") == (b or "")


async def _upsert_catalogo(db: AsyncSession, filas: list, usuario: str = "sistema") -> dict:
    """Recibe una lista de dicts ya normalizados (claves como 'sku', 'cm_cant', etc.) y hace insert/update en la
    tabla catalogo. Los campos que se editaron en la plataforma NO se pisan, y cada cambio queda en el historial.
    Compartido entre /importar y /sincronizar-drive."""
    insertados, actualizados, omitidos, protegidos = 0, 0, 0, 0
    cambios = 0
    for fila in filas:
        sku = str(fila.get("sku") or "").strip().upper()
        if not sku:
            omitidos += 1
            continue

        existente = await db.execute(select(CatalogoItem).where(CatalogoItem.sku == sku))
        item = existente.scalar_one_or_none()
        es_nuevo = item is None
        if es_nuevo:
            item = CatalogoItem(sku=sku, origen="hoja")
            db.add(item)

        nuevos = {
            "descripcion": str(fila.get("descripcion") or (item.descripcion or "")),
            "sat_codigo": str(fila.get("sat_codigo") or "") or None,
            "sat_desc": str(fila.get("sat_desc") or "") or None,
        }
        for c in CAMPOS_FLOAT:
            nuevos[c] = _num(fila.get(c))
        for c in CAMPOS_INT:
            nuevos[c] = _num(fila.get(c), es_entero=True)

        manual = set() if es_nuevo else _manuales(item)
        hubo = False
        for campo, val in nuevos.items():
            if campo in manual:
                if not _igual(getattr(item, campo), val):
                    protegidos += 1
                continue
            if es_nuevo:
                setattr(item, campo, val)
                continue
            ant = getattr(item, campo)
            if not _igual(ant, val):
                db.add(CatalogoHistorial(sku=sku, campo=campo, valor_anterior=_txt(ant), valor_nuevo=_txt(val),
                                         origen="hoja", motivo="Sincronizacion / importacion", usuario=usuario))
                setattr(item, campo, val)
                hubo = True
                cambios += 1
        if es_nuevo:
            db.add(CatalogoHistorial(sku=sku, campo="(alta)", valor_nuevo=nuevos["descripcion"], origen="hoja",
                                     motivo="Alta desde la hoja", usuario=usuario))
            insertados += 1
        else:
            actualizados += 1

    await db.commit()
    return {"insertados": insertados, "actualizados": actualizados, "omitidos": omitidos, "total_filas": len(filas),
            "cambios_registrados": cambios, "campos_protegidos": protegidos}

@router.get("/")
async def listar_catalogo(
    buscar: Optional[str] = None,
    limite: int = 100,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    q = select(CatalogoItem).order_by(CatalogoItem.sku).limit(limite)
    if buscar:
        patron = f"%{buscar.strip()}%"
        q = q.where(CatalogoItem.sku.ilike(patron) | CatalogoItem.descripcion.ilike(patron))
    result = await db.execute(q)
    items = result.scalars().all()
    return [{
        "sku": i.sku, "descripcion": i.descripcion,
        "peso_unit": i.peso_unit, "lr_unit": i.lr_unit, "an_unit": i.an_unit, "al_unit": i.al_unit,
        "cm_cant": i.cm_cant, "cm_peso": i.cm_peso, "cm_lr": i.cm_lr, "cm_an": i.cm_an, "cm_al": i.cm_al,
        "sat_codigo": i.sat_codigo,
    } for i in items]

@router.get("/diagnostico-pesos")
async def diagnostico_pesos(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lista SKUs con pesos sospechosos (posible error kg vs toneladas/gramos):
    peso de caja master incoherente con peso_unit x piezas por caja (ratio ~1000)
    o pesos unitarios fuera de lo razonable (>500 kg por pieza)."""
    res = await db.execute(select(CatalogoItem).order_by(CatalogoItem.sku))
    sospechosos = []
    for i in res.scalars():
        motivo = None
        if i.peso_unit and i.peso_unit > 500:
            motivo = "peso_unit > 500 kg por pieza"
        elif i.cm_cant and i.cm_cant > 0 and i.peso_unit and i.cm_peso:
            esperado = i.peso_unit * i.cm_cant
            ratio = i.cm_peso / esperado if esperado else 0
            if ratio and not (0.5 <= ratio <= 2):
                motivo = f"cm_peso / (peso_unit x cm_cant) = {ratio:.3g}"
        if motivo:
            sospechosos.append({"sku": i.sku, "peso_unit": i.peso_unit, "cm_cant": i.cm_cant,
                                "cm_peso": i.cm_peso, "motivo": motivo})
    return {"total_sospechosos": len(sospechosos), "items": sospechosos}

@router.get("/contar")
async def contar_catalogo(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    result = await db.execute(select(func.count(CatalogoItem.sku)))
    con_master = await db.execute(select(func.count(CatalogoItem.sku)).where(CatalogoItem.cm_cant > 0))
    return {"total": result.scalar() or 0, "con_caja_master": con_master.scalar() or 0}

@router.post("/importar")
async def importar_catalogo(
    archivo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede importar el catalogo")

    contenido = await archivo.read()
    nombre = (archivo.filename or "").lower()

    try:
        import openpyxl
    except ImportError:
        raise HTTPException(status_code=500, detail="Falta la libreria openpyxl en el servidor")

    filas = []
    hoja_usada = "(csv)"
    if nombre.endswith(".csv"):
        texto = contenido.decode("utf-8-sig", errors="ignore")
        lector = csv.reader(io.StringIO(texto))
        encabezado = None
        for fila in lector:
            if encabezado is None:
                encabezado = [_normalizar_col(c) for c in fila]
                continue
            filas.append(dict(zip(encabezado, fila)))
    else:
        wb = openpyxl.load_workbook(io.BytesIO(contenido), data_only=True)
        ws = None
        for nombre_hoja in wb.sheetnames:
            if "catalogo" in nombre_hoja.strip().lower():
                ws = wb[nombre_hoja]
                break
        if ws is None:
            ws = wb.active  # respaldo: si no encuentra una hoja "catalogo", usa la activa
        hoja_usada = ws.title
        filas_raw = list(ws.iter_rows(values_only=True))
        if not filas_raw:
            raise HTTPException(status_code=422, detail="El archivo esta vacio")
        encabezado = [_normalizar_col(c) for c in filas_raw[0]]
        for fila in filas_raw[1:]:
            filas.append(dict(zip(encabezado, fila)))

    resultado = await _upsert_catalogo(db, filas, user.get("sub") or "admin")
    resultado["ok"] = True
    resultado["hoja_usada"] = hoja_usada
    return resultado

@router.post("/sincronizar-drive")
async def sincronizar_desde_drive(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lee el catalogo directamente desde el Google Sheet compartido — sin
    necesidad de subir un archivo. Requiere que la hoja este compartida como
    'Cualquier persona con el enlace' (solo lectura)."""
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede sincronizar el catalogo")
    if not GOOGLE_SHEET_CATALOGO_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_SHEET_CATALOGO_ID no esta configurado en Railway")

    base = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_CATALOGO_ID}/gviz/tq"
    hoja = quote(GOOGLE_SHEET_CATALOGO_HOJA)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # JSON primero: trae el valor numerico REAL de cada celda ("v"), no el
            # texto formateado de la hoja (que puede traer comas/puntos de otra region).
            r = await client.get(f"{base}?tqx=out:json&headers=1&sheet={hoja}", follow_redirects=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"No se pudo conectar a Google Sheets: {e}")

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Google Sheets respondio {r.status_code} — revisa que la hoja siga compartida "
                   f"como 'Cualquier persona con el enlace' y que la pestana se llame "
                   f"'{GOOGLE_SHEET_CATALOGO_HOJA}'"
        )
    if "<html" in r.text[:200].lower():
        raise HTTPException(
            status_code=502,
            detail="Google regreso una pagina de login en vez de los datos — la hoja dejo de ser publica, "
                   "revisa el permiso de 'Compartir'"
        )

    filas, fuente = _filas_desde_gviz_json(r.text), "google_sheets_json"
    if not filas:
        async with httpx.AsyncClient(timeout=30) as client:
            r2 = await client.get(f"{base}?tqx=out:csv&sheet={hoja}", follow_redirects=True)
        filas_raw = list(csv.reader(io.StringIO(r2.content.decode("utf-8-sig", errors="ignore"))))
        if not filas_raw:
            raise HTTPException(status_code=422, detail=f"La pestana '{GOOGLE_SHEET_CATALOGO_HOJA}' esta vacia")
        encabezado = [_normalizar_col(c) for c in filas_raw[0]]
        filas, fuente = [dict(zip(encabezado, f)) for f in filas_raw[1:]], "google_sheets_csv"

    resultado = await _upsert_catalogo(db, filas, user.get("sub") or "admin")
    resultado["ok"] = True
    resultado["fuente"] = fuente
    resultado["hoja_usada"] = GOOGLE_SHEET_CATALOGO_HOJA
    return resultado


# ═════════════════════ GESTION DEL CATALOGO (edicion local, historial, empaques adicionales) ═════════════════════
# Todo lo que se edita aqui se queda en esta base de datos; nada se escribe en Odoo ni en la hoja.
TIPOS_ADICIONAL = {"extension": "Extensión", "adicional": "Adicional", "accesorio": "Accesorio"}
ETIQ_CAMPO = {
    "descripcion": "Descripción", "peso_unit": "Peso unitario (kg)", "lr_unit": "Largo (cm)", "an_unit": "Ancho (cm)",
    "al_unit": "Alto (cm)", "m3_unit": "Volumen (m3)", "cm_cant": "Piezas por caja master", "cm_peso": "Peso caja master (kg)",
    "cm_lr": "Largo caja master (cm)", "cm_an": "Ancho caja master (cm)", "cm_al": "Alto caja master (cm)",
    "cm_m3": "Volumen caja master (m3)", "adicional": "Empaque adicional", "(alta)": "Alta del producto", "sat_codigo": "Código SAT", "sat_desc": "Descripción SAT",
}


def _exigir_edicion(user):
    if user.get("rol") not in ("admin", "gerente"):
        raise HTTPException(403, "Solo un gerente o administrador puede editar el catálogo.")


def _iso(dt):
    return (dt.isoformat() + "Z") if dt else None


def _ser_item(i: CatalogoItem) -> dict:
    return {
        "sku": i.sku, "descripcion": i.descripcion or "",
        "peso_unit": i.peso_unit or 0, "lr_unit": i.lr_unit or 0, "an_unit": i.an_unit or 0, "al_unit": i.al_unit or 0, "m3_unit": i.m3_unit or 0,
        "cm_cant": i.cm_cant or 0, "cm_peso": i.cm_peso or 0, "cm_lr": i.cm_lr or 0, "cm_an": i.cm_an or 0, "cm_al": i.cm_al or 0, "cm_m3": i.cm_m3 or 0,
        "sat_codigo": i.sat_codigo or "", "sat_desc": i.sat_desc or "",
        "campos_manuales": sorted(_manuales(i)), "origen": i.origen or "hoja",
        "editado_por": i.editado_por, "editado_en": _iso(i.editado_en),
    }


def _ser_adic(a: CatalogoAdicional) -> dict:
    return {"id": a.id, "sku_base": a.sku_base, "tipo": a.tipo, "tipo_nombre": TIPOS_ADICIONAL.get(a.tipo, a.tipo), "nombre": a.nombre,
            "corresponde_a": a.corresponde_a or "", "cantidad_por_equipo": a.cantidad_por_equipo or 1, "peso_kg": a.peso_kg or 0,
            "lr": a.lr or 0, "an": a.an or 0, "al": a.al or 0, "activo": bool(a.activo), "creado_por": a.creado_por,
            "creado_en": _iso(a.creado_en), "actualizado_en": _iso(a.actualizado_en)}


def _ser_hist(h: CatalogoHistorial) -> dict:
    return {"id": h.id, "sku": h.sku, "campo": h.campo, "campo_nombre": ETIQ_CAMPO.get(h.campo, h.campo),
            "anterior": h.valor_anterior, "nuevo": h.valor_nuevo, "origen": h.origen, "motivo": h.motivo,
            "usuario": h.usuario, "creado_en": _iso(h.creado_en)}


def _avisos(d: dict) -> list:
    """Observaciones de coherencia; no bloquean el guardado."""
    av = []
    if d["cm_cant"] > 0:
        if not d["cm_peso"]:
            av.append("La caja master no tiene peso capturado.")
        elif d["peso_unit"] and not (0.5 <= d["cm_peso"] / (d["peso_unit"] * d["cm_cant"]) <= 2):
            av.append("El peso de la caja master no cuadra con peso unitario x piezas por caja; revísalo.")
        if not (d["cm_lr"] and d["cm_an"] and d["cm_al"]):
            av.append("A la caja master le faltan medidas.")
    if not d["peso_unit"]:
        av.append("El producto no tiene peso unitario.")
    return av


def _numero(valor, campo):
    if valor is None or valor == "":
        return 0
    try:
        n = float(str(valor).replace(",", "."))
    except ValueError:
        raise HTTPException(400, f"{ETIQ_CAMPO.get(campo, campo)}: escribe un número.")
    if n != n or n < 0 or n > 1_000_000:
        raise HTTPException(400, f"{ETIQ_CAMPO.get(campo, campo)}: valor fuera de rango.")
    return n


@router.get("/gestion")
async def gestion_listar(buscar: str = "", filtro: str = "todos", limite: int = 60, pagina: int = 0,
                         db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    limite = max(1, min(limite, 200))
    con_adic = select(CatalogoAdicional.sku_base).where(CatalogoAdicional.activo == True)  # noqa: E712
    q = select(CatalogoItem)
    if buscar.strip():
        p = f"%{buscar.strip()}%"
        q = q.where(CatalogoItem.sku.ilike(p) | CatalogoItem.descripcion.ilike(p))
    if filtro == "sin_master":
        q = q.where(or_(CatalogoItem.cm_cant == None, CatalogoItem.cm_cant <= 0))  # noqa: E711
    elif filtro == "editados":
        q = q.where(CatalogoItem.campos_manuales.is_not(None), CatalogoItem.campos_manuales != "[]")
    elif filtro == "con_adicionales":
        q = q.where(CatalogoItem.sku.in_(con_adic))
    elif filtro == "sin_peso":
        q = q.where(or_(CatalogoItem.peso_unit == None, CatalogoItem.peso_unit <= 0))  # noqa: E711
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    filas = list((await db.execute(q.order_by(CatalogoItem.sku).limit(limite).offset(max(0, pagina) * limite))).scalars())
    skus = [f.sku for f in filas]
    n_adic = {}
    if skus:
        r = await db.execute(select(CatalogoAdicional.sku_base, func.count()).where(CatalogoAdicional.sku_base.in_(skus), CatalogoAdicional.activo == True)  # noqa: E712
                             .group_by(CatalogoAdicional.sku_base))
        n_adic = {k: n for k, n in r.all()}
    items = []
    for f in filas:
        d = _ser_item(f)
        d["adicionales"] = n_adic.get(f.sku, 0)
        items.append(d)
    resumen = {
        "total": (await db.execute(select(func.count(CatalogoItem.sku)))).scalar() or 0,
        "sin_master": (await db.execute(select(func.count(CatalogoItem.sku)).where(or_(CatalogoItem.cm_cant == None, CatalogoItem.cm_cant <= 0)))).scalar() or 0,  # noqa: E711
        "editados": (await db.execute(select(func.count(CatalogoItem.sku)).where(CatalogoItem.campos_manuales.is_not(None), CatalogoItem.campos_manuales != "[]"))).scalar() or 0,
        "con_adicionales": len((await db.execute(select(CatalogoAdicional.sku_base).where(CatalogoAdicional.activo == True).distinct())).all()),  # noqa: E712
    }
    return {"total": total, "pagina": pagina, "limite": limite, "items": items, "resumen": resumen,
            "puede_editar": user.get("rol") in ("admin", "gerente")}


@router.get("/gestion/historial")
async def gestion_historial_global(sku: str = "", usuario: str = "", limite: int = 100,
                                   db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    q = select(CatalogoHistorial).order_by(CatalogoHistorial.creado_en.desc(), CatalogoHistorial.id.desc()).limit(max(1, min(limite, 500)))
    if sku.strip():
        q = q.where(CatalogoHistorial.sku.ilike(f"%{sku.strip()}%"))
    if usuario.strip():
        q = q.where(CatalogoHistorial.usuario == usuario.strip())
    return [_ser_hist(h) for h in (await db.execute(q)).scalars()]


async def _item_o_404(db, sku):
    i = await db.get(CatalogoItem, (sku or "").strip().upper())
    if not i:
        raise HTTPException(404, "Ese producto no está en el catálogo.")
    return i


@router.get("/gestion/{sku}")
async def gestion_detalle(sku: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    i = await _item_o_404(db, sku)
    ad = (await db.execute(select(CatalogoAdicional).where(CatalogoAdicional.sku_base == i.sku).order_by(CatalogoAdicional.creado_en))).scalars()
    hi = (await db.execute(select(CatalogoHistorial).where(CatalogoHistorial.sku == i.sku)
                           .order_by(CatalogoHistorial.creado_en.desc(), CatalogoHistorial.id.desc()).limit(200))).scalars()
    d = _ser_item(i)
    return {"item": d, "avisos": _avisos(d), "adicionales": [_ser_adic(a) for a in ad], "historial": [_ser_hist(h) for h in hi],
            "puede_editar": user.get("rol") in ("admin", "gerente"), "etiquetas": ETIQ_CAMPO}


class CatalogoEditarIn(BaseModel):
    campos: dict
    motivo: str = ""


class CatalogoNuevoIn(BaseModel):
    sku: str
    descripcion: str = ""
    campos: dict = {}
    motivo: str = ""


def _aplicar_edicion(db, item: CatalogoItem, campos: dict, motivo: str, usuario: str, origen="manual") -> list:
    cambiados = []
    manual = _manuales(item)
    for campo, valor in campos.items():
        if campo not in CAMPOS_EDITABLES:
            raise HTTPException(400, f"Campo no editable: {campo}")
        if campo in CAMPOS_TXT:
            nuevo = (str(valor).strip() if valor is not None else "") or None
            if campo == "descripcion":
                nuevo = nuevo or ""
        elif campo in CAMPOS_INT:
            nuevo = int(round(_numero(valor, campo)))
        else:
            nuevo = _numero(valor, campo)
        ant = getattr(item, campo)
        if _igual(ant, nuevo):
            continue
        db.add(CatalogoHistorial(sku=item.sku, campo=campo, valor_anterior=_txt(ant), valor_nuevo=_txt(nuevo),
                                 origen=origen, motivo=motivo or None, usuario=usuario))
        setattr(item, campo, nuevo)
        manual.add(campo)
        cambiados.append(campo)
    # volumen calculado a partir de las medidas (cm -> m3) cuando cambian y no se capturo a mano
    for pref, m3c in (("", "m3_unit"), ("cm_", "cm_m3")):
        dims = [pref + "lr" if pref else "lr_unit", pref + "an" if pref else "an_unit", pref + "al" if pref else "al_unit"]
        if any(d in cambiados for d in dims) and m3c not in cambiados:
            lr, an, al = (getattr(item, d) or 0 for d in dims)
            if lr and an and al:
                v = round(lr * an * al / 1_000_000, 6)
                ant = getattr(item, m3c)
                if not _igual(ant, v):
                    db.add(CatalogoHistorial(sku=item.sku, campo=m3c, valor_anterior=_txt(ant), valor_nuevo=_txt(v), origen=origen,
                                             motivo="Calculado de las medidas", usuario=usuario))
                    setattr(item, m3c, v)
                    manual.add(m3c)
                    cambiados.append(m3c)
    if cambiados:
        item.campos_manuales = json.dumps(sorted(manual))
        item.origen = "manual" if item.origen == "manual" else (item.origen or "hoja")
        item.editado_por = usuario
        item.editado_en = func.now()
        item.actualizado_en = func.now()
    return cambiados


@router.put("/gestion/{sku}")
async def gestion_editar(sku: str, body: CatalogoEditarIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir_edicion(user)
    if len(body.motivo.strip()) < 3:
        raise HTTPException(400, "Escribe el motivo del cambio (para el historial).")
    i = await _item_o_404(db, sku)
    cambiados = _aplicar_edicion(db, i, body.campos, body.motivo.strip(), user["sub"])
    await db.commit()
    await db.refresh(i)
    d = _ser_item(i)
    return {"ok": True, "cambiados": cambiados, "item": d, "avisos": _avisos(d)}


@router.post("/gestion")
async def gestion_alta(body: CatalogoNuevoIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir_edicion(user)
    sku = body.sku.strip().upper()
    if not re.match(r"^[A-Z0-9][A-Z0-9._\-]{0,38}$", sku):
        raise HTTPException(400, "Clave inválida: usa letras, números y . _ -")
    if await db.get(CatalogoItem, sku):
        raise HTTPException(409, "Esa clave ya existe en el catálogo.")
    i = CatalogoItem(sku=sku, descripcion=body.descripcion.strip(), origen="manual", peso_unit=0, cm_cant=0)
    db.add(i)
    db.add(CatalogoHistorial(sku=sku, campo="(alta)", valor_nuevo=body.descripcion.strip(), origen="alta",
                             motivo=body.motivo.strip() or "Alta manual", usuario=user["sub"]))
    _aplicar_edicion(db, i, body.campos, body.motivo.strip() or "Alta manual", user["sub"])
    i.campos_manuales = i.campos_manuales or "[]"
    await db.commit()
    await db.refresh(i)
    d = _ser_item(i)
    return {"ok": True, "item": d, "avisos": _avisos(d)}


class LiberarIn(BaseModel):
    campos: list


@router.post("/gestion/{sku}/liberar")
async def gestion_liberar(sku: str, body: LiberarIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Quita la protección a los campos para que la hoja vuelva a actualizarlos en la siguiente sincronización."""
    _exigir_edicion(user)
    i = await _item_o_404(db, sku)
    manual = _manuales(i)
    quitar = [c for c in body.campos if c in manual]
    for c in quitar:
        manual.discard(c)
        db.add(CatalogoHistorial(sku=i.sku, campo=c, valor_anterior=None, valor_nuevo=None, origen="manual",
                                 motivo="Se devolvió el campo a la sincronización de la hoja", usuario=user["sub"]))
    i.campos_manuales = json.dumps(sorted(manual))
    await db.commit()
    return {"ok": True, "liberados": quitar}


# ── empaques adicionales (extensiones / adicionales / accesorios) ──
class AdicionalIn(BaseModel):
    tipo: str = "extension"
    nombre: str
    corresponde_a: str = ""
    cantidad_por_equipo: int = 1
    peso_kg: float = 0
    lr: float = 0
    an: float = 0
    al: float = 0
    activo: bool = True
    motivo: str = ""


def _validar_adicional(b: AdicionalIn):
    if b.tipo not in TIPOS_ADICIONAL:
        raise HTTPException(400, "Tipo inválido.")
    if not b.nombre.strip():
        raise HTTPException(400, "Escribe el nombre del empaque adicional.")
    if not b.corresponde_a.strip():
        raise HTTPException(400, "Indica a qué corresponde (qué parte, función o pedido es).")
    if not (1 <= b.cantidad_por_equipo <= 50):
        raise HTTPException(400, "La cantidad por equipo debe estar entre 1 y 50.")
    if min(b.peso_kg, b.lr, b.an, b.al) < 0:
        raise HTTPException(400, "Peso y medidas no pueden ser negativos.")


@router.post("/gestion/{sku}/adicionales")
async def adicional_crear(sku: str, body: AdicionalIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir_edicion(user)
    i = await _item_o_404(db, sku)
    _validar_adicional(body)
    a = CatalogoAdicional(sku_base=i.sku, tipo=body.tipo, nombre=body.nombre.strip(), corresponde_a=body.corresponde_a.strip(),
                          cantidad_por_equipo=body.cantidad_por_equipo, peso_kg=body.peso_kg, lr=body.lr, an=body.an, al=body.al,
                          activo=True, creado_por=user["sub"])
    db.add(a)
    db.add(CatalogoHistorial(sku=i.sku, campo="adicional", valor_nuevo=f"{TIPOS_ADICIONAL[body.tipo]}: {body.nombre.strip()} (x{body.cantidad_por_equipo}) - {body.corresponde_a.strip()}",
                             origen="adicional", motivo=body.motivo.strip() or "Empaque adicional declarado", usuario=user["sub"]))
    await db.commit()
    await db.refresh(a)
    return {"ok": True, "adicional": _ser_adic(a)}


@router.put("/gestion/adicionales/{adic_id}")
async def adicional_editar(adic_id: str, body: AdicionalIn, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    _exigir_edicion(user)
    a = await db.get(CatalogoAdicional, adic_id)
    if not a:
        raise HTTPException(404, "No existe ese empaque adicional.")
    _validar_adicional(body)
    antes = f"{TIPOS_ADICIONAL.get(a.tipo, a.tipo)}: {a.nombre} (x{a.cantidad_por_equipo}) - {a.corresponde_a or ''}" + ("" if a.activo else " [inactivo]")
    a.tipo, a.nombre, a.corresponde_a = body.tipo, body.nombre.strip(), body.corresponde_a.strip()
    a.cantidad_por_equipo, a.peso_kg, a.lr, a.an, a.al, a.activo = body.cantidad_por_equipo, body.peso_kg, body.lr, body.an, body.al, body.activo
    a.actualizado_en = func.now()
    despues = f"{TIPOS_ADICIONAL[a.tipo]}: {a.nombre} (x{a.cantidad_por_equipo}) - {a.corresponde_a}" + ("" if a.activo else " [inactivo]")
    if antes != despues:
        db.add(CatalogoHistorial(sku=a.sku_base, campo="adicional", valor_anterior=antes, valor_nuevo=despues, origen="adicional",
                                 motivo=body.motivo.strip() or "Cambio en empaque adicional", usuario=user["sub"]))
    await db.commit()
    await db.refresh(a)
    return {"ok": True, "adicional": _ser_adic(a)}


@router.get("/gestion/resumen/avisos")
async def gestion_avisos(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Productos con caja master incompleta o sin peso, para que el gerente los complete."""
    res = await db.execute(select(CatalogoItem).where(CatalogoItem.cm_cant > 0).order_by(CatalogoItem.sku))
    out = []
    for i in res.scalars():
        a = _avisos(_ser_item(i))
        if a:
            out.append({"sku": i.sku, "descripcion": i.descripcion, "avisos": a})
    return {"total": len(out), "items": out[:300]}
