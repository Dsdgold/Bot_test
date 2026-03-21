#!/usr/bin/env python3
"""
Performance Analytics Engine — turns raw trade data into actionable intelligence.

Reads from bot_data.db and produces:
- Win rate decomposition by every dimension
- Factor correlation analysis
- MFE/MAE analysis with SL/TP recommendations
- Regime × setup × direction performance matrix
- Edge detection alerts
- Fee impact report
- Skip analysis
- Automated parameter recommendations

Run: python scripts/performance_engine.py [--db path] [--min-sample N]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB = "bot_data.db"
MIN_SAMPLE = 10


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_trades(conn: sqlite3.Connection) -> list[dict]:
    """Load closed trades (EXIT rows) from trade_decisions."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM trade_decisions
        WHERE decision LIKE 'EXIT_%' AND net_pnl_usd IS NOT NULL
        ORDER BY timestamp_utc
    """).fetchall()
    return [dict(r) for r in rows]


def load_skips(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM trade_decisions WHERE decision = 'SKIP'
        ORDER BY timestamp_utc
    """).fetchall()
    return [dict(r) for r in rows]


def load_all_decisions(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trade_decisions ORDER BY timestamp_utc").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Win rate helpers
# ---------------------------------------------------------------------------

def compute_wr(trades: list[dict]) -> dict:
    """Compute win rate stats for a group of trades."""
    if not trades:
        return {"win_rate": 0, "avg_pnl": 0, "count": 0, "reliable": False}
    wins = sum(1 for t in trades if (t.get("net_pnl_usd") or 0) > 0)
    pnls = [t.get("net_pnl_usd") or 0 for t in trades]
    n = len(trades)
    return {
        "win_rate": round(wins / n * 100, 1),
        "avg_pnl": round(sum(pnls) / n, 2),
        "total_pnl": round(sum(pnls), 2),
        "count": n,
        "reliable": n >= MIN_SAMPLE,
    }


def bucket_value(val, boundaries: list) -> str:
    """Put a numeric value into a labeled bucket."""
    if val is None:
        return "unknown"
    for i, b in enumerate(boundaries):
        if val < b:
            if i == 0:
                return f"<{b}"
            return f"{boundaries[i-1]}-{b}"
    return f"{boundaries[-1]}+"


def group_by(trades: list[dict], key_fn) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        groups[str(k) if k is not None else "unknown"].append(t)
    return dict(groups)


# ---------------------------------------------------------------------------
# A. Win rate decomposition
# ---------------------------------------------------------------------------

def win_rate_decomposition(trades: list[dict]) -> dict[str, dict]:
    """Break win rate by every dimension."""
    results = {}

    dimensions = {
        "regime": lambda t: t.get("regime"),
        "direction": lambda t: "LONG" if "LONG" in (t.get("decision") or "") else "SHORT",
        "setup_type": lambda t: t.get("setup_type"),
        "htf_alignment": lambda t: t.get("htf_alignment"),
        "confidence_bucket": lambda t: bucket_value(t.get("confidence"), [60, 65, 70, 75, 80]),
        "quality_bucket": lambda t: bucket_value(t.get("entry_quality"), [60, 65, 70, 75, 80, 85, 90]),
        "session_hour": lambda t: t.get("session_hour_local"),
        "day_of_week": lambda t: t.get("day_of_week"),
        "trade_source": lambda t: t.get("trade_source"),
        "entry_type": lambda t: t.get("entry_type"),
        "extension_bucket": lambda t: bucket_value(t.get("extension_atr"), [0.3, 0.6, 0.8]),
        "volume_ratio_bucket": lambda t: bucket_value(t.get("volume_ratio"), [1.0, 1.15, 1.5]),
        "cvd_aligned": lambda t: "yes" if t.get("cvd_aligned") else "no",
        "oi_confirmed": lambda t: "yes" if t.get("oi_confirmed") else "no",
    }

    for dim_name, key_fn in dimensions.items():
        groups = group_by(trades, key_fn)
        dim_results = {}
        for group_key, group_trades in sorted(groups.items()):
            dim_results[group_key] = compute_wr(group_trades)
        results[dim_name] = dim_results

    return results


# ---------------------------------------------------------------------------
# B. Factor correlation
# ---------------------------------------------------------------------------

def factor_correlation(trades: list[dict]) -> list[tuple[str, float]]:
    """Compute correlation of each numeric factor with net PnL."""
    factors = [
        "confidence", "entry_quality", "extension_atr", "volume_ratio",
        "adx", "chop_index", "bb_width", "rsi_14", "net_rr",
        "atr_1m", "ema_slope_1m", "cvd_aligned", "oi_confirmed",
    ]

    pnls = [t.get("net_pnl_usd") or 0 for t in trades]
    if not pnls or len(pnls) < 5:
        return []

    mean_pnl = sum(pnls) / len(pnls)
    std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls))
    if std_pnl == 0:
        return []

    correlations = []
    for factor in factors:
        vals = []
        valid_pnls = []
        for i, t in enumerate(trades):
            v = t.get(factor)
            if v is not None:
                try:
                    vals.append(float(v))
                    valid_pnls.append(pnls[i])
                except (ValueError, TypeError):
                    pass

        if len(vals) < 5:
            continue

        mean_v = sum(vals) / len(vals)
        std_v = math.sqrt(sum((v - mean_v) ** 2 for v in vals) / len(vals))
        if std_v == 0:
            continue

        cov = sum((vals[i] - mean_v) * (valid_pnls[i] - mean_pnl) for i in range(len(vals))) / len(vals)
        corr = cov / (std_v * std_pnl)
        correlations.append((factor, round(corr, 3)))

    correlations.sort(key=lambda x: abs(x[1]), reverse=True)
    return correlations


