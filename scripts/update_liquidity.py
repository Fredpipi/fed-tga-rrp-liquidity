#!/usr/bin/env python3
"""Update Fed/TGA/RRP liquidity data.

Indicator:
    net_liquidity_usd_mn = fed_total_assets - tga - rrp

All values are stored in USD millions. WALCL and RRPONTSYD are fetched from
FRED's public CSV endpoint; TGA is fetched from Treasury FiscalData.
"""

from __future__ import annotations

import csv
import html
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
TREASURY_TGA = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/"
    "accounting/dts/operating_cash_balance"
)

SOURCES = {
    "fed_total_assets": {
        "series": "WALCL",
        "bookmark": "https://www.federalreserve.gov/Releases/H41/default.htm",
        "machine_url": FRED_CSV.format(series_id="WALCL"),
    },
    "tga": {
        "bookmark": "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/",
        "machine_url": TREASURY_TGA,
    },
    "rrp": {
        "series": "RRPONTSYD",
        "bookmark": "https://www.newyorkfed.org/markets/desk-operations/reverse-repo",
        "machine_url": FRED_CSV.format(series_id="RRPONTSYD"),
    },
    "sp500": {
        "series": "SP500",
        "machine_url": FRED_CSV.format(series_id="SP500"),
    },
    "nasdaq": {
        "series": "NASDAQCOM",
        "machine_url": FRED_CSV.format(series_id="NASDAQCOM"),
    },
}


@dataclass(frozen=True)
class Point:
    record_date: date
    value: float


def get_text(url: str) -> str:
    last_error: subprocess.CalledProcessError | None = None
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["curl", "-g", "-L", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "25", url],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(3 * (attempt + 1))
    assert last_error is not None
    raise last_error


def fetch_fred(series_id: str, start_date: str = "2022-01-01") -> list[Point]:
    url = FRED_CSV.format(series_id=series_id) + "&" + urllib.parse.urlencode({"cosd": start_date})
    text = get_text(url)
    rows = csv.DictReader(text.splitlines())
    out: list[Point] = []
    for row in rows:
        raw = row.get(series_id, "")
        if not raw or raw == ".":
            continue
        out.append(Point(datetime.strptime(row["observation_date"], "%Y-%m-%d").date(), float(raw)))
    return out


def fetch_tga() -> list[Point]:
    out: list[Point] = []
    page = 1
    while True:
        params = {
            "sort": "record_date",
            "filter": "record_date:gte:2022-01-01,account_type:eq:Treasury General Account (TGA) Closing Balance",
            "fields": "record_date,open_today_bal",
            "page[size]": "1000",
            "page[number]": str(page),
        }
        url = TREASURY_TGA + "?" + urllib.parse.urlencode(params)
        payload = json.loads(get_text(url))
        for row in payload["data"]:
            raw = row.get("open_today_bal")
            if not raw or raw == "null":
                continue
            out.append(Point(datetime.strptime(row["record_date"], "%Y-%m-%d").date(), float(raw)))
        meta = payload.get("meta", {})
        if page >= int(meta.get("total-pages", page)):
            break
        page += 1
    return out


def write_series(name: str, points: list[Point]) -> None:
    with (DATA_DIR / f"{name}.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", f"{name}_usd_mn"])
        for point in points:
            writer.writerow([point.record_date.isoformat(), f"{point.value:.3f}"])


def write_index_series(name: str, points: list[Point]) -> None:
    with (DATA_DIR / f"{name}.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", name])
        for point in points:
            writer.writerow([point.record_date.isoformat(), f"{point.value:.3f}"])


def as_map(points: list[Point]) -> dict[date, float]:
    return {point.record_date: point.value for point in points}


def latest_on_or_before(values: dict[date, float], target: date) -> tuple[date, float] | None:
    candidates = [d for d in values if d <= target]
    if not candidates:
        return None
    latest_date = max(candidates)
    return latest_date, values[latest_date]


