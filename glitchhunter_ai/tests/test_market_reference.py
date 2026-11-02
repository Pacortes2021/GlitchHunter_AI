"""
Tests unitarios y de integración para el Sistema de Referencia de Mercado Inter-Tiendas (Punto 2).
Cubre:
1. Extracción estricta de códigos de modelo (TVs, GPUs, CPUs, Audio, Smartphones).
2. Exclusión estricta de la propia tienda y exigencia de >= 2 tiendas competidoras con stock.
3. Señal 1: Modulación de confianza (+5 por anomalía confirmada, -10 por precio estándar).
4. Señal 2: Detección de falsa inflación cruzada (precio normal > 1.8x mediana de mercado),
   que marca la alerta y penaliza su confianza sin descartarla ni enviar push.
5. Caché SQLite con TTL y fallback transparente a None en caso de timeout o error.
"""

from unittest.mock import MagicMock, patch
import pytest

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.model_extractor import extract_model_code
from glitchhunter_ai.core.market_reference import MarketReferenceService, MarketReferenceResult
from glitchhunter_ai.core.models import ProductItem, GlitchSeverity, VerificationLevel
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline


# =====================================================================
# 1. Pruebas de Extracción de Código de Modelo
# =====================================================================
def test_extract_model_code_accuracy():
    """Verifica que el extractor obtenga el código exacto de hardware tecnológico."""
    # TVs
    assert extract_model_code('Smart TV Samsung OLED 65" 4K UHD QN65S90C') == "QN65S90C"
    assert extract_model_code("LG OLED 55 C3 4K Smart TV OLED55C3PSA") == "OLED55C3PSA"
    assert extract_model_code("Smart TV Samsung Crystal UHD 55 UN55CU7000") == "UN55CU7000"

    # GPUs
    assert extract_model_code("Tarjeta de Video ASUS GeForce RTX 4070 Super 12GB") == "RTX 4070 Super"
    assert extract_model_code("PowerColor Radeon RX 7800 XT 16GB GDDR6") == "RX 7800 XT"

    # CPUs
    assert extract_model_code("Procesador AMD Ryzen 7 7800X3D 8-Core AM5") == "Ryzen 7 7800X3D"
    assert extract_model_code("Intel Core i7-14700HX 20-Core") == "i7-14700HX"

    # Audio & Smartphones
    assert extract_model_code("Audífonos Sony WH-1000XM5 Cancelación Ruido") == "WH-1000XM5"
    assert extract_model_code("Celular Samsung Galaxy S24 Ultra SM-S928B 512GB") == "SM-S928B"
    assert extract_model_code("Apple iPhone 15 Pro Max 256GB Titanio") == "iPhone 15 Pro Max 256GB"
    # Sin capacidad no hay modelo comparable (128GB y 1TB no valen lo mismo)
    assert extract_model_code("Apple iPhone 15 Pro Max Titanio") is None

    # Corchetes explícitos y filtros de tags de marketing
    assert extract_model_code("Notebook HP Omen 14-FB0004LA [A0DH7LA]") == "A0DH7LA"
    assert extract_model_code("Tarjeta de Video NVIDIA GeForce RTX 4080 Super Gaming 16GB [TEST-766DA8]") == "RTX 4080 Super"
    assert extract_model_code("Smart TV LG 55 UHD [REACONDICIONADO]") is None
    assert extract_model_code("Consola de Juegos [OFERTA CYBER]") is None

    # Casos sin modelo (no deben inventar o aproximar erróneamente)
    assert extract_model_code("Funda Silicona Roja para Smartphone") is None
    assert extract_model_code("Cable HDMI 2.1 Ultra High Speed 2 metros") is None
    assert extract_model_code("Soporte de Pared Fijo para Televisores") is None