# ---------------------------------------------------------------------------
# C. MFE/MAE analysis
# ---------------------------------------------------------------------------

def mfe_mae_analysis(trades: list[dict]) -> dict:
    """Analyze MFE/MAE patterns for SL/TP optimization."""
    winners = [t for t in trades if (t.get("net_pnl_usd") or 0) > 0]
    losers = [t for t in trades if (t.get("net_pnl_usd") or 0) <= 0]

    result = {"sufficient_data": False}

    def avg(vals):
        return sum(vals) / len(vals) if vals else 0

    win_mfe = [t.get("max_favorable_excursion") or 0 for t in winners]
    win_mae = [t.get("max_adverse_excursion") or 0 for t in winners]
    loss_mfe = [t.get("max_favorable_excursion") or 0 for t in losers]
    loss_mae = [t.get("max_adverse_excursion") or 0 for t in losers]

    if len(winners) >= 5:
        result["sufficient_data"] = True
        result["avg_mfe_winners"] = round(avg(win_mfe), 2)
        result["avg_mae_winners"] = round(avg(win_mae), 2)
        result["avg_mfe_losers"] = round(avg(loss_mfe), 2)
        result["avg_mae_losers"] = round(avg(loss_mae), 2)
        result["winner_count"] = len(winners)
        result["loser_count"] = len(losers)

        # Recommendations
        recs = []
        avg_tp = avg([abs(t.get("tp_price", 0) - t.get("entry_price", 0)) for t in winners if t.get("tp_price")])
        if avg_tp > 0 and avg(win_mfe) > avg_tp * 1.3:
            recs.append(f"TP too tight: avg MFE on wins (${avg(win_mfe):.0f}) >> avg TP distance (${avg_tp:.0f}). Widen targets.")

        loss_with_positive_mfe = sum(1 for m in loss_mfe if m > 0)
        if losers and loss_with_positive_mfe / max(len(losers), 1) > 0.3:
            recs.append(f"SL may be too tight: {loss_with_positive_mfe}/{len(losers)} losers had positive MFE.")

        result["recommendations"] = recs

    return result


# ---------------------------------------------------------------------------
# D. Regime performance matrix
# ---------------------------------------------------------------------------

def regime_performance_matrix(trades: list[dict]) -> list[dict]:
    """Cross-tabulate regime × setup_type × direction."""
    combos = defaultdict(list)
    for t in trades:
        regime = t.get("regime", "unknown")
        setup = t.get("setup_type", "unknown")
        direction = "LONG" if "LONG" in (t.get("decision") or "") else "SHORT"
        key = f"{regime} | {setup} | {direction}"
        combos[key].append(t)

    rows = []
    for key, group in sorted(combos.items()):
        stats = compute_wr(group)
        rows.append({"combo": key, **stats})

    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# E. Time-decay / edge detection
# ---------------------------------------------------------------------------

