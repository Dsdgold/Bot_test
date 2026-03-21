# BTCUSDT Perpetual Futures Scalping Bot — Documentation

## Core Philosophy

**WAIT is the default. Trading is the exception.**

This bot trades only when regime, higher-timeframe context, momentum, entry location, volume, and order flow ALL converge. Mixed signals, chop, extended price, or insufficient evidence all produce WAIT. One high-quality trade beats ten mediocre ones.

---

## Architecture Overview

```
Data Feed → Kill Switches → Regime Filter → Pre-Filters (FREE)
    → LLM Directional License (TOKENS) → Entry Quality Gates (FREE)
    → Cooldowns → Fee-Aware R:R → Position Sizing → Execution
    → MFE/MAE Tracking → Exit Logic → Data Collection
    → Analytics → Self-Optimization → Learning Journal
```

### Directional License Model

The LLM (`ai_brain.py`) evaluates higher-timeframe context and issues a **Directional License** — permission to trade a direction with quality/confidence ratings. It does NOT decide exact entry timing.

Deterministic Python code (`strategy.py`) handles tactical execution via strict gates. This separation prevents the LLM from guessing 1-minute timing while leveraging its pattern recognition for strategic context.

---

## File Structure

```
trading_agent/
├── __init__.py
├── config.py              # All env vars with defaults
├── models.py              # Data models (DirectionalLicense, RegimeState, etc.)
├── agent.py               # Main orchestrator
├── ai_brain.py            # LLM integration, selective system prompt
├── strategy.py            # Entry quality gates (9 sub-gates)
├── indicators.py          # Technical indicators + regime classifier
├── risk_manager.py        # Dynamic SL/TP, kill switches, cooldowns
├── position_sizer.py      # Fixed fractional + quality/streak/DD adjustments
├── data_collector.py       # SQLite black box recorder (7 tables)
├── persistence.py         # DB migrations
├── token_manager.py       # Pre-filtering, license cache, token budget
├── learning_journal.py    # Persistent learning memory
├── self_optimizer.py      # 3-tier autonomous tuning
├── manual_controls.py     # Owner manual trading with data isolation
└── log_sanitizer.py       # API key redaction in logs

scripts/
├── dashboard_server.py    # Web dashboard (FastAPI)
├── performance_engine.py  # Analytics engine (14-dimension WR decomposition)
├── export_data.py         # CSV/JSON/MD/HTML export
├── analyze_trade_performance.py  # Standalone trade analysis
└── check_secrets.sh       # Pre-commit secret scanner

tests/
├── test_chunk2.py         # Regime filter, entry gates (9 tests)
├── test_chunk3.py         # Microstructure, kill switches (10 tests)
├── test_chunk4.py         # Data collection (6 tests)
├── test_chunk5.py         # Analytics, position sizing (11 tests)
├── test_chunk6.py         # Token optimization, journal, self-opt (23 tests)
└── test_chunk7.py         # Dashboard, manual controls, export (22 tests)
```

---

## Regime Classification (Deterministic)

| Regime | Detection | Trading |
|---|---|---|
| `TRENDING` | ADX ≥ 18, CHOP < 61.8, clear EMA slope | Allowed |
| `RANGING` | CHOP > 61.8 + (low ADX or flat EMA) | **Blocked** |
| `DEAD_LOW_VOL` | ATR% < 0.10 | **Blocked** |
| `SPIKE_HIGH_VOL` | Candle range > 1.5× ATR | Block unless BREAKOUT_RETEST |

This filter is deterministic and independent of LLM opinion.

---

## Entry Quality Gates

ALL must pass — cannot be overridden by confidence alone:

