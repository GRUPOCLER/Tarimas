from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from database import engine, Base
from routers import auth, entregas, catalogo, dashboard, setup, odoo

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crear tablas al iniciar
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
    ]:
        try:
            async with engine.begin() as conn2:
                await conn2.execute(text(stmt))
            print(f"[migracion] OK: {stmt}")
        except Exception as e:
            print(f"[migracion] omitida ({stmt}): {type(e).__name__}: {e}")
    yield

app = FastAPI(
    title="Sistema Operativo GRUPO CLER",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En produccion: URL del frontend
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

@app.get("/")
def root():
    return {"sistema": "GRUPO CLER", "version": "2.0.0", "status": "ok"}
