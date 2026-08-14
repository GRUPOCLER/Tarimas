from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum, uuid
from database import Base

def gen_id():
    return str(uuid.uuid4())[:8].upper()

class RolEnum(str, enum.Enum):
    admin  = "admin"
    editor = "editor"

class SistemaEnum(str, enum.Enum):
    tarimas      = "TAR"
    carga_suelta = "CS"
    mixta        = "MIX"

class EstatusEntrega(str, enum.Enum):
    pendiente  = "pendiente"
    completada = "completada"

class Usuario(Base):
    __tablename__ = "usuarios"
    id             = Column(String, primary_key=True, default=gen_id)
    usuario        = Column(String(50), unique=True, nullable=False, index=True)
    password_hash  = Column(String(64), nullable=False)
    rol            = Column(Enum(RolEnum), default=RolEnum.editor)
    activo         = Column(Boolean, default=True)
    nombre_display = Column(String(100))
    ultimo_acceso  = Column(DateTime)
    creado_en      = Column(DateTime, server_default=func.now())

class Entrega(Base):
    __tablename__ = "entregas"
    id_entrega     = Column(String(20), primary_key=True)
    num_entrega    = Column(String(60), index=True)
    sistema        = Column(Enum(SistemaEnum), nullable=False)
    nombre_cliente = Column(String(200))
    rfc_cliente    = Column(String(20))
    direccion      = Column(Text)
    orden          = Column(String(40), index=True)
    fecha_entrega  = Column(String(20))
    fecha_creacion = Column(DateTime, server_default=func.now())
    estatus        = Column(Enum(EstatusEntrega), default=EstatusEntrega.pendiente)
    comercializador= Column(String(40))
    sucursal       = Column(String(60))
    fuente         = Column(String(20), default="pdf")
    creado_por     = Column(String(50))
    productos      = relationship("Producto", back_populates="entrega", cascade="all,delete")
    tarimas        = relationship("Tarima",   back_populates="entrega", cascade="all,delete")

cclass Producto(Base):
    __tablename__ = "productos"
    id_producto    = Column(String(30), primary_key=True)
    id_entrega     = Column(String(20), ForeignKey("entregas.id_entrega"), nullable=False)
    id_tarima      = Column(String(30), ForeignKey("tarimas.id_tarima"), nullable=True, index=True)
    clave          = Column(String(40), index=True)
    descripcion    = Column(Text)
    cantidad_total = Column(Integer, default=0)
    unidad         = Column(String(20), default="PZA")
    es_extension   = Column(Boolean, default=False)
    # Relaciones
    entrega        = relationship("Entrega", back_populates="productos")
    tarima         = relationship("Tarima", back_populates="productos")

class Tarima(Base):
    __tablename__ = "tarimas"
    id_tarima      = Column(String(30), primary_key=True)
    id_entrega     = Column(String(20), ForeignKey("entregas.id_entrega"), nullable=False)
    estatus        = Column(String(20), default="abierta")
    fecha_creacion = Column(DateTime, server_default=func.now())
    fecha_cierre   = Column(DateTime, nullable=True)
    comentario     = Column(Text)
    cerrado_por    = Column(String(50))
    entrega        = relationship("Entrega", back_populates="tarimas")

class CatalogoItem(Base):
    __tablename__ = "catalogo"
    sku            = Column(String(40), primary_key=True)
    descripcion    = Column(Text)
    peso_unit      = Column(Float, default=0)
    lr_unit        = Column(Float, default=0)
    an_unit        = Column(Float, default=0)
    al_unit        = Column(Float, default=0)
    m3_unit        = Column(Float, default=0)
    cm_cant        = Column(Integer, default=0)
    cm_peso        = Column(Float, default=0)
    cm_lr          = Column(Float, default=0)
    cm_an          = Column(Float, default=0)
    cm_al          = Column(Float, default=0)
    cm_m3          = Column(Float, default=0)
    sat_codigo     = Column(String(20))
    sat_desc       = Column(Text)
    actualizado_en = Column(DateTime, server_default=func.now())

class LogAcceso(Base):
    __tablename__ = "log_accesos"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    fecha   = Column(DateTime, server_default=func.now())
    usuario = Column(String(50))
    accion  = Column(String(50))
    detalle = Column(Text)
    exito   = Column(Boolean, default=True)
