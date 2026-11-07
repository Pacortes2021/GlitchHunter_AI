"""
Curador de las mejores ofertas ("🏆 Lo mejor").

El canal recibe miles de publicaciones al día; casi todas son rebajas comunes. El curador revisa las
publicaciones de un período (vigentes o ya pasadas), les pone puntaje y arma un ranking corto:

- descuento y ahorro en pesos (un -60% sobre $400.000 vale más que un -70% sobre $8.000);
- evidencia de que es excepcional: error de precio verificado, "falta un cero", precio más bajo
  que hemos visto para ese producto o muy bajo su precio habitual en nuestro historial;
- votos 👍/👎 de la comunidad;
- variedad: sin productos repetidos (tallas/colores) y como máximo N por tienda.

Además guarda cada día su top (tabla best_picks), así las mejores ofertas quedan registradas aunque
después expiren o el canal siga creciendo.
"""

import json
import logging
import math
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Santiago")   # el "día" del top es el día en Chile
except Exception:  # sin base de zonas horarias: UTC-3 fijo
    LOCAL_TZ = timezone(timedelta(hours=-3))

logger = logging.getLogger("GlitchHunter.Best")

MIN_SAVINGS_CLP = 3000.0
PER_STORE_DEFAULT = 3
SNAPSHOT_SIZE = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _title_family(title: str) -> str:
    """Título sin tallas ni colores, para no repetir el mismo producto en variantes."""
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((?:[^)]{0,12})\)", " ", t)                      # (MT), (XL)
    t = re.sub(r"\b(?:talla|t\.)\s*\w+|\b(?:xxs|xs|s|m|l|xl|xxl|\d{2})\b", " ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))[:60]


def score_post(post: Dict[str, Any], hist: Optional[Dict[str, float]] = None,
               votes: Optional[Dict[str, int]] = None) -> Tuple[float, List[str]]:
    """Puntaje de una publicación del canal y los motivos legibles que lo explican."""
    payload = post.get("payload") or {}
    product = payload.get("product") or payload
    verdict = payload.get("verdict") or {}
    best = float(post.get("best_price") or 0)
    discount = float(post.get("discount_pct") or verdict.get("discount_percentage") or 0)
    normal = float(product.get("normal_price") or 0)
    savings = float(verdict.get("absolute_savings_clp") or payload.get("savings_clp") or max(0.0, normal - best))
    reasons: List[str] = []

    score = discount * 0.8
    if savings > 5000:
        score += min(25.0, 10.0 * math.log10(savings / 5000.0))
    if savings >= 100_000:
        reasons.append(f"Ahorras ${savings:,.0f}".replace(",", "."))
    if post.get("kind") == "glitch":
        score += 15.0
        reasons.append("Error de precio verificado")
    if verdict.get("is_missing_zero"):
        score += 10.0
        reasons.append("Parece que le falta un cero")
    if payload.get("bulk_price_count"):
        score += 4.0

    if hist and hist.get("n", 0) >= 5 and best > 0:
        low, avg = float(hist.get("min") or 0), float(hist.get("avg") or 0)
        if low > 0 and best <= low * 1.001:
            score += 10.0
            reasons.append("El precio más bajo que hemos visto")
        if avg > 0 and best < avg:
            drop = (avg - best) / avg
            if drop >= 0.2:
                score += 8.0 * min(1.0, drop / 0.5)
                reasons.append(f"{drop * 100:.0f}% bajo su precio habitual")

    if votes:
        up, down = int(votes.get("up", 0)), int(votes.get("down", 0))
        score += 5.0 * min(up, 4) - 8.0 * min(down, 4)
        if up >= 2:
            reasons.append(f"{up} votos 👍")
    if not reasons:
        reasons.append(f"-{discount:.0f}% en la tienda")
    return round(score, 1), reasons


class BestDealsCurator:
    def __init__(self, db: Any, clock=_now):
        self.db = db
        self.clock = clock
        self._cache: Dict[Tuple, Tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self.db.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS best_picks (
                    day TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    feed_id INTEGER NOT NULL,
                    score REAL NOT NULL,
                    reasons TEXT,
                    PRIMARY KEY (day, rank)
                );
            """)
            conn.commit()

    # --- datos ---------------------------------------------------------------------
    def _candidates(self, since_iso: str, until_iso: Optional[str], only_active: bool) -> List[Dict[str, Any]]:
        where = ["received_at >= ?", "COALESCE(expired_reason, '') != 'withdrawn'"]
        params: List[Any] = [since_iso]
        if until_iso:
            where.append("received_at < ?")
            params.append(until_iso)
        if only_active:
            where.append("status = 'active'")
        with self.db.get_connection() as conn:
            rows = conn.execute(f"""
                SELECT id, kind, product_id, store_id, best_price, discount_pct, received_at, payload,
                       status, expired_reason, status_changed_at, price_now, title_key
                FROM offer_feed WHERE {' AND '.join(where)};
            """, params).fetchall()
        posts = []
        for r in rows:
            post = dict(r)
            try:
                post["payload"] = json.loads(post["payload"])
            except (TypeError, ValueError):
                continue
            posts.append(post)
        return posts

    def _history(self, product_ids: List[str]) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        ids = list(dict.fromkeys(product_ids))
        with self.db.get_connection() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in conn.execute(f"""
                    SELECT product_id, MIN(price) AS mn, AVG(price) AS av, COUNT(*) AS n
                    FROM price_history WHERE product_id IN ({','.join('?' for _ in chunk)}) AND price > 0
                    GROUP BY product_id;
                """, chunk).fetchall():
                    out[r["product_id"]] = {"min": r["mn"], "avg": r["av"], "n": r["n"]}
        return out

    def _votes(self, posts: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
        ids = []
        for p in posts:
            ids.append(p["product_id"])
            alert_id = (p.get("payload") or {}).get("alert_id")
            if alert_id:
                ids.append(alert_id)
        try:
            return self.db.get_feedback_counts(ids)
        except Exception:
            return {}

    # --- ranking -------------------------------------------------------------------
    def rank(self, days: float = 7.0, limit: int = 20, per_store: int = PER_STORE_DEFAULT,
             only_active: bool = False, until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Las mejores publicaciones de los últimos `days` días (cada una con 'best_score' y 'best_reasons')."""
        end = until or self.clock()
        key = (round(days, 3), limit, per_store, only_active, until.isoformat() if until else None)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < 60 and until is None:
                return hit[1]
        since = (end - timedelta(days=days)).isoformat()
        posts = self._candidates(since, end.isoformat() if until else None, only_active)
        from glitchhunter_ai.core.pipeline import SERVICE_RE
        posts = [p for p in posts if self._savings(p) >= MIN_SAVINGS_CLP and not SERVICE_RE.search(self._title(p))]
        hist = self._history([p["product_id"] for p in posts])
        votes = self._votes(posts)
        scored = []
        for p in posts:
            alert_id = (p.get("payload") or {}).get("alert_id")
            v = votes.get(alert_id) or votes.get(p["product_id"])
            s, reasons = score_post(p, hist.get(p["product_id"]), v)
            p["best_score"], p["best_reasons"] = s, reasons
            scored.append(p)
        scored.sort(key=lambda p: (-p["best_score"], -float(p.get("discount_pct") or 0)))

        picked: List[Dict[str, Any]] = []
        seen_products, seen_families = set(), set()
        per_store_count: Dict[str, int] = {}
        for p in scored:
            payload = p.get("payload") or {}
            title = str((payload.get("product") or payload).get("title") or "")
            family = f"{p['store_id']}|{_title_family(title)}"
            if p["product_id"] in seen_products or family in seen_families:
                continue
            if per_store_count.get(p["store_id"], 0) >= per_store:
                continue
            seen_products.add(p["product_id"])
            seen_families.add(family)
            per_store_count[p["store_id"]] = per_store_count.get(p["store_id"], 0) + 1
            picked.append(p)
            if len(picked) >= limit:
                break
        if until is None:
            with self._lock:
                self._cache[key] = (time.monotonic(), picked)
        return picked

    @staticmethod
    def _title(post: Dict[str, Any]) -> str:
        payload = post.get("payload") or {}
        return str((payload.get("product") or payload).get("title") or "")

    @staticmethod
    def _savings(post: Dict[str, Any]) -> float:
        payload = post.get("payload") or {}
        product = payload.get("product") or payload
        verdict = payload.get("verdict") or {}
        best = float(post.get("best_price") or 0)
        normal = float(product.get("normal_price") or 0)
        return float(verdict.get("absolute_savings_clp") or payload.get("savings_clp") or max(0.0, normal - best))

    # --- registro diario -------------------------------------------------------------
    def snapshot_day(self, day: Optional[datetime] = None) -> int:
        """Guarda el top del día (hora de Chile) hasta `day` en best_picks, reemplazando el anterior de ese día."""
        end = (day or self.clock()).astimezone(LOCAL_TZ)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        picks = self.rank(days=max((end - start).total_seconds() / 86400, 1e-6), limit=SNAPSHOT_SIZE,
                          until=end.astimezone(timezone.utc))
        label = start.date().isoformat()
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM best_picks WHERE day = ?;", (label,))
            conn.executemany(
                "INSERT INTO best_picks (day, rank, feed_id, score, reasons) VALUES (?, ?, ?, ?, ?);",
                [(label, i + 1, p["id"], p["best_score"], json.dumps(p["best_reasons"], ensure_ascii=False))
                 for i, p in enumerate(picks)],
            )
            conn.commit()
        return len(picks)

    def history(self, days: int = 14, per_day: int = 5) -> List[Dict[str, Any]]:
        """Top guardado de cada día (más reciente primero): [{day, posts: [...]}]."""
        since = (self.clock().astimezone(LOCAL_TZ) - timedelta(days=days)).date().isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT b.day, b.rank, b.score, b.reasons, f.id, f.kind, f.product_id, f.store_id, f.best_price,
                       f.discount_pct, f.received_at, f.payload, f.status, f.expired_reason,
                       f.status_changed_at, f.price_now
                FROM best_picks b JOIN offer_feed f ON f.id = b.feed_id
                WHERE b.day >= ? AND b.rank <= ? AND COALESCE(f.expired_reason, '') != 'withdrawn'
                ORDER BY b.day DESC, b.rank ASC;
            """, (since, per_day)).fetchall()
        days_out: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            post = dict(r)
            post["payload"] = json.loads(post["payload"])
            post["best_score"] = post.pop("score")
            post["best_reasons"] = json.loads(post.pop("reasons") or "[]")
            days_out.setdefault(post.pop("day"), []).append(post)
        return [{"day": d, "posts": ps} for d, ps in days_out.items()]

    def backfill(self, days: int = 7) -> int:
        """Guarda el top de los días anteriores que aún no lo tienen (p. ej. al instalar el curador)."""
        today = self.clock().astimezone(LOCAL_TZ).date()
        with self.db.get_connection() as conn:
            have = {r[0] for r in conn.execute("SELECT DISTINCT day FROM best_picks;").fetchall()}
        filled = 0
        for back in range(1, days + 1):
            d = today - timedelta(days=back)
            if d.isoformat() not in have:
                end = datetime.combine(d, datetime.max.time(), tzinfo=LOCAL_TZ)
                if self.snapshot_day(end):
                    filled += 1
        return filled

    # --- agente en segundo plano -------------------------------------------------------
    def start(self, every_seconds: float = 3600.0) -> "BestDealsCurator":
        def loop():
            last_day = None
            while not self._stop.wait(5.0 if last_day is None else every_seconds):
                try:
                    now = self.clock()
                    today = now.astimezone(LOCAL_TZ).date()
                    if last_day is None:
                        filled = self.backfill()
                        if filled:
                            logger.info("🏆 [Lo mejor] Top guardado para %d día(s) anteriores.", filled)
                    elif last_day != today:
                        # Cierra el día anterior con todo lo que llegó hasta medianoche
                        self.snapshot_day(datetime.combine(last_day, datetime.max.time(), tzinfo=LOCAL_TZ))
                    n = self.snapshot_day(now)
                    last_day = today
                    logger.info("🏆 [Lo mejor] Top del día actualizado (%d publicaciones).", n)
                except Exception as exc:
                    logger.warning("⚠️ [Lo mejor] No se pudo actualizar el top del día: %s", exc)
                    last_day = last_day or self.clock().astimezone(LOCAL_TZ).date()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=loop, name="curador-mejores", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if getattr(self, "_stop", None):
            self._stop.set()
