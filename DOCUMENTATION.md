# AI Trading Bot - Bybit Futures Scalper

## Pełna dokumentacja

---

## 1. Przegląd systemu

Bot do automatycznego handlu kontraktami futures BTCUSDT na Bybit. Używa Claude AI (Sonnet) jako głównego decydenta + strategii technicznej jako fallback. Cel: rozmnożyć $60 do $500+.

### Architektura

```
┌─────────────────────────────────────────────────┐
│                  FastAPI Dashboard               │
│              (app.py - port 8001)                │
├─────────────────────────────────────────────────┤
│                  TradingAgent                    │
│                  (agent.py)                      │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ AI Brain │  │ Strategy │  │ Risk Manager │  │
│  │ (Sonnet) │  │ (Tech)   │  │              │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │              │               │          │
│       └──────┬───────┘               │          │
│              ▼                       │          │
│     ┌────────────────┐               │          │
│     │ Signal Engine  │◄──────────────┘          │
│     └───────┬────────┘                          │
│             ▼                                    │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │  Bybit Client    │  │  SQLite Persistence │  │
│  │  (REST API)      │  │  (bot_data.db)      │  │
│  └──────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### Flow każdego ticka (co 60s)

1. Pobierz ticker + świeczki 1m z Bybit
2. Zaktualizuj balance
3. Co 3 ticki: pobierz funding rate, open interest, order book, multi-TF dane, Fear & Greed
4. Oblicz wskaźniki techniczne (RSI, EMA, MACD, BB, VWAP, ATR)
5. Jeśli jest otwarta pozycja → monitoruj (SL/TP/trailing/AI close check)
6. Jeśli brak pozycji → wyślij dane do Claude AI → otrzymaj decyzję
7. Jeśli AI mówi WAIT ale strategia + TF się zgadzają → fallback override
8. Sprawdź risk rules (cooldown, loss streak, daily limit)
9. Jeśli sygnał przejdzie → otwórz pozycję + ustaw SL/TP na Bybit
10. Zapisz do SQLite

---

## 2. Pliki i moduły

### `trading_agent/agent.py` - Główny orkiestrator

**Klasa:** `TradingAgent`

**Kluczowe metody:**
- `start()` — uruchamia bota, ładuje dane z DB, weryfikuje pozycję na Bybit
- `stop()` — zamyka pozycję, zapisuje stan, czyści zasoby
- `_tick()` — jeden cykl analizy (wywoływany co 60s)
- `_generate_ai_signal()` — AI + fallback logic
- `_open_position()` — otwiera trade z fee-aware RR check
- `_monitor_position()` — trailing stop + AI close + Bybit SL sync
- `_close_position()` — zamyka trade, zapisuje do DB
- `_update_market_context()` — funding, OI, order book, multi-TF, F&G
- `_auto_tune()` — performance scoring na bazie historii

**Fallback override (linie ~387-421):**
Gdy AI mówi WAIT ale strategia techniczna ma sygnał I 2+ timeframe'y zgadzają się na kierunek → override AI i wejdź z 35x leverage, 85% pozycji.

---

### `trading_agent/ai_brain.py` - Claude AI Brain

**Klasa:** `ClaudeAIBrain`

**Model:** `claude-sonnet-4-20250514`
**Max tokens:** 256
**Koszt:** ~$0.001 per call, ~$1.5/dzień przy 60 callach/h

**System prompt (ultra-kompresowany):**
```
Aggressive $60 crypto scalper. TRADE or DIE.
If 2+TFs agree: TRADE. Mixed signals: follow EMA trend.
WAIT only if completely flat.
Lev:25-50x SL:0.8-1.2% TP:2-4% Size:70-90%.
```

**Output format (JSON):**
```json
{
  "a": "L|S|W|C",     // Action: LONG/SHORT/WAIT/CLOSE
  "g": "A+|B|C|D",    // Grade
  "c": 0-100,         // Confidence %
  "lev": 25-50,       // Leverage
  "m": 70-90,         // Margin % of equity
  "sl": 0.8-1.2,      // Stop Loss %
  "tp": 2.0-4.0,      // Take Profit %
  "ts": 0.5-1.5,      // Trailing Stop %
  "rr": 0,            // Risk/Reward ratio
  "rc": ["HTF-"],     // Reason codes
  "iv": [""]          // Invalidation codes
}
```

**Kody powodów (rc):**
| Kod | Znaczenie |
|-----|-----------|
| HTF+ / HTF- | Higher TimeFrame bullish/bearish |
| MOM+ / MOM- | Momentum positive/negative |
| VOL+ / VOL- | Volume expanding/declining |
| CHOP | Choppy/sideways market |
| TREND | Clear trend detected |
| BOS+ / BOS- | Break of Structure bullish/bearish |
| RR+ / RR- | Good/poor Risk:Reward |
| REV | Reversal signal |
| SQZ | Bollinger squeeze |
| FG+ / FG- | Fear & Greed bullish/bearish |

**Dane wysyłane do AI:**
- Cena, zakres, ostatnie 5 ruchów %
- RSI, EMA cross, MACD histogram, ATR%, volume ratio
- Ostatnie 5 świeczek (close + volume)
- OI change, book imbalance
- Trendy 5m/15m/1h
- Fear & Greed Index
- Balance

---

### `trading_agent/strategy.py` - Strategia techniczna

**Klasa:** `ScalpingStrategy`

Scoring system (max 100 punktów):

| Wskaźnik | Waga | Sygnał LONG | Sygnał SHORT |
|----------|------|-------------|--------------|
| RSI | 20 | < 30 (oversold) | > 70 (overbought) |
| RSI divergence | 8 | Bullish divergence | Bearish divergence |
| EMA cross | 25 | Fast > Slow + crossover | Fast < Slow + crossover |
| EMA trend | 10 | Price > EMA50 | Price < EMA50 |
| MACD | 20 | Above signal + positive histogram | Below signal + negative histogram |
| MACD crossover | 12 | Bullish crossover | Bearish crossover |
| Bollinger Bands | 20 | Price at lower BB | Price at upper BB |
| VWAP | 5 | Price > VWAP | Price < VWAP |
| Volume | 5 | Spike confirms direction | Spike confirms direction |

**Generowanie sygnału:**
- Score > 50% → STRONG_BUY / STRONG_SELL
- Score 20-50% → BUY / SELL
- Score < 20% → NEUTRAL (brak trade'u)

---

### `trading_agent/risk_manager.py` - Zarządzanie ryzykiem

**Klasa:** `RiskManager`

**Zabezpieczenia:**

| Mechanizm | Wartość | Opis |
|-----------|---------|------|
| Cooldown | 120s | Minimum czas między trade'ami |
| Loss streak | 3 straty → 30 min pauza | Reset po pauzie |
| Daily loss limit | 25% | Stop trading na dziś |
| Min hold time | 120s | Nie zamykaj przed 2 min |
| Progressive stop lock | step-based | Co +$4 net open PnL lockuje +$1 netto (konfigurowalne przez .env) |

**Progressive profit locking (domyślnie włączony):**
- Oblicza gross open PnL, fee buffer (entry fee + exit fee + slippage), net open PnL
- Kroki: `steps = floor(net_pnl / PROFIT_STEP_NET_USD)`
- Lockowany zysk: `locked_net = steps * LOCK_STEP_NET_USD`
- Nowy SL zabezpiecza `fee_buffer + locked_net` w gross PnL
- Przykład (domyślne 4.0/1.0): +$4 netto → lock $1, +$8 → lock $2, +$12 → lock $3
- Agresywny wariant (2.0/0.5): +$2 netto → lock $0.50, +$4 → lock $1, +$6 → lock $1.50
- SL nigdy się nie cofa, minimalna poprawa `MIN_STOP_IMPROVEMENT_USD`
- Legacy system (breakeven/trailing/force-close) dostępny pod flagą konfiguracyjną

**Fee-aware RR:**
- Bybit taker fee: 0.055% per stronę
- Total round-trip: 0.11%
- Minimalny RR po opłatach: 1.3
- Formuła: `net_tp = tp% - 0.11%`, `net_sl = sl% + 0.11%`, `RR = net_tp / net_sl`

---

### `trading_agent/persistence.py` - Baza danych SQLite

**Klasa:** `BotDatabase`
**Plik DB:** `bot_data.db` (w katalogu głównym projektu)

**Tabele:**

```sql
trades          -- Historia wszystkich zamkniętych trade'ów
open_position   -- Aktualna otwarta pozycja (max 1)
bot_state       -- Key-value store na przyszłość
```

**Kluczowe operacje:**
- `save_trade()` / `load_trades()` — historia przeżywa restart
- `save_position()` / `load_position()` — pozycja odtwarzana po crashu
- `clear_position()` — usuwana po zamknięciu trade'u

**Crash recovery na starcie:**
1. Załaduj trades z DB
2. Załaduj saved position
3. Sprawdź na Bybit czy pozycja nadal istnieje
4. Jeśli tak → przywróć i monitoruj
5. Jeśli nie → wyczyść z DB

---

### `trading_agent/bybit_client.py` - Klient Bybit API

**Klasa:** `BybitClient`
**API:** Bybit V5 Unified

**Endpointy:**

| Metoda | Endpoint | Opis |
|--------|----------|------|
| `get_ticker()` | `/v5/market/tickers` | Aktualny kurs |
| `get_klines()` | `/v5/market/kline` | Świeczki (1m, 5m, 15m, 1h) |
| `get_depth()` | `/v5/market/orderbook` | Order book |
| `get_funding_rate()` | `/v5/market/funding/history` | Funding rate |
| `get_open_interest()` | `/v5/market/open-interest` | Open interest |
| `get_balance()` | `/v5/account/wallet-balance` | Saldo USDT |
| `set_leverage()` | `/v5/position/set-leverage` | Ustaw dźwignię |
| `open_position()` | `/v5/order/create` | Otwórz pozycję (market) |
| `close_position()` | `/v5/order/create` (reduceOnly) | Zamknij pozycję |
| `get_open_positions()` | `/v5/position/list` | Sprawdź otwarte pozycje |
| `set_stop_loss_take_profit()` | `/v5/position/trading-stop` | Ustaw SL/TP na giełdzie |

**Autentykacja:** HMAC SHA256 z API key + secret + timestamp

---

### `trading_agent/indicators.py` - Wskaźniki techniczne

Czyste obliczenia matematyczne bez side effects.

| Funkcja | Opis | Parametry |
|---------|------|-----------|
| `ema()` | Exponential Moving Average | period: 9/21/50 |
| `sma()` | Simple Moving Average | period: 20 |
| `rsi()` | Relative Strength Index | period: 14 |
| `macd()` | MACD + Signal + Histogram | fast:12 slow:26 signal:9 |
| `bollinger_bands()` | Upper/Middle/Lower/Width | period:20 std:2.0 |
| `vwap()` | Volume Weighted Avg Price | all candles |
| `atr()` | Average True Range | period: 14 |
| `calculate_all()` | Wszystkie naraz | candles + config |

---

### `trading_agent/models.py` - Modele danych

| Model | Opis |
|-------|------|
| `Side` | LONG / SHORT |
| `SignalStrength` | STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL |
| `Candle` | OHLCV + timestamp |
| `Ticker` | Cena, bid/ask, volume 24h, zmiana 24h |
| `Indicators` | Wszystkie wskaźniki techniczne |
| `Signal` | Sygnał: side, strength, confidence, reasons |
| `Position` | Otwarta pozycja: entry, qty, lev, SL, TP |
| `Trade` | Zamknięty trade: PnL, reason, czasy |
| `MarketContext` | Funding, OI, order book, trendy MTF, F&G |
| `AccountState` | Balance, PnL, win rate |

---

### `trading_agent/config.py` - Konfiguracja

Ładuje z `.env` + wartości domyślne.

| Parametr | Domyślna | Opis |
|----------|----------|------|
| `AI_MODEL` | claude-sonnet-4-20250514 | Model AI |
| `TRADING_SYMBOL` | BTCUSDT | Para handlowa |
| `TRADING_LEVERAGE` | 35x | Domyślna dźwignia |
| `MAX_LEVERAGE` | 50x | Maksymalna dźwignia |
| `STOP_LOSS_PCT` | 1.0% | Stop loss |
| `TAKE_PROFIT_PCT` | 3.0% | Take profit |
| `MIN_CONFIDENCE` | 40% | Min confidence do trade'u |
| `MIN_HOLD_TIME` | 120s | Min czas trzymania pozycji |
| `ANALYSIS_INTERVAL` | 60s | Interwał analizy |
| `COOLDOWN_AFTER_TRADE` | 120s | Cooldown między trade'ami |
| `MAX_DAILY_LOSS_PCT` | 25% | Max dzienna strata |
| `PROGRESSIVE_STOP_ENABLED` | true | Włącz progresywny locking zysku |
| `PROFIT_STEP_NET_USD` | 4.0 | Co ile $ netto PnL przesuwać SL |
| `LOCK_STEP_NET_USD` | 1.0 | Ile $ netto lockować na każdy krok |
| `TAKER_FEE_RATE` | 0.00055 | Bybit taker fee rate (0.055%) |
| `SLIPPAGE_BUFFER_USD` | 0.40 | Bufor na slippage w USD |
| `MIN_STOP_IMPROVEMENT_USD` | 0.25 | Min poprawa SL żeby aktualizować |
| `DISABLE_LEGACY_PROFIT_PROTECTION` | true | Wyłącz stary breakeven/trailing/force-close |
| `PAPER_TRADING` | false | True = tryb demo |
| `DASHBOARD_PORT` | 8001 | Port dashboardu |

---

### `trading_agent/app.py` - Dashboard API (FastAPI)

**Port:** 8001

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/` | GET | Dashboard HTML |
| `/api/state` | GET | Pełny stan bota (pozycja, balance, sygnały, świeczki) |
| `/api/start` | POST | Uruchom agenta |
| `/api/stop` | POST | Zatrzymaj agenta |
| `/api/config` | POST | Zmień konfigurację w locie |
| `/api/trades` | GET | Historia trade'ów |
| `/api/health` | GET | Health check |

