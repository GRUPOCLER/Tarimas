from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from database import engine, Base
from routers import auth, entregas, catalogo, dashboard, setup

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(
    title="Sistema Operativo GRUPO CLER",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,      prefix="/api/auth",      tags=["auth"])
app.include_router(entregas.router,  prefix="/api/entregas",  tags=["entregas"])
app.include_router(catalogo.router,  prefix="/api/catalogo",  tags=["catalogo"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(setup.router,     prefix="/api/setup",     tags=["setup"])

@app.get("/")
def root():
    return {"sistema": "GRUPO CLER", "version": "2.0.0", "status": "ok"}
