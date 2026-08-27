from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import time

from database import get_db
from models.models import SolicitudReimpresion, LogAcceso
from services.auth import verificar_token

router = APIRouter()

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
            raise HTTPException(status_code=403, detail="No tienes permisos para esta accion")
        return user
    return dep

class ResolucionIn(BaseModel):
    comentario: Optional[str] = None

@router.get("/pendientes-count")
async def contar_pendientes(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles("admin", "gerente"))
):
    from sqlalchemy import func as sa_func
    result = await db.execute(
        select(sa_func.count(SolicitudReimpresion.id)).where(SolicitudReimpresion.estatus == "pendiente")
    )
    return {"pendientes": result.scalar() or 0}

def _gen_id_solicitud() -> str:
    return f"SR-{int(time.time()*1000) % 100000000}"

# ── LISTAR SOLICITUDES ────────────────────────────────────────
@router.get("/")
async def listar_solicitudes(
    estatus: Optional[str] = None,
    db:      AsyncSession = Depends(get_db),
    user:    dict = Depends(require_roles("admin", "gerente"))
):
    q = select(SolicitudReimpresion).order_by(SolicitudReimpresion.fecha_solicitud.desc())
    if estatus:
        q = q.where(SolicitudReimpresion.estatus == estatus)
    result = await db.execute(q)
    return [_ser(s) for s in result.scalars()]

# ── MIS SOLICITUDES (para que el operador vea su propio estatus) ─
@router.get("/mias")
async def mis_solicitudes(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    result = await db.execute(
        select(SolicitudReimpresion)
        .where(SolicitudReimpresion.solicitado_por == user["sub"])
        .order_by(SolicitudReimpresion.fecha_solicitud.desc())
        .limit(20)
    )
    return [_ser(s) for s in result.scalars()]

# ── APROBAR ────────────────────────────────────────────────────
@router.post("/{id_solicitud}/aprobar")
async def aprobar(
    id_solicitud: str,
    body:         ResolucionIn,
    db:           AsyncSession = Depends(get_db),
    user:         dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(SolicitudReimpresion).where(SolicitudReimpresion.id == id_solicitud))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if s.estatus != "pendiente":
        raise HTTPException(status_code=400, detail="Esta solicitud ya fue resuelta")
    s.estatus = "aprobada"
    s.autorizado_por = user["sub"]
    s.fecha_resolucion = datetime.utcnow()
    s.comentario_resolucion = body.comentario
    db.add(LogAcceso(usuario=user["sub"], accion="APROBAR_REIMPRESION",
                      detalle=f"{s.tipo} {s.referencia} solicitada por {s.solicitado_por}", exito=True))
    await db.commit()
    return {"ok": True}

# ── RECHAZAR ───────────────────────────────────────────────────
@router.post("/{id_solicitud}/rechazar")
async def rechazar(
    id_solicitud: str,
    body:         ResolucionIn,
    db:           AsyncSession = Depends(get_db),
    user:         dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(SolicitudReimpresion).where(SolicitudReimpresion.id == id_solicitud))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if s.estatus != "pendiente":
        raise HTTPException(status_code=400, detail="Esta solicitud ya fue resuelta")
    s.estatus = "rechazada"
    s.autorizado_por = user["sub"]
    s.fecha_resolucion = datetime.utcnow()
    s.comentario_resolucion = body.comentario
    db.add(LogAcceso(usuario=user["sub"], accion="RECHAZAR_REIMPRESION",
                      detalle=f"{s.tipo} {s.referencia} solicitada por {s.solicitado_por}", exito=True))
    await db.commit()
    return {"ok": True}

def _ser(s: SolicitudReimpresion) -> dict:
    return {
        "id":                     s.id,
        "tipo":                   s.tipo,
        "id_entrega":             s.id_entrega,
        "referencia":             s.referencia,
        "num_entrega":            s.num_entrega,
        "motivo":                 s.motivo,
        "solicitado_por":         s.solicitado_por,
        "fecha_solicitud":        str(s.fecha_solicitud or ""),
        "estatus":                s.estatus,
        "autorizado_por":         s.autorizado_por,
        "fecha_resolucion":       str(s.fecha_resolucion or ""),
        "comentario_resolucion":  s.comentario_resolucion,
    }

# ── CAMBIOS DE SISTEMA (TAR/CS/MIX) ─────────────────────────────
from models.models import SolicitudCambioSistema

@router.get("/cambios-sistema/pendientes-count")
async def contar_pendientes_cambios(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles("admin", "gerente"))
):
    from sqlalchemy import func as sa_func
    result = await db.execute(
        select(sa_func.count(SolicitudCambioSistema.id)).where(SolicitudCambioSistema.estatus == "pendiente")
    )
    return {"pendientes": result.scalar() or 0}

