#!/usr/bin/env python3
"""
Web dashboard — single-page live monitor + settings/history/export pages.

Run: python scripts/dashboard_server.py
Access: http://127.0.0.1:8080
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading_agent import config

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


DB_PATH = config.DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_app() -> "FastAPI":
    app = FastAPI(title="BTCUSDT Scalper Dashboard", version="1.0")

    # ── API routes ──────────────────────────────────────────────

    @app.get("/api/status")
    async def status():
        """Live status for the monitor."""
        conn = get_conn()
        try:
            # Equity
            eq = conn.execute(
                "SELECT * FROM equity_curve ORDER BY id DESC LIMIT 1"
            ).fetchone()
            equity_data = dict(eq) if eq else {
                "equity_usdt": 0, "peak_equity": 0, "drawdown_pct": 0,
                "total_trades": 0, "total_wins": 0, "win_rate": 0, "total_net_pnl": 0,
            }

            # Recent trades (today)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_trades = conn.execute(
                """SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN net_pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                   SUM(net_pnl_usd) as net_pnl
                   FROM trade_decisions
                   WHERE decision LIKE 'EXIT_%' AND timestamp_utc LIKE ?""",
                (f"{today}%",),
            ).fetchone()

            # Kill switch / regime from latest decision
            latest = conn.execute(
                "SELECT regime, decision, skip_reason FROM trade_decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()

            # Token usage today
            tokens = conn.execute(
                "SELECT SUM(total_tokens) as total FROM token_usage WHERE timestamp_utc LIKE ?",
                (f"{today}%",),
            ).fetchone()

            return {
                "equity": equity_data,
                "today": dict(today_trades) if today_trades else {},
                "latest_decision": dict(latest) if latest else {},
                "tokens_today": (tokens["total"] or 0) if tokens else 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        finally:
            conn.close()

    @app.get("/api/journal")
    async def journal(limit: int = 20):
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM learning_journal ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"entries": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/trades")
    async def trades(limit: int = 100, source: str | None = None):
        conn = get_conn()
        try:
            query = "SELECT * FROM trade_decisions WHERE decision LIKE 'EXIT_%'"
            params = []
            if source:
                query += " AND trade_source = ?"
                params.append(source)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return {"trades": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/tuning")
    async def tuning(limit: int = 50):
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM parameter_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"history": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/settings")
    async def get_settings():
        """Current bot settings (non-secret)."""
        return {
            "MIN_CONFIDENCE": config.MIN_CONFIDENCE,
            "TRADE_QUALITY_MIN": config.TRADE_QUALITY_MIN,
            "MAX_ENTRY_EXTENSION_ATR": config.MAX_ENTRY_EXTENSION_ATR,
            "MIN_VOLUME_RATIO": config.MIN_VOLUME_RATIO,
            "ADX_MIN": config.ADX_MIN,
            "CHOP_MAX": config.CHOP_MAX,
            "AUTONOMOUS_TUNING_ENABLED": config.AUTONOMOUS_TUNING_ENABLED,
            "ENABLE_FALLBACK_OVERRIDE": config.ENABLE_FALLBACK_OVERRIDE,
            "MAX_LEVERAGE": config.MAX_LEVERAGE,
            "DAILY_MAX_LOSS_PCT": config.DAILY_MAX_LOSS_PCT,
            "WEEKLY_MAX_LOSS_PCT": config.WEEKLY_MAX_LOSS_PCT,
            "EQUITY_FLOOR_USDT": config.EQUITY_FLOOR_USDT,
            "DD_HALT_PCT": config.DD_HALT_PCT,
            "BYBIT_TESTNET": config.BYBIT_TESTNET,
            "LLM_MODEL": config.LLM_MODEL,
        }

    @app.post("/api/settings/freeze-tuning")
    async def freeze_tuning():
        config.AUTONOMOUS_TUNING_ENABLED = False
        return {"success": True, "message": "All tuning frozen"}

    @app.post("/api/settings/rollback-all")
    async def rollback_all():
        """Rollback all Tier 1/2 to defaults by reloading from env."""
        from dotenv import load_dotenv
        load_dotenv(override=True)
        config.AUTONOMOUS_TUNING_ENABLED = False
        return {"success": True, "message": "Parameters reloaded from .env, tuning frozen"}

    @app.post("/api/manual/open-long")
    async def manual_open_long():
        return {"success": True, "trade_source": "MANUAL_OWNER", "direction": "LONG",
                "message": "Manual LONG — execute via exchange API"}

    @app.post("/api/manual/open-short")
    async def manual_open_short():
        return {"success": True, "trade_source": "MANUAL_OWNER", "direction": "SHORT",
                "message": "Manual SHORT — execute via exchange API"}

    @app.post("/api/manual/close-all")
    async def manual_close_all():
        return {"success": True, "exit_type": "MANUAL_OWNER",
                "message": "Manual close — execute via exchange API"}

    @app.get("/api/candles")
    async def candles(interval: str = "15", limit: int = 200):
        """Fetch candles from Bybit for the chart."""
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet=config.BYBIT_TESTNET,
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
            )
            result = session.get_kline(
                category=config.CATEGORY,
                symbol=config.SYMBOL,
                interval=interval,
                limit=limit,
            )
            out = []
            for item in reversed(result["result"]["list"]):
                out.append({
                    "time": int(item[0]) // 1000,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return {"candles": out, "symbol": config.SYMBOL}
        except ImportError:
            return {"candles": [], "error": "pybit not installed"}
        except Exception as e:
            return {"candles": [], "error": str(e)}

    @app.get("/api/balance")
    async def balance():
        """Fetch account balance from Bybit."""
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet=config.BYBIT_TESTNET,
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
            )
            result = session.get_wallet_balance(accountType="UNIFIED")
            coins = result["result"]["list"][0]["coin"]
            usdt = next((c for c in coins if c["coin"] == "USDT"), None)
            return {
                "equity": float(usdt["equity"]) if usdt else 0,
                "available": float(usdt["availableToWithdraw"]) if usdt else 0,
                "wallet": float(usdt["walletBalance"]) if usdt else 0,
                "unrealised_pnl": float(usdt["unrealisedPnl"]) if usdt else 0,
            }
        except ImportError:
            return {"equity": 0, "error": "pybit not installed"}
        except Exception as e:
            return {"equity": 0, "error": str(e)}

    # ── Frontend ────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _DASHBOARD_HTML

    return app


# ── Dashboard HTML ──────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTCUSDT Scalper</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Courier New',monospace;background:#0a0a0a;color:#e0e0e0;height:100vh;overflow:hidden}
.header{background:#111;padding:6px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #333;font-size:13px}
.header .title{color:#00ff88;font-weight:bold;font-size:15px}
.header .stats{display:flex;gap:20px}
.header .stats span{color:#aaa}
.header .stats .val{color:#fff;font-weight:bold}
.header .stats .pos{color:#00ff88}.header .stats .neg{color:#ff4444}
.main{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:auto 1fr;height:calc(100vh - 72px);gap:0}
.panel{border:1px solid #222;padding:10px;overflow:hidden}
.panel h3{color:#888;font-size:11px;text-transform:uppercase;margin-bottom:6px;letter-spacing:1px}
.chart-panel{grid-column:1;grid-row:1}
.learn-panel{grid-column:2;grid-row:1/3;overflow-y:auto;max-height:calc(100vh - 110px)}
.controls-panel{grid-column:1;grid-row:2;display:grid;grid-template-rows:auto auto 1fr}
.chart-area{background:#111;height:200px;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#444;font-size:12px}
.status-bar{display:flex;gap:12px;margin-top:6px;font-size:11px;flex-wrap:wrap}
.status-bar .tag{padding:2px 6px;border-radius:3px;background:#1a1a1a}
.status-bar .regime-trending{color:#00ff88}.status-bar .regime-ranging{color:#ff8800}
.status-bar .regime-dead{color:#666}.status-bar .regime-spike{color:#ff4444}
.btn{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:bold}
.btn-long{background:#00aa55;color:#fff}.btn-long:hover{background:#00cc66}
.btn-short{background:#cc3333;color:#fff}.btn-short:hover{background:#ee4444}
.btn-close{background:#666;color:#fff}.btn-close:hover{background:#888}
.btn-row{display:flex;gap:8px;margin:8px 0}
.pos-status{font-size:12px;padding:6px;background:#111;border-radius:4px;margin-top:4px}
.journal-entry{padding:6px 0;border-bottom:1px solid #1a1a1a;font-size:11px;line-height:1.4}
.journal-entry .time{color:#666;font-size:10px}
.journal-entry .icon-win{color:#00ff88}.journal-entry .icon-loss{color:#ff4444}
.journal-entry .icon-skip{color:#4488ff}.journal-entry .icon-tune{color:#ffaa00}
.journal-entry .icon-rollback{color:#ff6600}.journal-entry .icon-regime{color:#aa66ff}
.journal-entry .icon-manual{color:#ff88ff}
.perf-row{display:grid;grid-template-columns:60px 1fr;gap:4px;font-size:11px;padding:3px 0;border-bottom:1px solid #1a1a1a}
.perf-label{color:#888}
.alert-item{font-size:11px;padding:3px 0;color:#ffaa00}
.switch-ok{color:#00ff88}.switch-fail{color:#ff4444}
.nav{display:flex;gap:0;background:#111;border-top:1px solid #333;height:32px}
.nav a{padding:8px 14px;color:#888;text-decoration:none;font-size:11px;border-right:1px solid #222}
.nav a:hover,.nav a.active{color:#fff;background:#1a1a1a}
</style>
</head>
<body>
<div class="header">
  <span class="title">BTCUSDT SCALPER</span>
  <div class="stats">
    <span>EQUITY <span class="val" id="equity">$0</span></span>
    <span>PEAK <span class="val" id="peak">$0</span></span>
    <span>DD <span class="val" id="dd">0%</span></span>
    <span>W/R <span class="val" id="wr">0%</span></span>
    <span>PnL <span class="val" id="pnl">$0</span></span>
    <span>Tokens <span class="val" id="tokens">0</span></span>
  </div>
</div>
<div class="main">
  <div class="panel chart-panel">
    <h3>Chart</h3>
    <div class="chart-area" id="chart">Chart: candles + EMA21 overlay + trade markers</div>
    <div class="status-bar" id="statusBar">
      <span class="tag" id="regimeTag">WAITING</span>
      <span class="tag" id="licenseTag">License: --</span>
      <span class="tag" id="skipsTag">Skips: 0</span>
      <span class="tag" id="switchesTag"></span>
    </div>
  </div>
  <div class="panel learn-panel">
    <h3>Bot Learned</h3>
    <div id="journalList"></div>
  </div>
  <div class="panel controls-panel">
    <div>
      <h3>Manual Controls</h3>
      <div class="btn-row">
        <button class="btn btn-long" onclick="manualAction('open-long')">OPEN LONG</button>
        <button class="btn btn-short" onclick="manualAction('open-short')">OPEN SHORT</button>
        <button class="btn btn-close" onclick="manualAction('close-all')">CLOSE NOW</button>
      </div>
      <div class="pos-status" id="posStatus">Position: FLAT</div>
    </div>
    <div style="margin-top:8px">
      <h3>Performance</h3>
      <div id="perfStats"></div>
    </div>
    <div style="margin-top:8px">
      <h3>Alerts</h3>
      <div id="alertsList"></div>
    </div>
  </div>
</div>
<div class="nav">
  <a href="/" class="active">Live Monitor</a>
  <a href="#settings" onclick="alert('Settings page — configure via .env or API')">Settings</a>
  <a href="#history" onclick="alert('Trade History — GET /api/trades')">Trade History</a>
  <a href="#journal" onclick="alert('Full Journal — GET /api/journal')">Full Journal</a>
  <a href="#tuning" onclick="alert('Tuning Log — GET /api/tuning')">Tuning Log</a>
  <a href="#export" onclick="alert('Export — python scripts/export_data.py --help')">Export</a>
</div>
<script>
const REFRESH = """ + str(config.DASHBOARD_REFRESH_SEC * 1000) + """;
const ICONS = {POST_WIN:'icon-win',POST_LOSS:'icon-loss',POST_SKIP_REVIEW:'icon-skip',
  TUNING_CYCLE:'icon-tune',ROLLBACK:'icon-rollback',REGIME_SHIFT:'icon-regime',
  EDGE_DECAY:'icon-loss',META_LEARNING:'icon-tune',PARAMETER_INSIGHT:'icon-tune',
  MANUAL_OWNER:'icon-manual'};
const LABELS = {POST_WIN:'WIN',POST_LOSS:'LOSS',POST_SKIP_REVIEW:'SKIP',
  TUNING_CYCLE:'TUNING',ROLLBACK:'ROLLBACK',REGIME_SHIFT:'REGIME',
  EDGE_DECAY:'EDGE',META_LEARNING:'META',PARAMETER_INSIGHT:'INSIGHT'};

async function refresh(){
  try{
    const [status,journal] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/journal?limit=12').then(r=>r.json()),
    ]);
    const eq = status.equity||{};
    document.getElementById('equity').textContent = '$'+(eq.equity_usdt||0).toFixed(0);
    document.getElementById('peak').textContent = '$'+(eq.peak_equity||0).toFixed(0);
    const dd = eq.drawdown_pct||0;
    const ddEl = document.getElementById('dd');
    ddEl.textContent = dd.toFixed(1)+'%';
    ddEl.className = dd>3?'val neg':'val';
    document.getElementById('wr').textContent = (eq.win_rate||0).toFixed(1)+'%';
    const pnl = eq.total_net_pnl||0;
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+pnl.toFixed(0);
    pnlEl.className = pnl>=0?'val pos':'val neg';
    document.getElementById('tokens').textContent = (status.tokens_today||0).toLocaleString();

    const regime = (status.latest_decision||{}).regime||'--';
    const regimeEl = document.getElementById('regimeTag');
    regimeEl.textContent = regime;
    regimeEl.className = 'tag regime-'+regime.toLowerCase().replace(/_/g,'-');

    // Journal
    const jList = document.getElementById('journalList');
    jList.innerHTML = (journal.entries||[]).map(e=>{
      const cls = ICONS[e.entry_type]||'icon-skip';
      const label = LABELS[e.entry_type]||e.entry_type;
      const time = (e.timestamp_utc||'').substring(11,16);
      const obs = (e.observation||'').substring(0,120);
      const conc = (e.conclusion||'').substring(0,100);
      return '<div class="journal-entry"><span class="time">'+time+'</span> '+
        '<span class="'+cls+'">'+label+'</span> '+obs+
        (conc?' — <em>'+conc+'</em>':'')+'</div>';
    }).join('');

    // Today stats
    const t = status.today||{};
    document.getElementById('perfStats').innerHTML =
      '<div class="perf-row"><span class="perf-label">TODAY</span><span>'+
      (t.cnt||0)+'t '+(t.wins||0)+'W '+(t.losses||0)+'L $'+(t.net_pnl||0).toFixed(0)+'</span></div>'+
      '<div class="perf-row"><span class="perf-label">ALL</span><span>'+
      (eq.total_trades||0)+'t WR='+(eq.win_rate||0).toFixed(1)+'% $'+(eq.total_net_pnl||0).toFixed(0)+'</span></div>';

  }catch(e){console.error('Refresh error:',e)}
}
async function manualAction(action){
  if(!confirm('Confirm manual '+action+'?'))return;
  const r = await fetch('/api/manual/'+action,{method:'POST'});
  const d = await r.json();
  alert(d.message||JSON.stringify(d));
  refresh();
}
refresh();
setInterval(refresh, REFRESH);
</script>
</body>
</html>"""


def main():
    if not HAS_FASTAPI:
        print("FastAPI not installed. Install with: pip install fastapi uvicorn")
        print("Dashboard API is still available via the app object.")
        sys.exit(1)

    import uvicorn
    app = create_app()
    print(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    uvicorn.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT)


if __name__ == "__main__":
    main()
