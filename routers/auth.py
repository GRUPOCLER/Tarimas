from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from database import get_db
from services import auth as auth_svc
from services.permisos import usuario_actual, catalogo

router = APIRouter()

class LoginRequest(BaseModel):
    usuario:  str
    password: str

@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    resultado = await auth_svc.login(db, req.usuario, req.password)
    if not resultado:
        raise HTTPException(status_code=401, detail="Usuario o contrasena incorrectos")
    return resultado

@router.get("/verificar")
async def verificar(user: dict = Depends(usuario_actual)):
    return {"ok": True, "usuario": user["sub"], "es_admin": user["rol"] == "admin"}

@router.get("/yo")
async def yo(user: dict = Depends(usuario_actual)):
    """Datos frescos del usuario: a que modulos entra y con que rol."""
    return {"usuario": user["sub"], "nombre": user.get("nombre"),
            "es_admin": user["rol"] == "admin", "modulos": user["modulos"]}

@router.get("/modulos")
async def modulos(user: dict = Depends(usuario_actual)):
    return catalogo()