def edge_detection(trades: list[dict], window: int = 30) -> dict:
    """Rolling win rate and edge decay detection."""
    if len(trades) < window:
        return {"sufficient_data": False, "rolling_wr": [], "alerts": []}

    rolling = []
    alerts = []
    for i in range(window, len(trades) + 1):
        batch = trades[i - window:i]
        wr = compute_wr(batch)["win_rate"]
        rolling.append({"trade_index": i, "win_rate": wr})

        if wr < 50:
            alerts.append(f"Edge decay at trade {i}: {window}-trade WR = {wr}%")

    current_wr = rolling[-1]["win_rate"] if rolling else 0
    return {
        "sufficient_data": True,
        "current_rolling_wr": current_wr,
        "rolling_points": len(rolling),
        "alerts": alerts[-3:],  # Last 3 alerts
    }


# ---------------------------------------------------------------------------
# F. Fee impact
# ---------------------------------------------------------------------------

def fee_impact_report(trades: list[dict]) -> dict:
    """Analyze fee drag on performance."""
    fees = [t.get("fees_paid_usd") or 0 for t in trades]
    gross = [t.get("gross_pnl_usd") or 0 for t in trades]
    slippage = [t.get("slippage_bps") or 0 for t in trades]
    net_rrs = [t.get("net_rr") or 0 for t in trades if t.get("net_rr")]

    total_fees = sum(fees)
    total_gross = sum(gross)
    maker_count = sum(1 for t in trades if t.get("entry_type") == "MAKER")
    taker_count = sum(1 for t in trades if t.get("entry_type") == "TAKER")

    fee_pct = abs(total_fees / total_gross * 100) if total_gross != 0 else 0

    return {
        "total_fees": round(total_fees, 2),
        "total_gross_pnl": round(total_gross, 2),
        "fee_pct_of_gross": round(fee_pct, 1),
        "maker_count": maker_count,
        "taker_count": taker_count,
        "avg_slippage_bps": round(sum(slippage) / len(slippage), 1) if slippage else 0,
        "avg_net_rr": round(sum(net_rrs) / len(net_rrs), 2) if net_rrs else 0,
        "fee_erosion_alert": fee_pct > 40,
    }


# ---------------------------------------------------------------------------
# G. Skip analysis
# ---------------------------------------------------------------------------

def skip_analysis(skips: list[dict], trades: list[dict]) -> dict:
    """Analyze skip patterns."""
    reason_counts: dict[str, int] = defaultdict(int)
    for s in skips:
        reason = s.get("skip_reason") or "unknown"
        # Take first reason
        first = reason.split(";")[0].strip().split(":")[0] if ":" in reason else reason.split(";")[0].strip()
        reason_counts[first] = reason_counts.get(first, 0) + 1

    total_decisions = len(skips) + len(trades)
    skip_rate = len(skips) / total_decisions * 100 if total_decisions > 0 else 0

    return {
        "total_skips": len(skips),
        "total_trades": len(trades),
        "skip_rate_pct": round(skip_rate, 1),
        "top_skip_reasons": dict(sorted(reason_counts.items(), key=lambda x: -x[1])[:10]),
        "skip_rate_alert": skip_rate > 95,
    }


# ---------------------------------------------------------------------------
# H. Automated parameter recommendations
# ---------------------------------------------------------------------------

def generate_recommendations(
    wr_decomp: dict,
    mfe_mae: dict,
    fee_report: dict,
    trades: list[dict],
    min_sample: int = 50,
) -> list[str]:
    """Generate concrete parameter recommendations with evidence."""
    recs = []

    if len(trades) < min_sample:
        return [f"Insufficient data ({len(trades)} trades < {min_sample} minimum). Collect more trades."]

    # Confidence bucket analysis
    conf_data = wr_decomp.get("confidence_bucket", {})
    low_conf = []
    high_conf = []
    for bucket, stats in conf_data.items():
        if stats["count"] >= 10:
            if stats["win_rate"] < 50:
                low_conf.append((bucket, stats))
            elif stats["win_rate"] >= 65:
                high_conf.append((bucket, stats))

    if low_conf and high_conf:
        worst = min(low_conf, key=lambda x: x[1]["win_rate"])
        best = max(high_conf, key=lambda x: x[1]["win_rate"])
        recs.append(
            f"RAISE MIN_CONFIDENCE: Bucket {worst[0]} shows {worst[1]['win_rate']}% WR "
            f"(n={worst[1]['count']}), vs {best[0]} at {best[1]['win_rate']}% (n={best[1]['count']})"
        )

    # MFE/MAE recommendations
    if mfe_mae.get("recommendations"):
        recs.extend(mfe_mae["recommendations"])

    # Fee erosion
    if fee_report.get("fee_erosion_alert"):
        recs.append(
            f"FEE DRAG HIGH: Fees consume {fee_report['fee_pct_of_gross']}% of gross PnL. "
            f"Prefer maker orders (currently {fee_report['maker_count']}M/{fee_report['taker_count']}T)"
        )

    # Regime analysis
    regime_data = wr_decomp.get("regime", {})
    for regime, stats in regime_data.items():
        if stats["count"] >= 10 and stats["win_rate"] < 45:
            recs.append(f"BLOCK regime {regime}: {stats['win_rate']}% WR (n={stats['count']})")

    # Session hours
    hour_data = wr_decomp.get("session_hour", {})
    bad_hours = []
    for hour, stats in hour_data.items():
        if stats["count"] >= 10 and stats["win_rate"] < 45:
            bad_hours.append(f"{hour}:00 ({stats['win_rate']}% WR, n={stats['count']})")
    if bad_hours:
        recs.append(f"BLOCK HOURS: {', '.join(bad_hours)}")

    return recs


