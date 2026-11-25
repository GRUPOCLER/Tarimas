from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from database import engine, Base
from routers import auth, entregas, catalogo, dashboard, setup, odoo, admin, reimpresiones, analisis, ov

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crear tablas al iniciar
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS campos_manuales TEXT"))
        await conn.execute(text("ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS origen VARCHAR(12) DEFAULT 'hoja'"))
        await conn.execute(text("ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS editado_por VARCHAR(50)"))
        await conn.execute(text("ALTER TABLE catalogo ADD COLUMN IF NOT EXISTS editado_en TIMESTAMP"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_surtido VARCHAR(50)"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_surtido_en TIMESTAMP"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_surtido_por VARCHAR(50)"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_prep VARCHAR(50)"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_prep_en TIMESTAMP"))
        await conn.execute(text("ALTER TABLE ordenes_venta ADD COLUMN IF NOT EXISTS asig_prep_por VARCHAR(50)"))
        # Migracion ligera: agregar columnas nuevas si no existen
        await conn.execute(text(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS cantidad_asignada INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS cantidad_pendiente INTEGER DEFAULT 0"
        ))
        # Backfill: productos sin asignaciones previas quedan con pendiente = total
        await conn.execute(text(
            "UPDATE productos SET cantidad_pendiente = cantidad_total "
            "WHERE cantidad_asignada = 0 AND (cantidad_pendiente IS NULL OR cantidad_pendiente = 0)"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS peso_palet_kg FLOAT DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS largo_cm FLOAT DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS ancho_cm FLOAT DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS alto_cm FLOAT DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS tipo_bulto VARCHAR(10) DEFAULT 'tarima'"
        ))
        await conn.execute(text(
            "ALTER TABLE log_accesos ADD COLUMN IF NOT EXISTS fecha_local TIMESTAMP "
            "DEFAULT (now() AT TIME ZONE 'America/Mexico_City')"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS ultima_impresion_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS ultima_impresion_por VARCHAR(50)"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS sueltas_primera_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS sueltas_ultima_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS sueltas_ultima_por VARCHAR(50)"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS packing_primera_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS packing_ultima_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS packing_ultima_por VARCHAR(50)"
        ))
        await conn.execute(text(
            "ALTER TABLE solicitudes_reimpresion ADD COLUMN IF NOT EXISTS datos_json TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE solicitudes_reimpresion ADD COLUMN IF NOT EXISTS fecha_solicitud_local TIMESTAMP "
            "DEFAULT (now() AT TIME ZONE 'America/Mexico_City')"
        ))
        await conn.execute(text(
            "ALTER TABLE solicitudes_cambio_sistema ADD COLUMN IF NOT EXISTS fecha_solicitud_local TIMESTAMP "
            "DEFAULT (now() AT TIME ZONE 'America/Mexico_City')"
        ))
        await conn.execute(text(
            "ALTER TABLE almacenes_traspaso ADD COLUMN IF NOT EXISTS tipo VARCHAR(10) DEFAULT 'destino'"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS ids_entregas_fusionadas TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS impresa_veces INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS primera_impresion_en TIMESTAMP"
        ))
        await conn.execute(text(
            "ALTER TABLE tarimas ADD COLUMN IF NOT EXISTS primera_impresion_por VARCHAR(50)"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS etiquetas_sueltas_impresas_veces INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE entregas ADD COLUMN IF NOT EXISTS packing_impreso_veces INTEGER DEFAULT 0"
        ))

    # Convertir columnas enum de entregas a texto simple — en transacciones
    # independientes para que un fallo no aborte la migracion principal
    for stmt in [
        "ALTER TABLE entregas ALTER COLUMN sistema DROP DEFAULT",
        "ALTER TABLE entregas ALTER COLUMN sistema TYPE VARCHAR(20) USING sistema::text",
        "UPDATE entregas SET sistema = 'TAR' WHERE sistema = 'tarimas'",
        "UPDATE entregas SET sistema = 'CS'  WHERE sistema = 'carga_suelta'",
        "UPDATE entregas SET sistema = 'MIX' WHERE sistema = 'mixta'",
        "ALTER TABLE entregas ALTER COLUMN estatus DROP DEFAULT",
        "ALTER TABLE entregas ALTER COLUMN estatus TYPE VARCHAR(20) USING estatus::text",
        "ALTER TABLE entregas ALTER COLUMN estatus SET DEFAULT 'pendiente'",
        "DROP TYPE IF EXISTS sistemaenum",
        "DROP TYPE IF EXISTS estatusentrega",
        "ALTER TABLE usuarios ALTER COLUMN rol DROP DEFAULT",
        "ALTER TABLE usuarios ALTER COLUMN rol TYPE VARCHAR(20) USING rol::text",
        "ALTER TABLE usuarios ALTER COLUMN rol SET DEFAULT 'operador'",
        "UPDATE usuarios SET rol = 'operador' WHERE rol = 'editor'",
        "DROP TYPE IF EXISTS rolenum",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email VARCHAR(120)",
    ]:
        try:
            async with engine.begin() as conn2:
                await conn2.execute(text(stmt))
            print(f"[migracion] OK: {stmt}")
        except Exception as e:
            print(f"[migracion] omitida ({stmt}): {type(e).__name__}: {e}")
    await _migrar_accesos_por_modulo()
    await _asegurar_admins()
    yield

async def _migrar_accesos_por_modulo():
    """Una sola vez: pasa el rol global anterior a accesos por modulo.
    operador/gerente -> Embarques con ese rol; sap/facturacion -> Refacciones.
    Solo corre si usuario_modulos esta vacia (no vuelve a tocar lo que el admin ajuste)."""
    try:
        async with engine.begin() as conn:
            n = (await conn.execute(text("SELECT COUNT(*) FROM usuario_modulos"))).scalar() or 0
            if n:
                return
            rows = (await conn.execute(text("SELECT usuario, rol FROM usuarios WHERE rol <> 'admin'"))).all()
            for usuario, rol in rows:
                if rol in ("operador", "gerente"):
                    mod, r = "embarques", rol
                elif rol in ("sap", "facturacion"):
                    mod, r = "refacciones", rol
                else:
                    mod, r = "embarques", "operador"
                await conn.execute(text("INSERT INTO usuario_modulos (usuario, modulo, rol) VALUES (:u,:m,:r)"),
                                   {"u": usuario, "m": mod, "r": r})
            await conn.execute(text("UPDATE usuarios SET rol = 'usuario' WHERE rol <> 'admin'"))
            print(f"[migracion] accesos por modulo: {len(rows)} usuarios migrados")
    except Exception as e:
        print(f"[migracion] accesos por modulo omitida: {type(e).__name__}: {e}")

async def _asegurar_admins():
    """Recuperacion de acceso: los usuarios o correos listados en la variable ADMIN_USUARIOS
    (separados por coma, p. ej. "procesos,gestion@grupocler.com.mx") quedan como admin y activos
    cada vez que arranca el servidor. Si la variable no existe, no hace nada."""
    import os
    lista = [x.strip().lower() for x in os.getenv("ADMIN_USUARIOS", "").split(",") if x.strip()]
    if not lista:
        return
    try:
        async with engine.begin() as conn:
            for x in lista:
                r = await conn.execute(
                    text("UPDATE usuarios SET rol = 'admin', activo = true WHERE lower(usuario) = :x OR lower(email) = :x"),
                    {"x": x})
                print(f"[admin] {x}: {r.rowcount} usuario(s) dejados como admin")
    except Exception as e:
        print(f"[admin] ADMIN_USUARIOS omitido: {type(e).__name__}: {e}")

app = FastAPI(
    title="Sistema Operativo GRUPO CLER",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cler-frontend-production.up.railway.app",
        "http://localhost:5173",  # desarrollo local
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,      prefix="/api/auth",      tags=["auth"])
app.include_router(entregas.router,  prefix="/api/entregas",  tags=["entregas"])
app.include_router(catalogo.router,  prefix="/api/catalogo",  tags=["catalogo"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(setup.router,     prefix="/api/setup",     tags=["setup"])
app.include_router(odoo.router,      prefix="/api/odoo",      tags=["odoo"])
app.include_router(admin.router,     prefix="/api/admin",     tags=["admin"])
app.include_router(reimpresiones.router, prefix="/api/reimpresiones", tags=["reimpresiones"])
app.include_router(analisis.router, prefix="/api/analisis", tags=["analisis"])
app.include_router(ov.router, prefix="/api/ov", tags=["ov"])

@app.get("/")
def root():
    return {"sistema": "GRUPO CLER", "version": "2.0.0", "status": "ok"}
