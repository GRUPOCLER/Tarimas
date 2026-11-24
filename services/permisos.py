"""
Modulos del sistema y acceso por usuario.
Cada usuario tiene, por modulo, un rol propio. 'admin' (rol global) entra a todo.
Las rutas de cada modulo usan acceso_modulo("<modulo>"): devuelve el usuario con
user["rol"] = rol que tiene EN ESE MODULO (o "admin"), asi los checks existentes
(user["rol"] in (...)) siguen funcionando sin cambios.
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.models import Usuario, UsuarioModulo
from services.auth import verificar_token

ROLES_INFO = {
    "operador":    "Opera el modulo; sin reimpresion, reapertura ni cancelacion",
    "gerente":     "Jefatura del modulo: autoriza, reabre, reimprime y registra operadores",
    "sap":         "Encargado SAP: carga OV y registra la entrega en SAP",
    "facturacion": "Facturacion: registra la factura",
}

MODULOS = {
    "embarques": {
        "nombre": "Embarques", "ruta": "/embarques", "disponible": True,
        "desc": "Entregas, tarimas, cargas sueltas, listas de empaque y analisis",
        "roles": ["operador", "gerente"],
    },
    "inventario": {
        "nombre": "Inventario y Operaciones", "ruta": "/inventario", "disponible": False,
        "desc": "Layout de ubicaciones y conteos de inventario",
        "roles": ["operador", "gerente"],
    },
    "refacciones": {
        "nombre": "Refacciones", "ruta": "/ov", "disponible": True,
        "desc": "Control de OV: surtido, preparacion, entrega SAP, factura y etiquetas",
        "roles": ["operador", "gerente", "sap", "facturacion"],
    },
}

def catalogo():
    return [{"id": k, **v} for k, v in MODULOS.items()]

async def cargar_usuario(db: AsyncSession, usuario: str):
    r = await db.execute(select(Usuario).where(Usuario.usuario == usuario))
    return r.scalar_one_or_none()

async def modulos_de(db: AsyncSession, u: Usuario) -> dict:
    """{modulo: rol}. Admin: todos con 'admin'."""
    if u.rol == "admin":
        return {k: "admin" for k in MODULOS}
    r = await db.execute(select(UsuarioModulo).where(UsuarioModulo.usuario == u.usuario))
    return {m.modulo: m.rol for m in r.scalars() if m.modulo in MODULOS}

async def usuario_actual(authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Usuario autenticado, leido de la BD en cada llamada: si se desactiva
    o se le quita un modulo, el cambio aplica de inmediato (sin esperar al token)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sin autorizacion")
    payload = verificar_token(authorization.split(" ")[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    u = await cargar_usuario(db, payload.get("sub"))
    if not u or not u.activo:
        raise HTTPException(status_code=401, detail="Usuario inactivo")
    mods = await modulos_de(db, u)
    return {**payload, "rol": "admin" if u.rol == "admin" else "usuario", "modulos": mods}

def acceso_modulo(modulo: str):
    async def dep(user: dict = Depends(usuario_actual)):
        rol = user["modulos"].get(modulo)
        if not rol:
            raise HTTPException(status_code=403, detail="No tienes acceso a este modulo")
        return {**user, "rol": rol, "modulo": modulo}
    return dep

async def guardar_modulos(db: AsyncSession, usuario: str, modulos: dict):
    """Reemplaza los accesos del usuario. Valida modulo y rol."""
    limpio = {}
    for m, rol in (modulos or {}).items():
        if m not in MODULOS:
            raise HTTPException(status_code=400, detail=f"Modulo invalido: {m}")
        if not rol:
            continue
        if rol not in MODULOS[m]["roles"]:
            raise HTTPException(status_code=400, detail=f"Rol '{rol}' no existe en {MODULOS[m]['nombre']}")
        limpio[m] = rol
    await db.execute(delete(UsuarioModulo).where(UsuarioModulo.usuario == usuario))
    for m, rol in limpio.items():
        db.add(UsuarioModulo(usuario=usuario, modulo=m, rol=rol))
    return limpio
