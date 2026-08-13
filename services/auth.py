import os, hashlib
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.models import Usuario, LogAcceso

SECRET_KEY  = os.getenv("JWT_SECRET", "cler-jwt-secret-2026-cambiar-en-produccion")
ALGORITHM   = "HS256"
TOKEN_HORAS = int(os.getenv("TOKEN_HORAS", "8"))

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()

def verificar_password(plain: str, hashed: str) -> bool:
    if len(hashed) == 64 and not hashed.startswith("$"):
        return sha256(plain) == hashed
    return pwd_ctx.verify(plain, hashed)

def hashear_password(password: str) -> str:
    return sha256(password)

def crear_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=TOKEN_HORAS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def verificar_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

async def login(db: AsyncSession, usuario: str, password: str):
    result = await db.execute(
        select(Usuario).where(Usuario.usuario == usuario.lower().strip())
    )
    usu = result.scalar_one_or_none()
    if not usu or not usu.activo:
        await _log(db, usuario, "LOGIN", "Usuario no encontrado o inactivo", False)
        return None
    if not verificar_password(password.strip(), usu.password_hash):
        await _log(db, usuario, "LOGIN", "Password incorrecto", False)
        return None
    usu.ultimo_acceso = datetime.utcnow()
    await db.commit()
    await _log(db, usuario, "LOGIN", "Sesion iniciada", True)
    token = crear_token({
        "sub":    usu.usuario,
        "rol":    usu.rol,
        "nombre": usu.nombre_display
    })
    return {
        "token":        token,
        "usuario":      usu.usuario,
        "nombre":       usu.nombre_display,
        "rol":          usu.rol,
        "expira_horas": TOKEN_HORAS
    }

async def _log(db: AsyncSession, usuario: str, accion: str, detalle: str, exito: bool):
    db.add(LogAcceso(usuario=usuario, accion=accion, detalle=detalle, exito=exito))
    await db.commit()
