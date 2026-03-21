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

# Import canonical schemas from the modules that own them
from trading_agent.data_collector import (
    _TRADE_DECISIONS_SQL, _MARKET_SNAPSHOTS_SQL, _EQUITY_CURVE_SQL, _DAILY_SESSIONS_SQL,
)
from trading_agent.learning_journal import _JOURNAL_SQL
from trading_agent.self_optimizer import _PARAM_HISTORY_SQL

_ENSURE_TABLES_SQL = ";".join([
    _TRADE_DECISIONS_SQL, _MARKET_SNAPSHOTS_SQL, _EQUITY_CURVE_SQL, _DAILY_SESSIONS_SQL,
    _JOURNAL_SQL, _PARAM_HISTORY_SQL,
    """CREATE TABLE IF NOT EXISTS token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_utc TEXT, call_type TEXT, input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        cached INTEGER DEFAULT 0, model TEXT, cost_usd REAL DEFAULT 0
    )""",
])

_tables_ensured = False


def get_conn():
    global _tables_ensured
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _tables_ensured:
        conn.executescript(_ENSURE_TABLES_SQL)
        _tables_ensured = True
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
        symbol = getattr(config, "SYMBOL", "BTCUSDT")
        category = getattr(config, "CATEGORY", "linear")
        # Try pybit first, fall back to public REST API (no auth needed for kline)
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet=config.BYBIT_TESTNET,
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
            )
            result = session.get_kline(
                category=category,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            raw_list = result["result"]["list"]
        except Exception:
            # Fallback: public Bybit v5 REST API (no pybit needed)
            import urllib.request, json as _json
            base = "https://api-testnet.bybit.com" if getattr(config, "BYBIT_TESTNET", False) else "https://api.bybit.com"
            url = f"{base}/v5/market/kline?category={category}&symbol={symbol}&interval={interval}&limit={limit}"
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = _json.loads(resp.read())
                raw_list = data["result"]["list"]
            except Exception as e2:
                return {"candles": [], "error": str(e2)}
        try:
            out = []
            for item in reversed(raw_list):
                out.append({
                    "time": int(item[0]) // 1000,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return {"candles": out, "symbol": symbol}
        except Exception as e:
            return {"candles": [], "error": str(e)}

    @app.get("/api/balance")
    async def balance():
        """Fetch account balance from Bybit."""
        if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
            return {"equity": 0, "error": "API keys not configured in .env"}
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet=config.BYBIT_TESTNET,
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
            )
            # Try UNIFIED first, then CONTRACT, then SPOT
            for account_type in ("UNIFIED", "CONTRACT", "SPOT"):
                try:
                    result = session.get_wallet_balance(accountType=account_type)
                    acct_list = result.get("result", {}).get("list", [])
                    if not acct_list:
                        continue
                    coins = acct_list[0].get("coin", [])
                    usdt = next((c for c in coins if c["coin"] == "USDT"), None)
                    if usdt and float(usdt.get("equity", 0)) > 0:
                        return {
                            "equity": float(usdt.get("equity", 0)),
                            "available": float(usdt.get("availableToWithdraw", 0)),
                            "wallet": float(usdt.get("walletBalance", 0)),
                            "unrealised_pnl": float(usdt.get("unrealisedPnl", 0)),
                            "account_type": account_type,
                        }
                except Exception:
                    continue
            # Fallback: return total equity from first account found
            result = session.get_wallet_balance(accountType="UNIFIED")
            acct_list = result.get("result", {}).get("list", [])
            if acct_list:
                total_eq = acct_list[0].get("totalEquity", "0")
                return {
                    "equity": float(total_eq),
                    "available": 0,
                    "wallet": float(total_eq),
                    "unrealised_pnl": 0,
                    "account_type": "UNIFIED",
                    "note": "totalEquity fallback",
                }
            return {"equity": 0, "error": "No USDT balance found on any account type"}
        except ImportError:
            return {"equity": 0, "error": "pybit not installed"}
        except Exception as e:
            return {"equity": 0, "error": str(e)}

    @app.get("/api/positions")
    async def positions():
        """Fetch open positions from Bybit — live PnL."""
        if not config.BYBIT_API_KEY:
            return {"positions": [], "error": "No API keys"}
        try:
            from pybit.unified_trading import HTTP
            session = HTTP(
                testnet=config.BYBIT_TESTNET,
                api_key=config.BYBIT_API_KEY,
                api_secret=config.BYBIT_API_SECRET,
            )
            result = session.get_positions(category=config.CATEGORY, symbol=config.SYMBOL)
            pos_list = result.get("result", {}).get("list", [])
            out = []
            for p in pos_list:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                out.append({
                    "side": p.get("side", ""),
                    "size": size,
                    "size_usd": float(p.get("positionValue", 0)),
                    "entry_price": float(p.get("avgPrice", 0)),
                    "mark_price": float(p.get("markPrice", 0)),
                    "liq_price": float(p.get("liqPrice", 0)),
                    "unrealised_pnl": float(p.get("unrealisedPnl", 0)),
                    "realised_pnl": float(p.get("cumRealisedPnl", 0)),
                    "leverage": p.get("leverage", "1"),
                    "stop_loss": float(p.get("stopLoss", 0)),
                    "take_profit": float(p.get("takeProfit", 0)),
                    "pnl_pct": float(p.get("unrealisedPnl", 0)) / float(p.get("positionValue", 1)) * 100 if float(p.get("positionValue", 0)) > 0 else 0,
                    "created_time": p.get("createdTime", ""),
                })
            return {"positions": out}
        except Exception as e:
            return {"positions": [], "error": str(e)}

    @app.get("/api/bot-positions")
    async def bot_positions():
        """Return bot's internal position tracking (more detailed than exchange API)."""
        try:
            from main import get_shared_positions
            return {"positions": get_shared_positions()}
        except Exception as e:
            return {"positions": [], "error": str(e)}

    @app.get("/api/strategy")
    async def strategy():
        """Return current strategy prompt and evolution history."""
        try:
            from trading_agent.strategy_evolution import StrategyEvolution
            evo = StrategyEvolution()
            history = evo.get_history(limit=20)
            return {
                "version": evo.current_version,
                "prompt": evo.current_prompt,
                "history": history,
            }
        except Exception as e:
            return {"version": 0, "prompt": f"Error: {e}", "history": []}

    @app.post("/api/strategy/evolve")
    async def strategy_evolve():
        """Force a strategy evolution cycle."""
        try:
            from trading_agent.strategy_evolution import StrategyEvolution
            from trading_agent.learning_journal import LearningJournal

            evo = StrategyEvolution()
            journal = LearningJournal()

            # Gather performance data
            conn = get_conn()
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                stats_row = conn.execute(
                    """SELECT COUNT(*) as total,
                       SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN net_pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                       SUM(net_pnl_usd) as total_pnl,
                       AVG(CASE WHEN net_pnl_usd > 0 THEN net_pnl_usd END) as avg_win,
                       AVG(CASE WHEN net_pnl_usd <= 0 THEN net_pnl_usd END) as avg_loss,
                       MAX(net_pnl_usd) as best,
                       MIN(net_pnl_usd) as worst
                       FROM trade_decisions
                       WHERE decision LIKE 'EXIT_%'"""
                ).fetchone()
            finally:
                conn.close()

            total = stats_row[0] or 0
            wins = stats_row[1] or 0
            trade_stats = {
                "total_trades": total,
                "wins": wins,
                "losses": stats_row[2] or 0,
                "win_rate": (wins / total * 100) if total > 0 else 0,
                "total_pnl": stats_row[3] or 0,
                "avg_win": stats_row[4] or 0,
                "avg_loss": stats_row[5] or 0,
                "best_trade": stats_row[6] or 0,
                "worst_trade": stats_row[7] or 0,
            }

            journal_entries = journal.get_recent_entries(limit=25)
            perf_summary = (
                f"Total: {total} trades, {wins}W/{trade_stats['losses']}L, "
                f"WR: {trade_stats['win_rate']:.1f}%, PnL: ${trade_stats['total_pnl']:.2f}"
            )

            import asyncio
            result = await evo.evolve(journal_entries, perf_summary, trade_stats)
            if result:
                # Update the live AI brain prompt
                try:
                    from trading_agent.ai_brain import set_system_prompt
                    set_system_prompt(evo.current_prompt)
                except Exception:
                    pass
                return {"message": f"Strategy evolved to v{result}", "version": result}
            else:
                return {"message": "Evolution skipped (not enough data or no improvement found)"}
        except Exception as e:
            return {"message": f"Error: {e}"}

    @app.post("/api/strategy/rollback")
    async def strategy_rollback():
        """Rollback strategy to previous version."""
        try:
            from trading_agent.strategy_evolution import StrategyEvolution
            evo = StrategyEvolution()
            old_v = evo.current_version
            evo.rollback()
            try:
                from trading_agent.ai_brain import set_system_prompt
                set_system_prompt(evo.current_prompt)
            except Exception:
                pass
            return {"message": f"Rolled back from v{old_v} to v{evo.current_version}"}
        except Exception as e:
            return {"message": f"Error: {e}"}

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
<script src="https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Courier New',monospace;background:#0a0a0a;color:#e0e0e0;height:100vh;overflow:hidden}
.header{background:#111;padding:6px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #333;font-size:13px}
.header .title{color:#00ff88;font-weight:bold;font-size:15px}
.header .stats{display:flex;gap:20px;flex-wrap:wrap}
.header .stats span{color:#aaa}
.header .stats .val{color:#fff;font-weight:bold}
.header .stats .pos{color:#00ff88}.header .stats .neg{color:#ff4444}
.nav{display:flex;gap:0;background:#111;border-bottom:1px solid #333;height:32px}
.nav a{padding:8px 14px;color:#888;text-decoration:none;font-size:11px;border-right:1px solid #222;cursor:pointer}
.nav a:hover,.nav a.active{color:#fff;background:#1a1a1a}
.page{display:none;height:calc(100vh - 72px);overflow:hidden}
.page.active{display:block}
/* Live Monitor */
.main{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:auto 1fr;height:100%;gap:0}
.panel{border:1px solid #222;padding:10px;overflow:hidden}
.panel h3{color:#888;font-size:11px;text-transform:uppercase;margin-bottom:6px;letter-spacing:1px}
.chart-panel{grid-column:1;grid-row:1}
.learn-panel{grid-column:2;grid-row:1/3;overflow-y:auto}
.controls-panel{grid-column:1;grid-row:2;display:grid;grid-template-rows:auto auto 1fr}
.chart-area{background:#111;height:280px;border-radius:4px}
.status-bar{display:flex;gap:12px;margin-top:6px;font-size:11px;flex-wrap:wrap}
.status-bar .tag{padding:2px 6px;border-radius:3px;background:#1a1a1a}
.status-bar .regime-trending{color:#00ff88}.status-bar .regime-ranging{color:#ff8800}
.status-bar .regime-dead{color:#666}.status-bar .regime-spike{color:#ff4444}
.btn{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:bold}
.btn-long{background:#00aa55;color:#fff}.btn-long:hover{background:#00cc66}
.btn-short{background:#cc3333;color:#fff}.btn-short:hover{background:#ee4444}
.btn-close{background:#666;color:#fff}.btn-close:hover{background:#888}
.btn-sm{padding:5px 12px;font-size:11px}
.btn-blue{background:#2266cc;color:#fff}.btn-blue:hover{background:#3377dd}
.btn-orange{background:#cc6600;color:#fff}.btn-orange:hover{background:#dd7700}
.btn-row{display:flex;gap:8px;margin:8px 0}
.pos-status{font-size:12px;padding:6px;background:#111;border-radius:4px;margin-top:4px}
.journal-entry{padding:6px 0;border-bottom:1px solid #1a1a1a;font-size:11px;line-height:1.4}
.journal-entry .time{color:#666;font-size:10px}
.icon-win{color:#00ff88}.icon-loss{color:#ff4444}
.icon-skip{color:#4488ff}.icon-tune{color:#ffaa00}
.icon-rollback{color:#ff6600}.icon-regime{color:#aa66ff}
.icon-manual{color:#ff88ff}
.icon-paper{color:#ffdd44}.icon-cycle{color:#66cccc}
.perf-row{display:grid;grid-template-columns:60px 1fr;gap:4px;font-size:11px;padding:3px 0;border-bottom:1px solid #1a1a1a}
.perf-label{color:#888}
/* Subpages */
.subpage{padding:16px;overflow-y:auto;height:100%}
.subpage h2{color:#00ff88;font-size:14px;margin-bottom:12px}
table.data{width:100%;border-collapse:collapse;font-size:11px}
table.data th{text-align:left;color:#888;padding:6px 8px;border-bottom:1px solid #333;position:sticky;top:0;background:#0a0a0a}
table.data td{padding:5px 8px;border-bottom:1px solid #1a1a1a}
table.data tr:hover{background:#111}
.td-pos{color:#00ff88}.td-neg{color:#ff4444}
.setting-row{display:grid;grid-template-columns:250px 120px 80px;gap:8px;padding:6px 0;border-bottom:1px solid #1a1a1a;font-size:12px;align-items:center}
.setting-row label{color:#aaa}.setting-row input{background:#111;border:1px solid #333;color:#fff;padding:4px 8px;font-family:inherit;border-radius:3px;width:100%}
.setting-row .cur{color:#666;font-size:10px}
.msg{padding:8px 12px;border-radius:4px;margin:8px 0;font-size:12px}
.msg-ok{background:#0a2a0a;color:#00ff88;border:1px solid #00ff88}
.msg-err{background:#2a0a0a;color:#ff4444;border:1px solid #ff4444}
</style>
</head>
<body>
<div class="header">
  <span class="title">BTCUSDT SCALPER</span>
  <div class="stats">
    <span>BALANCE <span class="val" id="balance">--</span></span>
    <span>EQUITY <span class="val" id="equity">$0</span></span>
    <span>DD <span class="val" id="dd">0%</span></span>
    <span>W/R <span class="val" id="wr">0%</span></span>
    <span>PnL <span class="val" id="pnl">$0</span></span>
    <span>Tokens <span class="val" id="tokens">0</span></span>
  </div>
</div>
<div class="nav" id="navBar">
  <a onclick="showPage('monitor')" class="active" data-page="monitor">Live Monitor</a>
  <a onclick="showPage('settings')" data-page="settings">Settings</a>
  <a onclick="showPage('history')" data-page="history">Trade History</a>
  <a onclick="showPage('journal')" data-page="journal">Full Journal</a>
  <a onclick="showPage('tuning')" data-page="tuning">Tuning Log</a>
  <a onclick="showPage('strategy')" data-page="strategy">Strategy</a>
</div>

<!-- PAGE: Live Monitor -->
<div class="page active" id="page-monitor">
<div class="main">
  <div class="panel chart-panel">
    <h3>Chart <span style="color:#555;font-size:10px">+ EMA21</span>
      <span style="margin-left:12px">
        <button onclick="changeInterval('1')" id="btn-tf-1" class="tf-btn" style="padding:2px 8px;font-size:10px;background:#222;color:#888;border:1px solid #333;border-radius:3px;cursor:pointer">1m</button>
        <button onclick="changeInterval('3')" id="btn-tf-3" class="tf-btn" style="padding:2px 8px;font-size:10px;background:#222;color:#888;border:1px solid #333;border-radius:3px;cursor:pointer">3m</button>
        <button onclick="changeInterval('15')" id="btn-tf-15" class="tf-btn tf-active" style="padding:2px 8px;font-size:10px;background:#333;color:#fff;border:1px solid #555;border-radius:3px;cursor:pointer">15m</button>
      </span>
    </h3>
    <div class="chart-area" id="chart"></div>
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
      <h3>Open Positions</h3>
      <div id="openPositions" style="font-size:11px"></div>
    </div>
    <div style="margin-top:8px">
      <h3>Performance</h3>
      <div id="perfStats"></div>
    </div>
  </div>
</div>
</div>

<!-- PAGE: Settings -->
<div class="page" id="page-settings">
<div class="subpage">
  <h2>Bot Settings</h2>
  <div class="btn-row">
    <button class="btn btn-orange btn-sm" onclick="freezeTuning()">Freeze All Tuning</button>
    <button class="btn btn-close btn-sm" onclick="rollbackAll()">Rollback to .env Defaults</button>
  </div>
  <div id="settingsMsg"></div>
  <div id="settingsList"></div>
</div>
</div>

<!-- PAGE: Trade History -->
<div class="page" id="page-history">
<div class="subpage">
  <h2>Trade History</h2>
  <div id="historyTable" style="overflow-y:auto;max-height:calc(100vh - 140px)"></div>
</div>
</div>

<!-- PAGE: Full Journal -->
<div class="page" id="page-journal">
<div class="subpage">
  <h2>Learning Journal</h2>
  <div id="journalTable" style="overflow-y:auto;max-height:calc(100vh - 140px)"></div>
</div>
</div>

<!-- PAGE: Tuning Log -->
<div class="page" id="page-tuning">
<div class="subpage">
  <h2>Parameter Tuning Log</h2>
  <div id="tuningTable" style="overflow-y:auto;max-height:calc(100vh - 140px)"></div>
</div>
</div>

<!-- PAGE: Strategy -->
<div class="page" id="page-strategy">
<div class="subpage">
  <h2>AI Strategy <span id="stratVersion" style="color:#888;font-size:14px"></span></h2>
  <div class="btn-row" style="margin-bottom:12px">
    <button class="btn btn-orange btn-sm" onclick="forceEvolve()">Force Evolution Now</button>
    <button class="btn btn-close btn-sm" onclick="rollbackStrategy()">Rollback to Previous</button>
  </div>
  <div id="stratMsg"></div>
  <h3 style="color:#00ff88;margin-top:12px">Current Strategy Prompt</h3>
  <pre id="stratPrompt" style="background:#111;padding:12px;border-radius:6px;white-space:pre-wrap;word-wrap:break-word;font-size:12px;max-height:50vh;overflow-y:auto;border:1px solid #333;color:#ddd"></pre>
  <h3 style="color:#ff8800;margin-top:16px">Evolution History</h3>
  <div id="stratHistory" style="overflow-y:auto;max-height:35vh"></div>
</div>
</div>

<script>
const REFRESH = """ + str(config.DASHBOARD_REFRESH_SEC * 1000) + """;
const ICONS = {POST_WIN:'icon-win',POST_LOSS:'icon-loss',POST_SKIP_REVIEW:'icon-skip',
  TUNING_CYCLE:'icon-tune',ROLLBACK:'icon-rollback',REGIME_SHIFT:'icon-regime',
  EDGE_DECAY:'icon-loss',META_LEARNING:'icon-tune',PARAMETER_INSIGHT:'icon-tune',
  MANUAL_OWNER:'icon-manual',PAPER_TRADE:'icon-paper',CYCLE_OBSERVATION:'icon-cycle',STRATEGY_EVOLUTION:'icon-tune'};
const LABELS = {POST_WIN:'WIN',POST_LOSS:'LOSS',POST_SKIP_REVIEW:'SKIP',
  TUNING_CYCLE:'TUNING',ROLLBACK:'ROLLBACK',REGIME_SHIFT:'REGIME',
  EDGE_DECAY:'EDGE',META_LEARNING:'META',PARAMETER_INSIGHT:'INSIGHT',
  PAPER_TRADE:'PAPER',CYCLE_OBSERVATION:'CYCLE',STRATEGY_EVOLUTION:'STRATEGY'};

let currentPage = 'monitor';
let chart = null, candleSeries = null, emaSeries = null;
let chartInterval = '3';  // default 3m for live feel
let chartAutoRefresh = null;

// ── SPA Navigation ──
function showPage(page){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(a=>a.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  document.querySelector('[data-page="'+page+'"]').classList.add('active');
  currentPage = page;
  if(page==='settings') loadSettings();
  if(page==='history') loadHistory();
  if(page==='journal') loadFullJournal();
  if(page==='tuning') loadTuning();
  if(page==='strategy') loadStrategy();
  if(page==='monitor' && !chart) changeInterval(chartInterval);
}

// ── Chart (TradingView Lightweight Charts) ──
function calcEMA(data, period){
  const ema = [];
  const k = 2/(period+1);
  let prev = data[0].close;
  for(let i=0;i<data.length;i++){
    prev = i===0 ? data[i].close : data[i].close*k + prev*(1-k);
    ema.push({time:data[i].time, value:Math.round(prev*100)/100});
  }
  return ema;
}

function changeInterval(tf){
  chartInterval = tf;
  document.querySelectorAll('.tf-btn').forEach(b=>{b.style.background='#222';b.style.color='#888';b.style.border='1px solid #333';});
  const active = document.getElementById('btn-tf-'+tf);
  if(active){active.style.background='#333';active.style.color='#fff';active.style.border='1px solid #555';}
  initChart();
  // Set auto-refresh interval based on timeframe
  if(chartAutoRefresh) clearInterval(chartAutoRefresh);
  const refreshMs = tf==='1'?5000 : tf==='3'?10000 : 30000;
  chartAutoRefresh = setInterval(refreshChart, refreshMs);
}

async function initChart(){
  const el = document.getElementById('chart');
  if(chart){chart.remove();chart=null;}
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: 280,
    layout:{background:{color:'#111'},textColor:'#888'},
    grid:{vertLines:{color:'#1a1a1a'},horzLines:{color:'#1a1a1a'}},
    crosshair:{mode:0},
    timeScale:{timeVisible:true,secondsVisible:false},
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#00cc55',downColor:'#cc3333',borderVisible:false,
    wickUpColor:'#00cc55',wickDownColor:'#cc3333',
  });
  emaSeries = chart.addLineSeries({color:'#ffaa00',lineWidth:1,priceLineVisible:false});
  try{
    const r = await fetch('/api/candles?interval='+chartInterval+'&limit=200');
    const d = await r.json();
    if(d.candles && d.candles.length){
      candleSeries.setData(d.candles);
      emaSeries.setData(calcEMA(d.candles,21));
      chart.timeScale().fitContent();
    }
  }catch(e){console.error('Chart error:',e)}
  window.addEventListener('resize',()=>{if(chart)chart.resize(el.clientWidth,280)});
  // Start auto-refresh for chart
  if(chartAutoRefresh) clearInterval(chartAutoRefresh);
  const refreshMs = chartInterval==='1'?5000 : chartInterval==='3'?10000 : 30000;
  chartAutoRefresh = setInterval(refreshChart, refreshMs);
}

function utcToLocal(utcStr){
  if(!utcStr)return '';
  let s=utcStr;
  if(!s.endsWith('Z')&&!s.includes('+'))s+='Z';
  const d=new Date(s);
  if(isNaN(d))return utcStr.substring(11,16);
  return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}

async function refreshChart(){
  if(!candleSeries) return;
  try{
    const r = await fetch('/api/candles?interval='+chartInterval+'&limit=200');
    const d = await r.json();
    if(d.candles && d.candles.length){
      candleSeries.setData(d.candles);
      emaSeries.setData(calcEMA(d.candles,21));
    }
  }catch(e){}
}

// ── Live Monitor refresh ──
async function refresh(){
  try{
    const [status,journal,bal,pos,botPos] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/journal?limit=12').then(r=>r.json()),
      fetch('/api/balance').then(r=>r.json()),
      fetch('/api/positions').then(r=>r.json()),
      fetch('/api/bot-positions').then(r=>r.json()),
    ]);
    // Balance from Bybit
    if(bal.equity>0){
      document.getElementById('balance').textContent = '$'+bal.equity.toFixed(2);
      document.getElementById('balance').className = 'val pos';
    } else if(bal.error){
      document.getElementById('balance').textContent = 'N/A';
    }

    const eq = status.equity||{};
    document.getElementById('equity').textContent = '$'+(eq.equity_usdt||0).toFixed(0);
    const dd = eq.drawdown_pct||0;
    const ddEl = document.getElementById('dd');
    ddEl.textContent = dd.toFixed(1)+'%';
    ddEl.className = dd>3?'val neg':'val';
    document.getElementById('wr').textContent = (eq.win_rate||0).toFixed(1)+'%';
    const pnl = eq.total_net_pnl||0;
    const pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (pnl>=0?'+':'')+pnl.toFixed(2);
    pnlEl.className = pnl>=0?'val pos':'val neg';
    document.getElementById('tokens').textContent = (status.tokens_today||0).toLocaleString();

    const regime = (status.latest_decision||{}).regime||'--';
    const regimeEl = document.getElementById('regimeTag');
    regimeEl.textContent = regime;
    regimeEl.className = 'tag regime-'+regime.toLowerCase().replace(/_/g,'-');

    // Journal sidebar
    const jList = document.getElementById('journalList');
    if(jList) jList.innerHTML = (journal.entries||[]).filter(e=>{
      // Hide empty skip reviews that add no learning value
      if(e.entry_type==='POST_SKIP_REVIEW'){
        const obs=e.observation||'';
        if(obs.includes('0/0 (0%)') || obs.includes('Top reasons: {}'))return false;
      }
      return true;
    }).map(e=>{
      const cls = ICONS[e.entry_type]||'icon-skip';
      const label = LABELS[e.entry_type]||e.entry_type;
      const time = utcToLocal(e.timestamp_utc);
      const obs = (e.observation||'').substring(0,200);
      const conc = (e.conclusion||'').substring(0,150);
      return '<div class="journal-entry"><span class="time">'+time+'</span> '+
        '<span class="'+cls+'">'+label+'</span> '+obs+
        (conc?' &mdash; <em>'+conc+'</em>':'')+'</div>';
    }).join('');

    // Open positions — prefer bot positions (shows each trade separately with SL/TP)
    // Exchange position is aggregated (1 entry for all), so bot data is more useful
    const posDiv = document.getElementById('openPositions');
    const positions = pos.positions||[];
    const botPositions = botPos.positions||[];
    const exchPos = positions.length>0 ? positions[0] : null;  // aggregated exchange data
    if(posDiv){
      if(botPositions.length>0){
        // Show each bot-tracked trade with its own SL/TP
        const totalPnl = botPositions.reduce((s,p)=>s+(p.unrealised_pnl||0),0);
        // Use exchange PnL if available (more accurate, real-time)
        const exchPnl = exchPos ? exchPos.unrealised_pnl : null;
        const displayPnl = exchPnl !== null ? exchPnl : totalPnl;
        posDiv.innerHTML = botPositions.map(p=>{
          const pnl = p.unrealised_pnl||0;
          const cls = pnl>=0?'td-pos':'td-neg';
          const side = p.direction||'SHORT';
          const sideClr = side==='LONG'?'#00cc55':'#cc3333';
          return '<div style="background:#111;padding:8px;border-radius:4px;margin:4px 0;border-left:3px solid '+sideClr+'">'+
            '<div style="display:flex;justify-content:space-between;align-items:center">'+
            '<span style="color:'+sideClr+';font-weight:bold;font-size:13px">'+side+' '+p.size+' BTC <span style="color:#555;font-size:10px">['+p.trade_id+']</span></span>'+
            '<span class="'+cls+'" style="font-size:14px;font-weight:bold">$'+pnl.toFixed(4)+'</span>'+
            '</div>'+
            '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-top:4px;color:#888;font-size:10px">'+
            '<span>Entry: $'+p.entry_price.toFixed(1)+'</span>'+
            '<span>SL: $'+p.sl_price.toFixed(1)+'</span>'+
            '<span>TP: $'+p.tp_price.toFixed(1)+'</span>'+
            '</div></div>';
        }).join('') +
        (exchPos ? '<div style="color:#555;font-size:10px;padding:4px;text-align:right">Exchange total: $'+exchPos.unrealised_pnl.toFixed(2)+' | Lev: '+exchPos.leverage+'x | Liq: $'+(exchPos.liq_price||0).toFixed(0)+'</div>' : '');
        const s0=botPositions[0].direction||'SHORT';
        document.getElementById('posStatus').textContent=s0+' '+botPositions.length+'x | PnL: $'+displayPnl.toFixed(2);
        document.getElementById('posStatus').style.color=displayPnl>=0?'#00ff88':'#ff4444';
      } else if(positions.length>0){
        // No bot positions but exchange shows something (edge case: manual trade)
        posDiv.innerHTML = positions.map(p=>{
          const pnl = p.unrealised_pnl||0;
          const cls = pnl>=0?'td-pos':'td-neg';
          const side = p.side==='Buy'?'LONG':'SHORT';
          const sideClr = side==='LONG'?'#00cc55':'#cc3333';
          return '<div style="background:#111;padding:8px;border-radius:4px;margin:4px 0;border-left:3px solid '+sideClr+'">'+
            '<div style="display:flex;justify-content:space-between;align-items:center">'+
            '<span style="color:'+sideClr+';font-weight:bold;font-size:13px">'+side+' '+p.size+' BTC</span>'+
            '<span class="'+cls+'" style="font-size:14px;font-weight:bold">$'+pnl.toFixed(2)+'</span>'+
            '</div>'+
            '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin-top:4px;color:#888;font-size:10px">'+
            '<span>Entry: $'+p.entry_price.toFixed(1)+'</span>'+
            '<span>Lev: '+p.leverage+'x</span>'+
            '<span>Liq: $'+(p.liq_price||0).toFixed(0)+'</span>'+
            '</div></div>';
        }).join('');
        const p0=positions[0];const s0=p0.side==='Buy'?'LONG':'SHORT';
        document.getElementById('posStatus').textContent=s0+' '+p0.size+' BTC | PnL: $'+(p0.unrealised_pnl||0).toFixed(2);
        document.getElementById('posStatus').style.color=(p0.unrealised_pnl||0)>=0?'#00ff88':'#ff4444';
      } else {
        posDiv.innerHTML='<div style="color:#666;padding:4px">No open positions</div>';
        document.getElementById('posStatus').textContent='Position: FLAT';
      }
    }

    // Today stats
    const t = status.today||{};
    const ps = document.getElementById('perfStats');
    if(ps) ps.innerHTML =
      '<div class="perf-row"><span class="perf-label">TODAY</span><span>'+
      (t.cnt||0)+'t '+(t.wins||0)+'W '+(t.losses||0)+'L $'+(t.net_pnl||0).toFixed(2)+'</span></div>'+
      '<div class="perf-row"><span class="perf-label">ALL</span><span>'+
      (eq.total_trades||0)+'t WR='+(eq.win_rate||0).toFixed(1)+'% $'+(eq.total_net_pnl||0).toFixed(2)+'</span></div>';

    // Refresh chart data
    if(currentPage==='monitor') refreshChart();

  }catch(e){console.error('Refresh error:',e)}
}

// ── Settings page ──
async function loadSettings(){
  try{
    const r = await fetch('/api/settings');
    const d = await r.json();
    const el = document.getElementById('settingsList');
    el.innerHTML = Object.entries(d).map(([k,v])=>
      '<div class="setting-row"><label>'+k+'</label>'+
      '<input id="set-'+k+'" value="'+v+'" />'+
      '<span class="cur">current</span></div>'
    ).join('');
  }catch(e){document.getElementById('settingsList').innerHTML='<p style="color:#ff4444">Failed to load settings</p>'}
}

async function freezeTuning(){
  const r = await fetch('/api/settings/freeze-tuning',{method:'POST'});
  const d = await r.json();
  document.getElementById('settingsMsg').innerHTML='<div class="msg msg-ok">'+d.message+'</div>';
  loadSettings();
}

async function rollbackAll(){
  if(!confirm('Rollback all parameters to .env defaults?')) return;
  const r = await fetch('/api/settings/rollback-all',{method:'POST'});
  const d = await r.json();
  document.getElementById('settingsMsg').innerHTML='<div class="msg msg-ok">'+d.message+'</div>';
  loadSettings();
}

// ── Trade History page ──
async function loadHistory(){
  try{
    const r = await fetch('/api/trades?limit=100');
    const d = await r.json();
    const trades = d.trades||[];
    if(!trades.length){document.getElementById('historyTable').innerHTML='<p style="color:#666">No trades yet</p>';return;}
    let html='<table class="data"><thead><tr><th>Time</th><th>Trade ID</th><th>Decision</th><th>Price</th><th>PnL</th><th>Hold</th><th>MFE</th><th>MAE</th></tr></thead><tbody>';
    trades.forEach(t=>{
      const pnl = t.net_pnl_usd||0;
      const cls = pnl>=0?'td-pos':'td-neg';
      html+='<tr><td>'+utcToLocal(t.timestamp_utc)+'</td><td>'+(t.trade_id||'-')+'</td>'+
        '<td>'+t.decision+'</td><td>'+(t.exit_price||'-')+'</td>'+
        '<td class="'+cls+'">$'+pnl.toFixed(2)+'</td>'+
        '<td>'+(t.hold_duration_sec||'-')+'s</td>'+
        '<td>'+(t.mfe_usd?t.mfe_usd.toFixed(1):'-')+'</td>'+
        '<td>'+(t.mae_usd?t.mae_usd.toFixed(1):'-')+'</td></tr>';
    });
    html+='</tbody></table>';
    document.getElementById('historyTable').innerHTML=html;
  }catch(e){document.getElementById('historyTable').innerHTML='<p style="color:#ff4444">Error loading trades</p>'}
}

// ── Full Journal page ──
async function loadFullJournal(){
  try{
    const r = await fetch('/api/journal?limit=100');
    const d = await r.json();
    const entries = d.entries||[];
    if(!entries.length){document.getElementById('journalTable').innerHTML='<p style="color:#666">No journal entries yet</p>';return;}
    let html='<table class="data"><thead><tr><th>Time</th><th>Type</th><th>Observation</th><th>Conclusion</th><th>Action</th></tr></thead><tbody>';
    entries.forEach(e=>{
      const cls = ICONS[e.entry_type]||'';
      html+='<tr><td>'+utcToLocal(e.timestamp_utc)+'</td>'+
        '<td><span class="'+cls+'">'+(LABELS[e.entry_type]||e.entry_type)+'</span></td>'+
        '<td>'+(e.observation||'-')+'</td>'+
        '<td>'+(e.conclusion||'-')+'</td>'+
        '<td>'+(e.action_taken||'-')+'</td></tr>';
    });
    html+='</tbody></table>';
    document.getElementById('journalTable').innerHTML=html;
  }catch(e){document.getElementById('journalTable').innerHTML='<p style="color:#ff4444">Error loading journal</p>'}
}

// ── Tuning Log page ──
async function loadTuning(){
  try{
    const r = await fetch('/api/tuning?limit=100');
    const d = await r.json();
    const hist = d.history||[];
    if(!hist.length){document.getElementById('tuningTable').innerHTML='<p style="color:#666">No tuning history yet</p>';return;}
    let html='<table class="data"><thead><tr><th>Time</th><th>Parameter</th><th>Old</th><th>New</th><th>Trigger</th><th>Status</th></tr></thead><tbody>';
    hist.forEach(h=>{
      html+='<tr><td>'+utcToLocal(h.timestamp_utc)+'</td>'+
        '<td>'+h.parameter_name+'</td>'+
        '<td>'+h.old_value+'</td><td>'+h.new_value+'</td>'+
        '<td>'+(h.trigger||'-')+'</td>'+
        '<td>'+(h.status||'-')+'</td></tr>';
    });
    html+='</tbody></table>';
    document.getElementById('tuningTable').innerHTML=html;
  }catch(e){document.getElementById('tuningTable').innerHTML='<p style="color:#ff4444">Error loading tuning log</p>'}
}

// ── Strategy page ──
async function loadStrategy(){
  try{
    const r = await fetch('/api/strategy');
    const d = await r.json();
    document.getElementById('stratVersion').textContent = 'v'+d.version;
    document.getElementById('stratPrompt').textContent = d.prompt||'No strategy loaded';
    const hist = d.history||[];
    if(!hist.length){
      document.getElementById('stratHistory').innerHTML='<p style="color:#666">No evolution history yet. Bot will evolve strategy every 6 hours.</p>';
      return;
    }
    let html='<table class="data"><thead><tr><th>Time</th><th>Version</th><th>Win Rate</th><th>PnL</th><th>Trades</th><th>Changes</th><th>Status</th></tr></thead><tbody>';
    hist.forEach(h=>{
      let changes='';
      try{const c=JSON.parse(h.change_reasoning||'{}');changes=(c.changes||[]).join('; ');}catch(e){changes=h.change_reasoning||'';}
      const status=h.rolled_back?'<span style="color:#ff4444">ROLLED BACK</span>':'<span style="color:#00ff88">ACTIVE</span>';
      const wr=(h.win_rate_at_change||0).toFixed(1);
      const pnl=(h.total_pnl_at_change||0).toFixed(2);
      html+='<tr><td>'+utcToLocal(h.timestamp_utc)+'</td><td>v'+h.version+'</td>'+
        '<td>'+wr+'%</td><td>$'+pnl+'</td><td>'+h.trades_since_last+'</td>'+
        '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">'+changes+'</td>'+
        '<td>'+status+'</td></tr>';
    });
    html+='</tbody></table>';
    document.getElementById('stratHistory').innerHTML=html;
  }catch(e){document.getElementById('stratPrompt').textContent='Error loading strategy: '+e}
}

async function forceEvolve(){
  if(!confirm('Force strategy evolution now? This will use an API call to rewrite the strategy.'))return;
  document.getElementById('stratMsg').innerHTML='<div class="msg msg-ok">Evolution in progress...</div>';
  try{
    const r = await fetch('/api/strategy/evolve',{method:'POST'});
    const d = await r.json();
    document.getElementById('stratMsg').innerHTML='<div class="msg msg-ok">'+(d.message||JSON.stringify(d))+'</div>';
    loadStrategy();
  }catch(e){document.getElementById('stratMsg').innerHTML='<div class="msg msg-ok" style="color:#ff4444">Error: '+e+'</div>';}
}

async function rollbackStrategy(){
  if(!confirm('Rollback strategy to previous version?'))return;
  try{
    const r = await fetch('/api/strategy/rollback',{method:'POST'});
    const d = await r.json();
    document.getElementById('stratMsg').innerHTML='<div class="msg msg-ok">'+(d.message||JSON.stringify(d))+'</div>';
    loadStrategy();
  }catch(e){document.getElementById('stratMsg').innerHTML='<div class="msg msg-ok" style="color:#ff4444">Error: '+e+'</div>';}
}

// ── Manual controls ──
async function manualAction(action){
  if(!confirm('Confirm manual '+action+'?'))return;
  const r = await fetch('/api/manual/'+action,{method:'POST'});
  const d = await r.json();
  document.getElementById('posStatus').textContent = d.message||JSON.stringify(d);
  refresh();
}

// ── Init ──
initChart();
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
