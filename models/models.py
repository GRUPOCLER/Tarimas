from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text, ForeignKey, Enum, text, LargeBinary
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
    # Fechas de impresion (UTC) de etiquetas sueltas y lista de empaque
    sueltas_primera_en  = Column(DateTime, nullable=True)
    sueltas_ultima_en   = Column(DateTime, nullable=True)
    sueltas_ultima_por  = Column(String(50), nullable=True)
    packing_primera_en  = Column(DateTime, nullable=True)
    packing_ultima_en   = Column(DateTime, nullable=True)
    packing_ultima_por  = Column(String(50), nullable=True)
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
    tipo_bulto     = Column(String(10), default="tarima")  # "tarima" | "caja"
    ids_entregas_fusionadas = Column(Text, nullable=True)  # "ID1,ID2,ID3" — mismo cliente
    impresa_veces        = Column(Integer, default=0)
    primera_impresion_en = Column(DateTime, nullable=True)
    primera_impresion_por= Column(String(50), nullable=True)
    ultima_impresion_en  = Column(DateTime, nullable=True)
    ultima_impresion_por = Column(String(50), nullable=True)
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
    # Gestion local: campos editados en la plataforma (JSON ["peso_unit", ...]); la hoja ya no los pisa
    linea          = Column(String(12))  # 'refaccion' | None (equipo)
    campos_manuales = Column(Text)
    origen          = Column(String(12), default="hoja")      # hoja | manual
    editado_por     = Column(String(50))
    editado_en      = Column(DateTime)
    # Fuente por campo: lo que dijo la hoja (JSON) y de donde sale el valor vigente (hoja | odoo | manual | sin_dato)
    valores_hoja    = Column(Text)
    fuentes         = Column(Text)


class CatalogoOdoo(Base):
    """Copia (solo lectura) de lo que Odoo tiene de cada producto: peso, volumen, medidas y empaques."""
    __tablename__ = "catalogo_odoo"
    sku            = Column(String(40), primary_key=True)
    encontrado     = Column(Boolean, default=False)
    odoo_id        = Column(Integer)
    datos          = Column(Text)          # JSON con los valores ya convertidos a kg / cm / m3
    sincronizado_en = Column(DateTime, server_default=func.now())


class CatalogoHistorial(Base):
    """Bitacora de cambios del catalogo (ediciones, altas, sincronizaciones, empaques adicionales)."""
    __tablename__ = "catalogo_historial"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    sku            = Column(String(40), index=True, nullable=False)
    campo          = Column(String(40))
    valor_anterior = Column(Text)
    valor_nuevo    = Column(Text)
    origen         = Column(String(12))     # manual | hoja | alta | adicional
    motivo         = Column(Text)
    usuario        = Column(String(50))
    creado_en      = Column(DateTime, server_default=func.now(), index=True)


class CatalogoAdicional(Base):
    """Empaque extra declarado para un SKU (extension, adicional o accesorio). Al cargar una entrega con ese SKU
    se crea solo, sin que el operador tenga que inventarlo."""
    __tablename__ = "catalogo_adicionales"
    id                  = Column(String(12), primary_key=True, default=gen_id)
    sku_base            = Column(String(40), index=True, nullable=False)
    tipo                = Column(String(20), default="extension")     # extension | adicional | accesorio
    nombre              = Column(String(120), nullable=False)
    corresponde_a       = Column(Text)                                 # a que corresponde (parte, funcion, proveedor...)
    cantidad_por_equipo = Column(Integer, default=1)
    peso_kg             = Column(Float, default=0)
    lr                  = Column(Float, default=0)
    an                  = Column(Float, default=0)
    al                  = Column(Float, default=0)
    activo              = Column(Boolean, default=True)
    creado_por          = Column(String(50))
    creado_en           = Column(DateTime, server_default=func.now())
    actualizado_en      = Column(DateTime, server_default=func.now())


