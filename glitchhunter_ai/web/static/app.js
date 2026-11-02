/**
 * GlitchHunter AI - Frontend Radar Dashboard (Dos Columnas Blanco & Verde)
 * - Columna 1: Errores de precio & Glitches críticos (≥65% o Missing Zero).
 * - Columna 2: Ofertas comerciales moderadas (40% a 64%).
 * - Diseño minimalista blanco y verde esmeralda.
 * - Desduplicación estricta y preservación exacta de la posición de scroll.
 */

(() => {
  'use strict';

  // Configuración de tiendas retail
  const STORES = {
    falabella: { name: 'Falabella', color: '#059669', badgeBg: '#ecfdf5', badgeText: '#047857' },
    ripley: { name: 'Ripley', color: '#9333ea', badgeBg: '#faf5ff', badgeText: '#7e22ce' },
    paris: { name: 'Paris', color: '#0284c7', badgeBg: '#f0f9ff', badgeText: '#0369a1' },
    lider: { name: 'Líder', color: '#2563eb', badgeBg: '#eff6ff', badgeText: '#1d4ed8' },
    mercadolibre: { name: 'Mercado Libre', color: '#ca8a04', badgeBg: '#fefce8', badgeText: '#a16207' },
    pcfactory: { name: 'PC Factory', color: '#dc2626', badgeBg: '#fef2f2', badgeText: '#b91c1c' },
    solotodo: { name: 'SoloTodo', color: '#10b981', badgeBg: '#ecfdf5', badgeText: '#065f46' }
  };

  const FALLBACK_IMAGE = `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200" fill="%23f1f5f9"><rect width="200" height="200" fill="%23f1f5f9"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="%2394a3b8" font-family="sans-serif" font-size="12">Sin imagen</text></svg>`;

  // Estado global
  const state = {
    glitches: [],       // Columna 1: ≥ 65% o Missing Zero
    commercialDeals: [], // Columna 2: 40% a 64%
    filters: {
      store: 'all',
      search: '',
      sortBy: 'recent',
      col2MinDiscount: 40.0
    },
    soundEnabled: localStorage.getItem('glitchhunter_sound') !== 'false',
    wsConnected: false,
    audioCtx: null
  };

  // Motor de sonido sutil
  class SoundManager {
    constructor() {
      this.ctx = null;
    }

    init() {
      if (!this.ctx) {
        const AudioClass = window.AudioContext || window.webkitAudioContext;
        if (AudioClass) this.ctx = new AudioClass();
      }
      if (this.ctx && this.ctx.state === 'suspended') {
        this.ctx.resume().catch(() => {});
      }
    }

    playDing() {
      if (!state.soundEnabled) return;
      try {
        this.init();
        if (!this.ctx) return;

        const now = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();

        osc.type = 'sine';
        osc.frequency.setValueAtTime(784, now); // Nota G5
        osc.frequency.exponentialRampToValueAtTime(1046.5, now + 0.14); // C6

        gain.gain.setValueAtTime(0.01, now);
        gain.gain.linearRampToValueAtTime(0.12, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.32);

        osc.connect(gain);
        gain.connect(this.ctx.destination);

        osc.start(now);
        osc.stop(now + 0.33);
      } catch (e) {
        // Bloqueo de audio por navegador
      }
    }
  }

  const sound = new SoundManager();

  // Formato de moneda chilena
  function formatCLP(val) {
    if (!val || isNaN(val)) return '$0 CLP';
    return new Intl.NumberFormat('es-CL', {
      style: 'currency',
      currency: 'CLP',
      maximumFractionDigits: 0
    }).format(val) + ' CLP';
  }

  // Sanitización estricta anti-XSS
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
    // Permitir exclusivamente HTTP, HTTPS o rutas relativas seguras
    if (/^https?:\/\/[^\s"'<>]+$/i.test(trimmed) || /^\/[^\s"'<>]*$/.test(trimmed)) {
      return trimmed.replace(/"/g, '%22').replace(/'/g, '%27');
    }
    return '#';
  }

  function formatTime(isoStr) {
    try {
      const d = new Date(isoStr);
      return d.toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }

  // Toast de notificaciones (Blanco & Verde)
  function showToast(text, title = 'GlitchHunter') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const safeTitle = escapeHtml(title);
    const safeText = escapeHtml(text);

    const el = document.createElement('div');
    el.className = 'bg-white border border-emerald-200 rounded-xl p-3.5 shadow-lg flex items-start gap-3 w-80 pointer-events-auto transition-all duration-200';
    el.innerHTML = `
      <div class="w-2 h-2 rounded-full bg-emerald-500 mt-1.5 flex-shrink-0"></div>
      <div class="flex-1">
        <div class="text-xs font-bold text-slate-900">${safeTitle}</div>
        <div class="text-xs text-slate-600 mt-0.5 leading-snug">${safeText}</div>
      </div>
      <button class="text-slate-400 hover:text-slate-600 toast-x">
        <i data-lucide="x" class="w-3.5 h-3.5"></i>
      </button>
    `;

    container.appendChild(el);
    if (window.lucide) window.lucide.createIcons({ root: el });

    const remove = () => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(-4px)';
      setTimeout(() => el.remove(), 200);
    };

    el.querySelector('.toast-x').addEventListener('click', remove);
    setTimeout(remove, 4000);
  }

  // Actualizar indicadores numéricos y contadores
  function updateKPIs() {
    const glitchesCount = state.glitches.length;
    const dealsCount = state.commercialDeals.length;
    const totalItems = glitchesCount + dealsCount;

    let totalSavings = 0;
    state.glitches.forEach(g => {
      const best = g.product.card_price && g.product.card_price < g.product.current_price
        ? g.product.card_price
        : g.product.current_price;
      totalSavings += Math.max(0, g.product.normal_price - best);
    });

    const kpiGlitches = document.getElementById('kpi-total-glitches');
    const kpiDeals = document.getElementById('kpi-total-deals');
    const kpiSavings = document.getElementById('kpi-total-savings');

    if (kpiGlitches) kpiGlitches.textContent = glitchesCount.toString();
    if (kpiDeals) kpiDeals.textContent = dealsCount.toString();
    if (kpiSavings) kpiSavings.textContent = formatCLP(totalSavings);

    // Contadores por tienda en la barra de filtros superior
    const counts = { all: totalItems, falabella: 0, ripley: 0, paris: 0, lider: 0, mercadolibre: 0, pcfactory: 0 };

    [...state.glitches, ...state.commercialDeals].forEach(item => {
      const s = item.product.store_id;
      if (counts[s] !== undefined) counts[s]++;
    });

    for (const [key, count] of Object.entries(counts)) {
      const el = document.getElementById(`count-${key}`);
      if (el) el.textContent = count;
    }
  }

  // Generador de Tarjeta de Producto (Estilo Blanco y Verde con Sanitización Anti-XSS)
  function createCardElement(item, isGlitchColumn = true) {
    const { product, verdict, created_at, alert_url } = item;
    const store = STORES[product.store_id] || { 
      name: product.store_name || product.store_id, 
      color: '#059669',
      badgeBg: '#ecfdf5',
      badgeText: '#047857'
    };

    const hasCardPrice = Boolean(product.card_price && product.card_price > 0 && product.card_price < product.current_price);
    const bestPrice = hasCardPrice ? product.card_price : product.current_price;

    const discountPct = Number(verdict.discount_percentage) ||
      Math.round(((product.normal_price - bestPrice) / product.normal_price) * 100);

    const savingsCLP = Number(verdict.absolute_savings_clp) ||
      Math.max(0, product.normal_price - bestPrice);

    const isMissingZero = Boolean(verdict.is_missing_zero);
    const isCritical = isGlitchColumn && (verdict.severity === 'CRITICO' || discountPct >= 85 || isMissingZero);

    // Sanitización estricta de todos los campos interpolados
    const safeId = escapeHtml(product.id || '');
    const safeTitle = escapeHtml(product.title || 'Producto sin título');
    const safeCategory = escapeHtml(product.category || 'Tecnología');
    const safeStoreName = escapeHtml(store.name || product.store_name || product.store_id);
    const safeUrl = sanitizeUrl(alert_url || product.url);
    const rawCartUrl = item.add_to_cart_url ? sanitizeUrl(item.add_to_cart_url) : '#';
    const safeCartUrl = rawCartUrl !== '#' ? rawCartUrl : '';
    const rawImage = product.image_url ? String(product.image_url).trim() : '';
    const safeImageUrl = (rawImage && /^https?:\/\//i.test(rawImage)) ? sanitizeUrl(rawImage) : FALLBACK_IMAGE;

    const card = document.createElement('article');
    card.id = `item-${safeId}`;
    card.className = `white-card rounded-xl p-4 shadow-sm flex flex-col justify-between ${
      isGlitchColumn ? 'glitch-item ' + (isCritical ? 'is-critical' : '') : 'deal-item'
    }`;

    card.innerHTML = `
      <div>
        <!-- Fila Superior: Tienda, Hora y Badge de Descuento -->
        <div class="flex items-center justify-between gap-2 mb-2.5">
          <div class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium" style="background-color: ${escapeHtml(store.badgeBg)}; color: ${escapeHtml(store.badgeText)}">
            <span class="w-2 h-2 rounded-full" style="background-color: ${escapeHtml(store.color)}"></span>
            <span>${safeStoreName}</span>
            <span class="opacity-60">•</span>
            <span class="opacity-80 text-[11px] font-mono">${escapeHtml(formatTime(created_at))}</span>
          </div>

          <div class="flex items-center gap-1">
            ${hasCardPrice ? `
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-purple-50 text-purple-700 border border-purple-200">
                💳 Tarjeta
              </span>
            ` : ''}
            ${isMissingZero ? `
              <span class="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-amber-50 text-amber-700 border border-amber-200">
                ⚡ Bug 10x
              </span>
            ` : ''}
            ${item.verification_level === 'COMPRABLE' ? `
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-100 text-emerald-800 border border-emerald-300" title="Enlace directo Sniper 1-Clic disponible">
                ⚡ Comprable
              </span>
            ` : (item.verification_level === 'VERIFICADO' ? `
              <span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-blue-50 text-blue-700 border border-blue-200" title="Confirmado en tienda con stock y precio">
                ✓ Verificado
              </span>
            ` : '')}
            ${item.is_watchlist_match ? `
              <span class="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-yellow-100 text-yellow-800 border border-yellow-300">
                ⭐ Watchlist
              </span>
            ` : ''}
            <span class="px-2 py-0.5 rounded text-xs font-mono font-bold ${
              isCritical
                ? 'bg-rose-50 text-rose-700 border border-rose-200'
                : 'bg-emerald-50 text-emerald-700 border border-emerald-200'
            }">
              -${discountPct}% OFF
            </span>
          </div>
        </div>

        <!-- Fila Media: Imagen + Título -->
        <div class="flex gap-3 mb-3">
          <div class="w-16 h-16 sm:w-20 sm:h-20 rounded-lg bg-slate-50 border border-slate-100 p-1 flex-shrink-0 flex items-center justify-center overflow-hidden">
            <img src="${safeImageUrl}" 
                 alt="${safeTitle}" 
                 class="w-full h-full object-contain rounded"
                 loading="lazy"
                 onerror="this.onerror=null; this.src='${FALLBACK_IMAGE}';" />
          </div>

          <div class="flex-1 min-w-0">
            <span class="text-[10px] font-mono uppercase text-slate-400 font-semibold tracking-wider">
              ${safeCategory}
            </span>
            <h3 class="text-xs sm:text-sm font-semibold text-slate-900 line-clamp-2 leading-snug mt-0.5" title="${safeTitle}">
              ${safeTitle}
            </h3>
            <div class="text-[11px] text-slate-500 font-medium mt-1">
              Ahorro: <span class="text-emerald-700 font-semibold font-mono">${formatCLP(savingsCLP)}</span>
            </div>
          </div>
        </div>

        <!-- Bloque de Precios Blanco y Verde -->
        <div class="bg-slate-50 rounded-lg p-2.5 border border-slate-100 mb-2.5 flex items-baseline justify-between">
          <div>
            <span class="text-[10px] font-mono text-slate-500 uppercase">${hasCardPrice ? 'Precio Tarjeta' : 'Precio Oferta'}</span>
            <div class="text-lg sm:text-xl font-bold font-mono text-emerald-600">
              ${formatCLP(bestPrice)}
            </div>
          </div>
          <div class="text-right">
            <span class="text-[10px] font-mono text-slate-400 uppercase">Normal</span>
            <div class="text-xs font-mono text-slate-400 line-through">
              ${formatCLP(product.normal_price)}
            </div>
          </div>
        </div>

        <!-- Referencia de Mercado Inter-Tiendas -->
        ${item.market_median_price ? `
          <div class="mb-3 px-2 py-1.5 rounded-lg bg-indigo-50/70 border border-indigo-100 flex items-center justify-between text-[11px]">
            <div class="flex items-center gap-1.5 text-indigo-900 font-medium">
              <span class="text-xs">🌐</span>
              <span>Ref. Mercado: <strong>${formatCLP(item.market_median_price)}</strong></span>
              <span class="text-[10px] text-indigo-600">(${item.market_stores_count} tiendas${item.market_matched_model ? ` por ${escapeHtml(item.market_matched_model)}` : ''})</span>
            </div>
            ${item.market_discount_pct !== null && item.market_discount_pct !== undefined ? `
              <span class="font-mono font-bold text-indigo-700">
                ${item.market_discount_pct > 0 ? '-' : '+'}${Math.abs(item.market_discount_pct).toFixed(0)}% vs mkt
              </span>
            ` : ''}
          </div>
        ` : ''}
      </div>

      <!-- Botones de Compra Directo / Sniper 1-Clic -->
      <div>
        ${safeCartUrl && safeCartUrl !== safeUrl ? `
          <div class="grid grid-cols-2 gap-2">
            <a href="${safeUrl}" 
               target="_blank" 
               rel="noopener noreferrer" 
               class="py-2 px-2 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold text-xs text-center transition flex items-center justify-center gap-1">
              <span>Ver Tienda</span>
              <i data-lucide="external-link" class="w-3 h-3"></i>
            </a>
            <a href="${safeCartUrl}" 
               target="_blank" 
               rel="noopener noreferrer" 
               class="py-2 px-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-xs text-center transition flex items-center justify-center gap-1 shadow-sm active:scale-95">
              <span>⚡ 1-Clic Sniper</span>
              <i data-lucide="zap" class="w-3 h-3"></i>
            </a>
          </div>
        ` : `
          <a href="${safeUrl}" 
             target="_blank" 
             rel="noopener noreferrer" 
             class="w-full py-2 px-3 rounded-lg ${
               isGlitchColumn
                 ? 'bg-emerald-600 hover:bg-emerald-700 text-white'
                 : 'bg-slate-800 hover:bg-slate-900 text-white'
             } font-semibold text-xs text-center transition flex items-center justify-center gap-1.5 shadow-sm active:scale-95">
            <span>Ver Oferta en ${safeStoreName}</span>
            <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
          </a>
        `}
      </div>

      <!-- Barra de Feedback de Usuario 👍/👎 (Punto 5) -->
      <div class="mt-2.5 pt-2 border-t border-slate-100 flex items-center justify-between text-xs text-slate-500">
        <span class="text-[11px] font-medium text-slate-400">¿Es oferta legítima?</span>
        <div class="flex items-center gap-1.5 feedback-buttons-bar">
          <button type="button" 
                  class="btn-feedback-up px-2 py-0.5 rounded bg-slate-100 hover:bg-emerald-50 hover:text-emerald-700 text-slate-600 transition flex items-center gap-1 text-[11px] font-medium active:scale-95" 
                  title="Confirmar oferta o glitch real (afina confianza positiva)"
                  data-deal-id="${escapeHtml(item.alert_id || product.id)}">
            <span>👍</span>
            <span class="up-count font-mono font-semibold">${item.upvotes || 0}</span>
          </button>
          <button type="button" 
                  class="btn-feedback-down px-2 py-0.5 rounded bg-slate-100 hover:bg-rose-50 hover:text-rose-700 text-slate-600 transition flex items-center gap-1 text-[11px] font-medium active:scale-95" 
                  title="Reportar falso positivo o engaño (reduce confianza)"
                  data-deal-id="${escapeHtml(item.alert_id || product.id)}">
            <span>👎</span>
            <span class="down-count font-mono font-semibold">${item.downvotes || 0}</span>
          </button>
        </div>
      </div>
    `;

    return card;
  }

  // Filtrar lista por tienda y búsqueda general
  function filterItems(items) {
    return items.filter(item => {
      if (state.filters.store !== 'all' && item.product.store_id !== state.filters.store) {
        return false;
      }
      if (state.filters.search.trim()) {
        const q = state.filters.search.toLowerCase();
        const t = (item.product.title || '').toLowerCase();
        const s = (item.product.store_name || '').toLowerCase();
        const c = (item.product.category || '').toLowerCase();
        if (!t.includes(q) && !s.includes(q) && !c.includes(q)) return false;
      }
      return true;
    });
  }

  // Ordenar lista según criterio
  function sortItems(items) {
    return [...items].sort((a, b) => {
      if (state.filters.sortBy === 'discount') {
        return (b.verdict.discount_percentage || 0) - (a.verdict.discount_percentage || 0);
      }
      if (state.filters.sortBy === 'savings') {
        return (b.verdict.absolute_savings_clp || 0) - (a.verdict.absolute_savings_clp || 0);
      }
      return 0; // 'recent'
    });
  }

  // Renderizar las dos columnas de forma coordinada
  function renderBothColumns() {
    // 🔒 Guardar scroll para que la pantalla NUNCA salte ni se mueva sola
    const scrollY = window.scrollY;

    // 1. Columna 1: Glitches Críticos (≥65%)
    const col1Container = document.getElementById('glitches-col-container');
    const emptyGlitches = document.getElementById('empty-glitches');
    const col1Badge = document.getElementById('col1-count-badge');

    let col1Items = sortItems(filterItems(state.glitches));

    if (col1Badge) col1Badge.textContent = `${col1Items.length} disponibles`;

    if (col1Container) {
      col1Container.innerHTML = '';
      if (col1Items.length === 0) {
        if (emptyGlitches) emptyGlitches.classList.remove('hidden');
      } else {
        if (emptyGlitches) emptyGlitches.classList.add('hidden');
        col1Items.forEach(item => {
          col1Container.appendChild(createCardElement(item, true));
        });
      }
    }

    // 2. Columna 2: Ofertas Comerciales (40% - 64%)
    const col2Container = document.getElementById('deals-col-container');
    const emptyDeals = document.getElementById('empty-deals');
    const col2Badge = document.getElementById('col2-count-badge');

    let col2Items = filterItems(state.commercialDeals);

    // Filtro adicional de rango de porcentaje en Columna 2
    if (state.filters.col2MinDiscount > 40.0) {
      col2Items = col2Items.filter(item => (item.verdict.discount_percentage || 0) >= state.filters.col2MinDiscount);
    }

    col2Items = sortItems(col2Items);

    if (col2Badge) col2Badge.textContent = `${col2Items.length} disponibles`;

    if (col2Container) {
      col2Container.innerHTML = '';
      if (col2Items.length === 0) {
        if (emptyDeals) emptyDeals.classList.remove('hidden');
      } else {
        if (emptyDeals) emptyDeals.classList.add('hidden');
        col2Items.forEach(item => {
          col2Container.appendChild(createCardElement(item, false));
        });
      }
    }

    if (window.lucide) window.lucide.createIcons();
    updateKPIs();

    // Restaurar posición exacta del scroll
    window.scrollTo({ top: scrollY, behavior: 'instant' });
  }

  // Ingesta de nuevo glitch vía WebSocket o API
  function ingestGlitch(glitchAlert, isTest = false) {
    if (!glitchAlert || !glitchAlert.product) return;

    const prodId = glitchAlert.product.id;
    const existingIndex = state.glitches.findIndex(
      g => g.product.id === prodId || g.alert_id === glitchAlert.alert_id
    );

    if (existingIndex >= 0) {
      const existing = state.glitches[existingIndex];
      if (existing.product.current_price === glitchAlert.product.current_price) {
        return;
      }
      state.glitches[existingIndex] = glitchAlert;
    } else {
      state.glitches.unshift(glitchAlert);
      sound.playDing();

      const store = STORES[glitchAlert.product.store_id]?.name || glitchAlert.product.store_name;
      const discount = glitchAlert.verdict?.discount_percentage || 85;
      showToast(
        `${store}: ${glitchAlert.product.title.slice(0, 42)}... (-${discount}% OFF)`,
        isTest ? '🧪 Glitch de Prueba' : '⚡ Nuevo Glitch Detectado'
      );
    }

    renderBothColumns();
  }

  // Conexión WebSocket en tiempo real con reconexión segura
  let activeSocket = null;
  let reconnectTimer = null;

  function initWebSocket() {
    if (activeSocket && (activeSocket.readyState === WebSocket.CONNECTING || activeSocket.readyState === WebSocket.OPEN)) {
      return;
    }

    const isHttps = window.location.protocol === 'https:';
    const protocol = isHttps ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/glitches`;

    try {
      activeSocket = new WebSocket(wsUrl);

      activeSocket.onopen = () => {
        state.wsConnected = true;
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
      };

      activeSocket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'deal_corrected' && data.product_id) {
            state.glitches = state.glitches.filter(g => g.product.id !== data.product_id);
            state.commercialDeals = state.commercialDeals.filter(d => (d.product?.id || d.product_id) !== data.product_id);
            renderBothColumns();
          } else if (data.alert_id && data.product) {
            ingestGlitch(data);
          } else if (data.type === 'new_glitch' && data.payload) {
            ingestGlitch(data.payload);
          }
        } catch {
          // Ignorar pings o mensajes de control
        }
      };

      activeSocket.onclose = () => {
        state.wsConnected = false;
        activeSocket = null;
        if (!reconnectTimer) {
          reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            initWebSocket();
          }, 4000);
        }
      };

      activeSocket.onerror = () => {
        state.wsConnected = false;
      };
    } catch {
      state.wsConnected = false;
    }
  }

  // Event Listeners y Controles
  function setupEventListeners() {
    // 1. Audio
    const toggleSoundBtn = document.getElementById('toggle-sound-btn');
    const soundIcon = document.getElementById('sound-icon');
    const soundLabel = document.getElementById('sound-label');

    const updateAudioUI = () => {
      if (state.soundEnabled) {
        if (soundLabel) soundLabel.textContent = 'Audio ON';
        if (soundIcon) soundIcon.setAttribute('data-lucide', 'volume-2');
      } else {
        if (soundLabel) soundLabel.textContent = 'Silenciado';
        if (soundIcon) soundIcon.setAttribute('data-lucide', 'volume-x');
      }
      if (window.lucide) window.lucide.createIcons();
    };

    updateAudioUI();

    // 1.1 Configurar API Key Administrativa (Hallazgo C de Auditoría 4)
    function getStoredApiKey() {
      return localStorage.getItem('glitchhunter_api_key') || '';
    }

    function setStoredApiKey(key) {
      if (key && key.trim()) {
        localStorage.setItem('glitchhunter_api_key', key.trim());
      } else {
        localStorage.removeItem('glitchhunter_api_key');
      }
      updateApiKeyUI();
    }

    function updateApiKeyUI() {
      const label = document.getElementById('api-key-label');
      if (label) {
        const hasKey = Boolean(getStoredApiKey());
        label.textContent = hasKey ? 'API Key ✓' : 'API Key';
      }
    }

    async function authenticatedFetch(url, options = {}) {
      options.headers = options.headers || {};
      const key = getStoredApiKey();
      if (key) {
        options.headers['X-API-Key'] = key;
      }

      let res = await fetch(url, options);
      if (res.status === 401) {
        const entered = prompt('🔐 Autenticación requerida: Ingresa tu clave API_AUTH_KEY para realizar esta acción:');
        if (entered) {
          setStoredApiKey(entered);
          options.headers['X-API-Key'] = entered.trim();
          res = await fetch(url, options);
        }
      }
      return res;
    }

    const btnApiKey = document.getElementById('btn-api-key');
    if (btnApiKey) {
      btnApiKey.addEventListener('click', () => {
        const current = getStoredApiKey();
        const entered = prompt('Configura tu clave de API administrativa (API_AUTH_KEY). Déjala vacía para removerla:', current);
        if (entered !== null) {
          setStoredApiKey(entered);
          showToast(getStoredApiKey() ? 'API Key guardada correctamente.' : 'API Key removida.', 'Seguridad');
        }
      });
      updateApiKeyUI();
    }

    if (toggleSoundBtn) {
      toggleSoundBtn.addEventListener('click', () => {
        state.soundEnabled = !state.soundEnabled;
        localStorage.setItem('glitchhunter_sound', state.soundEnabled ? 'true' : 'false');
        updateAudioUI();
        if (state.soundEnabled) sound.playDing();
      });
    }

    // 2. Escanear Ahora (Protegido contra carreras concurrentes y autenticado)
    const btnScan = document.getElementById('btn-force-scan');
    if (btnScan) {
      btnScan.addEventListener('click', async () => {
        btnScan.disabled = true;
        btnScan.innerHTML = `<i data-lucide="loader" class="w-3.5 h-3.5 animate-spin text-emerald-600"></i><span>Escaneando...</span>`;
        if (window.lucide) window.lucide.createIcons();

        try {
          const res = await authenticatedFetch('/api/scan', { method: 'POST' });
          if (res.status === 409) {
            const data = await res.json();
            showToast(data.message || 'Un ciclo de escaneo ya está en ejecución.', 'Escáner Ocupado');
            return;
          }
          if (res.ok) {
            const data = await res.json();
            showToast('Escaneo completado exitosamente.', 'Escaneo Listo');
            if (Array.isArray(data.new_glitches)) {
              data.new_glitches.forEach(g => ingestGlitch(g));
            }
            await loadData();
          } else if (res.status === 401) {
            showToast('Clave de API no autorizada o ausente.', 'No Autorizado');
          }
        } catch {
          showToast('Error conectando al backend.', 'Error');
        } finally {
          setTimeout(() => {
            btnScan.disabled = false;
            btnScan.innerHTML = `<i data-lucide="refresh-cw" class="w-3.5 h-3.5 text-slate-500"></i><span>Escanear</span>`;
            if (window.lucide) window.lucide.createIcons();
          }, 600);
        }
      });
    }

    // 3. Simular Glitch (Aislado y autenticado)
    const btnInject = document.getElementById('btn-inject-glitch');
    if (btnInject) {
      btnInject.addEventListener('click', async () => {
        try {
          const res = await authenticatedFetch('/api/test-glitch', { method: 'POST' });
          if (res.ok) {
            const data = await res.json();
            ingestGlitch(data, true);
          } else if (res.status === 403) {
            showToast('Inyección de pruebas deshabilitada en este entorno.', 'Prohibido');
          } else if (res.status === 401) {
            showToast('Clave de API requerida.', 'No Autorizado');
          }
        } catch {
          showToast('Error inyectando glitch de prueba.', 'Error');
        }
      });
    }

    // 4. Filtros por Tienda
    const storeButtons = document.querySelectorAll('.store-filter-btn');
    storeButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        storeButtons.forEach(b => {
          b.className = 'store-filter-btn px-3.5 py-1.5 rounded-lg text-xs font-medium transition bg-slate-50 hover:bg-slate-100 text-slate-700 border border-slate-200 flex-shrink-0';
        });
        btn.className = 'store-filter-btn px-3.5 py-1.5 rounded-lg text-xs font-semibold transition bg-emerald-600 text-white shadow-sm flex-shrink-0';

        state.filters.store = btn.getAttribute('data-store') || 'all';
        renderBothColumns();
      });
    });

    // 5. Filtros de Rango para Columna 2 (Ofertas Comerciales)
    const col2RangeButtons = document.querySelectorAll('.col2-range-btn');
    col2RangeButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        col2RangeButtons.forEach(b => {
          b.className = 'col2-range-btn px-2 py-0.5 rounded text-xs font-medium bg-slate-50 hover:bg-slate-100 text-slate-600 border border-slate-200';
        });
        btn.className = 'col2-range-btn px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200';

        const rangeVal = btn.getAttribute('data-col2-range');
        state.filters.col2MinDiscount = rangeVal === '60' ? 60.0 : rangeVal === '50' ? 50.0 : 40.0;
        renderBothColumns();
      });
    });

    // 6. Buscador
    const searchInput = document.getElementById('search-input');
    const btnClearSearch = document.getElementById('btn-clear-search');

    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        state.filters.search = e.target.value;
        if (btnClearSearch) {
          btnClearSearch.classList.toggle('hidden', state.filters.search.length === 0);
        }
        renderBothColumns();
      });
    }

    if (btnClearSearch) {
      btnClearSearch.addEventListener('click', () => {
        if (searchInput) searchInput.value = '';
        state.filters.search = '';
        btnClearSearch.classList.add('hidden');
        renderBothColumns();
      });
    }

    // 7. Ordenamiento
    const sortSelect = document.getElementById('sort-select');
    if (sortSelect) {
      sortSelect.addEventListener('change', (e) => {
        state.filters.sortBy = e.target.value;
        renderBothColumns();
      });
    }

    // 8. Delegación de Feedback de Usuario (👍 Oferta Real / 👎 Falso Positivo)
    document.addEventListener('click', async (e) => {
      const upBtn = e.target.closest('.btn-feedback-up');
      const downBtn = e.target.closest('.btn-feedback-down');
      if (!upBtn && !downBtn) return;

      const btn = upBtn || downBtn;
      const isUp = Boolean(upBtn);
      const dealId = btn.getAttribute('data-deal-id');
      if (!dealId) return;

      const bar = btn.closest('.feedback-buttons-bar') || btn.parentElement;
      const curUp = bar ? bar.querySelector('.btn-feedback-up') : null;
      const curDown = bar ? bar.querySelector('.btn-feedback-down') : null;

      if (btn.classList.contains('voted')) {
        showToast('Ya registraste tu voto para este producto.', 'Feedback');
        return;
      }

      try {
        const vote = isUp ? 'up' : 'down';
        const res = await fetch(`/api/deals/${encodeURIComponent(dealId)}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ vote: vote }),
        });

        if (res.ok) {
          const data = await res.json();
          if (curUp) {
            const countSpan = curUp.querySelector('.up-count');
            if (countSpan) countSpan.textContent = data.upvotes;
            curUp.disabled = true;
          }
          if (curDown) {
            const countSpan = curDown.querySelector('.down-count');
            if (countSpan) countSpan.textContent = data.downvotes;
            curDown.disabled = true;
          }

          if (isUp) {
            if (curUp) curUp.classList.add('voted', 'bg-emerald-100', 'text-emerald-800', 'border-emerald-300');
            showToast('¡Gracias! Oferta confirmada como legítima.', '👍 Feedback Registrado');
          } else {
            if (curDown) curDown.classList.add('voted', 'bg-rose-100', 'text-rose-800', 'border-rose-300');
            showToast('¡Gracias! Reporte de falso positivo recibido.', '👎 Feedback Registrado');
          }
          await refreshFeedbackStats();
        } else {
          showToast('No se pudo registrar tu voto.', 'Error');
        }
      } catch (err) {
        showToast('Error de conexión al enviar feedback.', 'Error');
      }
    });

    // Audio contexto al primer clic
    document.addEventListener('click', () => {
      sound.init();
    }, { once: true });
  }

  // Carga de datos
  async function loadData() {
    try {
      // 1. Cargar Glitches Críticos (Columna 1)
      const resGlitches = await fetch('/api/glitches');
      if (resGlitches.ok) {
        const list = await resGlitches.json();
        if (Array.isArray(list)) state.glitches = list;
      }

      // 2. Cargar Ofertas Comerciales (Columna 2)
      const resDeals = await fetch('/api/deals?min_discount=40&max_discount=65');
      if (resDeals.ok) {
        const deals = await resDeals.json();
        if (Array.isArray(deals)) state.commercialDeals = deals;
      }

      // 3. Cargar Matriz de Salud de Tiendas (Store Health Matrix)
      const resHealth = await fetch('/api/store-health');
      if (resHealth.ok) {
        const data = await resHealth.json();
        const container = document.getElementById('store-health-pills');
        if (container && Array.isArray(data.stores) && data.stores.length > 0) {
          container.innerHTML = data.stores.map(s => {
            let icon = '🟢';
            let bg = 'bg-emerald-50 text-emerald-700 border-emerald-200';
            let statusText = s.status;
            if (s.status === 'EMPTY_RESPONSE') {
              icon = '🟡';
              bg = 'bg-amber-50 text-amber-700 border-amber-200';
              statusText = '0 ítems';
            } else if (s.status === 'BLOCKED') {
              icon = '⛔';
              bg = 'bg-rose-50 text-rose-700 border-rose-200';
              statusText = 'Bloqueado';
            } else if (s.status === 'DEGRADED') {
              icon = '🟡';
              bg = 'bg-amber-50 text-amber-700 border-amber-200';
            } else if (s.status === 'OFFLINE') {
              icon = '🔴';
              bg = 'bg-rose-50 text-rose-700 border-rose-200';
            }
            const lat = Math.round(s.latency_ms || s.avg_latency_ms || 0);
            return `<span class="px-2 py-0.5 rounded ${bg} border font-mono text-[11px]">${s.store_name}: ${icon} ${lat}ms (${statusText})</span>`;
          }).join('');
        }
      }

      // 4. Cargar Estadísticas de Feedback Comunitario (Punto 5)
      await refreshFeedbackStats();
    } catch {
      // Servidor local offline
    }

    renderBothColumns();
  }

  async function refreshFeedbackStats() {
    try {
      const res = await fetch('/api/feedback/stats');
      if (res.ok) {
        const stats = await res.json();
        const accSpan = document.getElementById('feedback-accuracy-pct');
        const upSpan = document.getElementById('feedback-upvotes-count');
        const downSpan = document.getElementById('feedback-downvotes-count');
        const pill = document.getElementById('feedback-stats-pill');

        if (accSpan) accSpan.textContent = `${stats.accuracy_rate_pct}%`;
        if (upSpan) upSpan.textContent = stats.total_upvotes || 0;
        if (downSpan) downSpan.textContent = stats.total_downvotes || 0;
        if (pill && stats.total_votes > 0) pill.classList.remove('hidden');
      }
    } catch {
      // Omitir en offline
    }
  }

  // Inicialización
  function init() {
    setupEventListeners();
    loadData();
    initWebSocket();
    // ⏱️ Polling de respaldo cada 30 segundos (evita desincronizaciones si falla WebSocket)
    setInterval(loadData, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();