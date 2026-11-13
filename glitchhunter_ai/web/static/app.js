/**
 * GlitchHunter AI — Canal de ofertas (frontend).
 *
 * Funciona como un canal de Telegram:
 *  - El historial se carga UNA vez y cada publicación se dibuja UNA sola vez (un nodo DOM por feed_id).
 *  - Después solo se piden las publicaciones nuevas (/api/feed?after_id=último) y se agregan arriba.
 *  - Los filtros y el orden solo muestran/ocultan o reordenan los nodos existentes: nunca se vuelven a crear.
 *  - Precio y hora de cada publicación quedan fijos; solo el texto "hace N min" se actualiza.
 */
(() => {
  'use strict';

  // ---------------------------------------------------------------------------
  // Configuración
  // ---------------------------------------------------------------------------
  const CORE_STORE_COLORS = {
    falabella: '#3f8f3f',
    ripley: '#7e22ce',
    paris: '#0284c7',
    lider: '#2563eb',
    mercadolibre: '#ca8a04',
    pcfactory: '#dc2626',
    sodimac: '#f97316',
    abc: '#e11d48',
    jumbo: '#16a34a',
    hp: '#0096d6',
    winpy: '#f59e0b',
    spdigital: '#1e3a8a',
    santaisabel: '#dc2626',
    nike: '#111111',
    sony: '#111827',
    solotodo: '#0d9488'
  };
  const STORE_PALETTE = ['#0f766e', '#1d4ed8', '#7c3aed', '#be185d', '#b45309', '#4d7c0f', '#0369a1', '#9f1239', '#0e7490', '#c2410c'];
  const DEFAULT_STORE_COLOR = '#64748b';

  // Directorio vivo: pre-poblado con retailers principales y enriquecido dinámicamente vía /api/status.
  const STORES = {
    falabella: { name: 'Falabella', color: CORE_STORE_COLORS.falabella },
    ripley: { name: 'Ripley', color: CORE_STORE_COLORS.ripley },
    paris: { name: 'Paris', color: CORE_STORE_COLORS.paris },
    lider: { name: 'Líder', color: CORE_STORE_COLORS.lider },
    mercadolibre: { name: 'Mercado Libre', color: CORE_STORE_COLORS.mercadolibre },
    pcfactory: { name: 'PC Factory', color: CORE_STORE_COLORS.pcfactory },
    sodimac: { name: 'Sodimac', color: CORE_STORE_COLORS.sodimac },
    abc: { name: 'ABC', color: CORE_STORE_COLORS.abc },
    jumbo: { name: 'Jumbo', color: CORE_STORE_COLORS.jumbo },
    hp: { name: 'HP', color: CORE_STORE_COLORS.hp },
    winpy: { name: 'Winpy', color: CORE_STORE_COLORS.winpy },
    spdigital: { name: 'SP Digital', color: CORE_STORE_COLORS.spdigital },
    santaisabel: { name: 'Santa Isabel', color: CORE_STORE_COLORS.santaisabel },
    solotodo: { name: 'SoloTodo', color: CORE_STORE_COLORS.solotodo }
  };

  function storeColor(id) {
    if (!id) return DEFAULT_STORE_COLOR;
    if (STORES[id] && STORES[id].color) return STORES[id].color;
    if (CORE_STORE_COLORS[id]) return CORE_STORE_COLORS[id];
    let h = 0;
    for (let i = 0; i < id.length; i += 1) h = ((h << 5) - h + id.charCodeAt(i)) | 0;
    return STORE_PALETTE[Math.abs(h) % STORE_PALETTE.length];
  }

  async function loadStoreDirectory() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const data = await res.json();
      if (!Array.isArray(data.stores)) return;
      for (const s of data.stores) {
        if (!s || !s.id) continue;
        STORES[s.id] = {
          name: s.name || s.id,
          color: (STORES[s.id] && STORES[s.id].color) || storeColor(s.id)
        };
      }
      if (state.loaded) renderFilterChips();
    } catch { /* fallback al directorio base */ }
  }

  const INITIAL_LIMIT = 500;
  const MATCHING_LIMIT = 1000;   // publicaciones que se traen al aplicar un filtro
  const PAGE_STEP = 50;          // ofertas visibles por página en el feed
  const POLL_MS = 30000;
  const WS_PING_MS = 25000;
  const NEW_HIGHLIGHT_MS = 8000;
  const MAX_CATEGORY_CHIPS = 10;

  const DEFAULT_FILTERS = Object.freeze({
    kind: 'all',        // 'all' | 'glitch' | 'deal'
    minDiscount: 0,     // 0 | 50 | 65 | 80
    maxPrice: 0,        // 0 = sin tope | 50000 | 200000 | 500000
    stores: [],         // ids de tienda seleccionados (vacío = todas)
    category: '',       // categoría normalizada ('' = todas)
    search: '',
    verifiedOnly: false,
    watchlistOnly: false,
    hideExpired: true,  // por defecto ocultar ofertas que subieron de precio o se agotaron
    sortBy: 'recent'    // 'recent' | 'discount' | 'savings'
  });
  const STATUS_POLL_MS = 60000;

  // ---------------------------------------------------------------------------
  // Almacenamiento local seguro (puede no existir o lanzar en modo privado)
  // ---------------------------------------------------------------------------
  const storage = {
    get(key) {
      try { return window.localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, value); } catch { /* bloqueado */ }
    },
    remove(key) {
      try { window.localStorage.removeItem(key); } catch { /* bloqueado */ }
    },
    getJSON(key, fallback) {
      try {
        const raw = window.localStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
      } catch { return fallback; }
    },
    setJSON(key, value) {
      try { window.localStorage.setItem(key, JSON.stringify(value)); } catch { /* bloqueado */ }
    }
  };

  function loadFilters() {
    const saved = storage.getJSON('glitchhunter_filters_v3', {});
    const f = { ...DEFAULT_FILTERS, stores: [] };
    if (saved && typeof saved === 'object') {
      if (['all', 'glitch', 'deal'].includes(saved.kind)) f.kind = saved.kind;
      if ([0, 50, 65, 80].includes(saved.minDiscount)) f.minDiscount = saved.minDiscount;
      if ([0, 50000, 200000, 500000].includes(saved.maxPrice)) f.maxPrice = saved.maxPrice;
      if (Array.isArray(saved.stores)) f.stores = saved.stores.filter(s => typeof s === 'string').slice(0, 20);
      if (typeof saved.category === 'string') f.category = saved.category.slice(0, 60);
      if (typeof saved.verifiedOnly === 'boolean') f.verifiedOnly = saved.verifiedOnly;
      if (typeof saved.watchlistOnly === 'boolean') f.watchlistOnly = saved.watchlistOnly;
      if (typeof saved.hideExpired === 'boolean') f.hideExpired = saved.hideExpired;
      if (['recent', 'discount', 'savings'].includes(saved.sortBy)) f.sortBy = saved.sortBy;
    }
    return f; // la búsqueda no se recuerda a propósito
  }

  function saveFilters() {
    const { search, ...rest } = state.filters;
    storage.setJSON('glitchhunter_filters_v3', rest);
  }

  // ---------------------------------------------------------------------------
  // Estado
  // ---------------------------------------------------------------------------
  const historyCache = new Map();
  const state = {
    posts: new Map(),     // feed_id -> { post, el, derived }
    byProduct: new Map(), // product.id -> feed_id de su publicación más reciente
    lastFeedId: 0,
    loaded: false,
    filters: loadFilters(),
    appliedSort: 'recent',
    pageLimit: PAGE_STEP,
    soundEnabled: storage.get('glitchhunter_sound') !== 'false',
    wsConnected: false,
    unseenNew: 0,
    votes: storage.getJSON('glitchhunter_votes', {}) || {},
    statusSince: null,    // marca de tiempo del servidor para pedir solo cambios de vigencia nuevos
    watchlist: [],
    watchlistEntries: [], // [{ keyword, max_price }]
    adminMode: false
  };

  // ---------------------------------------------------------------------------
  // Utilidades: sanitización anti-XSS y formatos
  // ---------------------------------------------------------------------------
  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sanitizeUrl(rawUrl) {
    if (!rawUrl) return '#';
    const trimmed = String(rawUrl).trim();
    // Solo HTTP, HTTPS o rutas relativas simples (bloquea javascript: y //protocol-relative)
    if (/^https?:\/\/[^\s"'<>]+$/i.test(trimmed) || /^\/(?!\/)[^\s"'<>]*$/.test(trimmed)) {
      return trimmed.replace(/"/g, '%22').replace(/'/g, '%27');
    }
    return '#';
  }

  function sanitizeAttrUrl(rawUrl) {
    return escapeHtml(sanitizeUrl(rawUrl));
  }

  const clpFormatter = new Intl.NumberFormat('es-CL', { style: 'currency', currency: 'CLP', maximumFractionDigits: 0 });
  function formatCLP(val) {
    const n = Number(val);
    if (!isFinite(n) || n <= 0) return '$0';
    return clpFormatter.format(n);
  }

  function toNum(v) {
    const n = Number(v);
    return isFinite(n) ? n : 0;
  }

  function parseDate(iso) {
    const d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  function formatRelative(iso) {
    const d = parseDate(iso);
    if (!d) return '';
    const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
    if (diffSec < 45) return 'ahora';
    const min = Math.round(diffSec / 60);
    if (min < 60) return `hace ${min} min`;
    const hrs = Math.round(min / 60);
    if (hrs < 24) return `hace ${hrs} h`;
    const time = d.toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return `ayer ${time}`;
    return `${d.toLocaleDateString('es-CL', { day: 'numeric', month: 'short' })} ${time}`;
  }

  function formatAbsolute(iso) {
    const d = parseDate(iso);
    if (!d) return '';
    return d.toLocaleString('es-CL', { dateStyle: 'full', timeStyle: 'short' });
  }

  function normalizeCategory(raw) {
    if (!raw) return '';
    const first = String(raw).split('/')[0].trim().toLowerCase();
    return first === 'general' ? 'otros' : first.slice(0, 40);
  }

  // Nombres legibles de las categorías internas del radar (core/categories.py)
  const CATEGORY_LABELS = {
    notebooks: 'Notebooks', smartphones: 'Celulares', tablets: 'Tablets', televisores: 'Televisores',
    monitores: 'Monitores', consolas: 'Consolas', smartwatch: 'Relojes inteligentes', audio: 'Audio',
    componentes: 'Componentes PC', gaming: 'Gaming', linea_blanca: 'Línea blanca', accesorios: 'Accesorios',
    tecnologia: 'Tecnología', computacion: 'Computación', ropa_calzado: 'Ropa y calzado',
    belleza: 'Belleza y perfumes', deportes: 'Deportes y aire libre', herramientas: 'Herramientas',
    hogar: 'Hogar y cocina', otros: 'Otros'
  };

  function categoryLabel(c) {
    return CATEGORY_LABELS[c] || capitalize(String(c || '').replace(/_/g, ' '));
  }

  function capitalize(s) {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  function normalizeText(s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  }

  // ---------------------------------------------------------------------------
  // Sonido opcional
  // ---------------------------------------------------------------------------
  const sound = {
    ctx: null,
    init() {
      try {
        if (!this.ctx) {
          const AudioClass = window.AudioContext || window.webkitAudioContext;
          if (AudioClass) this.ctx = new AudioClass();
        }
        if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
      } catch { /* sin audio */ }
    },
    ding() {
      if (!state.soundEnabled) return;
      try {
        this.init();
        if (!this.ctx) return;
        const now = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(784, now);
        osc.frequency.exponentialRampToValueAtTime(1046.5, now + 0.14);
        gain.gain.setValueAtTime(0.01, now);
        gain.gain.linearRampToValueAtTime(0.12, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.32);
        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start(now);
        osc.stop(now + 0.33);
      } catch { /* bloqueado por el navegador */ }
    }
  };

  // ---------------------------------------------------------------------------
  // Avisos (toasts)
  // ---------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function showToast(text, title = 'GlitchHunter', ms = 4500) {
    const container = $('toast-container');
    if (!container) return;
    while (container.children.length >= 3) container.firstElementChild.remove();
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = `
      <div class="toast-body">
        <strong>${escapeHtml(title)}</strong>
        <div class="toast-text">${escapeHtml(text)}</div>
      </div>
      <button type="button" class="toast-x" aria-label="Cerrar aviso">✕</button>`;
    container.appendChild(el);
    const remove = () => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 200);
    };
    el.querySelector('.toast-x').addEventListener('click', remove);
    setTimeout(remove, ms);
  }

  // ---------------------------------------------------------------------------
  // API key y peticiones autenticadas
  // ---------------------------------------------------------------------------
  function getStoredApiKey() {
    return storage.get('glitchhunter_api_key') || '';
  }

  function setStoredApiKey(key) {
    if (key && key.trim()) storage.set('glitchhunter_api_key', key.trim());
    else storage.remove('glitchhunter_api_key');
    updateApiKeyUI();
  }

  function updateApiKeyUI() {
    const label = $('api-key-label');
    if (label) label.textContent = getStoredApiKey() ? 'Clave de API ✓' : 'Clave de API';
  }

  async function authenticatedFetch(url, options = {}) {
    const opts = { ...options, headers: { ...(options.headers || {}) } };
    const method = String(opts.method || 'GET').toUpperCase();
    // Anti-CSRF: header propio en toda escritura (fuerza preflight CORS para otros orígenes)
    if (method !== 'GET' && method !== 'HEAD') opts.headers['X-GlitchHunter-Client'] = 'web';
    const key = getStoredApiKey();
    if (key) opts.headers['X-API-Key'] = key;
    let res = await fetch(url, opts);
    if (res.status === 401) {
      const entered = window.prompt('Esta acción requiere la clave de API del servidor (API_AUTH_KEY). Ingrésala:');
      if (entered && entered.trim()) {
        setStoredApiKey(entered);
        opts.headers['X-API-Key'] = entered.trim();
        res = await fetch(url, opts);
      }
    }
    return res;
  }

  // ---------------------------------------------------------------------------
  // Derivados de cada publicación (se calculan una vez; los datos quedan fijos)
  // ---------------------------------------------------------------------------
  function derive(post) {
    const product = post.product || {};
    const verdict = post.verdict || {};
    const current = toNum(product.current_price);
    const normal = toNum(product.normal_price);
    const card = toNum(product.card_price);
    const hasCardPrice = card > 0 && card < current;
    const best = hasCardPrice ? card : current;
    let discount = toNum(verdict.discount_percentage);
    if (!discount && normal > 0 && best > 0) discount = ((normal - best) / normal) * 100;
    let savings = toNum(verdict.absolute_savings_clp);
    if (!savings) savings = Math.max(0, normal - best);
    const storeId = String(product.store_id || '');
    const storeName = (STORES[storeId] && STORES[storeId].name) || product.store_name || storeId || 'Tienda';
    return {
      isGlitch: post.feed_kind === 'glitch',
      storeId,
      storeName,
      category: normalizeCategory(product.category),
      discount: Math.max(0, Math.round(discount)),
      savings,
      best,
      current,
      normal,
      hasCardPrice,
      createdAt: post.created_at || post.received_at,
      searchText: normalizeText(`${product.title || ''} ${storeName} ${product.category || ''}`)
    };
  }

  function hasVerifiedBacking(post, d) {
    const verdict = post.verdict || {};
    return Boolean(
      verdict.verified_drop
      || toNum(verdict.baseline_30d) > 0
      || (toNum(post.market_median_price) > 0 && toNum(post.market_stores_count) >= 2)
      || (d && d.isGlitch && verdict.evidence_label)
    );
  }

  function buildProofTags(post, verdict, d) {
    const tags = [];
    tags.push(d.isGlitch
      ? '<span class="tag tag-glitch">⚡ Posible error de precio</span>'
      : '<span class="tag tag-deal">🏷️ Oferta</span>');
    if (verdict.is_missing_zero) {
      tags.push('<span class="tag tag-zero" title="El precio parece estar ~10 veces más bajo por un cero omitido">~10× bajo lo normal</span>');
    }
    const tier = d.isGlitch ? post.risk_tier : null;
    if (tier === 'irrisorio') {
      tags.push('<span class="tag tag-risk-high" title="Con descuentos de 95% o más, las tiendas casi siempre anulan la compra">⚠️ Muy probable anulación</span>');
    } else if (tier === 'plausible') {
      tags.push('<span class="tag tag-risk-ok" title="Rango donde suele haber más posibilidades de que respeten la compra">✅ Error plausible</span>');
    }
    if (toNum(post.bulk_price_count) >= 4) {
      tags.push(`<span class="tag tag-bulk" title="Varios productos distintos de esta tienda aparecieron al mismo precio: típico error masivo">📦 Mismo precio en ${toNum(post.bulk_price_count)} productos</span>`);
    }
    if (d.hasCardPrice) tags.push('<span class="tag tag-card">💳 Precio tarjeta</span>');
    if (post.__isTest) tags.unshift('<span class="tag tag-test" title="Simulación: no se guarda y desaparece al recargar">🧪 PRUEBA</span>');
    if (post.is_watchlist_match) tags.push('<span class="tag tag-watch">⭐ Tu watchlist</span>');

    const b30 = toNum(verdict.baseline_30d);
    const mkt = toNum(post.market_median_price);
    const mktStores = toNum(post.market_stores_count);
    if (b30 > 0 && d.best > 0 && d.best < b30 * 0.92) {
      tags.push(`<span class="tag tag-verified" title="Respaldo histórico real en el radar">✓ Bajó vs su precio 30d (${escapeHtml(formatCLP(b30))})</span>`);
    } else if (mkt > 0 && mktStores >= 2) {
      tags.push(`<span class="tag tag-verified" title="Comparado con otras tiendas del mercado">✓ Más barato vs ${mktStores} tiendas</span>`);
    } else if (verdict.evidence_label) {
      tags.push(`<span class="tag tag-verified">${escapeHtml(verdict.evidence_label)}</span>`);
    } else if (verdict.verified_drop) {
      tags.push('<span class="tag tag-verified">✓ Baja respaldada</span>');
    } else {
      tags.push('<span class="tag tag-neutral" title="Aún no tenemos suficiente historial o tiendas comparables para confirmar si el precio antes realmente era mayor">Sin historial suficiente</span>');
    }
    return tags;
  }

  // ---------------------------------------------------------------------------
  // Tarjeta de publicación (se crea UNA vez por feed_id)
  // ---------------------------------------------------------------------------
  function createPostElement(post, d) {
    const product = post.product || {};
    const verdict = post.verdict || {};
    // El id de voto es siempre alert_id tal como lo entrega el servidor (nunca se construye aquí)
    const dealId = post.alert_id !== undefined && post.alert_id !== null && post.alert_id !== '' ? String(post.alert_id) : '';
    const productId = product.id !== undefined && product.id !== null ? String(product.id) : '';
    const storeColorVal = storeColor(d.storeId);

    const safeTitle = escapeHtml(product.title || 'Producto sin título');
    const safeStore = escapeHtml(d.storeName);
    const cleanUrl = sanitizeUrl(post.alert_url || product.url);
    const cleanCart = sanitizeUrl(post.add_to_cart_url || product.add_to_cart_url);
    const safeUrl = sanitizeAttrUrl(cleanUrl);
    const safeCartUrl = cleanCart !== '#' && cleanCart !== cleanUrl ? sanitizeAttrUrl(cleanCart) : '';
    const rawImage = product.image_url ? String(product.image_url).trim() : '';
    const safeImage = /^https?:\/\//i.test(rawImage) ? sanitizeAttrUrl(rawImage) : '';
    const safeCategory = escapeHtml(categoryLabel(d.category));

    const tags = buildProofTags(post, verdict, d);
    const riskTier = d.isGlitch ? post.risk_tier : null;

    let marketHtml = '';
    if (toNum(post.market_median_price) > 0) {
      const stores = toNum(post.market_stores_count);
      const mPct = post.market_discount_pct;
      const mPctTxt = (mPct !== null && mPct !== undefined && isFinite(Number(mPct)))
        ? ` · ${Number(mPct) > 0 ? `${Math.round(Number(mPct))}% más barato` : `${Math.abs(Math.round(Number(mPct)))}% más caro`}`
        : '';
      marketHtml = `<div class="market">🌐 Otras tiendas: <strong>${formatCLP(post.market_median_price)}</strong>${stores ? ` (${stores} tiendas)` : ''}${escapeHtml(mPctTxt)}</div>`;
    }
    const min90 = toNum(verdict.min_90d);
    if (min90 > 0) {
      marketHtml += `<div class="market">📉 Mínimo visto (90d): <strong>${formatCLP(min90)}</strong></div>`;
    }

    // Consejos prácticos (texto fijo, sin datos del servidor)
    let tipsHtml = '';
    if (riskTier === 'irrisorio') {
      tipsHtml = '<p class="tips">Casos así casi siempre se anulan con reembolso. Si compras: 1 sola unidad, guarda captura del precio y del comprobante.</p>';
    } else if (riskTier === 'plausible') {
      tipsHtml = '<p class="tips">Si compras, hazlo por 1 unidad y guarda captura del precio y del comprobante. Si la tienda anula sin justificación, puedes reclamar en sernac.cl.</p>';
    }

    const priceNote = d.hasCardPrice
      ? `<div class="price-note">Con tarjeta de la tienda · sin tarjeta ${formatCLP(d.current)}</div>`
      : '';

    const voted = dealId ? state.votes[dealId] : undefined;
    const up = toNum(post.upvotes);
    const down = toNum(post.downvotes);

    const el = document.createElement('article');
    el.className = `post${d.isGlitch ? ' is-glitch' : ''}`;
    el.dataset.feedId = String(post.feed_id);
    el.setAttribute('aria-label', `${d.isGlitch ? 'Error de precio' : 'Oferta'}: ${product.title || ''}, ${d.discount}% de descuento en ${d.storeName}`);
    el.innerHTML = `
      <a class="post-media" href="${safeUrl}" target="_blank" rel="noopener noreferrer" tabindex="-1" aria-hidden="true">
        ${safeImage && safeImage !== '#'
          ? `<img src="${safeImage}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />`
          : '<span class="img-fallback">🛍️</span>'}
      </a>
      <div class="post-body">
        <div class="post-meta">
          <span class="store-name"><span class="chip-dot" style="background:${storeColorVal}"></span>${safeStore}</span>
          <span aria-hidden="true">·</span>
          <time class="rel-time" datetime="${escapeHtml(d.createdAt || '')}" title="${escapeHtml(formatAbsolute(d.createdAt))}">${escapeHtml(formatRelative(d.createdAt))}</time>
          ${safeCategory ? `<span aria-hidden="true">·</span><span>${safeCategory}</span>` : ''}
        </div>
        <h3 class="post-title" title="${safeTitle}">${safeTitle}</h3>
        <div class="tags">${tags.join('')}</div>
      </div>
      <div class="post-deal">
        <div class="price-row">
          <span class="discount" aria-label="${d.discount}% de descuento">-${d.discount}%</span>
          <div class="prices">
            <span class="price-now">${formatCLP(d.best)}</span>
            <span class="price-before">Antes <s>${formatCLP(d.normal)}</s></span>
          </div>
          <span class="savings">Ahorras ${formatCLP(d.savings)}</span>
        </div>
        ${priceNote}
        ${marketHtml}
        ${tipsHtml}
      </div>
      <div class="post-actions">
        <a class="btn btn-primary" href="${safeUrl}" target="_blank" rel="noopener noreferrer">
          Ver en ${safeStore}
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
        </a>
        ${safeCartUrl ? `
        <a class="btn btn-secondary" href="${safeCartUrl}" target="_blank" rel="noopener noreferrer" title="Abre la tienda con el producto en el carro">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/><path d="M2 3h3l2.6 12.2a1 1 0 0 0 1 .8h9.7a1 1 0 0 0 1-.8L21 7H6"/></svg>
          Al carro
        </a>` : ''}
        ${productId && !post.__isTest ? `<button type="button" class="btn-history" data-product-id="${escapeHtml(productId)}" data-best-price="${d.best}" aria-expanded="false">📈 Historial 30d</button>` : ''}
        ${dealId && !post.__isTest ? `<div class="votes" role="group" aria-label="¿Es una oferta real?">
          <span class="votes-label">¿Es real?</span>
          <button type="button" class="vote-btn up${voted === 'up' ? ' voted' : ''}" data-vote="up" data-deal-id="${escapeHtml(dealId)}" aria-label="Sí, es una oferta real" ${voted ? 'disabled' : ''}>👍 <span class="vote-count">${up}</span></button>
          <button type="button" class="vote-btn down${voted === 'down' ? ' voted' : ''}" data-vote="down" data-deal-id="${escapeHtml(dealId)}" aria-label="No, es un falso positivo" ${voted ? 'disabled' : ''}>👎 <span class="vote-count">${down}</span></button>
        </div>` : ''}
      </div>
      <div class="history-box" hidden></div>`;

    renderStatus(el, post);

    // Imagen con fallback sin handlers inline
    const img = el.querySelector('.post-media img');
    if (img) {
      img.addEventListener('error', () => {
        const span = document.createElement('span');
        span.className = 'img-fallback';
        span.textContent = '🛍️';
        img.replaceWith(span);
      }, { once: true });
    }
    return el;
  }

  // ---------------------------------------------------------------------------
  // Vigencia: una oferta publicada puede expirar (subió, se agotó) o quedar sin confirmar.
  // Se marca en la misma tarjeta, sin volver a dibujarla.
  // ---------------------------------------------------------------------------
  function statusText(post) {
    if (post.status !== 'expired') return '';
    if (post.expired_reason === 'price_up') {
      return toNum(post.price_now) > 0 ? `Expirada · ahora cuesta ${formatCLP(post.price_now)}` : 'Expirada · subió de precio';
    }
    if (post.expired_reason === 'out_of_stock') return 'Agotada';
    if (post.expired_reason === 'not_seen') return 'Sin confirmar · no la vemos hace más de 24 h';
    if (post.expired_reason === 'no_discount') return 'Expirada · la tienda ya no muestra descuento';
    if (post.expired_reason === 'withdrawn') return 'Retirada · no era una rebaja real';
    return 'Ya no disponible';
  }

  function renderStatus(el, post) {
    const expired = post.status === 'expired';
    el.classList.toggle('is-expired', expired);
    let note = el.querySelector('.status-note');
    if (!expired) {
      if (note) note.remove();
      return;
    }
    if (!note) {
      note = document.createElement('div');
      note.className = 'status-note';
      note.setAttribute('role', 'status');
      const body = el.querySelector('.post-body');
      if (body) body.appendChild(note);
    }
    note.textContent = statusText(post); // texto plano: sin HTML del servidor
  }

  async function fetchStatusChanges() {
    if (!state.loaded) return;
    try {
      const url = state.statusSince
        ? `/api/feed/status?since=${encodeURIComponent(state.statusSince)}`
        : '/api/feed/status';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (data && typeof data.now === 'string') state.statusSince = data.now;
      const changes = Array.isArray(data && data.changes) ? data.changes : [];
      let touched = 0;
      changes.forEach(c => {
        const entry = state.posts.get(Number(c.feed_id));
        if (!entry) return;
        entry.post.status = c.status;
        entry.post.expired_reason = c.expired_reason;
        entry.post.price_now = c.price_now;
        renderStatus(entry.el, entry.post);
        touched++;
      });
      if (touched) applyFilters();
    } catch { /* sin conexión: se reintenta en el próximo ciclo */ }
  }

  // ---------------------------------------------------------------------------
  // Filtros (solo muestran/ocultan nodos existentes)
  // ---------------------------------------------------------------------------
  function matches(entry) {
    const f = state.filters;
    const d = entry.derived;
    if (f.kind === 'glitch' && !d.isGlitch) return false;
    if (f.kind === 'deal' && d.isGlitch) return false;
    if (f.minDiscount && d.discount < f.minDiscount) return false;
    if (f.maxPrice && d.best > f.maxPrice) return false;
    if (f.verifiedOnly && !hasVerifiedBacking(entry.post, d)) return false;
    if (f.stores.length && !f.stores.includes(d.storeId)) return false;
    if (f.category && d.category !== f.category) return false;
    if (f.watchlistOnly && !entry.post.is_watchlist_match) return false;
    if (f.hideExpired && entry.post.status === 'expired') return false;
    if (f.search.trim()) {
      const terms = normalizeText(f.search).split(/\s+/).filter(Boolean);
      if (!terms.every(t => d.searchText.includes(t))) return false;
    }
    return true;
  }

  function activeFilterCount() {
    const f = state.filters;
    let n = 0;
    if (f.kind !== 'all') n++;
    if (f.minDiscount) n++;
    if (f.maxPrice) n++;
    if (f.stores.length) n++;
    if (f.category) n++;
    if (f.verifiedOnly) n++;
    if (f.watchlistOnly) n++;
    if (!f.hideExpired) n++;
    return n;
  }

  function sortedEntries() {
    const entries = [...state.posts.values()];
    const by = state.filters.sortBy;
    entries.sort((a, b) => {
      if (by === 'discount' && b.derived.discount !== a.derived.discount) return b.derived.discount - a.derived.discount;
      if (by === 'savings' && b.derived.savings !== a.derived.savings) return b.derived.savings - a.derived.savings;
      return b.post.feed_id - a.post.feed_id; // más reciente primero
    });
    return entries;
  }

  /** Reordena moviendo los nodos existentes (appendChild mueve, no recrea). */
  function reorderNodes() {
    const list = $('feed-list');
    if (!list) return;
    sortedEntries().forEach(e => list.appendChild(e.el));
    state.appliedSort = state.filters.sortBy;
  }

  function applyFilters({ resetPage = false } = {}) {
    if (resetPage) state.pageLimit = PAGE_STEP;
    const ordered = sortedEntries();
    if (state.appliedSort !== state.filters.sortBy) {
      const list = $('feed-list');
      if (list) ordered.forEach(e => list.appendChild(e.el));
      state.appliedSort = state.filters.sortBy;
    }
    let matchedTotal = 0;
    let shown = 0;
    ordered.forEach(entry => {
      const ok = matches(entry);
      if (ok) matchedTotal++;
      const show = ok && matchedTotal <= state.pageLimit;
      entry.el.hidden = !show;
      if (show) shown++;
    });
    const moreBtn = $('btn-feed-more');
    if (moreBtn) {
      const rem = matchedTotal - shown;
      moreBtn.hidden = rem <= 0;
      if (rem > 0) moreBtn.textContent = `Mostrar ${Math.min(PAGE_STEP, rem)} ofertas más (${shown} de ${matchedTotal})`;
    }
    updateSummary(matchedTotal);
    syncFilterControls();
    saveFilters();
  }

  function updateSummary(visible) {
    const total = state.posts.size;
    let glitches = 0;
    let latest = null;
    state.posts.forEach(e => {
      if (e.derived.isGlitch) glitches++;
      if (!latest || e.post.feed_id > latest.post.feed_id) latest = e;
    });

    const summary = $('feed-summary');
    if (summary && state.loaded) {
      const parts = [];
      if (visible !== total) parts.push(`Mostrando ${visible} de ${total}`);
      else parts.push(`${total} ${total === 1 ? 'publicación' : 'publicaciones'}`);
      parts.push(`${glitches} ${glitches === 1 ? 'error de precio' : 'errores de precio'}`);
      if (latest) parts.push(`última ${formatRelative(latest.derived.createdAt)}`);
      summary.textContent = parts.join(' · ');
    }

    const empty = $('feed-empty');
    if (empty && state.loaded) {
      empty.hidden = visible > 0;
      const hasFilters = activeFilterCount() > 0 || state.filters.search.trim();
      $('feed-empty-title').textContent = total === 0
        ? 'Todavía no hay publicaciones'
        : 'No hay ofertas con estos filtros';
      $('feed-empty-text').textContent = total === 0
        ? 'El radar publicará aquí cada oferta apenas la detecte. Esta página se actualiza sola.'
        : 'Prueba quitando algún filtro o buscando otra palabra.';
      $('btn-empty-clear').hidden = !hasFilters;
    }

    const kp = $('kpi-posts');
    if (kp) kp.textContent = String(total);
    const kg = $('kpi-glitches');
    if (kg) kg.textContent = String(glitches);
  }

  /** Reconstruye solo los chips de filtros (no las tarjetas) con los conteos actuales. */
  function renderFilterChips() {
    const storeCounts = new Map();
    const catCounts = new Map();
    state.posts.forEach(e => {
      storeCounts.set(e.derived.storeId, (storeCounts.get(e.derived.storeId) || 0) + 1);
      if (e.derived.category) catCounts.set(e.derived.category, (catCounts.get(e.derived.category) || 0) + 1);
    });

    const storeWrap = $('store-chips');
    if (storeWrap) {
      const ids = new Set([...Object.keys(STORES), ...storeCounts.keys(), ...state.filters.stores]);
      const html = [...ids]
        .filter(Boolean)
        .sort((a, b) => (storeCounts.get(b) || 0) - (storeCounts.get(a) || 0))
        .map(id => {
          const count = storeCounts.get(id) || 0;
          const selected = state.filters.stores.includes(id);
          if (!count && !selected) return '';
          const name = (STORES[id] && STORES[id].name) || id;
          const color = storeColor(id);
          return `<button type="button" class="chip" data-store="${escapeHtml(id)}" aria-pressed="${selected}">
              <span class="chip-dot" style="background:${color}"></span>${escapeHtml(name)} <span class="chip-count">${count}</span></button>`;
        }).join('');
      storeWrap.innerHTML = html || '<span class="muted small">Sin tiendas aún</span>';
    }

    const catWrap = $('category-chips');
    if (catWrap) {
      const cats = [...catCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, MAX_CATEGORY_CHIPS);
      if (state.filters.category && !cats.some(([c]) => c === state.filters.category)) {
        cats.push([state.filters.category, catCounts.get(state.filters.category) || 0]);
      }
      catWrap.innerHTML = cats.length
        ? cats.map(([c, n]) => `<button type="button" class="chip" data-category="${escapeHtml(c)}" aria-pressed="${state.filters.category === c}">${escapeHtml(categoryLabel(c))} <span class="chip-count">${n}</span></button>`).join('')
        : '<span class="muted small">Sin categorías aún</span>';
    }
  }

  function syncFilterControls() {
    const f = state.filters;
    document.querySelectorAll('.seg-btn[data-kind]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.kind === f.kind)));
    document.querySelectorAll('.seg-btn[data-min]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.min) === f.minDiscount)));
    document.querySelectorAll('.seg-btn[data-max-price]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.maxPrice) === f.maxPrice)));
    document.querySelectorAll('#store-chips .chip').forEach(b => b.setAttribute('aria-pressed', String(f.stores.includes(b.dataset.store))));
    document.querySelectorAll('#category-chips .chip').forEach(b => b.setAttribute('aria-pressed', String(f.category === b.dataset.category)));
    const vf = $('filter-verified');
    if (vf) vf.checked = f.verifiedOnly;
    const wl = $('filter-watchlist');
    if (wl) wl.checked = f.watchlistOnly;
    const hx = $('filter-hide-expired');
    if (hx) hx.checked = f.hideExpired;
    const sort = $('sort-select');
    if (sort && sort.value !== f.sortBy) sort.value = f.sortBy;
    const n = activeFilterCount();
    const badge = $('active-filters-count');
    if (badge) { badge.textContent = String(n); badge.hidden = n === 0; }
    const clear = $('btn-clear-filters');
    if (clear) clear.hidden = n === 0;
  }

  function clearFilters() {
    state.filters = { ...DEFAULT_FILTERS, stores: [], sortBy: state.filters.sortBy };
    const input = $('search-input');
    if (input) input.value = '';
    deepSetQuery('');
    applyFilters({ resetPage: true });
  }

  // ---------------------------------------------------------------------------
  // Canal: carga inicial + agregar arriba solo lo nuevo
  // ---------------------------------------------------------------------------
  /**
   * Registra publicaciones nuevas (ignora repetidas). Si un producto se republica (bajó de precio:
   * nuevo feed_id, mismo product.id) se conserva solo la publicación más reciente.
   * Devuelve { added, replaced }: agregadas (de la más antigua a la más nueva) y entradas reemplazadas
   * (ya quitadas del estado; su nodo se quita del DOM al insertar).
   */
  function registerPosts(posts, { older = false } = {}) {
    const added = [];
    const replaced = [];
    [...posts]
      .filter(p => p && p.product && Number.isFinite(Number(p.feed_id)))
      .sort((a, b) => a.feed_id - b.feed_id)
      .forEach(post => {
        if (state.posts.has(post.feed_id)) return;
        // Las publicaciones antiguas solo entran al traer resultados de un filtro (older)
        if (!older && post.feed_id <= state.lastFeedId) return;
        const pid = post.product.id !== undefined && post.product.id !== null ? String(post.product.id) : '';
        if (pid && state.byProduct.has(pid)) {
          const oldId = state.byProduct.get(pid);
          if (oldId > post.feed_id) return;   // ya está la publicación más reciente de este producto
          const old = state.posts.get(oldId);
          if (old) {
            state.posts.delete(oldId);
            replaced.push(old);
          }
        }
        const derived = derive(post);
        const el = createPostElement(post, derived);
        const entry = { post, el, derived };
        state.posts.set(post.feed_id, entry);
        if (pid) state.byProduct.set(pid, post.feed_id);
        state.lastFeedId = Math.max(state.lastFeedId, post.feed_id);
        added.push(entry);
      });
    // Una entrada agregada y reemplazada en el mismo lote no debe dibujarse
    return { added: added.filter(e => state.posts.get(e.post.feed_id) === e), replaced };
  }

  async function loadFeed() {
    try {
      const res = await fetch(`/api/feed?limit=${INITIAL_LIMIT}`);
      if (res.ok) {
        const posts = await res.json();
        const { added } = registerPosts(Array.isArray(posts) ? posts : []);
        const list = $('feed-list');
        // Primera y única vez que se dibuja el historial
        added.reverse().forEach(e => list.appendChild(e.el));
      } else {
        showToast('No se pudo cargar el canal. Reintentando…', 'Sin conexión');
      }
    } catch {
      showToast('No se pudo conectar con el radar. Reintentando…', 'Sin conexión');
    }
    state.loaded = true;
    $('feed-loading').hidden = true;
    state.appliedSort = 'recent';
    renderFilterChips();
    applyFilters();
    loadMatching();   // filtros guardados de una visita anterior
  }

  /**
   * Trae del servidor las publicaciones que calzan con los filtros de tipo, descuento y tienda,
   * aunque sean más antiguas que las cargadas al inicio (la página parte con las últimas INITIAL_LIMIT).
   */
  let matchingSeq = 0;
  async function loadMatching() {
    const f = state.filters;
    if (f.kind === 'all' && !f.minDiscount && !f.stores.length) return;
    const params = new URLSearchParams({ limit: String(MATCHING_LIMIT) });
    if (f.kind !== 'all') params.set('kind', f.kind);
    if (f.minDiscount) params.set('min_discount', String(f.minDiscount));
    if (f.stores.length) params.set('stores', f.stores.join(','));
    const seq = ++matchingSeq;
    try {
      const res = await fetch(`/api/feed?${params}`);
      if (!res.ok || seq !== matchingSeq) return;
      const posts = await res.json();
      const { added, replaced } = registerPosts(Array.isArray(posts) ? posts : [], { older: true });
      if (!added.length) return;
      replaced.forEach(e => e.el.remove());
      const list = $('feed-list');
      added.forEach(e => list.appendChild(e.el));
      reorderNodes();
      renderFilterChips();
      applyFilters();
    } catch { /* sin conexión: quedan las publicaciones ya cargadas */ }
  }

  function isScrolledAway() {
    return window.scrollY > 240;
  }

  /** Inserta las tarjetas nuevas arriba sin tocar las existentes y sin mover lo que el usuario está leyendo. */
  function insertNewPosts(added, replaced = []) {
    const list = $('feed-list');
    if (!list || (!added.length && !replaced.length)) return;
    const away = isScrolledAway();
    const beforeHeight = document.documentElement.scrollHeight;
    const beforeY = window.scrollY;

    // La versión anterior de un producto republicado se retira (sin duplicados ni conteos inflados)
    replaced.forEach(e => e.el.remove());

    added.forEach(e => {
      e.el.hidden = !matches(e);
      e.el.classList.add('is-new');
      setTimeout(() => e.el.classList.remove('is-new'), NEW_HIGHLIGHT_MS);
      list.prepend(e.el);
    });
    if (state.filters.sortBy !== 'recent') reorderNodes();

    renderFilterChips();
    applyFilters();

    if (away) {
      // Mantener la vista: compensar la altura agregada arriba (tarjetas, resumen y chips)
      const delta = document.documentElement.scrollHeight - beforeHeight;
      if (delta !== 0) window.scrollTo({ top: Math.max(0, beforeY + delta), behavior: 'instant' });
      const visibleNew = added.filter(e => !e.el.hidden).length;
      if (visibleNew > 0) {
        state.unseenNew += visibleNew;
        updateNewPill();
      }
    }
  }

  /**
   * Tarjeta temporal de simulación: no entra al estado del canal (ni a conteos, filtros o lastFeedId),
   * no admite votos y desaparece al recargar.
   */
  const shownTests = new Set();
  function showTestCard(alert) {
    if (!alert || typeof alert !== 'object' || !alert.product) return false;
    const key = String(alert.alert_id || (alert.product && alert.product.id) || Math.random());
    if (shownTests.has(key)) return true;
    shownTests.add(key);
    const post = { ...alert, feed_id: undefined, feed_kind: 'glitch', __isTest: true };
    if (!post.created_at) post.created_at = new Date().toISOString();
    const derived = derive(post);
    const el = createPostElement(post, derived);
    el.classList.add('is-test');
    el.removeAttribute('data-feed-id');
    const list = $('feed-list');
    if (!list) return false;
    list.prepend(el);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    sound.ding();
    showToast(`${derived.storeName}: -${derived.discount}% (no se guarda)`, '🧪 Oferta de prueba');
    return true;
  }

  function updateNewPill() {
    const pill = $('new-posts-pill');
    if (!pill) return;
    if (state.unseenNew > 0) {
      $('new-posts-text').textContent = state.unseenNew === 1 ? '1 oferta nueva' : `${state.unseenNew} ofertas nuevas`;
      pill.hidden = false;
    } else {
      pill.hidden = true;
    }
  }

  let fetchingNew = false;
  let fetchAgain = false;
  async function fetchNewPosts({ quiet = false, isTest = false } = {}) {
    if (!state.loaded) return [];
    if (fetchingNew) { fetchAgain = true; return []; }
    fetchingNew = true;
    let added = [];
    try {
      const res = await fetch(`/api/feed?after_id=${encodeURIComponent(state.lastFeedId)}`);
      if (!res.ok) return [];
      const posts = await res.json();
      const result = registerPosts(Array.isArray(posts) ? posts : []);
      added = result.added;
      if (!added.length && !result.replaced.length) return [];
      insertNewPosts(added, result.replaced);
      if (!added.length) return [];
      if (!quiet) notifyNew(added, isTest);
      return added;
    } catch {
      return [];
    } finally {
      fetchingNew = false;
      if (fetchAgain) {
        fetchAgain = false;
        setTimeout(() => fetchNewPosts(), 0);
      }
    }
  }

  function notifyNew(added, isTest) {
    const important = added.filter(e => e.derived.isGlitch || e.post.is_watchlist_match);
    if (important.length) sound.ding();
    const top = important.length ? important[important.length - 1] : added[added.length - 1];
    const title = (top.post.product && top.post.product.title) || '';
    const short = title.length > 60 ? `${title.slice(0, 57)}…` : title;
    const head = isTest
      ? '🧪 Oferta de prueba publicada'
      : top.derived.isGlitch ? '⚡ Nuevo error de precio'
      : top.post.is_watchlist_match ? '⭐ Coincide con tu watchlist'
      : added.length > 1 ? `🏷️ ${added.length} ofertas nuevas` : '🏷️ Nueva oferta';
    showToast(`${top.derived.storeName}: ${short} (-${top.derived.discount}%)`, head);
  }

  // ---------------------------------------------------------------------------
  // WebSocket en vivo con heartbeat y reconexión
  // ---------------------------------------------------------------------------
  let socket = null;
  let reconnectTimer = null;
  let wsPingTimer = null;

  function setLiveStatus(connected) {
    state.wsConnected = connected;
    const pill = $('live-status');
    const text = $('live-status-text');
    if (!pill || !text) return;
    pill.classList.toggle('is-live', connected);
    pill.classList.toggle('is-connecting', !connected);
    text.textContent = connected ? 'En vivo' : 'Reconectando…';
    pill.title = connected ? 'Conectado: las ofertas llegan al instante' : 'Sin conexión en vivo: se revisa cada 30 s';
  }

  function stopWsPing() {
    if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
  }

  function initWebSocket() {
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/glitches`);
      socket.onopen = () => {
        setLiveStatus(true);
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        stopWsPing();
        wsPingTimer = setInterval(() => {
          if (socket && socket.readyState === WebSocket.OPEN && document.visibilityState !== 'hidden') {
            try { socket.send('ping'); } catch { /* noop */ }
          }
        }, WS_PING_MS);
        fetchNewPosts({ quiet: true }); // ponerse al día tras una reconexión
      };
      socket.onmessage = (event) => {
        if (event.data === 'pong') return;
        try {
          const data = JSON.parse(event.data);
          if (data && (data.type === 'feed_new' || data.type === 'feed_updated' || data.type === 'new_glitch' || (data.alert_id && data.product))) {
            fetchNewPosts();
          } else if (data && data.type === 'feed_status') {
            fetchStatusChanges();
          } else if (data && data.type === 'deal_corrected' && data.product_id) {
            const feedId = state.byProduct.get(String(data.product_id));
            const entry = feedId ? state.posts.get(feedId) : null;
            if (entry) {
              entry.post.status = 'expired';
              entry.post.expired_reason = 'price_up';
              if (data.new_price) entry.post.price_now = data.new_price;
              renderStatus(entry.el, entry.post);
              applyFilters();
            }
          }
        } catch { /* pings u otros mensajes */ }
      };
      socket.onclose = () => {
        setLiveStatus(false);
        stopWsPing();
        socket = null;
        if (!reconnectTimer && document.visibilityState !== 'hidden') {
          reconnectTimer = setTimeout(() => { reconnectTimer = null; initWebSocket(); }, 4000);
        }
      };
      socket.onerror = () => setLiveStatus(false);
    } catch {
      setLiveStatus(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Watchlist (con tope opcional de precio y re-filtrado en vivo)
  // ---------------------------------------------------------------------------
  function normalizeWatchlistEntries(data) {
    if (Array.isArray(data && data.entries)) {
      return data.entries.map(e => ({
        keyword: String(e.keyword || '').toLowerCase(),
        max_price: e.max_price ? Number(e.max_price) : null
      })).filter(e => e.keyword);
    }
    return (Array.isArray(data && data.keywords) ? data.keywords : []).map(k => ({
      keyword: String(k).toLowerCase(),
      max_price: null
    }));
  }

  function refreshFeedWatchlistMatches() {
    state.posts.forEach(entry => {
      const title = String((entry.post.product && entry.post.product.title) || '').toLowerCase();
      const matched = state.watchlistEntries.some(w =>
        title.includes(w.keyword) && (!w.max_price || entry.derived.best <= w.max_price)
      );
      if (Boolean(entry.post.is_watchlist_match) !== matched) {
        entry.post.is_watchlist_match = matched;
        const tagsWrap = entry.el.querySelector('.tags');
        if (tagsWrap) {
          const old = tagsWrap.querySelector('.tag-watch');
          if (matched && !old) tagsWrap.insertAdjacentHTML('beforeend', '<span class="tag tag-watch">⭐ Tu watchlist</span>');
          else if (!matched && old) old.remove();
        }
      }
    });
    if (state.filters.watchlistOnly) applyFilters({ resetPage: true });
  }

  function renderWatchlist() {
    const list = $('watchlist-list');
    const empty = $('watchlist-empty');
    const count = $('watchlist-count');
    if (count) { count.textContent = String(state.watchlistEntries.length); count.hidden = state.watchlistEntries.length === 0; }
    if (!list) return;
    list.innerHTML = state.watchlistEntries.map(e => `
      <li class="wl-item"><span>${escapeHtml(e.keyword)}${e.max_price ? `<span class="wl-cap"> · ≤ ${escapeHtml(formatCLP(e.max_price))}</span>` : ''}</span>
        <button type="button" class="wl-remove" data-keyword="${escapeHtml(e.keyword)}" aria-label="Quitar ${escapeHtml(e.keyword)} de la watchlist">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button></li>`).join('');
    if (empty) empty.hidden = state.watchlistEntries.length > 0;
  }

  async function loadWatchlist() {
    try {
      const res = await fetch('/api/watchlist');
      if (!res.ok) return;
      const data = await res.json();
      state.watchlistEntries = normalizeWatchlistEntries(data);
      state.watchlist = state.watchlistEntries.map(e => e.keyword);
      renderWatchlist();
      refreshFeedWatchlistMatches();
    } catch { /* offline */ }
  }

  /** Traduce un 422 del servidor (FastAPI/Pydantic) a un mensaje legible. */
  async function friendlyValidationError(res) {
    const fallback = 'La palabra debe tener entre 2 y 60 caracteres.';
    try {
      const data = await res.json();
      const detail = data && data.detail;
      if (typeof detail === 'string' && detail.trim()) return detail.slice(0, 160);
      if (Array.isArray(detail) && detail.length) {
        const d = detail[0] || {};
        if (d.type && /too_short|too_long|string_/.test(String(d.type))) return fallback;
        if (d.msg) return String(d.msg).slice(0, 160);
      }
    } catch { /* cuerpo no JSON */ }
    return fallback;
  }

  async function addWatchlist(keyword, maxPriceRaw = '') {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return;
    if (kw.length < 2 || kw.length > 60) {
      showToast('La palabra debe tener entre 2 y 60 caracteres.', 'Watchlist');
      return;
    }
    const maxPrice = Number(maxPriceRaw);
    const hasCap = isFinite(maxPrice) && maxPrice > 0;
    if (state.watchlist.includes(kw) && !hasCap) { showToast(`"${kw}" ya está en tu watchlist.`, 'Watchlist'); return; }
    try {
      const payload = hasCap ? { keyword: kw, max_price: maxPrice } : { keyword: kw };
      const res = await authenticatedFetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const data = await res.json();
        state.watchlistEntries = normalizeWatchlistEntries(data);
        state.watchlist = state.watchlistEntries.map(e => e.keyword);
        renderWatchlist();
        refreshFeedWatchlistMatches();
        showToast(hasCap ? `Te avisaremos de "${kw}" hasta ${formatCLP(maxPrice)}.` : `Te avisaremos de ofertas de "${kw}".`, '⭐ Guardado en watchlist');
      } else if (res.status === 422) {
        showToast(await friendlyValidationError(res), 'Palabra no válida');
      } else if (res.status === 401) {
        showToast('Se necesita la clave de API para cambiar la watchlist.', 'No autorizado');
      } else {
        showToast('No se pudo agregar la palabra.', 'Error');
      }
    } catch {
      showToast('Error de conexión al agregar la palabra.', 'Error');
    }
  }

  async function removeWatchlist(keyword) {
    try {
      const res = await authenticatedFetch(`/api/watchlist?keyword=${encodeURIComponent(keyword)}`, { method: 'DELETE' });
      if (res.ok) {
        const data = await res.json();
        state.watchlistEntries = normalizeWatchlistEntries(data);
        state.watchlist = state.watchlistEntries.map(e => e.keyword);
        renderWatchlist();
        refreshFeedWatchlistMatches();
        showToast(`"${keyword}" se quitó de tu watchlist.`, 'Watchlist');
      } else if (res.status === 401) {
        showToast('Se necesita la clave de API para cambiar la watchlist.', 'No autorizado');
      } else {
        showToast('No se pudo quitar la palabra.', 'Error');
      }
    } catch {
      showToast('Error de conexión al quitar la palabra.', 'Error');
    }
  }

  // ---------------------------------------------------------------------------
  // Panel "Estado del radar" (detalles técnicos)
  // ---------------------------------------------------------------------------
  const HEALTH_LABELS = {
    HEALTHY: ['Funcionando', 'st-ok'],
    DEGRADED: ['Lenta', 'st-warn'],
    EMPTY_RESPONSE: ['Sin productos', 'st-warn'],
    BLOCKED: ['Bloqueada', 'st-bad'],
    OFFLINE: ['Caída', 'st-bad'],
    IDLE: ['Sin escanear', 'st-idle']
  };

  function healthBadge(s) {
    if (s.status === 'DEGRADED' && toNum(s.error_count) > 0) {
      return ['Con errores', 'st-warn'];
    }
    return HEALTH_LABELS[s.status] || [String(s.status || '–'), 'st-idle'];
  }

  async function refreshStatusPanel() {
    const [stats, status, health, rotation, feedback] = await Promise.all([
      fetch('/api/stats').then(r => (r.ok ? r.json() : null)).catch(() => null),
      fetch('/api/status').then(r => (r.ok ? r.json() : null)).catch(() => null),
      fetch('/api/store-health').then(r => (r.ok ? r.json() : null)).catch(() => null),
      fetch('/api/catalog/rotation').then(r => (r.ok ? r.json() : null)).catch(() => null),
      fetch('/api/feedback/stats').then(r => (r.ok ? r.json() : null)).catch(() => null)
    ]);

    if (stats) $('kpi-products').textContent = toNum(stats.total_products).toLocaleString('es-CL');
    if (feedback) {
      $('kpi-accuracy').textContent = toNum(feedback.total_votes) > 0
        ? `${Math.round(toNum(feedback.accuracy_rate_pct))}%`
        : 'Sin votos';
    }

    const scan = $('scan-status');
    if (scan) {
      if (status) {
        const rows = [
          ['Radar', status.status === 'online' ? 'En línea' : escapeHtml(status.status)],
          ['Escaneo automático', status.auto_scan_enabled ? `Sí, cada ${escapeHtml(status.scan_interval_minutes)} min (${escapeHtml(status.scan_interval_seconds)} s)` : 'Pausado'],
          ['Lectura directa', status.direct_scan_interval_seconds ? `Cada ${escapeHtml(status.direct_scan_interval_seconds)} s` : '–'],
          ['Modo evento', status.event_mode
            ? `🎯 Activo: ${escapeHtml(status.event_mode)}`
            : (() => {
              const today = new Date().toISOString().slice(0, 10);
              const next = (Array.isArray(status.event_windows) ? status.event_windows : []).filter(w => String(w.end) >= today);
              return next.length
                ? `Próximos: ${next.map(w => `${escapeHtml(w.name)} (${escapeHtml(w.start)})`).join(', ')}`
                : 'Sin fechas próximas configuradas';
            })()],
          ['Fuentes vigiladas', `${escapeHtml(status.stores_monitored)} (tiendas + SoloTodo)`],
          ['Conexión en vivo', state.wsConnected ? 'Conectada' : 'Reconectando'],
          ['Votos de la comunidad', feedback ? `👍 ${toNum(feedback.total_upvotes)} · 👎 ${toNum(feedback.total_downvotes)}` : '–']
        ];
        scan.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
      } else {
        scan.innerHTML = '<dt>Radar</dt><dd>Sin respuesta del servidor</dd>';
      }
    }

    const hl = $('store-health');
    if (hl) {
      const rawStores = health && Array.isArray(health.stores) ? health.stores : [];
      if (rawStores.length) {
        const rank = { OFFLINE: 0, BLOCKED: 1, EMPTY_RESPONSE: 2, DEGRADED: 3, HEALTHY: 4, IDLE: 5 };
        const storesH = [...rawStores].sort((a, b) => {
          const ea = a.status === 'DEGRADED' && toNum(a.error_count) > 0 ? 2.5 : (rank[a.status] ?? 9);
          const eb = b.status === 'DEGRADED' && toNum(b.error_count) > 0 ? 2.5 : (rank[b.status] ?? 9);
          if (ea !== eb) return ea - eb;
          return String(a.store_name || a.store_id).localeCompare(String(b.store_name || b.store_id), 'es');
        });
        const okCount = storesH.filter(s => s.status === 'HEALTHY').length;
        const slowCount = storesH.filter(s => s.status === 'DEGRADED' && toNum(s.error_count) === 0).length;
        const errCount = storesH.filter(s => ['OFFLINE', 'BLOCKED', 'EMPTY_RESPONSE'].includes(s.status) || (s.status === 'DEGRADED' && toNum(s.error_count) > 0)).length;
        const parts = [`${okCount} funcionando`];
        if (slowCount) parts.push(`${slowCount} lenta${slowCount === 1 ? '' : 's'}`);
        if (errCount) parts.push(`${errCount} con aviso`);
        const summary = `<li class="muted small">${escapeHtml(parts.join(' · '))}</li>`;
        hl.innerHTML = summary + storesH.map(s => {
          const [label, cls] = healthBadge(s);
          const lat = Math.round(toNum(s.avg_latency_ms || s.latency_ms));
          const when = s.last_scan_at ? formatRelative(s.last_scan_at) : 'nunca';
          return `<li class="health-item"><div>${escapeHtml(s.store_name || s.store_id)}
              <small>${toNum(s.items_scanned).toLocaleString('es-CL')} productos · ${lat} ms · último escaneo ${escapeHtml(when)}</small></div>
              <span class="status-badge ${cls}">${escapeHtml(label)}</span></li>`;
        }).join('');
      } else {
        hl.innerHTML = '<li class="muted small">Aún no hay escaneos desde que se inició el servidor.</li>';
      }
    }

    const rot = $('rotation');
    if (rot) {
      if (rotation && Array.isArray(rotation.categories) && rotation.categories.length) {
        const cur = toNum(rotation.current_cursor_index);
        const strategy = { discount_desc: 'mayor descuento primero' }[rotation.current_strategy] || rotation.current_strategy || '';
        rot.innerHTML = `<div>Revisa ${escapeHtml(rotation.batch_size || '')} categorías por ciclo${strategy ? `, ${escapeHtml(strategy)}` : ''}. Ciclo #${toNum(rotation.cycle_counter)}.</div>
          <div class="rot-list">${rotation.categories.map((c, i) => `<span class="rot-item${i === cur ? ' is-current' : ''}">${escapeHtml(String(c).replace(/_/g, ' '))}</span>`).join('')}</div>`;
      } else {
        rot.textContent = 'Rotación inactiva.';
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Paneles laterales y menú
  // ---------------------------------------------------------------------------
  let lastFocus = null;
  let statusTimer = null;

  function openDrawer(id) {
    const drawer = $(id);
    if (!drawer) return;
    closeMenu();
    lastFocus = document.activeElement;
    drawer.hidden = false;
    document.body.style.overflow = 'hidden';
    const focusTarget = drawer.querySelector('.drawer-body input') || drawer.querySelector('.drawer-head button');
    if (focusTarget) focusTarget.focus();
    if (id === 'drawer-status') {
      refreshStatusPanel();
      statusTimer = setInterval(refreshStatusPanel, POLL_MS);
    }
    if (id === 'drawer-watchlist') loadWatchlist();
    if (id === 'drawer-best') loadBest(bestTab);
  }

  // ---------------------------------------------------------------------------
  // 🏆 Lo mejor: ranking del curador (vigentes, 7 / 30 días) y el top guardado de cada día
  // ---------------------------------------------------------------------------
  let bestTab = 'active';
  let bestRequest = 0;

  function bestItem(post) {
    const wrap = document.createElement('div');
    wrap.className = 'best-item';
    const head = document.createElement('div');
    head.className = 'best-head';
    const reasons = Array.isArray(post.best_reasons) ? post.best_reasons : [];
    head.innerHTML = `<span class="best-rank">#${escapeHtml(String(post.best_rank || ''))}</span>`
      + `<span class="best-reasons">${reasons.map(r => `<span class="best-reason">${escapeHtml(r)}</span>`).join('')}</span>`;
    wrap.appendChild(head);
    wrap.appendChild(createPostElement(post, derive(post)));
    return wrap;
  }

  function dayLabel(iso) {
    const d = new Date(`${iso}T12:00:00Z`);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString('es-CL', { weekday: 'long', day: 'numeric', month: 'long' });
  }

  async function loadBest(tab) {
    bestTab = tab;
    document.querySelectorAll('[data-best]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.best === tab)));
    const list = $('best-list');
    const status = $('best-status');
    if (!list) return;
    const req = ++bestRequest;
    status.textContent = 'Revisando el canal…';
    const url = tab === 'history' ? '/api/best/history?days=14&per_day=5'
      : tab === 'active' ? '/api/best?days=3&limit=20&active=true'
      : `/api/best?days=${encodeURIComponent(tab)}&limit=25`;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (req !== bestRequest) return;
      list.replaceChildren();
      if (tab === 'history') {
        data.forEach(day => {
          const h = document.createElement('h3');
          h.className = 'section-title';
          h.textContent = dayLabel(day.day);
          list.appendChild(h);
          day.posts.forEach(p => list.appendChild(bestItem(p)));
        });
        status.textContent = data.length ? 'El top que el curador guardó cada día, aunque ya hayan expirado.' : 'Aún no hay días guardados: el curador guarda el top cada hora.';
      } else {
        data.forEach(p => list.appendChild(bestItem(p)));
        status.textContent = data.length ? `${data.length} ofertas elegidas.` : 'No hay ofertas destacadas en este período.';
      }
    } catch (err) {
      if (req === bestRequest) status.textContent = 'No se pudo cargar el ranking. Intenta de nuevo en unos segundos.';
    }
  }

  function closeDrawers() {
    let closed = false;
    document.querySelectorAll('.drawer').forEach(d => {
      if (!d.hidden) { d.hidden = true; closed = true; }
    });
    if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
    document.body.style.overflow = '';
    if (closed && lastFocus && lastFocus.focus) lastFocus.focus();
    return closed;
  }

  function closeMenu() {
    const menu = $('tools-menu');
    const btn = $('btn-tools');
    if (menu && !menu.hidden) {
      menu.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------------------
  // Tema y sonido
  // ---------------------------------------------------------------------------
  function currentTheme() {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function updateThemeButton() {
    const btn = $('btn-theme');
    if (btn) btn.setAttribute('aria-label', currentTheme() === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
  }

  function updateSoundButton() {
    const btn = $('btn-sound');
    if (!btn) return;
    btn.setAttribute('aria-pressed', String(state.soundEnabled));
    btn.title = state.soundEnabled ? 'Sonido activado (clic para silenciar)' : 'Sonido silenciado (clic para activar)';
    const label = $('sound-label');
    if (label) label.textContent = state.soundEnabled ? 'Sonido activado' : 'Sonido silenciado';
  }

  // ---------------------------------------------------------------------------
  // Eventos
  // ---------------------------------------------------------------------------
  // ---------------------------------------------------------------------------
  // Búsqueda profunda: todo el catálogo que el radar ya leyó (al escribir) y el
  // buscador propio de cada tienda compatible (con Enter o "Buscar en vivo").
  // ---------------------------------------------------------------------------
  const DEEP_PAGE = 40;
  const deep = { query: '', local: [], live: new Map(), jobId: null, liveQuery: '', pollTimer: null, shown: DEEP_PAGE, seq: 0, liveInfo: null };

  function deepSetStatus(text) { const el = $('deep-status'); if (el) el.textContent = text; }

  function deepProgress(done, total) {
    const wrap = document.querySelector('.deep-progress');
    const bar = $('deep-progress-bar');
    if (!wrap || !bar) return;
    if (!total || done >= total) { wrap.hidden = true; bar.style.width = '0'; return; }
    wrap.hidden = false;
    bar.style.width = `${Math.round((done / total) * 100)}%`;
  }

  function deepMerged() {
    const byId = new Map();
    for (const r of deep.local) byId.set(r.product_id, r);
    if (deep.liveQuery === deep.query) for (const [id, r] of deep.live) byId.set(id, r);
    let rows = [...byId.values()];
    const onlyDiscount = $('deep-only-discount') && $('deep-only-discount').checked;
    if (onlyDiscount) rows = rows.filter(r => Number(r.discount_pct) > 0);
    rows.sort((a, b) => (Number(b.discount_pct) - Number(a.discount_pct)) || (Number(a.best_price) - Number(b.best_price)));
    return rows;
  }

  function deepItemHtml(r) {
    const store = (STORES[r.store_id] && STORES[r.store_id].name) || r.store_name || r.store_id;
    const off = Math.round(Number(r.discount_pct) || 0);
    const imgUrl = sanitizeUrl(r.image_url);
    const img = r.image_url && imgUrl !== '#'
      ? `<img class="deep-img" src="${sanitizeAttrUrl(imgUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
      : '<span class="deep-img-empty" aria-hidden="true"></span>';
    const normal = Number(r.normal_price) > Number(r.best_price) ? `<s>${formatCLP(r.normal_price)}</s>` : '';
    return `<li class="deep-item">
      ${img}
      <div class="deep-main">
        <a class="deep-name" href="${sanitizeAttrUrl(r.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.title)}</a>
        <div class="deep-meta"><span class="deep-store">${escapeHtml(store)}</span>${r.source === 'live' ? '<span class="deep-live">en vivo</span>' : ''}</div>
      </div>
      <div class="deep-price"><strong>${formatCLP(r.best_price)}</strong>${normal}${off > 0 ? `<span class="deep-off${off >= 65 ? ' is-big' : ''}">-${off}%</span>` : ''}</div>
    </li>`;
  }

  function deepRender() {
    const section = $('deep-search');
    if (!section) return;
    if (deep.query.length < 2) { section.hidden = true; return; }
    section.hidden = false;
    const title = $('deep-search-title');
    if (title) title.textContent = `“${deep.query}” en todas las tiendas`;
    const rows = deepMerged();
    $('deep-results').innerHTML = rows.slice(0, deep.shown).map(deepItemHtml).join('');
    const more = $('btn-deep-more');
    if (more) more.hidden = rows.length <= deep.shown;
    const liveBtn = $('btn-deep-live');
    const info = deep.liveInfo;
    if (info && deep.liveQuery === deep.query) {
      if (info.finished) {
        deepSetStatus(`${rows.length} producto(s). Búsqueda en vivo lista: ${info.stores_with_results} de ${info.total_stores} tiendas con resultados.`);
      } else {
        deepSetStatus(`Buscando en vivo… ${info.done_stores} de ${info.total_stores} tiendas (${rows.length} producto(s) hasta ahora).`);
      }
      deepProgress(info.finished ? 1 : info.done_stores, info.finished ? 1 : info.total_stores);
      if (liveBtn) liveBtn.disabled = !info.finished;
    } else {
      deepSetStatus(rows.length
        ? `${rows.length} producto(s) en el catálogo que el radar ya leyó. Presiona Enter para buscar también en vivo en cada tienda.`
        : 'Nada en el catálogo leído. Presiona Enter para buscar en vivo en cada tienda.');
      deepProgress(0, 0);
      if (liveBtn) liveBtn.disabled = false;
    }
  }

  async function deepLoadLocal() {
    const q = deep.query;
    const seq = ++deep.seq;
    if (q.length < 2) { deep.local = []; deepRender(); return; }
    try {
      const res = await fetch(`/api/search?${new URLSearchParams({ q, limit: '300' })}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (seq !== deep.seq) return;   // llegó otra búsqueda mientras tanto
      deep.local = data.results || [];
      deep.shown = DEEP_PAGE;
      deepRender();
    } catch (e) {
      if (seq === deep.seq) deepSetStatus('No se pudo buscar en el catálogo.');
    }
  }

  async function deepPoll() {
    clearTimeout(deep.pollTimer);
    if (!deep.jobId) return;
    const activeJob = deep.jobId;
    const activeQuery = deep.liveQuery;
    try {
      const res = await fetch(`/api/search/live/${encodeURIComponent(activeJob)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (deep.query !== activeQuery || deep.liveQuery !== activeQuery) return;
      deepApplyJob(data);
    } catch (e) {
      if (deep.query === activeQuery) {
        deep.jobId = null;
        deepSetStatus('Se perdió la búsqueda en vivo. Intenta de nuevo.');
        deepProgress(0, 0);
      }
    }
  }

  function deepApplyJob(job) {
    deep.liveInfo = job;
    deep.live = new Map((job.results || []).map(r => [r.product_id, r]));
    deepRender();
    if (!job.finished) deep.pollTimer = setTimeout(deepPoll, 1200);
    else deep.jobId = null;
  }

  async function deepStartLive() {
    const q = deep.query;
    if (q.length < 2) return;
    clearTimeout(deep.pollTimer);
    deep.liveQuery = q;
    deep.live = new Map();
    deep.liveInfo = { finished: false, done_stores: 0, total_stores: 1, stores_with_results: 0 };
    deepRender();
    try {
      const res = await authenticatedFetch('/api/search/live', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q })
      });
      if (deep.query !== q || deep.liveQuery !== q) return;
      if (res.status === 429) { deep.liveInfo = null; deep.liveQuery = ''; deepRender(); deepSetStatus('Hay otras búsquedas en vivo en curso. Intenta en unos segundos.'); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const job = await res.json();
      if (deep.query !== q || deep.liveQuery !== q) return;
      deep.jobId = job.id;
      deepApplyJob(job);
    } catch (e) {
      if (deep.query === q) {
        deep.liveInfo = null; deep.liveQuery = '';
        deepRender();
        deepSetStatus('No se pudo iniciar la búsqueda en vivo.');
      }
    }
  }

  function deepSetQuery(raw) {
    const q = String(raw || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    if (q === deep.query) return;
    deep.query = q;
    deep.shown = DEEP_PAGE;
    if (deep.liveQuery !== q) {
      clearTimeout(deep.pollTimer);
      deep.jobId = null;
      deep.liveQuery = '';
      deep.liveInfo = null;
      deep.live = new Map();
    }
    deepLoadLocal();
  }

  function setupDeepSearch(search) {
    let timer = null;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => deepSetQuery(search.value), 350);
    });
    search.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      clearTimeout(timer);
      deepSetQuery(search.value);
      deepStartLive();
    });
    const live = $('btn-deep-live');
    if (live) live.addEventListener('click', deepStartLive);
    const only = $('deep-only-discount');
    if (only) only.addEventListener('change', () => { deep.shown = DEEP_PAGE; deepRender(); });
    const more = $('btn-deep-more');
    if (more) more.addEventListener('click', () => { deep.shown += DEEP_PAGE; deepRender(); });
    if (search.value) deepSetQuery(search.value);
  }

  function buildSparklineSvg(points, currentPrice) {
    if (!Array.isArray(points) || points.length < 2) return '';
    const prices = points.map(p => Number(p.price)).filter(n => isFinite(n) && n > 0);
    if (prices.length < 2) return '';
    const min = Math.min(...prices, currentPrice || prices[0]);
    const max = Math.max(...prices, currentPrice || prices[0]);
    const span = Math.max(1, max - min);
    const w = 320;
    const h = 64;
    const pad = 6;
    const coords = prices.map((p, idx) => {
      const x = pad + (idx / Math.max(1, prices.length - 1)) * (w - pad * 2);
      const y = h - pad - ((p - min) / span) * (h - pad * 2);
      return [Number(x.toFixed(1)), Number(y.toFixed(1))];
    });
    const linePts = coords.map(c => `${c[0]},${c[1]}`).join(' ');
    const areaPts = `${coords[0][0]},${h} ${linePts} ${coords[coords.length - 1][0]},${h}`;
    const last = coords[coords.length - 1];
    return `
      <svg class="sparkline-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Evolución de precio últimos 30 días">
        <polygon class="sparkline-area" points="${areaPts}"></polygon>
        <polyline class="sparkline-line" points="${linePts}"></polyline>
        <circle class="sparkline-dot" cx="${last[0]}" cy="${last[1]}" r="3.5"></circle>
      </svg>
      <div class="sparkline-labels">
        <span>Mínimo: ${escapeHtml(formatCLP(min))}</span>
        <span>Máximo: ${escapeHtml(formatCLP(max))}</span>
      </div>`;
  }

  function buildHistoryHtml(data, bestPrice) {
    const points = Array.isArray(data && data.points) ? data.points : [];
    const med30 = toNum(data && data.median_30d);
    const min90 = toNum(data && data.min_90d);
    const daysSpan = Math.round(toNum(data && data.days_span));
    const headParts = [];
    if (med30 > 0) headParts.push(`<span>Habitual 30d: <strong>${escapeHtml(formatCLP(med30))}</strong></span>`);
    if (min90 > 0) headParts.push(`<span>Mínimo 90d: <strong>${escapeHtml(formatCLP(min90))}</strong></span>`);
    headParts.push(`<span>${points.length} registro(s)${daysSpan > 0 ? ` en ${daysSpan}d` : ''}</span>`);
    const chart = buildSparklineSvg(points, bestPrice);
    if (!chart) {
      return `<div class="history-head">${headParts.join('')}</div><div class="muted">Todavía estamos juntando muestras de este producto para graficar la tendencia.</div>`;
    }
    return `<div class="history-head">${headParts.join('')}</div>${chart}`;
  }

  async function toggleHistoryChart(btn) {
    const card = btn.closest('.post');
    const box = card && card.querySelector('.history-box');
    const pid = btn.dataset.productId;
    if (!card || !box || !pid) return;
    const opening = box.hidden;
    box.hidden = !opening;
    btn.setAttribute('aria-expanded', String(opening));
    if (!opening || box.dataset.loaded === '1') return;
    box.innerHTML = '<span class="muted">Cargando historial de precios…</span>';
    try {
      let data = historyCache.get(pid);
      if (!data) {
        const res = await fetch(`/api/products/${encodeURIComponent(pid)}/history?days=30`);
        if (!res.ok) throw new Error('no history');
        data = await res.json();
        historyCache.set(pid, data);
      }
      box.innerHTML = buildHistoryHtml(data, toNum(btn.dataset.bestPrice));
      box.dataset.loaded = '1';
    } catch {
      box.innerHTML = '<span class="muted">No hay historial previo registrado para este producto aún.</span>';
      box.dataset.loaded = '1';
    }
  }

  function syncVoteButtonsForDeal(dealId, vote, upvotes, downvotes) {
    document.querySelectorAll(`.votes .vote-btn[data-deal-id="${CSS.escape(dealId)}"]`).forEach(btn => {
      const bar = btn.closest('.votes');
      if (!bar) return;
      bar.querySelectorAll('.vote-btn').forEach(b => {
        b.disabled = true;
        b.classList.toggle('voted', b.dataset.vote === vote);
      });
      const upC = bar.querySelector('.vote-btn.up .vote-count');
      const downC = bar.querySelector('.vote-btn.down .vote-count');
      if (upC) upC.textContent = String(toNum(upvotes));
      if (downC) downC.textContent = String(toNum(downvotes));
    });
  }

  async function submitVote(dealId, vote, reason = null) {
    const body = reason ? { vote, reason } : { vote };
    const res = await authenticatedFetch(`/api/deals/${encodeURIComponent(dealId)}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error('vote failed');
    const data = await res.json();
    syncVoteButtonsForDeal(dealId, vote, data.upvotes, data.downvotes);
    state.votes[dealId] = vote;
    storage.setJSON('glitchhunter_votes', state.votes);
    return data;
  }

  function openRejectReasons(btn) {
    const card = btn.closest('.post');
    const dealId = btn.dataset.dealId;
    if (!card || !dealId) return;
    let bar = card.querySelector('.reject-reasons');
    if (bar) { bar.remove(); return; }
    bar = document.createElement('div');
    bar.className = 'reject-reasons';
    const reasons = [
      ['Precio inflado', 'Precio inflado'],
      ['Es accesorio', 'Es accesorio'],
      ['Sin stock', 'Sin stock'],
      ['No es oferta', 'No es oferta'],
      ['Confirmar 👎', '']
    ];
    bar.innerHTML = '<span>¿Qué falló?</span>' + reasons.map(([label, val]) =>
      `<button type="button" class="reason-chip" data-deal-id="${escapeHtml(dealId)}" data-reason="${escapeHtml(val)}">${escapeHtml(label)}</button>`
    ).join('');
    card.appendChild(bar);
  }

  async function handleCardInteraction(e) {
    const histBtn = e.target.closest('.btn-history');
    if (histBtn) { toggleHistoryChart(histBtn); return; }

    const reasonBtn = e.target.closest('.reason-chip');
    if (reasonBtn) {
      const dealId = reasonBtn.dataset.dealId;
      const reason = reasonBtn.dataset.reason || null;
      const bar = reasonBtn.closest('.reject-reasons');
      if (bar) bar.querySelectorAll('.reason-chip').forEach(b => { b.disabled = true; });
      try {
        await submitVote(dealId, 'down', reason);
        if (bar) bar.remove();
        showToast(reason ? `Gracias: reportado como "${reason}".` : '¡Gracias! Reportaste un falso positivo.', '👎 Voto registrado');
      } catch {
        if (bar) bar.querySelectorAll('.reason-chip').forEach(b => { b.disabled = false; });
        showToast('No se pudo registrar tu voto.', 'Error');
      }
      return;
    }

    const voteBtn = e.target.closest('.vote-btn');
    if (!voteBtn || voteBtn.disabled) return;
    const dealId = voteBtn.dataset.dealId;
    if (!dealId) return;
    if (voteBtn.dataset.vote === 'down') {
      openRejectReasons(voteBtn);
      return;
    }
    const bar = voteBtn.closest('.votes');
    const buttons = bar ? bar.querySelectorAll('.vote-btn') : [voteBtn];
    buttons.forEach(b => { b.disabled = true; });
    try {
      await submitVote(dealId, 'up');
      showToast('¡Gracias! Marcaste la oferta como real.', '👍 Voto registrado');
    } catch {
      buttons.forEach(b => { b.disabled = false; });
      showToast('No se pudo registrar tu voto.', 'Error');
    }
  }

  function applyAdminMode() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('admin') === '1') storage.set('glitchhunter_admin', 'true');
    state.adminMode = params.get('admin') === '1' || storage.get('glitchhunter_admin') === 'true';
    document.querySelectorAll('.admin-only').forEach(el => { el.hidden = !state.adminMode; });
    const lbl = $('admin-toggle-label');
    if (lbl) lbl.textContent = state.adminMode ? 'Ocultar herramientas técnicas' : 'Mostrar herramientas técnicas';
  }

  function setupEvents() {
    // Búsqueda (con pequeño retardo)
    const search = $('search-input');
    let searchTimer = null;
    if (search) {
      search.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          state.filters.search = search.value;
          applyFilters({ resetPage: true });
        }, 120);
      });
      setupDeepSearch(search);
    }

    // Navegación principal simplificada
    const navFeed = $('btn-nav-feed');
    if (navFeed) navFeed.addEventListener('click', () => { closeDrawers(); window.scrollTo({ top: 0, behavior: 'smooth' }); });
    const navCompare = $('btn-compare');
    if (navCompare) navCompare.addEventListener('click', () => {
      closeDrawers();
      if (search) {
        search.focus();
        search.scrollIntoView({ behavior: 'smooth', block: 'center' });
        showToast('Escribe un producto (ej: iPhone, RTX, notebook) y presiona Enter para comparar entre tiendas.', '🔍 Comparar tiendas', 3500);
      }
    });

    // Atajo "/" para buscar
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (closeMenu()) { $('btn-tools').focus(); return; }
        closeDrawers();
        return;
      }
      const tag = (e.target && e.target.tagName) || '';
      if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        if (search) search.focus();
      }
    });

    // Filtros: segmentados, chips, precio, respaldo, watchlist
    $('filters-panel').addEventListener('click', (e) => {
      const kindBtn = e.target.closest('.seg-btn[data-kind]');
      const minBtn = e.target.closest('.seg-btn[data-min]');
      const maxPriceBtn = e.target.closest('.seg-btn[data-max-price]');
      const storeChip = e.target.closest('.chip[data-store]');
      const catChip = e.target.closest('.chip[data-category]');
      if (kindBtn) state.filters.kind = kindBtn.dataset.kind;
      else if (minBtn) state.filters.minDiscount = Number(minBtn.dataset.min) || 0;
      else if (maxPriceBtn) state.filters.maxPrice = Number(maxPriceBtn.dataset.maxPrice) || 0;
      else if (storeChip) {
        const id = storeChip.dataset.store;
        state.filters.stores = state.filters.stores.includes(id)
          ? state.filters.stores.filter(s => s !== id)
          : [...state.filters.stores, id];
      } else if (catChip) {
        const c = catChip.dataset.category;
        state.filters.category = state.filters.category === c ? '' : c;
      } else return;
      applyFilters({ resetPage: true });
      if (!catChip && !maxPriceBtn) loadMatching();
    });

    const vfCheck = $('filter-verified');
    if (vfCheck) vfCheck.addEventListener('change', (e) => {
      state.filters.verifiedOnly = e.target.checked;
      applyFilters({ resetPage: true });
    });
    $('filter-watchlist').addEventListener('change', (e) => {
      state.filters.watchlistOnly = e.target.checked;
      applyFilters({ resetPage: true });
    });
    $('filter-hide-expired').addEventListener('change', (e) => {
      state.filters.hideExpired = e.target.checked;
      applyFilters({ resetPage: true });
    });
    $('btn-clear-filters').addEventListener('click', clearFilters);
    $('btn-empty-clear').addEventListener('click', clearFilters);
    const feedMoreBtn = $('btn-feed-more');
    if (feedMoreBtn) feedMoreBtn.addEventListener('click', () => {
      state.pageLimit += PAGE_STEP;
      applyFilters();
    });

    $('sort-select').addEventListener('change', (e) => {
      state.filters.sortBy = e.target.value;
      applyFilters({ resetPage: true });
    });

    // Mostrar/ocultar filtros en móvil
    $('btn-toggle-filters').addEventListener('click', () => {
      const panel = $('filters-panel');
      const open = !panel.classList.contains('is-open');
      panel.classList.toggle('is-open', open);
      $('btn-toggle-filters').setAttribute('aria-expanded', String(open));
    });

    // Píldora de ofertas nuevas
    $('new-posts-pill').addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
      state.unseenNew = 0;
      updateNewPill();
    });
    window.addEventListener('scroll', () => {
      if (state.unseenNew && window.scrollY < 120) {
        state.unseenNew = 0;
        updateNewPill();
      }
    }, { passive: true });

    // Paneles
    $('btn-watchlist').addEventListener('click', () => openDrawer('drawer-watchlist'));
    $('btn-status').addEventListener('click', () => openDrawer('drawer-status'));
    const menuStatus = $('btn-menu-status');
    if (menuStatus) menuStatus.addEventListener('click', () => openDrawer('drawer-status'));
    $('btn-best').addEventListener('click', () => openDrawer('drawer-best'));
    document.querySelectorAll('[data-best]').forEach(b => b.addEventListener('click', () => loadBest(b.dataset.best)));
    document.querySelectorAll('.drawer').forEach(d => {
      d.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) closeDrawers(); });
    });

    $('watchlist-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('watchlist-input');
      const priceInput = $('watchlist-max-price');
      const value = input.value;
      const cap = priceInput ? priceInput.value : '';
      input.value = '';
      if (priceInput) priceInput.value = '';
      addWatchlist(value, cap);
    });
    $('watchlist-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.wl-remove');
      if (btn) removeWatchlist(btn.dataset.keyword);
    });

    // Menú Herramientas / Modo técnico
    const toggleAdminBtn = $('btn-toggle-admin');
    if (toggleAdminBtn) toggleAdminBtn.addEventListener('click', () => {
      storage.set('glitchhunter_admin', state.adminMode ? 'false' : 'true');
      applyAdminMode();
      closeMenu();
    });
    $('btn-tools').addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = $('tools-menu');
      const open = menu.hidden;
      menu.hidden = !open;
      $('btn-tools').setAttribute('aria-expanded', String(open));
      if (open) { const first = menu.querySelector('.menu-item'); if (first) first.focus(); }
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.menu-wrap')) closeMenu();
    });

    // Escanear ahora
    const scanBtn = $('btn-force-scan');
    scanBtn.addEventListener('click', async () => {
      closeMenu();
      if (scanBtn.disabled) return;
      scanBtn.disabled = true;
      showToast('Revisando las tiendas… puede tardar un poco.', '🔄 Escaneando', 3000);
      try {
        const res = await authenticatedFetch('/api/scan', { method: 'POST' });
        if (res.status === 409) {
          const data = await res.json().catch(() => ({}));
          showToast(data.message || 'Ya hay un escaneo en curso.', 'Escáner ocupado');
        } else if (res.ok) {
          const added = await fetchNewPosts();
          if (!added.length) showToast('Escaneo terminado: no hay ofertas nuevas.', 'Listo');
        } else if (res.status === 401) {
          showToast('Clave de API no válida o ausente.', 'No autorizado');
        } else {
          showToast('El escaneo falló.', 'Error');
        }
      } catch {
        showToast('No se pudo conectar con el radar.', 'Error');
      } finally {
        scanBtn.disabled = false;
      }
    });

    // Clave de API
    $('btn-api-key').addEventListener('click', () => {
      closeMenu();
      const entered = window.prompt('Clave de API del servidor (API_AUTH_KEY). Déjala vacía para borrarla:', getStoredApiKey());
      if (entered !== null) {
        setStoredApiKey(entered);
        showToast(getStoredApiKey() ? 'Clave guardada en este navegador.' : 'Clave borrada.', 'Clave de API');
      }
    });

    // Oferta de prueba (con advertencia: publica en el canal real)
    $('btn-inject-glitch').addEventListener('click', async () => {
      closeMenu();
      const ok = window.confirm(
        '🧪 Simular una oferta de PRUEBA\n\n' +
        'Se mostrará una tarjeta falsa marcada "PRUEBA" solo en esta pantalla, para comprobar avisos y sonido. ' +
        'No se guarda en el canal y desaparece al recargar.\n\n¿Continuar?'
      );
      if (!ok) return;
      try {
        const res = await authenticatedFetch('/api/test-glitch', { method: 'POST' });
        if (res.ok) {
          const data = await res.json().catch(() => null);
          if (!showTestCard(data)) showToast('Simulación enviada.', '🧪 Prueba');
        }
        else if (res.status === 403) showToast('Las ofertas de prueba están deshabilitadas en este servidor.', 'No permitido');
        else if (res.status === 401) showToast('Se necesita la clave de API.', 'No autorizado');
        else showToast('No se pudo publicar la oferta de prueba.', 'Error');
      } catch {
        showToast('No se pudo conectar con el radar.', 'Error');
      }
    });

    // Sonido
    $('btn-sound').addEventListener('click', () => {
      state.soundEnabled = !state.soundEnabled;
      storage.set('glitchhunter_sound', state.soundEnabled ? 'true' : 'false');
      updateSoundButton();
      if (state.soundEnabled) sound.ding();
    });

    // Tema
    $('btn-theme').addEventListener('click', () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      storage.set('glitchhunter_theme', next);
      updateThemeButton();
    });

    // Votos 👍/👎 + Gráfico 30d en canal principal y en "Lo mejor"
    $('feed-list').addEventListener('click', handleCardInteraction);
    const bestListEl = $('best-list');
    if (bestListEl) bestListEl.addEventListener('click', handleCardInteraction);

    // Pausar polling cuando la pestaña está oculta y sincronizar al volver
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        initWebSocket();
        fetchNewPosts({ quiet: true });
        fetchStatusChanges();
      }
    });

    // Desbloquear audio al primer gesto
    document.addEventListener('pointerdown', () => sound.init(), { once: true });
  }

  /** Solo actualiza el texto "hace N min" (la hora original queda fija). */
  function refreshRelativeTimes() {
    if (document.visibilityState === 'hidden') return;
    document.querySelectorAll('#feed-list time.rel-time').forEach(t => {
      const iso = t.getAttribute('datetime');
      const txt = formatRelative(iso);
      if (t.textContent !== txt) t.textContent = txt;
    });
    updateSummary([...state.posts.values()].filter(matches).length);
  }

  // ---------------------------------------------------------------------------
  // Inicio
  // ---------------------------------------------------------------------------
  function init() {
    applyAdminMode();
    updateApiKeyUI();
    updateSoundButton();
    updateThemeButton();
    syncFilterControls();
    setupEvents();
    loadStoreDirectory();
    // La vigencia inicial ya viene en /api/feed; esta primera consulta solo fija el punto de partida
    loadFeed().then(() => { fetchStatusChanges(); initWebSocket(); });
    loadWatchlist();
    // Respaldo cada 30 s solo si WS cayó y la pestaña está visible.
    setInterval(() => {
      if (document.visibilityState === 'hidden' || state.wsConnected) return;
      fetchNewPosts();
    }, POLL_MS);
    setInterval(refreshRelativeTimes, 60000);
    setInterval(() => {
      if (document.visibilityState === 'hidden') return;
      fetchStatusChanges();
    }, STATUS_POLL_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
