"""Read and clean the fuel price CSV.

Cleaning rules (see PLAN.md, "Cleaning decisions"):
1. Strip whitespace from every field (city names have long trailing spaces).
2. Drop Canadian provinces.
3. Merge rows that share an OPIS ID: price = mean (min/max kept), name = most
   frequent spelling (first seen on a tie). Address, city, state and rack are
   identical within a group, so they come from the first row.
4. City names are title-cased for display; matching uses normalize_city().
"""

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .places import display_city

CANADIAN_PROVINCES = {
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT',
}
PRICE_PLACES = Decimal('0.0001')

COLUMNS = {
    'opis_id': 'OPIS Truckstop ID',
    'name': 'Truckstop Name',
    'address': 'Address',
    'city': 'City',
    'state': 'State',
    'rack_id': 'Rack ID',
    'price': 'Retail Price',
}


@dataclass
class CleanStats:
    rows_read: int = 0
    canadian_dropped: int = 0
    invalid_dropped: int = 0
    duplicate_rows_merged: int = 0
    stations: int = 0
    invalid_rows: list = field(default_factory=list)


@dataclass
class CleanStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int
    price: Decimal
    min_price: Decimal
    max_price: Decimal
    row_count: int


def _parse_row(raw):
    row = {key: (raw.get(col) or '').strip() for key, col in COLUMNS.items()}
    row['opis_id'] = int(row['opis_id'])
    row['rack_id'] = int(row['rack_id'])
    row['state'] = row['state'].upper()
    row['city'] = display_city(row['city'])
    row['name'] = ' '.join(row['name'].split())
    row['price'] = Decimal(row['price'])
    if row['price'] <= 0 or not row['city'] or len(row['state']) != 2:
        raise ValueError('missing city/state or non-positive price')
    return row


def read_fuel_csv(path):
    """Return (stations, stats) where stations are CleanStation sorted by OPIS ID."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        return clean_rows(csv.DictReader(f))


def clean_rows(raw_rows):
    stats = CleanStats()
    groups = {}
    for line_no, raw in enumerate(raw_rows, start=2):  # line 1 is the header
        stats.rows_read += 1
        try:
            row = _parse_row(raw)
        except (ValueError, InvalidOperation) as exc:
            stats.invalid_dropped += 1
            stats.invalid_rows.append((line_no, str(exc)))
            continue
        if row['state'] in CANADIAN_PROVINCES:
            stats.canadian_dropped += 1
            continue
        groups.setdefault(row['opis_id'], []).append(row)

    stations = []
    for opis_id in sorted(groups):
        rows = groups[opis_id]
        prices = [r['price'] for r in rows]
        first = rows[0]
        stations.append(CleanStation(
            opis_id=opis_id,
            # most_common keeps first-seen order on ties.
            name=Counter(r['name'] for r in rows).most_common(1)[0][0],
            address=first['address'],
            city=first['city'],
            state=first['state'],
            rack_id=first['rack_id'],
            price=(sum(prices) / len(prices)).quantize(PRICE_PLACES),
            min_price=min(prices).quantize(PRICE_PLACES),
            max_price=max(prices).quantize(PRICE_PLACES),
            row_count=len(rows),
        ))
        stats.duplicate_rows_merged += len(rows) - 1
    stats.stations = len(stations)
    return stations, stats
