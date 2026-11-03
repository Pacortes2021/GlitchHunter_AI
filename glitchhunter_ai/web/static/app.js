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
  const STORES = {
    falabella: { name: 'Falabella', color: '#3f8f3f' },
    ripley: { name: 'Ripley', color: '#7e22ce' },
    paris: { name: 'Paris', color: '#0284c7' },
    lider: { name: 'Líder', color: '#2563eb' },
    mercadolibre: { name: 'Mercado Libre', color: '#ca8a04' },
    pcfactory: { name: 'PC Factory', color: '#dc2626' },
    sodimac: { name: 'Sodimac', color: '#f97316' },
    solotodo: { name: 'SoloTodo', color: '#0d9488' }
  };
  const DEFAULT_STORE_COLOR = '#64748b';
  const INITIAL_LIMIT = 500;
  const POLL_MS = 30000;
  const NEW_HIGHLIGHT_MS = 8000;
  const MAX_CATEGORY_CHIPS = 10;

  const DEFAULT_FILTERS = Object.freeze({
    kind: 'all',        // 'all' | 'glitch' | 'deal'
    minDiscount: 0,     // 0 | 50 | 65 | 80
    stores: [],         // ids de tienda seleccionados (vacío = todas)
    category: '',       // categoría normalizada ('' = todas)
    search: '',
    watchlistOnly: false,
    hideExpired: false, // ocultar ofertas que subieron de precio, se agotaron o no se confirman
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
    const saved = storage.getJSON('glitchhunter_filters_v2', {});
    const f = { ...DEFAULT_FILTERS, stores: [] };
    if (saved && typeof saved === 'object') {
      if (['all', 'glitch', 'deal'].includes(saved.kind)) f.kind = saved.kind;
      if ([0, 50, 65, 80].includes(saved.minDiscount)) f.minDiscount = saved.minDiscount;
      if (Array.isArray(saved.stores)) f.stores = saved.stores.filter(s => typeof s === 'string').slice(0, 20);
      if (typeof saved.category === 'string') f.category = saved.category.slice(0, 60);
      if (typeof saved.watchlistOnly === 'boolean') f.watchlistOnly = saved.watchlistOnly;
      if (typeof saved.hideExpired === 'boolean') f.hideExpired = saved.hideExpired;
      if (['recent', 'discount', 'savings'].includes(saved.sortBy)) f.sortBy = saved.sortBy;
    }
    return f; // la búsqueda no se recuerda a propósito
  }

  function saveFilters() {
    const { search, ...rest } = state.filters;
    storage.setJSON('glitchhunter_filters_v2', rest);
  }

  // ---------------------------------------------------------------------------
  // Estado
  // ---------------------------------------------------------------------------
  const state = {
    posts: new Map(),     // feed_id -> { post, el, derived }
    byProduct: new Map(), // product.id -> feed_id de su publicación más reciente
    lastFeedId: 0,
    loaded: false,
    filters: loadFilters(),
    appliedSort: 'recent',
    soundEnabled: storage.get('glitchhunter_sound') !== 'false',
    wsConnected: false,
    unseenNew: 0,
    votes: storage.getJSON('glitchhunter_votes', {}) || {},
    statusSince: null,    // marca de tiempo del servidor para pedir solo cambios de vigencia nuevos
    watchlist: []
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
    // Solo HTTP, HTTPS o rutas relativas seguras
    if (/^https?:\/\/[^\s"'<>]+$/i.test(trimmed) || /^\/[^\s"'<>]*$/.test(trimmed)) {
      return trimmed.replace(/"/g, '%22').replace(/'/g, '%27');
    }
    return '#';
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
    return first.slice(0, 40);
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

  // ---------------------------------------------------------------------------
  // Tarjeta de publicación (se crea UNA vez por feed_id)
  // ---------------------------------------------------------------------------
  function createPostElement(post, d) {
    const product = post.product || {};
    const verdict = post.verdict || {};
    // El id de voto es siempre alert_id tal como lo entrega el servidor (nunca se construye aquí)
    const dealId = post.alert_id !== undefined && post.alert_id !== null && post.alert_id !== '' ? String(post.alert_id) : '';
    const store = STORES[d.storeId];
    const storeColor = store ? store.color : DEFAULT_STORE_COLOR; // solo colores propios, nunca del servidor

    const safeTitle = escapeHtml(product.title || 'Producto sin título');
    const safeStore = escapeHtml(d.storeName);
    const safeUrl = sanitizeUrl(post.alert_url || product.url);
    const rawCart = sanitizeUrl(post.add_to_cart_url || product.add_to_cart_url);
    const safeCartUrl = rawCart !== '#' && rawCart !== safeUrl ? rawCart : '';
    const rawImage = product.image_url ? String(product.image_url).trim() : '';
    const safeImage = /^https?:\/\//i.test(rawImage) ? sanitizeUrl(rawImage) : '';
    const safeCategory = escapeHtml(capitalize(d.category));

    const tags = [];
    tags.push(d.isGlitch
      ? '<span class="tag tag-glitch">⚡ Error de precio</span>'
      : '<span class="tag tag-deal">🏷️ Oferta</span>');
    if (verdict.is_missing_zero) tags.push('<span class="tag tag-zero" title="El precio parece tener un cero de menos">Falta un cero</span>');
    // Nivel de riesgo según los casos chilenos documentados (ver core/price_signals.py)
    if (post.risk_tier === 'irrisorio') {
      tags.push('<span class="tag tag-risk-high" title="Con descuentos de 95% o más, las tiendas casi siempre anulan la compra y los tribunales suelen darles la razón">⚠️ Precio irrisorio · probable anulación</span>');
    } else if (post.risk_tier === 'plausible') {
      tags.push('<span class="tag tag-risk-ok" title="En este rango están la mayoría de los errores que se respetaron o compensaron">✅ Error plausible</span>');
    }
    if (toNum(post.bulk_price_count) >= 4) tags.push(`<span class="tag tag-bulk" title="Varios productos distintos de esta tienda aparecieron al mismo precio: típico de un error al cargar el catálogo">📦 Mismo precio en ${toNum(post.bulk_price_count)} productos</span>`);
    if (post.odd_price) tags.push('<span class="tag tag-odd" title="Los precios del retail terminan en …90 o …00; una cifra rara suele venir de un descuento mal calculado">🔢 Cifra inusual</span>');
    if (d.hasCardPrice) tags.push('<span class="tag tag-card">💳 Precio tarjeta</span>');
    if (post.__isTest) tags.unshift('<span class="tag tag-test" title="Simulación: no se guarda y desaparece al recargar">🧪 PRUEBA</span>');
    if (post.is_watchlist_match) tags.push('<span class="tag tag-watch">⭐ Tu watchlist</span>');
    if (post.verification_level === 'COMPRABLE') tags.push('<span class="tag tag-verified" title="Confirmado con stock y enlace de compra directa">✓ Comprable</span>');
    else if (post.verification_level === 'VERIFICADO') tags.push('<span class="tag tag-verified" title="Confirmado en la tienda con stock y precio">✓ Verificado</span>');

    let marketHtml = '';
    if (toNum(post.market_median_price) > 0) {
      const stores = toNum(post.market_stores_count);
      const mPct = post.market_discount_pct;
      const mPctTxt = (mPct !== null && mPct !== undefined && isFinite(Number(mPct)))
        ? ` · ${Number(mPct) > 0 ? `${Math.round(Number(mPct))}% más barato` : `${Math.abs(Math.round(Number(mPct)))}% más caro`}`
        : '';
      marketHtml = `<div class="market">🌐 Otras tiendas: <strong>${formatCLP(post.market_median_price)}</strong>${stores ? ` (${stores} tiendas)` : ''}${escapeHtml(mPctTxt)}</div>`;
    }

    // Consejos prácticos (texto fijo, sin datos del servidor)
    let tipsHtml = '';
    if (post.risk_tier === 'irrisorio') {
      tipsHtml = '<p class="tips">Casos así casi siempre se anulan con reembolso. Si compras: 1 sola unidad, guarda captura del precio y del comprobante.</p>';
    } else if (post.risk_tier === 'plausible') {
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
        ${safeImage
          ? `<img src="${safeImage}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />`
          : '<span class="img-fallback">🛍️</span>'}
      </a>
      <div class="post-body">
        <div class="post-meta">
          <span class="store-name"><span class="chip-dot" style="background:${storeColor}"></span>${safeStore}</span>
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
        ${dealId && !post.__isTest ? `<div class="votes" role="group" aria-label="¿Es una oferta real?">
          <span class="votes-label">¿Es real?</span>
          <button type="button" class="vote-btn up${voted === 'up' ? ' voted' : ''}" data-vote="up" data-deal-id="${escapeHtml(dealId)}" aria-label="Sí, es una oferta real" ${voted ? 'disabled' : ''}>👍 <span class="vote-count">${up}</span></button>
          <button type="button" class="vote-btn down${voted === 'down' ? ' voted' : ''}" data-vote="down" data-deal-id="${escapeHtml(dealId)}" aria-label="No, es un falso positivo" ${voted ? 'disabled' : ''}>👎 <span class="vote-count">${down}</span></button>
        </div>` : ''}
      </div>`;

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
    if (f.stores.length) n++;
    if (f.category) n++;
    if (f.watchlistOnly) n++;
    if (f.hideExpired) n++;
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

  function applyFilters() {
    if (state.appliedSort !== state.filters.sortBy) reorderNodes();
    let visible = 0;
    state.posts.forEach(entry => {
      const show = matches(entry);
      entry.el.hidden = !show;
      if (show) visible++;
    });
    updateSummary(visible);
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
          const color = (STORES[id] && STORES[id].color) || DEFAULT_STORE_COLOR;
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
        ? cats.map(([c, n]) => `<button type="button" class="chip" data-category="${escapeHtml(c)}" aria-pressed="${state.filters.category === c}">${escapeHtml(capitalize(c))} <span class="chip-count">${n}</span></button>`).join('')
        : '<span class="muted small">Sin categorías aún</span>';
    }
  }

  function syncFilterControls() {
    const f = state.filters;
    document.querySelectorAll('.seg-btn[data-kind]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.kind === f.kind)));
    document.querySelectorAll('.seg-btn[data-min]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.min) === f.minDiscount)));
    document.querySelectorAll('#store-chips .chip').forEach(b => b.setAttribute('aria-pressed', String(f.stores.includes(b.dataset.store))));
    document.querySelectorAll('#category-chips .chip').forEach(b => b.setAttribute('aria-pressed', String(f.category === b.dataset.category)));
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
    applyFilters();
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
  function registerPosts(posts) {
    const added = [];
    const replaced = [];
    [...posts]
      .filter(p => p && p.product && Number.isFinite(Number(p.feed_id)))
      .sort((a, b) => a.feed_id - b.feed_id)
      .forEach(post => {
        if (state.posts.has(post.feed_id) || post.feed_id <= state.lastFeedId) return;
        const pid = post.product.id !== undefined && post.product.id !== null ? String(post.product.id) : '';
        if (pid && state.byProduct.has(pid)) {
          const oldId = state.byProduct.get(pid);
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
  // WebSocket en vivo con reconexión
  // ---------------------------------------------------------------------------
  let socket = null;
  let reconnectTimer = null;

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

  function initWebSocket() {
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/glitches`);
      socket.onopen = () => {
        setLiveStatus(true);
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        fetchNewPosts({ quiet: true }); // ponerse al día tras una reconexión
      };
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && (data.type === 'feed_updated' || data.type === 'new_glitch' || (data.alert_id && data.product))) {
            fetchNewPosts();
          } else if (data && data.type === 'feed_status') {
            fetchStatusChanges();
          }
        } catch { /* pings u otros mensajes */ }
      };
      socket.onclose = () => {
        setLiveStatus(false);
        socket = null;
        if (!reconnectTimer) {
          reconnectTimer = setTimeout(() => { reconnectTimer = null; initWebSocket(); }, 4000);
        }
      };
      socket.onerror = () => setLiveStatus(false);
    } catch {
      setLiveStatus(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Watchlist
  // ---------------------------------------------------------------------------
  function renderWatchlist() {
    const list = $('watchlist-list');
    const empty = $('watchlist-empty');
    const count = $('watchlist-count');
    if (count) { count.textContent = String(state.watchlist.length); count.hidden = state.watchlist.length === 0; }
    if (!list) return;
    list.innerHTML = state.watchlist.map(kw => `
      <li class="wl-item"><span>${escapeHtml(kw)}</span>
        <button type="button" class="wl-remove" data-keyword="${escapeHtml(kw)}" aria-label="Quitar ${escapeHtml(kw)} de la watchlist">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button></li>`).join('');
    if (empty) empty.hidden = state.watchlist.length > 0;
  }

  async function loadWatchlist() {
    try {
      const res = await fetch('/api/watchlist');
      if (!res.ok) return;
      const data = await res.json();
      state.watchlist = Array.isArray(data.keywords) ? data.keywords.map(String) : [];
      renderWatchlist();
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

  async function addWatchlist(keyword) {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return;
    if (kw.length < 2 || kw.length > 60) {
      showToast('La palabra debe tener entre 2 y 60 caracteres.', 'Watchlist');
      return;
    }
    if (state.watchlist.includes(kw)) { showToast(`"${kw}" ya está en tu watchlist.`, 'Watchlist'); return; }
    try {
      const res = await authenticatedFetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keyword: kw })
      });
      if (res.ok) {
        const data = await res.json();
        state.watchlist = Array.isArray(data.keywords) ? data.keywords.map(String) : state.watchlist;
        renderWatchlist();
        showToast(`Te avisaremos de ofertas de "${kw}".`, '⭐ Agregado a la watchlist');
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
        state.watchlist = Array.isArray(data.keywords) ? data.keywords.map(String) : state.watchlist.filter(k => k !== keyword);
        renderWatchlist();
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
    DEGRADED: ['Lenta / con errores', 'st-warn'],
    EMPTY_RESPONSE: ['Sin productos', 'st-warn'],
    BLOCKED: ['Bloqueada', 'st-bad'],
    OFFLINE: ['Caída', 'st-bad'],
    IDLE: ['Sin escanear', 'st-idle']
  };

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
      const storesH = health && Array.isArray(health.stores) ? health.stores : [];
      hl.innerHTML = storesH.length
        ? storesH.map(s => {
            const [label, cls] = HEALTH_LABELS[s.status] || [String(s.status || '–'), 'st-idle'];
            const lat = Math.round(toNum(s.avg_latency_ms || s.latency_ms));
            const when = s.last_scan_at ? formatRelative(s.last_scan_at) : 'nunca';
            return `<li class="health-item"><div>${escapeHtml(s.store_name || s.store_id)}
                <small>${toNum(s.items_scanned).toLocaleString('es-CL')} productos · ${lat} ms · último escaneo ${escapeHtml(when)}</small></div>
                <span class="status-badge ${cls}">${escapeHtml(label)}</span></li>`;
          }).join('')
        : '<li class="muted small">Aún no hay escaneos desde que se inició el servidor.</li>';
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
  function setupEvents() {
    // Búsqueda (con pequeño retardo)
    const search = $('search-input');
    let searchTimer = null;
    if (search) {
      search.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          state.filters.search = search.value;
          applyFilters();
        }, 120);
      });
    }

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

    // Filtros: segmentados, chips, watchlist
    $('filters-panel').addEventListener('click', (e) => {
      const kindBtn = e.target.closest('.seg-btn[data-kind]');
      const minBtn = e.target.closest('.seg-btn[data-min]');
      const storeChip = e.target.closest('.chip[data-store]');
      const catChip = e.target.closest('.chip[data-category]');
      if (kindBtn) state.filters.kind = kindBtn.dataset.kind;
      else if (minBtn) state.filters.minDiscount = Number(minBtn.dataset.min) || 0;
      else if (storeChip) {
        const id = storeChip.dataset.store;
        state.filters.stores = state.filters.stores.includes(id)
          ? state.filters.stores.filter(s => s !== id)
          : [...state.filters.stores, id];
      } else if (catChip) {
        const c = catChip.dataset.category;
        state.filters.category = state.filters.category === c ? '' : c;
      } else return;
      applyFilters();
    });

    $('filter-watchlist').addEventListener('change', (e) => {
      state.filters.watchlistOnly = e.target.checked;
      applyFilters();
    });
    $('filter-hide-expired').addEventListener('change', (e) => {
      state.filters.hideExpired = e.target.checked;
      applyFilters();
    });
    $('btn-clear-filters').addEventListener('click', clearFilters);
    $('btn-empty-clear').addEventListener('click', clearFilters);

    $('sort-select').addEventListener('change', (e) => {
      state.filters.sortBy = e.target.value;
      applyFilters();
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
    document.querySelectorAll('.drawer').forEach(d => {
      d.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) closeDrawers(); });
    });

    $('watchlist-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('watchlist-input');
      const value = input.value;
      input.value = '';
      addWatchlist(value);
    });
    $('watchlist-list').addEventListener('click', (e) => {
      const btn = e.target.closest('.wl-remove');
      if (btn) removeWatchlist(btn.dataset.keyword);
    });

    // Menú Herramientas
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

    // Votos 👍/👎 (delegación)
    $('feed-list').addEventListener('click', async (e) => {
      const btn = e.target.closest('.vote-btn');
      if (!btn || btn.disabled) return;
      const dealId = btn.dataset.dealId;
      const vote = btn.dataset.vote === 'down' ? 'down' : 'up';
      if (!dealId) return;
      const bar = btn.closest('.votes');
      const buttons = bar ? bar.querySelectorAll('.vote-btn') : [btn];
      buttons.forEach(b => { b.disabled = true; });
      try {
        const res = await authenticatedFetch(`/api/deals/${encodeURIComponent(dealId)}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ vote })
        });
        if (!res.ok) throw new Error('vote failed');
        const data = await res.json();
        if (bar) {
          const upC = bar.querySelector('.vote-btn.up .vote-count');
          const downC = bar.querySelector('.vote-btn.down .vote-count');
          if (upC) upC.textContent = String(toNum(data.upvotes));
          if (downC) downC.textContent = String(toNum(data.downvotes));
        }
        btn.classList.add('voted');
        state.votes[dealId] = vote;
        storage.setJSON('glitchhunter_votes', state.votes);
        showToast(vote === 'up' ? '¡Gracias! Marcaste la oferta como real.' : '¡Gracias! Reportaste un falso positivo.', vote === 'up' ? '👍 Voto registrado' : '👎 Voto registrado');
      } catch {
        buttons.forEach(b => { b.disabled = false; });
        showToast('No se pudo registrar tu voto.', 'Error');
      }
    });

    // Desbloquear audio al primer gesto
    document.addEventListener('pointerdown', () => sound.init(), { once: true });
  }

  /** Solo actualiza el texto "hace N min" (la hora original queda fija). */
  function refreshRelativeTimes() {
    document.querySelectorAll('#feed-list time.rel-time').forEach(t => {
      const iso = t.getAttribute('datetime');
      const txt = formatRelative(iso);
      if (t.textContent !== txt) t.textContent = txt;
    });
    updateSummary([...state.posts.values()].filter(e => !e.el.hidden).length);
  }

  // ---------------------------------------------------------------------------
  // Inicio
  // ---------------------------------------------------------------------------
  function init() {
    updateApiKeyUI();
    updateSoundButton();
    updateThemeButton();
    syncFilterControls();
    setupEvents();
    // La vigencia inicial ya viene en /api/feed; esta primera consulta solo fija el punto de partida
    loadFeed().then(() => { fetchStatusChanges(); initWebSocket(); });
    loadWatchlist();
    // Respaldo cada 30 s por si falla el WebSocket: solo pide publicaciones NUEVAS (after_id).
    setInterval(() => fetchNewPosts(), POLL_MS);
    setInterval(refreshRelativeTimes, 60000);
    setInterval(fetchStatusChanges, STATUS_POLL_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
