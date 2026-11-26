"""Seguimiento publico de una OV de Refacciones para el cliente. Sin usuario: se entra con una liga con token secreto.
Solo expone lo que le sirve al cliente (etapas, avance, partidas y cantidades). Nada de precios, folios internos, responsables ni notas."""
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import OVEnlace, OrdenVenta, OVLinea, OVEvento, OVCaja

router = APIRouter()

ETAPAS = [
    ("recibida", "Pedido recibido"),
    ("en_surtido", "Surtiendo tu pedido"),
    ("surtida", "Surtido completo"),
    ("en_preparacion", "Preparando cajas"),
    ("preparada", "Listo para entrega"),
    ("entregada_sap", "Entrega generada"),
    ("facturada", "Facturado"),
]
_ORDEN = [e for e, _ in ETAPAS]

# Freno simple contra adivinar ligas: 40 consultas por minuto por IP.
_golpes = defaultdict(deque)


def _limitar(ip: str):
    t = time.time()
    q = _golpes[ip]
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= 40:
        raise HTTPException(429, "Demasiadas consultas. Intenta de nuevo en un minuto.")
    q.append(t)


def _iso(dt):
    return (dt.isoformat() + "Z") if dt else None


@router.get("/{token}")
async def ver(token: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    _limitar(request.client.host if request.client else "x")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    e = (await db.execute(select(OVEnlace).where(OVEnlace.token == token))).scalars().first()
    if not e or not e.activo:
        raise HTTPException(404, "Esta liga no existe o ya no está vigente. Pide una nueva a tu contacto.")
    ov = await db.get(OrdenVenta, e.ov_id)
    if not ov:
        raise HTTPException(404, "Esta liga no existe o ya no está vigente.")
    e.vistas = (e.vistas or 0) + 1
    e.ultima_vista = datetime.utcnow()
    ls = list((await db.execute(select(OVLinea).where(OVLinea.ov_id == ov.id).order_by(OVLinea.linea))).scalars())
    evs = list((await db.execute(select(OVEvento).where(OVEvento.ov_id == ov.id).order_by(OVEvento.en, OVEvento.id))).scalars())
    cajas = list((await db.execute(select(OVCaja).where(OVCaja.ov_id == ov.id, OVCaja.estatus == "cerrada"))).scalars())
    await db.commit()

    fechas = {}
    for ev in evs:
        fechas.setdefault(ev.estatus, ev.en)
    cancelada = ov.estatus == "cancelada"
    idx = _ORDEN.index(ov.estatus) if ov.estatus in _ORDEN else -1
    if cancelada:   # la etapa donde se quedo
        previos = [ev.estatus for ev in evs if ev.estatus in _ORDEN]
        idx = _ORDEN.index(previos[-1]) if previos else 0
    etapas = []
    for i, (k, nombre) in enumerate(ETAPAS):
        est = "hecha" if i < idx or (i == idx and k == "facturada") else ("actual" if i == idx and not cancelada else "pendiente")
        if cancelada and i == idx:
            est = "hecha"
        etapas.append({"id": k, "nombre": nombre, "estado": est, "fecha": _iso(fechas.get(k)) if est != "pendiente" else None})

    pedidas = sum(l.cantidad or 0 for l in ls)
    surtidas = sum(min(l.surtida or 0, l.cantidad or 0) for l in ls)
    empacadas = sum(l.empacada or 0 for l in ls)
    cerrado_surtido = idx >= _ORDEN.index("surtida")
    partidas = [{
        "codigo": l.codigo, "descripcion": l.descripcion, "pedida": l.cantidad or 0, "surtida": min(l.surtida or 0, l.cantidad or 0),
        "empacada": l.empacada or 0,
        "faltante": max(0, (l.cantidad or 0) - (l.surtida or 0)) if cerrado_surtido else 0,
    } for l in ls]
    return {
        "num_ov": ov.num_ov, "cliente": ov.cliente, "ciudad": ov.destino_ciudad, "referencia": ov.referencia,
        "fecha_entrega": ov.fecha_entrega, "cancelada": cancelada,
        "estatus": ov.estatus, "estatus_nombre": "Cancelado" if cancelada else dict(ETAPAS).get(ov.estatus, ov.estatus),
        "etapas": etapas,
        "avance": {"piezas": pedidas, "surtidas": surtidas, "empacadas": empacadas, "cajas": len(cajas),
                   "pct_surtido": round(100 * surtidas / pedidas, 1) if pedidas else 0,
                   "pct_preparado": round(100 * empacadas / surtidas, 1) if surtidas else 0},
        "partidas": partidas,
        "actualizado": _iso(datetime.utcnow()),
    }
