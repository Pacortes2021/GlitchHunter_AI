"""
GlitchHunter AI - Servidor Web FastAPI & Radar en Tiempo Real
Provee:
- Dashboard reactivo Cyberpunk/Terminal en tiempo real (HTML5/Tailwind/Lucide)
- Canal WebSocket (/ws/glitches) con broadcast instantáneo de nuevos glitches
- Endpoints REST (/api/glitches, /api/scan, /api/test-glitch, /api/stats, /api/status)
"""

import asyncio
import hashlib
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
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Security, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from glitchhunter_ai.config import (
    ALLOW_TEST_GLITCH,
    API_AUTH_KEY,
    AUTO_SCAN_ENABLED,
    CORS_ORIGINS,
    DATA_DIR,
    MIN_DISCOUNT_PERCENTAGE,
    SCAN_INTERVAL_SECONDS,
    STORES,
    USE_LIVE_SCRAPING,
    WEB_HOST,
    WEB_PORT,
)
from glitchhunter_ai.core.cart_sniper import generate_direct_add_to_cart_url
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
)
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.store_health import global_store_health
from glitchhunter_ai.notifications.telegram_bot import TelegramAlertDispatcher
from glitchhunter_ai.notifications.telegram_feedback import TelegramFeedbackPoller

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

    # Si la base de datos está vacía, el primer escaneo corre en un hilo de trabajo
    # para no bloquear el arranque del servidor mientras se scrapean las tiendas
    stats = db.get_stats()
    initial_scan = None
    if stats.get("total_products", 0) == 0:
        logger.info("📡 Inicializando radar: ejecutando primer escaneo LIVE de catálogo retail chileno...")
        initial_scan = MAIN_EVENT_LOOP.run_in_executor(None, pipeline.run_scan, USE_LIVE_SCRAPING)

    # Iniciar la tarea de escaneo periódico en segundo plano
    scanner_task = asyncio.create_task(background_scanner_loop())

    # Receptor de votos 👍/👎 de Telegram (solo con un bot real, fuera del modo simulación)
    feedback_poller = None
    if not pipeline.telegram_dispatcher.is_simulation:
        feedback_poller = TelegramFeedbackPoller(pipeline.telegram_dispatcher.bot_token, db)
        feedback_poller.start()

    yield

    if feedback_poller:
        feedback_poller.stop()

    # Cancelar tarea al apagar el servidor
    scanner_task.cancel()
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass
    if initial_scan is not None and not initial_scan.done():
        logger.info("⏳ Esperando a que finalice el escaneo inicial en curso...")
        await asyncio.wait([initial_scan])
    logger.info("🛑 Apagando Radar Web GlitchHunter AI...")


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_admin_key(
    api_key: Optional[str] = Security(api_key_header),
    authorization: Optional[str] = Header(None),
) -> bool:
    """
    Control de Acceso Administrativo (Hallazgo 3 de Auditoría):
    Protege rutas de escaneo e inyección. Si API_AUTH_KEY está configurada en .env,
    exige coincidencia con 'X-API-Key' o 'Authorization: Bearer <key>'.
    En modo desarrollo local (API_AUTH_KEY vacía), permite acceso transparente.
    """
    if not API_AUTH_KEY:
        return True

    provided_key = api_key
    if not provided_key and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided_key = parts[1]
        elif len(parts) == 1:
            provided_key = parts[0]

    if not provided_key or provided_key != API_AUTH_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticación administrativa requerida: API Key inválida o ausente.",
        )
    return True


app = FastAPI(
    title="GlitchHunter AI - Retail Glitch Radar",
    description="Plataforma de detección en tiempo real de errores de precio y bugs retail en Chile",
    version="1.0.0",
    lifespan=lifespan,
)

