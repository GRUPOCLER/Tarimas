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
    tarimas     = "TAR"
    carga_suelta= "CS"
    mixta       = "MIX"

class EstatusEntrega(str, enum.Enum):
    pendiente  = "pendiente"
    completada = "completada"

# ── USUARIOS ─────────────────────────────────────────────────
class Usuario(Base):
    __tablename__ = "usuarios"
    id             = Column(String, primary_key=True, default=gen_id)
    usuario        = Column(String(50), unique=True, nullable=False, index=True)
    email          = Column(String(120), nullable=True, index=True)
    password_hash  = Column(String(64), nullable=False)
    rol            = Column(String(20), default="operador")  # admin | gerente | operador
    activo         = Column(Boolean, default=True)
    nombre_display = Column(String(100))
    ultimo_acceso  = Column(DateTime)
    creado_en      = Column(DateTime, server_default=func.now())

# ── ENTREGAS ──────────────────────────────────────────────────
class Entrega(Base):
    __tablename__ = "entregas"
    id_entrega     = Column(String(20), primary_key=True)
    num_entrega    = Column(String(60), index=True)
    sistema        = Column(String(20), nullable=False)   # TAR | CS | MIX
    nombre_cliente = Column(String(200))
    rfc_cliente    = Column(String(20))
    direccion      = Column(Text)
    orden          = Column(String(40), index=True)   # OV Odoo
    fecha_entrega  = Column(String(20))
    fecha_creacion = Column(DateTime, server_default=func.now())
    estatus        = Column(String(20), default="pendiente")  # pendiente | completada
    comercializador= Column(String(40))
    sucursal       = Column(String(60))
    fuente         = Column(String(20), default="pdf") # pdf | odoo | manual
    creado_por     = Column(String(50))
    etiquetas_sueltas_impresas_veces = Column(Integer, default=0)
    packing_impreso_veces            = Column(Integer, default=0)
    # Relaciones
    productos      = relationship("Producto", back_populates="entrega", cascade="all,delete")
    tarimas        = relationship("Tarima",   back_populates="entrega", cascade="all,delete")

# ── PRODUCTOS ─────────────────────────────────────────────────
class Producto(Base):
    __tablename__ = "productos"
    id_producto        = Column(String(30), primary_key=True)
    id_entrega         = Column(String(20), ForeignKey("entregas.id_entrega"), nullable=False)
    clave              = Column(String(40), index=True)
    descripcion        = Column(Text)
    cantidad_total     = Column(Integer, default=0)
    cantidad_asignada  = Column(Integer, default=0)
    cantidad_pendiente = Column(Integer, default=0)
    unidad             = Column(String(20), default="PZA")
    es_extension       = Column(Boolean, default=False)
    # Relaciones
    entrega            = relationship("Entrega", back_populates="productos")

# ── TARIMAS ───────────────────────────────────────────────────
class Tarima(Base):
    __tablename__ = "tarimas"
    id_tarima      = Column(String(30), primary_key=True)
    id_entrega     = Column(String(20), ForeignKey("entregas.id_entrega"), nullable=False)
    estatus        = Column(String(20), default="abierta")
    fecha_creacion = Column(DateTime, server_default=func.now())
    fecha_cierre   = Column(DateTime, nullable=True)
    comentario     = Column(Text)
    cerrado_por    = Column(String(50))
    peso_palet_kg  = Column(Float, default=0)
    largo_cm       = Column(Float, default=0)
    ancho_cm       = Column(Float, default=0)
    alto_cm        = Column(Float, default=0)
    ids_entregas_fusionadas = Column(Text, nullable=True)  # "ID1,ID2,ID3" — mismo cliente
    impresa_veces        = Column(Integer, default=0)
    primera_impresion_en = Column(DateTime, nullable=True)
    primera_impresion_por= Column(String(50), nullable=True)
    # Relaciones
    entrega        = relationship("Entrega", back_populates="tarimas")
    detalles       = relationship("DetalleTarima", back_populates="tarima", cascade="all,delete")

# ── DETALLE DE TARIMA — reparto de cantidades por SKU ──────────
class DetalleTarima(Base):
    __tablename__ = "detalle_tarimas"
    id_detalle        = Column(String(35), primary_key=True)
    id_tarima         = Column(String(30), ForeignKey("tarimas.id_tarima"), nullable=False, index=True)
    id_producto        = Column(String(30), ForeignKey("productos.id_producto"), nullable=False, index=True)
    clave              = Column(String(40))
    descripcion        = Column(Text)
    cantidad_asignada  = Column(Integer, default=0)
    unidad             = Column(String(20), default="PZA")
    fecha_asignacion   = Column(DateTime, server_default=func.now())
    # Relaciones
    tarima             = relationship("Tarima", back_populates="detalles")

# ── CATÁLOGO HMCK (cache local) ───────────────────────────────
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

# ── LOG DE ACCESOS ────────────────────────────────────────────
class LogAcceso(Base):
    __tablename__ = "log_accesos"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    fecha   = Column(DateTime, server_default=func.now())
    usuario = Column(String(50))
    accion  = Column(String(50))
    detalle = Column(Text)
    exito   = Column(Boolean, default=True)

# ── SOLICITUDES DE REIMPRESION ──────────────────────────────────
class SolicitudReimpresion(Base):
    __tablename__ = "solicitudes_reimpresion"
    id                     = Column(String(35), primary_key=True)
    tipo                   = Column(String(20))   # TARIMA | SUELTAS | PACKING
    id_entrega             = Column(String(20), index=True)
    referencia             = Column(String(40))   # id_tarima o id_entrega, para mostrar
    num_entrega            = Column(String(60))   # folio, para mostrar sin joins
    motivo                 = Column(Text)
    solicitado_por         = Column(String(50))
    fecha_solicitud        = Column(DateTime, server_default=func.now())
    estatus                = Column(String(20), default="pendiente")  # pendiente | aprobada | rechazada | usada
    autorizado_por         = Column(String(50), nullable=True)
    fecha_resolucion       = Column(DateTime, nullable=True)
    comentario_resolucion  = Column(Text, nullable=True)

# ── SOLICITUDES DE CAMBIO DE SISTEMA (TAR/CS/MIX) ───────────────
class SolicitudCambioSistema(Base):
    __tablename__ = "solicitudes_cambio_sistema"
    id                     = Column(String(35), primary_key=True)
    id_entrega             = Column(String(20), index=True)
    num_entrega            = Column(String(60))
    sistema_actual         = Column(String(10))
    sistema_nuevo          = Column(String(10))
    motivo                 = Column(Text)
    solicitado_por         = Column(String(50))
    fecha_solicitud        = Column(DateTime, server_default=func.now())
    estatus                = Column(String(20), default="pendiente")  # pendiente | aprobada | rechazada
    autorizado_por         = Column(String(50), nullable=True)
    fecha_resolucion       = Column(DateTime, nullable=True)
    comentario_resolucion  = Column(Text, nullable=True)

# ── ALMACENES DE TRASPASO CONFIGURADOS (destinos a vigilar en Odoo) ─
class AlmacenTraspaso(Base):
    __tablename__ = "almacenes_traspaso"
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    odoo_warehouse_id  = Column(Integer, nullable=False)
    odoo_location_id   = Column(Integer, nullable=False)  # view_location_id, para el filtro child_of
    nombre             = Column(String(120))
    codigo             = Column(String(30))
    activo             = Column(Boolean, default=True)
    agregado_por       = Column(String(50))
    fecha_agregado     = Column(DateTime, server_default=func.now())
