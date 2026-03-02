#!/bin/bash
# ScraperBot - Quick Start Script

set -e

echo "========================================"
echo "  ScraperBot - Product Scraper"
echo "========================================"
echo ""

# Check if running with Docker
if command -v docker &> /dev/null && command -v docker compose &> /dev/null; then
    echo "[1] Docker detected. Choose run method:"
    echo "    a) Docker Compose (recommended for production)"
    echo "    b) Local Python (development)"
    echo ""
    read -p "Select [a/b]: " choice

    if [ "$choice" = "a" ]; then
        echo ""
        echo ">>> Building and starting with Docker Compose..."
        docker compose up --build -d
        echo ""
        echo ">>> ScraperBot is running at http://localhost:80"
        echo ">>> Direct API access at http://localhost:8000"
        echo ">>> Logs: docker compose logs -f scraper-bot"
        exit 0
    fi
fi

echo ""
echo ">>> Starting local development setup..."

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo ">>> Creating virtual environment..."
    python3 -m venv venv
fi

# Activate
source venv/bin/activate

# Install dependencies
echo ">>> Installing dependencies..."
pip install -r requirements.txt

# Install Playwright browsers
echo ">>> Installing Playwright browsers..."
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

# Create exports dir
mkdir -p exports

echo ""
echo ">>> Starting ScraperBot..."
echo ">>> Open http://localhost:8000 in your browser"
echo ""

uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