@router.get("/cambios-sistema")
async def listar_cambios_sistema(
    estatus: Optional[str] = None,
    db:      AsyncSession = Depends(get_db),
    user:    dict = Depends(require_roles("admin", "gerente"))
):
    q = select(SolicitudCambioSistema).order_by(SolicitudCambioSistema.fecha_solicitud.desc())
    if estatus:
        q = q.where(SolicitudCambioSistema.estatus == estatus)
    result = await db.execute(q)
    return [_ser_cambio(s) for s in result.scalars()]

@router.post("/cambios-sistema/{id_solicitud}/aprobar")
async def aprobar_cambio_sistema(
    id_solicitud: str,
    body:         ResolucionIn,
    db:           AsyncSession = Depends(get_db),
    user:         dict = Depends(require_roles("admin", "gerente"))
):
    from models.models import Entrega
    result = await db.execute(select(SolicitudCambioSistema).where(SolicitudCambioSistema.id == id_solicitud))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if s.estatus != "pendiente":
        raise HTTPException(status_code=400, detail="Esta solicitud ya fue resuelta")

    ent_r = await db.execute(select(Entrega).where(Entrega.id_entrega == s.id_entrega))
    entrega = ent_r.scalar_one_or_none()
    if entrega:
        entrega.sistema = s.sistema_nuevo

    s.estatus = "aprobada"
    s.autorizado_por = user["sub"]
    s.fecha_resolucion = datetime.utcnow()
    s.comentario_resolucion = body.comentario
    db.add(LogAcceso(usuario=user["sub"], accion="APROBAR_CAMBIO_SISTEMA",
                      detalle=f"{s.id_entrega}: {s.sistema_actual} -> {s.sistema_nuevo}", exito=True))
    await db.commit()
    return {"ok": True}

@router.post("/cambios-sistema/{id_solicitud}/rechazar")
async def rechazar_cambio_sistema(
    id_solicitud: str,
    body:         ResolucionIn,
    db:           AsyncSession = Depends(get_db),
    user:         dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(SolicitudCambioSistema).where(SolicitudCambioSistema.id == id_solicitud))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if s.estatus != "pendiente":
        raise HTTPException(status_code=400, detail="Esta solicitud ya fue resuelta")
    s.estatus = "rechazada"
    s.autorizado_por = user["sub"]
    s.fecha_resolucion = datetime.utcnow()
    s.comentario_resolucion = body.comentario
    db.add(LogAcceso(usuario=user["sub"], accion="RECHAZAR_CAMBIO_SISTEMA",
                      detalle=f"{s.id_entrega}: {s.sistema_actual} -> {s.sistema_nuevo}", exito=True))
    await db.commit()
    return {"ok": True}

def _ser_cambio(s: SolicitudCambioSistema) -> dict:
    return {
        "id":                     s.id,
        "id_entrega":             s.id_entrega,
        "num_entrega":            s.num_entrega,
        "sistema_actual":         s.sistema_actual,
        "sistema_nuevo":          s.sistema_nuevo,
        "motivo":                 s.motivo,
        "solicitado_por":         s.solicitado_por,
        "fecha_solicitud":        str(s.fecha_solicitud or ""),
        "estatus":                s.estatus,
        "autorizado_por":         s.autorizado_por,
        "fecha_resolucion":       str(s.fecha_resolucion or ""),
        "comentario_resolucion":  s.comentario_resolucion,
    }
