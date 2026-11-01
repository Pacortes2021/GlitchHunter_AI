"""
Core module de GlitchHunter AI.
Exporta modelos y estructuras centrales del sistema.
"""

from .models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    PriceHistoryRecord,
    ProductItem,
    SemanticClassificationResult,
    TelegramAlertPayload,
)
from .database import PriceDatabase

__all__ = [
    "GlitchAlert",
    "GlitchSeverity",
    "GlitchVerdict",
    "PriceDatabase",
    "PriceHistoryRecord",
    "ProductItem",
    "SemanticClassificationResult",
    "TelegramAlertPayload",
]