# ---------------------------------------------------------------------------
# I. Edge detection alerts
# ---------------------------------------------------------------------------

def edge_alerts(trades: list[dict], skips: list[dict], fee_report: dict) -> list[str]:
    """Generate edge detection alerts."""
    alerts = []

    # Rolling WR check
    if len(trades) >= 30:
        recent = trades[-30:]
        wr = compute_wr(recent)["win_rate"]
        if wr < 50:
            alerts.append(f"EDGE DECAY: Last 30-trade WR = {wr}% (< 50%)")

    # Drawdown check
    equities = [t.get("equity_after") or 0 for t in trades if t.get("equity_after")]
    if equities:
        peak = max(equities)
        current = equities[-1]
        if peak > 0:
            dd = (peak - current) / peak * 100
            if dd > 5:
                alerts.append(f"DRAWDOWN: {dd:.1f}% from peak (${peak:.0f} → ${current:.0f})")

    # Fee erosion
    if fee_report.get("fee_erosion_alert"):
        alerts.append(f"FEE DRAG: {fee_report['fee_pct_of_gross']}% of gross PnL consumed by fees")

    # Skip rate
    total = len(trades) + len(skips)
    if total > 0 and len(skips) / total > 0.95:
        alerts.append(f"HIGH SKIP RATE: {len(skips)/total*100:.0f}% — filters may be too tight")

    return alerts


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def run_full_report(db_path: str, min_sample: int = MIN_SAMPLE) -> dict:
    """Run full analytics and return structured report."""
    global MIN_SAMPLE
    MIN_SAMPLE = min_sample

    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}", "sufficient_data": False}

    conn = sqlite3.connect(db_path)

    try:
        # Check if table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if "trade_decisions" not in tables:
            return {"error": "No trade_decisions table", "sufficient_data": False}

        trades = load_trades(conn)
        skips = load_skips(conn)

        if not trades:
            return {
                "sufficient_data": False,
                "trade_count": 0,
                "skip_count": len(skips),
                "message": "No closed trades found. Collect more data.",
            }

        report = {
            "sufficient_data": len(trades) >= min_sample,
            "trade_count": len(trades),
            "skip_count": len(skips),
        }

        # A. Win rate decomposition
        report["win_rate_decomposition"] = win_rate_decomposition(trades)

        # B. Factor correlation
        report["factor_correlation"] = factor_correlation(trades)

        # C. MFE/MAE
        report["mfe_mae"] = mfe_mae_analysis(trades)

        # D. Regime matrix
        report["regime_matrix"] = regime_performance_matrix(trades)

        # E. Edge detection
        report["edge_detection"] = edge_detection(trades)

        # F. Fees
        report["fee_impact"] = fee_impact_report(trades)

        # G. Skips
        report["skip_analysis"] = skip_analysis(skips, trades)

        # H. Recommendations
        report["recommendations"] = generate_recommendations(
            report["win_rate_decomposition"],
            report["mfe_mae"],
            report["fee_impact"],
            trades,
            min_sample,
        )

        # I. Alerts
        report["alerts"] = edge_alerts(trades, skips, report["fee_impact"])

        return report

    finally:
        conn.close()


