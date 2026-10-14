"""Find the fuel stations that sit along a route.

Stations live in a cKDTree over 3D unit vectors, so straight-line (chord)
distances are correct anywhere in the US without a map projection. The tree
is built once per process and rebuilt when the data version changes.

For a request, the route is resampled to about one point per mile with a
cumulative mile marker at each point. Stations within the corridor of any
route point are candidates; each one keeps its nearest route point, which is
where the optimizer "buys" fuel and where the map draws the marker.

Station coordinates are city-level (the CSV has no lat/lng), so
`detour_miles`, the straight-line distance from that route point to the
station's city, is approximate.
"""

import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from ..models import DataVersion, FuelStation

EARTH_RADIUS_MILES = 3958.8


@dataclass(frozen=True)
class Station:
    id: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lng: float


@dataclass(frozen=True)
class Candidate:
    station: Station
    mile_marker: float      # miles from the start to route_point
    route_lat: float        # nearest resampled route point
    route_lng: float
    detour_miles: float     # approximate: station coordinates are city-level


@dataclass(frozen=True)
class ResampledRoute:
    points: np.ndarray      # (n, 2) lat, lng
    miles: np.ndarray       # (n,) cumulative mile marker at each point

    @property
    def total_miles(self):
        return float(self.miles[-1])


def to_unit_vectors(lats, lngs):
    lat = np.radians(np.asarray(lats, dtype=float))
    lng = np.radians(np.asarray(lngs, dtype=float))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lng), cos_lat * np.sin(lng), np.sin(lat)))


def miles_to_chord(miles):
    return 2 * np.sin(np.asarray(miles, dtype=float) / (2 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord, dtype=float) / 2, 0, 1))


def haversine_miles(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def resample_route(coords, step_miles=1.0):
    """Resample [(lat, lng), ...] to points every `step_miles`, plus the end.

    Segments are interpolated linearly in lat/lng, which is accurate at the
    scale of a mile. Mile markers follow the polyline, so they can differ
    slightly from the distance the routing provider reports.
    """
    coords = np.asarray(coords, dtype=float)
    if coords.ndim != 2 or len(coords) == 0:
        raise ValueError('Route has no coordinates.')
    if len(coords) == 1:
        return ResampledRoute(points=coords.copy(), miles=np.zeros(1))

    seg = haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    cumulative = np.concatenate(([0.0], np.cumsum(seg)))
    total = cumulative[-1]

    targets = np.arange(0.0, total, step_miles)
    if len(targets) == 0 or targets[-1] < total:
        targets = np.append(targets, total)
    lats = np.interp(targets, cumulative, coords[:, 0])
    lngs = np.interp(targets, cumulative, coords[:, 1])
    return ResampledRoute(points=np.column_stack((lats, lngs)), miles=targets)


class StationIndex:
    """All located stations plus a KD-tree over their unit vectors."""

    _lock = threading.Lock()
    _cached = None
    _version = None

    def __init__(self, stations):
        self.stations = list(stations)
        if self.stations:
            lats = [s.lat for s in self.stations]
            lngs = [s.lng for s in self.stations]
            self.tree = cKDTree(to_unit_vectors(lats, lngs))
        else:
            self.tree = None

    @classmethod
    def from_db(cls):
        rows = FuelStation.objects.values_list(
            'id', 'opis_id', 'name', 'address', 'city', 'state', 'price', 'lat', 'lng')
        return cls(
            Station(id=pk, opis_id=opis_id, name=name, address=address, city=city,
                    state=state, price=float(price), lat=lat, lng=lng)
            for pk, opis_id, name, address, city, state, price, lat, lng in rows
        )

    @classmethod
    def current(cls):
        version = DataVersion.current()
        if cls._cached is None or cls._version != version:
            with cls._lock:
                if cls._cached is None or cls._version != version:
                    cls._cached = cls.from_db()
                    cls._version = version
        return cls._cached

    def candidates(self, route, corridor_miles):
        """Stations within `corridor_miles` of the route, sorted by mile marker."""
        if self.tree is None:
            return []
        route_vectors = to_unit_vectors(route.points[:, 0], route.points[:, 1])
        radius = float(miles_to_chord(corridor_miles))

        # Coarse pass: every station near any route point.
        hits = self.tree.query_ball_point(route_vectors, r=radius)
        nearby = sorted(set().union(*hits)) if len(hits) else []
        if not nearby:
            return []

        # Fine pass: each station's nearest route point.
        route_tree = cKDTree(route_vectors)
        chords, idx = route_tree.query(self.tree.data[nearby])

        result = []
        for station_i, chord, point_i in zip(nearby, chords, idx):
            detour = float(chord_to_miles(chord))
            if detour > corridor_miles:
                continue
            lat, lng = route.points[point_i]
            result.append(Candidate(
                station=self.stations[station_i],
                mile_marker=float(route.miles[point_i]),
                route_lat=float(lat),
                route_lng=float(lng),
                detour_miles=detour,
            ))
        result.sort(key=lambda c: (c.mile_marker, c.station.price, c.station.opis_id))
        return result


def stations_along(coords, corridor_miles, step_miles=1.0):
    """Resample a route and return (ResampledRoute, candidates)."""
    route = resample_route(coords, step_miles)
    return route, StationIndex.current().candidates(route, corridor_miles)
