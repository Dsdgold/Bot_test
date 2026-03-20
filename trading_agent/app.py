"""
FastAPI Dashboard API for the Trading Agent.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, AgentConfig
from .agent import TradingAgent

logger = logging.getLogger("dashboard")

# ── Global state ─────────────────────────────────────────────
agent: TradingAgent | None = None
agent_task: asyncio.Task | None = None
config: AgentConfig = load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown."""
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Trading Dashboard starting...")
    yield
    # Shutdown
    if agent:
        await agent.stop()
    logger.info("Trading Dashboard stopped.")


app = FastAPI(title="AI Trading Agent - MEXC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files ─────────────────────────────────────────────
frontend_dir = Path(__file__).parent.parent / "frontend" / "trading"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ── Routes ───────────────────────────────────────────────────

@app.get("/")
async def index():
    """Serve the dashboard."""
    index_file = frontend_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse({"message": "Trading Agent API", "docs": "/docs"})


@app.get("/api/state")
async def get_state():
    """Get current agent state."""
    if agent:
        return agent.get_state()
    return {
        "running": False,
        "symbol": config.trading.symbol,
        "leverage": config.trading.leverage,
        "mode": "PAPER" if config.paper_trading else "LIVE",
        "ai_enabled": bool(config.ai.api_key),
        "ai_reasoning": "",
        "ai_risk_level": "",
        "ai_analysis_count": 0,
        "account": None,
        "ticker": None,
        "position": None,
        "last_signal": None,
        "indicators": None,
        "trades": [],
        "candles": [],
    }


@app.post("/api/start")
async def start_agent():
    """Start the trading agent."""
    global agent, agent_task

    if agent and agent.running:
        return {"status": "already_running"}

    agent = TradingAgent(config)
    agent_task = asyncio.create_task(agent.start())
    return {"status": "started", "symbol": config.trading.symbol}


@app.post("/api/stop")
async def stop_agent():
    """Stop the trading agent."""
    global agent, agent_task

    if not agent or not agent.running:
        return {"status": "not_running"}

    await agent.stop()
    if agent_task:
        agent_task.cancel()
        agent_task = None

    return {"status": "stopped"}


@app.post("/api/config")
async def update_config(data: dict):
    """Update trading configuration."""
    global config

    trading = config.trading
    if "symbol" in data:
        trading.symbol = data["symbol"]
    if "leverage" in data:
        trading.leverage = min(int(data["leverage"]), trading.max_leverage)
    if "stop_loss_pct" in data:
        trading.stop_loss_pct = float(data["stop_loss_pct"])
    if "take_profit_pct" in data:
        trading.take_profit_pct = float(data["take_profit_pct"])
    if "min_confidence" in data:
        trading.min_confidence = float(data["min_confidence"])
    if "paper_trading" in data:
        config.paper_trading = bool(data["paper_trading"])

    # API keys (set at runtime through dashboard)
    if "anthropic_api_key" in data and data["anthropic_api_key"]:
        config.ai.api_key = data["anthropic_api_key"]
    if "mexc_api_key" in data and data["mexc_api_key"]:
        config.mexc.api_key = data["mexc_api_key"]
    if "mexc_api_secret" in data and data["mexc_api_secret"]:
        config.mexc.api_secret = data["mexc_api_secret"]

    return {"status": "updated", "config": {
        "symbol": trading.symbol,
        "leverage": trading.leverage,
        "stop_loss_pct": trading.stop_loss_pct,
        "take_profit_pct": trading.take_profit_pct,
        "min_confidence": trading.min_confidence,
        "paper_trading": config.paper_trading,
        "ai_enabled": bool(config.ai.api_key),
        "mexc_configured": bool(config.mexc.api_key),
    }}


@app.get("/api/trades")
async def get_trades():
    """Get trade history."""
    if agent:
        return {
            "trades": [
                {
                    "side": t.side.value,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": round(t.pnl, 2),
                    "pnl_pct": round(t.pnl_pct, 2),
                    "reason": t.reason,
                    "leverage": t.leverage,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                }
                for t in agent.trades
            ]
        }
    return {"trades": []}


@app.get("/api/health")
async def health():
    return {"status": "ok", "agent_running": agent.running if agent else False}


def run():
    """Run the dashboard server."""
    import uvicorn
    port = int(os.getenv("DASHBOARD_PORT", "8001"))
    uvicorn.run(
        "trading_agent.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
