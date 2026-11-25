from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header, BackgroundTasks, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_
from pydantic import BaseModel
from typing import Optional
from urllib.parse import quote
import io, os, csv, re, json, httpx
from datetime import datetime

from database import get_db
from models.models import CatalogoItem, CatalogoHistorial, CatalogoAdicional, CatalogoOdoo
from services.auth import verificar_token

router = APIRouter()

# Hoja de Google Sheets con el catalogo — via variables de entorno para
# poder cambiarla sin tocar codigo. Requiere que este compartida como
# "Cualquier persona con el enlace" (solo lectura), sin eso Google
# regresa una pagina de login en vez del CSV.
GOOGLE_SHEET_CATALOGO_ID   = os.getenv("GOOGLE_SHEET_CATALOGO_ID", "12Ewr2SvJXRt-g5HqGo-JrnJ07mv3vXboHU7Y2Gfkedw")
GOOGLE_SHEET_CATALOGO_HOJA = os.getenv("GOOGLE_SHEET_CATALOGO_HOJA", "CATALOGO_V2")

from services.permisos import usuario_actual


async def get_current_user(user: dict = Depends(usuario_actual)):
    """Catalogo compartido: entra quien tenga Embarques o Refacciones. El rol efectivo es el mas alto de los dos."""
    roles = [user["modulos"].get(m) for m in ("embarques", "refacciones")]
    if not any(roles):
        raise HTTPException(status_code=403, detail="No tienes acceso a este modulo")
    rol = "admin" if "admin" in roles else "gerente" if "gerente" in roles else "operador"
    return {**user, "rol": rol, "modulo": "embarques"}

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


def _json(txt, defecto):
    try:
        v = json.loads(txt) if txt else defecto
        return v if isinstance(v, type(defecto)) else defecto
    except Exception:
        return defecto


CAMPOS_ODOO = ("peso_unit", "m3_unit", "lr_unit", "an_unit", "al_unit", "cm_cant", "cm_lr", "cm_an", "cm_al")


def _recalcular(db, item, hoja: dict, odoo, usuario: str, motivo_base: str) -> int:
    """Fija el valor vigente de cada campo con la prioridad: editado aqui > Odoo (si tiene dato) > hoja.
    Registra en el historial lo que cambie por esta causa y deja anotada la fuente de cada campo."""
    manual = _manuales(item)
    fuentes = _json(item.fuentes, {})
    cambios = 0
    for campo in CAMPOS_FLOAT + CAMPOS_INT:
        if campo in manual:
            fuentes[campo] = "manual"
            continue
        ov = (odoo or {}).get(campo) if (odoo or {}).get("encontrado") else None
        hv = hoja.get(campo)
        if ov and ov > 0:
            nuevo, f = ov, "odoo"
        elif hv and hv > 0:
            nuevo, f = hv, "hoja"
        else:
            nuevo, f = (hv or 0), "sin_dato"
        if campo in CAMPOS_INT:
            nuevo = int(round(nuevo))
        ant = getattr(item, campo)
        if not _igual(ant, nuevo):
            db.add(CatalogoHistorial(sku=item.sku, campo=campo, valor_anterior=_txt(ant), valor_nuevo=_txt(nuevo),
                                     origen=("odoo" if f == "odoo" else "hoja"), motivo=motivo_base, usuario=usuario))
            setattr(item, campo, nuevo)
            cambios += 1
        fuentes[campo] = f
    item.fuentes = json.dumps(fuentes)
    return cambios


async def _snapshots_odoo(db) -> dict:
    r = await db.execute(select(CatalogoOdoo))
    out = {}
    for o in r.scalars():
        d = _json(o.datos, {})
        d["encontrado"] = bool(o.encontrado)
        d["odoo_id"] = o.odoo_id
        out[o.sku] = d
    return out


