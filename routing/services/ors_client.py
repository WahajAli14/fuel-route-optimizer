"""OpenRouteService directions client: one HTTP call per route.

Kept behind a small class so the provider can be swapped (OSRM is the
fallback). The free plan allows a limited number of directions requests per
day (200 on this key), which is why responses are cached upstream and
geocoding never goes through ORS.
"""

import time
from dataclasses import dataclass, field

import requests
from django.conf import settings

METERS_PER_MILE = 1609.344

# ORS error codes (https://giscience.github.io/openrouteservice/api-reference/error-codes)
_NO_ROUTE_CODES = {2009, 2010}  # route not found, point not routable
_ROUTE_TOO_LONG_CODE = 2004


class RoutingError(Exception):
    """A routing failure with an HTTP status suitable for our API response."""

    def __init__(self, message, status=502, provider_code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.provider_code = provider_code


@dataclass
class Route:
    coords: list            # [(lat, lng), ...] along the route
    distance_miles: float
    duration_s: float
    roads: list = field(default_factory=list)      # distinct road names, in order
    warnings: list = field(default_factory=list)
    profile: str = ''
    elapsed_ms: float = 0.0


def decode_polyline(encoded, precision=5):
    """Decode a Google encoded polyline into [(lat, lng), ...]."""
    coords, index, lat, lng = [], 0, 0, 0
    factor = 10 ** precision
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        coords.append((lat / factor, lng / factor))
    return coords


class ORSClient:
    def __init__(self, api_key, profile='driving-hgv', base_url='https://api.openrouteservice.org',
                 timeout=15, snap_radius_m=5000, session=None):
        self.api_key = api_key
        self.profile = profile
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        # Start/finish are city centroids, which may sit a few km from a routable road.
        self.snap_radius_m = snap_radius_m
        self.session = session or requests.Session()
        self.calls = 0

    def route(self, start, finish):
        """Route between two (lat, lng) points. Raises RoutingError."""
        if not self.api_key:
            raise RoutingError('Routing is not configured: set ORS_API_KEY in .env.', status=503)

        url = f'{self.base_url}/v2/directions/{self.profile}'
        body = {
            'coordinates': [[start[1], start[0]], [finish[1], finish[0]]],
            'instructions': True,  # step names give us the roads used
            'radiuses': [self.snap_radius_m, self.snap_radius_m],
        }
        started = time.monotonic()
        self.calls += 1
        try:
            response = self.session.post(url, json=body, timeout=self.timeout,
                                         headers={'Authorization': self.api_key})
        except requests.Timeout as exc:
            raise RoutingError('The routing service timed out. Please try again.', status=504) from exc
        except requests.RequestException as exc:
            raise RoutingError(f'Could not reach the routing service: {exc}', status=502) from exc
        elapsed_ms = (time.monotonic() - started) * 1000

        if response.status_code != 200:
            raise self._error_for(response)

        route = response.json()['routes'][0]
        roads = []
        for segment in route.get('segments', []):
            for step in segment.get('steps', []):
                name = step.get('name', '')
                if name and name != '-' and name not in roads:
                    roads.append(name)
        summary = route['summary']
        return Route(
            coords=decode_polyline(route['geometry']),
            distance_miles=summary.get('distance', 0.0) / METERS_PER_MILE,
            duration_s=summary.get('duration', 0.0),
            roads=roads,
            warnings=[w.get('message', '') for w in route.get('warnings', [])],
            profile=self.profile,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _error_for(response):
        try:
            error = response.json().get('error', {})
        except ValueError:
            error = {}
        if isinstance(error, str):
            error = {'message': error}
        code = error.get('code')
        message = error.get('message') or response.text[:200]

        if response.status_code in (401, 403):
            return RoutingError('The routing service rejected the API key.', status=502,
                                provider_code=code)
        if response.status_code == 429:
            return RoutingError('Routing rate limit reached. Please try again later.', status=503,
                                provider_code=code)
        if code == _ROUTE_TOO_LONG_CODE:
            return RoutingError(f'Route is too long for the routing service: {message}', status=422,
                                provider_code=code)
        if code in _NO_ROUTE_CODES:
            return RoutingError(f'No drivable route found between these locations: {message}',
                                status=422, provider_code=code)
        return RoutingError(f'Routing service error ({response.status_code}): {message}',
                            status=502, provider_code=code)


def client_from_settings():
    return ORSClient(
        api_key=settings.ORS_API_KEY,
        profile=settings.ORS_PROFILE,
        base_url=settings.ORS_BASE_URL,
        timeout=settings.ORS_TIMEOUT_SECONDS,
    )