| Gate | What | Threshold |
|---|---|---|
| A. HTF Alignment | 15m+1h aligned, 1h opposing → block | Required |
| B. Trend Strength | ADX ≥ 18 or EMA slope ≥ 0.0001 | Required |
| C. Extension | Price ≤ 0.8 ATR from EMA21 | MAX_ENTRY_EXTENSION_ATR |
| D. Candle Close | Last closed candle confirms direction | CANDLE_CLOSE_CONFIRMATION |
| E. Volume | Current bar ≥ 1.15× average | MIN_VOLUME_RATIO |
| F. Reversal Bar | Quality ≥ 80, confidence ≥ 80 | REVERSAL_QUALITY_MIN |
| G. CVD Alignment | CVD rising for LONG, falling for SHORT | REQUIRE_CVD_ALIGNMENT |
| H. OI Confirmation | Rising OI confirms breakout | REQUIRE_OI_CONFIRMATION |
| I. Session Filter | Blocked hours → no entry | BLOCKED_HOURS_LOCAL |

---

## Microstructure / Order Flow

- **CVD (Cumulative Volume Delta):** Approximated from candle data. Divergence → WAIT.
- **OI (Open Interest):** Rising OI confirms breakouts. Dropping OI at price extreme → WAIT.

---

## Fee-Aware Execution

- `net_RR = (TP - entry - 2×fee - 2×slip) / (entry - SL + 2×fee + 2×slip)`
- If `net_RR < MIN_NET_RR (1.15)` → trade blocked
- Prefer maker orders (0.02% vs 0.055% taker)

---

## Dynamic SL/TP

- **Stop:** Behind local swing point (padded 0.2× ATR). ATR fallback. Guardrails: 0.35%-0.90%.
- **Target:** Scaled by quality — A+=1.6× R, A=1.35× R, B=1.2× R. Guardrails: 0.45%-1.50%.

---

## Kill Switches & Cooldowns

| Switch | Trigger | Effect |
|---|---|---|
| Spread | > 2.5 USDT | Freeze ALL |
| Funding | > 0.001 in trade direction | Block continuation |
| Latency | > 500ms | Pause ALL |
| Post-loss | After any loss | 600s pause |
| Re-entry | After any trade | 3 candles gap |
| Same-side | 2+ consecutive same-side losses | Freeze side 1800s |
| Session | Configured blocked hours | Block entries |

---

## Data Collection (7 SQLite Tables)

| Table | Columns | Purpose |
|---|---|---|
| `trade_decisions` | 67 | Every entry/skip/exit snapshot |
| `market_snapshots` | 12 | Periodic market state (5min) |
| `equity_curve` | 12 | Equity + drawdown (15min + trade close) |
| `daily_sessions` | 18 | Daily summary |
| `learning_journal` | 13 | Learning memory entries |
| `parameter_history` | 16 | Full tuning audit trail |
| `token_usage` | 9 | Token budget tracking |

MFE/MAE tracked tick-by-tick during trade lifetime.

---

## Position Sizing

**Formula:** `position = (equity × risk% × quality × streak × dd) / risk_per_unit`

| Adjustment | Values |
|---|---|
| Quality | A+=1.5×, A=1.2×, B=1.0×, <70=blocked |
| Streak | 1 loss=0.75×, 2=0.50×, 3+=0.25× |
| Drawdown | 3%=0.70×, 5%=0.40×, 8%=0.20×, 10%+=HALT |
| Growth tiers | $1k=1.0%, $2.5k=1.25%, $5k=1.5%, $10k=1.75% |

---

## Capital Protection (Non-Negotiable)

| Breaker | Trigger | Effect |
|---|---|---|
| Daily loss | -3.0% | HALT until next UTC day |
| Weekly loss | -7.0% | HALT until Monday |
| Equity floor | Below $500 | HALT permanently |
| Max drawdown | ≥10% from peak | HALT all trading |
| Max leverage | Never exceed 10× | Caps position |
| Max positions | 1 concurrent | Blocks new entries |

---

## Token Optimization

Pre-filter gates (FREE) run before LLM call:
1. Kill switches → 2. Session → 3. Cooldowns → 4. Capital limits → 5. Regime → 6. Extension → 7. Volume

Only if ALL pass → LLM call (TOKENS). Eliminates 80-90% of calls in choppy markets.

License cache: 180s TTL. Invalidates on regime change, 1 ATR move, 3× volume spike.

