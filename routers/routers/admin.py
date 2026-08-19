from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from models.models import Usuario, LogAcceso
from services.auth import verificar_token, hashear_password

router = APIRouter()

ROLES_VALIDOS = ("admin", "gerente", "operador")

# ── AUTH / PERMISOS ──────────────────────────────────────────
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

# ── SCHEMAS ───────────────────────────────────────────────────
class UsuarioIn(BaseModel):
    usuario:        str
    password:       str
    nombre_display: str = ""
    rol:            str = "operador"

class UsuarioEditIn(BaseModel):
    nombre_display: Optional[str] = None
    rol:            Optional[str] = None
    activo:         Optional[bool] = None
    password:       Optional[str] = None

# ── LISTAR USUARIOS ────────────────────────────────────────────
@router.get("/usuarios")
async def listar_usuarios(
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(Usuario).order_by(Usuario.creado_en.desc()))
    return [{
        "usuario":        u.usuario,
        "nombre_display": u.nombre_display,
        "rol":            u.rol,
        "activo":         u.activo,
        "ultimo_acceso":  str(u.ultimo_acceso or ""),
        "creado_en":      str(u.creado_en or ""),
    } for u in result.scalars()]

# ── CREAR USUARIO ──────────────────────────────────────────────
@router.post("/usuarios")
async def crear_usuario(
    body: UsuarioIn,
    db:   AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles("admin", "gerente"))
):
    if body.rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=400, detail="Rol invalido")
    if body.rol == "admin" and user["rol"] != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede crear otro Administrador")

    usu_norm = body.usuario.lower().strip()
    existe = await db.execute(select(Usuario).where(Usuario.usuario == usu_norm))
    if existe.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Ese nombre de usuario ya existe")

    nuevo = Usuario(
        usuario=usu_norm, password_hash=hashear_password(body.password.strip()),
        rol=body.rol, activo=True, nombre_display=body.nombre_display or usu_norm
    )
    db.add(nuevo)
    db.add(LogAcceso(usuario=user["sub"], accion="CREAR_USUARIO", detalle=f"Creo a {usu_norm} ({body.rol})", exito=True))
    await db.commit()
    return {"ok": True, "usuario": usu_norm}

# ── EDITAR USUARIO ──────────────────────────────────────────────
@router.patch("/usuarios/{usuario}")
async def editar_usuario(
    usuario: str,
    body:    UsuarioEditIn,
    db:      AsyncSession = Depends(get_db),
    user:    dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(Usuario).where(Usuario.usuario == usuario.lower().strip()))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Un gerente no puede tocar ni ascender a cuentas de administrador
    if (u.rol == "admin" or body.rol == "admin") and user["rol"] != "admin":
        raise HTTPException(status_code=403, detail="Solo un Administrador puede modificar cuentas de Administrador")

    if body.nombre_display is not None:
        u.nombre_display = body.nombre_display
    if body.rol is not None:
        if body.rol not in ROLES_VALIDOS:
            raise HTTPException(status_code=400, detail="Rol invalido")
        u.rol = body.rol
    if body.activo is not None:
        u.activo = body.activo
    if body.password:
        u.password_hash = hashear_password(body.password.strip())

    db.add(LogAcceso(usuario=user["sub"], accion="EDITAR_USUARIO", detalle=f"Modifico a {usuario}", exito=True))
    await db.commit()
    return {"ok": True}

# ── LOG DE ACCESOS / ACCIONES ────────────────────────────────────
@router.get("/logs")
async def ver_logs(
    limite: int = 100,
    db:     AsyncSession = Depends(get_db),
    user:   dict = Depends(require_roles("admin", "gerente"))
):
    result = await db.execute(select(LogAcceso).order_by(LogAcceso.fecha.desc()).limit(limite))
    return [{
        "id":      l.id,
        "fecha":   str(l.fecha or ""),
        "usuario": l.usuario,
        "accion":  l.accion,
        "detalle": l.detalle,
        "exito":   l.exito,
    } for l in result.scalars()]
