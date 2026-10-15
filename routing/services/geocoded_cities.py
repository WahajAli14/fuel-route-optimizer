"""Committed snapshot of the cities the Census files don't list.

`load_fuel_prices` asks Nominatim once for station cities missing from the
Census files and stores the answers (and misses) in CityCoord.
`export_geocoded_cities` writes them to data/geocoded_cities.csv, and
`load_places` loads that file after the Census files, so a fresh clone gets
every station located with zero Nominatim calls.
"""

import csv

from ..models import CityCoord
from .places import normalize_city

FIELDS = ['state', 'name', 'lat', 'lng', 'source']
EXPORTED_SOURCES = [CityCoord.SOURCE_NOMINATIM, CityCoord.SOURCE_NOT_FOUND]


def export_rows(path):
    """Write Nominatim answers and recorded misses to `path`; returns the row count."""
    rows = (CityCoord.objects.filter(source__in=EXPORTED_SOURCES)
            .order_by('state', 'name').values_list(*FIELDS))
    count = 0
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f, lineterminator='\n')  # LF, not csv's default CRLF
        writer.writerow(FIELDS)
        for state, name, lat, lng, source in rows:
            writer.writerow([state, name, '' if lat is None else lat, '' if lng is None else lng,
                             source])
            count += 1
    return count


def read_rows(path):
    """CityCoord objects (unsaved) for every row in an exported file."""
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            if row['source'] not in EXPORTED_SOURCES:
                raise ValueError(f'Unexpected source {row["source"]!r} in {path}')
            yield CityCoord(
                state=row['state'], key=normalize_city(row['name']), name=row['name'],
                lat=float(row['lat']) if row['lat'] else None,
                lng=float(row['lng']) if row['lng'] else None,
                source=row['source'],
            )
