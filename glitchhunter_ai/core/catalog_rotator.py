"""
Módulo de Cobertura Ampliada y Rotación de Catálogo (Catalog Rotator).

Permite maximizar la superficie de monitoreo a través de:
1. Rotación Round-Robin de categorías clave entre ciclos de escaneo para
   no saturar los sitios web ni disparar defensas anti-bot.
2. Estrategias de ordenamiento prioritario:
   - 'discount_desc' (mayor descuento): donde se concentran los errores de 80%-95% de descuento.
   - 'price_asc' (menor precio): donde se concentran los bugs de pérdida de ceros (~10x).
3. Pool expandido de categorías de alta probabilidad de error en retail chileno.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("glitchhunter.core.catalog_rotator")


class SortStrategy(str, Enum):
    """Estrategia de ordenamiento para priorizar la detección de glitches."""
    DISCOUNT_DESC = "discount_desc"   # Mayor descuento (errores % masivos)
    PRICE_ASC = "price_asc"           # Menor precio (errores missing-zero y tipeo)


@dataclass
class CategoryDefinition:
    """Definición de categoría tecnológica para escaneo rotativo."""
    key: str
    display_name: str
    search_terms: List[str]
    solotodo_category_id: Optional[int] = None
    default_sort: SortStrategy = SortStrategy.DISCOUNT_DESC


@dataclass
class CategoryBatch:
    """Conjunto de categorías y estrategia de ordenamiento para un ciclo de escaneo."""
    cycle_number: int
    categories: List[CategoryDefinition]
    strategy: SortStrategy
    all_search_terms: List[str] = field(default_factory=list)

    def __post_init__(self):
        terms = []
        for cat in self.categories:
            terms.extend(cat.search_terms)
        self.all_search_terms = terms


# Catálogo maestro de 10 categorías críticas de retail chileno
DEFAULT_CATEGORIES: List[CategoryDefinition] = [
    CategoryDefinition(
        key="smartphones",
        display_name="Smartphones y Telefonía",
        search_terms=["iphone 15", "samsung galaxy s24", "smartphone xiaomi", "celular motorola"],
        solotodo_category_id=6,
        default_sort=SortStrategy.DISCOUNT_DESC,
    ),
    CategoryDefinition(
        key="notebooks",
        display_name="Laptops y Computación Portátil",
        search_terms=["macbook pro", "notebook gamer", "thinkpad", "laptop asus tuf"],
        solotodo_category_id=1,
        default_sort=SortStrategy.PRICE_ASC,
    ),
    CategoryDefinition(
        key="televisores",
        display_name="Smart TVs y Pantallas OLED/QLED",
        search_terms=["smart tv oled", "samsung qled 65", "lg c3 55", "televisor 4k"],
        solotodo_category_id=11,
        default_sort=SortStrategy.DISCOUNT_DESC,
    ),
    CategoryDefinition(
        key="consolas",
        display_name="Consolas y Videojuegos",
        search_terms=["playstation 5 slim", "nintendo switch oled", "xbox series x"],
        solotodo_category_id=33,
        default_sort=SortStrategy.PRICE_ASC,
    ),
    CategoryDefinition(
        key="componentes",
        display_name="Componentes PC (GPUs, CPUs, SSD)",
        search_terms=["rtx 4070 super", "rtx 4080", "ryzen 7 7800x3d", "ssd 2tb nvme"],
        solotodo_category_id=2,
        default_sort=SortStrategy.PRICE_ASC,
    ),
    CategoryDefinition(
        key="monitores",
        display_name="Monitores Gamer y Curvos",
        search_terms=["monitor 165hz", "monitor oled curvo", "monitor 27 144hz"],
        solotodo_category_id=4,
        default_sort=SortStrategy.DISCOUNT_DESC,
    ),
    CategoryDefinition(
        key="audio",
        display_name="Audio y Cancelación de Ruido",
        search_terms=["sony wh-1000xm5", "airpods pro", "soundbar samsung", "parlante jbl"],
        solotodo_category_id=50,
        default_sort=SortStrategy.DISCOUNT_DESC,
    ),
    CategoryDefinition(
        key="smartwatch",
        display_name="Smartwatches y Wearables",
        search_terms=["apple watch ultra", "galaxy watch 6", "reloj garmin"],
        solotodo_category_id=30,
        default_sort=SortStrategy.PRICE_ASC,
    ),
    CategoryDefinition(
        key="linea_blanca",
        display_name="Electro y Línea Blanca Premium",
        search_terms=["refrigerador inverter", "lavadora secadora", "cafetera espresso automatica"],
        solotodo_category_id=37,
        default_sort=SortStrategy.DISCOUNT_DESC,
    ),
    CategoryDefinition(
        key="gaming",
        display_name="Periféricos Gaming y Simulación",
        search_terms=["volante logitech g29", "teclado mecanico gamer", "mouse inalambrico gamer"],
        solotodo_category_id=33,
        default_sort=SortStrategy.PRICE_ASC,
    ),
]


class CatalogRotator:
    """
    Gestor de rotación de categorías y ordenamiento prioritario.
    Mantiene un cursor atómico para ciclar equitativamente por todo el catálogo
    en escaneos sucesivos.
    """

    def __init__(
        self,
        categories: Optional[List[CategoryDefinition]] = None,
        batch_size: int = 3,
    ) -> None:
        self._categories = categories or list(DEFAULT_CATEGORIES)
        self.batch_size = max(1, batch_size)
        self._current_index = 0
        self._cycle_counter = 0
        self._lock = threading.Lock()
        self._current_strategy = SortStrategy.DISCOUNT_DESC

    def get_next_batch(self, batch_size: Optional[int] = None) -> CategoryBatch:
        """
        Retorna el siguiente conjunto de categorías a escanear en rotación Round-Robin.
        Alterna la estrategia de ordenamiento entre DISCOUNT_DESC y PRICE_ASC.
        """
        size = batch_size or self.batch_size
        with self._lock:
            self._cycle_counter += 1
            num_categories = len(self._categories)
            if num_categories == 0:
                return CategoryBatch(
                    cycle_number=self._cycle_counter,
                    categories=[],
                    strategy=self._current_strategy,
                )

            # Selección de categorías circulares
            batch_cats: List[CategoryDefinition] = []
            for i in range(size):
                idx = (self._current_index + i) % num_categories
                batch_cats.append(self._categories[idx])

            # Avanzar cursor para el próximo ciclo
            self._current_index = (self._current_index + size) % num_categories

            # Alternar estrategia de ordenamiento
            if self._cycle_counter % 2 == 1:
                self._current_strategy = SortStrategy.DISCOUNT_DESC
            else:
                self._current_strategy = SortStrategy.PRICE_ASC

            batch = CategoryBatch(
                cycle_number=self._cycle_counter,
                categories=batch_cats,
                strategy=self._current_strategy,
            )

            logger.info(
                "🔄 [Rotación Catálogo] Ciclo #%d: Escaneando %d categorías (%s) con orden '%s'",
                self._cycle_counter,
                len(batch_cats),
                ", ".join(c.display_name for c in batch_cats),
                self._current_strategy.value,
            )
            return batch

    def get_status(self) -> Dict[str, Any]:
        """Retorna el estado actual de la rotación para diagnóstico y dashboard."""
        with self._lock:
            total = len(self._categories)
            return {
                "cycle_counter": self._cycle_counter,
                "current_cursor_index": self._current_index,
                "total_categories": total,
                "current_strategy": self._current_strategy.value,
                "batch_size": self.batch_size,
                "categories": [c.key for c in self._categories],
            }

    def reset(self) -> None:
        """Reinicia el cursor de rotación al inicio."""
        with self._lock:
            self._current_index = 0
            self._cycle_counter = 0
            self._current_strategy = SortStrategy.DISCOUNT_DESC


# Instancia singleton predeterminada
default_catalog_rotator = CatalogRotator()
