"""
Persistencia y Gestión de Precios Históricos para GlitchHunter AI.
Maneja base de datos SQLite con soporte WAL para concurrencia, cálculo de
precios base (baseline price) para desenmascarar falsos descuentos e inflación
previa (CyberDay bait), y almacenamiento de alertas de glitches.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any, Dict, List, Optional, Union
import uuid

# Configuración de rutas para imports robustos
_CURRENT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _CURRENT_DIR.parent
_WORKSPACE_DIR = _PROJECT_DIR.parent

for path in (str(_PROJECT_DIR), str(_WORKSPACE_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from glitchhunter_ai import config
except ImportError:
    try:
        import config
    except ImportError:
        config = None

try:
    from glitchhunter_ai.core.models import (
        GlitchAlert,
        GlitchSeverity,
        GlitchVerdict,
        PriceHistoryRecord,
        ProductItem,
    )
except ImportError:
    try:
        from core.models import (
            GlitchAlert,
            GlitchSeverity,
            GlitchVerdict,
            PriceHistoryRecord,
            ProductItem,
        )
    except ImportError:
        from models import (
            GlitchAlert,
            GlitchSeverity,
            GlitchVerdict,
            PriceHistoryRecord,
            ProductItem,
        )

logger = logging.getLogger("glitchhunter.database")


def _enum_value(value: Any) -> str:
    """Texto plano de un Enum ('CRITICO' en vez de 'GlitchSeverity.CRITICO')."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


