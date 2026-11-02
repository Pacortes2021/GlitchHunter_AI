"""
Modelos de Datos y Esquemas Pydantic v2 para GlitchHunter AI.
Define entidades fuertemente tipadas para productos, historial de precios,
análisis semántico, veredictos de glitch y alertas en tiempo real.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field


class GlitchSeverity(str, Enum):
    """Niveles de severidad y prioridad para un glitch de precio detectado."""
    CRITICO = "CRITICO"            # Descuento masivo (>=85%) o caída de ceros en producto de alto valor
    ALTO = "ALTO"                  # Descuento muy fuerte (>=65%) con ahorro significativo
    MODERADO = "MODERADO"          # Descuento sospechoso pero dentro de márgenes de liquidación
    FALSO_POSITIVO = "FALSO_POSITIVO"  # Descartado (ej. accesorio confundido, precio inflado previo)


class ProductItem(BaseModel):
    """
    Representa un producto extraído de un retail chileno en un momento dado.
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="Identificador único del producto en el retail o hash normalizado")
    store_id: str = Field(..., description="ID de tienda: falabella, ripley, paris, lider, mercadolibre, pcfactory")
    store_name: str = Field(..., description="Nombre comercial legible de la tienda")
    title: str = Field(..., description="Título completo del producto publicado")
    category: str = Field(..., description="Categoría detectada o reportada (ej: smartphones, notebooks, consolas)")
    current_price: float = Field(..., ge=0.0, description="Precio actual de oferta o venta normal")
    normal_price: float = Field(..., ge=0.0, description="Precio de referencia / lista previo al descuento")
    card_price: Optional[float] = Field(default=None, ge=0.0, description="Precio exclusivo con tarjeta de la tienda (CMR, Ripley, Cencosud)")
    url: str = Field(..., description="Enlace directo a la publicación del producto")
    add_to_cart_url: Optional[str] = Field(default=None, description="URL directa para agregar al carrito en 1 clic (Sniper Mode)")
    image_url: Optional[str] = Field(default=None, description="URL de la imagen principal del producto")
    in_stock: bool = Field(default=True, description="Indica si el producto tiene stock disponible")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Fecha y hora de captura")

    @property
    def best_price(self) -> float:
        """Retorna el menor precio disponible (considerando precio tarjeta si existe)."""
        if self.card_price is not None and 0.0 < self.card_price < self.current_price:
            return self.card_price
        return self.current_price

    @property
    def discount_percentage(self) -> float:
        """Calcula el porcentaje de descuento respecto al precio normal de referencia."""
        if self.normal_price <= 0.0:
            return 0.0
        effective_price = self.best_price
        diff = self.normal_price - effective_price
        if diff <= 0.0:
            return 0.0
        return round((diff / self.normal_price) * 100.0, 2)

    @property
    def absolute_savings_clp(self) -> float:
        """Calcula el ahorro absoluto en pesos chilenos (CLP)."""
        effective_price = self.best_price
        diff = self.normal_price - effective_price
        return round(max(0.0, diff), 2)


class PriceHistoryRecord(BaseModel):
    """
    Registro histórico de precio para seguimiento cronológico y detección de inflación artificial.
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    product_id: str = Field(..., description="ID del producto monitoreado")
    recorded_at: datetime = Field(default_factory=datetime.utcnow, description="Fecha y hora del registro")
    price: float = Field(..., ge=0.0, description="Precio registrado en esa fecha")
    store_id: str = Field(..., description="ID de la tienda retail")


class SemanticClassificationResult(BaseModel):
    """
    Resultado del análisis semántico con IA para filtrar falsos positivos
    (ej: distinguir una carcasa de iPhone a $9.990 de un iPhone a $9.990).
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    is_accessory: bool = Field(..., description="True si es accesorio, repuesto, funda, lámina o cable")
    is_main_device: bool = Field(..., description="True si es el equipo principal (consola, smartphone, laptop)")
    detected_item_type: str = Field(..., description="Tipo semántico detectado (ej: smartphone, carcasa_funda, cable, notebook)")
    explanation: str = Field(..., description="Justificación breve del clasificador semántico")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Nivel de confianza en la clasificación (0.0 a 1.0)")


class GlitchVerdict(BaseModel):
    """
    Dictamen final emitido por el motor de detección de anomalías y reglas de negocio.
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    is_glitch: bool = Field(..., description="True si cumple todos los criterios para ser considerado glitch real")
    severity: GlitchSeverity = Field(..., description="Severidad del glitch")
    confidence_score: float = Field(..., ge=0.0, le=100.0, description="Puntuación de certeza global (0.0 a 100.0)")
    discount_percentage: float = Field(..., description="Porcentaje de descuento analizado")
    absolute_savings_clp: float = Field(..., description="Ahorro total estimado en CLP")
    is_missing_zero: bool = Field(default=False, description="True si coincide con patrón de 'falta un cero' (~10x)")
    is_fake_inflation: bool = Field(default=False, description="True si el precio normal fue inflado artificialmente antes")
    semantic_check: SemanticClassificationResult = Field(..., description="Desglose del análisis semántico")
    reason: str = Field(..., description="Explicación detallada del veredicto para el usuario / log")


class GlitchAlert(BaseModel):
    """
    Entidad de alerta generada para notificación inmediata (Telegram, Webhook, Dashboard).
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    alert_id: str = Field(default_factory=lambda: f"ALT-{uuid.uuid4().hex[:8].upper()}", description="Identificador único de alerta")
    product: ProductItem = Field(..., description="Producto que generó la alerta")
    verdict: GlitchVerdict = Field(..., description="Veredicto y análisis del glitch")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Fecha y hora de generación de la alerta")
    alert_url: str = Field(..., description="Enlace para comprar o ver el producto")
    add_to_cart_url: Optional[str] = Field(default=None, description="URL directa para compra Sniper en 1 clic")
    is_watchlist_match: bool = Field(default=False, description="True si coincide con palabras clave monitoreadas por el usuario")
    telegram_sent: bool = Field(default=False, description="Indica si la alerta ya fue despachada por Telegram")


class TelegramAlertPayload(BaseModel):
    """
    Esquema formateado para la API de bots de Telegram.
    """
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    chat_id: str = Field(..., description="ID del canal o chat de Telegram de destino")
    text: str = Field(..., description="Mensaje formateado listo para enviar")
    parse_mode: str = Field(default="HTML", description="Modo de parsing (HTML o MarkdownV2)")
    reply_markup: Optional[Dict[str, Any]] = Field(default=None, description="Botones inline o teclado interactivo")
    photo_url: Optional[str] = Field(default=None, description="URL de la imagen del producto para banner")
