#!/usr/bin/env python3
"""
Fetch Trump approval ratings (Civiqs daily tracking) + U.S. gas prices (EIA).

Approval source: civiqs.com/results/approve_president_trump_2025
  Approval data is decoded from the embedded SVG chart.
  Parties available: All Adults, Democrats, Republicans, Independents.
  Daily resolution back to Jan 20, 2025.

Gas source: EIA API v2, series EMM_EPMRR_PTE_NUS_DPG (weekly retail regular).

Usage:
    python fetch_data.py

Set EIA_API_KEY env var for production (free key: https://www.eia.gov/opendata/register.php).
Falls back to DEMO_KEY (rate-limited but functional).
"""
from __future__ import annotations

import json
import os
import re
import sys
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

EIA_API_KEY = os.environ.get("EIA_API_KEY", "DEMO_KEY")

TRUMP_TERM2_START = date(2025, 1, 20)  # Second inauguration
CHART_WIDTH = 965                       # Civiqs SVG chart area width  (px)
CHART_HEIGHT = 315                      # Civiqs SVG chart area height (px)
# Coordinate calibration: y=0 → 100% approval, y=CHART_HEIGHT → 0% approval
# Verified: y=195.3 → 38% (matches meta "Approve 38%" for March 25, 2026)

CIVIQS_BASE = "https://civiqs.com/results/approve_president_trump_2025"
PARTIES = {
    "all":         "",
    "democrat":    "&party=Democrat",
    "republican":  "&party=Republican",
    "independent": "&party=Independent",
}

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


# ─── Civiqs SVG decoder ───────────────────────────────────────────────────────

def _parse_bezier_path(path_d: str) -> list[tuple[float, float]]:
    """
    Extract (x, y) anchor-point coordinates from an SVG cubic-bezier path string.
    The path uses M / L (move/line) and C (cubic bezier) commands without spaces
    between the command letter and its arguments (e.g. 'M0,173.25L0.375,173.25C…').
    """
    spaced = re.sub(r"([MLCmlc])", r" \1 ", path_d)
    tokens = spaced.split()
    points: list[tuple[float, float]] = []
    i = 0
    while i < len(tokens):
        cmd = tokens[i]
        if cmd in ("M", "L"):
            if i + 1 < len(tokens):
                parts = tokens[i + 1].split(",")
                if len(parts) >= 2:
                    points.append((float(parts[0]), float(parts[1])))
            i += 2
        elif cmd == "C":
            # Cubic bezier: cx1,cy1 cx2,cy2 x,y  ← we want the endpoint x,y
            if i + 3 < len(tokens):
                parts = tokens[i + 3].split(",")
                if len(parts) >= 2:
                    points.append((float(parts[0]), float(parts[1])))
            i += 4
        else:
            i += 1
    return points


def _decode_series(path_d: str, end_date: date) -> list[tuple[str, float]]:
    """Convert an SVG path → [(date_str, approval_pct), …]."""
    pts = _parse_bezier_path(path_d)
    if not pts:
        return []
    total_days = (end_date - TRUMP_TERM2_START).days
    x_scale = total_days / CHART_WIDTH   # days per SVG pixel
    result = []
    for x, y in pts:
        d = TRUMP_TERM2_START + timedelta(days=round(x * x_scale))
        pct = round((CHART_HEIGHT - y) / CHART_HEIGHT * 100, 2)
        result.append((d.isoformat(), pct))
    return result


def _fetch_party(party_key: str, suffix: str) -> dict[str, dict]:
    """
    Returns {date_str: {"approve": float, "disapprove": float, "net": float}}
    for one party subgroup.
    """
    url = f"{CIVIQS_BASE}?annotations=true{suffix}"
    r = _get(url, f"Civiqs [{party_key}]")
    if r is None:
        return {}

    # Determine latest date from the meta description
    meta = re.search(r"Results through (\w+ \d+, \d{4}):", r.text[:5000])
    end_date = date.today()
    if meta:
        try:
            end_date = datetime.strptime(meta.group(1), "%B %d, %Y").date()
        except ValueError:
            pass

    # Locate the 1000×350 chart SVG (attribute order may vary)
    svg_match = (
        re.search(
            r'<svg[^>]*width=["\']1000["\'][^>]*height=["\']350["\'][^>]*>(.*?)</svg>',
            r.text, re.DOTALL,
        )
        or re.search(
            r'<svg[^>]*height=["\']350["\'][^>]*width=["\']1000["\'][^>]*>(.*?)</svg>',
            r.text, re.DOTALL,
        )
    )
    if not svg_match:
        print(f"  ERROR: chart SVG not found for [{party_key}]")
        return {}

    svg = svg_match.group(1)
    trendlines = re.findall(
        r'data-testid=["\']timeseries-line["\'][^>]*d=["\']([^"\']+)["\']', svg
    )
    if len(trendlines) < 2:
        print(f"  WARNING: only {len(trendlines)} trendlines for [{party_key}]")
        return {}

    approve_series    = _decode_series(trendlines[0], end_date)
    disapprove_series = _decode_series(trendlines[1], end_date)

    result: dict[str, dict] = {}
    for (d, app), (_, dis) in zip(approve_series, disapprove_series):
        result[d] = {
            "approve":    app,
            "disapprove": dis,
            "net":        round(app - dis, 2),
        }

    print(f"  {len(result)} pts  latest={max(result)} approve={result[max(result)]['approve']}%")
    return result


def fetch_approval() -> dict[str, dict]:
    """Fetch all-party approval from Civiqs SVG data."""
    print("Fetching approval data from Civiqs…")

    party_series: dict[str, dict] = {}
    for party_key, suffix in PARTIES.items():
        series = _fetch_party(party_key, suffix)
        if series:
            party_series[party_key] = series

    if not party_series:
        return {}

    # Combine into {date: {party_key: {approve, disapprove, net}}}
    all_dates = sorted({d for s in party_series.values() for d in s})
    combined: dict[str, dict] = {}
    for d in all_dates:
        entry = {pk: s[d] for pk, s in party_series.items() if d in s}
        if entry:
            combined[d] = entry

    print(f"  Combined: {len(combined)} dates × {len(party_series)} parties")
    return combined


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

    # ── Approval ────────────────────────────────────────────────────────────
    approval = fetch_approval()
    if approval:
        (DATA_DIR / "approval.json").write_text(json.dumps({
            "updated": datetime.utcnow().isoformat() + "Z",
            "source":  "Civiqs daily tracking poll (civiqs.com)",
            "note":    "Decoded from embedded SVG chart. Parties: all, democrat, republican, independent.",
            "data":    approval,
        }, indent=2))
    else:
        failed.append("approval")
        if not (DATA_DIR / "approval.json").exists():
            print("FATAL: no approval data and no cache.", file=sys.stderr)
            sys.exit(1)
        print("  Using cached approval.json")

    # ── Gas prices ──────────────────────────────────────────────────────────
    try:
        gas = fetch_gas_prices()
        (DATA_DIR / "gas_prices.json").write_text(json.dumps({
            "updated": datetime.utcnow().isoformat() + "Z",
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
