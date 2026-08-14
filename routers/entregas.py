from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import io, re

from database import get_db
from models.models import Entrega, Producto, Tarima, SistemaEnum, EstatusEntrega
from services.auth import verificar_token

router = APIRouter()

# ── REMITENTE POR COMERCIALIZADOR (para etiquetas) ────────────
REMITENTE_MAP = {
    "ECOR":   "EQUIPOS COREANOS SA DE CV",
    "Raiker": "AGROINDUSTRIAS RAIKER SA DE CV",
    "TDK":    "TDK INTERNATIONAL SA DE CV",
    "Korei":  "WORLD KOREI CORPORATION SA DE CV",
}
REMITENTE_DEFAULT = "GRUPO CLER"

def _remitente(comercializador: str) -> str:
    return REMITENTE_MAP.get((comercializador or "").strip(), REMITENTE_DEFAULT)

def _barcode_url(data: str, height: int = 50) -> str:
    import urllib.parse
    return (
        "https://barcode.tec-it.com/barcode.ashx?data="
        + urllib.parse.quote(data)
        + f"&code=Code128&dpi=200&unit=Fit&width=280&height={height}&quiet=0&color=%23000000"
    )

# ── DEPENDENCIA DE AUTH ──────────────────────────────────────
async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin autorizacion")
    payload = verificar_token(authorization.split(" ")[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    return payload

# ── SCHEMAS ───────────────────────────────────────────────────
class ProductoIn(BaseModel):
    clave:          str
    descripcion:    str
    cantidad_total: int
    unidad:         str = "PZA"
    es_extension:   bool = False

class EntregaIn(BaseModel):
    sistema:        str  # TAR | CS | MIX
    num_entrega:    str
    nombre_cliente: str
    rfc_cliente:    Optional[str] = ""
    direccion:      Optional[str] = ""
    orden:          Optional[str] = ""
    fecha_entrega:  Optional[str] = ""
    comercializador:Optional[str] = ""
    sucursal:       Optional[str] = ""
    fuente:         Optional[str] = "manual"
    productos:      List[ProductoIn]

class CerrarTarimaIn(BaseModel):
    categoria: Optional[str] = ""
    notas:     Optional[str] = ""
    largo_cm:  Optional[float] = 0
    ancho_cm:  Optional[float] = 0
    alto_cm:   Optional[float] = 0

class CrearTarimaIn(BaseModel):
    id_productos:  List[str] = []   # productos a incluir de una vez (opcional)
    peso_palet_kg: Optional[float] = 0

class AsignarTarimaIn(BaseModel):
    id_tarima: Optional[str] = None  # None = quitar de la tarima (vuelve a suelto)

class DimensionesIn(BaseModel):
    largo_cm: float = 0
    ancho_cm: float = 0
    alto_cm:  float = 0

# ── HELPERS ───────────────────────────────────────────────────
def _gen_id_entrega(sistema: str) -> str:
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{sistema}-{ts}"

def _gen_id_prod(id_entrega: str, idx: int) -> str:
    return f"{id_entrega}-P{idx:03d}"

def _gen_id_tarima(id_entrega: str, idx: int) -> str:
    return f"{id_entrega}-T{idx:03d}"

def _numero_tarima(id_tarima: str) -> int:
    m = re.search(r"-T(\d+)$", id_tarima)
    return int(m.group(1)) if m else 1

# ── LISTAR ENTREGAS ───────────────────────────────────────────
@router.get("/")
async def listar_entregas(
    sistema:  Optional[str] = None,
    estatus:  Optional[str] = None,
    limite:   int = 50,
    db:       AsyncSession = Depends(get_db),
    user:     dict = Depends(get_current_user)
):
    q = select(Entrega).order_by(Entrega.fecha_creacion.desc()).limit(limite)
    if sistema: q = q.where(Entrega.sistema == sistema)
    if estatus: q = q.where(Entrega.estatus == estatus)
    result = await db.execute(q)
    entregas = result.scalars().all()
    return [_serializar_entrega(e) for e in entregas]

# ── DETALLE DE ENTREGA ────────────────────────────────────────
@router.get("/{id_entrega}")
async def detalle_entrega(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    prods = await db.execute(select(Producto).where(Producto.id_entrega == id_entrega))
    tarimas = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    data = _serializar_entrega(entrega)
    data["productos"] = [_ser_prod(p) for p in prods.scalars()]
    data["tarimas"]   = [_ser_tarima(t) for t in tarimas.scalars()]
    return data

# ── CREAR ENTREGA ─────────────────────────────────────────────
@router.post("/")
async def crear_entrega(
    body: EntregaIn,
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    id_e = _gen_id_entrega(body.sistema)
    entrega = Entrega(
        id_entrega     = id_e,
        num_entrega    = body.num_entrega,
        sistema        = body.sistema,
        nombre_cliente = body.nombre_cliente,
        rfc_cliente    = body.rfc_cliente,
        direccion      = body.direccion,
        orden          = body.orden,
        fecha_entrega  = body.fecha_entrega,
        comercializador= body.comercializador,
        sucursal       = body.sucursal,
        fuente         = body.fuente,
        creado_por     = user["sub"]
    )
    db.add(entrega)
    for i, p in enumerate(body.productos, 1):
        db.add(Producto(
            id_producto    = _gen_id_prod(id_e, i),
            id_entrega     = id_e,
            clave          = p.clave.strip().upper(),
            descripcion    = p.descripcion,
            cantidad_total = p.cantidad_total,
            unidad         = p.unidad,
            es_extension   = p.es_extension
        ))
    await db.commit()
    return {"ok": True, "id_entrega": id_e, "total": len(body.productos)}

# ── PROCESAR PDF ──────────────────────────────────────────────
@router.post("/pdf")
async def procesar_pdf(
    archivo:   UploadFile = File(...),
    sistema:   str = "CS",
    comercializador: str = "",
    db:        AsyncSession = Depends(get_db),
    user:      dict = Depends(get_current_user)
):
    contenido = await archivo.read()
    from parsers.ecor import parsear_ecor
    from parsers.sap_raiker import parsear_sap_raiker
    from parsers.detector import detectar_tipo

    texto = _extraer_texto_pdf(contenido)
    tipo  = detectar_tipo(texto)

    if tipo == "SAP_RAIKER":
        datos = parsear_sap_raiker(texto, archivo.filename)
    else:
        datos = parsear_ecor(texto)

    if not datos.get("productos"):
        raise HTTPException(status_code=422, detail="No se encontraron productos en el PDF")

    datos["sistema"]  = sistema
    datos["fuente"]   = "pdf"
    datos["comercializador"] = datos.get("comercializador") or comercializador
    body = EntregaIn(**datos)
    return await crear_entrega(body, db, user)

def _extraer_texto_pdf(contenido: bytes) -> str:
    import pdfplumber, io
    texto = ""
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        for page in pdf.pages:
            texto += (page.extract_text() or "") + "\n"
    return texto

# ── CREAR TARIMA (agrupar productos) ──────────────────────────
@router.post("/{id_entrega}/tarimas")
async def crear_tarima(
    id_entrega: str,
    body:       CrearTarimaIn,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")

    conteo = await db.execute(select(func.count(Tarima.id_tarima)).where(Tarima.id_entrega == id_entrega))
    idx = (conteo.scalar() or 0) + 1
    id_t = _gen_id_tarima(id_entrega, idx)

    tarima = Tarima(
        id_tarima=id_t, id_entrega=id_entrega, estatus="abierta",
        peso_palet_kg=body.peso_palet_kg or 0
    )
    db.add(tarima)

    if body.id_productos:
        prods = await db.execute(
            select(Producto).where(Producto.id_producto.in_(body.id_productos), Producto.id_entrega == id_entrega)
        )
        for p in prods.scalars():
            p.id_tarima = id_t

    await db.commit()
    return {"ok": True, "id_tarima": id_t, "numero_tarima": idx}

# ── ASIGNAR / QUITAR PRODUCTO DE UNA TARIMA ───────────────────
@router.patch("/{id_entrega}/productos/{id_producto}/tarima")
async def asignar_tarima(
    id_entrega:  str,
    id_producto: str,
    body:        AsignarTarimaIn,
    db:          AsyncSession = Depends(get_db),
    user:        dict = Depends(get_current_user)
):
    result = await db.execute(
        select(Producto).where(Producto.id_producto == id_producto, Producto.id_entrega == id_entrega)
    )
    prod = result.scalar_one_or_none()
    if not prod:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    if body.id_tarima:
        t = await db.execute(select(Tarima).where(Tarima.id_tarima == body.id_tarima, Tarima.id_entrega == id_entrega))
        if not t.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Tarima no encontrada")

    prod.id_tarima = body.id_tarima
    await db.commit()
    return {"ok": True}

# ── ACTUALIZAR DIMENSIONES DE TARIMA ──────────────────────────
@router.patch("/{id_entrega}/tarimas/{id_tarima}/dimensiones")
async def actualizar_dimensiones(
    id_entrega: str,
    id_tarima:  str,
    body:       DimensionesIn,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    tarima.largo_cm = body.largo_cm
    tarima.ancho_cm = body.ancho_cm
    tarima.alto_cm  = body.alto_cm
    await db.commit()
    return {"ok": True}

# ── ELIMINAR TARIMA (los productos vuelven a sueltos) ─────────
@router.delete("/{id_entrega}/tarimas/{id_tarima}")
async def eliminar_tarima(
    id_entrega: str,
    id_tarima:  str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")

    prods = await db.execute(select(Producto).where(Producto.id_tarima == id_tarima))
    for p in prods.scalars():
        p.id_tarima = None

    await db.delete(tarima)
    await db.commit()
    return {"ok": True}

# ── DATOS COMPLETOS PARA ETIQUETA DE TARIMA ───────────────────
@router.get("/{id_entrega}/tarimas/{id_tarima}/etiqueta")
async def etiqueta_tarima(
    id_entrega: str,
    id_tarima:  str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")

    t = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = t.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")

    todas = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    total_tarimas = len(todas.scalars().all())

    prods = await db.execute(select(Producto).where(Producto.id_tarima == id_tarima))
    productos = [_ser_prod(p) for p in prods.scalars()]

    folio_limpio = re.sub(r"[^A-Z0-9\-]", "", (entrega.num_entrega or id_tarima).upper())
    numero_tarima = _numero_tarima(id_tarima)
    barcode_entrega = folio_limpio
    barcode_tarima  = f"{folio_limpio}-T{numero_tarima}"

    peso_palet  = tarima.peso_palet_kg or 0
    total_piezas = sum(p["cantidad_total"] for p in productos)

    return {
        "id_tarima":       tarima.id_tarima,
        "numero_tarima":   numero_tarima,
        "total_tarimas":   total_tarimas,
        "id_entrega":      entrega.id_entrega,
        "num_entrega":     entrega.num_entrega,
        "nombre_cliente":  entrega.nombre_cliente,
        "direccion":       entrega.direccion,
        "orden":           entrega.orden,
        "comercializador": entrega.comercializador,
        "remitente":       _remitente(entrega.comercializador),
        "sucursal":        entrega.sucursal,
        "fecha_entrega":   entrega.fecha_entrega,
        "estatus":         tarima.estatus,
        "productos":       productos,
        "total_piezas":    total_piezas,
        "peso_palet_kg":   peso_palet,
        # peso_neto se calculara cuando se sincronice el catalogo HMCK; por ahora 0
        "peso_neto_kg":    0,
        "peso_bruto_kg":   peso_palet,
        "largo_cm":        tarima.largo_cm or 0,
        "ancho_cm":        tarima.ancho_cm or 0,
        "alto_cm":         tarima.alto_cm or 0,
        "barcode_entrega":     barcode_entrega,
        "barcode_entrega_url": _barcode_url(barcode_entrega),
        "barcode_tarima":      barcode_tarima,
        "barcode_tarima_url":  _barcode_url(barcode_tarima),
    }

# ── TODAS LAS ETIQUETAS DE UNA ENTREGA (imprimir juntas) ──────
@router.get("/{id_entrega}/etiquetas")
async def todas_etiquetas(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    tarimas = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    ids = [t.id_tarima for t in tarimas.scalars()]
    if not ids:
        raise HTTPException(status_code=404, detail="Esta entrega no tiene tarimas")
    resultado = []
    for id_t in ids:
        resultado.append(await etiqueta_tarima(id_entrega, id_t, db, user))
    return resultado

# ── CERRAR TARIMA ─────────────────────────────────────────────
@router.post("/{id_entrega}/tarimas/{id_tarima}/cerrar")
async def cerrar_tarima(
    id_entrega: str,
    id_tarima:  str,
    body:       CerrarTarimaIn,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    tarima.estatus     = "cerrada"
    tarima.fecha_cierre= datetime.utcnow()
    if body.categoria:
        tarima.comentario = body.categoria + (f" — {body.notas}" if body.notas else "")
    if body.largo_cm: tarima.largo_cm = body.largo_cm
    if body.ancho_cm: tarima.ancho_cm = body.ancho_cm
    if body.alto_cm:  tarima.alto_cm  = body.alto_cm
    tarima.cerrado_por = user["sub"]
    await db.commit()
    return {"ok": True}

# ── REABRIR TARIMA ─────────────────────────────────────────────
@router.post("/{id_entrega}/tarimas/{id_tarima}/reabrir")
async def reabrir_tarima(
    id_entrega: str,
    id_tarima:  str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    tarima.estatus      = "abierta"
    tarima.fecha_cierre = None
    await db.commit()
    return {"ok": True}

# ── MARCAR ENTREGA COMPLETADA ─────────────────────────────────
@router.post("/{id_entrega}/completar")
async def completar_entrega(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    entrega.estatus = EstatusEntrega.completada
    await db.commit()
    return {"ok": True}

# ── SERIALIZERS ───────────────────────────────────────────────
def _serializar_entrega(e: Entrega) -> dict:
    return {
        "id_entrega":     e.id_entrega,
        "num_entrega":    e.num_entrega,
        "sistema":        e.sistema,
        "nombre_cliente": e.nombre_cliente,
        "direccion":      e.direccion,
        "orden":          e.orden,
        "fecha_entrega":  e.fecha_entrega,
        "fecha_creacion": str(e.fecha_creacion or ""),
        "estatus":        e.estatus,
        "comercializador":e.comercializador,
        "sucursal":       e.sucursal,
        "fuente":         e.fuente,
    }

def _ser_prod(p: Producto) -> dict:
    return {
        "id_producto":    p.id_producto,
        "id_entrega":     p.id_entrega,
        "id_tarima":      p.id_tarima,
        "clave":          p.clave,
        "descripcion":    p.descripcion,
        "cantidad_total": p.cantidad_total,
        "unidad":         p.unidad,
        "es_extension":   p.es_extension,
    }

def _ser_tarima(t: Tarima) -> dict:
    return {
        "id_tarima":      t.id_tarima,
        "id_entrega":     t.id_entrega,
        "numero_tarima":  _numero_tarima(t.id_tarima),
        "estatus":        t.estatus,
        "fecha_creacion": str(t.fecha_creacion or ""),
        "fecha_cierre":   str(t.fecha_cierre or ""),
        "comentario":     t.comentario,
        "cerrado_por":    t.cerrado_por,
        "peso_palet_kg":  t.peso_palet_kg or 0,
        "largo_cm":       t.largo_cm or 0,
        "ancho_cm":       t.ancho_cm or 0,
        "alto_cm":        t.alto_cm or 0,
    }
