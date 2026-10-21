from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from models.models import Entrega, Producto
from routers.entregas import get_current_user
from datetime import datetime

router = APIRouter()

@router.get("/")
async def resumen(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user)
):
    hoy     = datetime.utcnow().date()
    mes_ini = hoy.replace(day=1)

    total_ents  = await db.execute(select(func.count(Entrega.id_entrega)))
    total_prods = await db.execute(select(func.sum(Producto.cantidad_total)))
    mes_ents    = await db.execute(
        select(func.count(Entrega.id_entrega))
        .where(func.date(Entrega.fecha_creacion) >= mes_ini)
    )

    return {
        "total_entregas": total_ents.scalar()  or 0,
        "total_piezas":   total_prods.scalar() or 0,
        "entregas_mes":   mes_ents.scalar()    or 0,
        "fecha":          str(hoy)
    }
