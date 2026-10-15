"""Endpoint tests on real ORS geometry (saved fixtures), with synthetic stations."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from routing.models import CityCoord, FuelStation
from routing.services import planner
from routing.services.corridor import StationIndex
from routing.services.locations import LocationResolver
from routing.services.ors_client import ORSClient, decode_polyline
from routing.services.ors_replay import ReplaySession
from routing.services.places import normalize_city


class ReplayEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for cls, attrs in ((LocationResolver, ('_index', '_version')),
                           (StationIndex, ('_cached', '_version'))):
            for attr in attrs:
                setattr(cls, attr, None)
                self.addCleanup(setattr, cls, attr, None)

        self.session = ReplaySession('chicago_los_angeles')
        coords = decode_polyline(self.session.payload['routes'][0]['geometry'])
        start, finish = coords[0], coords[-1]
        for name, state, (lat, lng) in (('Chicago', 'IL', start), ('Los Angeles', 'CA', finish)):
            CityCoord.objects.create(state=state, key=normalize_city(name), name=name, lat=lat,
                                     lng=lng, source=CityCoord.SOURCE_CENSUS_PLACE)
        # A station every ~350 polyline points, slightly off the road, prices varying.
        for k, i in enumerate(range(0, len(coords), 350)):
            lat, lng = coords[i]
            price = Decimal('3.000') + Decimal(k % 5) / 10
            FuelStation.objects.create(
                opis_id=k + 1, name=f'Stop {k + 1}', address='I-80, EXIT 1', city='Town',
                state='NE', rack_id=1, price=price, min_price=price, max_price=price,
                lat=lat + 0.01, lng=lng + 0.01)

        self.client_ors = ORSClient(api_key='replay', session=self.session)
        patcher = mock.patch.object(planner, 'client_from_settings', return_value=self.client_ors)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = APIClient()

    def test_real_geometry_end_to_end(self):
        response = self.api.post('/api/route/', {'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'},
                                 format='json')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertAlmostEqual(data['route_miles'], 2027.4, delta=0.5)
        self.assertAlmostEqual(data['meta']['ors_summary_miles'], 2027.4, delta=0.1)
        self.assertEqual(data['meta']['external_api_calls'], 1)
        self.assertIn('There may be restrictions on some roads', data['meta']['route_warnings'])
        self.assertGreaterEqual(len(data['fuel_stops']), 5)  # 2,027 mi on a 500 mi range
        self.assertAlmostEqual(data['total_gallons'], data['total_driving_miles'] / 10, delta=0.02)
        self.assertAlmostEqual(data['detour_miles'],
                               2 * sum(s['detour_miles'] for s in data['fuel_stops']),
                               delta=0.1 * len(data['fuel_stops']) + 0.05)
        miles = [s['route_point']['mile_marker'] for s in data['fuel_stops']]
        self.assertEqual(miles, sorted(miles))
        self.assertTrue(all(b - a <= 500 for a, b in zip([0] + miles, miles + [data['route_miles']])))

        page = self.api.get('/api/route/map/', {'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '"cache_hit": true')
        self.assertEqual(self.session.posts, 1)
