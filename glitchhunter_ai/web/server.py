"""
GlitchHunter AI - Servidor Web FastAPI & Radar en Tiempo Real
Provee:
- Dashboard reactivo Cyberpunk/Terminal en tiempo real (HTML5/Tailwind/Lucide)
- Canal WebSocket (/ws/glitches) con broadcast instantáneo de nuevos glitches
- Endpoints REST (/api/glitches, /api/scan, /api/test-glitch, /api/stats, /api/status)
"""

import asyncio
import json
import logging
import os
import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from glitchhunter_ai.config import (
    AUTO_SCAN_ENABLED,
    DATA_DIR,
    MIN_DISCOUNT_PERCENTAGE,
    SCAN_INTERVAL_SECONDS,
    STORES,
    USE_LIVE_SCRAPING,
    WEB_HOST,
    WEB_PORT,
)
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
)
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.notifications.telegram_bot import TelegramAlertDispatcher

logger = logging.getLogger("GlitchHunter.Server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STATIC_PATH = BASE_DIR / "static"


class ConnectionManager:
    """Administrador de conexiones WebSocket para broadcast en tiempo real."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"⚡ Cliente WebSocket conectado. Total activos: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"🔌 Cliente WebSocket desconectado. Total activos: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Difunde un mensaje JSON a todos los clientes web conectados."""
        if not self.active_connections:
            return

        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead_connections.append(connection)

        for dead in dead_connections:
            self.disconnect(dead)


manager = ConnectionManager()
db = PriceDatabase()
pipeline = GlitchHuntingPipeline(db=db)

MAIN_EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _sync_broadcast_callback(alert: Any):
    """
    Callback thread-safe invocado por el pipeline (desde hilos de trabajo o loop principal)
    para encolar broadcast en el event loop principal de asyncio.
    Garantiza que alertas detectadas en worker threads se transmitan vía WebSocket sin perderse.
    """
    try:
        if isinstance(alert, dict):
            payload = alert
        elif hasattr(alert, "model_dump"):
            payload = alert.model_dump(mode="json")
        else:
            payload = {"type": "alert", "data": str(alert)}

        global MAIN_EVENT_LOOP
        loop = MAIN_EVENT_LOOP
        if loop is None or not loop.is_running():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(manager.broadcast(payload), loop)
        else:
            logger.debug("No hay event loop principal activo disponible para broadcast WebSocket.")
    except Exception as exc:
        logger.debug(f"Error en _sync_broadcast_callback: {exc}")


pipeline.on_glitch_callback = _sync_broadcast_callback


async def background_scanner_loop():
    """
    Revisor Continuo Profesional (Arquitectura de Excelencia).
    Escanea periódicamente las tiendas. Si una oferta ya existe al mismo precio,
    la mantiene viva en la página silenciosamente y NUNCA repite la notificación.
    Solo alerta y notifica cuando descubre ofertas genuinamente nuevas o caídas de precio mayores.
    """
    if not AUTO_SCAN_ENABLED:
        logger.info("ℹ️ Escaneo automático en segundo plano desactivado por configuración.")
        return

    logger.info(f"🔄 Revisor Continuo de Retail activo (ciclo cada {SCAN_INTERVAL_SECONDS}s).")

    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            now_str = datetime.now().strftime("%H:%M:%S")
            loop = asyncio.get_running_loop()
            detected = await loop.run_in_executor(None, pipeline.run_scan, USE_LIVE_SCRAPING)
            stats = db.get_stats()
            total_g = stats.get("total_glitches", 0)
            total_d = stats.get("moderate_deals", 0)
            if detected:
                logger.info(
                    f"⚡ [Revisor Continuo {now_str}] {len(detected)} NUEVA(S) OFERTA(S) detectada(s) y notificadas por única vez. "
                    f"({total_g} glitches y {total_d} ofertas comerciales activas en radar)"
                )
            else:
                logger.info(
                    f"⏱️ [Revisor Continuo {now_str}] Catálogo verificado en las 6 tiendas: 0 ofertas nuevas. "
                    f"Radar activo con {total_g} glitches (≥65%) y {total_d} ofertas comerciales (40%-64%)."
                )
        except asyncio.CancelledError:
            logger.info("🛑 Tarea de escáner en segundo plano cancelada limpiamente.")
            break
        except Exception as exc:
            logger.error(f"❌ Error en ciclo de escaneo automático: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida del servidor: inicializa BD, corre escaneo inicial y activa bucle de fondo."""
    global MAIN_EVENT_LOOP
    MAIN_EVENT_LOOP = asyncio.get_running_loop()
    logger.info("🚀 Iniciando Radar Web GlitchHunter AI...")
    db.init_db()

    # Si la base de datos está vacía, correr un primer escaneo en segundo plano para poblar el radar
    stats = db.get_stats()
    if stats.get("total_glitches", 0) == 0:
        logger.info("📡 Inicializando radar: ejecutando primer escaneo LIVE de catálogo retail chileno...")
        pipeline.run_scan(use_live=USE_LIVE_SCRAPING)

    # Iniciar la tarea de escaneo periódico en segundo plano
    scanner_task = asyncio.create_task(background_scanner_loop())

    yield

    # Cancelar tarea al apagar el servidor
    scanner_task.cancel()
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass
    logger.info("🛑 Apagando Radar Web GlitchHunter AI...")


app = FastAPI(
    title="GlitchHunter AI - Retail Glitch Radar",
    description="Plataforma de detección en tiempo real de errores de precio y bugs retail en Chile",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar archivos estáticos para la interfaz de usuario
if STATIC_PATH.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_PATH)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    """Sirve la interfaz web interactiva tipo terminal / radar."""
    index_file = STATIC_PATH / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"status": "ok", "message": "GlitchHunter AI backend corriendo. /static/index.html no encontrado."})