---

## 3. Konfiguracja `.env`

```env
# AI
ANTHROPIC_API_KEY=sk-ant-api03-...
AI_MODEL=claude-sonnet-4-20250514
AI_ANALYSIS_INTERVAL=4
AI_CLOSE_DECISIONS=true

# Bybit
BYBIT_API_KEY=rmckXAMRH9gnPus78c
BYBIT_API_SECRET=

# Trading
TRADING_SYMBOL=BTCUSDT
TRADING_LEVERAGE=35
STOP_LOSS_PCT=1.0
TAKE_PROFIT_PCT=3.0
MIN_CONFIDENCE=40
MIN_HOLD_TIME=120

# Progressive profit locking
PROGRESSIVE_STOP_ENABLED=true
PROFIT_STEP_NET_USD=4.0
LOCK_STEP_NET_USD=1.0
TAKER_FEE_RATE=0.00055
SLIPPAGE_BUFFER_USD=0.40
MIN_STOP_IMPROVEMENT_USD=0.25
DISABLE_LEGACY_PROFIT_PROTECTION=true

# System
PAPER_TRADING=false
DASHBOARD_PORT=8001
LOG_LEVEL=INFO
```

---

## 4. Uruchomienie

### Windows PowerShell
```powershell
cd C:\Users\[USER]\Bot_test
.\venv\Scripts\Activate          # opcjonalnie
python -m uvicorn trading_agent.app:app --host 0.0.0.0 --port 8001 --reload
```

