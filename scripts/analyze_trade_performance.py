#!/usr/bin/env python3
"""
Standalone diagnostic script for trade performance analysis.

Reads from bot_data.db (SQLite) and prints a formatted report.
Re-run anytime: python scripts/analyze_trade_performance.py
"""

import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Try to import zoneinfo for timezone conversion
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

DB_CANDIDATES = [
    "bot_data.db",
    "trading_agent/bot_data.db",
    "data/bot_data.db",
]


def find_database() -> str | None:
    """Locate the trade database."""
    base = Path(__file__).resolve().parent.parent
    for candidate in DB_CANDIDATES:
        path = base / candidate
        if path.exists():
            return str(path)
    return None


def get_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {table_name: [columns]} for all tables."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    schema = {}
    for (table,) in cursor.fetchall():
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        schema[table] = [col[1] for col in cols]
    return schema


def load_trades(conn: sqlite3.Connection, schema: dict) -> list[dict]:
    """Attempt to load closed trades from the most likely table."""
    trade_tables = [t for t in schema if "trade" in t.lower()]
    if not trade_tables:
        return []

    table = trade_tables[0]
    cols = schema[table]
    cursor = conn.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    return [dict(zip(cols, row)) for row in rows]


def classify_trade(trade: dict) -> dict:
    """Normalize trade fields into a standard shape."""
    # Try common column names
    pnl = None
    for key in ("pnl", "profit", "realized_pnl", "pnl_usd", "profit_usd"):
        if key in trade and trade[key] is not None:
            try:
                pnl = float(trade[key])
            except (ValueError, TypeError):
                pass
            break

    direction = None
    for key in ("direction", "side", "type"):
        if key in trade and trade[key]:
            val = str(trade[key]).upper()
            if "LONG" in val or "BUY" in val:
                direction = "LONG"
            elif "SHORT" in val or "SELL" in val:
                direction = "SHORT"
            break

    confidence = None
    for key in ("confidence", "ai_confidence", "confidence_score"):
        if key in trade and trade[key] is not None:
            try:
                confidence = float(trade[key])
            except (ValueError, TypeError):
                pass
            break

    source = None
    for key in ("source", "trade_source", "entry_type", "signal_source"):
        if key in trade and trade[key]:
            source = str(trade[key]).lower()
            break

    entry_time = None
    exit_time = None
    for key in ("entry_time", "open_time", "created_at", "timestamp"):
        if key in trade and trade[key]:
            try:
                entry_time = datetime.fromisoformat(str(trade[key]))
            except (ValueError, TypeError):
                pass
            break
    for key in ("exit_time", "close_time", "closed_at"):
        if key in trade and trade[key]:
            try:
                exit_time = datetime.fromisoformat(str(trade[key]))
            except (ValueError, TypeError):
                pass
            break

    fees = None
    for key in ("fee", "fees", "commission", "total_fees"):
        if key in trade and trade[key] is not None:
            try:
                fees = float(trade[key])
            except (ValueError, TypeError):
                pass
            break

    entry_price = None
    for key in ("entry_price", "open_price", "price"):
        if key in trade and trade[key] is not None:
            try:
                entry_price = float(trade[key])
            except (ValueError, TypeError):
                pass
            break

    return {
        "pnl": pnl,
        "direction": direction,
        "confidence": confidence,
        "source": source,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "fees": fees,
        "entry_price": entry_price,
        "is_win": pnl > 0 if pnl is not None else None,
    }


def confidence_bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    if conf < 50:
        return "<50"
    if conf < 60:
        return "50-59"
    if conf < 70:
        return "60-69"
    return "70+"


def compute_win_rate(trades: list[dict], label: str = "") -> str:
    wins = sum(1 for t in trades if t["is_win"] is True)
    losses = sum(1 for t in trades if t["is_win"] is False)
    total = wins + losses
    if total == 0:
        return f"  {label}: no data"
    rate = wins / total * 100
    return f"  {label}: {rate:.1f}% ({wins}W / {losses}L / {total} total)"