# ── LOG DE ACCESOS ────────────────────────────────────────────
class LogAcceso(Base):
    __tablename__ = "log_accesos"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    fecha   = Column(DateTime, server_default=func.now())  # UTC — fuente de verdad
    fecha_local = Column(DateTime, server_default=text("(now() AT TIME ZONE 'America/Mexico_City')"))  # hora Veracruz, solo para auditoria directa en BD
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
    fecha_solicitud        = Column(DateTime, server_default=func.now())  # UTC — fuente de verdad
    fecha_solicitud_local  = Column(DateTime, server_default=text("(now() AT TIME ZONE 'America/Mexico_City')"))
    estatus                = Column(String(20), default="pendiente")  # pendiente | aprobada | rechazada | usada
    autorizado_por         = Column(String(50), nullable=True)
    fecha_resolucion       = Column(DateTime, nullable=True)
    comentario_resolucion  = Column(Text, nullable=True)
    datos_json             = Column(Text, nullable=True)  # PACKING_PARCIAL: seleccion de bultos/cantidades

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
    fecha_solicitud        = Column(DateTime, server_default=func.now())  # UTC — fuente de verdad
    fecha_solicitud_local  = Column(DateTime, server_default=text("(now() AT TIME ZONE 'America/Mexico_City')"))
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
    tipo               = Column(String(10), default="destino")  # "destino" | "origen"
    nombre             = Column(String(120))
    codigo             = Column(String(30))
    activo             = Column(Boolean, default=True)
    agregado_por       = Column(String(50))
    fecha_agregado     = Column(DateTime, server_default=func.now())

# ── ALMACENES AUTORIZADOS POR USUARIO (visibilidad de OVs/Traspasos) ─
class UsuarioAlmacen(Base):
    __tablename__ = "usuarios_almacenes"
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    usuario            = Column(String(50), index=True)
    odoo_warehouse_id  = Column(Integer, nullable=False)
    nombre             = Column(String(120))
    codigo             = Column(String(30))
    agregado_por       = Column(String(50))
    fecha_agregado     = Column(DateTime, server_default=func.now())


# ══════════════════════════════════════════════════════════════
#  CONTROL DE OV (refacciones): surtido -> preparacion -> entrega SAP -> factura
# ══════════════════════════════════════════════════════════════
class OrdenVenta(Base):
    __tablename__ = "ordenes_venta"
    id               = Column(String(30), primary_key=True)          # = num_ov de SAP
    num_ov           = Column(String(30), unique=True, index=True)
    fecha_ov         = Column(String(12))
    emisor           = Column(String(120))                           # razon social que emite la OV
    cliente          = Column(String(200), index=True)
    cliente_codigo   = Column(String(30))
    cliente_rfc      = Column(String(20))
    destino          = Column(String(200))
    destino_ciudad   = Column(String(120))
    fecha_entrega    = Column(String(12))
    fecha_cancelacion= Column(String(12))
    referencia       = Column(String(200))
    notas_json       = Column(Text)
    permite_parcial  = Column(Boolean, default=False)
    almacen          = Column(String(30))
    moneda           = Column(String(8))
    total            = Column(Float, default=0)
    estatus          = Column(String(20), default="recibida", index=True)
    estatus_desde    = Column(DateTime, server_default=func.now())
    creado_en        = Column(DateTime, server_default=func.now())
    creado_por       = Column(String(50))
    nota_faltantes   = Column(Text)
    # entrega SAP
    entrega_folio    = Column(String(30), index=True)
    entrega_fecha    = Column(String(12))
    entrega_en       = Column(DateTime)
    entrega_por      = Column(String(50))
    # factura
    factura_folio    = Column(String(30))
    factura_uuid     = Column(String(40))
    factura_fecha    = Column(String(12))
    factura_en       = Column(DateTime)
    factura_por      = Column(String(50))
    # responsables (usuario asignado por etapa)
    asig_surtido     = Column(String(50), index=True)
    asig_surtido_en  = Column(DateTime)
    asig_surtido_por = Column(String(50))
    asig_prep        = Column(String(50), index=True)
    asig_prep_en     = Column(DateTime)
    asig_prep_por    = Column(String(50))


