/* ═══════════════════════════════════════════════════════
   AI Trading Agent Dashboard - Frontend Logic
   ═══════════════════════════════════════════════════════ */

const API = '';  // Same origin
let pollInterval = null;
let isRunning = false;
let chartCtx = null;
let candles = [];
let prevTradeCount = 0;

// ── Init ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    chartCtx = document.getElementById('chart-canvas').getContext('2d');
    fetchState();
    startPolling();
});

function startPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(fetchState, 2000);
}

// ── API Calls ────────────────────────────────────────────

async function fetchState() {
    try {
        const resp = await fetch(`${API}/api/state`);
        const data = await resp.json();
        updateDashboard(data);
    } catch (e) {
        console.error('Fetch error:', e);
    }
}

async function startAgent() {
    try {
        const resp = await fetch(`${API}/api/start`, { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'started' || data.status === 'already_running') {
            isRunning = true;
            updateButtons();
        }
    } catch (e) {
        console.error('Start error:', e);
    }
}

async function stopAgent() {
    try {
        const resp = await fetch(`${API}/api/stop`, { method: 'POST' });
        const data = await resp.json();
        isRunning = false;
        updateButtons();
    } catch (e) {
        console.error('Stop error:', e);
    }
}

async function saveSettings() {
    const config = {
        symbol: document.getElementById('cfg-symbol').value,
        leverage: parseInt(document.getElementById('cfg-leverage').value),
        stop_loss_pct: parseFloat(document.getElementById('cfg-sl').value),
        take_profit_pct: parseFloat(document.getElementById('cfg-tp').value),
        min_confidence: parseFloat(document.getElementById('cfg-confidence').value),
        paper_trading: document.getElementById('cfg-mode').value === 'true',
    };

    // API keys - only send if user entered them
    const anthropicKey = document.getElementById('cfg-anthropic-key').value.trim();
    const mexcKey = document.getElementById('cfg-mexc-key').value.trim();
    const mexcSecret = document.getElementById('cfg-mexc-secret').value.trim();

    if (anthropicKey) config.anthropic_api_key = anthropicKey;
    if (mexcKey) config.mexc_api_key = mexcKey;
    if (mexcSecret) config.mexc_api_secret = mexcSecret;

    try {
        const resp = await fetch(`${API}/api/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        const result = await resp.json();

        // Clear password fields after saving
        document.getElementById('cfg-anthropic-key').value = '';
        document.getElementById('cfg-mexc-key').value = '';
        document.getElementById('cfg-mexc-secret').value = '';

        // Show confirmation
        if (result.config) {
            const aiBadge = document.getElementById('ai-badge');
            if (result.config.ai_enabled) {
                aiBadge.textContent = 'AI ON';
                aiBadge.style.background = 'rgba(0, 200, 83, 0.15)';
                aiBadge.style.color = '#00c853';
            }
        }

        closeSettings();
    } catch (e) {
        console.error('Config error:', e);
    }
}

// ── Dashboard Update ─────────────────────────────────────

function updateDashboard(data) {
    if (!data) return;

    isRunning = data.running;
    updateButtons();

    // Status bar
    const dot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    dot.className = `status-dot ${isRunning ? 'running' : 'stopped'}`;
    statusText.textContent = isRunning ? 'Running' : 'Stopped';

    // Symbol & mode
    document.getElementById('symbol-display').textContent = data.symbol || '--';
    document.getElementById('leverage-display').textContent = `${data.leverage || 20}x`;

    const modeBadge = document.getElementById('mode-badge');
    modeBadge.textContent = data.mode || 'PAPER';
    modeBadge.className = `mode-badge ${data.mode === 'LIVE' ? 'mode-live' : 'mode-paper'}`;

    // AI badge
    const aiBadge = document.getElementById('ai-badge');
    if (data.ai_enabled) {
        aiBadge.textContent = 'AI ON';
        aiBadge.style.background = 'rgba(0, 200, 83, 0.15)';
        aiBadge.style.color = '#00c853';
    } else {
        aiBadge.textContent = 'AI OFF';
        aiBadge.style.background = 'rgba(124,77,255,0.15)';
        aiBadge.style.color = '#7c4dff';
    }

    // AI analysis count
    document.getElementById('ai-count-display').textContent = data.ai_analysis_count || 0;

    // AI Reasoning Card
    const aiReasonText = document.getElementById('ai-reasoning-text');
    const aiRiskBadge = document.getElementById('ai-risk-badge');
    if (data.ai_reasoning) {
        aiReasonText.textContent = data.ai_reasoning;
        aiReasonText.style.color = 'var(--text-primary)';
    } else if (data.ai_enabled) {
        aiReasonText.textContent = 'Claude AI is active. Waiting for next analysis cycle...';
        aiReasonText.style.color = 'var(--text-secondary)';
    } else {
        aiReasonText.textContent = 'Configure your Anthropic API key in Settings to enable Claude AI analysis.';
        aiReasonText.style.color = 'var(--text-muted)';
    }

    if (data.ai_risk_level) {
        aiRiskBadge.textContent = data.ai_risk_level;
        const riskColors = {
            'LOW': { bg: 'rgba(0,200,83,0.15)', color: '#00c853' },
            'MEDIUM': { bg: 'rgba(255,214,0,0.15)', color: '#ffd600' },
            'HIGH': { bg: 'rgba(255,23,68,0.15)', color: '#ff1744' },
        };
        const rc = riskColors[data.ai_risk_level] || riskColors['MEDIUM'];
        aiRiskBadge.style.background = rc.bg;
        aiRiskBadge.style.color = rc.color;
    }

    // Ticker
    if (data.ticker) {
        document.getElementById('price-display').textContent = formatPrice(data.ticker.price);
        const change = data.ticker.change_24h;
        const changeEl = document.getElementById('change-display');
        changeEl.textContent = `${change >= 0 ? '+' : ''}${(change * 100).toFixed(2)}%`;
        changeEl.className = `status-value ${change >= 0 ? 'positive' : 'negative'}`;
        document.getElementById('volume-display').textContent = formatVolume(data.ticker.volume_24h);
    }

    // Account stats
    if (data.account) {
        const acc = data.account;
        document.getElementById('stat-balance').textContent = `$${formatNum(acc.balance)}`;

        const dailyEl = document.getElementById('stat-pnl-daily');
        dailyEl.textContent = `Daily: ${acc.daily_pnl >= 0 ? '+' : ''}$${formatNum(acc.daily_pnl)}`;
        dailyEl.className = `stat-sub ${acc.daily_pnl >= 0 ? 'positive' : 'negative'}`;

        const unrealEl = document.getElementById('stat-unrealized');
        unrealEl.textContent = `${acc.unrealized_pnl >= 0 ? '+' : ''}$${formatNum(acc.unrealized_pnl)}`;
        unrealEl.className = `stat-value ${acc.unrealized_pnl > 0 ? 'positive' : acc.unrealized_pnl < 0 ? 'negative' : 'neutral'}`;

        document.getElementById('stat-trades').textContent = acc.total_trades;

        const wr = document.getElementById('stat-winrate');
        wr.textContent = `${acc.win_rate}%`;
        wr.className = `stat-value ${acc.win_rate >= 50 ? 'positive' : acc.win_rate > 0 ? 'negative' : 'neutral'}`;
        document.getElementById('stat-wl').textContent = `W: ${acc.win_trades} / L: ${acc.loss_trades}`;
    }

    // Signal
    if (data.last_signal) {
        const sig = data.last_signal;
        const sigBox = document.getElementById('signal-box');
        const sigLabel = document.getElementById('signal-dir-label');
        const side = sig.side || 'NEUTRAL';

        sigLabel.textContent = side === 'LONG' ? 'LONG' : side === 'SHORT' ? 'SHORT' : sig.strength;
        sigBox.className = `signal-direction ${side === 'LONG' ? 'long' : side === 'SHORT' ? 'short' : 'neutral'}`;

        document.getElementById('signal-conf-text').textContent = `Confidence: ${sig.confidence}%`;
        const fill = document.getElementById('confidence-bar-fill');
        fill.style.width = `${sig.confidence}%`;
        fill.style.background = side === 'LONG' ? 'var(--green)' : side === 'SHORT' ? 'var(--red)' : 'var(--text-muted)';

        const statSig = document.getElementById('stat-signal');
        statSig.textContent = sig.strength;
        statSig.className = `stat-value ${side === 'LONG' ? 'positive' : side === 'SHORT' ? 'negative' : 'neutral'}`;
        document.getElementById('stat-confidence').textContent = `${sig.confidence}%`;

        // Reasons
        const list = document.getElementById('reasons-list');
        if (sig.reasons && sig.reasons.length > 0) {
            list.innerHTML = sig.reasons.map(r => {
                // Highlight AI reasons
                const isAI = r.startsWith('AI:') || r.startsWith('Technical');
                const style = isAI ? 'color: var(--purple); font-weight: 500;' : '';
                return `<li style="${style}">${escapeHtml(r)}</li>`;
            }).join('');
        }
    }

    // Indicators
    if (data.indicators) {
        const ind = data.indicators;
        setIndicator('ind-rsi', ind.rsi, ind.rsi < 30 ? 'positive' : ind.rsi > 70 ? 'negative' : '');
        setIndicator('ind-ema-fast', formatPrice(ind.ema_fast));
        setIndicator('ind-ema-slow', formatPrice(ind.ema_slow));
        setIndicator('ind-ema-trend', formatPrice(ind.ema_trend));
        setIndicator('ind-macd', ind.macd?.toFixed(4));
        setIndicator('ind-macd-sig', ind.macd_signal?.toFixed(4));
        setIndicator('ind-macd-hist', ind.macd_histogram?.toFixed(4), ind.macd_histogram > 0 ? 'positive' : 'negative');
        setIndicator('ind-bb-up', formatPrice(ind.bb_upper));
        setIndicator('ind-bb-mid', formatPrice(ind.bb_middle));
        setIndicator('ind-bb-low', formatPrice(ind.bb_lower));
        setIndicator('ind-vwap', formatPrice(ind.vwap));
        setIndicator('ind-atr', ind.atr?.toFixed(2));

        document.getElementById('stat-rsi').textContent = ind.rsi?.toFixed(1) || '--';
    }

    // Position
    const posCard = document.getElementById('position-card');
    if (data.position) {
        posCard.classList.add('active');
        const pos = data.position;
        const sideEl = document.getElementById('pos-side');
        sideEl.textContent = pos.side;
        sideEl.className = `position-side ${pos.side.toLowerCase()}`;
        document.getElementById('pos-entry').textContent = formatPrice(pos.entry_price);
        document.getElementById('pos-sl').textContent = formatPrice(pos.stop_loss);
        document.getElementById('pos-tp').textContent = formatPrice(pos.take_profit);
        document.getElementById('pos-size').textContent = pos.quantity?.toFixed(6);
        const posPnl = document.getElementById('pos-pnl');
        posPnl.textContent = `${pos.unrealized_pnl >= 0 ? '+' : ''}$${formatNum(pos.unrealized_pnl)}`;
        posPnl.className = `field-value ${pos.unrealized_pnl >= 0 ? 'positive' : 'negative'}`;
    } else {
        posCard.classList.remove('active');
    }

    // Trades
    if (data.trades && data.trades.length > 0) {
        const tbody = document.getElementById('trades-body');
        const trades = [...data.trades].reverse();
        tbody.innerHTML = trades.map((t, i) => `
            <tr class="${i === 0 && data.trades.length > prevTradeCount ? 'trade-new' : ''}">
                <td><span class="side-badge ${t.side.toLowerCase()}">${t.side}</span></td>
                <td>${formatPrice(t.entry_price)}</td>
                <td>${formatPrice(t.exit_price)}</td>
                <td class="${t.pnl >= 0 ? 'positive' : 'negative'}">${t.pnl >= 0 ? '+' : ''}$${formatNum(t.pnl)}</td>
                <td class="${t.pnl_pct >= 0 ? 'positive' : 'negative'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%</td>
                <td style="color: var(--text-secondary);">${escapeHtml(t.reason)}</td>
                <td style="color: var(--text-muted);">${formatTime(t.exit_time)}</td>
            </tr>
        `).join('');
        prevTradeCount = data.trades.length;
    }

    // Chart
    if (data.candles && data.candles.length > 0) {
        candles = data.candles;
        drawChart(data.position, data.indicators);
    }
}

// ── Chart Drawing (Canvas) ───────────────────────────────

function drawChart(position, indicators) {
    const canvas = document.getElementById('chart-canvas');
    const ctx = chartCtx;
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width - 32;
    canvas.height = Math.max(rect.height - 60, 300);

    const W = canvas.width;
    const H = canvas.height;
    const padding = { top: 10, right: 70, bottom: 30, left: 10 };
    const chartW = W - padding.left - padding.right;
    const chartH = H - padding.top - padding.bottom;

    // Clear
    ctx.fillStyle = '#12141a';
    ctx.fillRect(0, 0, W, H);

    if (candles.length === 0) return;

    // Price range
    let minPrice = Infinity, maxPrice = -Infinity;
    for (const c of candles) {
        if (c.l < minPrice) minPrice = c.l;
        if (c.h > maxPrice) maxPrice = c.h;
    }
    const priceRange = maxPrice - minPrice || 1;
    const pricePadding = priceRange * 0.05;
    minPrice -= pricePadding;
    maxPrice += pricePadding;
    const totalRange = maxPrice - minPrice;

    const toY = (price) => padding.top + chartH - ((price - minPrice) / totalRange) * chartH;

    const candleWidth = Math.max(2, (chartW / candles.length) * 0.7);
    const gap = chartW / candles.length;

    // Grid
    ctx.strokeStyle = '#1e2130';
    ctx.lineWidth = 1;
    const gridLines = 6;
    for (let i = 0; i <= gridLines; i++) {
        const y = padding.top + (chartH / gridLines) * i;
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(W - padding.right, y);
        ctx.stroke();

        const price = maxPrice - (totalRange / gridLines) * i;
        ctx.fillStyle = '#5f6368';
        ctx.font = '10px JetBrains Mono';
        ctx.textAlign = 'left';
        ctx.fillText(formatPrice(price), W - padding.right + 5, y + 4);
    }

    // Bollinger Bands
    if (indicators && indicators.bb_upper && indicators.bb_lower) {
        const bbUp = toY(indicators.bb_upper);
        const bbMid = toY(indicators.bb_middle);
        const bbLow = toY(indicators.bb_lower);

        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = 'rgba(124, 77, 255, 0.3)';
        ctx.lineWidth = 1;
        [bbUp, bbMid, bbLow].forEach(y => {
            ctx.beginPath();
            ctx.moveTo(padding.left, y);
            ctx.lineTo(W - padding.right, y);
            ctx.stroke();
        });
        ctx.setLineDash([]);

        // BB fill
        ctx.fillStyle = 'rgba(124, 77, 255, 0.05)';
        ctx.fillRect(padding.left, bbUp, chartW, bbLow - bbUp);
    }

    // VWAP line
    if (indicators && indicators.vwap) {
        const vwapY = toY(indicators.vwap);
        ctx.strokeStyle = 'rgba(255, 145, 0, 0.4)';
        ctx.lineWidth = 1;
        ctx.setLineDash([6, 3]);
        ctx.beginPath();
        ctx.moveTo(padding.left, vwapY);
        ctx.lineTo(W - padding.right, vwapY);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    // Candles
    for (let i = 0; i < candles.length; i++) {
        const c = candles[i];
        const x = padding.left + i * gap + gap / 2;
        const isGreen = c.c >= c.o;

        // Wick
        ctx.strokeStyle = isGreen ? '#00c853' : '#ff1744';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, toY(c.h));
        ctx.lineTo(x, toY(c.l));
        ctx.stroke();

        // Body
        const bodyTop = toY(Math.max(c.o, c.c));
        const bodyBottom = toY(Math.min(c.o, c.c));
        const bodyHeight = Math.max(1, bodyBottom - bodyTop);

        ctx.fillStyle = isGreen ? '#00c853' : '#ff1744';
        ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    }

    // Position lines
    if (position) {
        drawHLine(ctx, toY(position.entry_price), padding.left, W - padding.right, '#4285f4', 'Entry: ' + formatPrice(position.entry_price));
        drawHLine(ctx, toY(position.stop_loss), padding.left, W - padding.right, '#ff1744', 'SL: ' + formatPrice(position.stop_loss));
        drawHLine(ctx, toY(position.take_profit), padding.left, W - padding.right, '#00c853', 'TP: ' + formatPrice(position.take_profit));
    }

    // EMA lines (fast and slow)
    if (indicators) {
        if (indicators.ema_fast) {
            const emaY = toY(indicators.ema_fast);
            ctx.strokeStyle = 'rgba(0, 200, 83, 0.3)';
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 2]);
            ctx.beginPath();
            ctx.moveTo(padding.left, emaY);
            ctx.lineTo(W - padding.right, emaY);
            ctx.stroke();
            ctx.setLineDash([]);
        }
        if (indicators.ema_slow) {
            const emaY = toY(indicators.ema_slow);
            ctx.strokeStyle = 'rgba(255, 23, 68, 0.3)';
            ctx.lineWidth = 1;
            ctx.setLineDash([2, 2]);
            ctx.beginPath();
            ctx.moveTo(padding.left, emaY);
            ctx.lineTo(W - padding.right, emaY);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }

    // Current price line
    const lastPrice = candles[candles.length - 1].c;
    const priceY = toY(lastPrice);
    ctx.strokeStyle = '#e8eaed';
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(W - padding.right - 20, priceY);
    ctx.lineTo(W - padding.right, priceY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Price label
    ctx.fillStyle = '#4285f4';
    ctx.fillRect(W - padding.right, priceY - 8, padding.right, 16);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 10px JetBrains Mono';
    ctx.textAlign = 'left';
    ctx.fillText(formatPrice(lastPrice), W - padding.right + 4, priceY + 3);
}

function drawHLine(ctx, y, x1, x2, color, label) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 2]);
    ctx.beginPath();
    ctx.moveTo(x1, y);
    ctx.lineTo(x2, y);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = color;
    ctx.font = '10px JetBrains Mono';
    ctx.textAlign = 'right';
    ctx.fillText(label, x2 - 5, y - 4);
}

// ── Settings Modal ───────────────────────────────────────

function openSettings() {
    document.getElementById('settings-modal').classList.add('active');
}

function closeSettings() {
    document.getElementById('settings-modal').classList.remove('active');
}

// ── Helpers ──────────────────────────────────────────────

function updateButtons() {
    document.getElementById('btn-start').style.display = isRunning ? 'none' : '';
    document.getElementById('btn-stop').style.display = isRunning ? '' : 'none';
}

function setIndicator(id, value, cls) {
    const el = document.getElementById(id);
    if (el) {
        el.textContent = value ?? '--';
        if (cls) el.className = `indicator-value ${cls}`;
    }
}

function formatPrice(price) {
    if (!price || price === 0) return '--';
    if (price >= 1000) return price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (price >= 1) return price.toFixed(4);
    return price.toFixed(6);
}

function formatNum(n) {
    return Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatVolume(v) {
    if (!v) return '--';
    if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return v.toFixed(0);
}

function formatTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