### Linux/WSL
```bash
cd ~/Bot_test
source venv/bin/activate
bash run_trading.sh
# lub bezpośrednio:
python -m uvicorn trading_agent.app:app --host 0.0.0.0 --port 8001 --reload
```

### Docker
```bash
docker compose up --build -d
```

### Restart po zmianach
```
Ctrl+C
git pull origin claude/ai-trading-agent-mexc-rZiDb
python -m uvicorn trading_agent.app:app --host 0.0.0.0 --port 8001 --reload
```

---

## 5. Decyzyjny flowchart

```
START TICK
    │
    ├── Fetch ticker + candles
    ├── Update balance
    ├── [co 3 ticki] Update market context
    ├── Calculate indicators
    │
    ├── Position open?
    │   ├── YES → Monitor position
    │   │         ├── SL hit? → CLOSE
    │   │         ├── TP hit? → CLOSE
    │   │         ├── Progressive stop: gross PnL → fee buffer → net PnL
    │   │         │   → steps = floor(net/step) → lock net → new SL
    │   │         │   → sync to Bybit if SL improved
    │   │         ├── AI says CLOSE/REVERSE? → CLOSE
    │   │         └── Otherwise → HOLD
    │   │
    │   └── NO → Generate signal
    │             ├── Ask Claude AI
    │             │   ├── AI says LONG/SHORT → use AI signal
    │             │   ├── AI says WAIT + strategy signal + 2+ TFs align
    │             │   │   └── FALLBACK OVERRIDE → trade
    │             │   └── AI says WAIT + no alignment → skip
    │             │
    │             ├── Check confidence >= 40%?
    │             ├── Check cooldown (120s)?
    │             ├── Check loss streak (3 losses = 30 min pause)?
    │             ├── Check daily loss limit (25%)?
    │             ├── Check fee-adjusted RR >= 1.3?
    │             │
    │             └── ALL PASS → OPEN POSITION
    │                           ├── Set leverage on Bybit
    │                           ├── Place market order
    │                           ├── Set SL/TP on Bybit
    │                           └── Save to SQLite
    │
    └── Sleep 60s → NEXT TICK
```

