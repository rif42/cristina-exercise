#!/usr/bin/env python3
"""Fetch place data from countries.dev into SQLite.

Usage:
    python fetch_place.py                  # sync every city in Indonesia (default)
    python fetch_place.py 1625822          # sync one place (geoname id or URL)
    python fetch_place.py --country ID     # sync every city in another country
    python fetch_place.py --db other.db    # choose the database (default places.db)

Requires only the Python standard library (3.9+).

Why the substring scan: /cities accepts only q, country and limit (max 100) and
ignores offset, so a country with more than 100 cities cannot be paged. We query
every two-letter substring, which every city name of length >= 2 contains, and
split any query that returns a full page into longer substrings until it no
longer saturates. The result is the complete set of matching cities.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

API = "https://countries.dev"
DEFAULT_COUNTRY = "ID"
DEFAULT_DB = "places.db"
PAGE_LIMIT = 100
TIMEOUT = 30
MAX_ATTEMPTS = 5
MAX_QUERY_LEN = 12
LETTERS = "abcdefghijklmnopqrstuvwxyz"
USER_AGENT = "countries-dev-fetcher/2.0 (+https://countries.dev/)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    geoname_id    INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    ascii_name    TEXT,
    feature_class TEXT,
    feature_code  TEXT,
    country_code  TEXT,
    admin1_code   TEXT,
    latitude      REAL,
    longitude     REAL,
    population    INTEGER,
    timezone      TEXT,
    raw_json      TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL
);
"""

UPSERT = """
INSERT INTO places (
    geoname_id, name, ascii_name, feature_class, feature_code,
    country_code, admin1_code, latitude, longitude, population,
    timezone, raw_json, fetched_at
) VALUES (
    :geoname_id, :name, :ascii_name, :feature_class, :feature_code,
    :country_code, :admin1_code, :latitude, :longitude, :population,
    :timezone, :raw_json, :fetched_at
)
ON CONFLICT(geoname_id) DO UPDATE SET
    name          = excluded.name,
    ascii_name    = excluded.ascii_name,
    feature_class = COALESCE(excluded.feature_class, places.feature_class),
    feature_code  = COALESCE(excluded.feature_code, places.feature_code),
    country_code  = COALESCE(excluded.country_code, places.country_code),
    admin1_code   = COALESCE(excluded.admin1_code, places.admin1_code),
    latitude      = COALESCE(excluded.latitude, places.latitude),
    longitude     = COALESCE(excluded.longitude, places.longitude),
    population    = COALESCE(excluded.population, places.population),
    timezone      = COALESCE(excluded.timezone, places.timezone),
    raw_json      = excluded.raw_json,
    fetched_at    = excluded.fetched_at;
"""


def get_json(url: str, *, empty_on_404: bool = False):
    """GET a JSON document, retrying throttling and transient server errors."""
    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return json.loads(resp.read().decode(charset))
        except urllib.error.HTTPError as exc:
            # The cities search answers 404 when nothing matches; an unknown
            # country also yields 404 ("Country not found"), which the caller
            # cannot distinguish here, so a country typo surfaces as zero rows.
            if exc.code == 404 and empty_on_404:
                return []
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 10)
                last_error = f"HTTP {exc.code}"
                time.sleep(delay)
                continue
            raise SystemExit(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            last_error = f"network error: {exc.reason}"
            time.sleep(min(2 ** attempt, 10))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"response is not valid JSON: {exc}") from exc
    raise SystemExit(f"giving up on {url} after {MAX_ATTEMPTS} attempts ({last_error})")


def place_url(target: str) -> str:
    """Accept a bare geoname id or a full countries.dev URL."""
    if target.startswith(("http://", "https://")):
        return target
    if not target.isdigit():
        raise ValueError(f"expected a numeric place id or URL, got {target!r}")
    return f"{API}/places/{target}"


def fetch_place(target: str) -> dict:
    place = get_json(place_url(target))
    if not isinstance(place, dict) or "geonameId" not in place:
        raise SystemExit(f"unexpected payload for {target!r}: no geonameId")
    return place


