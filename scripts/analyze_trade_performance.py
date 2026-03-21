#!/usr/bin/env python3
"""
Trade Performance Analysis Script (Phase 1)
Standalone diagnostic that reads bot_data.db and computes baseline metrics.
Re-run anytime to assess current performance.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

DB_PATH = Path(__file__).parent.parent / "bot_data.db"
LOCAL_TZ = "Europe/Warsaw"


def get_connection(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_trades(conn: sqlite3.Connection) -> list[dict]:
    """Load all closed trades from both legacy 'trades' table and new 'trade_decisions' table."""
    trades = []

    # Legacy trades table
    try:
        rows = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        for r in rows:
            trades.append({
                "id": r["id"],
                "side": r["side"],
                "entry_price": r["entry_price"],
                "exit_price": r["exit_price"],
                "quantity": r["quantity"],
                "leverage": r["leverage"],
                "pnl": r["pnl"],
                "pnl_pct": r["pnl_pct"],
                "entry_time": datetime.fromisoformat(r["entry_time"]),
                "exit_time": datetime.fromisoformat(r["exit_time"]),
                "reason": r["reason"] or "",
                "source": "legacy",
                "trade_source": "UNKNOWN",
                "confidence": 0,
            })
    except sqlite3.OperationalError:
        pass

    # New trade_decisions table (entries with exit data filled)
    try:
        rows = conn.execute(
            "SELECT * FROM trade_decisions WHERE decision LIKE 'ENTRY_%' AND exit_price IS NOT NULL ORDER BY id ASC"
        ).fetchall()
        for r in rows:
            entry_time = datetime.fromisoformat(r["timestamp_utc"]) if r["timestamp_utc"] else datetime.now()
            trades.append({
                "id": r["id"],
                "side": "LONG" if r["decision"] == "ENTRY_LONG" else "SHORT",
                "entry_price": r["entry_price"] or r["price"],
                "exit_price": r["exit_price"],
                "quantity": r.get("position_size_usd", 0) / (r["price"] or 1) if r.get("position_size_usd") else 0,
                "leverage": r.get("leverage", 1),
                "pnl": r.get("net_pnl_usd", 0),
                "pnl_pct": r.get("net_pnl_pct", 0),
                "entry_time": entry_time,
                "exit_time": datetime.fromisoformat(r.get("exit_time", entry_time.isoformat())) if r.get("exit_time") else entry_time,
                "reason": r.get("exit_type", ""),
                "source": "new",
                "trade_source": r.get("trade_source", "AI"),
                "confidence": r.get("confidence", 0),
            })
    except (sqlite3.OperationalError, KeyError):
        pass

    return trades


def compute_win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return wins / len(trades) * 100


def analyze(db_path: str = str(DB_PATH)):
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        print("No trade history available. Run the bot first to collect data.")
        print("\nSchema report: Database does not exist yet.")
        return

    conn = get_connection(db_path)

    # Report available tables
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = [t["name"] for t in tables]
    print("=" * 60)
    print("  TRADE PERFORMANCE ANALYSIS")
    print("=" * 60)
    print(f"\nDatabase: {db_path}")
    print(f"Tables found: {', '.join(table_names)}")

    # Check each table's row count
    for tname in table_names:
        count = conn.execute(f"SELECT COUNT(*) as cnt FROM {tname}").fetchone()["cnt"]
        print(f"  {tname}: {count} rows")

    trades = load_trades(conn)
    if not trades:
        print("\nNo closed trades found. Insufficient data for analysis.")
        print("Run the bot to collect trade history, then re-run this script.")
        conn.close()
        return

    total = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    print(f"\n{'─' * 40}")
    print(f"  BASELINE METRICS ({total} trades)")
    print(f"{'─' * 40}")

    # 1. Win rate by sample windows
    for window in [50, 100, 200]:
        if total >= window:
            subset = trades[-window:]
            wr = compute_win_rate(subset)
            print(f"  Win rate (last {window}): {wr:.1f}%")
        else:
            print(f"  Win rate (last {window}): N/A (only {total} trades)")

    print(f"  Win rate (ALL {total}): {compute_win_rate(trades):.1f}%")

    # 2. Win rate by hour (UTC)
    print(f"\n  WIN RATE BY HOUR (UTC):")
    by_hour_utc = defaultdict(list)
    for t in trades:
        h = t["entry_time"].hour
        by_hour_utc[h].append(t["pnl"] > 0)

    for h in sorted(by_hour_utc.keys()):
        vals = by_hour_utc[h]
        wr = sum(vals) / len(vals) * 100
        flag = " ⚠ UNRELIABLE" if len(vals) < 10 else ""
        flag += " ❌ WEAK" if wr < 45 and len(vals) >= 10 else ""
        print(f"    {h:02d}:00 UTC  {wr:5.1f}%  n={len(vals)}{flag}")

    # 3. Win rate by direction
    print(f"\n  WIN RATE BY DIRECTION:")
    for side in ["LONG", "SHORT"]:
        subset = [t for t in trades if t["side"] == side]
        if subset:
            wr = compute_win_rate(subset)
            avg_pnl = sum(t["pnl"] for t in subset) / len(subset)
            print(f"    {side}: {wr:.1f}% (n={len(subset)}, avg PnL=${avg_pnl:+.2f})")

    # 4. Win rate by trade source
    print(f"\n  WIN RATE BY SOURCE:")
    sources = defaultdict(list)
    for t in trades:
        src = t.get("trade_source", "UNKNOWN")
        if "FALLBACK" in t.get("reason", "").upper() or "OVERRIDE" in t.get("reason", "").upper():
            src = "FALLBACK_OVERRIDE"
        sources[src].append(t)

    for src, subset in sources.items():
        wr = compute_win_rate(subset)
        avg_pnl = sum(t["pnl"] for t in subset) / len(subset)
        print(f"    {src}: {wr:.1f}% (n={len(subset)}, avg PnL=${avg_pnl:+.2f})")

    # 5. Win rate by confidence bucket
    print(f"\n  WIN RATE BY CONFIDENCE:")
    buckets = {"<50": [], "50-59": [], "60-69": [], "70+": []}
    for t in trades:
        c = t.get("confidence", 0)
        if c < 50:
            buckets["<50"].append(t)
        elif c < 60:
            buckets["50-59"].append(t)
        elif c < 70:
            buckets["60-69"].append(t)
        else:
            buckets["70+"].append(t)

    for bk, subset in buckets.items():
        if subset:
            wr = compute_win_rate(subset)
            avg_pnl = sum(t["pnl"] for t in subset) / len(subset)
            print(f"    Conf {bk}: {wr:.1f}% (n={len(subset)}, avg PnL=${avg_pnl:+.2f})")

    # 6. Average holding time
    win_holds = []
    loss_holds = []
    for t in trades:
        hold = (t["exit_time"] - t["entry_time"]).total_seconds()
        if t["pnl"] > 0:
            win_holds.append(hold)
        else:
            loss_holds.append(hold)

    if win_holds:
        print(f"\n  AVG HOLD TIME:")
        print(f"    Winners: {sum(win_holds)/len(win_holds):.0f}s ({sum(win_holds)/len(win_holds)/60:.1f}m)")
    if loss_holds:
        print(f"    Losers:  {sum(loss_holds)/len(loss_holds):.0f}s ({sum(loss_holds)/len(loss_holds)/60:.1f}m)")

    # 7. Fee impact
    total_gross = sum(t["pnl"] for t in trades)
    total_fees = sum(abs(t["entry_price"] * t["quantity"] * 0.00055 * 2) for t in trades if t["quantity"] > 0)
    if total_gross != 0:
        print(f"\n  FEE IMPACT:")
        print(f"    Total gross PnL: ${total_gross:+.2f}")
        print(f"    Est. total fees: ${total_fees:.2f}")
        if total_gross > 0:
            print(f"    Fees as % of gross PnL: {total_fees/total_gross*100:.1f}%")

    # 8. Loss clustering
    print(f"\n  LOSS CLUSTERING:")
    consecutive_losses = 0
    max_consecutive = 0
    for t in trades:
        if t["pnl"] <= 0:
            consecutive_losses += 1
            max_consecutive = max(max_consecutive, consecutive_losses)
        else:
            consecutive_losses = 0
    print(f"    Max consecutive losses: {max_consecutive}")
    print(f"    Total losses: {len(losses)} / {total} ({len(losses)/total*100:.1f}%)")

    print(f"\n{'=' * 60}")
    print("  Analysis complete. Use these findings to tune parameters.")
    print(f"{'=' * 60}")

    conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else str(DB_PATH)
    analyze(db)