# Configuración de CORS dinámica según configuración de entorno
parsed_origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=parsed_origins,
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

    prod_url = row.get("url") or ""
    prod_id = row.get("product_id") or ""
    title = row.get("title") or "Producto"

    sniper_url = generate_direct_add_to_cart_url(store_id, prod_url, prod_id)
    is_watchlist = bool(db.is_in_watchlist(title))
    real_latency = global_store_health.get_store_latency(store_id)
    verif_level = row.get("verification_level") or "VERIFICADO"
    if verif_level == "COMPRABLE":
        # Registros antiguos marcados COMPRABLE solo por el patrón del enlace Sniper (sin validar)
        verif_level = "VERIFICADO"

    return {
        "alert_id": row.get("id") or f"ALT-{store_id.upper()}-{uuid.uuid4().hex[:6].upper()}",
        "created_at": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "detected_at_exact": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "origin": "SIMULACION" if "test" in prod_id.lower() or "sim" in prod_id.lower() or prod_id.startswith(("fal-", "rip-", "par-", "lid-", "mlc-", "pcf-")) else "EN_VIVO",
        "pipeline_latency_ms": real_latency,
        "verification_level": verif_level,
        "alert_url": prod_url,
        "add_to_cart_url": sniper_url,
        "is_watchlist_match": is_watchlist,
        "market_median_price": float(row["market_median_price"]) if row.get("market_median_price") is not None and float(row["market_median_price"]) > 0 else None,
        "market_discount_pct": float(row["market_discount_pct"]) if row.get("market_discount_pct") is not None else None,
        "market_stores_count": int(row.get("market_stores_count") or 0),
        "market_match_method": row.get("market_match_method"),
        "market_matched_model": row.get("market_matched_model"),
        "upvotes": int(row.get("upvotes") or 0),
        "downvotes": int(row.get("downvotes") or 0),
        "telegram_sent": bool(row.get("telegram_sent") or False),
        "product": {
            "id": prod_id,
            "store_id": store_id,
            "store_name": store_name,
            "title": title,
            "category": row.get("category") or "General",
            "current_price": current_price,
            "normal_price": normal_price,
            "card_price": float(row["card_price"]) if row.get("card_price") is not None and float(row["card_price"]) > 0 else None,
            "url": prod_url,
            "add_to_cart_url": sniper_url,
            "image_url": row.get("image_url") or "",
            "in_stock": True,
        },
        "verdict": {
            "is_glitch": True,
            "severity": row.get("severity") or "MODERADO",
            "confidence_score": float(row.get("confidence_score") or 90.0),
            "discount_percentage": discount_pct,
            "absolute_savings_clp": savings_clp,
            "is_missing_zero": missing_zero,
            "is_fake_inflation": False,
            "verification_level": verif_level,
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
    prod_url = row.get("url") or ""
    prod_id = row.get("product_id") or ""
    title = row.get("title") or "Producto en Oferta"

    sniper_url = generate_direct_add_to_cart_url(store_id, prod_url, prod_id)
    is_watchlist = bool(db.is_in_watchlist(title))

    return {
        "alert_id": f"DEAL-{store_id.upper()}-{int(hashlib.sha1(prod_id.encode()).hexdigest(), 16) % 100000000}",
        "created_at": row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "origin": "OFERTA_COMERCIAL",
        "alert_url": prod_url,
        "add_to_cart_url": sniper_url,
        "is_watchlist_match": is_watchlist,
        "upvotes": int(row.get("upvotes") or 0),
        "downvotes": int(row.get("downvotes") or 0),
        "product": {
            "id": prod_id,
            "store_id": store_id,
            "store_name": store_name,
            "title": title,
            "category": row.get("category") or "General",
            "current_price": current_price,
            "normal_price": normal_price,
            "card_price": float(row["card_price"]) if row.get("card_price") is not None else None,
            "url": prod_url,
            "add_to_cart_url": sniper_url,
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


@app.post("/api/scan", dependencies=[Depends(verify_admin_key)])
async def trigger_scan(
    use_live: bool = USE_LIVE_SCRAPING,
    watchlist_only: bool = False,
):
    """
    Dispara un ciclo de escaneo inmediato descargado en un hilo de trabajo (run_in_executor)
    para evitar bloquear el event loop principal del servidor web.
    Soporta modo general con rotación de catálogo o escaneo prioritario de watchlist.
    Protegido contra concurrencia con cerrojo atómico (Hallazgo F de Auditoría 4).
    """
    if pipeline.is_scanning:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "busy",
                "message": "Un ciclo de escaneo ya se encuentra en ejecución. Por favor espera a que finalice.",
                "running": True,
            },
        )

    logger.info(f"⚡ Escaneo manual solicitado vía API (use_live={use_live}, watchlist_only={watchlist_only})")
    loop = asyncio.get_running_loop()
    # Cada nuevo glitch ya se difunde por WebSocket vía pipeline.on_glitch_callback
    if watchlist_only:
        detected = await loop.run_in_executor(None, pipeline.run_watchlist_scan, use_live)
    else:
        detected = await loop.run_in_executor(None, pipeline.run_scan, use_live)

    return {
        "status": "success",
        "scanned_stores": len(pipeline.scrapers),
        "detected_glitches_count": len(detected),
        "new_glitches": [a.model_dump(mode="json") for a in detected],
    }


@app.post("/api/test-glitch", dependencies=[Depends(verify_admin_key)])
async def inject_test_glitch():
    """
    Inyecta un glitch de prueba representativo para verificar en vivo
    la animación de radar, el sonido chiptune y el broadcast WebSocket.
    Aislado estrictamente de notificaciones push externas (send_notifications=False).
    """
    if not ALLOW_TEST_GLITCH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La inyección de glitches de prueba está deshabilitada en este entorno.",
        )
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
        title=f"{choice['title']} (Simulación {uid})",
        category=choice["category"],
        normal_price=choice["normal_price"],
        current_price=choice["current_price"],
        card_price=choice.get("card_price"),
        image_url=choice["image_url"],
        url=choice["url"],
        in_stock=True,
    )

    # El broadcast WebSocket lo realiza pipeline.on_glitch_callback
    alert = pipeline.process_product(product, send_notifications=False)
    if alert:
        return alert.model_dump(mode="json")

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