def search_cities(country: str, q: str) -> list[dict]:
    params = urllib.parse.urlencode({"country": country, "q": q, "limit": PAGE_LIMIT})
    rows = get_json(f"{API}/cities?{params}", empty_on_404=True)
    if not isinstance(rows, list):
        raise SystemExit(f"unexpected payload for city search q={q!r}: {type(rows).__name__}")
    return rows


def collect_cities(country: str, workers: int) -> dict[int, dict]:
    """Enumerate every city of a country through substring queries."""
    found: dict[int, dict] = {}
    queried: set[str] = set()
    pending = [a + b for a in LETTERS for b in LETTERS]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending:
            queried.update(pending)
            results = pool.map(lambda q: (q, search_cities(country, q)), pending)

            saturated: list[str] = []
            for query, rows in results:
                for row in rows:
                    found[row["geonameId"]] = row
                if len(rows) >= PAGE_LIMIT:
                    saturated.append(query)

            next_queries: list[str] = []
            for query in saturated:
                if len(query) >= MAX_QUERY_LEN:
                    raise SystemExit(
                        f"too many matches for {country!r} containing {query!r}; "
                        "the result would be truncated (this enumeration is sized for normal countries)"
                    )
                # A name contains `query` with a neighbour on at least one side;
                # widening by one letter on either side covers every occurrence.
                for char in LETTERS:
                    for candidate in (char + query, query + char):
                        if candidate not in queried:
                            next_queries.append(candidate)
            pending = list(dict.fromkeys(next_queries))

    return found


def to_row(place: dict, fetched_at: str) -> dict:
    return {
        "geoname_id": int(place["geonameId"]),
        "name": place.get("name") or place.get("asciiName") or "",
        "ascii_name": place.get("asciiName"),
        # /cities rows carry no featureClass; they are populated places by definition.
        "feature_class": place.get("featureClass") or ("P" if place.get("featureCode") else None),
        "feature_code": place.get("featureCode"),
        "country_code": place.get("countryCode"),
        "admin1_code": place.get("admin1Code"),
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
        "population": place.get("population"),
        "timezone": place.get("timezone"),
        "raw_json": json.dumps(place, ensure_ascii=False, sort_keys=True),
        "fetched_at": fetched_at,
    }


def save(db_path: str, rows: list[dict]) -> int:
    """Upsert all rows in one transaction; returns the row count for the DB."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.executemany(UPSERT, rows)
        total = conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch countries.dev data and save it to SQLite.")
    parser.add_argument(
        "place",
        nargs="?",
        help=f"geoname id or URL of a single place to sync (omitted: sync all cities of --country)",
    )
    parser.add_argument(
        "--country",
        default=None,
        help=f"ISO 3166-1 alpha-2 country whose cities are synced (default: {DEFAULT_COUNTRY} when no place is given)",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    parser.add_argument("--workers", type=int, default=10, help="concurrent requests during the city scan (default: 10)")
    args = parser.parse_args(argv)

    if args.place and args.country:
        parser.error("give either a single place or --country, not both")
    db_dir = os.path.dirname(os.path.abspath(args.db))
    if not os.path.isdir(db_dir):
        parser.error(f"database directory does not exist: {db_dir}")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.place:
        try:
            url = place_url(args.place)
        except ValueError as exc:
            parser.error(str(exc))
        row = to_row(fetch_place(url), fetched_at)
        rows = [row]
        label = f"geoname {row['geoname_id']} ({row['name']})"
    else:
        country = (args.country or DEFAULT_COUNTRY).upper()
        cities = collect_cities(country, max(1, args.workers))
        if not cities:
            raise SystemExit(f"no cities returned for country {country!r}")
        rows = [to_row(city, fetched_at) for city in cities.values()]
        label = f"{len(rows)} cities in {country}"

    total = save(args.db, rows)
    print(f"saved {label} to {args.db} ({total} rows in places)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
