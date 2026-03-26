#!/usr/bin/env python3
"""
Fetch Trump approval + generic ballot (Nate Silver / 538) + U.S. gas prices (EIA).

Approval source:  https://natesilver.net  (Google Sheets CSV)
Generic ballot:   https://natesilver.net  (Google Sheets CSV)
Gas source: EIA API v2, series EMM_EPMRR_PTE_NUS_DPG (weekly retail regular).

Usage:
    python fetch_data.py

Set EIA_API_KEY env var for production (free key: https://www.eia.gov/opendata/register.php).
Falls back to DEMO_KEY (rate-limited but functional).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

EIA_API_KEY = os.environ.get("EIA_API_KEY", "DEMO_KEY")

APPROVAL_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vS-FKWVTTFtJT6u56e0bqdfoMcXvDO1DUChsJ3jQAMB2lZk2SMqVfmg7dGjclTYkYWz-Pm5lfcLPjp4"
    "/pub?output=csv"
)
GENERIC_BALLOT_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRsvXNCZ0ubJr8D_yNcU5q6C0_HBa35K7oDK03KpO7Ca43UwdXaIdvVLWoXEmHHph0EREz5430Hm5yZ"
    "/pub?output=csv"
)

ROLLING_WINDOW = 28  # days

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _get(url: str, label: str = "") -> requests.Response | None:
    try:
        r = requests.get(url, timeout=35, headers={"User-Agent": UA})
        r.raise_for_status()
        print(f"  ✓ {label or url[:70]}")
        return r
    except Exception as e:
        print(f"  ✗ {label or url[:70]}: {e}")
        return None


# ─── Rolling average ──────────────────────────────────────────────────────────

def _weighted_rolling_avg(
    polls: list[tuple[date, float, float]],
    window: int = ROLLING_WINDOW,
) -> dict[str, float]:
    """
    Given a list of (poll_end_date, value, weight), compute a daily weighted
    rolling average over `window` days and return {date_str: avg}.
    """
    if not polls:
        return {}
    polls.sort(key=lambda x: x[0])
    min_date = polls[0][0]
    max_date = polls[-1][0]

    result: dict[str, float] = {}
    current = min_date
    while current <= max_date:
        cutoff = current - timedelta(days=window - 1)
        in_window = [(v, w) for d, v, w in polls if cutoff <= d <= current]
        if in_window:
            total_w = sum(w for _, w in in_window)
            if total_w > 0:
                avg = sum(v * w for v, w in in_window) / total_w
                result[current.isoformat()] = round(avg, 2)
        current += timedelta(days=1)
    return result


def _parse_polls(
    text: str,
    value_col: str,
    subgroup: str = "All polls",
) -> list[tuple[date, float, float]]:
    """Parse a Nate Silver CSV and return (enddate, adjusted_value, weight) tuples."""
    reader = csv.DictReader(io.StringIO(text))
    polls: list[tuple[date, float, float]] = []
    for row in reader:
        if row.get("subgroup", "").strip() != subgroup:
            continue
        try:
            d = datetime.strptime(row["enddate"].strip(), "%m/%d/%Y").date()
            v = float(row[value_col].strip())
            w = float(row.get("weight", "1").strip() or "1")
            polls.append((d, v, w))
        except (ValueError, KeyError):
            continue
    return polls


# ─── Approval ─────────────────────────────────────────────────────────────────

def fetch_approval() -> dict[str, float]:
    print("Fetching Trump approval from Nate Silver…")
    r = _get(APPROVAL_CSV, "Approval CSV")
    if r is None:
        raise RuntimeError("Approval CSV fetch failed")
    polls = _parse_polls(r.text, "adjusted_net")
    if not polls:
        raise RuntimeError("No approval polls parsed")
    result = _weighted_rolling_avg(polls)
    print(f"  {len(polls)} polls → {len(result)} daily pts  latest={max(result)} net={result[max(result)]:+.2f}%")
    return result


# ─── Generic ballot ───────────────────────────────────────────────────────────

def fetch_generic_ballot() -> dict[str, float]:
    print("Fetching generic ballot from Nate Silver…")
    r = _get(GENERIC_BALLOT_CSV, "Generic ballot CSV")
    if r is None:
        raise RuntimeError("Generic ballot CSV fetch failed")
    polls = _parse_polls(r.text, "adjusted_net")
    if not polls:
        raise RuntimeError("No generic ballot polls parsed")
    result = _weighted_rolling_avg(polls)
    print(f"  {len(polls)} polls → {len(result)} daily pts  latest={max(result)} D-R={result[max(result)]:+.2f}%")
    return result


# ─── EIA gas prices ───────────────────────────────────────────────────────────

def fetch_gas_prices() -> dict[str, float]:
    print("Fetching gas prices from EIA…")
    url = (
        "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
        f"?api_key={EIA_API_KEY}"
        "&frequency=weekly"
        "&data[0]=value"
        "&facets[series][]=EMM_EPMRR_PTE_NUS_DPG"
        "&sort[0][column]=period"
        "&sort[0][direction]=desc"
        "&length=520"
    )
    r = _get(url, "EIA API")
    if r is None:
        raise RuntimeError("EIA API failed")
    payload = r.json()
    prices: dict[str, float] = {}
    for item in payload.get("response", {}).get("data", []):
        prices[item["period"]] = round(float(item["value"]), 3)
    print(f"  {len(prices)} weekly observations  latest={max(prices)} ${prices[max(prices)]}")
    return prices


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    failed: list[str] = []
    now = datetime.utcnow().isoformat() + "Z"

    # ── Approval ────────────────────────────────────────────────────────────
    try:
        approval = fetch_approval()
        (DATA_DIR / "approval.json").write_text(json.dumps({
            "updated": now,
            "source":  "Nate Silver / 538 (natesilver.net) — 28-day weighted rolling avg",
            "unit":    "net approval (approve − disapprove), %",
            "data":    approval,
        }, indent=2))
    except Exception as e:
        failed.append("approval")
        print(f"  Approval error: {e}", file=sys.stderr)
        if not (DATA_DIR / "approval.json").exists():
            print("FATAL: no approval data and no cache.", file=sys.stderr)
            sys.exit(1)
        print("  Using cached approval.json")

    # ── Generic ballot ──────────────────────────────────────────────────────
    try:
        generic = fetch_generic_ballot()
        (DATA_DIR / "generic_ballot.json").write_text(json.dumps({
            "updated": now,
            "source":  "Nate Silver / 538 (natesilver.net) — 28-day weighted rolling avg",
            "unit":    "generic ballot net (D − R), %",
            "data":    generic,
        }, indent=2))
    except Exception as e:
        failed.append("generic_ballot")
        print(f"  Generic ballot error: {e}", file=sys.stderr)
        if not (DATA_DIR / "generic_ballot.json").exists():
            print("FATAL: no generic ballot data and no cache.", file=sys.stderr)
            sys.exit(1)
        print("  Using cached generic_ballot.json")

    # ── Gas prices ──────────────────────────────────────────────────────────
    try:
        gas = fetch_gas_prices()
        (DATA_DIR / "gas_prices.json").write_text(json.dumps({
            "updated": now,
            "source":  "EIA (EMM_EPMRR_PTE_NUS_DPG)",
            "unit":    "USD per gallon",
            "data":    gas,
        }, indent=2))
    except Exception as e:
        failed.append("gas_prices")
        print(f"  Gas error: {e}", file=sys.stderr)
        if not (DATA_DIR / "gas_prices.json").exists():
            print("FATAL: no gas data and no cache.", file=sys.stderr)
            sys.exit(1)
        print("  Using cached gas_prices.json")

    if failed:
        print(f"\nWarning: could not refresh: {failed}")
    else:
        print("\nAll data refreshed successfully!")


if __name__ == "__main__":
    main()
