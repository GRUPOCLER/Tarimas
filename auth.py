from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from database import get_db
from services import auth as auth_svc

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
async def verificar(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin token")
    token = authorization.split(" ")[1]
    payload = auth_svc.verificar_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido o expirado")
    return {"ok": True, "usuario": payload["sub"], "rol": payload["rol"]}
