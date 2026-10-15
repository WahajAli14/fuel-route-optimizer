from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from routing.models import CityCoord, DataVersion, FuelStation
from routing.services import planner
from routing.services.corridor import StationIndex
from routing.services.locations import LocationResolver
from routing.services.ors_client import Route, RoutingError
from routing.services.places import normalize_city

# Due north along -90 from Alpha (lat 38) to Omega (lat 46): about 553 miles.
NORTH = [(38.0 + k * 0.5, -90.0) for k in range(17)]


class FakeORS:
    """Stands in for ORSClient: counts calls and returns a fixed route."""

    profile = 'driving-hgv'

    def __init__(self, coords=NORTH, error=None):
        self.coords = coords
        self.error = error
        self.calls = 0

    def route(self, start, finish):
        self.calls += 1
        if self.error:
            raise self.error
        return Route(coords=list(self.coords), distance_miles=553.5, duration_s=30000.0,
                     roads=['I 39'], warnings=[], profile=self.profile, elapsed_ms=5.0)


def city(name, state, lat, lng):
    CityCoord.objects.create(state=state, key=normalize_city(name), name=name, lat=lat, lng=lng,
                             source=CityCoord.SOURCE_CENSUS_PLACE)


def station(opis_id, lat, lng, price, city_name='Town'):
    price = Decimal(price)
    FuelStation.objects.create(opis_id=opis_id, name=f'Stop {opis_id}', address='I-39, EXIT 1',
                               city=city_name, state='IL', rack_id=1, price=price,
                               min_price=price, max_price=price, lat=lat, lng=lng)