def print_report(report: dict) -> None:
    """Print formatted report to stdout."""
    print("=" * 70)
    print(" PERFORMANCE ANALYTICS ENGINE — REPORT")
    print("=" * 70)

    if report.get("error"):
        print(f"\nERROR: {report['error']}")
        return

    print(f"\nTrades: {report['trade_count']} | Skips: {report['skip_count']}")
    print(f"Sufficient data: {'YES' if report['sufficient_data'] else 'NO'}")

    if not report.get("sufficient_data") and report["trade_count"] == 0:
        print(f"\n{report.get('message', 'No data.')}")
        return

    # Win rate decomposition
    print(f"\n{'─' * 70}")
    print(" A. WIN RATE DECOMPOSITION")
    print(f"{'─' * 70}")
    for dim_name, groups in report.get("win_rate_decomposition", {}).items():
        print(f"\n  {dim_name.upper()}:")
        for key, stats in groups.items():
            flag = " ⚠" if not stats["reliable"] else ""
            print(f"    {key:>20s}: {stats['win_rate']:5.1f}% WR | "
                  f"avg ${stats['avg_pnl']:>8.2f} | n={stats['count']}{flag}")

    # Factor correlation
    print(f"\n{'─' * 70}")
    print(" B. FACTOR CORRELATION WITH PnL")
    print(f"{'─' * 70}")
    for factor, corr in report.get("factor_correlation", []):
        bar = "█" * int(abs(corr) * 20)
        sign = "+" if corr > 0 else "-"
        print(f"  {factor:>25s}: {sign}{abs(corr):.3f} {bar}")

    # MFE/MAE
    print(f"\n{'─' * 70}")
    print(" C. MFE / MAE ANALYSIS")
    print(f"{'─' * 70}")
    mfe = report.get("mfe_mae", {})
    if mfe.get("sufficient_data"):
        print(f"  Avg MFE (winners): ${mfe['avg_mfe_winners']:.2f}")
        print(f"  Avg MAE (winners): ${mfe['avg_mae_winners']:.2f}")
        print(f"  Avg MFE (losers):  ${mfe['avg_mfe_losers']:.2f}")
        print(f"  Avg MAE (losers):  ${mfe['avg_mae_losers']:.2f}")
        for rec in mfe.get("recommendations", []):
            print(f"  → {rec}")
    else:
        print("  Insufficient MFE/MAE data")

    # Regime matrix
    print(f"\n{'─' * 70}")
    print(" D. REGIME × SETUP × DIRECTION MATRIX")
    print(f"{'─' * 70}")
    for row in report.get("regime_matrix", [])[:15]:
        flag = " ⚠" if not row["reliable"] else ""
        print(f"  {row['combo']:>45s}: {row['win_rate']:5.1f}% WR | "
              f"${row['total_pnl']:>8.2f} | n={row['count']}{flag}")

    # Fee impact
    print(f"\n{'─' * 70}")
    print(" F. FEE IMPACT")
    print(f"{'─' * 70}")
    fees = report.get("fee_impact", {})
    print(f"  Total fees:      ${fees.get('total_fees', 0):.2f}")
    print(f"  Total gross PnL: ${fees.get('total_gross_pnl', 0):.2f}")
    print(f"  Fee % of gross:  {fees.get('fee_pct_of_gross', 0):.1f}%")
    print(f"  Maker/Taker:     {fees.get('maker_count', 0)}M / {fees.get('taker_count', 0)}T")

    # Skip analysis
    print(f"\n{'─' * 70}")
    print(" G. SKIP ANALYSIS")
    print(f"{'─' * 70}")
    sa = report.get("skip_analysis", {})
    print(f"  Skip rate: {sa.get('skip_rate_pct', 0):.1f}%")
    for reason, count in list(sa.get("top_skip_reasons", {}).items())[:5]:
        print(f"    {reason}: {count}")

    # Recommendations
    print(f"\n{'─' * 70}")
    print(" H. RECOMMENDATIONS")
    print(f"{'─' * 70}")
    for i, rec in enumerate(report.get("recommendations", []), 1):
        print(f"  {i}. {rec}")

    # Alerts
    alerts = report.get("alerts", [])
    if alerts:
        print(f"\n{'─' * 70}")
        print(" I. ALERTS")
        print(f"{'─' * 70}")
        for alert in alerts:
            print(f"  ⚠ {alert}")

    print(f"\n{'=' * 70}")
    print(" END OF REPORT")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Performance Analytics Engine")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to database")
    parser.add_argument("--min-sample", type=int, default=MIN_SAMPLE, help="Min sample for recommendations")
    args = parser.parse_args()

    report = run_full_report(args.db, args.min_sample)
    print_report(report)


if __name__ == "__main__":
    main()