Compressed prompt: ~300 chars with pre-computed indicators. max_tokens=200.

Budget: 50k daily tokens, alert at 80%, hard stop → deterministic-only.

---

## Learning Journal

| Entry Type | When | Content |
|---|---|---|
| POST_WIN | After win | MFE vs TP analysis, hold time |
| POST_LOSS | After loss | Stop hunt detection, overconfidence check |
| POST_SKIP_REVIEW | Every 4h | Skip rate, filter value analysis |
| REGIME_SHIFT | On change | Old→new, duration, trade count |
| EDGE_DECAY | WR drop | Rolling WR, potential causes |
| TUNING_CYCLE | Per cycle | Changes applied, evidence |
| ROLLBACK | On revert | Parameter reverted, reason |

---

## Self-Optimization (3 Tiers)

| Tier | Params | Behavior |
|---|---|---|
| 1: Full Autonomy | MIN_CONFIDENCE, TRADE_QUALITY_MIN, ADX_MIN, CHOP_MAX, volume/extension/cooldown | Auto-adjust within min/max/delta bounds |
| 2: Supervised | TARGET_RR, ATR_STOP_MULT, MIN_NET_RR, BASE_RISK_PCT, REVERSAL_QUALITY_MIN | 30-trade probation + auto-rollback |
| 3: Manual Only | MAX_LEVERAGE, loss limits, equity floor, DD halt, growth tiers | NEVER auto-changed |

Safety: max 2 concurrent probations, freeze at 5% DD, emergency override, lockout after failed rollbacks.

---

## Dashboard

Web dashboard on `127.0.0.1:8080`. Single-page live monitor (no scroll on 1920×1080).

- **Live Monitor:** Equity, drawdown, chart area, learning timeline, manual controls, performance stats, alerts
- **Manual Controls:** [OPEN LONG] [OPEN SHORT] [CLOSE NOW] — tagged MANUAL_OWNER, excluded from bot learning
- **Settings:** Parameter view, [Freeze All], [Rollback All]
- **Trade History / Journal / Tuning Log:** Scrollable detail pages via API
- **Export:** `python scripts/export_data.py --help`

---

## Data Export

```bash
python scripts/export_data.py --all --format csv --output ./exports/
python scripts/export_data.py --trades --format csv          # merged entry+exit
python scripts/export_data.py --journal-report --format md   # readable markdown
python scripts/export_data.py --analytics-report --format html
python scripts/export_data.py --all --last-days 7
```

Auto-export: daily CSV to `./exports/`, retain 90 days.

---

## Security

- API keys in `.env` only (never in source)
- `.gitignore`: `.env`, `*.db`, `exports/`
- Log sanitization: `SanitizingFormatter` redacts hex/base64 patterns
- `scripts/check_secrets.sh`: pre-commit scanner
- Dashboard binds to `127.0.0.1` only

---

## Environment Variables

### API Keys
| Variable | Default | Description |
|---|---|---|
| `BYBIT_API_KEY` | (empty) | Bybit API key |
| `BYBIT_API_SECRET` | (empty) | Bybit API secret |
| `BYBIT_TESTNET` | `true` | Use testnet |
| `ANTHROPIC_API_KEY` | (empty) | Claude API key |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | LLM model |

### Core Selectivity
| Variable | Default | Description |
|---|---|---|
| `MIN_CONFIDENCE` | `60` | Minimum AI confidence to trade |
| `TRADE_QUALITY_MIN` | `70` | Minimum entry quality |
| `REVERSAL_QUALITY_MIN` | `80` | Minimum quality for reversals |
| `ENABLE_FALLBACK_OVERRIDE` | `false` | Allow fallback override |

### Regime Filter
| Variable | Default | Description |
|---|---|---|
| `ADX_MIN` | `18` | Minimum ADX for trending |
| `CHOP_MAX` | `61.8` | Maximum choppiness index |
| `DEAD_VOL_ATR_PCT_MIN` | `0.10` | ATR% threshold for dead zone |

