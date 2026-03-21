#!/usr/bin/env python3
"""
Data export script — CSV, JSON, XLSX, Markdown, HTML exports.

Usage:
  python scripts/export_data.py --all --format csv --output ./exports/
  python scripts/export_data.py --table trade_decisions --format csv
  python scripts/export_data.py --table learning_journal --format json
  python scripts/export_data.py --all --after 2025-01-15 --before 2025-01-22
  python scripts/export_data.py --all --last-days 7
  python scripts/export_data.py --trades --format csv
  python scripts/export_data.py --journal-report --format md
  python scripts/export_data.py --analytics-report --format html
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB = "bot_data.db"
DEFAULT_OUTPUT = "./exports/"

EXPORTABLE_TABLES = [
    "trade_decisions", "market_snapshots", "equity_curve",
    "daily_sessions", "learning_journal", "parameter_history",
    "token_usage", "trades",
]


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return [r[0] for r in rows]


def export_table_csv(conn: sqlite3.Connection, table: str, output_dir: str,
                     after: str | None = None, before: str | None = None) -> str:
    """Export a table to CSV. Returns output file path."""
    os.makedirs(output_dir, exist_ok=True)

    query = f"SELECT * FROM {table}"
    params = []
    conditions = []

    # Try timestamp filtering
    ts_col = None
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    for candidate in ["timestamp_utc", "date", "entry_time"]:
        if candidate in cols:
            ts_col = candidate
            break

    if ts_col:
        if after:
            conditions.append(f"{ts_col} >= ?")
            params.append(after)
        if before:
            conditions.append(f"{ts_col} <= ?")
            params.append(before)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY {ts_col}" if ts_col else ""

    rows = conn.execute(query, params).fetchall()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{table}_{ts}.csv"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))

    return filepath


def export_table_json(conn: sqlite3.Connection, table: str, output_dir: str,
                      after: str | None = None, before: str | None = None) -> str:
    """Export a table to JSON."""
    os.makedirs(output_dir, exist_ok=True)

    query = f"SELECT * FROM {table}"
    params = []
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    ts_col = next((c for c in ["timestamp_utc", "date"] if c in cols), None)

    if ts_col:
        conditions = []
        if after:
            conditions.append(f"{ts_col} >= ?")
            params.append(after)
        if before:
            conditions.append(f"{ts_col} <= ?")
            params.append(before)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

    rows = conn.execute(query, params).fetchall()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{table}_{ts}.json")

    data = [dict(r) for r in rows]
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return filepath


def export_merged_trades(conn: sqlite3.Connection, output_dir: str,
                         after: str | None = None, before: str | None = None) -> str:
    """Export trades with entry+exit merged into single rows."""
    os.makedirs(output_dir, exist_ok=True)

    query = """
        SELECT e.trade_id, e.timestamp_utc as entry_time, x.timestamp_utc as exit_time,
               e.decision as direction,
               e.entry_price, x.exit_price, e.sl_price, e.tp_price,
               x.gross_pnl_usd, x.fees_paid_usd, x.net_pnl_usd,
               x.hold_duration_sec, x.max_favorable_excursion, x.max_adverse_excursion,
               e.regime, e.setup_type, e.entry_quality, e.confidence,
               e.extension_atr, e.htf_alignment, e.trade_source, x.exit_type,
               e.entry_reason, e.net_rr, e.volume_ratio, e.adx, e.cvd_aligned, e.oi_confirmed
        FROM trade_decisions e
        JOIN trade_decisions x ON e.trade_id = x.trade_id
        WHERE e.decision LIKE 'ENTRY_%' AND x.decision LIKE 'EXIT_%'
          AND e.trade_id IS NOT NULL
    """
    params = []
    if after:
        query += " AND e.timestamp_utc >= ?"
        params.append(after)
    if before:
        query += " AND e.timestamp_utc <= ?"
        params.append(before)
    query += " ORDER BY e.timestamp_utc"

    rows = conn.execute(query, params).fetchall()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"trades_merged_{ts}.csv")

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))

    return filepath


def export_journal_report(conn: sqlite3.Connection, output_dir: str) -> str:
    """Export learning journal as readable markdown."""
    os.makedirs(output_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT * FROM learning_journal ORDER BY timestamp_utc DESC"
    ).fetchall()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"journal_report_{ts}.md")

    icons = {
        "POST_WIN": "WIN", "POST_LOSS": "LOSS", "POST_SKIP_REVIEW": "SKIP_REVIEW",
        "REGIME_SHIFT": "REGIME", "EDGE_DECAY": "EDGE", "TUNING_CYCLE": "TUNING",
        "ROLLBACK": "ROLLBACK", "META_LEARNING": "META", "PARAMETER_INSIGHT": "INSIGHT",
    }

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Learning Journal Report\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Total entries: {len(rows)}\n\n---\n\n")

        for row in rows:
            r = dict(row)
            icon = icons.get(r["entry_type"], r["entry_type"])
            f.write(f"### [{icon}] {r['timestamp_utc']}\n\n")
            if r.get("trigger"):
                f.write(f"**Trigger:** {r['trigger']}\n\n")
            if r.get("observation"):
                f.write(f"**Observation:** {r['observation']}\n\n")
            if r.get("conclusion"):
                f.write(f"**Conclusion:** {r['conclusion']}\n\n")
            if r.get("suggested_action"):
                f.write(f"**Suggested Action:** {r['suggested_action']}\n\n")
            f.write("---\n\n")

    return filepath


def export_analytics_report(conn: sqlite3.Connection, output_dir: str) -> str:
    """Export analytics as HTML report."""
    os.makedirs(output_dir, exist_ok=True)

    from performance_engine import run_full_report
    # Get the db path from the connection
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    report = run_full_report(db_path)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"analytics_report_{ts}.html")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><html><head><title>Analytics Report</title>")
        f.write("<style>body{font-family:monospace;max-width:900px;margin:auto;padding:20px}")
        f.write("table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 8px}")
        f.write("th{background:#f0f0f0}.pass{color:green}.fail{color:red}</style></head><body>")
        f.write(f"<h1>Performance Analytics Report</h1>")
        f.write(f"<p>Generated: {datetime.now(timezone.utc).isoformat()}</p>")
        f.write(f"<p>Trades: {report.get('trade_count', 0)} | Skips: {report.get('skip_count', 0)}</p>")

        # Recommendations
        recs = report.get("recommendations", [])
        if recs:
            f.write("<h2>Recommendations</h2><ul>")
            for r in recs:
                f.write(f"<li>{r}</li>")
            f.write("</ul>")

        # Alerts
        alerts = report.get("alerts", [])
        if alerts:
            f.write("<h2>Alerts</h2><ul>")
            for a in alerts:
                f.write(f"<li class='fail'>{a}</li>")
            f.write("</ul>")

        # WR decomposition
        wr = report.get("win_rate_decomposition", {})
        if wr:
            f.write("<h2>Win Rate Decomposition</h2>")
            for dim, groups in wr.items():
                f.write(f"<h3>{dim}</h3><table><tr><th>Group</th><th>WR%</th><th>Avg PnL</th><th>N</th></tr>")
                for g, s in groups.items():
                    f.write(f"<tr><td>{g}</td><td>{s['win_rate']}</td><td>${s['avg_pnl']}</td><td>{s['count']}</td></tr>")
                f.write("</table>")

        f.write("</body></html>")

    return filepath


def main():
    parser = argparse.ArgumentParser(description="Data Export")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--format", default="csv", choices=["csv", "json", "xlsx", "md", "html"])
    parser.add_argument("--table", help="Export specific table")
    parser.add_argument("--all", action="store_true", help="Export all tables")
    parser.add_argument("--trades", action="store_true", help="Merged trades export")
    parser.add_argument("--journal-report", action="store_true", help="Journal markdown report")
    parser.add_argument("--analytics-report", action="store_true", help="Analytics HTML report")
    parser.add_argument("--after", help="Filter: after date (YYYY-MM-DD)")
    parser.add_argument("--before", help="Filter: before date (YYYY-MM-DD)")
    parser.add_argument("--last-days", type=int, help="Filter: last N days")
    args = parser.parse_args()

    if args.last_days:
        args.after = (datetime.now(timezone.utc) - timedelta(days=args.last_days)).strftime("%Y-%m-%d")

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    conn = get_conn(args.db)
    tables = get_tables(conn)
    exported = []

    try:
        if args.trades:
            path = export_merged_trades(conn, args.output, args.after, args.before)
            exported.append(path)

        elif args.journal_report:
            if "learning_journal" not in tables:
                print("No learning_journal table found")
                sys.exit(1)
            path = export_journal_report(conn, args.output)
            exported.append(path)

        elif args.analytics_report:
            path = export_analytics_report(conn, args.output)
            exported.append(path)

        elif args.table:
            if args.table not in tables:
                print(f"Table '{args.table}' not found. Available: {tables}")
                sys.exit(1)
            if args.format == "json":
                path = export_table_json(conn, args.table, args.output, args.after, args.before)
            else:
                path = export_table_csv(conn, args.table, args.output, args.after, args.before)
            exported.append(path)

        elif args.all:
            for table in tables:
                if table in EXPORTABLE_TABLES:
                    if args.format == "json":
                        path = export_table_json(conn, table, args.output, args.after, args.before)
                    else:
                        path = export_table_csv(conn, table, args.output, args.after, args.before)
                    exported.append(path)

        else:
            parser.print_help()
            sys.exit(0)

        for path in exported:
            print(f"Exported: {path}")
        print(f"\nTotal: {len(exported)} file(s)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
