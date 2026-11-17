"""
Radar y Benchmark contra canales comunitarios de ofertas en Chile (Descuentos Rata / Odum).

Permite:
1. Capturar en tiempo casi real las ofertas virales publicadas en estos canales (incluidas
   tiendas de nicho o promociones con cupones/tarjetas que no siempre están en listas directas).
2. Resolver la URL real de la tienda y cruzarla contra nuestro historial para medir la latencia:
   ¿lo detectamos nosotros antes (`ahead`), llegó después (`behind`), o lo capturamos vía canal (`captured`)?
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html as html_lib
import logging
import re
import threading
import unicodedata
from typing import Any, Dict, List, Optional
import urllib.parse

from bs4 import BeautifulSoup

from glitchhunter_ai.core.categories import ACCESSORY_CATEGORY, classify_category
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.direct_reader import STORE_NAMES as DIRECT_STORE_NAMES
from glitchhunter_ai.scrapers.product_ids import canonical_product_id
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.scrapers.social_radar")

RATA_HOME_URL = "https://descuentosrata.com/"
ODUM_POSTS_URL = "https://odum.cl/whats-new/posts/"

RATA_STORE_MAP: Dict[str, str] = {
    "paris": "paris",
    "falabella": "falabella",
    "ripley": "ripley",
    "lider": "lider",
    "mercado libre": "mercadolibre",
    "mercadolibre": "mercadolibre",
    "sodimac": "sodimac",
    "jumbo": "jumbo",
    "santa isabel": "santaisabel",
    "abc": "abc",
    "abcdin": "abc",
    "la polar": "abc",
    "pc factory": "pcfactory",
    "pcfactory": "pcfactory",
    "dbs": "dbs",
    "lindt": "lindt",
    "blanik": "blanik",
    "nike": "nike",
    "adidas": "adidas",
    "caterpillar": "caterpillar",
    "lippi": "lippi",
    "columbia": "columbia",
    "cannon home": "cannonhome",
    "kitchen center": "kitchencenter",
    "oster": "oster",
    "electrolux": "electrolux",
    "fensa": "fensa",
    "sony": "sony",
    "motorola": "motorola",
    "hp": "hp",
    "lg": "lg",
    "samsung": "samsung",
    "lenovo": "lenovo",
    "aliexpress": "aliexpress",
    "hites": "hites",
    "farmacias ahumada": "farmaciasahumada",
    "salcobrand": "salcobrand",
    "cruz verde": "cruzverde",
    "travel tienda": "traveltienda",
    "tienda lego": "tiendalego",
}

_URL_DOMAIN_TO_STORE: tuple = (
    ("falabella.com", "falabella"),
    ("creators.falabella.com", "falabella"),
    ("paris.cl", "paris"),
    ("simple.ripley.cl", "ripley"),
    ("ripley.cl", "ripley"),
    ("lider.cl", "lider"),
    ("mercadolibre.cl", "mercadolibre"),
    ("mercadolibre.com", "mercadolibre"),
    ("sodimac.cl", "sodimac"),
    ("jumbo.cl", "jumbo"),
    ("santaisabel.cl", "santaisabel"),
    ("abc.cl", "abc"),
    ("pcfactory.cl", "pcfactory"),
    ("dbs.cl", "dbs"),
    ("lindt.cl", "lindt"),
    ("blanik.cl", "blanik"),
    ("nike.cl", "nike"),
    ("adidas.cl", "adidas"),
    ("shopcaterpillar.cl", "caterpillar"),
    ("lippioutdoor.com", "lippi"),
    ("columbiachile.cl", "columbia"),
    ("cannonhome.cl", "cannonhome"),
    ("kitchencenter.cl", "kitchencenter"),
    ("oster.cl", "oster"),
    ("electrolux.cl", "electrolux"),
    ("tiendafensa.cl", "fensa"),
    ("store.sony.cl", "sony"),
    ("motorola.cl", "motorola"),
    ("hp.com", "hp"),
    ("lg.com", "lg"),
    ("samsung.com", "samsung"),
    ("hites.com", "hites"),
    ("aliexpress.com", "aliexpress"),
)

_IGNORED_LINK_DOMAINS = (
    "rata.link",
    "descuentosrata.com",
    "odum.cl",
    "googletagmanager.com",
    "cloudflareinsights.com",
    "walmartimages.cl",
    "google-analytics.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "open.spotify.com",
    "t.me",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "schema.org",
    "w3.org",
    "play.google.com",
    "appgallery.huawei.com",
    "ecomm.cencosud.com",
)
_STATIC_EXT_RE = re.compile(r"\.(?:js|css|png|jpg|jpeg|webp|svg|gif|ico|woff2?)(?:\?|$)", re.IGNORECASE)
_AGE_RE = re.compile(r"\b(\d+)\s*(min|m|h|hr|hrs|d|s)\b", re.IGNORECASE)
_PRICE_CLP_RE = re.compile(r"\$\s*(\d+(?:\.\d{3})+|\d{4,})")
_ODUM_DEAL_PREFIX_RE = re.compile(r"\b(?:ofertas?(?:\s+destacadas)?|mercado\s+libre)\b", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalize_store_id(store_name: str) -> str:
    clean = _strip_accents(store_name)
    if clean in RATA_STORE_MAP:
        return RATA_STORE_MAP[clean]
    slug = re.sub(r"[^a-z0-9]", "", clean)
    return slug or "retail"


def _store_id_from_url(url: str, fallback: str = "retail") -> str:
    low = (url or "").lower()
    for domain, sid in _URL_DOMAIN_TO_STORE:
        if domain in low:
            return sid
    return fallback


def _parse_digits_price(text: Any) -> float:
    digits = re.sub(r"[^\d]", "", str(text or ""))
    if not digits:
        return 0.0
    try:
        return float(int(digits))
    except ValueError:
        return 0.0


def parse_relative_age(age_str: str, now: Optional[datetime] = None) -> datetime:
    """Convierte textos relativos ('6min', '1h', '2d') a datetime UTC."""
    ref = now.astimezone(timezone.utc) if (now and now.tzinfo) else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
    m = _AGE_RE.search(age_str or "")
    if not m:
        return ref
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("s"):
        return ref - timedelta(seconds=val)
    if unit.startswith("m"):
        return ref - timedelta(minutes=val)
    if unit.startswith("h"):
        return ref - timedelta(hours=val)
    if unit.startswith("d"):
        return ref - timedelta(days=val)
    return ref


def parse_descuentos_rata_html(html: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Extrae tarjetas de ofertas recientes desde https://descuentosrata.com/."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []
    seen_ids: set = set()
    ref_now = now or datetime.now(timezone.utc)

    for anchor in soup.select("a[href*='/oferta/']"):
        href = str(anchor.get("href") or "").strip()
        m_id = re.search(r"/oferta/(\d+)(?:/|$)", href)
        if not m_id:
            continue
        oferta_id = m_id.group(1)
        if oferta_id in seen_ids:
            continue
        seen_ids.add(oferta_id)

        h6 = anchor.select_one("h6")
        store_name = " ".join(h6.get_text(" ").split()) if h6 else ""
        store_id = _normalize_store_id(store_name)

        age_text = ""
        for sm in anchor.select("small"):
            txt = " ".join(sm.get_text(" ").split())
            if _AGE_RE.search(txt) and "$" not in txt:
                age_text = txt
                break
        published_dt = parse_relative_age(age_text, now=ref_now)

        title_el = anchor.select_one(".line-clamp--3, .font-weight-bold.line-clamp")
        title = " ".join(title_el.get_text(" ").split()) if title_el else ""
        if not title:
            continue

        curr_el = anchor.select_one("span.rata-font-poppins")
        current_price = _parse_digits_price(curr_el.get_text(" ") if curr_el else "")
        if current_price <= 0.0:
            price_match = _PRICE_CLP_RE.search(anchor.get_text(" "))
            current_price = _parse_digits_price(price_match.group(1)) if price_match else 0.0
        if current_price <= 0.0:
            continue

        prev_el = anchor.select_one(".oferta-precio-anterior, [aria-label='Precio anterior']")
        normal_price = _parse_digits_price(prev_el.get_text(" ") if prev_el else "")
        if normal_price <= current_price:
            normal_price = 0.0

        offer_page_url = href if href.startswith("http") else f"https://descuentosrata.com{href}"
        items.append({
            "external_id": f"rata-{oferta_id}",
            "oferta_id": oferta_id,
            "channel": "descuentos_rata",
            "store_name": store_name or DIRECT_STORE_NAMES.get(store_id, store_id.title()),
            "store_id": store_id,
            "title": title,
            "current_price": current_price,
            "normal_price": normal_price,
            "external_published_at": published_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "offer_page_url": offer_page_url,
            "go_url": f"https://rata.link/o/{oferta_id}",
        })
    return items


def _is_valid_store_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    if _STATIC_EXT_RE.search(low):
        return False
    host = urllib.parse.urlparse(low).netloc
    return not any(ign in host for ign in _IGNORED_LINK_DOMAINS)


def extract_store_url_from_rata_link(html: str, final_url: str = "") -> Optional[str]:
    """Obtiene la URL real de destino de una página puente rata.link/o/{id}."""
    if final_url and _is_valid_store_url(final_url):
        return final_url
    if not html:
        return None
    for raw in re.findall(r'https?://[^\s"\'<>\\]+', html):
        cand = html_lib.unescape(raw).rstrip(").,;\"'")
        if _is_valid_store_url(cand):
            return cand
    return None


def parse_odum_html(html: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Extrae publicaciones de ofertas recientes desde Odum.cl."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict[str, Any]] = []
    seen_ids: set = set()
    ref_now = now.astimezone(timezone.utc) if (now and now.tzinfo) else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))

    for row in soup.select(".structItem"):
        title_box = row.select_one(".structItem-title")
        if not title_box:
            continue
        link = title_box.select_one("a[href*='/tema/']")
        if not link:
            continue
        href = str(link.get("href") or "").strip()
        m_tid = re.search(r"\.(\d+)/?$", href.split("#")[0])
        if not m_tid:
            continue
        tid = m_tid.group(1)
        if tid in seen_ids:
            continue

        prefix_texts = [sp.get_text(" ", strip=True) for sp in title_box.select("span")]
        prefix_joined = " ".join(prefix_texts)
        full_box_text = " ".join(title_box.get_text(" ").split())
        if not (_ODUM_DEAL_PREFIX_RE.search(prefix_joined) or _ODUM_DEAL_PREFIX_RE.search(full_box_text)):
            continue
        if any(bad in _strip_accents(prefix_joined) for bad in ("cupon", "vendo", "compro", "reclamo", "duda", "off topic", "comparto")):
            continue

        title = " ".join(link.get_text(" ").split())
        if not title:
            continue
        m_price = _PRICE_CLP_RE.search(title)
        if not m_price:
            continue
        price = _parse_digits_price(m_price.group(1))
        if price < 2000.0:
            continue

        time_el = row.select_one("time[datetime]")
        published_str = (
            str(time_el.get("datetime"))
            if time_el and time_el.get("datetime")
            else ref_now.strftime("%Y-%m-%d %H:%M:%S")
        )
        store_id = "mercadolibre" if "mercado libre" in _strip_accents(prefix_joined) else "odum"
        thread_url = href if href.startswith("http") else f"https://odum.cl{href}"
        seen_ids.add(tid)
        items.append({
            "external_id": f"odum-{tid}",
            "thread_id": tid,
            "channel": "odum",
            "store_name": "Mercado Libre" if store_id == "mercadolibre" else "Odum",
            "store_id": store_id,
            "title": title,
            "current_price": price,
            "normal_price": 0.0,
            "external_published_at": published_str,
            "thread_url": thread_url,
            "go_url": thread_url,
        })
    return items


class SocialChannelRadar:
    """Captura ofertas virales de Descuentos Rata / Odum e incorpora métricas de latencia."""

    def __init__(self, client: Any = None, timeout: float = 8.0) -> None:
        self.client = client or default_stealth_client
        self.timeout = timeout
        self._lock = threading.Lock()
        self._seen_external_ids: set = set()
        self._last_fetch_at: float = 0.0

    def _remember_seen(self, ext_id: str) -> bool:
        with self._lock:
            if ext_id in self._seen_external_ids:
                return False
            if len(self._seen_external_ids) > 2000:
                self._seen_external_ids.clear()
            self._seen_external_ids.add(ext_id)
            return True

    def _resolve_rata_url(self, deal: Dict[str, Any]) -> Optional[str]:
        go_url = deal.get("go_url")
        if not go_url:
            return None
        try:
            resp = self.client.get(go_url, timeout=self.timeout)
            if getattr(resp, "status_code", 0) == 200:
                return extract_store_url_from_rata_link(resp.text or "", str(getattr(resp, "url", "") or ""))
        except Exception as exc:
            logger.debug("No se pudo resolver %s: %s", go_url, exc)
        return None

    def _resolve_odum_url(self, deal: Dict[str, Any]) -> Optional[str]:
        thread_url = deal.get("thread_url")
        if not thread_url:
            return None
        try:
            resp = self.client.get(thread_url, timeout=self.timeout)
            if getattr(resp, "status_code", 0) != 200:
                return None
            soup = BeautifulSoup(resp.text or "", "html.parser")
            post = soup.select_one(".message-body, article.message")
            if not post:
                return None
            for a in post.select("a[href^='http']"):
                cand = str(a.get("href") or "").strip()
                if _is_valid_store_url(cand):
                    return cand
        except Exception as exc:
            logger.debug("No se pudo resolver hilo Odum %s: %s", thread_url, exc)
        return None

    def fetch_recent_social_deals(self, max_resolve: int = 12) -> List[Dict[str, Any]]:
        """Descarga Descuentos Rata y Odum y resuelve hasta `max_resolve` enlaces nuevos."""
        deals: List[Dict[str, Any]] = []
        resolved = 0

        try:
            r_rata = self.client.get(RATA_HOME_URL, timeout=self.timeout)
            if getattr(r_rata, "status_code", 0) == 200:
                for d in parse_descuentos_rata_html(r_rata.text or ""):
                    if not self._remember_seen(d["external_id"]):
                        continue
                    store_url = None
                    if resolved < max_resolve:
                        store_url = self._resolve_rata_url(d)
                        resolved += 1
                    d["store_url"] = store_url or d.get("offer_page_url") or d.get("go_url")
                    if store_url:
                        d["store_id"] = _store_id_from_url(store_url, d["store_id"])
                        d["product_id"] = canonical_product_id(d["store_id"], store_url)
                    else:
                        d["product_id"] = None
                    deals.append(d)
        except Exception as exc:
            logger.debug("Lectura de Descuentos Rata omitida: %s", exc)

        try:
            r_odum = self.client.get(ODUM_POSTS_URL, timeout=self.timeout)
            if getattr(r_odum, "status_code", 0) == 200:
                for d in parse_odum_html(r_odum.text or ""):
                    if not self._remember_seen(d["external_id"]):
                        continue
                    store_url = None
                    if resolved < max_resolve:
                        store_url = self._resolve_odum_url(d)
                        resolved += 1
                    d["store_url"] = store_url or d.get("thread_url")
                    if store_url:
                        d["store_id"] = _store_id_from_url(store_url, d["store_id"])
                        if d["store_id"] != "odum":
                            d["store_name"] = DIRECT_STORE_NAMES.get(d["store_id"], d["store_id"].title())
                        d["product_id"] = canonical_product_id(d["store_id"], store_url)
                    else:
                        d["product_id"] = None
                    deals.append(d)
        except Exception as exc:
            logger.debug("Lectura de Odum omitida: %s", exc)

        return deals

    def ingest_social_deals(self, pipeline: Any, deals: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Registra métricas de latencia en `social_benchmark` e incorpora ofertas válidas
        al catálogo y canal de ofertas cuando aún no estaban publicadas.
        """
        from glitchhunter_ai.core.pipeline import REFURBISHED_RE, SERVICE_RE

        stats = {"evaluated": 0, "ahead": 0, "behind": 0, "captured": 0, "published": 0}
        if not deals:
            return stats

        for deal in deals:
            ext_id = str(deal.get("external_id") or "").strip()
            title = str(deal.get("title") or "").strip()
            store_id = str(deal.get("store_id") or "retail").strip().lower()
            deal_price = float(deal.get("current_price") or 0.0)
            normal_raw = float(deal.get("normal_price") or 0.0)
            if not ext_id or not title or deal_price <= 0.0:
                continue

            store_url = str(deal.get("store_url") or deal.get("offer_page_url") or deal.get("thread_url") or "").strip()
            prod_id = deal.get("product_id") or (canonical_product_id(store_id, store_url) if store_url else None)
            if not prod_id:
                prod_id = f"{store_id}-{ext_id}"

            bench = pipeline.db.evaluate_social_deal(
                external_id=ext_id,
                channel=str(deal.get("channel") or "descuentos_rata"),
                store_id=store_id,
                product_id=prod_id,
                title=title,
                deal_price=deal_price,
                normal_price=normal_raw if normal_raw > deal_price else None,
                store_url=store_url,
                external_published_at=deal.get("external_published_at"),
            )
            stats["evaluated"] += 1
            outcome = bench.get("outcome")
            if outcome in ("ahead", "behind", "captured"):
                stats[outcome] += 1

            if bench.get("product_id") and (
                not deal.get("product_id") or prod_id.startswith(f"{store_id}-rata-") or prod_id.startswith(f"{store_id}-odum-")
            ):
                prod_id = str(bench["product_id"])

            existing = None
            get_prod = getattr(pipeline.db, "get_product", None)
            if callable(get_prod):
                existing = get_prod(prod_id)
            preserved_normal = float(existing.get("normal_price") or 0.0) if isinstance(existing, dict) else 0.0
            eff_normal = max(normal_raw if normal_raw > deal_price else 0.0, preserved_normal if preserved_normal > deal_price else 0.0)

            cat = classify_category(title)
            item = ProductItem(
                id=prod_id,
                store_id=store_id,
                store_name=str(deal.get("store_name") or DIRECT_STORE_NAMES.get(store_id, store_id.title())),
                title=title,
                category=cat,
                current_price=deal_price,
                normal_price=eff_normal,
                url=store_url or f"https://www.{store_id}.cl/",
                in_stock=True,
                brand=deal.get("brand"),
                seller=deal.get("seller"),
                source=str(deal.get("channel") or "social"),
            )
            pipeline.db.upsert_product(item)

            if not store_url:
                continue
            if SERVICE_RE.search(title) or REFURBISHED_RE.search(title):
                continue
            if cat == ACCESSORY_CATEGORY:
                continue
            if pipeline.is_generic_marketplace_trap(title, item.brand, item.seller):
                continue

            if eff_normal > deal_price:
                disc_pct = round(((eff_normal - deal_price) / eff_normal) * 100.0, 1)
                sav_clp = round(eff_normal - deal_price, 0)
            else:
                disc_pct = 0.0
                sav_clp = 0.0

            ch_label = "Descuentos Rata" if str(deal.get("channel")) == "descuentos_rata" else "Odum"
            lead_m = bench.get("lead_minutes")
            if outcome == "ahead" and lead_m is not None and float(lead_m) >= 1.0:
                ev_label = f"⚡ Detectado {int(round(float(lead_m)))} min antes que {ch_label}"
            else:
                ev_label = f"📢 Destacado en {ch_label}"

            row = pipeline._build_deal_payload(
                item,
                disc_pct,
                sav_clp,
                extra={
                    "verified_drop": True,
                    "evidence_source": str(deal.get("channel") or "social"),
                    "evidence_label": ev_label,
                    "social_lead_minutes": lead_m,
                    "social_outcome": outcome,
                },
            )
            post_id = pipeline.db.publish_feed_offer(
                kind="deal",
                product_id=item.id,
                store_id=item.store_id,
                best_price=item.best_price,
                discount_pct=disc_pct,
                payload=row,
            )
            if post_id:
                stats["published"] += 1

        return stats

    def scan_and_compare(self, pipeline: Any, max_resolve: int = 12) -> Dict[str, int]:
        """Obtiene ofertas recientes de canales sociales e ingesta + mide latencia."""
        deals = self.fetch_recent_social_deals(max_resolve=max_resolve)
        return self.ingest_social_deals(pipeline, deals)