async def _upsert_catalogo(db: AsyncSession, filas: list, usuario: str = "sistema", commit: bool = True) -> dict:
    """Recibe una lista de dicts ya normalizados (claves como 'sku', 'cm_cant', etc.) y hace insert/update en la
    tabla catalogo. Guarda lo que dice la hoja y calcula el valor vigente (editado aqui > Odoo > hoja); cada cambio
    queda en el historial. Compartido entre /importar y /sincronizar-drive."""
    insertados, actualizados, omitidos, protegidos = 0, 0, 0, 0
    cambios = 0
    snaps = await _snapshots_odoo(db)
    for fila in filas:
        sku = str(fila.get("sku") or "").strip().upper()
        if not sku:
            omitidos += 1
            continue

        existente = await db.execute(select(CatalogoItem).where(CatalogoItem.sku == sku))
        item = existente.scalar_one_or_none()
        es_nuevo = item is None
        if es_nuevo:
            item = CatalogoItem(sku=sku, origen="hoja", linea=fila.get("linea") or None)
            db.add(item)

        # lo que dice la hoja (descripcion y SAT solo vienen de la hoja)
        desc = str(fila.get("descripcion") or (item.descripcion or ""))
        sat_c = str(fila.get("sat_codigo") or "") or None
        sat_d = str(fila.get("sat_desc") or "") or None
        hoja = {c: _num(fila.get(c)) for c in CAMPOS_FLOAT}
        for c in CAMPOS_INT:
            hoja[c] = _num(fila.get(c), es_entero=True)
        manual = set() if es_nuevo else _manuales(item)
        for campo, val in (("descripcion", desc), ("sat_codigo", sat_c), ("sat_desc", sat_d)):
            if campo in manual:
                continue
            ant = getattr(item, campo)
            if es_nuevo:
                setattr(item, campo, val)
            elif not _igual(ant, val):
                db.add(CatalogoHistorial(sku=sku, campo=campo, valor_anterior=_txt(ant), valor_nuevo=_txt(val), origen="hoja",
                                         motivo="Sincronizacion / importacion", usuario=usuario))
                setattr(item, campo, val)
                cambios += 1
        item.valores_hoja = json.dumps(hoja)
        for campo in manual:
            if campo in hoja and not _igual(getattr(item, campo), hoja[campo]):
                protegidos += 1
        cambios += _recalcular(db, item, hoja, snaps.get(sku), usuario, "Sincronizacion / importacion")

        if es_nuevo:
            db.add(CatalogoHistorial(sku=sku, campo="(alta)", valor_nuevo=desc, origen="hoja", motivo="Alta desde la hoja", usuario=usuario))
            insertados += 1
        else:
            actualizados += 1

    if commit:
        await db.commit()
    else:
        await db.flush()
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
        "campos_manuales": sorted(_manuales(i)), "origen": i.origen or "hoja", "linea": i.linea or "equipo",
        "fuentes": _json(i.fuentes, {}),
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
async def gestion_listar(buscar: str = "", filtro: str = "todos", linea: str = "", limite: int = 60, pagina: int = 0,
                         db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    limite = max(1, min(limite, 200))
    con_adic = select(CatalogoAdicional.sku_base).where(CatalogoAdicional.activo == True)  # noqa: E712
    q = select(CatalogoItem)
    if linea == "refacciones":
        q = q.where(CatalogoItem.linea == "refaccion")
    elif linea == "equipos":
        q = q.where(or_(CatalogoItem.linea == None, CatalogoItem.linea != "refaccion"))  # noqa: E711
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
        "refacciones": (await db.execute(select(func.count(CatalogoItem.sku)).where(CatalogoItem.linea == "refaccion"))).scalar() or 0,
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
        fu = _json(item.fuentes, {})
        for c in cambiados:
            fu[c] = "manual"
        item.fuentes = json.dumps(fu)
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
    if quitar and i.valores_hoja:
        # el campo vuelve a tomar el valor de Odoo (si tiene) o de la hoja
        snap = (await _snapshots_odoo(db)).get(i.sku)
        _recalcular(db, i, _json(i.valores_hoja, {}), snap, user["sub"], "Se devolvió el campo a Odoo / hoja")
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


# ═════════════════════ ODOO (solo lectura) + COMPARACION + EXPORTACION ═════════════════════
# Prioridad de cada dato del producto: editado aqui > Odoo (si tiene dato) > hoja de Google.
ETIQ_CAMPO.update({"lr_unit": "Largo (cm)"})
CAMPOS_COMPARA = ("peso_unit", "m3_unit", "lr_unit", "an_unit", "al_unit", "cm_cant", "cm_lr", "cm_an", "cm_al")
_estado_odoo = {"corriendo": False, "total": 0, "hechos": 0, "inicio": None, "fin": None, "error": None, "resultado": None, "por": None}


@router.get("/gestion/odoo/diagnostico")
async def odoo_diagnostico(sku: str = "", user: dict = Depends(get_current_user)):
    """Que campos de peso / medidas / empaques existen en este Odoo (y, opcional, lo que devuelve para un producto)."""
    _exigir_edicion(user)
    from services import odoo as odoo_srv
    odoo_srv.reset_cache_campos()
    try:
        return await odoo_srv.diagnostico_catalogo(sku)
    except Exception as e:
        raise HTTPException(502, f"No se pudo consultar Odoo: {e}")


@router.get("/gestion/odoo/estado")
async def odoo_estado(db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    ult = (await db.execute(select(func.max(CatalogoOdoo.sincronizado_en)))).scalar()
    n = (await db.execute(select(func.count(CatalogoOdoo.sku)))).scalar() or 0
    enc = (await db.execute(select(func.count(CatalogoOdoo.sku)).where(CatalogoOdoo.encontrado == True))).scalar() or 0  # noqa: E712
    return {**_estado_odoo, "ultima_sincronizacion": _iso(ult), "productos_consultados": n, "encontrados": enc}


async def _trabajo_sincronizar_odoo(usuario: str):
    from database import AsyncSessionLocal
    from services import odoo as odoo_srv
    st = _estado_odoo
    st.update({"corriendo": True, "hechos": 0, "total": 0, "error": None, "resultado": None, "inicio": _iso(datetime.utcnow()), "fin": None, "por": usuario})
    try:
        odoo_srv.reset_cache_campos()
        async with AsyncSessionLocal() as db:
            skus = [r[0] for r in (await db.execute(select(CatalogoItem.sku).order_by(CatalogoItem.sku))).all()]
        st["total"] = len(skus)
        res = {"encontrados": 0, "no_encontrados": 0, "cambios_registrados": 0, "con_peso": 0, "con_empaque": 0, "empaques_ambiguos": 0}
        for k in range(0, len(skus), 80):
            lote = skus[k:k + 80]
            datos = await odoo_srv.leer_catalogo_odoo(lote)
            async with AsyncSessionLocal() as db:
                for sku in lote:
                    d = datos.get(sku) or {"encontrado": False}
                    snap = await db.get(CatalogoOdoo, sku)
                    if not snap:
                        snap = CatalogoOdoo(sku=sku)
                        db.add(snap)
                    snap.encontrado = bool(d.get("encontrado"))
                    snap.odoo_id = d.get("odoo_id")
                    snap.datos = json.dumps({k2: v for k2, v in d.items() if k2 not in ("encontrado", "odoo_id")})
                    snap.sincronizado_en = datetime.utcnow()
                    if d.get("encontrado"):
                        res["encontrados"] += 1
                        res["con_peso"] += 1 if (d.get("peso_unit") or 0) > 0 else 0
                        res["con_empaque"] += 1 if (d.get("cm_cant") or 0) > 0 else 0
                        res["empaques_ambiguos"] += 1 if d.get("empaque_ambiguo") else 0
                    else:
                        res["no_encontrados"] += 1
                    item = await db.get(CatalogoItem, sku)
                    if item and item.valores_hoja:
                        res["cambios_registrados"] += _recalcular(db, item, _json(item.valores_hoja, {}), {**d}, usuario, "Sincronización con Odoo")
                await db.commit()
            st["hechos"] = min(len(skus), k + 80)
        st["resultado"] = res
    except Exception as e:
        st["error"] = str(e)[:300]
    finally:
        st["corriendo"] = False
        st["fin"] = _iso(datetime.utcnow())


@router.post("/gestion/odoo/sincronizar")
async def odoo_sincronizar(fondo: BackgroundTasks, user: dict = Depends(get_current_user)):
    """Lee de Odoo (solo lectura) peso, volumen, medidas y empaques de todos los productos del catalogo."""
    _exigir_edicion(user)
    if _estado_odoo["corriendo"]:
        raise HTTPException(409, "Ya hay una sincronización con Odoo en curso.")
    fondo.add_task(_trabajo_sincronizar_odoo, user["sub"])
    _estado_odoo.update({"corriendo": True, "hechos": 0, "total": 0, "error": None, "resultado": None})
    return {"ok": True}


def _clasifica(local, odoo, campo):
    """igual | diferente | solo_local | solo_odoo | sin_dato"""
    l, o = (local or 0), (odoo or 0)
    if l > 0 and o > 0:
        tol = 0.0 if campo == "cm_cant" else max(0.01 * max(l, o), 1e-6)
        return "igual" if abs(l - o) <= tol else "diferente"
    if l > 0:
        return "solo_local"
    if o > 0:
        return "solo_odoo"
    return "sin_dato"


def _posible_unidad(l, o):
    if l and o and l > 0 and o > 0:
        r = o / l
        for f in (1000, 100, 10, 0.1, 0.01, 0.001):
            if abs(r - f) / f < 0.05:
                return True
    return False


async def _filas_comparacion(db):
    items = list((await db.execute(select(CatalogoItem).order_by(CatalogoItem.sku))).scalars())
    snaps = await _snapshots_odoo(db)
    filas = []
    for i in items:
        sn = snaps.get(i.sku)
        manual = _manuales(i)
        hoja = _json(i.valores_hoja, {})
        row = {"sku": i.sku, "descripcion": i.descripcion or "", "en_odoo": bool(sn and sn.get("encontrado")), "consultado": sn is not None,
               "odoo_id": (sn or {}).get("odoo_id"), "campos": {}, "empaque_ambiguo": bool((sn or {}).get("empaque_ambiguo"))}
        estados = []
        for c in CAMPOS_COMPARA:
            local = getattr(i, c) if c in manual else (hoja.get(c) if hoja else getattr(i, c))
            ov = (sn or {}).get(c) if row["en_odoo"] else None
            est = _clasifica(local, ov, c) if row["en_odoo"] else ("solo_local" if (local or 0) > 0 else "sin_dato")
            row["campos"][c] = {"local": local or 0, "odoo": ov or 0, "estado": est, "editado": c in manual,
                                "unidad_dudosa": est == "diferente" and _posible_unidad(local, ov)}
            estados.append(est)
        if not row["consultado"]:
            row["estado"] = "sin_consultar"
        elif not row["en_odoo"]:
            row["estado"] = "no_en_odoo"
        elif "diferente" in estados:
            row["estado"] = "diferencias"
        elif "solo_local" in estados:
            row["estado"] = "falta_en_odoo"
        elif all(e in ("igual", "solo_odoo", "sin_dato") for e in estados) and any(e in ("igual", "solo_odoo") for e in estados):
            row["estado"] = "coincide"
        else:
            row["estado"] = "sin_datos"
        filas.append(row)
    return filas


@router.get("/gestion/odoo/comparar")
async def comparar(estado: str = "diferencias", buscar: str = "", limite: int = 60, pagina: int = 0,
                   db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    filas = await _filas_comparacion(db)
    resumen = {k: 0 for k in ("total", "sin_consultar", "no_en_odoo", "diferencias", "falta_en_odoo", "coincide", "sin_datos")}
    por_campo = {c: {"igual": 0, "diferente": 0, "solo_local": 0, "solo_odoo": 0, "sin_dato": 0} for c in CAMPOS_COMPARA}
    for r in filas:
        resumen["total"] += 1
        resumen[r["estado"]] += 1
        if r["en_odoo"]:
            for c, v in r["campos"].items():
                por_campo[c][v["estado"]] += 1
    sel = [r for r in filas if estado in ("todos", "", r["estado"])]
    if buscar.strip():
        b = buscar.strip().lower()
        sel = [r for r in sel if b in r["sku"].lower() or b in (r["descripcion"] or "").lower()]
    limite = max(1, min(limite, 200))
    page = sel[pagina * limite:(pagina + 1) * limite]
    return {"resumen": resumen, "por_campo": por_campo, "total": len(sel), "limite": limite, "pagina": pagina, "items": page,
            "estado": {k: _estado_odoo[k] for k in ("corriendo", "hechos", "total", "error", "resultado")}}


def _csv(filas, cols) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for f in filas:
        w.writerow([("" if v is None else v) for v in f])
    return buf.getvalue()


@router.get("/gestion/exportar/{tipo}")
async def exportar(tipo: str, incluir: str = "faltantes", db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """CSV para cargar a Odoo despues (importacion nativa de Odoo). Tipos: productos | empaques | no_en_odoo | completo.
    incluir=faltantes (solo donde Odoo no tiene el dato) o todo (tambien donde difiere)."""
    _exigir_edicion(user)
    filas = await _filas_comparacion(db)
    snaps = await _snapshots_odoo(db)
    items = {i.sku: i for i in (await db.execute(select(CatalogoItem))).scalars()}
    cab = {"Content-Disposition": f'attachment; filename="{tipo}.csv"'}

    def devolver(texto):
        return Response(content="﻿" + texto, media_type="text/csv; charset=utf-8", headers=cab)

    if tipo == "no_en_odoo":
        rows = [(r["sku"], r["descripcion"], items[r["sku"]].peso_unit, items[r["sku"]].cm_cant) for r in filas if r["estado"] == "no_en_odoo"]
        return devolver(_csv(rows, ["default_code", "name", "weight", "piezas_caja_master"]))

    if tipo == "productos":
        hay_dims = any((snaps.get(r["sku"]) or {}).get("tiene_campos_dim") for r in filas)
        cols = [".id", "default_code", "weight", "volume"] + (["product_length", "product_width", "product_height"] if hay_dims else [])
        rows = []
        for r in filas:
            if not r["en_odoo"] or not r["odoo_id"]:
                continue
            sn = snaps.get(r["sku"]) or {}
            f = sn.get("dim_factor") or 1.0
            mapa = {"weight": "peso_unit", "volume": "m3_unit", "product_length": "lr_unit", "product_width": "an_unit", "product_height": "al_unit"}
            vals, hay = {}, False
            for col, campo in mapa.items():
                if col not in cols:
                    continue
                c = r["campos"][campo]
                suben = (c["estado"] == "solo_local") or (incluir == "todo" and c["estado"] == "diferente")
                if suben:
                    v = c["local"] / f if col.startswith("product_") else c["local"]
                    vals[col] = round(v, 6)
                    hay = True
            if hay:
                rows.append([r["odoo_id"], r["sku"]] + [vals.get(c, "") for c in cols[2:]])
        return devolver(_csv(rows, cols))

    if tipo == "empaques":
        from services import odoo as odoo_srv
        if not await odoo_srv._modelo_existe("product.packaging"):
            raise HTTPException(409, "Este Odoo no tiene el modelo de empaques (product.packaging). Pide el diagnóstico para adaptar la exportación.")
        rows = []
        for r in filas:
            if not r["en_odoo"] or not r["odoo_id"]:
                continue
            c = r["campos"]["cm_cant"]
            if c["estado"] == "solo_local" or (incluir == "todo" and c["estado"] == "diferente"):
                rows.append([r["odoo_id"], "Caja master", int(c["local"])])
        return devolver(_csv(rows, ["product_id/.id", "name", "qty"]))

    if tipo == "completo":
        ad = {}
        for a in (await db.execute(select(CatalogoAdicional).where(CatalogoAdicional.activo == True))).scalars():  # noqa: E712
            ad.setdefault(a.sku_base, []).append(f"{TIPOS_ADICIONAL.get(a.tipo, a.tipo)}: {a.nombre} x{a.cantidad_por_equipo} ({a.corresponde_a})")
        cols = ["sku", "descripcion"] + list(CAMPOS_FLOAT) + list(CAMPOS_INT) + ["sat_codigo", "sat_desc"] + ["fuente_" + c for c in CAMPOS_COMPARA] + ["en_odoo", "empaques_adicionales"]
        rows = []
        for sku, i in items.items():
            fu = _json(i.fuentes, {})
            r = next((x for x in filas if x["sku"] == sku), None)
            rows.append([sku, i.descripcion] + [getattr(i, c) for c in CAMPOS_FLOAT + CAMPOS_INT] + [i.sat_codigo, i.sat_desc]
                        + [fu.get(c, "") for c in CAMPOS_COMPARA] + [("si" if (r and r["en_odoo"]) else "no"), " | ".join(ad.get(sku, []))])
        return devolver(_csv(rows, cols))

    raise HTTPException(400, "Tipo de exportación inválido.")



@router.post("/gestion/importar-refacciones")
async def importar_refacciones(archivo: UploadFile = File(...), simular: bool = False,
                               db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    """Carga el archivo PESOS_Y_DIMENSIONES_REFACCIONES (hoja CONSOLIDADO): CODIGO, DESCRIPCION, PESO (gramos), LARGO, ANCHO, ALTO (cm).
    Solo toca la base de esta plataforma, nunca Odoo. Peso se convierte de g a kg. Respeta lo editado aqui y lo que ya viene de Odoo.
    simular=true devuelve el reporte sin guardar nada."""
    _exigir_edicion(user)
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(await archivo.read()), data_only=True)
    except Exception:
        raise HTTPException(422, "No se pudo leer el archivo. Sube el .xlsx de pesos y dimensiones.")
    ws = wb["CONSOLIDADO"] if "CONSOLIDADO" in wb.sheetnames else wb[wb.sheetnames[0]]
    filas_raw = list(ws.iter_rows(values_only=True))
    ie = next((i for i, r in enumerate(filas_raw[:10]) if r and any(str(c or "").strip().upper() == "CODIGO" for c in r)), None)
    if ie is None:
        raise HTTPException(422, "No encuentro el encabezado con la columna CODIGO.")
    enc = [re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", str(c or "")).strip().lower()) for c in filas_raw[ie]]
    def col(*nombres):
        for n in nombres:
            if n in enc:
                return enc.index(n)
        return None
    ic, idc, ip, il, ia, ih = col("codigo", "sku"), col("descripcion"), col("peso"), col("largo"), col("ancho"), col("alto")
    if None in (ic, ip, il, ia, ih):
        raise HTTPException(422, "Faltan columnas: se esperan CODIGO, PESO, LARGO, ANCHO y ALTO.")
    filas, vistos, dup, avisos = [], set(), [], []
    for r in filas_raw[ie + 1:]:
        cod = str(r[ic] or "").strip().upper()
        if not cod:
            continue
        if cod in vistos:
            dup.append(cod)
            continue
        vistos.add(cod)
        g, l, a, h = (_num(r[ip]), _num(r[il]), _num(r[ia]), _num(r[ih]))
        kg = round(g / 1000.0, 4) if g else 0
        m3 = round(l * a * h / 1_000_000, 6) if l and a and h else 0
        if not (l and a and h):
            avisos.append(f"{cod}: medidas incompletas")
        if kg and kg < 0.005:
            avisos.append(f"{cod}: peso muy bajo ({g} g)")
        filas.append({"sku": cod, "descripcion": str(r[idc] or "").strip() if idc is not None else "", "peso_unit": kg,
                      "lr_unit": l, "an_unit": a, "al_unit": h, "m3_unit": m3, "linea": "refaccion"})
    if not filas:
        raise HTTPException(422, "El archivo no trae filas con CODIGO.")
    existentes = {x for (x,) in (await db.execute(select(CatalogoItem.sku).where(CatalogoItem.sku.in_([f["sku"] for f in filas])))).all()}
    res = await _upsert_catalogo(db, filas, user["sub"], commit=not simular)
    if simular:
        await db.rollback()
    return {"simulacion": simular, **res, "ya_existian": len(existentes), "duplicados_en_archivo": dup[:20],
            "avisos": avisos[:40], "total_avisos": len(avisos), "nota": "Peso convertido de gramos a kg. Odoo no se modifica."}