### Entry Gates
| Variable | Default | Description |
|---|---|---|
| `MAX_ENTRY_EXTENSION_ATR` | `0.8` | Max extension from EMA |
| `MIN_VOLUME_RATIO` | `1.15` | Min volume vs average |
| `CANDLE_CLOSE_CONFIRMATION` | `true` | Require candle close |

### Execution
| Variable | Default | Description |
|---|---|---|
| `TAKER_FEE_RATE` | `0.00055` | Taker fee rate |
| `MAKER_FEE_RATE` | `0.0002` | Maker fee rate |
| `MIN_NET_RR` | `1.15` | Minimum fee-adjusted R:R |
| `PREFER_POST_ONLY_ENTRIES` | `true` | Prefer maker orders |

### Dynamic SL/TP
| Variable | Default | Description |
|---|---|---|
| `ATR_STOP_MULT` | `1.0` | ATR multiplier for stop |
| `TARGET_RR_A_PLUS` | `1.6` | R:R for A+ setups |
| `TARGET_RR_A` | `1.35` | R:R for A setups |
| `MIN_SL_PCT` / `MAX_SL_PCT` | `0.35` / `0.90` | SL guardrails |
| `MIN_TP_PCT` / `MAX_TP_PCT` | `0.45` / `1.50` | TP guardrails |

### Kill Switches
| Variable | Default | Description |
|---|---|---|
| `MAX_SPREAD_TOLERANCE_USDT` | `2.5` | Spread freeze threshold |
| `MAX_FUNDING_RATE_ABS` | `0.001` | Extreme funding threshold |
| `LATENCY_KILL_SWITCH_MS` | `500` | Latency freeze threshold |

### Cooldowns
| Variable | Default | Description |
|---|---|---|
| `POST_LOSS_COOLDOWN_SEC` | `600` | Pause after loss |
| `REENTRY_COOLDOWN_CANDLES` | `3` | Candle gap between trades |
| `SAME_SIDE_LOSS_PAUSE_COUNT` | `2` | Consecutive losses to freeze side |

### Position Sizing
| Variable | Default | Description |
|---|---|---|
| `BASE_RISK_PER_TRADE_PCT` | `1.0` | Base risk per trade |
| `MAX_LEVERAGE` | `10` | Maximum leverage |
| `DAILY_MAX_LOSS_PCT` | `3.0` | Daily loss halt |
| `WEEKLY_MAX_LOSS_PCT` | `7.0` | Weekly loss halt |
| `EQUITY_FLOOR_USDT` | `500` | Equity floor halt |
| `DD_HALT_PCT` | `10.0` | Drawdown halt |

### Token Optimization
| Variable | Default | Description |
|---|---|---|
| `LICENSE_CACHE_TTL_SEC` | `180` | License cache TTL |
| `DAILY_TOKEN_BUDGET` | `50000` | Daily token budget |
| `TOKEN_BUDGET_HARD_STOP` | `true` | Stop LLM on budget exceeded |

### Self-Optimization
| Variable | Default | Description |
|---|---|---|
| `AUTONOMOUS_TUNING_ENABLED` | `true` | Enable auto-tuning |
| `TUNING_CYCLE_HOURS` | `24` | Hours between tuning cycles |
| `PROBATION_TRADES` | `30` | Probation period for Tier 2 |
| `TUNING_FREEZE_DD_PCT` | `5.0` | Freeze tuning above this DD |

### Dashboard
| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind address |
| `DASHBOARD_PORT` | `8080` | Dashboard port |
| `DASHBOARD_REFRESH_SEC` | `10` | Auto-refresh interval |

---

## Getting Started

1. Copy `.env.example` to `.env` and fill in API keys
2. Install dependencies: `pip install -r requirements.txt`
3. Run bot: (main loop not yet implemented — see Chunk 7 notes)
4. Run dashboard: `python scripts/dashboard_server.py`
5. Run analytics: `python scripts/performance_engine.py`
6. Export data: `python scripts/export_data.py --help`