def test_extract_model_code_rejects_component_codes_in_host_devices():
    """Un chip (GPU/CPU) dentro de un notebook o PC no identifica al equipo completo."""
    assert extract_model_code("Notebook ASUS TUF Gaming F15 Intel Core i7 RTX 4060 16GB 512GB") is None
    assert extract_model_code("Notebook Lenovo IdeaPad Slim 3 Intel Core i5-12450H 16GB") is None
    assert extract_model_code("PC Gamer Ryzen 7 7800X3D RTX 4070 32GB") is None
    assert extract_model_code("Apple MacBook Air 13 Chip M2 256GB") is None
    # El mismo chip vendido como componente sí es un modelo válido
    assert extract_model_code("Tarjeta de Video ASUS Dual GeForce RTX 4060 8GB") == "RTX 4060"
    # Nombres de línea de consola aparecen en controles y juegos: no se usan
    assert extract_model_code("Control DualSense PlayStation 5 Blanco") is None
    assert extract_model_code("PlayStation 5 Slim 1TB") is None


# =====================================================================
# 2. Pruebas de Servicio de Referencia de Mercado (Exclusión y Tiendas Mínimas)
# =====================================================================
def test_market_reference_excludes_evaluating_store_and_requires_minimum_two():
    """
    Verifica que:
    1. Se excluya estrictamente la tienda del producto que se está evaluando.
    2. Se requieran al menos 2 tiendas competidoras distintas con stock disponible.
    """
    service = MarketReferenceService(timeout=2.0)
    item = ProductItem(
        id="fal-tv-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Smart TV Samsung OLED 65 QN65S90C",
        category="televisores",
        current_price=129990.0,
        normal_price=999990.0,
        url="https://falabella.com/p/tv",
        in_stock=True,
    )

    # Simular respuesta de SoloTodo con entidades:
    # - Entidad 1: Falabella (debe ser excluida)
    # - Entidad 2: Ripley con stock ($989.990)
    # - Entidad 3: Paris SIN stock (debe ser ignorada)
    mock_entities = [
        {
            "store_id": 9,
            "external_url": "https://www.falabella.com/product/123",
            "active_registry": {"is_available": True, "offer_price": 129990.0, "normal_price": 999990.0},
        },
        {
            "store_id": 18,
            "external_url": "https://simple.ripley.cl/product/456",
            "active_registry": {"is_available": True, "offer_price": 989990.0, "normal_price": 1099990.0},
        },
        {
            "store_id": 11,
            "external_url": "https://www.paris.cl/product/789",
            "active_registry": {"is_available": False, "offer_price": 899990.0, "normal_price": 999990.0},
        },
    ]

    with patch.object(service, "_query_solotodo") as mock_query:
        # Caso 1: Solo 1 tienda competidora con stock disponible -> debe retornar None
        mock_query.return_value = None  # _query_solotodo ya aplica el filtro y retorna None si < 2 tiendas
        res = service.get_market_reference(item, exclude_store_id="falabella")
        assert res is None

        # Caso 2: 3 tiendas competidoras con stock (excluyendo Falabella)
        mock_query.return_value = {
            "median_price": 999990.0,
            "median_normal_price": 1099990.0,
            "stores_count": 3,
            "sample_stores": ["18", "11", "12"],
        }
        res2 = service.get_market_reference(item, exclude_store_id="falabella")
        assert res2 is not None
        assert res2.median_price == 999990.0
        assert res2.stores_count == 3
        assert res2.match_method == "solotodo"
        assert res2.matched_model == "QN65S90C"
        assert res2.market_discount_pct > 80.0
        assert res2.is_market_anomaly is True


