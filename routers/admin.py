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

from sqlalchemy import select as _sel
from models.models import UsuarioModulo
from services.permisos import usuario_actual, guardar_modulos, catalogo, MODULOS, ROLES_INFO

get_current_user = usuario_actual

def solo_admin(user: dict = Depends(usuario_actual)):
    if user.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Solo el administrador puede hacer esto")
    return user

def admin_o_gerente(user: dict = Depends(usuario_actual)):
    es_gerente = any(r == "gerente" for r in user["modulos"].values())
    if user.get("rol") != "admin" and not es_gerente:
        raise HTTPException(status_code=403, detail="No tienes permisos para esta accion")
    return user

# ── SCHEMAS ───────────────────────────────────────────────────
class UsuarioIn(BaseModel):
    usuario:        str
    password:       str
    nombre_display: str = ""
    email:          Optional[str] = None
    es_admin:       bool = False
    modulos:        dict = {}          # {"embarques": "operador", "refacciones": "sap"}

class UsuarioEditIn(BaseModel):
    nombre_display: Optional[str] = None
    email:          Optional[str] = None
    es_admin:       Optional[bool] = None
    modulos:        Optional[dict] = None
    activo:         Optional[bool] = None
    password:       Optional[str] = None

@router.get("/modulos")
async def catalogo_modulos(user: dict = Depends(admin_o_gerente)):
    return {"modulos": catalogo(), "roles": ROLES_INFO}

# ── LISTAR USUARIOS (solo Admin) ────────────────────────────────
@router.get("/usuarios")
async def listar_usuarios(db: AsyncSession = Depends(get_db), user: dict = Depends(solo_admin)):
    result = await db.execute(select(Usuario).order_by(Usuario.creado_en.desc()))
    mods = {}
    for m in (await db.execute(select(UsuarioModulo))).scalars():
        mods.setdefault(m.usuario, {})[m.modulo] = m.rol
    return [{
        "usuario":        u.usuario,
        "email":          u.email,
        "nombre_display": u.nombre_display,
        "es_admin":       u.rol == "admin",
        "modulos":        mods.get(u.usuario, {}),
        "activo":         u.activo,
        "ultimo_acceso":  str(u.ultimo_acceso or ""),
        "creado_en":      str(u.creado_en or ""),
    } for u in result.scalars()]

# ── CREAR USUARIO (Admin: lo que quiera / Gerente: operadores en sus modulos) ─
@router.post("/usuarios")
async def crear_usuario(body: UsuarioIn, db: AsyncSession = Depends(get_db),
                        user: dict = Depends(admin_o_gerente)):
    if user["rol"] == "admin":
        es_admin, modulos = body.es_admin, body.modulos
    else:
        # un gerente solo da de alta operadores, en los modulos donde el es gerente
        es_admin = False
        propios = [m for m, r in user["modulos"].items() if r == "gerente"]
        modulos = {m: "operador" for m in propios if (not body.modulos) or m in body.modulos}

    usu_norm = body.usuario.lower().strip()
    existe = await db.execute(select(Usuario).where(Usuario.usuario == usu_norm))
    if existe.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Ese nombre de usuario ya existe")

    db.add(Usuario(
        usuario=usu_norm, password_hash=hashear_password(body.password.strip()),
        rol="admin" if es_admin else "usuario", activo=True,
        nombre_display=body.nombre_display or usu_norm,
        email=(body.email or "").strip().lower() or None
    ))
    guardados = await guardar_modulos(db, usu_norm, {} if es_admin else modulos)
    detalle = "admin" if es_admin else ", ".join(f"{m}:{r}" for m, r in guardados.items()) or "sin modulos"
    db.add(LogAcceso(usuario=user["sub"], accion="CREAR_USUARIO", detalle=f"Creo a {usu_norm} ({detalle})", exito=True))
    await db.commit()
    return {"ok": True, "usuario": usu_norm}

# ── EDITAR USUARIO (solo Admin) ─────────────────────────────────
@router.patch("/usuarios/{usuario}")
async def editar_usuario(usuario: str, body: UsuarioEditIn, db: AsyncSession = Depends(get_db),
                         user: dict = Depends(solo_admin)):
    result = await db.execute(select(Usuario).where(Usuario.usuario == usuario.lower().strip()))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    if body.nombre_display is not None:
        u.nombre_display = body.nombre_display
    if body.email is not None:
        u.email = body.email.strip().lower() or None
    propio = u.usuario == user["sub"]
    if body.es_admin is not None:
        if propio and not body.es_admin:
            raise HTTPException(status_code=400, detail="No puedes quitarte tu propio acceso de administrador")
        u.rol = "admin" if body.es_admin else "usuario"
    if body.activo is not None:
        if propio and not body.activo:
            raise HTTPException(status_code=400, detail="No puedes desactivar tu propia cuenta")
        u.activo = body.activo
    if body.password:
        u.password_hash = hashear_password(body.password.strip())
    if body.modulos is not None:
        await guardar_modulos(db, u.usuario, body.modulos)

    db.add(LogAcceso(usuario=user["sub"], accion="EDITAR_USUARIO", detalle=f"Modifico a {usuario}", exito=True))
    await db.commit()
    return {"ok": True}

# ── LOG DE ACCESOS / ACCIONES (solo Admin) ──────────────────────
@router.get("/logs")
async def ver_logs(
    limite: int = 100,
    db:     AsyncSession = Depends(get_db),
    user:   dict = Depends(solo_admin)
):
    result = await db.execute(select(LogAcceso).order_by(LogAcceso.fecha.desc()).limit(limite))
    return [{
        "id":      l.id,
        "fecha":   str(l.fecha or ""),
        "fecha_local": str(l.fecha_local or ""),
        "usuario": l.usuario,
        "accion":  l.accion,
        "detalle": l.detalle,
        "exito":   l.exito,
    } for l in result.scalars()]