class RouteTestBase(TestCase):
    """Shared fixtures: two cities, three stations along a due-north route, a fake ORS."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for cls, attrs in ((LocationResolver, ('_index', '_version')),
                           (StationIndex, ('_cached', '_version'))):
            for attr in attrs:
                setattr(cls, attr, None)
                self.addCleanup(setattr, cls, attr, None)
        city('Alpha', 'IL', 38.0, -90.0)
        city('Omega', 'WI', 46.0, -90.0)
        station(1, 38.1, -90.02, '3.000', 'Near Alpha')   # mile ~7, ~1 mi off the route
        station(2, 42.0, -90.05, '2.800', 'Midway')       # mile ~276
        station(3, 44.0, -90.00, '3.500', 'Late')         # mile ~415
        self.ors = FakeORS()
        patcher = mock.patch.object(planner, 'client_from_settings', return_value=self.ors)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = APIClient()

    def post(self, **body):
        body = {'start': 'Alpha, IL', 'finish': 'Omega, WI', **body}
        return self.api.post('/api/route/', body, format='json')


class RouteApiTests(RouteTestBase):

    def test_happy_path(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()

        self.assertEqual(data['start'], {'query': 'Alpha, IL', 'label': 'Alpha, IL',
                                         'lat': 38.0, 'lng': -90.0})
        self.assertEqual(data['finish']['label'], 'Omega, WI')
        self.assertAlmostEqual(data['route_miles'], 552.8, delta=0.5)
        self.assertEqual([s['opis_id'] for s in data['fuel_stops']], [1, 2])

        first = data['fuel_stops'][0]
        self.assertEqual(first['location'], {'lat': 38.1, 'lng': -90.02, 'precision': 'city'})
        self.assertEqual(first['route_point']['lng'], -90.0)
        self.assertAlmostEqual(first['route_point']['mile_marker'], 7.0, delta=1)
        self.assertFalse(first['assumed'])
        self.assertFalse(data['assumed_origin_fill'])

        geo = data['route_geojson']
        self.assertEqual(geo['type'], 'LineString')
        self.assertEqual(geo['coordinates'][0], [-90.0, 38.0])
        self.assertEqual(len(geo['coordinates']), int(data['route_miles']) + 2)  # ~1 point per mile

        meta = data['meta']
        self.assertEqual((meta['external_api_calls'], meta['cache_hit']), (1, False))
        self.assertEqual((meta['stop_penalty_dollars'], meta['corridor_miles']), (0.0, 10.0))
        self.assertEqual(meta['ors_profile'], 'driving-hgv')
        self.assertEqual(set(meta['timings']),
                         {'resolve_ms', 'cache_get_ms', 'routing_ms', 'corridor_ms', 'optimize_ms'})
        self.assertEqual(self.ors.calls, 1)

    def test_distances_and_gallons_reconcile(self):
        data = self.post().json()
        stops = data['fuel_stops']
        # Top-level detour is the round trip to each stop made.
        self.assertAlmostEqual(data['detour_miles'], 2 * sum(s['detour_miles'] for s in stops),
                               delta=0.1 * len(stops) + 0.05)
        self.assertAlmostEqual(data['total_driving_miles'],
                               data['route_miles'] + data['detour_miles'], delta=0.11)
        self.assertAlmostEqual(data['total_gallons'], data['total_driving_miles'] / 10, delta=0.02)
        self.assertAlmostEqual(data['total_fuel_cost'], sum(s['cost'] for s in stops),
                               delta=0.01 * len(stops))
        # Fuel cost only: never includes the stop penalty.
        with_penalty = self.post(stop_penalty=5).json()
        self.assertAlmostEqual(with_penalty['total_fuel_cost'],
                               sum(s['cost'] for s in with_penalty['fuel_stops']),
                               delta=0.01 * len(with_penalty['fuel_stops']))

    def test_penalty_defaults_to_zero_and_explicit_value_reaches_optimizer(self):
        with mock.patch.object(planner, 'optimize', wraps=planner.optimize) as spy:
            self.post()
            self.assertEqual(spy.call_args.kwargs['stop_penalty'], 0.0)
            data = self.post(stop_penalty=5).json()
            self.assertEqual(spy.call_args.kwargs['stop_penalty'], 5.0)
        self.assertEqual(data['meta']['stop_penalty_dollars'], 5.0)

    def test_bad_locations_are_400_and_skip_routing(self):
        for body, field in (({'start': 'Atlantis, IL'}, 'start'),
                            ({'finish': 'Toronto, ON'}, 'finish'),
                            ({'start': '51.5074,-0.1278'}, 'start')):
            response = self.post(**body)
            self.assertEqual(response.status_code, 400)
            error = response.json()['error']
            self.assertEqual((error['code'], error['field']), ('invalid_location', field))
        self.assertEqual(self.ors.calls, 0)

    def test_foreign_coordinates_inside_the_old_boxes_are_400(self):
        for coords in ('43.6532,-79.3832', '49.2827,-123.1207', '42.3149,-83.0364',
                       '32.5149,-117.0382'):
            response = self.post(start=coords)
            self.assertEqual(response.status_code, 400, coords)
            self.assertIn('outside the USA', response.json()['error']['message'])
        self.assertEqual(self.ors.calls, 0)

    def test_invalid_fields_are_400(self):
        response = self.api.post('/api/route/', {'start': 'Alpha, IL'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('finish', response.json()['error']['fields'])
        response = self.post(corridor_miles=500, stop_penalty=-1)
        self.assertEqual(set(response.json()['error']['fields']), {'corridor_miles', 'stop_penalty'})

    def test_gap_longer_than_range_is_422(self):
        FuelStation.objects.filter(opis_id__in=[2, 3]).delete()
        response = self.post()
        self.assertEqual(response.status_code, 422)
        error = response.json()['error']
        self.assertEqual(error['code'], 'no_feasible_plan')
        self.assertAlmostEqual(error['gap']['from_mile'], 7.0, delta=1)
        self.assertAlmostEqual(error['gap']['to_mile'], 552.8, delta=0.5)

    def test_routing_errors_pass_through_their_status(self):
        self.ors.error = RoutingError('Routing rate limit reached. Please try again later.',
                                      status=503)
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'routing_error')
        self.assertIn('rate limit', response.json()['error']['message'])

    def test_assumed_origin_fill(self):
        FuelStation.objects.filter(opis_id=1).delete()
        station(4, 37.5, -89.0, '3.300', 'Off Route')  # nearest to Alpha, far from the route
        data = self.post().json()
        self.assertTrue(data['assumed_origin_fill'])
        first = data['fuel_stops'][0]
        self.assertEqual((first['opis_id'], first['assumed']), (4, True))
        self.assertEqual(first['route_point'], {'lat': 38.0, 'lng': -90.0, 'mile_marker': 0.0})
        self.assertEqual(first['detour_miles'], 0.0)

        page = self.api.get('/api/route/map/', {'start': 'Alpha, IL', 'finish': 'Omega, WI'})
        self.assertContains(page, '"assumed": true')
        self.assertContains(page, 'assumed origin fill')

    def test_map_page(self):
        page = self.api.get('/api/route/map/', {'start': 'Alpha, IL', 'finish': 'Omega, WI',
                                                 'stop_penalty': '5'})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page['Content-Type'], 'text/html; charset=utf-8')
        self.assertContains(page, 'leaflet.min.js')
        self.assertContains(page, 'id="route-data"')
        self.assertContains(page, 'Alpha, IL &rarr; Omega, WI')
        self.assertContains(page, '"stop_penalty_dollars": 5.0')

    def test_map_page_error(self):
        page = self.api.get('/api/route/map/', {'start': 'Atlantis, IL', 'finish': 'Omega, WI'})
        self.assertEqual(page.status_code, 400)
        self.assertContains(page, 'Could not find', status_code=400)


class RouteCacheTests(RouteTestBase):
    def test_repeat_request_is_a_hit_with_fresh_meta(self):
        first = self.post().json()
        second = self.post().json()
        self.assertEqual(self.ors.calls, 1)
        self.assertEqual((first['meta']['cache_hit'], first['meta']['external_api_calls']), (False, 1))
        self.assertEqual((second['meta']['cache_hit'], second['meta']['external_api_calls']), (True, 0))
        self.assertNotIn('routing_ms', second['meta']['timings'])
        self.assertEqual(second['meta']['stop_penalty_dollars'], 0.0)
        for field in ('fuel_stops', 'total_fuel_cost', 'route_geojson', 'total_gallons'):
            self.assertEqual(first[field], second[field])

    def test_equivalent_spelling_hits_and_echoes_its_own_query(self):
        self.post(start='Alpha, IL', finish='Omega, WI')
        data = self.post(start='alpha, illinois', finish='Omega, Wisconsin, USA').json()
        self.assertTrue(data['meta']['cache_hit'])
        self.assertEqual(self.ors.calls, 1)
        self.assertEqual(data['start'], {'query': 'alpha, illinois', 'label': 'Alpha, IL',
                                         'lat': 38.0, 'lng': -90.0})
        self.assertEqual(data['finish']['query'], 'Omega, Wisconsin, USA')

    def test_data_version_bump_misses(self):
        self.post()
        DataVersion.bump()
        data = self.post().json()
        self.assertFalse(data['meta']['cache_hit'])
        self.assertEqual(self.ors.calls, 2)

    def test_penalty_corridor_and_profile_are_part_of_the_key(self):
        self.post()
        self.assertFalse(self.post(stop_penalty=5).json()['meta']['cache_hit'])
        self.assertFalse(self.post(corridor_miles=20).json()['meta']['cache_hit'])
        self.ors.profile = 'driving-car'
        data = self.post().json()
        self.assertFalse(data['meta']['cache_hit'])
        self.assertEqual(data['meta']['ors_profile'], 'driving-car')
        self.assertEqual(self.ors.calls, 4)
        self.assertFalse(self.post(stop_penalty=5).json()['meta']['cache_hit'])  # car + $5 is new
        self.assertTrue(self.post().json()['meta']['cache_hit'])

    def test_errors_are_not_cached(self):
        self.ors.error = RoutingError('Routing rate limit reached.', status=503)
        self.assertEqual(self.post().status_code, 503)
        self.ors.error = None
        data = self.post().json()
        self.assertFalse(data['meta']['cache_hit'])
        self.assertEqual(self.ors.calls, 2)

    def test_map_page_uses_the_same_cache(self):
        self.post()
        page = self.api.get('/api/route/map/', {'start': 'Alpha, IL', 'finish': 'Omega, WI'})
        self.assertContains(page, '"cache_hit": true')
        self.assertEqual(self.ors.calls, 1)