# =====================================================================
# 3. Pruebas de Caché SQLite con TTL
# =====================================================================
def test_market_reference_sqlite_cache_ttl(tmp_path):
    """Verifica que el servicio consulte y persista en caché SQLite respetando el TTL."""
    db = PriceDatabase(db_path=tmp_path / "test_cache.db")
    db.init_db()

    service = MarketReferenceService(timeout=2.0, cache_ttl_hours=6.0)
    item = ProductItem(
        id="pcf-gpu-01",
        store_id="pcfactory",
        store_name="PC Factory",
        title="Tarjeta ASUS GeForce RTX 4070 Super",
        category="componentes",
        current_price=109990.0,
        normal_price=699990.0,
        url="https://pcfactory.cl/p/rtx4070",
        in_stock=True,
    )

    # 1. Guardar en caché previamente
    db.save_cached_market_reference(
        cache_key="mkt_ref:rtx4070super:pcfactory",
        median_price=689990.0,
        median_normal_price=749990.0,
        stores_count=4,
        match_method="solotodo",
        matched_model="RTX 4070 Super",
        ttl_hours=6.0,
    )

    # 2. get_market_reference debe devolver directamente el resultado desde caché sin llamar a SoloTodo
    with patch.object(service, "_query_solotodo") as mock_solotodo:
        res = service.get_market_reference(item, exclude_store_id="pcfactory", db=db)
        assert res is not None
        assert res.median_price == 689990.0
        assert res.stores_count == 4
        assert res.matched_model == "RTX 4070 Super"
        # No debió hacer petición HTTP a SoloTodo
        mock_solotodo.assert_not_called()

    # 3. Para otra tienda la mediana cacheada no sirve (incluía a esa tienda): se vuelve a consultar
    item_ripley = item.model_copy(update={"id": "rip-gpu-01", "store_id": "ripley"})
    with patch.object(service, "_query_solotodo", return_value=None) as mock_solotodo:
        assert service.get_market_reference(item_ripley, exclude_store_id="ripley", db=db) is None
        mock_solotodo.assert_called_once()


