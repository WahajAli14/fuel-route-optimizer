"""Is a point inside the United States? Pure-Python point-in-polygon.

The boundary is the US Census Bureau cartographic boundary file for the
nation (see the README for the exact source), stored as a GeoJSON
MultiPolygon in data/. It is loaded once per process.

Ray casting with the even-odd rule over every ring of a polygon, so holes are
respected; a point is in the US if it is inside any polygon. A bounding box per
polygon skips most of them quickly.
"""

import json
from functools import lru_cache

from django.conf import settings


@lru_cache(maxsize=1)
def _polygons():
    """[(min_lng, min_lat, max_lng, max_lat, rings)] for every polygon in the boundary."""
    path = settings.DATA_DIR / settings.US_BOUNDARY_FILE
    with open(path) as f:
        geometry = json.load(f)['features'][0]['geometry']
    polygons = []
    for rings in geometry['coordinates']:
        lngs = [p[0] for p in rings[0]]
        lats = [p[1] for p in rings[0]]
        polygons.append((min(lngs), min(lats), max(lngs), max(lats),
                         [[(p[0], p[1]) for p in ring] for ring in rings]))
    return polygons


def _inside(rings, lng, lat):
    inside = False
    for ring in rings:
        x1, y1 = ring[-1]
        for x2, y2 in ring:
            if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
            x1, y1 = x2, y2
    return inside


def contains(lat, lng):
    """True if (lat, lng) lies inside the US boundary."""
    for min_lng, min_lat, max_lng, max_lat, rings in _polygons():
        if min_lng <= lng <= max_lng and min_lat <= lat <= max_lat and _inside(rings, lng, lat):
            return True
    return False
