#!/usr/bin/env python3
"""Render population totals per time zone from places.db into a Plotly bar chart page.

Usage:
    python build_timezone_report.py [--db places.db] [--out index.html]

The output is a single HTML file with the data embedded; Plotly itself is loaded
from the versioned plotly-basic CDN (1.1 MB, bar charts only), so the page renders when opened online and is ready to
publish as-is with GitHub Pages. Requires only the Python standard library.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from string import Template

DEFAULT_DB = "places.db"
DEFAULT_OUT = "index.html"
PLOTLY_VERSION = "2.35.2"

QUERY = """
SELECT timezone,
       COUNT(*)                        AS cities,
       SUM(COALESCE(population, 0))    AS population
FROM places
GROUP BY timezone
ORDER BY population DESC, timezone ASC
"""

PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<meta name="description" content="Total population per time zone, charted from countries.dev city data.">
<script src="https://cdn.plot.ly/plotly-basic-$plotly_version.min.js" defer></script>
<style>
:root{color-scheme:light dark;--fg:#111418;--muted:#5b6470;--bg:#fbfbfc;--surface:#fff;--border:#e3e6ea;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--fg:#e6e9ee;--muted:#9aa4b2;--bg:#0f1216;--surface:#151a20;--border:#252d38;--accent:#7aa2ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:820px;margin:0 auto;padding:48px 20px 64px}
h1{font-size:clamp(1.5rem,1rem + 2vw,2rem);line-height:1.15;margin:0 0 10px;letter-spacing:-.02em}
p{margin:0}
.meta{margin-top:10px;color:var(--muted);font-size:.9rem;max-width:68ch}
.meta a{color:var(--accent)}
strong{color:var(--fg);font-weight:600}
figure{margin:28px 0 0}
.chart{width:100%}
figcaption{margin-top:6px;color:var(--muted);font-size:.8rem}
.noscript{margin-top:24px;color:var(--muted);font-size:.9rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
</style>
</head>
<body>
<main>
<h1>$title</h1>
<p class="meta">$scope. Source: <a href="https://countries.dev/" rel="noopener">countries.dev</a> city data, saved in <code>$db</code>$fetched; page generated $generated.</p>
<p class="meta">Largest time zone: <strong>$top_name</strong> — $top_population people ($top_share of the total).</p>
<figure>
<div id="chart" class="chart" role="img" aria-label="$aria"></div>
<figcaption>Population is the sum of the city populations stored per time zone. Hover a bar for its city count; labels show population and share of the total.</figcaption>
</figure>
<noscript>
<ul class="noscript">
$noscript_rows
</ul>
</noscript>
</main>
<script>
const DATA = $data_json;

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function colors() {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  return {
    fg: cssVar("--fg", dark ? "#e6e9ee" : "#111418"),
    muted: cssVar("--muted", dark ? "#9aa4b2" : "#5b6470"),
    surface: cssVar("--surface", dark ? "#151a20" : "#ffffff"),
    border: cssVar("--border", dark ? "#252d38" : "#e3e6ea"),
    accent: cssVar("--accent", dark ? "#7aa2ff" : "#1d4ed8")
  };
}

function draw() {
  const chart = document.getElementById("chart");
  if (!window.Plotly) {
    chart.textContent = "The chart library could not be loaded. The zone totals are listed below.";
    return;
  }
  const c = colors();
  const max = Math.max.apply(null, DATA.map(d => d.population));
  const gap = max * 0.015;
  const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
  const value = d => narrow ? compact.format(d.population) : d.population.toLocaleString("en-US");
  const trace = {
    type: "bar",
    orientation: "h",
    x: DATA.map(d => d.population),
    y: DATA.map(d => d.timezone),
    customdata: DATA.map(d => ({ cities: d.cities, share: d.share })),
    marker: { color: c.accent, line: { width: 0 } },
    cliponaxis: false,
    hovertemplate:
      "%{y}<br>Population: %{x:,}<br>Cities: %{customdata.cities}<br>" +
      "Share: %{customdata.share:.1f}%<extra></extra>"
  };
  // Labels are annotations, not trace text: outside text starts exactly at the bar
  // end, so the smallest bar's label would sit against its axis tick label.
  const annotations = DATA.map(d => ({
    x: d.population + gap,
    y: d.timezone,
    xanchor: "left",
    yanchor: "middle",
    showarrow: false,
    text: value(d) + " · " + d.share.toFixed(1) + "%",
    font: { color: c.fg, size: 12 }
  }));
  const layout = {
    height: Math.max(340, DATA.length * 46 + 130),
    margin: { l: 8, r: 20, t: 8, b: 44 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif', size: 13, color: c.muted },
    hoverlabel: { bgcolor: c.surface, bordercolor: c.border, font: { color: c.fg } },
    annotations: annotations,
    xaxis: {
      gridcolor: c.border,
      zeroline: false,
      range: [0, max * 1.34],
      tickformat: "~s",
      tickfont: { color: c.muted },
      title: { text: "Population", font: { size: 12, color: c.muted } }
    },
    yaxis: { automargin: true, autorange: "reversed", tickfont: { color: c.fg, size: 13 } },
    bargap: 0.35,
    showlegend: false
  };
  Plotly.react(chart, [trace], layout, { displayModeBar: false, responsive: true });
}

window.addEventListener("load", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
// Full numbers need ~130px per label; below 640px the labels switch to compact.
// Plotly's responsive layout already resizes the SVG, so redraw only at the flip.
let narrow = window.innerWidth < 640;
let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const next = window.innerWidth < 640;
    if (next !== narrow) { narrow = next; draw(); }
  }, 150);
});
</script>
</body>
</html>
""")