def analyze(trades: list[dict]) -> None:
    """Run full analysis and print report."""
    classified = [classify_trade(t) for t in trades]
    # Filter to trades with PnL data
    with_pnl = [t for t in classified if t["pnl"] is not None]

    if not with_pnl:
        print("No trades with PnL data found. Cannot compute metrics.")
        print(f"Total raw trade records: {len(trades)}")
        if trades:
            print(f"Sample columns: {list(trades[0].keys())}")
        return

    print(f"{'='*60}")
    print(f" TRADE PERFORMANCE REPORT")
    print(f" Generated: {datetime.now().isoformat()}")
    print(f" Total trades with PnL: {len(with_pnl)}")
    print(f"{'='*60}")

    # 1. Overall win rate by sample size
    print("\n--- 1. OVERALL WIN RATE ---")
    for n in [50, 100, 200, len(with_pnl)]:
        sample = with_pnl[-n:] if n <= len(with_pnl) else with_pnl
        if len(sample) > 0:
            print(compute_win_rate(sample, f"Last {min(n, len(with_pnl))}"))

    # 2. Win rate by hour (UTC)
    print("\n--- 2. WIN RATE BY HOUR (UTC) ---")
    by_hour_utc = defaultdict(list)
    for t in with_pnl:
        if t["entry_time"]:
            h = t["entry_time"].hour
            by_hour_utc[h].append(t)
    for h in sorted(by_hour_utc):
        label = f"  {h:02d}:00 UTC"
        count = len(by_hour_utc[h])
        flag = " ⚠ (< 10 trades)" if count < 10 else ""
        print(compute_win_rate(by_hour_utc[h], label) + flag)

    # 2b. Win rate by hour (Europe/Warsaw)
    if ZoneInfo:
        print("\n--- 2b. WIN RATE BY HOUR (Europe/Warsaw) ---")
        warsaw_tz = ZoneInfo("Europe/Warsaw")
        by_hour_warsaw = defaultdict(list)
        for t in with_pnl:
            if t["entry_time"]:
                try:
                    utc_time = t["entry_time"].replace(tzinfo=timezone.utc)
                    local_time = utc_time.astimezone(warsaw_tz)
                    by_hour_warsaw[local_time.hour].append(t)
                except Exception:
                    pass
        for h in sorted(by_hour_warsaw):
            label = f"  {h:02d}:00 Warsaw"
            count = len(by_hour_warsaw[h])
            flag = " ⚠ (< 10 trades)" if count < 10 else ""
            print(compute_win_rate(by_hour_warsaw[h], label) + flag)

    # 3. Win rate by direction
    print("\n--- 3. WIN RATE BY DIRECTION ---")
    by_dir = defaultdict(list)
    for t in with_pnl:
        by_dir[t["direction"] or "unknown"].append(t)
    for d in sorted(by_dir):
        print(compute_win_rate(by_dir[d], d))

    # 4. Win rate by source
    print("\n--- 4. WIN RATE BY SOURCE ---")
    by_source = defaultdict(list)
    for t in with_pnl:
        by_source[t["source"] or "unknown"].append(t)
    for s in sorted(by_source):
        print(compute_win_rate(by_source[s], s))

    # 5. Win rate by confidence bucket
    print("\n--- 5. WIN RATE BY CONFIDENCE ---")
    by_conf = defaultdict(list)
    for t in with_pnl:
        by_conf[confidence_bucket(t["confidence"])].append(t)
    for bucket in ["<50", "50-59", "60-69", "70+", "unknown"]:
        if bucket in by_conf:
            print(compute_win_rate(by_conf[bucket], bucket))

    # 6. Average PnL by confidence bucket
    print("\n--- 6. AVG PnL BY CONFIDENCE ---")
    for bucket in ["<50", "50-59", "60-69", "70+", "unknown"]:
        if bucket in by_conf:
            pnls = [t["pnl"] for t in by_conf[bucket] if t["pnl"] is not None]
            if pnls:
                avg = sum(pnls) / len(pnls)
                total = sum(pnls)
                print(f"  {bucket}: avg ${avg:.2f}, total ${total:.2f} ({len(pnls)} trades)")

    # 7. Trade count by hour
    print("\n--- 7. TRADE COUNT BY HOUR (UTC) ---")
    for h in range(24):
        count = len(by_hour_utc.get(h, []))
        bar = "#" * min(count, 50)
        flag = " ⚠ unreliable" if 0 < count < 10 else ""
        print(f"  {h:02d}:00  {count:4d} {bar}{flag}")

    # 8. Loss clustering
    print("\n--- 8. LOSS CLUSTERING ---")
    losses = [t for t in with_pnl if t["is_win"] is False]
    if losses:
        # By source
        loss_by_source = defaultdict(int)
        for t in losses:
            loss_by_source[t["source"] or "unknown"] += 1
        print("  By source:")
        for s, c in sorted(loss_by_source.items(), key=lambda x: -x[1]):
            print(f"    {s}: {c} losses")

        # By hour
        loss_by_hour = defaultdict(int)
        for t in losses:
            if t["entry_time"]:
                loss_by_hour[t["entry_time"].hour] += 1
        if loss_by_hour:
            worst_hour = max(loss_by_hour, key=loss_by_hour.get)
            print(f"  Worst hour (UTC): {worst_hour:02d}:00 with {loss_by_hour[worst_hour]} losses")

        # By direction
        loss_by_dir = defaultdict(int)
        for t in losses:
            loss_by_dir[t["direction"] or "unknown"] += 1
        print("  By direction:")
        for d, c in sorted(loss_by_dir.items(), key=lambda x: -x[1]):
            print(f"    {d}: {c} losses")
    else:
        print("  No losses recorded.")

    # 9. Average holding time
    print("\n--- 9. AVG HOLDING TIME ---")
    win_times = []
    loss_times = []
    for t in with_pnl:
        if t["entry_time"] and t["exit_time"]:
            duration = (t["exit_time"] - t["entry_time"]).total_seconds()
            if duration > 0:
                if t["is_win"]:
                    win_times.append(duration)
                elif t["is_win"] is False:
                    loss_times.append(duration)
    if win_times:
        avg_w = sum(win_times) / len(win_times)
        print(f"  Wins:   avg {avg_w/60:.1f} min ({len(win_times)} trades)")
    else:
        print("  Wins:   no timing data")
    if loss_times:
        avg_l = sum(loss_times) / len(loss_times)
        print(f"  Losses: avg {avg_l/60:.1f} min ({len(loss_times)} trades)")
    else:
        print("  Losses: no timing data")

    # 10. Fee impact
    print("\n--- 10. FEE IMPACT ---")
    total_fees = sum(t["fees"] for t in with_pnl if t["fees"] is not None)
    total_gross_pnl = sum(t["pnl"] for t in with_pnl if t["pnl"] is not None)
    trades_with_fees = sum(1 for t in with_pnl if t["fees"] is not None)
    if trades_with_fees > 0 and total_gross_pnl != 0:
        fee_fraction = abs(total_fees / total_gross_pnl) * 100
        print(f"  Total fees:      ${total_fees:.2f}")
        print(f"  Total gross PnL: ${total_gross_pnl:.2f}")
        print(f"  Net PnL:         ${total_gross_pnl - total_fees:.2f}")
        print(f"  Fees as % of gross PnL: {fee_fraction:.1f}%")
    else:
        print("  No fee data available.")

    print(f"\n{'='*60}")
    print(" END OF REPORT")
    print(f"{'='*60}")


def main():
    db_path = find_database()
    if not db_path:
        print("No trade database found (checked: " + ", ".join(DB_CANDIDATES) + ")")
        print("This is expected if the trading bot hasn't run yet.")
        print("Re-run this script after the bot has executed some trades.")
        sys.exit(0)

    print(f"Database found: {db_path}")
    conn = sqlite3.connect(db_path)

    try:
        schema = get_schema(conn)
        if not schema:
            print("Database is empty (no tables).")
            sys.exit(0)

        print(f"\nDatabase schema:")
        for table, cols in schema.items():
            print(f"  {table}: {', '.join(cols)}")

        trades = load_trades(conn, schema)
        if not trades:
            print("\nNo trade records found in database.")
            print("The bot may not have executed any trades yet.")
            sys.exit(0)

        print(f"\nLoaded {len(trades)} trade records.\n")
        analyze(trades)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