---

## 6. Koszty operacyjne

| Pozycja | Koszt |
|---------|-------|
| Claude Sonnet API | ~$0.001/call × 60/h = **$1.44/dzień** |
| Bybit taker fee | 0.055% × 2 strony = **0.11% per trade** |
| Na 35x leverage | Fee = ~3.85% marginu per trade |
| Przy $60 koncie i 10 trade'ów/dzień | ~$0.66 w opłatach |
| **Total dzienny koszt** | **~$2.10** |

---

## 7. Limity ryzyka

| Mechanizm | Wartość | Co robi |
|-----------|---------|---------|
| Max leverage | 50x | Hard cap w kodzie |
| Max pozycja | 90% balance | Nie ryzykuj 100% |
| Stop loss | 0.8-1.5% | Max strata per trade |
| Daily loss | 25% | Stop na dziś |
| Cooldown | 120s | Anty-overtrading |
| 3 straty z rzędu | 30 min pauza | Anty-tilt |
| Min hold time | 120s | Nie zamykaj za wcześnie |
| Fee-adjusted RR | >= 1.3 | Nie wchodź w złe trades |
| Progressive stop lock | +$4 net PnL/krok | Lockuje +$1 netto/krok (konfigurowalne) |

---

## 8. Struktura plików

```
Bot_test/
├── .env                          # Konfiguracja (API keys, parametry)
├── bot_data.db                   # SQLite (trades, pozycje) — auto-tworzony
├── run_trading.sh                # Skrypt startowy (Linux)
├── requirements.txt              # Zależności Python
├── DOCUMENTATION.md              # Ten plik
│
├── trading_agent/
│   ├── __init__.py
│   ├── app.py                    # FastAPI dashboard + API
│   ├── agent.py                  # Główny orchestrator
│   ├── ai_brain.py               # Claude AI integration
│   ├── strategy.py               # Strategia techniczna (scoring)
│   ├── risk_manager.py           # Risk management + trailing stop
│   ├── bybit_client.py           # Bybit REST API client
│   ├── indicators.py             # Obliczenia wskaźników (RSI, EMA, etc.)
│   ├── models.py                 # Dataclassy (Position, Trade, Signal, etc.)
│   ├── config.py                 # Konfiguracja z .env
│   ├── persistence.py            # SQLite persistence
│   └── mexc_client.py            # MEXC client (nieaktywny)
│
└── frontend/trading/             # Dashboard HTML/CSS/JS
    ├── index.html
    ├── css/style.css
    └── js/app.js
```
