"""The route request pipeline, shared by the JSON endpoint and the map page.

    resolve start/finish locally (no API call)
    -> one routing-provider call (ORS directions)
    -> stations along the route (in-memory KD-tree)
    -> fuel plan (optimizer)
    -> response dict

Distances in the response:
    route_miles          driving distance along the route, measured on the
                         resampled ORS polyline (the same distances the
                         optimizer uses; meta.ors_summary_miles shows the
                         provider's own figure)
    fuel_stops[].detour_miles
                         one-way, approximate straight-line separation between
                         the station's city-level location and its nearest
                         route point
    detour_miles         sum of round trips (2 x one-way) over the stops made
    total_driving_miles  route_miles + detour_miles; total_gallons is this / MPG

Caching: the computed result is cached under a key built from the resolved
places (not the caller's text) and every input that changes the plan, so
"Chicago, IL" and "chicago, illinois" share an entry. The cached value holds
no caller input: start/finish `query` and all of `meta` are rebuilt from the
current request on every call, hit or miss.
"""

import hashlib
import json
import logging
import math
import time

from django.conf import settings
from django.core.cache import cache

from ..models import DataVersion
from .corridor import StationIndex, stations_along
from .locations import LocationError, LocationResolver
from .optimizer import optimize
from .ors_client import client_from_settings

_GALLON_TOLERANCE = 1e-6

logger = logging.getLogger(__name__)


def _ms(start):
    return round((time.perf_counter() - start) * 1000, 1)


def _resolve(resolver, text, field):
    try:
        return resolver.resolve(text)
    except LocationError as exc:
        exc.field = field
        raise


def _place(location):
    """The resolved place, without the caller's query text (that is added per request)."""
    return {'label': location.label, 'lat': location.lat, 'lng': location.lng}


def _stop(stop, origin):
    station = stop.station
    if stop.assumed:
        # No candidate on the route: the fill is assumed to happen at the origin.
        point = {'lat': origin.lat, 'lng': origin.lng, 'mile_marker': 0.0}
        detour = 0.0
    else:
        candidate = stop.candidate
        point = {'lat': round(candidate.route_lat, 5), 'lng': round(candidate.route_lng, 5),
                 'mile_marker': round(candidate.mile_marker, 1)}
        detour = round(candidate.detour_miles, 1)
    return {
        'name': station.name,
        'address': station.address,
        'city': station.city,
        'state': station.state,
        'opis_id': station.opis_id,
        'location': {'lat': station.lat, 'lng': station.lng, 'precision': 'city'},
        'route_point': point,
        'detour_miles': detour,
        'price': round(station.price, 3),
        'gallons': round(stop.gallons, 2),
        'cost': round(stop.cost, 2),
        'assumed': stop.assumed,
    }


def cache_key(origin, destination, *, corridor_miles, stop_penalty, profile, data_version):
    """Everything that changes the computed result, and nothing from the caller's text."""
    fuel = settings.FUEL
    parts = {
        'start': [origin.label, round(origin.lat, 6), round(origin.lng, 6)],
        'finish': [destination.label, round(destination.lat, 6), round(destination.lng, 6)],
        'corridor_miles': float(corridor_miles),
        'stop_penalty': float(stop_penalty),
        'profile': profile,
        'max_range_miles': fuel['MAX_RANGE_MILES'],
        'mpg': fuel['MPG'],
        'start_fuel_miles': fuel['START_FUEL_SEARCH_MILES'],
    }
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]
    return f'route:v{data_version}:{digest}'