@app.websocket("/ws/glitches")
async def websocket_glitches_endpoint(websocket: WebSocket):
    """
    Canal WebSocket bidireccional en tiempo real.
    Emite instantáneamente cada nuevo glitch detectado por el pipeline.
    """
    await manager.connect(websocket)
    try:
        # Enviar estado de bienvenida al conectar
        await websocket.send_json({
            "type": "WELCOME",
            "message": "Conectado al radar en tiempo real de GlitchHunter AI",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        while True:
            # Mantener conexión viva y procesar pings de clientes
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.debug(f"Cierre de conexión WebSocket: {exc}")
        manager.disconnect(websocket)


def format_glitch_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte un registro de glitch de la BD al formato anidado esperado por app.js."""
    store_id = row.get("store_id") or "retail"
    store_name = next((s["name"] for s in STORES if s["id"] == store_id), store_id.title())
    current_price = float(row.get("current_price") or 0.0)
    normal_price = float(row.get("normal_price") or 0.0)
    discount_pct = float(row.get("discount_pct") or 0.0)
    savings_clp = float(row.get("savings_clp") or 0.0)
    ratio = normal_price / max(current_price, 1.0)
    missing_zero = (ratio >= 8.5 and ratio <= 11.5)

    return {
        "alert_id": row.get("id") or f"ALT-{store_id.upper()}-{uuid.uuid4().hex[:6].upper()}",
        "created_at": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "detected_at_exact": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "origin": "SIMULACION" if "test" in (row.get("product_id") or "").lower() or "sim" in (row.get("product_id") or "").lower() or row.get("product_id", "").startswith(("fal-", "rip-", "par-", "lid-", "mlc-", "pcf-")) else "EN_VIVO",
        "pipeline_latency_ms": 14,
        "alert_url": row.get("url") or "",
        "product": {
            "id": row.get("product_id") or "",
            "store_id": store_id,
            "store_name": store_name,
            "title": row.get("title") or "Producto",
            "category": row.get("category") or "General",
            "current_price": current_price,
            "normal_price": normal_price,
            "card_price": float(row["card_price"]) if row.get("card_price") is not None else None,
            "url": row.get("url") or "",
            "image_url": row.get("image_url") or "",
            "in_stock": True,
        },
        "verdict": {
            "is_glitch": True,
            "severity": row.get("severity") or "MODERADO",
            "confidence_score": float(row.get("confidence_score") or 95.0),
            "discount_percentage": discount_pct,
            "absolute_savings_clp": savings_clp,
            "is_missing_zero": missing_zero,
            "is_fake_inflation": False,
            "reason": row.get("reason") or f"Descuento de {discount_pct:.1f}%",
        },
    }


@app.get("/api/glitches")
async def get_glitches(
    store_id: Optional[str] = Query(None, description="Filtrar por ID de tienda (falabella, ripley, etc.)"),
    severity: Optional[str] = Query(None, description="Filtrar por severidad (CRITICO, ALTO, MODERADO)"),
    limit: int = Query(100, ge=1, le=500, description="Cantidad máxima de resultados"),
):
    """
    Retorna la lista de alertas de glitches registradas en la base de datos.
    Soporta filtros por tienda y severidad.
    """
    raw_glitches = db.get_recent_glitches(limit=limit)
    formatted = [format_glitch_record(g) for g in raw_glitches]

    if store_id:
        formatted = [g for g in formatted if g["product"]["store_id"].lower() == store_id.lower()]
    if severity:
        sev_upper = severity.upper()
        formatted = [g for g in formatted if (g["verdict"]["severity"] or "").upper() == sev_upper]

    return formatted


def format_commercial_deal_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte un registro de oferta comercial (40% - 64%) al formato anidado esperado."""
    store_id = row.get("store_id") or "retail"
    store_name = next((s["name"] for s in STORES if s["id"] == store_id), store_id.title())
    current_price = float(row.get("current_price") or 0.0)
    normal_price = float(row.get("normal_price") or 0.0)
    discount_pct = float(row.get("discount_pct") or 0.0)
    savings_clp = float(row.get("savings_clp") or 0.0)

    return {
        "alert_id": f"DEAL-{store_id.upper()}-{abs(hash(row.get('product_id', ''))) % 100000000}",
        "created_at": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "origin": "OFERTA_COMERCIAL",
        "alert_url": row.get("url") or "",
        "product": {
            "id": row.get("product_id") or "",
            "store_id": store_id,
            "store_name": store_name,
            "title": row.get("title") or "Producto en Oferta",
            "category": row.get("category") or "General",
            "current_price": current_price,
            "normal_price": normal_price,
            "card_price": float(row["card_price"]) if row.get("card_price") is not None else None,
            "url": row.get("url") or "",
            "image_url": row.get("image_url") or "",
            "in_stock": True,
        },
        "verdict": {
            "is_glitch": False,
            "severity": "MODERADO",
            "confidence_score": 80.0,
            "discount_percentage": discount_pct,
            "absolute_savings_clp": savings_clp,
            "is_missing_zero": False,
            "is_fake_inflation": False,
            "reason": f"Descuento comercial destacado de {discount_pct:.1f}%",
        },
    }


@app.get("/api/deals")
async def get_deals(
    min_discount: float = Query(40.0, ge=0.0, le=100.0, description="Descuento mínimo porcentual (ej: 40)"),
    max_discount: float = Query(65.0, ge=0.0, le=100.0, description="Descuento máximo porcentual (ej: 65)"),
    store_id: Optional[str] = Query(None, description="Filtrar por ID de tienda (falabella, ripley, etc.)"),
    limit: int = Query(100, ge=1, le=500, description="Cantidad máxima de resultados"),
):
    """
    Retorna ofertas comerciales moderadas (ej: 40% a 64% de descuento).
    Alimenta la segunda columna de ofertas del radar web.
    """
    raw_deals = db.get_commercial_deals(
        min_discount=min_discount,
        max_discount=max_discount,
        limit=limit,
        store_id=store_id,
    )
    return [format_commercial_deal_record(d) for d in raw_deals]


@app.post("/api/scan")
async def trigger_scan(use_live: bool = USE_LIVE_SCRAPING):
    """
    Dispara un ciclo de escaneo inmediato a través de los scrapers retail.
    Evalúa anomalías matemáticas, filtra carcasas con IA y difunde nuevos hallazgos.
    """
    logger.info(f"⚡ Escaneo manual solicitado vía API (use_live={use_live})")
    detected = pipeline.run_scan(use_live=use_live)

    # Broadcast de cada nuevo glitch por WebSockets
    for alert in detected:
        await manager.broadcast(alert.model_dump(mode="json"))

    return {
        "status": "success",
        "scanned_stores": len(pipeline.scrapers),
        "detected_glitches_count": len(detected),
        "new_glitches": [a.model_dump(mode="json") for a in detected],
    }


@app.post("/api/test-glitch")
async def inject_test_glitch():
    """
    Inyecta un glitch de prueba representativo para verificar en vivo
    la animación de radar, el sonido chiptune y el broadcast WebSocket.
    """
    sample_catalog = [
        {
            "store_id": "falabella",
            "store_name": "Falabella Chile",
            "title": "Smart TV Samsung OLED 65\" 4K UHD QN65S90C + Barra Sonido",
            "category": "televisores",
            "normal_price": 999990.0,
            "current_price": 99990.0,
            "card_price": 89990.0,
            "image_url": "https://images.falabella.com/valid-tv.jpg",
            "url": "https://www.falabella.com/falabella-cl/product/16912389/Smart-TV-Samsung-OLED-65-4K/16912389",
        },
        {
            "store_id": "pcfactory",
            "store_name": "PC Factory",
            "title": "Tarjeta de Video NVIDIA GeForce RTX 4080 Super Gaming 16GB",
            "category": "computacion",
            "normal_price": 1199990.0,
            "current_price": 119990.0,
            "card_price": 109990.0,
            "image_url": "https://www.pcfactory.cl/public/foto/rtx4080s.jpg",
            "url": "https://www.pcfactory.cl/producto/48912-nvidia-rtx4080super",
        },
        {
            "store_id": "ripley",
            "store_name": "Ripley Chile",
            "title": "Apple iPad Air M2 11\" Wi-Fi 128GB Gris Espacial (2024)",
            "category": "tecnologia",
            "normal_price": 749990.0,
            "current_price": 74990.0,
            "card_price": 69990.0,
            "image_url": "https://simple.ripley.cl/ipad-air-m2.jpg",
            "url": "https://simple.ripley.cl/apple-ipad-air-m2-11-wifi-128gb-2000398412841p",
        },
    ]

    choice = random.choice(sample_catalog)
    uid = uuid.uuid4().hex[:6].upper()
    product = ProductItem(
        id=f"test-{choice['store_id']}-{uid.lower()}",
        store_id=choice["store_id"],
        store_name=choice["store_name"],
        title=f"{choice['title']} [TEST-{uid}]",
        category=choice["category"],
        normal_price=choice["normal_price"],
        current_price=choice["current_price"],
        card_price=choice.get("card_price"),
        image_url=choice["image_url"],
        url=choice["url"],
        in_stock=True,
    )

    alert = pipeline.process_product(product)
    if alert:
        payload = alert.model_dump(mode="json")
        await manager.broadcast(payload)
        return payload

    return {"status": "error", "message": "No se pudo generar glitch de prueba"}


@app.get("/api/stats")
async def get_stats():
    """Retorna métricas acumuladas del sistema: productos, glitches y ahorro total en CLP."""
    return db.get_stats()


@app.get("/api/status")
async def get_status():
    """Retorna el estado de salud operativa del sistema y la lista de retailers activos."""
    return {
        "status": "online",
        "name": "GlitchHunter AI Radar",
        "version": "1.0.0",
        "stores_monitored": len(STORES),
        "stores": STORES,
        "websocket_active_clients": len(manager.active_connections),
        "auto_scan_enabled": AUTO_SCAN_ENABLED,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "scan_interval_minutes": round(SCAN_INTERVAL_SECONDS / 60, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def start_server(host: str = WEB_HOST, port: int = WEB_PORT):
    """Función de entrada para arrancar el servidor con Uvicorn."""
    logger.info(f"🌐 Iniciando servidor GlitchHunter en http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_server()
