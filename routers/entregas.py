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

async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin autorizacion")
    payload = verificar_token(authorization.split(" ")[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    return payload

class ProductoIn(BaseModel):
    clave:          str
    descripcion:    str
    cantidad_total: int
    unidad:         str = "PZA"
    es_extension:   bool = False

class EntregaIn(BaseModel):
    sistema:        str
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
    categoria: str
    notas:     Optional[str] = ""

def _gen_id_entrega(sistema: str) -> str:
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{sistema}-{ts}"

def _gen_id_prod(id_entrega: str, idx: int) -> str:
    return f"{id_entrega}-P{idx:03d}"

@router.get("/")
async def listar_entregas(
    sistema: Optional[str] = None,
    estatus: Optional[str] = None,
    limite:  int = 50,
    db:      AsyncSession = Depends(get_db),
    user:    dict = Depends(get_current_user)
):
    q = select(Entrega).order_by(Entrega.fecha_creacion.desc()).limit(limite)
    if sistema: q = q.where(Entrega.sistema == sistema)
    if estatus: q = q.where(Entrega.estatus == estatus)
    result = await db.execute(q)
    return [_serializar_entrega(e) for e in result.scalars().all()]

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
    prods   = await db.execute(select(Producto).where(Producto.id_entrega == id_entrega))
    tarimas = await db.execute(select(Tarima).where(Tarima.id_entrega == id_entrega))
    data = _serializar_entrega(entrega)
    data["productos"] = [_ser_prod(p) for p in prods.scalars()]
    data["tarimas"]   = [_ser_tarima(t) for t in tarimas.scalars()]
    return data

@router.post("/")
async def crear_entrega(
    body: EntregaIn,
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    id_e = _gen_id_entrega(body.sistema)
    db.add(Entrega(
        id_entrega=id_e, num_entrega=body.num_entrega,
        sistema=body.sistema, nombre_cliente=body.nombre_cliente,
        rfc_cliente=body.rfc_cliente, direccion=body.direccion,
        orden=body.orden, fecha_entrega=body.fecha_entrega,
        comercializador=body.comercializador, sucursal=body.sucursal,
        fuente=body.fuente, creado_por=user["sub"]
    ))
    for i, p in enumerate(body.productos, 1):
        db.add(Producto(
            id_producto=_gen_id_prod(id_e, i), id_entrega=id_e,
            clave=p.clave.strip().upper(), descripcion=p.descripcion,
            cantidad_total=p.cantidad_total, unidad=p.unidad,
            es_extension=p.es_extension
        ))
    await db.commit()
    return {"ok": True, "id_entrega": id_e, "total": len(body.productos)}

@router.post("/pdf")
async def procesar_pdf(
    archivo: UploadFile = File(...),
    sistema: str = "CS",
    comercializador: str = "",
    db:      AsyncSession = Depends(get_db),
    user:    dict = Depends(get_current_user)
):
    contenido = await archivo.read()
    from parsers.ecor import parsear_ecor
    from parsers.sap_raiker import parsear_sap_raiker
    from parsers.detector import detectar_tipo
    texto = _extraer_texto_pdf(contenido)
    tipo  = detectar_tipo(texto)
    datos = parsear_sap_raiker(texto, archivo.filename) if tipo == "SAP_RAIKER" else parsear_ecor(texto)
    if not datos.get("productos"):
        raise HTTPException(status_code=422, detail="No se encontraron productos en el PDF")
    datos["sistema"] = sistema
    datos["fuente"]  = "pdf"
    datos["comercializador"] = datos.get("comercializador") or comercializador
    return await crear_entrega(EntregaIn(**datos), db, user)

def _extraer_texto_pdf(contenido: bytes) -> str:
    import pdfplumber
    texto = ""
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        for page in pdf.pages:
            texto += (page.extract_text() or "") + "\n"
    return texto

@router.post("/{id_entrega}/tarimas/{id_tarima}/cerrar")
async def cerrar_tarima(
    id_entrega: str, id_tarima: str, body: CerrarTarimaIn,
    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)
):
    result = await db.execute(select(Tarima).where(Tarima.id_tarima == id_tarima))
    tarima = result.scalar_one_or_none()
    if not tarima:
        raise HTTPException(status_code=404, detail="Tarima no encontrada")
    tarima.estatus      = "cerrada"
    tarima.fecha_cierre = datetime.utcnow()
    tarima.comentario   = body.categoria + (f" - {body.notas}" if body.notas else "")
    tarima.cerrado_por  = user["sub"]
    await db.commit()
    return {"ok": True}

@router.post("/{id_entrega}/completar")
async def completar_entrega(
    id_entrega: str,
    db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)
):
    result = await db.execute(select(Entrega).where(Entrega.id_entrega == id_entrega))
    entrega = result.scalar_one_or_none()
    if not entrega:
        raise HTTPException(status_code=404, detail="Entrega no encontrada")
    entrega.estatus = EstatusEntrega.completada
    await db.commit()
    return {"ok": True}

def _serializar_entrega(e): return {
    "id_entrega": e.id_entrega, "num_entrega": e.num_entrega,
    "sistema": e.sistema, "nombre_cliente": e.nombre_cliente,
    "direccion": e.direccion, "orden": e.orden,
    "fecha_entrega": e.fecha_entrega, "fecha_creacion": str(e.fecha_creacion or ""),
    "estatus": e.estatus, "comercializador": e.comercializador,
    "sucursal": e.sucursal, "fuente": e.fuente,
}

def _ser_prod(p): return {
    "id_producto": p.id_producto, "id_entrega": p.id_entrega,
    "clave": p.clave, "descripcion": p.descripcion,
    "cantidad_total": p.cantidad_total, "unidad": p.unidad, "es_extension": p.es_extension,
}

def _ser_tarima(t): return {
    "id_tarima": t.id_tarima, "id_entrega": t.id_entrega, "estatus": t.estatus,
    "fecha_creacion": str(t.fecha_creacion or ""), "fecha_cierre": str(t.fecha_cierre or ""),
    "comentario": t.comentario, "cerrado_por": t.cerrado_por,
}