def read_totals(db_path: Path) -> tuple[list[tuple[str, int, int]], str | None]:
    if not db_path.is_file():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(QUERY).fetchall()
        fetched_at = conn.execute("SELECT MAX(fetched_at) FROM places").fetchone()[0]
    finally:
        conn.close()
    if not rows:
        raise SystemExit(f"no rows in {db_path}")
    return [(name or "Unspecified", int(cities), int(population)) for name, cities, population in rows], fetched_at


def render(rows: list[tuple[str, int, int]], db_path: Path, fetched_at: str | None, generated: str) -> str:
    total_population = sum(population for _, _, population in rows)
    total_cities = sum(cities for _, cities, _ in rows)

    data = [
        {
            "timezone": name,
            "cities": cities,
            "population": population,
            "share": population / total_population * 100 if total_population else 0.0,
        }
        for name, cities, population in rows
    ]

    top = data[0]
    fetched = f", last fetched {html.escape(fetched_at)}" if fetched_at else ""
    aria = (
        f"Horizontal bar chart of population by time zone: "
        f"{html.escape(top['timezone'])} is largest with {top['population']:,} people "
        f"({total_cities:,} cities across {len(data)} time zones)."
    )
    noscript_rows = "\n".join(
        '  <li>{name}: {population:,} people in {cities:,} cities ({share:.1f}%)</li>'.format(
            name=html.escape(item["timezone"]),
            population=item["population"],
            cities=item["cities"],
            share=item["share"],
        )
        for item in data
    )

    return PAGE.substitute(
        title="Population by Time Zone",
        plotly_version=PLOTLY_VERSION,
        scope=f"{total_cities:,} cities across {len(data)} time zones",
        db=html.escape(str(db_path)),
        fetched=fetched,
        generated=generated,
        top_name=html.escape(top["timezone"]),
        top_population=f"{top['population']:,}",
        top_share=f"{top['share']:.1f}%",
        aria=html.escape(aria),
        noscript_rows=noscript_rows,
        # Escape "<" so a timezone name can never terminate the script element early.
        data_json=json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chart places.db population per time zone into a static HTML page.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"HTML output path (default: {DEFAULT_OUT})")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    out_path = Path(args.out)

    out_dir = out_path.parent if str(out_path.parent) else Path(".")
    if not out_dir.is_dir():
        parser.error(f"output directory does not exist: {out_dir}")

    rows, fetched_at = read_totals(db_path)
    generated = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    document = render(rows, db_path, fetched_at, generated)

    # Write beside the target and swap, so a page being served is never truncated.
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(document, encoding="utf-8")
    os.replace(tmp_path, out_path)

    total = sum(population for _, _, population in rows)
    print(f"wrote {out_path} ({len(rows)} time zones, {total:,} people) at {generated}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