def _compute(origin, destination, *, corridor_miles, stop_penalty, client, timings):
    """The computed (cacheable) part of the response. Makes one routing call."""
    fuel = settings.FUEL

    t = time.perf_counter()
    route = client.route((origin.lat, origin.lng), (destination.lat, destination.lng))
    timings['routing_ms'] = _ms(t)

    t = time.perf_counter()
    resampled, candidates = stations_along(route.coords, corridor_miles)
    nearest_station, _ = StationIndex.current().nearest(origin.lat, origin.lng)
    timings['corridor_ms'] = _ms(t)

    t = time.perf_counter()
    plan = optimize(candidates, resampled.total_miles,
                    max_range_miles=fuel['MAX_RANGE_MILES'], mpg=fuel['MPG'],
                    start_fuel_miles=fuel['START_FUEL_SEARCH_MILES'],
                    origin_station=nearest_station, stop_penalty=stop_penalty)
    timings['optimize_ms'] = _ms(t)

    route_miles = resampled.total_miles
    total_driving_miles = route_miles + plan.detour_miles
    # Checked on unrounded values, before any display rounding.
    if not math.isclose(plan.total_gallons, total_driving_miles / fuel['MPG'],
                        rel_tol=0, abs_tol=_GALLON_TOLERANCE * max(1.0, total_driving_miles)):
        raise AssertionError('Fuel plan does not cover the driving distance.')

    return {
        'start': _place(origin),
        'finish': _place(destination),
        'route_miles': round(route_miles, 1),
        'detour_miles': round(plan.detour_miles, 1),
        'total_driving_miles': round(total_driving_miles, 1),
        'route_geojson': {
            'type': 'LineString',
            # The resampled route: about one point per mile, [lng, lat].
            'coordinates': [[round(lng, 5), round(lat, 5)] for lat, lng in resampled.points],
        },
        'fuel_stops': [_stop(s, origin) for s in plan.stops],
        'total_gallons': round(plan.total_gallons, 2),
        'total_fuel_cost': round(plan.total_cost, 2),
        'assumed_origin_fill': plan.assumed_origin_fill,
        'route_info': {
            'ors_summary_miles': round(route.distance_miles, 1),
            'route_warnings': route.warnings,
        },
    }


def plan_route(start, finish, *, corridor_miles, stop_penalty, client=None, use_cache=True):
    """Run the pipeline (or reuse a cached result) and return the response dict.

    use_cache=False always computes and stores nothing (for timing replays).

    Raises LocationError (with .field), RoutingError or NoFeasibleRoute.
    """
    started = time.perf_counter()
    timings = {}

    t = time.perf_counter()
    resolver = LocationResolver()
    origin = _resolve(resolver, start, 'start')
    destination = _resolve(resolver, finish, 'finish')
    timings['resolve_ms'] = _ms(t)

    client = client or client_from_settings()
    data_version = DataVersion.current()
    key = cache_key(origin, destination, corridor_miles=corridor_miles,
                    stop_penalty=stop_penalty, profile=client.profile, data_version=data_version)

    t = time.perf_counter()
    computed = cache.get(key) if use_cache else None
    timings['cache_get_ms'] = _ms(t)
    hit = computed is not None
    calls_before = client.calls
    if not hit:
        computed = _compute(origin, destination, corridor_miles=corridor_miles,
                            stop_penalty=stop_penalty, client=client, timings=timings)
        if use_cache:
            cache.set(key, computed)

    # Request-specific fields, rebuilt on every call (never read from the cache).
    result = {key_: value for key_, value in computed.items() if key_ != 'route_info'}
    result['start'] = {'query': origin.query, **computed['start']}
    result['finish'] = {'query': destination.query, **computed['finish']}
    result['meta'] = {
        'external_api_calls': client.calls - calls_before,
        'cache_hit': hit,
        'elapsed_ms': _ms(started),
        'timings': timings,
        'data_version': data_version,
        'stop_penalty_dollars': stop_penalty,
        'corridor_miles': corridor_miles,
        'ors_profile': client.profile,
        **computed['route_info'],
    }
    logger.info('route %s -> %s: cache_hit=%s calls=%s elapsed_ms=%s timings=%s',
                origin.label, destination.label, hit, result['meta']['external_api_calls'],
                result['meta']['elapsed_ms'], timings)
    return result
