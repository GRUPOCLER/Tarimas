from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import io, re, time

from database import get_db
from models.models import Entrega, Producto, Tarima, DetalleTarima, SistemaEnum, EstatusEntrega, LogAcceso, SolicitudReimpresion
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

def require_roles(*roles):
    async def dep(user: dict = Depends(get_current_user)):
        if user.get("rol") not in roles:
            raise HTTPException(status_code=403, detail="No tienes permisos para esta accion. Se requiere autorizacion de un Gerente.")
        return user
    return dep

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

class CrearTarimaIn(BaseModel):
    peso_palet_kg: Optional[float] = 0
    ids_entregas_fusionadas: Optional[List[str]] = None  # 2+ ids del mismo cliente

class FusionDetalleIn(BaseModel):
    ids_entregas: List[str]

class AsignacionIn(BaseModel):
    id_producto: str
    cantidad:    int

class AsignarLoteIn(BaseModel):
    asignaciones: List[AsignacionIn]

class CerrarTarimaIn(BaseModel):
    categoria: Optional[str] = ""
    notas:     Optional[str] = ""
    largo_cm:  Optional[float] = 0
    ancho_cm:  Optional[float] = 0
    alto_cm:   Optional[float] = 0

class DimensionesIn(BaseModel):
    largo_cm: float = 0
    ancho_cm: float = 0
    alto_cm:  float = 0

class MarcarImpresoIn(BaseModel):
    motivo: Optional[str] = None

class ExtensionIn(BaseModel):
    cantidad: int

class ActualizarEntregaIn(BaseModel):
    sucursal:        Optional[str] = None
    comercializador: Optional[str] = None
    direccion:       Optional[str] = None
    nombre_cliente:  Optional[str] = None

# ── HELPERS ───────────────────────────────────────────────────
def _gen_id_entrega(sistema: str) -> str:
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{sistema}-{ts}"

def _gen_id_prod(id_entrega: str, idx: int) -> str:
    return f"{id_entrega}-P{idx:03d}"

def _gen_id_tarima(id_entrega: str, idx: int) -> str:
    return f"{id_entrega}-T{idx:03d}"

def _gen_id_detalle(id_tarima: str) -> str:
    return f"{id_tarima}-D{int(time.time()*1000) % 1000000}"

def _numero_tarima(id_tarima: str) -> int:
    m = re.search(r"-T(\d+)$", id_tarima)
    return int(m.group(1)) if m else 1

# ── LISTAR ENTREGAS ───────────────────────────────────────────
@router.get("/")
async def listar_entregas(
    sistema:      Optional[str] = None,
    estatus:      Optional[str] = None,
    fuente:       Optional[str] = None,
    fecha_desde:  Optional[str] = None,
    fecha_hasta:  Optional[str] = None,
    limite:       int = 200,
    db:           AsyncSession = Depends(get_db),
    user:         dict = Depends(get_current_user)
):
    q = select(Entrega).order_by(Entrega.fecha_creacion.desc()).limit(limite)
    if sistema: q = q.where(Entrega.sistema == sistema)
    if estatus: q = q.where(Entrega.estatus == estatus)
    if fuente:  q = q.where(Entrega.fuente == fuente)
    if fecha_desde: q = q.where(func.date(Entrega.fecha_creacion) >= fecha_desde)
    if fecha_hasta: q = q.where(func.date(Entrega.fecha_creacion) <= fecha_hasta)
    result = await db.execute(q)
    entregas = result.scalars().all()

    # Cruzar con tarimas fusionadas para marcar que entregas estan agrupadas
    tarimas_fus = await db.execute(select(Tarima).where(Tarima.ids_entregas_fusionadas.is_not(None)))
    grupos: dict = {}       # id_entrega -> set(otros ids del mismo grupo)
    grupo_clave: dict = {}  # id_entrega -> clave de grupo compartida (para colorear igual en frontend)
    for t in tarimas_fus.scalars():
        ids = sorted(set(x for x in t.ids_entregas_fusionadas.split(",") if x))
        if not ids:
            continue
        clave = "|".join(ids)
        for id_e in ids:
            grupos.setdefault(id_e, set()).update(set(ids) - {id_e})
            grupo_clave[id_e] = clave

    ids_para_nombres = set()
    for otros in grupos.values():
        ids_para_nombres |= otros
    nombres = {}
    if ids_para_nombres:
        nr = await db.execute(select(Entrega.id_entrega, Entrega.num_entrega).where(Entrega.id_entrega.in_(list(ids_para_nombres))))
        nombres = {row[0]: row[1] for row in nr.all()}

    resultado = []
    for e in entregas:
        data = _serializar_entrega(e)
        otros_ids = grupos.get(e.id_entrega)
        if otros_ids:
            data["es_fusion"]    = True
            data["fusion_con"]   = [nombres.get(i, i) for i in otros_ids]
            data["grupo_fusion"] = grupo_clave.get(e.id_entrega)
        else:
            data["es_fusion"]    = False
            data["fusion_con"]   = []
            data["grupo_fusion"] = None
        resultado.append(data)
    return resultado

