#!/bin/bash
# AI Trading Agent - MEXC Futures Scalper

set -e

echo "════════════════════════════════════════════════════════"
echo "  AI TRADING AGENT - MEXC Futures Scalper"
echo "════════════════════════════════════════════════════════"
echo ""

# Load .env if exists
if [ -f .env ]; then
    echo ">>> Loading .env configuration..."
    export $(grep -v '^#' .env | xargs)
fi

# Default to paper trading
export PAPER_TRADING=${PAPER_TRADING:-true}
export TRADING_SYMBOL=${TRADING_SYMBOL:-BTC_USDT}
export TRADING_LEVERAGE=${TRADING_LEVERAGE:-20}
export DASHBOARD_PORT=${DASHBOARD_PORT:-8001}

echo "  Mode:     $([ \"$PAPER_TRADING\" = \"true\" ] && echo 'PAPER TRADING' || echo 'LIVE TRADING')"
echo "  Symbol:   $TRADING_SYMBOL"
echo "  Leverage: ${TRADING_LEVERAGE}x"
echo "  Port:     $DASHBOARD_PORT"
echo ""

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo ">>> Creating virtual environment..."
    python3 -m venv venv
fi

# Activate
source venv/bin/activate

# Install dependencies
echo ">>> Installing dependencies..."
pip install -r requirements.txt -q

echo ""
echo ">>> Starting AI Trading Agent Dashboard..."
echo ">>> Open http://localhost:${DASHBOARD_PORT} in your browser"
echo ""

python -m uvicorn trading_agent.app:app --host 0.0.0.0 --port ${DASHBOARD_PORT} --reload
