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
from .catalog_rotator import CatalogRotator, CategoryBatch, SortStrategy, default_catalog_rotator

__all__ = [
    "CatalogRotator",
    "CategoryBatch",
    "GlitchAlert",
    "GlitchSeverity",
    "GlitchVerdict",
    "PriceDatabase",
    "PriceHistoryRecord",
    "ProductItem",
    "SemanticClassificationResult",
    "SortStrategy",
    "TelegramAlertPayload",
    "default_catalog_rotator",
]
