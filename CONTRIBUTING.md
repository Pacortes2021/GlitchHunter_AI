# 🤝 Guía de Contribución — GlitchHunter AI

¡Gracias por tu interés en colaborar con GlitchHunter AI! Este proyecto está pensado para crecer de manera abierta y colaborativa con la comunidad de desarrolladores de Chile y Latinoamérica.

---

## 🛠️ Entorno de Desarrollo

1. Realiza un `fork` o clona el repositorio:
   ```bash
   git clone <URL_DEL_REPOSITORIO>
   cd GlitchHunter_AI
   ```
2. Crea tu entorno virtual:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Verifica que la suite de pruebas pase al 100%:
   ```bash
   python3 run.py test
   ```

---

## 🏬 Cómo agregar un nuevo Retailer (Scraper)

Para sumar una nueva tienda (por ejemplo `hites`, `abcdin` o `sodimac`):

1. **Crear el archivo en `glitchhunter_ai/scrapers/<tienda>.py`**:
   Implementa una clase con la siguiente interfaz:
   ```python
   from typing import List
   from glitchhunter_ai.core.models import ProductItem

   class MiTiendaScraper:
       store_id: str = "mitienda"
       store_name: str = "Mi Tienda Chile"
       base_url: str = "https://www.mitienda.cl"

       def fetch_catalog(self, use_live: bool = False) -> List[ProductItem]:
           if use_live:
               # 1. Tu lógica de extracción en vivo (HTTP, API o LiveRetailFeed)
               pass
           # 2. Catálogo simulado de respaldo para pruebas unitarias
           return self.get_simulated_catalog()
   ```

2. **Registrar el scraper en `glitchhunter_ai/scrapers/__init__.py`**:
   Añade tu clase a `get_all_scrapers()`.

3. **Agregar la tienda en `glitchhunter_ai/config.py`**:
   Añade un dict a `STORES`:
   ```python
   {"id": "mitienda", "name": "Mi Tienda", "domain": "mitienda.cl"}
   ```

4. **Crear sus pruebas unitarias en `glitchhunter_ai/tests/test_<tienda>_scraper.py`**:
   Asegúrate de que extraiga precios correctos, filtre valores negativos y tipifique como `ProductItem`.

---

## 🧠 Cómo mejorar el Motor de Detección de IA

* **Filtro Semántico (`glitchhunter_ai/ai_detector/semantic_filter.py`):**
  Si detectas que una funda o accesorio no fue filtrado correctamente, añade sus palabras clave o patrones de expresión regular a `KEYWORD_RULES` o calibra el analizador de contexto.
* **Detector Matemático (`glitchhunter_ai/ai_detector/math_detector.py`):**
  Maneja la detección de ceros perdidos (`missing_zero_ratio`) y cálculo de z-scores sobre el historial base.
* **Verificador de Inflación (`glitchhunter_ai/ai_detector/inflation_verifier.py`):**
  Compara contra la mediana de 30 días para desenmascarar aumentos artificiales previos a eventos CyberDay.

---

## 📢 Nuevos Canales de Alerta

Actualmente soportamos:
* **ntfy.sh:** Notificaciones push directas a teléfonos móviles con URL clickeable.
* **Telegram Bot:** Mensajes formateados en HTML con teclado interactivo inline.

Si deseas sumar **Discord Webhooks**, **WhatsApp Business API** o **Slack**:
1. Crea un despachador en `glitchhunter_ai/notifications/<canal>.py`.
2. Conéctalo en `glitchhunter_ai/core/pipeline.py` dentro de `process_product()`.

---

## ✅ Lista de Chequeo antes de enviar tu Pull Request

* [ ] El código sigue la guía de estilo PEP 8 y tipos de Pydantic v2.
* [ ] No se incluyeron archivos de cache (`__pycache__`, `.DS_Store`) ni secretos (`.env`).
* [ ] Se ejecutaron todas las pruebas automatizadas con éxito: `python3 run.py test`.
* [ ] Se añadieron pruebas unitarias para las nuevas funcionalidades.

¡Cualquier aporte, desde corregir un typo en la documentación hasta añadir un nuevo retailer masivo, es sumamente bienvenido!