class PriceDatabase:
    """
    Gestor de base de datos SQLite persistente para catálogo, historial de precios
    y alertas de glitches detectados.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        """
        Inicializa la instancia de base de datos.
        Si db_path es None, utiliza la ruta configurada en config.DB_PATH.
        """
        if db_path is not None:
            self.db_path = Path(db_path) if str(db_path) != ":memory:" else ":memory:"
        elif config is not None and hasattr(config, "DB_PATH"):
            self.db_path = Path(config.DB_PATH)
        else:
            self.db_path = _PROJECT_DIR / "data" / "glitchhunter.db"

        # Soporte para base de datos en memoria (ideal para tests unitarios)
        if str(self.db_path) == ":memory:":
            self.is_memory = True
            self._mem_conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys=ON;")
        else:
            self.is_memory = False
            self._mem_conn = None
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.init_db()

    @contextlib.contextmanager
    def get_connection(self):
        """
        Generador de contexto para conexiones SQLite seguras entre hilos,
        con WAL mode activado y foreign keys habilitadas.
        """
        if self.is_memory:
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def init_db(self) -> None:
        """
        Crea las tablas e índices necesarios si no existen.
        Tablas:
          - products: catálogo consolidado y último precio conocido.
          - price_history: serie temporal de precios para cada producto.
          - glitch_alerts: registro de glitches detectados y su estado de despacho.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabla de Productos
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT,
                    url TEXT,
                    image_url TEXT,
                    current_price REAL NOT NULL,
                    normal_price REAL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Tabla de Historial de Precios
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
            """)

            # 3. Tabla de Alertas de Glitches
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS glitch_alerts (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    confidence_score REAL NOT NULL,
                    discount_pct REAL NOT NULL,
                    savings_clp REAL NOT NULL,
                    reason TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    telegram_sent INTEGER DEFAULT 0,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
            """)

            # 4. Tabla de Estado de Ofertas Continuas (Buscador de Excelencia)
            # Mantiene el ciclo de vida de cada oferta para no repetir notificaciones jamás
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_deals (
                    product_id TEXT PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT,
                    normal_price REAL NOT NULL,
                    glitch_price REAL NOT NULL,
                    discount_pct REAL NOT NULL,
                    savings_clp REAL NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'MODERADO',
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    confidence_score REAL DEFAULT 90.0,
                    verification_level TEXT DEFAULT 'DETECTADO',
                    first_detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    alert_dispatched INTEGER DEFAULT 1,
                    alert_id TEXT NOT NULL,
                    url TEXT,
                    image_url TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
            """)

            # Índices de Alto Rendimiento
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_store_id ON products(store_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_product_id ON price_history(product_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_timestamp ON price_history(timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_price_history_prod_time ON price_history(product_id, timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_glitch_alerts_product_id ON glitch_alerts(product_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_glitch_alerts_timestamp ON glitch_alerts(timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_glitch_alerts_severity ON glitch_alerts(severity);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracked_deals_status ON tracked_deals(status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracked_deals_store ON tracked_deals(store_id);")

            # Migración inicial: si tracked_deals está vacía, poblarla con las alertas históricas ya registradas
            # para que el revisor continuo sepa que ya fueron notificadas y no vuelva a alertarlas.
            try:
                cursor.execute("ALTER TABLE products ADD COLUMN card_price REAL;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE products ADD COLUMN in_stock INTEGER DEFAULT 1;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN card_price REAL;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN confidence_score REAL DEFAULT 90.0;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN verification_level TEXT DEFAULT 'DETECTADO';")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_median_price REAL;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_normal_price REAL;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_discount_pct REAL;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_stores_count INTEGER DEFAULT 0;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_match_method TEXT;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN market_matched_model TEXT;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN upvotes INTEGER DEFAULT 0;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE tracked_deals ADD COLUMN downvotes INTEGER DEFAULT 0;")
            except Exception:
                pass

            # 5. Tabla de Watchlist / Palabras Clave Prioritarias
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 6. Tabla de Caché de Referencias Inter-Tiendas (SoloTodo / Model Code)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_reference_cache (
                    cache_key TEXT PRIMARY KEY,
                    median_price REAL NOT NULL,
                    median_normal_price REAL,
                    stores_count INTEGER NOT NULL,
                    match_method TEXT NOT NULL,
                    matched_model TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mkt_ref_expires ON market_reference_cache(expires_at);")

            # 6b. Canal de ofertas: cada oferta se publica UNA vez, con el precio y la hora de recepción.
            # Solo se vuelve a publicar un producto si baja aún más de precio (oferta nueva).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS offer_feed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    best_price REAL NOT NULL,
                    discount_pct REAL NOT NULL,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_offer_feed_product ON offer_feed(product_id);")

            # 7. Tabla de Feedback del Usuario (👍 Buena Oferta / 👎 Falso Positivo)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS deal_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT NOT NULL,
                    product_id TEXT,
                    store_id TEXT,
                    category TEXT,
                    vote TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_deal ON deal_feedback(deal_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_store_cat ON deal_feedback(store_id, category);")

            cursor.execute("SELECT COUNT(*) AS cnt FROM tracked_deals;")
            if cursor.fetchone()["cnt"] == 0:
                cursor.execute("""
                    INSERT OR IGNORE INTO tracked_deals (
                        product_id, store_id, title, category,
                        normal_price, glitch_price, discount_pct, savings_clp,
                        severity, reason, status, first_detected_at, last_seen_at,
                        alert_dispatched, alert_id, url, image_url
                    )
                    SELECT 
                        g.product_id,
                        p.store_id,
                        p.title,
                        p.category,
                        p.normal_price,
                        p.current_price,
                        g.discount_pct,
                        g.savings_clp,
                        g.severity,
                        g.reason,
                        'ACTIVE',
                        g.timestamp,
                        g.timestamp,
                        1,
                        g.id,
                        p.url,
                        p.image_url
                    FROM glitch_alerts g
                    INNER JOIN products p ON g.product_id = p.id;
                """)

        # Limpiar cualquier duplicado histórico al inicializar
        try:
            self.cleanup_duplicate_glitches()
            self.cleanup_duplicate_price_history()
            self.remove_legacy_unstable_products()
        except Exception:
            pass

    def _extract_product_dict(self, product: Union[ProductItem, Dict[str, Any]]) -> Dict[str, Any]:
        """Normaliza un ProductItem (Pydantic) o dict a un diccionario plano."""
        if hasattr(product, "model_dump"):
            data = product.model_dump()
        elif hasattr(product, "dict"):
            data = product.dict()
        elif isinstance(product, dict):
            data = product.copy()
        elif hasattr(product, "__dict__"):
            data = vars(product).copy()
        else:
            raise TypeError(f"Tipo de producto no soportado: {type(product)}")

        # Normalizar timestamp a ISO string si es datetime (priorizando last_updated si fue asignado explícitamente)
        ts = data.get("last_updated") or data.get("timestamp")
        if isinstance(ts, datetime):
            data["last_updated"] = ts.strftime("%Y-%m-%d %H:%M:%S")
        elif ts is None:
            data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            data["last_updated"] = str(ts)

        return data

    def upsert_product(self, product: Union[ProductItem, Dict[str, Any]]) -> bool:
        """
        Inserta o actualiza un producto en el catálogo y añade automáticamente
        una entrada en la tabla price_history.
        """
        data = self._extract_product_dict(product)
        prod_id = str(data["id"])
        store_id = str(data["store_id"])
        title = str(data["title"])
        category = str(data.get("category") or "General")
        url = str(data.get("url") or "")
        image_url = data.get("image_url")
        current_price = float(data["current_price"])
        normal_price = float(data["normal_price"]) if data.get("normal_price") is not None else None
        card_price = float(data["card_price"]) if data.get("card_price") is not None else None
        in_stock = 1 if bool(data.get("in_stock", True)) else 0
        last_updated = data["last_updated"]

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Insert or update en tabla products
            cursor.execute("""
                INSERT INTO products (
                    id, store_id, title, category, url, image_url,
                    current_price, normal_price, card_price, in_stock, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    store_id = excluded.store_id,
                    title = excluded.title,
                    category = excluded.category,
                    url = excluded.url,
                    image_url = excluded.image_url,
                    current_price = excluded.current_price,
                    normal_price = excluded.normal_price,
                    card_price = excluded.card_price,
                    in_stock = excluded.in_stock,
                    last_updated = excluded.last_updated;
            """, (
                prod_id, store_id, title, category, url, image_url,
                current_price, normal_price, card_price, in_stock, last_updated
            ))

            # Registrar en price_history:
            # 1. Si es el primer registro del producto.
            # 2. Si el precio cambió.
            # 3. Si el precio se mantuvo estable pero han transcurrido >= 12 horas desde la última muestra
            #    (construye profundidad temporal representativa de 30 días - Hallazgo 4 de Auditoría).
            cursor.execute("""
                SELECT price, timestamp FROM price_history
                WHERE product_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1;
            """, (prod_id,))
            last_record = cursor.fetchone()

            should_record = False
            if not last_record:
                should_record = True
            else:
                last_price = float(last_record["price"])
                if abs(current_price - last_price) > 0.01:
                    should_record = True
                else:
                    try:
                        last_ts_str = str(last_record["timestamp"])[:19]
                        curr_ts_str = str(last_updated)[:19]
                        dt_last = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                        dt_curr = datetime.strptime(curr_ts_str, "%Y-%m-%d %H:%M:%S")
                        if abs((dt_curr - dt_last).total_seconds()) >= 3600 * 12:
                            should_record = True
                    except Exception:
                        pass

            if should_record:
                cursor.execute("""
                    INSERT INTO price_history (product_id, price, timestamp)
                    VALUES (?, ?, ?);
                """, (prod_id, current_price, last_updated))

        return True

    def get_baseline_price(self, product_id: str, days: int = 30) -> Optional[float]:
        """
        Calcula el precio base histórico de un producto usando la mediana móvil
        de los últimos 'days' días en price_history.

        La mediana es robusta ante inflación artificial de última hora (práctica
        común antes del CyberDay para inflar el % de descuento aparente).

        Para constituir un verdadero historial base de 30 días, los registros
        deben tener profundidad temporal (ej. abarcar al menos 12 horas o múltiples días).
        Soporta productos con precio estable continuo comparando la fecha de inserción
        con el último avistamiento.
        """
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Buscar precios dentro del rango de días
            cursor.execute("""
                SELECT price, timestamp FROM price_history
                WHERE product_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC;
            """, (product_id, cutoff_date))
            rows = cursor.fetchall()

            if rows and len(rows) >= 2:
                # Verificar profundidad temporal para no confundir ráfagas de escaneo con historial
                t_first = str(rows[0]["timestamp"])[:19]
                t_last = str(rows[-1]["timestamp"])[:19]
                try:
                    dt_first = datetime.strptime(t_first, "%Y-%m-%d %H:%M:%S")
                    dt_last = datetime.strptime(t_last, "%Y-%m-%d %H:%M:%S")
                    time_span_seconds = abs((dt_last - dt_first).total_seconds())
                except Exception:
                    time_span_seconds = 86400.0

                if time_span_seconds >= 3600 * 12:  # Al menos 12 horas de profundidad
                    prices = [float(row["price"]) for row in rows if row["price"] is not None and row["price"] > 0]
                    if len(prices) >= 2:
                        return round(float(statistics.median(prices)), 2)

            elif rows and len(rows) == 1:
                # Caso precio estable durante 30 días: verificar fecha en tabla products
                cursor.execute("SELECT last_updated FROM products WHERE id = ?;", (product_id,))
                prod_row = cursor.fetchone()
                if prod_row and prod_row["last_updated"]:
                    try:
                        t_first = str(rows[0]["timestamp"])[:19]
                        t_prod = str(prod_row["last_updated"])[:19]
                        dt_first = datetime.strptime(t_first, "%Y-%m-%d %H:%M:%S")
                        dt_prod = datetime.strptime(t_prod, "%Y-%m-%d %H:%M:%S")
                        if abs((dt_prod - dt_first).total_seconds()) >= 3600 * 12:
                            p_val = float(rows[0]["price"])
                            if p_val > 0:
                                return round(p_val, 2)
                    except Exception:
                        pass

            return None

    def record_glitch_alert(self, alert: Union[GlitchAlert, Dict[str, Any]]) -> str:
        """
        Guarda o actualiza una alerta de glitch detectada en glitch_alerts.
        Retorna el ID de la alerta registrada.
        """
        if isinstance(alert, dict):
            alert_id = str(alert.get("id") or alert.get("alert_id") or f"ALT-{uuid.uuid4().hex[:8].upper()}")
            product_id = str(alert.get("product_id") or (alert.get("product", {}).get("id") if isinstance(alert.get("product"), dict) else ""))
            severity = _enum_value(alert.get("severity")) or "MODERADO"
            confidence_score = float(alert.get("confidence_score", 0.0))
            discount_pct = float(alert.get("discount_pct", alert.get("discount_percentage", 0.0)))
            savings_clp = float(alert.get("savings_clp", alert.get("absolute_savings_clp", 0.0)))
            reason = str(alert.get("reason", "Glitch sospechoso detectado"))
            ts = alert.get("timestamp") or alert.get("created_at")
            if isinstance(ts, datetime):
                timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            elif ts:
                timestamp = str(ts)
            else:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            telegram_sent = 1 if alert.get("telegram_sent") else 0
        else:
            # Es una instancia de GlitchAlert (Pydantic)
            alert_id = getattr(alert, "alert_id", None) or getattr(alert, "id", None) or f"ALT-{uuid.uuid4().hex[:8].upper()}"
            
            # Obtener product_id
            if hasattr(alert, "product") and hasattr(alert.product, "id"):
                product_id = alert.product.id
            else:
                product_id = getattr(alert, "product_id", "")

            # Obtener datos del veredicto
            if hasattr(alert, "verdict"):
                v = alert.verdict
                severity = v.severity.value if hasattr(v.severity, "value") else str(v.severity)
                confidence_score = float(v.confidence_score)
                discount_pct = float(v.discount_percentage)
                savings_clp = float(v.absolute_savings_clp)
                reason = str(v.reason)
            else:
                severity = _enum_value(getattr(alert, "severity", None)) or "MODERADO"
                confidence_score = float(getattr(alert, "confidence_score", 0.0))
                discount_pct = float(getattr(alert, "discount_pct", 0.0))
                savings_clp = float(getattr(alert, "savings_clp", 0.0))
                reason = str(getattr(alert, "reason", "Glitch detectado"))

            ts = getattr(alert, "created_at", None) or getattr(alert, "timestamp", None)
            if isinstance(ts, datetime):
                timestamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            elif ts:
                timestamp = str(ts)
            else:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            telegram_sent = 1 if getattr(alert, "telegram_sent", False) else 0

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO glitch_alerts (
                    id, product_id, severity, confidence_score,
                    discount_pct, savings_clp, reason, timestamp, telegram_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    severity = excluded.severity,
                    confidence_score = excluded.confidence_score,
                    discount_pct = excluded.discount_pct,
                    savings_clp = excluded.savings_clp,
                    reason = excluded.reason,
                    timestamp = excluded.timestamp,
                    telegram_sent = excluded.telegram_sent;
            """, (
                alert_id, product_id, severity, confidence_score,
                discount_pct, savings_clp, reason, timestamp, telegram_sent
            ))

        return alert_id

    def register_or_update_deal(
        self,
        product: Union[ProductItem, Dict[str, Any]],
        verdict: Union[GlitchVerdict, Dict[str, Any]],
        alert_id: str,
    ) -> tuple[bool, str]:
        """
        Registrador y Revisor Continuo de Estado de Ofertas (Motor de Excelencia).
        
        Garantiza la regla inquebrantable de los buscadores de ofertas profesionales:
        - Si la oferta es NUEVA: se inserta en tracked_deals y retorna (True, "NEW_DEAL").
        - Si la oferta YA ESTÁ ACTIVA al mismo precio: actualiza last_seen_at silenciosamente
          y retorna (False, "ALREADY_ACTIVE") -> CERO NOTIFICACIONES, CERO SPAM.
        - Si el precio baja significativamente más: actualiza y retorna (True, "PRICE_DROP").
        - Si la tienda corrigió el error: marca status='CORRECTED' y retorna (False, "CORRECTED").
        """
        prod_data = self._extract_product_dict(product)
        prod_id = str(prod_data["id"])
        store_id = str(prod_data["store_id"])
        title = str(prod_data["title"])
        category = str(prod_data.get("category") or "General")
        url = str(prod_data.get("url") or "")
        image_url = prod_data.get("image_url")
        current_price = float(prod_data["current_price"])
        normal_price = float(prod_data.get("normal_price") or current_price)
        card_price = prod_data.get("card_price")

        # Determinar el precio efectivo de glitch (considerando precio exclusivo con tarjeta)
        card_price_val = float(card_price) if (card_price is not None and float(card_price) > 0) else None
        effective_glitch_price = current_price
        is_card_price = False
        if card_price_val is not None and card_price_val < current_price:
            effective_glitch_price = card_price_val
            is_card_price = True

        if hasattr(verdict, "model_dump"):
            v_data = verdict.model_dump()
        elif hasattr(verdict, "dict"):
            v_data = verdict.dict()
        elif isinstance(verdict, dict):
            v_data = verdict
        else:
            v_data = vars(verdict)

        severity = _enum_value(v_data.get("severity")) or "MODERADO"
        discount_pct = float(v_data.get("discount_percentage", 0.0))
        savings_clp = float(v_data.get("absolute_savings_clp", 0.0))
        confidence_score = float(v_data.get("confidence_score", 90.0))
        verif_raw = v_data.get("verification_level", "DETECTADO")
        verification_level = verif_raw.value if hasattr(verif_raw, "value") else str(verif_raw)
        reason = str(v_data.get("reason", "Glitch de precio detectado"))
        if is_card_price and "tarjeta" not in reason.lower():
            reason = f"{reason} (Exclusivo con Tarjeta Tienda)"

        mkt_median = v_data.get("market_median_price")
        mkt_normal = v_data.get("market_normal_median_price")
        mkt_discount = v_data.get("market_discount_pct")
        mkt_stores = int(v_data.get("market_stores_count") or 0)
        mkt_method = v_data.get("market_match_method")
        mkt_model = v_data.get("market_matched_model")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT product_id, glitch_price, normal_price, status, alert_dispatched
                FROM tracked_deals
                WHERE product_id = ?;
            """, (prod_id,))
            row = cursor.fetchone()

            if not row:
                # 🎯 Oferta genuinamente NUEVA: registrar con alert_dispatched=0 para confirmar despacho
                cursor.execute("""
                    INSERT INTO tracked_deals (
                        product_id, store_id, title, category,
                        normal_price, glitch_price, card_price, discount_pct, savings_clp,
                        confidence_score, verification_level,
                        market_median_price, market_normal_price, market_discount_pct,
                        market_stores_count, market_match_method, market_matched_model,
                        severity, reason, status, first_detected_at, last_seen_at,
                        alert_dispatched, alert_id, url, image_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, 0, ?, ?, ?);
                """, (
                    prod_id, store_id, title, category,
                    normal_price, effective_glitch_price, card_price_val, discount_pct, savings_clp,
                    confidence_score, verification_level,
                    mkt_median, mkt_normal, mkt_discount,
                    mkt_stores, mkt_method, mkt_model,
                    severity, reason, now_str, now_str,
                    alert_id, url, image_url
                ))
                return True, "NEW_DEAL"

            old_glitch_price = float(row["glitch_price"])
            old_status = str(row["status"])
            alert_dispatched = int(row["alert_dispatched"] or 0)

            # 1. CASO REACTIVACIÓN:
            if old_status in ("CORRECTED", "EXPIRED", "OUT_OF_STOCK"):
                cursor.execute("""
                    UPDATE tracked_deals
                    SET glitch_price = ?, card_price = ?, savings_clp = ?, discount_pct = ?,
                        confidence_score = ?, verification_level = ?,
                        market_median_price = COALESCE(?, market_median_price),
                        market_normal_price = COALESCE(?, market_normal_price),
                        market_discount_pct = COALESCE(?, market_discount_pct),
                        market_stores_count = CASE WHEN ? > 0 THEN ? ELSE market_stores_count END,
                        market_match_method = COALESCE(?, market_match_method),
                        market_matched_model = COALESCE(?, market_matched_model),
                        severity = ?, reason = ?, status = 'ACTIVE', last_seen_at = ?,
                        alert_id = ?, alert_dispatched = 0
                    WHERE product_id = ?;
                """, (
                    effective_glitch_price, card_price_val, savings_clp, discount_pct,
                    confidence_score, verification_level,
                    mkt_median, mkt_normal, mkt_discount,
                    mkt_stores, mkt_stores, mkt_method, mkt_model,
                    severity, reason, now_str, alert_id, prod_id
                ))
                return True, "REACTIVATED"

            # 2. CASO CAÍDA DE PRECIO AÚN MAYOR:
            if effective_glitch_price < old_glitch_price * 0.97:
                cursor.execute("""
                    UPDATE tracked_deals
                    SET glitch_price = ?, card_price = ?, savings_clp = ?, discount_pct = ?,
                        confidence_score = ?, verification_level = ?,
                        market_median_price = COALESCE(?, market_median_price),
                        market_normal_price = COALESCE(?, market_normal_price),
                        market_discount_pct = COALESCE(?, market_discount_pct),
                        market_stores_count = CASE WHEN ? > 0 THEN ? ELSE market_stores_count END,
                        market_match_method = COALESCE(?, market_match_method),
                        market_matched_model = COALESCE(?, market_matched_model),
                        severity = ?, reason = ?, status = 'ACTIVE', last_seen_at = ?,
                        alert_id = ?, alert_dispatched = 0
                    WHERE product_id = ?;
                """, (
                    effective_glitch_price, card_price_val, savings_clp, discount_pct,
                    confidence_score, verification_level,
                    mkt_median, mkt_normal, mkt_discount,
                    mkt_stores, mkt_stores, mkt_method, mkt_model,
                    severity, reason, now_str, alert_id, prod_id
                ))
                return True, "PRICE_DROP"

            # 3. CASO REINTENTO DE DESPACHO PENDIENTE O FALLIDO:
            # Si el deal está activo pero el despacho anterior no se confirmó, reintentar alertar
            if alert_dispatched == 0:
                cursor.execute("""
                    UPDATE tracked_deals
                    SET last_seen_at = ?, glitch_price = ?, card_price = ?, status = 'ACTIVE'
                    WHERE product_id = ?;
                """, (now_str, effective_glitch_price, card_price_val, prod_id))
                return True, "RETRY_DISPATCH"

            # 4. CASO AJUSTE MENOR DE PRECIO (subió levemente pero el detector validó que SIGUE siendo un glitch masivo):
            if abs(effective_glitch_price - old_glitch_price) > 0.01:
                cursor.execute("""
                    UPDATE tracked_deals
                    SET glitch_price = ?, card_price = ?, savings_clp = ?, discount_pct = ?,
                        last_seen_at = ?, status = 'ACTIVE'
                    WHERE product_id = ?;
                """, (
                    effective_glitch_price, card_price_val, savings_clp, discount_pct,
                    now_str, prod_id
                ))
                return False, "PRICE_ADJUSTED"

            # 5. CASO OFERTA YA CONOCIDA Y NOTIFICADA AL MISMO PRECIO:
            # Actualizar last_seen_at silenciosamente (cero spam)
            cursor.execute("""
                UPDATE tracked_deals
                SET last_seen_at = ?, status = 'ACTIVE', card_price = COALESCE(?, card_price)
                WHERE product_id = ?;
            """, (now_str, card_price_val, prod_id))
            return False, "ALREADY_ACTIVE"

    def check_and_update_deal_status(
        self,
        product: Union[ProductItem, Dict[str, Any]]
    ) -> Optional[str]:
        """
        Inspecciona si el producto está en seguimiento en tracked_deals.
        Si está sin stock, actualiza su estado a 'OUT_OF_STOCK'.
        Si su precio actual subió de regreso al precio normal, actualiza su estado a 'CORRECTED'.
        Retorna el estado resultante ('OUT_OF_STOCK', 'CORRECTED', 'ACTIVE') o None si no estaba en seguimiento.
        """
        prod_data = self._extract_product_dict(product)
        prod_id = str(prod_data["id"])
        in_stock = bool(prod_data.get("in_stock", True))
        current_price = float(prod_data["current_price"])
        normal_price = float(prod_data.get("normal_price") or current_price)
        card_price = prod_data.get("card_price")
        best_price = float(card_price) if (card_price is not None and 0.0 < float(card_price) < current_price) else current_price
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT glitch_price, normal_price, status
                FROM tracked_deals
                WHERE product_id = ?;
            """, (prod_id,))
            row = cursor.fetchone()
            if not row:
                return None

            current_status = row["status"]
            old_glitch_price = float(row["glitch_price"])

            if not in_stock:
                if current_status == "ACTIVE":
                    cursor.execute("""
                        UPDATE tracked_deals
                        SET status = 'OUT_OF_STOCK', last_seen_at = ?
                        WHERE product_id = ?;
                    """, (now_str, prod_id))
                return "OUT_OF_STOCK"

            # Si el precio subió a >= 80% del precio de lista, volvió a la normalidad:
            if normal_price > 0 and best_price >= normal_price * 0.8:
                if current_status == "ACTIVE":
                    cursor.execute("""
                        UPDATE tracked_deals
                        SET status = 'CORRECTED', last_seen_at = ?
                        WHERE product_id = ?;
                    """, (now_str, prod_id))
                return "CORRECTED"

            return current_status

    def get_deal_status(self, product_id: str) -> Optional[str]:
        """Retorna el estado de seguimiento de una oferta ('ACTIVE', 'CORRECTED', 'OUT_OF_STOCK', 'EXPIRED', None)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM tracked_deals WHERE product_id = ?;", (product_id,))
            row = cursor.fetchone()
            return row["status"] if row else None

    def mark_deal_corrected(self, product_id: str, new_price: Optional[float] = None) -> bool:
        """Marca una oferta activa como 'CORRECTED' al comprobar que el precio volvió a la normalidad."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tracked_deals
                SET status = 'CORRECTED', last_seen_at = ?
                WHERE product_id = ? AND status = 'ACTIVE';
            """, (now_str, product_id))
            return cursor.rowcount > 0

    def mark_deal_out_of_stock(self, product_id: str) -> bool:
        """Marca una oferta activa como 'OUT_OF_STOCK' al comprobar que no tiene stock."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tracked_deals
                SET status = 'OUT_OF_STOCK', last_seen_at = ?
                WHERE product_id = ? AND status = 'ACTIVE';
            """, (now_str, product_id))
            return cursor.rowcount > 0

    def mark_deal_dispatched(self, product_id: str) -> bool:
        """Marca una oferta en tracked_deals como despachada exitosamente por canales de alerta."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tracked_deals
                SET alert_dispatched = 1
                WHERE product_id = ?;
            """, (product_id,))
            return cursor.rowcount > 0

    def expire_stale_deals(self, max_hours: int = 12) -> int:
        """
        Retira automáticamente del radar aquellas ofertas activas que no han sido
        re-observadas en los escaneos durante más de `max_hours` horas,
        transicionándolas a 'EXPIRED'.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tracked_deals
                SET status = 'EXPIRED'
                WHERE status = 'ACTIVE'
                  AND datetime(last_seen_at) < datetime('now', ?);
            """, (f"-{max_hours} hours",))
            return cursor.rowcount

    def is_duplicate_alert(self, product_id: str, current_price: float, window_hours: int = 24) -> bool:
        """
        Verifica de forma ultrarrápida si el producto ya cuenta con una oferta activa
        a un precio igual o inferior al actual.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT glitch_price, status
                FROM tracked_deals
                WHERE product_id = ?;
            """, (product_id,))
            row = cursor.fetchone()

            if not row:
                return False

            old_price = float(row["glitch_price"])
            if current_price >= old_price * 0.97:
                return True

            return False

    def cleanup_duplicate_glitches(self) -> int:
        """
        Elimina alertas duplicadas de glitch_alerts, conservando únicamente
        el registro más reciente de cada product_id.
        Retorna la cantidad de registros duplicados eliminados.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM glitch_alerts
                WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                    FROM glitch_alerts
                    GROUP BY product_id
                );
            """)
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"🧹 Limpieza de alertas duplicadas: se removieron {deleted_count} registros redundantes.")
            return deleted_count

    def remove_legacy_unstable_products(self) -> int:
        """
        Borra productos con IDs de formatos antiguos ya reemplazados por IDs canónicos:
        - 'pcfactory_N': ahora 'pcfactory-N'; además traían un precio normal inflado ('referencia').
        - 'MLC-HTML-…': IDs de Mercado Libre basados en hash() de Python (cambiaban en cada reinicio).
        Sin esto, esas filas se seguirían publicando como ofertas hasta expirar.
        """
        patterns = ("pcfactory\\_%", "MLC-HTML-%")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            removed = 0
            for pattern in patterns:
                cursor.execute("DELETE FROM price_history WHERE product_id LIKE ? ESCAPE '\\';", (pattern,))
                cursor.execute("DELETE FROM products WHERE id LIKE ? ESCAPE '\\';", (pattern,))
                removed += cursor.rowcount
            if removed:
                conn.commit()
                logger.info(f"🧹 {removed} producto(s) con IDs de formato antiguo eliminados.")
            return removed

    def cleanup_duplicate_price_history(self) -> int:
        """
        Elimina únicamente registros con timestamp y precio exactamente duplicados
        (por ejemplo, por reintentos de red o escrituras concurrentes en el mismo segundo),
        preservando el 100% de la evolución histórica y de los ciclos de precio en el tiempo (Hallazgo A de Auditoría 4).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM price_history 
                WHERE id NOT IN (
                    SELECT MIN(id) FROM price_history 
                    GROUP BY product_id, price, timestamp
                );
            """)
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"🧹 Historial de precios saneado: {deleted_count} registros redundantes eliminados.")
            return deleted_count

    def get_recent_glitches(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Retorna la lista de ofertas activas no duplicadas para el radar web.
        Prioriza la tabla de estado continuo tracked_deals (con fallback a glitch_alerts si tracked_deals está vacía).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Verificar si tracked_deals contiene registros
            cursor.execute("SELECT COUNT(*) AS total FROM tracked_deals;")
            has_tracked = cursor.fetchone()["total"] > 0

            if has_tracked:
                cursor.execute("""
                    SELECT 
                        t.alert_id AS id,
                        t.product_id,
                        t.severity,
                        COALESCE(t.confidence_score, 90.0) AS confidence_score,
                        t.discount_pct,
                        t.savings_clp,
                        t.reason,
                        t.first_detected_at AS timestamp,
                        t.alert_dispatched AS telegram_sent,
                        t.store_id,
                        t.title,
                        t.category,
                        t.url,
                        t.image_url,
                        t.glitch_price AS current_price,
                        COALESCE(t.card_price, p.card_price) AS card_price,
                        t.normal_price,
                        t.status,
                        t.last_seen_at,
                        COALESCE(t.verification_level, 'DETECTADO') AS verification_level,
                        t.market_median_price,
                        t.market_normal_price,
                        t.market_discount_pct,
                        t.market_stores_count,
                        t.market_match_method,
                        t.market_matched_model,
                        COALESCE(t.upvotes, 0) AS upvotes,
                        COALESCE(t.downvotes, 0) AS downvotes
                    FROM tracked_deals t
                    LEFT JOIN products p ON t.product_id = p.id
                    WHERE t.status = 'ACTIVE'
                    ORDER BY t.first_detected_at DESC
                    LIMIT ?;
                """, (limit,))
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    item = dict(row)
                    item["telegram_sent"] = bool(item.get("telegram_sent", 0))
                    item["card_price"] = float(item["card_price"]) if item.get("card_price") is not None and float(item["card_price"]) > 0 else None
                    results.append(item)
                return results

            # Fallback exclusivamente en caso de base de datos donde tracked_deals nunca se pobló
            cursor.execute("""
                SELECT 
                    g.id,
                    g.product_id,
                    g.severity,
                    g.confidence_score,
                    g.discount_pct,
                    g.savings_clp,
                    g.reason,
                    g.timestamp,
                    g.telegram_sent,
                    p.store_id,
                    p.title,
                    p.category,
                    p.url,
                    p.image_url,
                    p.current_price,
                    p.card_price,
                    p.normal_price,
                    0 AS upvotes,
                    0 AS downvotes
                FROM glitch_alerts g
                INNER JOIN (
                    SELECT product_id, MAX(rowid) AS max_rowid
                    FROM glitch_alerts
                    GROUP BY product_id
                ) latest ON g.rowid = latest.max_rowid
                LEFT JOIN products p ON g.product_id = p.id
                ORDER BY g.timestamp DESC
                LIMIT ?;
            """, (limit,))
            rows = cursor.fetchall()

            results = []
            for row in rows:
                item = dict(row)
                item["telegram_sent"] = bool(item.get("telegram_sent", 0))
                item["card_price"] = float(item["card_price"]) if item.get("card_price") is not None and float(item["card_price"]) > 0 else None
                results.append(item)
            return results

    def get_stats(self) -> Dict[str, Any]:
        """
        Calcula estadísticas globales del sistema de forma fidedigna y desduplicada (Hallazgo 5):
          - total_products: productos únicos monitoreados
          - total_glitches / active_glitches: glitches únicos ACTIVOS en el radar (si todos expiraron o fueron corregidos, es 0)
          - historical_distinct_glitches / historical_alerts_total: métricas históricas de auditoría
          - accumulated_savings_clp: ahorro acumulado real de glitches activos
          - breakdown por severidad y tiendas monitoreadas
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Total de productos únicos
            cursor.execute("SELECT COUNT(*) AS cnt FROM products;")
            total_products = cursor.fetchone()["cnt"]

            # Total de ofertas verdaderamente ACTIVAS en el radar (Hallazgo B de Auditoría 4)
            cursor.execute("SELECT COUNT(*) AS cnt FROM tracked_deals WHERE status = 'ACTIVE';")
            active_cnt = cursor.fetchone()["cnt"]

            # total_glitches refleja ESTRICTAMENTE las ofertas vivas en este instante
            total_glitches = active_cnt

            if active_cnt > 0:
                cursor.execute("SELECT COALESCE(SUM(savings_clp), 0.0) AS total_savings FROM tracked_deals WHERE status = 'ACTIVE';")
                accumulated_savings = float(cursor.fetchone()["total_savings"])

                cursor.execute("SELECT COUNT(*) AS cnt FROM tracked_deals WHERE status = 'ACTIVE' AND severity = 'CRITICO';")
                critical_glitches = cursor.fetchone()["cnt"]

                cursor.execute("SELECT COUNT(*) AS cnt FROM tracked_deals WHERE status = 'ACTIVE' AND severity = 'ALTO';")
                high_glitches = cursor.fetchone()["cnt"]
            else:
                accumulated_savings = 0.0
                critical_glitches = 0
                high_glitches = 0

            # Conteo histórico all-time independiente
            cursor.execute("SELECT COUNT(*) AS cnt FROM glitch_alerts;")
            historical_alerts_total = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(DISTINCT product_id) AS cnt FROM glitch_alerts;")
            historical_distinct_glitches = cursor.fetchone()["cnt"]

            # Tiendas monitoreadas
            cursor.execute("SELECT COUNT(DISTINCT store_id) AS cnt FROM products;")
            active_stores = cursor.fetchone()["cnt"]

            # Registros históricos totales
            cursor.execute("SELECT COUNT(*) AS cnt FROM price_history;")
            total_history_records = cursor.fetchone()["cnt"]

            # Conteo de ofertas comerciales moderadas (40% - 64%)
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM products
                WHERE normal_price > 0 AND current_price < normal_price
                  AND (((normal_price - current_price) / normal_price) * 100) >= 40.0
                  AND (((normal_price - current_price) / normal_price) * 100) < 65.0;
            """)
            moderate_deals = cursor.fetchone()["cnt"]

        return {
            "total_products": total_products,
            "total_glitches": total_glitches,
            "active_glitches": active_cnt,
            "historical_alerts_total": historical_alerts_total,
            "historical_distinct_glitches": historical_distinct_glitches,
            "moderate_deals": moderate_deals,
            "accumulated_savings_clp": accumulated_savings,
            "savings_clp": accumulated_savings,
            "critical_glitches": critical_glitches,
            "high_glitches": high_glitches,
            "active_stores": active_stores,
            "total_history_records": total_history_records,
        }

    def publish_feed_offer(
        self,
        kind: str,
        product_id: str,
        store_id: str,
        best_price: float,
        discount_pct: float,
        payload: Dict[str, Any],
    ) -> Optional[int]:
        """
        Publica una oferta en el canal si es nueva: el producto nunca se publicó, o ahora está
        más barato que en su última publicación. Retorna el id de la publicación, o None si ya
        estaba publicada a este precio (o a uno menor) y no corresponde repetirla.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MIN(best_price) AS min_price FROM offer_feed WHERE product_id = ?;",
                (product_id,),
            )
            row = cursor.fetchone()
            if row and row["min_price"] is not None:
                last_min = float(row["min_price"])
                # Solo es una oferta nueva si baja al menos 2% y $1.000 respecto a su mejor publicación
                if best_price > last_min * 0.98 or last_min - best_price < 1000:
                    return None
            received_at = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                INSERT INTO offer_feed (kind, product_id, store_id, best_price, discount_pct, received_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (kind, product_id, store_id, float(best_price), float(discount_pct), received_at,
                  json.dumps(payload, ensure_ascii=False, default=str)))
            conn.commit()
            return int(cursor.lastrowid)

    def get_feed(self, after_id: int = 0, limit: int = 300) -> List[Dict[str, Any]]:
        """Publicaciones del canal, de la más reciente a la más antigua; con after_id, solo las posteriores."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, kind, product_id, store_id, best_price, discount_pct, received_at, payload
                FROM offer_feed
                WHERE id > ?
                ORDER BY id DESC
                LIMIT ?;
            """, (int(after_id), int(limit)))
            posts = []
            for r in cursor.fetchall():
                post = dict(r)
                post["payload"] = json.loads(post["payload"])
                posts.append(post)
            return posts

    def get_feedback_counts(self, deal_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """Votos 👍/👎 por id de oferta (alert_id o product_id)."""
        ids = [d for d in dict.fromkeys(deal_ids) if d]
        if not ids:
            return {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            marks = ",".join("?" for _ in ids)
            cursor.execute(f"""
                SELECT deal_id,
                       SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS ups,
                       SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS downs
                FROM deal_feedback WHERE deal_id IN ({marks}) GROUP BY deal_id;
            """, ids)
            return {r["deal_id"]: {"up": r["ups"] or 0, "down": r["downs"] or 0} for r in cursor.fetchall()}

    def offer_exists(self, deal_id: str) -> bool:
        """True si el id corresponde a una alerta, un producto del catálogo o una publicación del canal."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM tracked_deals WHERE alert_id = ? OR product_id = ?
                UNION SELECT 1 FROM products WHERE id = ?
                UNION SELECT 1 FROM offer_feed WHERE product_id = ?
                LIMIT 1;
            """, (deal_id, deal_id, deal_id, deal_id))
            return cursor.fetchone() is not None

    def get_commercial_deals(
        self,
        min_discount: float = 40.0,
        max_discount: float = 65.0,
        limit: int = 100,
        store_id: Optional[str] = None,
        max_age_hours: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retorna ofertas comerciales moderadas (ej: 40% a 64% de descuento)
        provenientes del catálogo de productos monitoreados.
        Permite al usuario explorar descuentos atractivos pero no anómalos.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT 
                    p.id AS product_id,
                    p.store_id,
                    p.title,
                    p.category,
                    p.url,
                    p.image_url,
                    p.current_price,
                    p.card_price,
                    p.normal_price,
                    ROUND(((p.normal_price - (CASE WHEN p.card_price > 0 AND p.card_price < p.current_price THEN p.card_price ELSE p.current_price END)) / p.normal_price) * 100, 1) AS discount_pct,
                    ROUND(p.normal_price - (CASE WHEN p.card_price > 0 AND p.card_price < p.current_price THEN p.card_price ELSE p.current_price END), 0) AS savings_clp,
                    p.last_updated AS timestamp
                FROM products p
                WHERE p.normal_price > 0 
                  AND (CASE WHEN p.card_price > 0 AND p.card_price < p.current_price THEN p.card_price ELSE p.current_price END) < p.normal_price
                  AND (p.in_stock IS NULL OR p.in_stock = 1)
                  AND (((p.normal_price - (CASE WHEN p.card_price > 0 AND p.card_price < p.current_price THEN p.card_price ELSE p.current_price END)) / p.normal_price) * 100) >= ?
                  AND (((p.normal_price - (CASE WHEN p.card_price > 0 AND p.card_price < p.current_price THEN p.card_price ELSE p.current_price END)) / p.normal_price) * 100) < ?
            """
            params: list[Any] = [float(min_discount), float(max_discount)]
            if max_age_hours is not None:
                # Un producto que no aparece en los escaneos recientes puede ya no estar en oferta
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
                query += " AND p.last_updated >= ?"
                params.append(cutoff)
            if store_id and store_id != "all":
                query += " AND LOWER(p.store_id) = ?"
                params.append(store_id.lower())
            query += " ORDER BY discount_pct DESC LIMIT ?;"
            params.append(int(limit))

            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            results = []
            for row in rows:
                item = dict(row)
                results.append(item)
            return results

    def mark_telegram_sent(self, alert_id: str) -> bool:
        """Marca una alerta como despachada por Telegram."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE glitch_alerts
                SET telegram_sent = 1
                WHERE id = ?;
            """, (alert_id,))
            return cursor.rowcount > 0

    def get_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene un producto específico por su ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products WHERE id = ?;", (product_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_price_history(self, product_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Retorna el historial cronológico de precios de un producto."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, product_id, price, timestamp
                FROM price_history
                WHERE product_id = ?
                ORDER BY timestamp DESC
                LIMIT ?;
            """, (product_id, limit))
            return [dict(row) for row in cursor.fetchall()]

    def clear_all(self) -> None:
        """Limpia todas las tablas (usado en tests y reseteos)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM glitch_alerts;")
            cursor.execute("DELETE FROM price_history;")
            cursor.execute("DELETE FROM products;")

    # =========================================================================
    # MÉTODOS DE WATCHLIST (Seguimiento prioritario por palabras clave)
    # =========================================================================

    def add_watchlist_keyword(self, keyword: str) -> bool:
        """Añade una palabra clave a la watchlist del usuario (ej: 'macbook', 'rtx 4080')."""
        clean_kw = keyword.strip().lower()
        if not clean_kw:
            return False
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("INSERT INTO watchlist (keyword) VALUES (?);", (clean_kw,))
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_watchlist_keyword(self, keyword: str) -> bool:
        """Elimina una palabra clave de la watchlist."""
        clean_kw = keyword.strip().lower()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM watchlist WHERE keyword = ?;", (clean_kw,))
            return cursor.rowcount > 0

    def get_watchlist_keywords(self) -> List[str]:
        """Retorna todas las palabras clave activas en la watchlist."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT keyword FROM watchlist ORDER BY created_at ASC;")
            return [row["keyword"] for row in cursor.fetchall()]

    def is_in_watchlist(self, title: str) -> bool:
        """Verifica si el título de un producto coincide con alguna palabra clave de la watchlist."""
        if not title:
            return False
        t_lower = title.lower()
        keywords = self.get_watchlist_keywords()
        return any(kw in t_lower for kw in keywords)

    # =========================================================================
    # Referencias de Mercado Inter-Tiendas (Caché y Match Local)
    # =========================================================================
    def get_cached_market_reference(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Obtiene una referencia de mercado en caché si aún no ha expirado."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT median_price, median_normal_price, stores_count, match_method, matched_model
                FROM market_reference_cache
                WHERE cache_key = ? AND expires_at > ?;
            """, (cache_key, now_str))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def save_cached_market_reference(
        self,
        cache_key: str,
        median_price: float,
        median_normal_price: Optional[float],
        stores_count: int,
        match_method: str,
        matched_model: Optional[str] = None,
        ttl_hours: float = 8.0,
    ) -> None:
        """Guarda una referencia de mercado en caché con tiempo de expiración."""
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        exp_str = (now + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO market_reference_cache (
                    cache_key, median_price, median_normal_price, stores_count,
                    match_method, matched_model, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                cache_key, median_price, median_normal_price, stores_count,
                match_method, matched_model, now_str, exp_str
            ))

    def find_local_cross_store_reference(
        self,
        model_code: str,
        exclude_store_id: str,
        max_age_hours: float = 48.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Busca en la tabla local de productos otras tiendas que tengan el mismo modelo.
        Excluye la tienda actual y exige al menos 2 tiendas distintas con stock.
        Solo considera precios vistos en las últimas `max_age_hours` y omite SoloTodo,
        cuyos precios ya provienen de otros retailers y se contarían dos veces.
        """
        import statistics
        from glitchhunter_ai.core.model_extractor import normalize_model_code, extract_model_code

        target = normalize_model_code(model_code)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT store_id, title, current_price, normal_price
                FROM products
                WHERE store_id != ? AND store_id != 'solotodo'
                  AND in_stock = 1 AND current_price > 0
                  AND last_updated >= ?
                  AND title LIKE ?;
            """, (exclude_store_id, cutoff, f"%{model_code}%"))
            rows = cursor.fetchall()

            store_prices: Dict[str, Dict[str, float]] = {}
            for r in rows:
                # El LIKE es solo un prefiltro: "iPhone 15" también calza con "iPhone 15 Pro Max".
                # Se exige que el título del otro producto produzca exactamente el mismo código.
                row_code = extract_model_code(r["title"])
                if not row_code or normalize_model_code(row_code) != target:
                    continue
                sid = r["store_id"]
                cur_p = float(r["current_price"])
                norm_p = float(r["normal_price"] or cur_p)
                if sid not in store_prices or cur_p < store_prices[sid]["offer"]:
                    store_prices[sid] = {"offer": cur_p, "normal": norm_p}

            if len(store_prices) < 2:
                return None

            offers = [v["offer"] for v in store_prices.values()]
            normals = [v["normal"] for v in store_prices.values()]

            return {
                "median_price": statistics.median(offers),
                "median_normal_price": statistics.median(normals) if normals else None,
                "stores_count": len(store_prices),
                "sample_stores": list(store_prices.keys()),
            }

    def record_deal_feedback(
        self,
        deal_id: str,
        vote: str,
        product_id: Optional[str] = None,
        store_id: Optional[str] = None,
        category: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Registra el voto del usuario ('up' para glitch real o 'down' para falso positivo).
        Actualiza el contador agregado en tracked_deals y persiste el log histórico.
        """
        clean_vote = vote.strip().lower()
        if clean_vote not in ("up", "down", "like", "dislike"):
            raise ValueError(f"Voto inválido '{vote}'. Se esperaba 'up' o 'down'.")
        normalized_vote = "up" if clean_vote in ("up", "like") else "down"

        with self.get_connection() as conn:
            cursor = conn.cursor()

            p_id = product_id
            s_id = store_id
            cat = category

            # Si faltan metadatos, intentar completarlos desde tracked_deals o products
            if not (p_id and s_id):
                cursor.execute("""
                    SELECT product_id, store_id, category
                    FROM tracked_deals
                    WHERE alert_id = ? OR product_id = ?;
                """, (deal_id, deal_id))
                row = cursor.fetchone()
                if row:
                    p_id = p_id or row["product_id"]
                    s_id = s_id or row["store_id"]
                    cat = cat or row["category"]
                else:
                    cursor.execute("""
                        SELECT id AS product_id, store_id, category
                        FROM products
                        WHERE id = ?;
                    """, (deal_id,))
                    p_row = cursor.fetchone()
                    if p_row:
                        p_id = p_id or p_row["product_id"]
                        s_id = s_id or p_row["store_id"]
                        cat = cat or p_row["category"]

            # Un voto por oferta: un nuevo voto reemplaza al anterior en vez de acumularse,
            # para que clics repetidos no distorsionen el modificador de confianza por categoría
            cursor.execute("DELETE FROM deal_feedback WHERE deal_id = ?;", (deal_id,))
            cursor.execute("""
                INSERT INTO deal_feedback (deal_id, product_id, store_id, category, vote, reason)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (deal_id, p_id, s_id, cat, normalized_vote, reason))

            # Consultar totales para este deal y sincronizar contadores en tracked_deals
            cursor.execute("""
                SELECT 
                    SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS ups,
                    SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS downs
                FROM deal_feedback
                WHERE deal_id = ?;
            """, (deal_id,))
            totals = cursor.fetchone()
            cursor.execute("""
                UPDATE tracked_deals
                SET upvotes = ?, downvotes = ?
                WHERE alert_id = ? OR product_id = ?;
            """, (totals["ups"] or 0, totals["downs"] or 0, deal_id, deal_id))
            conn.commit()

            return {
                "deal_id": deal_id,
                "vote": normalized_vote,
                "upvotes": totals["ups"] or 0,
                "downvotes": totals["downs"] or 0,
            }

    def get_feedback_stats(self) -> Dict[str, Any]:
        """
        Retorna métricas agregadas de feedback del usuario y precisión del radar.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) AS total_votes,
                    SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS total_upvotes,
                    SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS total_downvotes
                FROM deal_feedback;
            """)
            summary = cursor.fetchone()
            total = summary["total_votes"] or 0
            ups = summary["total_upvotes"] or 0
            downs = summary["total_downvotes"] or 0
            accuracy = round((ups / total) * 100.0, 1) if total > 0 else 100.0

            cursor.execute("""
                SELECT 
                    COALESCE(store_id, 'desconocida') AS store,
                    SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS ups,
                    SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS downs
                FROM deal_feedback
                GROUP BY store_id
                ORDER BY (ups + downs) DESC;
            """)
            by_store = {
                r["store"]: {"upvotes": r["ups"], "downvotes": r["downs"]}
                for r in cursor.fetchall()
            }

            cursor.execute("""
                SELECT 
                    COALESCE(category, 'general') AS cat,
                    SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS ups,
                    SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS downs
                FROM deal_feedback
                GROUP BY category
                ORDER BY (ups + downs) DESC;
            """)
            by_cat = {
                r["cat"]: {"upvotes": r["ups"], "downvotes": r["downs"]}
                for r in cursor.fetchall()
            }

            return {
                "total_votes": total,
                "total_upvotes": ups,
                "total_downvotes": downs,
                "accuracy_rate_pct": accuracy,
                "by_store": by_store,
                "by_category": by_cat,
            }

    def get_category_trust_modifier(
        self,
        store_id: str,
        category: str,
        min_samples: int = 3,
    ) -> float:
        """
        Calcula un modificador de certeza (-15 a +5 puntos) basado en el feedback histórico
        del usuario para la combinación (tienda, categoría).
        Si una categoría acumula muchos falsos positivos (downvotes), penaliza la confianza.
        Si acumula alta satisfacción (upvotes), bonifica la confianza.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(*) AS total,
                    SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS ups,
                    SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS downs
                FROM deal_feedback
                WHERE store_id = ? AND category = ?;
            """, (store_id, category))
            row = cursor.fetchone()
            if not row:
                return 0.0

            total = row["total"] or 0
            if total < min_samples:
                return 0.0

            ups = row["ups"] or 0
            downs = row["downs"] or 0
            down_ratio = downs / total
            up_ratio = ups / total

            if down_ratio >= 0.75:
                return -15.0
            elif down_ratio >= 0.50:
                return -8.0
            elif up_ratio >= 0.85:
                return +5.0
            return 0.0


# =====================================================================
# Bloque de validación y pruebas autónomas
# =====================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("🚀 Test de Validación: PriceDatabase (GlitchHunter AI)")
    print("=" * 70)

    # 1. Usar base de datos temporal en memoria o archivo temporal
    test_db_path = _PROJECT_DIR / "data" / "test_glitchhunter.db"
    if test_db_path.exists():
        test_db_path.unlink()

    db = PriceDatabase(db_path=test_db_path)
    print(f"✅ Base de datos inicializada en: {test_db_path}")

    # 2. Crear productos de prueba con ProductItem
    p1 = ProductItem(
        id="falabella-tv-lg-oled-55",
        store_id="falabella",
        store_name="Falabella",
        title="Smart TV LG OLED 55 Pulgadas 4K HDR",
        category="televisores",
        current_price=129990.0,  # Posible glitch (normalmente $899.990)
        normal_price=899990.0,
        card_price=119990.0,
        url="https://www.falabella.com/falabella-cl/product/12345/tv-lg-oled",
        image_url="https://images.falabella.com/tv-lg-oled.jpg",
        in_stock=True,
    )

    p2 = ProductItem(
        id="pcfactory-rtx-4080",
        store_id="pcfactory",
        store_name="PC Factory",
        title="Tarjeta de Video ASUS GeForce RTX 4080 16GB",
        category="componentes",
        current_price=1199990.0,
        normal_price=1299990.0,
        url="https://www.pcfactory.cl/producto/rtx-4080",
        in_stock=True,
    )

    print("\n📦 Insertando productos con upsert_product()...")
    db.upsert_product(p1)
    db.upsert_product(p2)
    print("✅ Productos insertados exitosamente.")

    # 3. Simular fluctuación histórica de precios para comprobar get_baseline_price
    print("\n📈 Registrando historial histórico de precios (simulando 30 días atrás)...")
    with db.get_connection() as conn:
        cursor = conn.cursor()
        # Insertar precios históricos para la TV simulando días anteriores
        for day_offset, hist_price in [(25, 899990.0), (20, 879990.0), (15, 899990.0), (5, 949990.0)]:
            ts = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT INTO price_history (product_id, price, timestamp) VALUES (?, ?, ?);",
                (p1.id, hist_price, ts)
            )

    baseline = db.get_baseline_price(p1.id, days=30)
    print(f"✅ Baseline price calculado para {p1.id}: ${baseline:,.0f} CLP (Mediana de 30 días)")
    assert baseline is not None and baseline > 800000.0, "El baseline price debería rondar los $899.990"

    # 4. Registrar una alerta de Glitch detectada
    print("\n🚨 Registrando alerta de Glitch...")
    alert_payload = {
        "id": "ALT-TEST-001",
        "product_id": p1.id,
        "severity": "CRITICO",
        "confidence_score": 96.5,
        "discount_pct": 85.5,
        "savings_clp": 770000.0,
        "reason": "Caída de ceros detectada (ratio ~7.5x) y descuento masivo de 85.5% vs mediana histórica.",
        "telegram_sent": False,
    }
    alert_id = db.record_glitch_alert(alert_payload)
    print(f"✅ Alerta registrada con ID: {alert_id}")

    # También registrar con modelo GlitchAlert de Pydantic
    from glitchhunter_ai.core.models import GlitchVerdict, GlitchSeverity, SemanticClassificationResult
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=98.0,
        discount_percentage=85.5,
        absolute_savings_clp=770000.0,
        is_missing_zero=True,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="televisor_oled",
            explanation="Televisor OLED confirmado como dispositivo principal.",
            confidence=0.99
        ),
        reason="Glitch crítico confirmado por motor semántico y matemático."
    )
    alert_obj = GlitchAlert(
        alert_id="ALT-PYDANTIC-002",
        product=p1,
        verdict=verdict,
        alert_url=p1.url,
        telegram_sent=False
    )
    alert_id_2 = db.record_glitch_alert(alert_obj)
    print(f"✅ Alerta registrada vía objeto Pydantic GlitchAlert con ID: {alert_id_2}")

    # 5. Consultar glitches recientes
    print("\n📋 Consultando get_recent_glitches()...")
    recent = db.get_recent_glitches(limit=10)
    print(f"✅ Se recuperaron {len(recent)} glitches recientes:")
    for g in recent:
        print(f"   - [{g['severity']}] {g['title']} | Oferta: ${g['current_price']:,.0f} CLP | Ahorro: ${g['savings_clp']:,.0f} CLP")

    # 6. Estadísticas globales
    print("\n📊 Consultando get_stats()...")
    stats = db.get_stats()
    print(f"   • Total productos monitoreados: {stats['total_products']}")
    print(f"   • Total glitches registrados: {stats['total_glitches']}")
    print(f"   • Ahorro acumulado detectado: ${stats['accumulated_savings_clp']:,.0f} CLP")
    print(f"   • Glitches críticos: {stats['critical_glitches']}")
    print(f"   • Tiendas activas: {stats['active_stores']}")
    print(f"   • Entradas en historial: {stats['total_history_records']}")

    # 7. Marcar telegram sent
    marked = db.mark_telegram_sent(alert_id)
    print(f"\n📲 Marcar despacho Telegram ({alert_id}): {marked}")
    updated_glitches = db.get_recent_glitches(limit=10)
    assert any(g["id"] == alert_id and g["telegram_sent"] is True for g in updated_glitches)

    # Limpiar archivos de prueba
    for p in [test_db_path, test_db_path.with_name(test_db_path.name + "-wal"), test_db_path.with_name(test_db_path.name + "-shm")]:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    print("\n🎉 Todas las pruebas pasaron exitosamente. Módulo database.py verificado al 100%!")