@app.get("/api/store-health")
async def get_store_health():
    """Retorna la matriz de salud, latencia y tiempos de respuesta de cada scraper."""
    return {"stores": global_store_health.get_matrix()}


class DealFeedbackRequest(BaseModel):
    vote: str = Field(..., description="Voto: 'up' (oferta real) o 'down' (falso positivo)")
    reason: Optional[str] = Field(None, description="Motivo opcional del voto")


@app.post("/api/deals/{deal_id}/feedback")
async def submit_deal_feedback(deal_id: str, payload: DealFeedbackRequest):
    """
    Registra el voto de la comunidad sobre un glitch u oferta comercial.
    No requiere clave administrativa (permite recopilar feedback libre de los usuarios).
    """
    try:
        res = db.record_deal_feedback(
            deal_id=deal_id,
            vote=payload.vote,
            reason=payload.reason,
        )
        return {
            "status": "success",
            "deal_id": res["deal_id"],
            "vote": res["vote"],
            "upvotes": res["upvotes"],
            "downvotes": res["downvotes"],
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.error(f"Error al registrar feedback para deal {deal_id}: {exc}")
        raise HTTPException(status_code=500, detail="Error interno al registrar feedback")


@app.get("/api/feedback/stats")
async def get_feedback_statistics():
    """
    Retorna métricas globales de satisfacción y precisión del radar según feedback de usuarios.
    """
    try:
        return db.get_feedback_stats()
    except Exception as exc:
        logger.error(f"Error al obtener estadísticas de feedback: {exc}")
        raise HTTPException(status_code=500, detail="Error interno al obtener estadísticas de feedback")


class WatchlistRequest(BaseModel):
    keyword: str


@app.get("/api/watchlist")
async def get_watchlist():
    """Retorna las palabras clave prioritarias configuradas en la watchlist."""
    return {"keywords": db.get_watchlist_keywords()}


@app.post("/api/watchlist", dependencies=[Depends(verify_admin_key)])
async def add_watchlist(payload: WatchlistRequest):
    """Agrega una nueva palabra clave a la watchlist del usuario."""
    success = db.add_watchlist_keyword(payload.keyword)
    return {
        "success": success,
        "keywords": db.get_watchlist_keywords(),
    }


@app.delete("/api/watchlist/{keyword}", dependencies=[Depends(verify_admin_key)])
async def delete_watchlist(keyword: str):
    """Elimina una palabra clave de la watchlist."""
    removed = db.remove_watchlist_keyword(keyword)
    return {
        "success": removed,
        "keywords": db.get_watchlist_keywords(),
    }


@app.post("/api/watchlist/scan", dependencies=[Depends(verify_admin_key)])
async def scan_watchlist_endpoint():
    """Ejecuta un escaneo bajo demanda exclusivo de las palabras clave de la watchlist."""
    if not pipeline:
        raise HTTPException(status_code=503, detail="Pipeline no inicializado")
    loop = asyncio.get_running_loop()
    glitches = await loop.run_in_executor(
        None,
        lambda: pipeline.run_watchlist_scan(use_live=USE_LIVE_SCRAPING),
    )
    return {
        "status": "success",
        "scanned_keywords": db.get_watchlist_keywords(),
        "glitches_found": len(glitches),
        "glitches": [g.model_dump(mode="json") for g in glitches],
    }


@app.get("/api/catalog/rotation")
async def get_catalog_rotation():
    """Retorna el estado de la rotación Round-Robin de categorías y ordenamiento prioritario."""
    if not pipeline or not pipeline.catalog_rotator:
        return {"status": "inactive"}
    return pipeline.catalog_rotator.get_status()


def start_server(host: str = WEB_HOST, port: int = WEB_PORT):
    """Función de entrada para arrancar el servidor con Uvicorn."""
    logger.info(f"🌐 Iniciando servidor GlitchHunter en http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_server()