# =====================================================================
# 4. Pruebas de Modulación de Certeza (Señal 1: +5 y -10)
# =====================================================================
def test_pipeline_market_signal_1_confidence_modulation(tmp_path):
    """
    Verifica que:
    - Si el mercado confirma anomalía masiva (market_discount >= 60%), la confianza sube +5 puntos.
    - Si el mercado ya vende el producto al mismo precio (market_discount <= 15%), la confianza baja -10 puntos.
    """
    db = PriceDatabase(db_path=tmp_path / "test_signal_1.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # Caso A: Anomalía confirmada frente a todo el retail (+5 puntos)
    item_glitch = ProductItem(
        id="item-rtx-anomaly",
        store_id="pcfactory",
        store_name="PC Factory",
        title="ASUS GeForce RTX 4070 Super 12GB",
        category="componentes",
        current_price=119990.0,
        normal_price=699990.0,
        url="https://pcfactory.cl/p/gpu",
        in_stock=True,
    )

    ref_anomaly = MarketReferenceResult(
        median_price=699990.0,
        median_normal_price=749990.0,
        stores_count=3,
        match_method="solotodo",
        matched_model="RTX 4070 Super",
        market_discount_pct=82.8,
        is_market_anomaly=True,
        is_fake_inflation=False,
        sample_stores=["falabella", "ripley", "paris"],
    )

    with patch("glitchhunter_ai.core.pipeline.default_market_reference.get_market_reference", return_value=ref_anomaly):
        alert_a = pipeline.process_product(item_glitch, send_notifications=False)
        assert alert_a is not None
        # Confianza original ~82%, con +5 del mercado debe ser >= 87%
        assert alert_a.verdict.confidence_score >= 87.0
        assert alert_a.market_median_price == 699990.0
        assert alert_a.market_stores_count == 3
        assert alert_a.market_matched_model == "RTX 4070 Super"

    # Caso B: Precio estándar de mercado (-10 puntos de confianza)
    item_standard = ProductItem(
        id="item-standard-price",
        store_id="paris",
        store_name="Paris.cl",
        title="Monitor Gamer Alienware AW3423DWF Curvo",
        category="computacion",
        current_price=109990.0,
        normal_price=699990.0,  # La tienda dice que su normal es $699.990
        url="https://paris.cl/p/mon",
        in_stock=True,
    )

    # Pero el resto de las tiendas también lo tiene a ~$112.000 (descuento vs mercado solo 1.8%)
    ref_standard = MarketReferenceResult(
        median_price=112000.0,
        median_normal_price=120000.0,
        stores_count=3,
        match_method="solotodo",
        matched_model="AW3423DWF",
        market_discount_pct=1.8,
        is_market_anomaly=False,
        is_fake_inflation=False,
        sample_stores=["pcfactory", "ripley", "falabella"],
    )

    with patch("glitchhunter_ai.core.pipeline.default_market_reference.get_market_reference", return_value=ref_standard):
        alert_b = pipeline.process_product(item_standard, send_notifications=False)
        # La confianza debe haber sido penalizada en -10 puntos
        assert alert_b is not None
        assert alert_b.verdict.confidence_score <= 80.0


# =====================================================================
# 5. Pruebas de Señal 2: Detección de Falsa Inflación Inter-Tiendas
# =====================================================================
def test_pipeline_market_signal_2_fake_inflation_flags_without_push(tmp_path):
    """
    Verifica que si una tienda infla artificialmente su precio normal (> 1.8x la mediana de mercado)
    para aparentar un descuento de 70%, el pipeline NO descarte la alerta (un match erróneo no debe
    ocultar un glitch real), sino que la marque como inflada, penalice su confianza y omita el push.
    """
    db = PriceDatabase(db_path=tmp_path / "test_signal_2.db")
    db.init_db()

    telegram = MagicMock(simulation_mode=False, is_simulation=False)
    ntfy = MagicMock()
    discord = MagicMock()
    pipeline = GlitchHuntingPipeline(
        db=db, telegram_dispatcher=telegram, ntfy_dispatcher=ntfy, discord_dispatcher=discord
    )

    # Tienda infla el precio normal a $500.000 y lo ofrece a $149.990 (70% dcto artificial)
    fake_bait_item = ProductItem(
        id="fake-bait-tv",
        store_id="ripley",
        store_name="Ripley Chile",
        title="Smart TV Samsung UN55CU7000",
        category="televisores",
        current_price=149990.0,
        normal_price=500000.0,
        url="https://ripley.cl/p/tv",
        in_stock=True,
    )

    # Mediana en las otras tiendas es $155.000 (el producto siempre vale ~$150.000)
    ref_fake = MarketReferenceResult(
        median_price=155000.0,
        median_normal_price=160000.0,
        stores_count=3,
        match_method="solotodo",
        matched_model="UN55CU7000",
        market_discount_pct=3.2,
        is_market_anomaly=False,
        is_fake_inflation=True,  # normal $500k > 1.8 * $155k ($279k)
        sample_stores=["falabella", "paris", "lider"],
    )

    with patch("glitchhunter_ai.core.pipeline.default_market_reference.get_market_reference", return_value=ref_fake):
        alert = pipeline.process_product(fake_bait_item, send_notifications=True)

    assert alert is not None
    assert alert.verdict.is_fake_inflation is True
    assert "inflado vs mercado" in alert.verdict.reason
    assert alert.verdict.confidence_score <= 75.0
    telegram.send_alert.assert_not_called()
    ntfy.send_alert.assert_not_called()
    discord.send_alert.assert_not_called()


# =====================================================================
# 5b. Consulta a SoloTodo con respuestas reales (exclusión, condición, match de nombre)
# =====================================================================
def _mock_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _entity(store, url, price, available=True, condition="https://schema.org/NewCondition"):
    return {
        "store": store,
        "store_id": store,
        "external_url": url,
        "condition": condition,
        "active_registry": {"is_available": available, "offer_price": str(price), "normal_price": str(price)},
    }


def test_query_solotodo_excludes_only_own_store_and_used_items():
    """
    Estructura real de /products/{id}/entities/:
    - Excluye la tienda evaluada por ID exacto y por dominio (incluye IDs alternativos como 8147).
    - No excluye otras tiendas solo porque su URL contenga dígitos como "9".
    - Ignora productos reacondicionados y open-box.
    """
    service = MarketReferenceService()
    browse = {"results": [{"product_entries": [{"product": {"id": 140261, "name": "Sony WH-1000XM5 Wireless Headphone"}}]}]}
    entities = [
        _entity(9, "https://www.falabella.com/falabella-cl/product/119/x", 199990),
        _entity(8147, "https://www.falabella.com/falabella-cl/product/999/y", 189990),
        _entity(11, "https://www.paris.cl/audifonos-sony-wh-1000xm5-999999999.html", 229990),
        _entity(18, "https://simple.ripley.cl/audifonos-sony-wh-1000xm5-2000390119p", 239990),
        _entity(8510, "https://simple.ripley.cl/otro-listado-ripley-2000390999p", 249990),
        _entity(12, "https://www.pcfactory.cl/producto/47938-sony", 219990),
        _entity(2471, "https://www.reuse.cl/collections/audio/wh-1000xm5", 99990, condition="https://schema.org/RefurbishedCondition"),
        _entity(5771, "https://bodegaoportunidades.cl/products/wh-1000xm5", 89990, condition="https://schema.org/OpenBoxCondition"),
    ]
    service.client = MagicMock()
    service.client.get.side_effect = [_mock_response(browse), _mock_response(entities)]

    data = service._query_solotodo("WH-1000XM5", exclude_store_id="falabella")
    assert data is not None
    # Paris, Ripley (dos IDs, una sola tienda) y PC Factory; sin Falabella ni usados
    assert sorted(data["sample_stores"]) == ["paris", "pcfactory", "ripley"]
    assert data["stores_count"] == 3
    assert data["median_price"] == 229990


def test_query_solotodo_returns_none_without_name_match():
    """Si ningún resultado de búsqueda contiene el código de modelo, no se usa el primero 'a ciegas'."""
    service = MarketReferenceService()
    browse = {"results": [{"product_entries": [{"product": {"id": 1, "name": "Sony WH-CH520 Headphone"}}]}]}
    service.client = MagicMock()
    service.client.get.side_effect = [_mock_response(browse)]

    assert service._query_solotodo("WH-1000XM5", exclude_store_id="paris") is None
    assert service.client.get.call_count == 1


def test_local_cross_store_reference_requires_same_model_and_fresh_prices(tmp_path):
    """El fallback local no mezcla variantes (iPhone 15 vs 15 Pro Max) ni usa SoloTodo como tienda."""
    db = PriceDatabase(db_path=tmp_path / "test_local_ref.db")
    db.init_db()

    def add(pid, store, title, price):
        db.upsert_product(ProductItem(
            id=pid, store_id=store, store_name=store, title=title, category="smartphones",
            current_price=price, normal_price=price, url=f"https://{store}.cl/{pid}", in_stock=True,
        ))

    add("p1", "paris", "Apple iPhone 15 128GB Negro", 699990)
    add("p2", "ripley", "iPhone 15 128GB Azul", 709990)
    add("p3", "lider", "Apple iPhone 15 Pro Max 1TB", 1899990)   # otra variante: no debe contar
    add("p4", "solotodo", "Apple iPhone 15 128GB", 649990)       # meta-retailer: no debe contar

    ref = db.find_local_cross_store_reference("iPhone 15 128GB", exclude_store_id="falabella")
    assert ref is not None
    assert sorted(ref["sample_stores"]) == ["paris", "ripley"]
    assert ref["median_price"] == (699990 + 709990) / 2


# =====================================================================
# 6. Fallback Transparente a None en Timeout o Error
# =====================================================================
def test_pipeline_timeout_fallback_does_not_block_alert(tmp_path):
    """
    Verifica que si la consulta de mercado falla o supera el timeout,
    el pipeline continúa su curso normalmente sin bloquear la alerta.
    """
    db = PriceDatabase(db_path=tmp_path / "test_fallback.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    genuine_glitch = ProductItem(
        id="genuine-glitch-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="PlayStation 5 Slim 1TB",
        category="consolas",
        current_price=49990.0,
        normal_price=499990.0,
        url="https://falabella.com/p/ps5",
        in_stock=True,
    )

    # Simular que el servicio de mercado retorna None (por timeout o sin conexión)
    with patch("glitchhunter_ai.core.pipeline.default_market_reference.get_market_reference", return_value=None):
        alert = pipeline.process_product(genuine_glitch, send_notifications=False)
        # La alerta legítima DEBE salir sin retrasos ni bloqueos
        assert alert is not None
        assert alert.market_median_price is None
        assert alert.market_stores_count == 0
        assert alert.verdict.is_glitch is True
        assert alert.verdict.severity == GlitchSeverity.CRITICO
