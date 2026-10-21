from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from database import get_db
from models.models import Usuario, RolEnum
from services.auth import hashear_password

router = APIRouter()

class SetupRequest(BaseModel):
    usuario:  str
    password: str
    nombre:   str = "Administrador"

@router.post("/inicial")
async def setup_inicial(req: SetupRequest, db: AsyncSession = Depends(get_db)):
    total = await db.execute(select(func.count(Usuario.id)))
    if (total.scalar() or 0) > 0:
        raise HTTPException(
            status_code=403,
            detail="El sistema ya fue inicializado. Este endpoint solo funciona una vez."
        )
    db.add(Usuario(
        usuario        = req.usuario.lower().strip(),
        password_hash  = hashear_password(req.password.strip()),
        rol            = RolEnum.admin,
        activo         = True,
        nombre_display = req.nombre
    ))
    await db.commit()
    return {"ok": True, "mensaje": f"Usuario admin '{req.usuario}' creado. Ya puedes iniciar sesion."}