# ── DETALLE DE ENTREGA ────────────────────────────────────────
@router.get("/candidatas-fusion")
async def candidatas_fusion(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.estatus == "pendiente"))
    entregas = list(result.scalars())
    por_cliente: dict = {}
    for e in entregas:
        cliente = (e.nombre_cliente or "Sin nombre").strip()
        por_cliente.setdefault(cliente, []).append(_serializar_entrega(e))
    return [{"cliente": c, "entregas": lista} for c, lista in por_cliente.items() if len(lista) >= 2]

@router.post("/fusion/detalle")
async def fusion_detalle(
    body: FusionDetalleIn,
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    if len(body.ids_entregas) < 2:
        raise HTTPException(status_code=400, detail="Selecciona al menos 2 entregas")

    result = await db.execute(select(Entrega).where(Entrega.id_entrega.in_(body.ids_entregas)))
    entregas = list(result.scalars())
    if len(entregas) != len(set(body.ids_entregas)):
        raise HTTPException(status_code=404, detail="Alguna entrega no fue encontrada")
    clientes = set((e.nombre_cliente or "").strip() for e in entregas)
    if len(clientes) > 1:
        raise HTTPException(status_code=400, detail="Solo puedes fusionar entregas del mismo cliente")

    prods_r = await db.execute(select(Producto).where(Producto.id_entrega.in_(body.ids_entregas)))
    productos = list(prods_r.scalars())

    tarimas_ids = set()
    tarimas_todas = []
    for id_e in body.ids_entregas:
        for t in await _tarimas_relacionadas(db, id_e):
            if t.id_tarima not in tarimas_ids:
                tarimas_ids.add(t.id_tarima)
                tarimas_todas.append(t)

    detalles_r = await db.execute(
        select(DetalleTarima).where(DetalleTarima.id_tarima.in_(list(tarimas_ids)))
    ) if tarimas_ids else None
    detalles = list(detalles_r.scalars()) if detalles_r else []

    total_unidades  = sum(p.cantidad_total for p in productos)
    total_asignado  = sum(p.cantidad_asignada or 0 for p in productos)
    total_pendiente = sum(p.cantidad_pendiente if p.cantidad_pendiente is not None else p.cantidad_total for p in productos)

    return {
        "es_fusion":       True,
        "ids_entregas":    body.ids_entregas,
        "entregas":        [_serializar_entrega(e) for e in entregas],
        "cliente":         entregas[0].nombre_cliente,
        "productos":       [_ser_prod(p) for p in productos],
        "tarimas":         [_ser_tarima(t, [d for d in detalles if d.id_tarima == t.id_tarima]) for t in tarimas_todas],
        "total_unidades":  total_unidades,
        "total_asignado":  total_asignado,
        "total_pendiente": total_pendiente,
    }


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

    prods_r  = await db.execute(select(Producto).where(Producto.id_entrega == id_entrega))
    productos = list(prods_r.scalars())

    tarimas_r = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    tarimas = list(tarimas_r.scalars())

    if tarimas:
        detalles_r = await db.execute(
            select(DetalleTarima).where(DetalleTarima.id_tarima.in_([t.id_tarima for t in tarimas]))
        )
        detalles = list(detalles_r.scalars())
    else:
        detalles = []

    data = _serializar_entrega(entrega)
    data["productos"] = [_ser_prod(p) for p in productos]
    data["tarimas"] = [_ser_tarima(t, [d for d in detalles if d.id_tarima == t.id_tarima]) for t in tarimas]
    return data

# ── CREAR ENTREGA ─────────────────────────────────────────────
@router.post("/")
async def crear_entrega(
    body: EntregaIn,
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    # Evitar duplicados: misma OV/folio ya registrada en cualquier sistema
    num_norm = (body.num_entrega or "").strip().upper()
    orden_norm = (body.orden or "").strip().upper()
    condiciones = [func.upper(Entrega.num_entrega) == num_norm] if num_norm else []
    if orden_norm:
        condiciones.append(func.upper(Entrega.orden) == orden_norm)
    if condiciones:
        from sqlalchemy import or_
        dup = await db.execute(select(Entrega).where(or_(*condiciones)))
        existente = dup.scalars().first()
        if existente:
            raise HTTPException(
                status_code=409,
                detail=f"Ya existe una entrega para '{body.num_entrega or body.orden}' "
                       f"(folio {existente.id_entrega}, sistema {existente.sistema}). "
                       f"No se puede registrar de nuevo en otro sistema."
            )

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
        cant = p.cantidad_total
        db.add(Producto(
            id_producto        = _gen_id_prod(id_e, i),
            id_entrega         = id_e,
            clave              = p.clave.strip().upper(),
            descripcion        = p.descripcion,
            cantidad_total     = cant,
            cantidad_asignada  = 0,
            cantidad_pendiente = cant,
            unidad             = p.unidad,
            es_extension       = p.es_extension
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
    from parsers.traspaso_raiker import parsear_traspaso_raiker
    from parsers.factura_ecor import parsear_factura_ecor
    from parsers.detector import detectar_tipo

    texto = _extraer_texto_pdf(contenido)
    tipo  = detectar_tipo(texto)

    if tipo == "SAP_RAIKER":
        datos = parsear_sap_raiker(texto, archivo.filename)
    elif tipo == "TRASPASO_RAIKER":
        datos = parsear_traspaso_raiker(texto)
    elif tipo == "FACTURA_ECOR":
        datos = parsear_factura_ecor(texto)
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

# ── EXTENSION DE SKU (empaque fisico separado) ────────────────
@router.post("/{id_entrega}/productos/{id_producto}/extension")
async def agregar_extension(
    id_entrega:  str,
    id_producto: str,
    body:        ExtensionIn,
    db:          AsyncSession = Depends(get_db),
    user:        dict = Depends(get_current_user)
):
    result = await db.execute(select(Producto).where(Producto.id_producto == id_producto, Producto.id_entrega == id_entrega))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Producto original no encontrado")
    cant = body.cantidad or 1

    existentes = await db.execute(
        select(func.count(Producto.id_producto)).where(Producto.id_producto.like(f"{id_producto}-EXT%"))
    )
    num_ext = (existentes.scalar() or 0) + 1
    id_ext  = f"{id_producto}-EXT{num_ext}"

    db.add(Producto(
        id_producto=id_ext, id_entrega=id_entrega,
        clave=f"{original.clave} (EXT{num_ext})",
        descripcion=f"{original.descripcion} - Extension/empaque adicional",
        cantidad_total=cant, cantidad_asignada=0, cantidad_pendiente=cant,
        unidad=original.unidad
    ))
    await db.commit()
    return {"ok": True, "id_producto": id_ext, "clave": f"{original.clave} (EXT{num_ext})"}

# ── CREAR TARIMA (vacia, opcionalmente fusionada) ───────────────
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

    fusion_str = None
    if body.ids_entregas_fusionadas and len(body.ids_entregas_fusionadas) >= 2:
        otras = await db.execute(select(Entrega).where(Entrega.id_entrega.in_(body.ids_entregas_fusionadas)))
        entregas_fusion = list(otras.scalars())
        if len(entregas_fusion) != len(set(body.ids_entregas_fusionadas)):
            raise HTTPException(status_code=404, detail="Alguna entrega de la fusion no existe")
        clientes = set((e.nombre_cliente or "").strip() for e in entregas_fusion)
        if len(clientes) > 1:
            raise HTTPException(status_code=400, detail="Solo puedes fusionar entregas del mismo cliente")
        fusion_str = ",".join(body.ids_entregas_fusionadas)

    conteo = await db.execute(select(func.count(Tarima.id_tarima)).where(Tarima.id_entrega == id_entrega))
    idx = (conteo.scalar() or 0) + 1
    id_t = _gen_id_tarima(id_entrega, idx)

    db.add(Tarima(
        id_tarima=id_t, id_entrega=id_entrega, estatus="abierta",
        peso_palet_kg=body.peso_palet_kg or 0, ids_entregas_fusionadas=fusion_str
    ))
    await db.commit()
    return {"ok": True, "id_tarima": id_t, "numero_tarima": idx}

# ── ASIGNAR CANTIDADES DE PRODUCTOS A UNA TARIMA ──────────────
@router.post("/{id_entrega}/tarimas/{id_tarima}/asignar")
async def asignar_productos(
    id_entrega: str,
    id_tarima:  str,
    body:       AsignarLoteIn,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    t = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = t.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    if tarima.estatus == "cerrada":
        raise HTTPException(status_code=400, detail="La tarima esta cerrada")

    ids_permitidos = {id_entrega}
    if tarima.ids_entregas_fusionadas:
        ids_permitidos |= set(tarima.ids_entregas_fusionadas.split(","))

    errores = []
    asignados = 0
    for asig in body.asignaciones:
        if asig.cantidad <= 0:
            continue
        pr = await db.execute(select(Producto).where(Producto.id_producto == asig.id_producto))
        prod = pr.scalar_one_or_none()
        if not prod or prod.id_entrega not in ids_permitidos:
            errores.append(f"Producto no encontrado: {asig.id_producto}")
            continue
        if asig.cantidad > prod.cantidad_pendiente:
            errores.append(f"[{prod.clave}]: solicitado {asig.cantidad}, disponible {prod.cantidad_pendiente}")
            continue

        db.add(DetalleTarima(
            id_detalle=_gen_id_detalle(id_tarima), id_tarima=id_tarima, id_producto=prod.id_producto,
            clave=prod.clave, descripcion=prod.descripcion, cantidad_asignada=asig.cantidad, unidad=prod.unidad
        ))
        prod.cantidad_asignada  += asig.cantidad
        prod.cantidad_pendiente -= asig.cantidad
        asignados += 1

    if errores and asignados == 0:
        raise HTTPException(status_code=400, detail="; ".join(errores))

    await db.commit()
    return {"ok": True, "asignados": asignados, "advertencias": errores or None}

# ── QUITAR UNA ASIGNACION (devuelve stock al pendiente) ───────
@router.delete("/{id_entrega}/detalle/{id_detalle}")
async def quitar_detalle(
    id_entrega: str,
    id_detalle: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    d = await db.execute(select(DetalleTarima).where(DetalleTarima.id_detalle == id_detalle))
    det = d.scalar_one_or_none()
    if not det:
        raise HTTPException(status_code=404, detail="Registro no encontrado")

    t = await db.execute(select(Tarima).where(Tarima.id_tarima == det.id_tarima))
    tarima = t.scalar_one_or_none()
    if tarima and tarima.estatus == "cerrada":
        raise HTTPException(status_code=400, detail="No se puede modificar una tarima cerrada")

    pr = await db.execute(select(Producto).where(Producto.id_producto == det.id_producto))
    prod = pr.scalar_one_or_none()
    if prod:
        prod.cantidad_asignada  = max(0, prod.cantidad_asignada - det.cantidad_asignada)
        prod.cantidad_pendiente = prod.cantidad_pendiente + det.cantidad_asignada

    await db.delete(det)
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

# ── ELIMINAR TARIMA (devuelve todas sus cantidades) ────────────
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

    detalles = await db.execute(select(DetalleTarima).where(DetalleTarima.id_tarima == id_tarima))
    for det in detalles.scalars():
        pr = await db.execute(select(Producto).where(Producto.id_producto == det.id_producto))
        prod = pr.scalar_one_or_none()
        if prod:
            prod.cantidad_asignada  = max(0, prod.cantidad_asignada - det.cantidad_asignada)
            prod.cantidad_pendiente = prod.cantidad_pendiente + det.cantidad_asignada

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

    # Para MIX: el conteo de bultos combina tarimas + carga suelta pendiente,
    # ya que ambos son "bultos" fisicos que suben al mismo embarque
    es_mix = entrega.sistema == "MIX"
    total_bultos = total_tarimas
    if es_mix:
        prods_r2 = await db.execute(select(Producto).where(Producto.id_entrega == id_entrega))
        pend = [p for p in prods_r2.scalars()
                if (p.cantidad_pendiente if p.cantidad_pendiente is not None else p.cantidad_total) > 0]
        total_bultos = total_tarimas + len(pend)

    detalles_r = await db.execute(select(DetalleTarima).where(DetalleTarima.id_tarima == id_tarima))
    productos = [{
        "id_producto":    d.id_producto,
        "clave":          d.clave,
        "descripcion":    d.descripcion,
        "cantidad_total": d.cantidad_asignada,
        "unidad":         d.unidad,
    } for d in detalles_r.scalars()]

    folio_limpio = re.sub(r"[^A-Z0-9\-]", "", (entrega.num_entrega or id_tarima).upper())
    numero_tarima = _numero_tarima(id_tarima)
    barcode_entrega = folio_limpio
    barcode_tarima  = f"{folio_limpio}-T{numero_tarima}"

    peso_palet   = tarima.peso_palet_kg or 0
    total_piezas = sum(p["cantidad_total"] for p in productos)

    return {
        "id_tarima":       tarima.id_tarima,
        "numero_tarima":   numero_tarima,
        "total_tarimas":   total_tarimas,
        "es_mix":          es_mix,
        "total_bultos":    total_bultos,
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
        "impresa_veces":   tarima.impresa_veces or 0,
        "productos":       productos,
        "total_piezas":    total_piezas,
        "peso_palet_kg":   peso_palet,
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

# ── ETIQUETAS INDIVIDUALES POR SKU (carga suelta) ──────────────
@router.get("/{id_entrega}/etiquetas-sueltas")
async def etiquetas_sueltas(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")

    prods_r = await db.execute(select(Producto).where(Producto.id_entrega == id_entrega))
    todos = list(prods_r.scalars())
    # Solo lo que sigue pendiente (no asignado a ninguna tarima) se imprime como suelto
    productos = [p for p in todos if (p.cantidad_pendiente if p.cantidad_pendiente is not None else p.cantidad_total) > 0]
    if not productos:
        raise HTTPException(status_code=404, detail="No hay productos pendientes de asignar (todo esta en tarimas)")

    folio_limpio = re.sub(r"[^A-Z0-9\-]", "", (entrega.num_entrega or id_entrega).upper())
    barcode_entrega = folio_limpio
    barcode_url = _barcode_url(barcode_entrega)

    # Para MIX: la numeracion de bultos continua despues de las tarimas ya
    # cerradas (que ya se contaron como bultos 1..N en sus propias etiquetas)
    offset = 0
    if entrega.sistema == "MIX":
        tars_r = await db.execute(
            select(func.count(Tarima.id_tarima)).where(Tarima.id_entrega == id_entrega, Tarima.estatus == "cerrada")
        )
        offset = tars_r.scalar() or 0

    total_skus = len(productos)
    total_piezas = sum(p.cantidad_pendiente for p in productos)

    resultado = []
    acumulado = 0
    for i, p in enumerate(productos, 1):
        cant = p.cantidad_pendiente
        pieza_inicio = acumulado + 1
        acumulado += cant
        resultado.append({
            "id_producto":         p.id_producto,
            "clave":               p.clave,
            "descripcion":         p.descripcion,
            "cantidad":            cant,
            "unidad":              p.unidad,
            "num_sku":             offset + i,
            "total_skus_entrega":  offset + total_skus,
            "es_mix":              entrega.sistema == "MIX",
            "pieza_inicio":        pieza_inicio,
            "total_piezas_entrega":total_piezas,
            "num_entrega":         entrega.num_entrega,
            "nombre_cliente":      entrega.nombre_cliente,
            "direccion":           entrega.direccion,
            "orden":               entrega.orden,
            "comercializador":     entrega.comercializador,
            "remitente":           _remitente(entrega.comercializador),
            "sucursal":            entrega.sucursal,
            "fecha_entrega":       entrega.fecha_entrega,
            "barcode_entrega":     barcode_entrega,
            "barcode_entrega_url": barcode_url,
            "impresa_veces":       entrega.etiquetas_sueltas_impresas_veces or 0,
        })
    return resultado

# ── FUSION DE ENTREGAS (mismo cliente) ──────────────────────────
async def _tarimas_relacionadas(db: AsyncSession, id_entrega: str):
    """Tarimas propias de la entrega + tarimas fusionadas que la incluyen."""
    propias = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    resultado = {t.id_tarima: t for t in propias.scalars()}
    con_fusion = await db.execute(select(Tarima).where(Tarima.ids_entregas_fusionadas.is_not(None)))
    for t in con_fusion.scalars():
        if id_entrega in (t.ids_entregas_fusionadas or "").split(","):
            resultado[t.id_tarima] = t
    return list(resultado.values())

# ── LISTA DE EMPAQUE (packing list) ─────────────────────────────
@router.get("/{id_entrega}/packing")
async def lista_empaque(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")

    tarimas = await _tarimas_relacionadas(db, id_entrega)

    ids_involucrados = {id_entrega}
    for t in tarimas:
        ids_involucrados.add(t.id_entrega)
        if t.ids_entregas_fusionadas:
            ids_involucrados |= set(t.ids_entregas_fusionadas.split(","))

    entregas_r = await db.execute(select(Entrega).where(Entrega.id_entrega.in_(list(ids_involucrados))))
    entregas_involucradas = [_serializar_entrega(e) for e in entregas_r.scalars()]
    es_fusion = len(entregas_involucradas) > 1

    tarimas_ids = [t.id_tarima for t in tarimas]
    detalles_r = await db.execute(
        select(DetalleTarima).where(DetalleTarima.id_tarima.in_(tarimas_ids))
    ) if tarimas_ids else None
    detalles = list(detalles_r.scalars()) if detalles_r else []

    tarimas_data = []
    for t in sorted(tarimas, key=lambda x: _numero_tarima(x.id_tarima)):
        det = [d for d in detalles if d.id_tarima == t.id_tarima]
        piezas = sum(d.cantidad_asignada for d in det)
        tarimas_data.append({
            "id_tarima":     t.id_tarima,
            "numero_tarima": _numero_tarima(t.id_tarima),
            "estatus":       t.estatus,
            "peso_palet_kg": t.peso_palet_kg or 0,
            "largo_cm":      t.largo_cm or 0,
            "ancho_cm":      t.ancho_cm or 0,
            "alto_cm":       t.alto_cm or 0,
            "total_piezas":  piezas,
            "productos":     [_ser_detalle(d) for d in det],
        })

    # Productos sueltos (sin tarima): en CS son todos; en MIX solo lo pendiente
    prods_r = await db.execute(select(Producto).where(Producto.id_entrega.in_(list(ids_involucrados))))
    todos_prods = list(prods_r.scalars())
    if entrega.sistema == "CS":
        sueltos_data = [_ser_prod(p) for p in todos_prods if p.cantidad_total > 0]
    else:
        sueltos_data = [_ser_prod(p) for p in todos_prods
                         if (p.cantidad_pendiente if p.cantidad_pendiente is not None else p.cantidad_total) > 0]

    if not tarimas_data and not sueltos_data:
        raise HTTPException(status_code=404, detail="No hay productos ni tarimas para generar la lista de empaque")

    total_piezas_sueltos = sum(
        (p["cantidad_pendiente"] if entrega.sistema != "CS" else p["cantidad_total"]) for p in sueltos_data
    )

    folio_limpio = re.sub(r"[^A-Z0-9\-]", "", (entrega.num_entrega or id_entrega).upper())

    return {
        "entrega":              _serializar_entrega(entrega),
        "es_fusion":            es_fusion,
        "entregas_involucradas":entregas_involucradas,
        "remitente":            _remitente(entrega.comercializador),
        "tarimas":              tarimas_data,
        "sueltos":              sueltos_data,
        "total_bultos":         len(tarimas_data),
        "total_piezas":         sum(t["total_piezas"] for t in tarimas_data) + total_piezas_sueltos,
        "total_piezas_sueltos": total_piezas_sueltos,
        "peso_palet_total_kg":  round(sum(t["peso_palet_kg"] for t in tarimas_data), 2),
        "barcode_entrega":      folio_limpio,
        "barcode_entrega_url":  _barcode_url(folio_limpio),
    }

# ── TODAS LAS ETIQUETAS DE UNA ENTREGA ─────────────────────────
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
    tarima.estatus      = "cerrada"
    tarima.fecha_cierre = datetime.utcnow()
    if body.categoria:
        tarima.comentario = body.categoria + (f" — {body.notas}" if body.notas else "")
    if body.largo_cm: tarima.largo_cm = body.largo_cm
    if body.ancho_cm: tarima.ancho_cm = body.ancho_cm
    if body.alto_cm:  tarima.alto_cm  = body.alto_cm
    tarima.cerrado_por = user["sub"]
    await db.commit()
    return {"ok": True}

# ── REABRIR TARIMA (solo Gerente/Admin) ─────────────────────────
@router.post("/{id_entrega}/tarimas/{id_tarima}/reabrir")
async def reabrir_tarima(
    id_entrega: str,
    id_tarima:  str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    tarima.estatus      = "abierta"
    tarima.fecha_cierre = None
    await db.commit()
    return {"ok": True}

# ── ACTUALIZAR SUCURSAL / COMERCIALIZADOR DE LA ENTREGA ───────
@router.patch("/{id_entrega}")
async def actualizar_entrega(
    id_entrega: str,
    body:       ActualizarEntregaIn,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    if body.sucursal is not None:
        entrega.sucursal = body.sucursal
    if body.comercializador is not None:
        entrega.comercializador = body.comercializador
    if body.direccion is not None:
        entrega.direccion = body.direccion
    if body.nombre_cliente is not None:
        entrega.nombre_cliente = body.nombre_cliente
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

# ── REABRIR ENTREGA (solo Gerente/Admin) ────────────────────────
@router.post("/{id_entrega}/reabrir")
async def reabrir_entrega(
    id_entrega: str,
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    entrega.estatus = "pendiente"
    await db.commit()
    return {"ok": True}

# ── MARCAR IMPRESION (bloquea reimpresion a Operador) ───────────
async def _controlar_impresion(db: AsyncSession, user: dict, veces_previas: int, motivo: str,
                                tipo: str, referencia: str, id_entrega: str, num_entrega: str) -> bool:
    """Primera impresion: libre para cualquiera.
    Reimpresion:
      - Admin/Gerente: pueden reimprimir directo, con motivo obligatorio (queda en bitacora).
      - Operador: no puede reimprimir directo. Si ya tiene una solicitud APROBADA
        para este mismo documento, se le permite (y se consume). Si no, se crea
        una solicitud PENDIENTE con su motivo y se le informa que debe esperar
        autorizacion de un Gerente.
    """
    if veces_previas == 0:
        return True

    if user.get("rol") in ("admin", "gerente"):
        if not (motivo or "").strip():
            raise HTTPException(status_code=400, detail="Debes indicar un motivo para justificar la reimpresion.")
        ahora = datetime.utcnow()
        db.add(LogAcceso(
            usuario=user["sub"], accion=f"REIMPRESION_{tipo}",
            detalle=f"{referencia} — motivo: {motivo.strip()}", exito=True
        ))
        db.add(SolicitudReimpresion(
            id=_gen_id_solicitud(), tipo=tipo, id_entrega=id_entrega, referencia=referencia,
            num_entrega=num_entrega, motivo=motivo.strip(), solicitado_por=user["sub"],
            estatus="aprobada", autorizado_por=user["sub"], fecha_resolucion=ahora
        ))
        return True

    # Operador
    aprobada = await db.execute(
        select(SolicitudReimpresion).where(
            SolicitudReimpresion.tipo == tipo,
            SolicitudReimpresion.referencia == referencia,
            SolicitudReimpresion.solicitado_por == user["sub"],
            SolicitudReimpresion.estatus == "aprobada"
        )
    )
    sol_aprobada = aprobada.scalars().first()
    if sol_aprobada:
        sol_aprobada.estatus = "usada"
        return True

    pendiente = await db.execute(
        select(SolicitudReimpresion).where(
            SolicitudReimpresion.tipo == tipo,
            SolicitudReimpresion.referencia == referencia,
            SolicitudReimpresion.solicitado_por == user["sub"],
            SolicitudReimpresion.estatus == "pendiente"
        )
    )
    if pendiente.scalars().first():
        raise HTTPException(status_code=403, detail="Ya existe una solicitud pendiente de autorizacion para este documento.")

    rechazada = await db.execute(
        select(SolicitudReimpresion).where(
            SolicitudReimpresion.tipo == tipo,
            SolicitudReimpresion.referencia == referencia,
            SolicitudReimpresion.solicitado_por == user["sub"],
            SolicitudReimpresion.estatus == "rechazada"
        ).order_by(SolicitudReimpresion.fecha_resolucion.desc())
    )
    sol_rechazada = rechazada.scalars().first()
    if sol_rechazada and not (motivo or "").strip():
        raise HTTPException(
            status_code=403,
            detail=f"Tu solicitud anterior fue rechazada" +
                   (f" ({sol_rechazada.comentario_resolucion})" if sol_rechazada.comentario_resolucion else "") +
                   ". Escribe un nuevo motivo para volver a solicitar."
        )

    if not (motivo or "").strip():
        raise HTTPException(status_code=400, detail="Escribe el motivo de la reimpresion para enviar la solicitud.")

    db.add(SolicitudReimpresion(
        id=_gen_id_solicitud(), tipo=tipo, id_entrega=id_entrega, referencia=referencia,
        num_entrega=num_entrega, motivo=motivo.strip(), solicitado_por=user["sub"], estatus="pendiente"
    ))
    await db.commit()
    raise HTTPException(status_code=403, detail="Solicitud enviada. Debe ser autorizada por un Gerente antes de imprimir.")

def _gen_id_solicitud() -> str:
    return f"SR-{int(time.time()*1000) % 100000000}"

@router.post("/{id_entrega}/tarimas/{id_tarima}/marcar-impresa")
async def marcar_impresa_tarima(
    id_entrega: str,
    id_tarima:  str,
    body:       MarcarImpresoIn = MarcarImpresoIn(),
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima, Tarima.id_entrega == id_entrega))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    ent_r = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = ent_r.scalar_one_or_none()
    await _controlar_impresion(db, user, tarima.impresa_veces or 0, body.motivo, "TARIMA", id_tarima,
                                id_entrega, entrega.num_entrega if entrega else "")
    if not tarima.impresa_veces:
        tarima.primera_impresion_en  = datetime.utcnow()
        tarima.primera_impresion_por = user["sub"]
    tarima.impresa_veces = (tarima.impresa_veces or 0) + 1
    await db.commit()
    return {"ok": True, "veces": tarima.impresa_veces}

@router.post("/{id_entrega}/etiquetas-sueltas/marcar-impresa")
async def marcar_impresa_sueltas(
    id_entrega: str,
    body:       MarcarImpresoIn = MarcarImpresoIn(),
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    await _controlar_impresion(db, user, entrega.etiquetas_sueltas_impresas_veces or 0, body.motivo,
                                "SUELTAS", id_entrega, id_entrega, entrega.num_entrega)
    entrega.etiquetas_sueltas_impresas_veces = (entrega.etiquetas_sueltas_impresas_veces or 0) + 1
    await db.commit()
    return {"ok": True, "veces": entrega.etiquetas_sueltas_impresas_veces}

@router.post("/{id_entrega}/packing/marcar-impreso")
async def marcar_impreso_packing(
    id_entrega: str,
    body:       MarcarImpresoIn = MarcarImpresoIn(),
    db:         AsyncSession = Depends(get_db),
    user:       dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    await _controlar_impresion(db, user, entrega.packing_impreso_veces or 0, body.motivo,
                                "PACKING", id_entrega, id_entrega, entrega.num_entrega)
    entrega.packing_impreso_veces = (entrega.packing_impreso_veces or 0) + 1
    await db.commit()
    return {"ok": True, "veces": entrega.packing_impreso_veces}

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
        "etiquetas_sueltas_impresas_veces": e.etiquetas_sueltas_impresas_veces or 0,
        "packing_impreso_veces":            e.packing_impreso_veces or 0,
    }

def _ser_prod(p: Producto) -> dict:
    return {
        "id_producto":        p.id_producto,
        "id_entrega":         p.id_entrega,
        "clave":              p.clave,
        "descripcion":        p.descripcion,
        "cantidad_total":     p.cantidad_total,
        "cantidad_asignada":  p.cantidad_asignada or 0,
        "cantidad_pendiente": p.cantidad_pendiente if p.cantidad_pendiente is not None else p.cantidad_total,
        "unidad":             p.unidad,
        "es_extension":       p.es_extension,
    }

def _ser_detalle(d: DetalleTarima) -> dict:
    return {
        "id_detalle":        d.id_detalle,
        "id_tarima":         d.id_tarima,
        "id_producto":       d.id_producto,
        "clave":             d.clave,
        "descripcion":       d.descripcion,
        "cantidad_asignada": d.cantidad_asignada,
        "unidad":            d.unidad,
    }

def _ser_tarima(t: Tarima, detalles: list = None) -> dict:
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
        "impresa_veces":  t.impresa_veces or 0,
        "productos":      [_ser_detalle(d) for d in (detalles or [])],
    }