def build_daily_history(fed: list[Point], tga: list[Point], rrp: list[Point]) -> list[dict[str, str]]:
    fed_map, tga_map, rrp_map = as_map(fed), as_map(tga), as_map(rrp)
    start = max(min(fed_map), min(tga_map), min(rrp_map))
    end = max(max(fed_map), max(tga_map), max(rrp_map))
    all_dates = sorted(d for d in set(fed_map) | set(tga_map) | set(rrp_map) if start <= d <= end)

    rows: list[dict[str, str]] = []
    for current in all_dates:
        fed_latest = latest_on_or_before(fed_map, current)
        tga_latest = latest_on_or_before(tga_map, current)
        rrp_latest = latest_on_or_before(rrp_map, current)
        if not (fed_latest and tga_latest and rrp_latest):
            continue
        fed_date, fed_value = fed_latest
        tga_date, tga_value = tga_latest
        rrp_date, rrp_value = rrp_latest
        net = fed_value - tga_value - rrp_value
        rows.append(
            {
                "date": current.isoformat(),
                "net_liquidity_usd_mn": f"{net:.3f}",
                "fed_total_assets_usd_mn": f"{fed_value:.3f}",
                "fed_total_assets_date": fed_date.isoformat(),
                "tga_usd_mn": f"{tga_value:.3f}",
                "tga_date": tga_date.isoformat(),
                "rrp_usd_mn": f"{rrp_value:.3f}",
                "rrp_date": rrp_date.isoformat(),
            }
        )
    return rows


def write_daily_history(rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "net_liquidity_usd_mn",
        "fed_total_assets_usd_mn",
        "fed_total_assets_date",
        "tga_usd_mn",
        "tga_date",
        "rrp_usd_mn",
        "rrp_date",
    ]
    with (DATA_DIR / "liquidity_history.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def latest_snapshot(rows: list[dict[str, str]]) -> dict[str, object]:
    latest = rows[-1]
    net = float(latest["net_liquidity_usd_mn"])
    previous = rows[-2] if len(rows) > 1 else latest
    previous_net = float(previous["net_liquidity_usd_mn"])

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "indicator": "Fed total assets - TGA - ON RRP",
        "unit": "USD millions",
        "latest": latest,
        "change_from_previous_observation_usd_mn": round(net - previous_net, 3),
        "sources": SOURCES,
    }


def write_report(snapshot: dict[str, object]) -> None:
    latest = snapshot["latest"]
    assert isinstance(latest, dict)
    net = float(latest["net_liquidity_usd_mn"])
    change = float(snapshot["change_from_previous_observation_usd_mn"])
    chart_lines = [
        "- reports/liquidity_chart.svg",
        "- reports/liquidity_chart.svg.png",
    ]
    if (REPORT_DIR / "liquidity_vs_markets_chart.svg").exists():
        chart_lines.extend(
            [
                "- reports/liquidity_vs_markets_chart.svg",
                "- reports/liquidity_vs_markets_chart.svg.png",
            ]
        )

    lines = [
        "# Liquidity Indicator",
        "",
        f"Updated at: {snapshot['updated_at']}",
        "",
        f"Net liquidity: {net:,.0f} USD mn ({net / 1_000_000:.3f} USD tn)",
        f"Change from previous observation: {change:,.0f} USD mn",
        "",
        "Formula: Fed total assets - TGA - ON RRP",
        "",
        "| Component | Value, USD mn | Data date |",
        "| --- | ---: | --- |",
        f"| Fed total assets | {float(latest['fed_total_assets_usd_mn']):,.0f} | {latest['fed_total_assets_date']} |",
        f"| TGA | {float(latest['tga_usd_mn']):,.0f} | {latest['tga_date']} |",
        f"| ON RRP | {float(latest['rrp_usd_mn']):,.0f} | {latest['rrp_date']} |",
        "",
        "Sources:",
        f"- H.4.1: {SOURCES['fed_total_assets']['bookmark']}",
        f"- TGA: {SOURCES['tga']['bookmark']}",
        f"- RRP: {SOURCES['rrp']['bookmark']}",
        "",
        "Chart:",
        *chart_lines,
        "",
    ]
    (REPORT_DIR / "liquidity_latest.md").write_text("\n".join(lines), encoding="utf-8")