class OVLinea(Base):
    __tablename__ = "ov_lineas"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    ov_id            = Column(String(30), ForeignKey("ordenes_venta.id"), index=True, nullable=False)
    linea            = Column(Integer)
    codigo           = Column(String(60))
    codigo_norm      = Column(String(60), index=True)
    descripcion      = Column(Text)
    cantidad         = Column(Integer, default=0)
    surtida          = Column(Integer, default=0)
    empacada         = Column(Integer, default=0)
    precio           = Column(Float, default=0)
    tipo             = Column(String(40))
    marca            = Column(String(60))
    division         = Column(String(60))
    grupo            = Column(String(80))
    en_mezanine      = Column(Boolean, default=False)
    motivo_faltante  = Column(String(200))
    entrega_cantidad = Column(Integer)          # lo que quedo en la entrega de SAP (conciliacion)
    entrega_lotes    = Column(Text)             # JSON [{lote, cantidad}]


class OVEvento(Base):
    __tablename__ = "ov_eventos"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    ov_id   = Column(String(30), ForeignKey("ordenes_venta.id"), index=True, nullable=False)
    estatus = Column(String(20))
    en      = Column(DateTime, server_default=func.now())
    por     = Column(String(50))
    nota    = Column(Text)


class OVEscaneo(Base):
    __tablename__ = "ov_escaneos"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    ov_id    = Column(String(30), ForeignKey("ordenes_venta.id"), index=True, nullable=False)
    linea_id = Column(Integer, index=True)
    caja_id  = Column(Integer)
    etapa    = Column(String(15))               # surtido | preparacion
    cantidad = Column(Integer)                  # negativo = correccion
    codigo   = Column(String(60))
    metodo   = Column(String(10), default="scan")   # scan | manual | total
    usuario  = Column(String(50))
    ts       = Column(DateTime, server_default=func.now())


class OVCaja(Base):
    __tablename__ = "ov_cajas"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    ov_id            = Column(String(30), ForeignKey("ordenes_venta.id"), index=True, nullable=False)
    numero           = Column(Integer)
    estatus          = Column(String(10), default="abierta")      # abierta | cerrada
    peso_kg          = Column(Float)
    creada_en        = Column(DateTime, server_default=func.now())
    creada_por       = Column(String(50))
    cerrada_en       = Column(DateTime)
    cerrada_por      = Column(String(50))
    impresa_veces    = Column(Integer, default=0)
    primera_impresion_en  = Column(DateTime)
    ultima_impresion_en   = Column(DateTime)
    ultima_impresion_por  = Column(String(50))


class OVCajaItem(Base):
    __tablename__ = "ov_caja_items"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    caja_id  = Column(Integer, ForeignKey("ov_cajas.id"), index=True, nullable=False)
    linea_id = Column(Integer, index=True)
    cantidad = Column(Integer, default=0)


class CatalogoRefacciones(Base):
    """Maestro de categorias (archivo CATEGORIAS de SAP): tipo, marca, division, grupo."""
    __tablename__ = "catalogo_refacciones"
    codigo_norm = Column(String(60), primary_key=True)
    codigo      = Column(String(60))
    descripcion = Column(Text)
    tipo        = Column(String(40))
    marca       = Column(String(60))
    division    = Column(String(60))
    grupo       = Column(String(80))
    en_catalogo = Column(Boolean, default=False)
    en_mezanine = Column(Boolean, default=False)


# ── ACCESO POR MODULO ────────────────────────────────────────
class OVAdjunto(Base):
    """PDF / XML de una OV (OV, entrega, factura). Se guardan en la base para no depender de disco."""
    __tablename__ = "ov_adjuntos"
    id         = Column(String(12), primary_key=True, default=gen_id)
    ov_id      = Column(String(30), ForeignKey("ordenes_venta.id"), index=True, nullable=False)
    tipo       = Column(String(20), nullable=False)
    nombre     = Column(String(150))
    mime       = Column(String(60))
    tam        = Column(Integer, default=0)
    subido_en  = Column(DateTime, server_default=func.now())
    subido_por = Column(String(50))
    contenido  = Column(LargeBinary)


class OVConfig(Base):
    __tablename__ = "ov_config"
    clave = Column(String(40), primary_key=True)
    valor = Column(String(200))


class UsuarioModulo(Base):
    """Un usuario tiene acceso a un modulo con un rol propio en ese modulo.
    Los admin entran a todo y no necesitan filas aqui."""
    __tablename__ = "usuario_modulos"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    usuario = Column(String(50), nullable=False, index=True)
    modulo  = Column(String(20), nullable=False)
    rol     = Column(String(20), nullable=False, default="operador")