def write_pages_index(snapshot: dict[str, object]) -> None:
    latest = snapshot["latest"]
    assert isinstance(latest, dict)
    net = float(latest["net_liquidity_usd_mn"])
    updated = str(snapshot["updated_at"]).replace("+00:00", "Z")
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fed/TGA/RRP Liquidity</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f3ea;
      --ink: #1f2933;
      --muted: #6b7280;
      --line: #d8d2c4;
      --green: #167c6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 28px auto 48px;
    }}
    header {{
      display: flex;
      gap: 18px;
      align-items: flex-end;
      justify-content: space-between;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
      margin-bottom: 22px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(26px, 4vw, 42px);
      line-height: 1.05;
      letter-spacing: 0;
    }}
    .meta {{
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    .metric {{
      text-align: right;
      min-width: 230px;
    }}
    .metric strong {{
      display: block;
      color: var(--green);
      font-size: clamp(28px, 5vw, 46px);
      line-height: 1;
    }}
    .metric span {{
      color: var(--muted);
      font-size: 14px;
    }}
    img {{
      display: block;
      width: 100%;
      height: auto;
      border: 1px solid var(--line);
      background: var(--bg);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 22px;
      font-size: 15px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 12px 8px;
      text-align: left;
    }}
    td:nth-child(2), th:nth-child(2) {{ text-align: right; }}
    a {{ color: var(--green); }}
    @media (max-width: 720px) {{
      header {{ display: block; }}
      .metric {{ text-align: left; margin-top: 18px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Fed/TGA/RRP Liquidity</h1>
        <p class="meta">Net liquidity = Fed total assets - TGA - ON RRP<br>Updated: {html.escape(updated)}</p>
      </div>
      <div class="metric">
        <strong>{net / 1_000_000:.3f}T</strong>
        <span>USD net liquidity</span>
      </div>
    </header>

    <img src="reports/liquidity_chart.svg" alt="Fed TGA RRP liquidity chart">

    <table>
      <thead>
        <tr><th>Component</th><th>Value, USD mn</th><th>Data date</th></tr>
      </thead>
      <tbody>
        <tr><td>Fed total assets</td><td>{float(latest['fed_total_assets_usd_mn']):,.0f}</td><td>{html.escape(latest['fed_total_assets_date'])}</td></tr>
        <tr><td>TGA</td><td>{float(latest['tga_usd_mn']):,.0f}</td><td>{html.escape(latest['tga_date'])}</td></tr>
        <tr><td>ON RRP</td><td>{float(latest['rrp_usd_mn']):,.0f}</td><td>{html.escape(latest['rrp_date'])}</td></tr>
      </tbody>
    </table>

    <p class="meta">Data files: <a href="data/liquidity_latest.json">latest JSON</a> · <a href="data/liquidity_history.csv">history CSV</a></p>
  </main>
</body>
</html>
"""
    (ROOT / "index.html").write_text(html_text, encoding="utf-8")


def scale(value: float, low: float, high: float, size: float, reverse: bool = False) -> float:
    if high == low:
        return size / 2
    pos = (value - low) / (high - low) * size
    return size - pos if reverse else pos


def nice_bounds(values: list[float]) -> tuple[float, float]:
    low = min(values)
    high = max(values)
    pad = (high - low) * 0.08 if high != low else abs(high) * 0.05 or 1
    return low - pad, high + pad


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def x_for_date(current: date, start: date, end: date, x0: int, width: int) -> float:
    total_days = max((end - start).days, 1)
    return x0 + ((current - start).days / total_days) * width


def polyline(rows: list[dict[str, str]], key: str, x0: int, y0: int, width: int, height: int) -> str:
    values = [float(row[key]) / 1_000_000 for row in rows]
    low, high = nice_bounds(values)
    start_date = parse_iso_date(rows[0]["date"])
    end_date = parse_iso_date(rows[-1]["date"])
    points = []
    for row in rows:
        x = x_for_date(parse_iso_date(row["date"]), start_date, end_date, x0, width)
        y = y0 + scale(float(row[key]) / 1_000_000, low, high, height, reverse=True)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def axis_labels(rows: list[dict[str, str]], key: str) -> tuple[str, str]:
    values = [float(row[key]) / 1_000_000 for row in rows]
    low, high = nice_bounds(values)
    return f"{high:.2f}T", f"{low:.2f}T"


def add_months(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def date_ticks(start: date, end: date) -> list[date]:
    span_days = max((end - start).days, 1)
    if span_days <= 95:
        step_months = 1
    elif span_days <= 460:
        step_months = 1
    elif span_days <= 900:
        step_months = 3
    else:
        step_months = 6

    tick = date(start.year, start.month, 1)
    if tick < start:
        tick = add_months(tick, 1)

    ticks = [start]
    while tick < end:
        ticks.append(tick)
        tick = add_months(tick, step_months)
    if ticks[-1] != end:
        ticks.append(end)
    return ticks


def write_chart(rows: list[dict[str, str]], snapshot: dict[str, object]) -> None:
    chart_rows = rows[-260:] if len(rows) > 260 else rows
    latest = snapshot["latest"]
    assert isinstance(latest, dict)

    width, height = 1280, 760
    x0, y0, plot_w, plot_h = 90, 145, 1120, 275
    x1, y1, plot2_w, plot2_h = 90, 500, 1120, 165
    bg = "#f7f3ea"
    ink = "#1f2933"
    muted = "#6b7280"
    grid = "#d8d2c4"
    green = "#167c6b"
    blue = "#2f6fbb"
    amber = "#b7791f"
    red = "#b83242"

    net_high, net_low = axis_labels(chart_rows, "net_liquidity_usd_mn")
    component_keys = ("fed_total_assets_usd_mn", "tga_usd_mn", "rrp_usd_mn")
    comp_bases = {
        key: next(float(row[key]) for row in chart_rows if float(row[key]) != 0)
        for key in component_keys
    }
    comp_values = [
        float(row[key]) / comp_bases[key] * 100
        for row in chart_rows
        for key in component_keys
    ]
    comp_low, comp_high = nice_bounds(comp_values)

    def comp_poly(key: str) -> str:
        points = []
        start_date = parse_iso_date(chart_rows[0]["date"])
        end_date = parse_iso_date(chart_rows[-1]["date"])
        for row in chart_rows:
            x = x_for_date(parse_iso_date(row["date"]), start_date, end_date, x1, plot_w)
            indexed = float(row[key]) / comp_bases[key] * 100
            y = y1 + scale(indexed, comp_low, comp_high, plot2_h, reverse=True)
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)

    start = chart_rows[0]["date"]
    end = chart_rows[-1]["date"]
    start_date = parse_iso_date(start)
    end_date = parse_iso_date(end)
    net = float(latest["net_liquidity_usd_mn"]) / 1_000_000
    updated = str(snapshot["updated_at"]).replace("+00:00", "Z")

    grid_lines = []
    for frac in (0, 0.25, 0.5, 0.75, 1):
        y = y0 + frac * plot_h
        grid_lines.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + plot_w}" y2="{y:.1f}" stroke="{grid}" stroke-width="1"/>')
    for frac in (0, 0.5, 1):
        y = y1 + frac * plot2_h
        grid_lines.append(f'<line x1="{x1}" y1="{y:.1f}" x2="{x1 + plot_w}" y2="{y:.1f}" stroke="{grid}" stroke-width="1"/>')

    tick_marks = []
    ticks = date_ticks(start_date, end_date)
    for tick in ticks:
        x_top = x_for_date(tick, start_date, end_date, x0, plot_w)
        x_bottom = x_for_date(tick, start_date, end_date, x1, plot_w)
        label = tick.strftime("%Y-%m-%d") if tick in (start_date, end_date) else tick.strftime("%Y-%m")
        tick_marks.extend(
            [
                f'<line x1="{x_top:.1f}" y1="{y0}" x2="{x_top:.1f}" y2="{y0 + plot_h}" stroke="{grid}" stroke-width="1"/>',
                f'<line x1="{x_bottom:.1f}" y1="{y1}" x2="{x_bottom:.1f}" y2="{y1 + plot2_h}" stroke="{grid}" stroke-width="1"/>',
                f'<line x1="{x_top:.1f}" y1="{y0 + plot_h}" x2="{x_top:.1f}" y2="{y0 + plot_h + 7}" stroke="#9b9383" stroke-width="1"/>',
                f'<text x="{x_top:.1f}" y="{y0 + plot_h + 25}" font-family="Arial, Helvetica, sans-serif" font-size="12" fill="{muted}" text-anchor="middle">{html.escape(label)}</text>',
            ]
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="{bg}"/>
  <text x="60" y="58" font-family="Arial, Helvetica, sans-serif" font-size="30" font-weight="700" fill="{ink}">Fed / TGA / ON RRP Liquidity</text>
  <text x="60" y="91" font-family="Arial, Helvetica, sans-serif" font-size="18" fill="{muted}">Net liquidity in USD trillions; components indexed to compare trends.</text>
  <text x="60" y="121" font-family="Arial, Helvetica, sans-serif" font-size="18" fill="{green}" font-weight="700">Latest: {net:.3f}T</text>
  <text x="210" y="121" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{muted}">date range: {html.escape(start)} to {html.escape(end)} | updated: {html.escape(updated)}</text>

  {''.join(grid_lines)}
  {''.join(tick_marks)}
  <rect x="{x0}" y="{y0}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#b8b0a0" stroke-width="1"/>
  <text x="30" y="{y0 + 6}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{net_high}</text>
  <text x="30" y="{y0 + plot_h}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{net_low}</text>
  <text x="{x0}" y="{y0 - 18}" font-family="Arial, Helvetica, sans-serif" font-size="16" font-weight="700" fill="{ink}">Net liquidity</text>
  <polyline fill="none" stroke="{green}" stroke-width="4" stroke-linejoin="round" stroke-linecap="round" points="{polyline(chart_rows, 'net_liquidity_usd_mn', x0, y0, plot_w, plot_h)}"/>

  <rect x="{x1}" y="{y1}" width="{plot_w}" height="{plot2_h}" fill="none" stroke="#b8b0a0" stroke-width="1"/>
  <text x="{x1}" y="{y1 - 20}" font-family="Arial, Helvetica, sans-serif" font-size="16" font-weight="700" fill="{ink}">Components trend (start = 100)</text>
  <polyline fill="none" stroke="{blue}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{comp_poly('fed_total_assets_usd_mn')}"/>
  <polyline fill="none" stroke="{amber}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{comp_poly('tga_usd_mn')}"/>
  <polyline fill="none" stroke="{red}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{comp_poly('rrp_usd_mn')}"/>
  <text x="30" y="{y1 + 6}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{comp_high:.1f}</text>
  <text x="30" y="{y1 + plot2_h}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{comp_low:.1f}</text>
  <circle cx="875" cy="466" r="6" fill="{blue}"/><text x="890" y="471" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">Fed assets</text>
  <circle cx="985" cy="466" r="6" fill="{amber}"/><text x="1000" y="471" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">TGA</text>
  <circle cx="1055" cy="466" r="6" fill="{red}"/><text x="1070" y="471" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">ON RRP</text>

  <text x="60" y="714" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">Lower chart indexes each component to 100 on {html.escape(start)}. Latest component dates: Fed {html.escape(latest['fed_total_assets_date'])}, TGA {html.escape(latest['tga_date'])}, RRP {html.escape(latest['rrp_date'])}.</text>
  <text x="60" y="736" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">Source pages: Federal Reserve H.4.1, Treasury DTS, New York Fed reverse repo operations.</text>
</svg>
'''
    svg_path = REPORT_DIR / "liquidity_chart.svg"
    png_path = REPORT_DIR / "liquidity_chart.svg.png"
    svg_path.write_text(svg, encoding="utf-8")

    try:
        subprocess.run(
            ["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        try:
            subprocess.run(
                ["qlmanage", "-t", "-s", "1280", "-o", str(REPORT_DIR), str(svg_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            if png_path.exists():
                png_path.unlink()


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0
    return (current / previous) - 1


def corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    if x_var == 0 or y_var == 0:
        return None
    return cov / ((x_var * y_var) ** 0.5)


def build_market_history(
    liquidity_rows: list[dict[str, str]], sp500: list[Point], nasdaq: list[Point]
) -> list[dict[str, str]]:
    sp_map = as_map(sp500)
    ndx_map = as_map(nasdaq)
    out: list[dict[str, str]] = []
    for row in liquidity_rows:
        current = parse_iso_date(row["date"])
        sp_latest = latest_on_or_before(sp_map, current)
        ndx_latest = latest_on_or_before(ndx_map, current)
        if not (sp_latest and ndx_latest):
            continue
        sp_date, sp_value = sp_latest
        ndx_date, ndx_value = ndx_latest
        out.append(
            {
                "date": row["date"],
                "net_liquidity_usd_mn": row["net_liquidity_usd_mn"],
                "sp500": f"{sp_value:.3f}",
                "sp500_date": sp_date.isoformat(),
                "nasdaq": f"{ndx_value:.3f}",
                "nasdaq_date": ndx_date.isoformat(),
            }
        )

    for i, row in enumerate(out):
        if i == 0:
            row["liquidity_return"] = ""
            row["sp500_return"] = ""
            row["nasdaq_return"] = ""
            row["corr_60d_sp500"] = ""
            row["corr_60d_nasdaq"] = ""
            continue

        prev = out[i - 1]
        row["liquidity_return"] = f"{pct_change(float(row['net_liquidity_usd_mn']), float(prev['net_liquidity_usd_mn'])):.8f}"
        row["sp500_return"] = f"{pct_change(float(row['sp500']), float(prev['sp500'])):.8f}"
        row["nasdaq_return"] = f"{pct_change(float(row['nasdaq']), float(prev['nasdaq'])):.8f}"

        window = out[max(1, i - 59) : i + 1]
        liq_returns = [float(item["liquidity_return"]) for item in window if item["liquidity_return"]]
        sp_returns = [float(item["sp500_return"]) for item in window if item["sp500_return"]]
        ndx_returns = [float(item["nasdaq_return"]) for item in window if item["nasdaq_return"]]
        sp_corr = corr(liq_returns, sp_returns)
        ndx_corr = corr(liq_returns, ndx_returns)
        row["corr_60d_sp500"] = "" if sp_corr is None else f"{sp_corr:.4f}"
        row["corr_60d_nasdaq"] = "" if ndx_corr is None else f"{ndx_corr:.4f}"
    return out


def write_market_history(rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "net_liquidity_usd_mn",
        "sp500",
        "sp500_date",
        "nasdaq",
        "nasdaq_date",
        "liquidity_return",
        "sp500_return",
        "nasdaq_return",
        "corr_60d_sp500",
        "corr_60d_nasdaq",
    ]
    with (DATA_DIR / "liquidity_vs_markets_history.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def normalized_polyline(
    rows: list[dict[str, str]],
    key: str,
    base_key: str,
    low: float,
    high: float,
    x0: int,
    y0: int,
    width: int,
    height: int,
) -> str:
    start_date = parse_iso_date(rows[0]["date"])
    end_date = parse_iso_date(rows[-1]["date"])
    base = float(rows[0][base_key])
    points = []
    for row in rows:
        indexed = float(row[key]) / base * 100
        x = x_for_date(parse_iso_date(row["date"]), start_date, end_date, x0, width)
        y = y0 + scale(indexed, low, high, height, reverse=True)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def corr_polyline(rows: list[dict[str, str]], key: str, x0: int, y0: int, width: int, height: int) -> str:
    valid = [row for row in rows if row.get(key)]
    if not valid:
        return ""
    start_date = parse_iso_date(rows[0]["date"])
    end_date = parse_iso_date(rows[-1]["date"])
    points = []
    for row in valid:
        x = x_for_date(parse_iso_date(row["date"]), start_date, end_date, x0, width)
        y = y0 + scale(float(row[key]), -1, 1, height, reverse=True)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def write_market_chart(rows: list[dict[str, str]], snapshot: dict[str, object]) -> None:
    chart_rows = rows[-260:] if len(rows) > 260 else rows
    if len(chart_rows) < 3:
        return

    width, height = 1280, 790
    x0, y0, plot_w, plot_h = 90, 145, 1120, 315
    x1, y1, corr_h = 90, 560, 1120, 125
    bg = "#f7f3ea"
    ink = "#1f2933"
    muted = "#6b7280"
    grid = "#d8d2c4"
    green = "#167c6b"
    blue = "#2f6fbb"
    purple = "#7c3aed"
    orange = "#c05621"

    indexed_values = []
    liq_base = float(chart_rows[0]["net_liquidity_usd_mn"])
    sp_base = float(chart_rows[0]["sp500"])
    ndx_base = float(chart_rows[0]["nasdaq"])
    for row in chart_rows:
        indexed_values.extend(
            [
                float(row["net_liquidity_usd_mn"]) / liq_base * 100,
                float(row["sp500"]) / sp_base * 100,
                float(row["nasdaq"]) / ndx_base * 100,
            ]
        )
    idx_low, idx_high = nice_bounds(indexed_values)
    start_date = parse_iso_date(chart_rows[0]["date"])
    end_date = parse_iso_date(chart_rows[-1]["date"])
    start = chart_rows[0]["date"]
    end = chart_rows[-1]["date"]
    latest = chart_rows[-1]
    updated = str(snapshot["updated_at"]).replace("+00:00", "Z")

    grid_lines = []
    for frac in (0, 0.25, 0.5, 0.75, 1):
        y = y0 + frac * plot_h
        grid_lines.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + plot_w}" y2="{y:.1f}" stroke="{grid}" stroke-width="1"/>')
    for frac in (0, 0.5, 1):
        y = y1 + frac * corr_h
        grid_lines.append(f'<line x1="{x1}" y1="{y:.1f}" x2="{x1 + plot_w}" y2="{y:.1f}" stroke="{grid}" stroke-width="1"/>')
    zero_y = y1 + scale(0, -1, 1, corr_h, reverse=True)
    grid_lines.append(f'<line x1="{x1}" y1="{zero_y:.1f}" x2="{x1 + plot_w}" y2="{zero_y:.1f}" stroke="#9b9383" stroke-width="1.5"/>')

    tick_marks = []
    for tick in date_ticks(start_date, end_date):
        x_top = x_for_date(tick, start_date, end_date, x0, plot_w)
        x_bottom = x_for_date(tick, start_date, end_date, x1, plot_w)
        label = tick.strftime("%Y-%m-%d") if tick in (start_date, end_date) else tick.strftime("%Y-%m")
        tick_marks.extend(
            [
                f'<line x1="{x_top:.1f}" y1="{y0}" x2="{x_top:.1f}" y2="{y0 + plot_h}" stroke="{grid}" stroke-width="1"/>',
                f'<line x1="{x_bottom:.1f}" y1="{y1}" x2="{x_bottom:.1f}" y2="{y1 + corr_h}" stroke="{grid}" stroke-width="1"/>',
                f'<line x1="{x_top:.1f}" y1="{y0 + plot_h}" x2="{x_top:.1f}" y2="{y0 + plot_h + 7}" stroke="#9b9383" stroke-width="1"/>',
                f'<text x="{x_top:.1f}" y="{y0 + plot_h + 25}" font-family="Arial, Helvetica, sans-serif" font-size="12" fill="{muted}" text-anchor="middle">{html.escape(label)}</text>',
            ]
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="{bg}"/>
  <text x="60" y="58" font-family="Arial, Helvetica, sans-serif" font-size="30" font-weight="700" fill="{ink}">Liquidity vs S&amp;P 500 / Nasdaq</text>
  <text x="60" y="91" font-family="Arial, Helvetica, sans-serif" font-size="18" fill="{muted}">Top panel: indexed to 100 at the first date. Bottom panel: rolling 60-observation correlation of daily changes.</text>
  <text x="60" y="121" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{muted}">date range: {html.escape(start)} to {html.escape(end)} | updated: {html.escape(updated)}</text>

  {''.join(grid_lines)}
  {''.join(tick_marks)}
  <rect x="{x0}" y="{y0}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#b8b0a0" stroke-width="1"/>
  <text x="28" y="{y0 + 6}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{idx_high:.0f}</text>
  <text x="28" y="{y0 + plot_h}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">{idx_low:.0f}</text>
  <text x="{x0}" y="{y0 - 18}" font-family="Arial, Helvetica, sans-serif" font-size="16" font-weight="700" fill="{ink}">Trend, indexed</text>
  <polyline fill="none" stroke="{green}" stroke-width="4" stroke-linejoin="round" stroke-linecap="round" points="{normalized_polyline(chart_rows, 'net_liquidity_usd_mn', 'net_liquidity_usd_mn', idx_low, idx_high, x0, y0, plot_w, plot_h)}"/>
  <polyline fill="none" stroke="{blue}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{normalized_polyline(chart_rows, 'sp500', 'sp500', idx_low, idx_high, x0, y0, plot_w, plot_h)}"/>
  <polyline fill="none" stroke="{purple}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{normalized_polyline(chart_rows, 'nasdaq', 'nasdaq', idx_low, idx_high, x0, y0, plot_w, plot_h)}"/>
  <circle cx="780" cy="116" r="6" fill="{green}"/><text x="795" y="121" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">Net liquidity</text>
  <circle cx="910" cy="116" r="6" fill="{blue}"/><text x="925" y="121" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">S&amp;P 500</text>
  <circle cx="1015" cy="116" r="6" fill="{purple}"/><text x="1030" y="121" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">Nasdaq</text>

  <rect x="{x1}" y="{y1}" width="{plot_w}" height="{corr_h}" fill="none" stroke="#b8b0a0" stroke-width="1"/>
  <text x="{x1}" y="{y1 - 18}" font-family="Arial, Helvetica, sans-serif" font-size="16" font-weight="700" fill="{ink}">Rolling correlation with liquidity changes</text>
  <text x="28" y="{y1 + 6}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">+1</text>
  <text x="35" y="{zero_y + 5:.1f}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">0</text>
  <text x="30" y="{y1 + corr_h}" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">-1</text>
  <polyline fill="none" stroke="{blue}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{corr_polyline(chart_rows, 'corr_60d_sp500', x1, y1, plot_w, corr_h)}"/>
  <polyline fill="none" stroke="{purple}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" points="{corr_polyline(chart_rows, 'corr_60d_nasdaq', x1, y1, plot_w, corr_h)}"/>
  <circle cx="920" cy="531" r="6" fill="{blue}"/><text x="935" y="536" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">S&amp;P corr</text>
  <circle cx="1025" cy="531" r="6" fill="{purple}"/><text x="1040" y="536" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="{ink}">Nasdaq corr</text>

  <text x="60" y="744" font-family="Arial, Helvetica, sans-serif" font-size="14" fill="{muted}">Latest market data dates: S&amp;P 500 {html.escape(latest['sp500_date'])}, Nasdaq {html.escape(latest['nasdaq_date'])}. Latest 60-observation corr: S&amp;P {html.escape(latest.get('corr_60d_sp500') or 'n/a')}, Nasdaq {html.escape(latest.get('corr_60d_nasdaq') or 'n/a')}.</text>
</svg>
'''
    svg_path = REPORT_DIR / "liquidity_vs_markets_chart.svg"
    png_path = REPORT_DIR / "liquidity_vs_markets_chart.svg.png"
    svg_path.write_text(svg, encoding="utf-8")

    try:
        subprocess.run(
            ["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        try:
            subprocess.run(
                ["qlmanage", "-t", "-s", "1280", "-o", str(REPORT_DIR), str(svg_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            if png_path.exists():
                png_path.unlink()


def sync_desktop_outputs() -> None:
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return

    today_dir = desktop / "repo" / date.today().isoformat()
    today_dir.mkdir(parents=True, exist_ok=True)

    copies = [
        (REPORT_DIR / "liquidity_chart.svg.png", today_dir / "liquidity_chart.png"),
        (REPORT_DIR / "liquidity_chart.svg", today_dir / "liquidity_chart.svg"),
        (REPORT_DIR / "liquidity_latest.md", today_dir / "liquidity_latest.md"),
        (DATA_DIR / "liquidity_latest.json", today_dir / "liquidity_latest.json"),
        (DATA_DIR / "liquidity_history.csv", today_dir / "liquidity_history.csv"),
        (DATA_DIR / "sp500.csv", today_dir / "sp500.csv"),
        (DATA_DIR / "nasdaq.csv", today_dir / "nasdaq.csv"),
        (DATA_DIR / "liquidity_vs_markets_history.csv", today_dir / "liquidity_vs_markets_history.csv"),
        (REPORT_DIR / "liquidity_vs_markets_chart.svg.png", today_dir / "liquidity_vs_markets_chart.png"),
        (REPORT_DIR / "liquidity_vs_markets_chart.svg", today_dir / "liquidity_vs_markets_chart.svg"),
    ]
    for source, destination in copies:
        if source.exists():
            shutil.copy2(source, destination)


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    fed = fetch_fred("WALCL")
    tga = fetch_tga()
    rrp = fetch_fred("RRPONTSYD")

    if not fed or not tga or not rrp:
        raise RuntimeError("One or more data sources returned no data")

    write_series("fed_total_assets", fed)
    write_series("tga", tga)
    write_series("rrp", rrp)

    rows = build_daily_history(fed, tga, rrp)
    if len(rows) < 2:
        raise RuntimeError("Not enough overlapping observations to build indicator")

    write_daily_history(rows)
    snapshot = latest_snapshot(rows)
    (DATA_DIR / "liquidity_latest.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_chart(rows, snapshot)
    if os.environ.get("LIQUIDITY_WITH_MARKETS") == "1":
        sp500 = fetch_fred("SP500")
        nasdaq = fetch_fred("NASDAQCOM")
        if not sp500 or not nasdaq:
            raise RuntimeError("Market data source returned no data")
        write_index_series("sp500", sp500)
        write_index_series("nasdaq", nasdaq)
        market_rows = build_market_history(rows, sp500, nasdaq)
        write_market_history(market_rows)
        write_market_chart(market_rows, snapshot)
    write_report(snapshot)
    write_pages_index(snapshot)
    sync_desktop_outputs()

    latest = snapshot["latest"]
    assert isinstance(latest, dict)
    print(
        "net_liquidity_usd_mn={net} date={date} fed_date={fed} tga_date={tga} rrp_date={rrp}".format(
            net=latest["net_liquidity_usd_mn"],
            date=latest["date"],
            fed=latest["fed_total_assets_date"],
            tga=latest["tga_date"],
            rrp=latest["rrp_date"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
