from decimal import Decimal

import numpy as np
from django.test import SimpleTestCase, TestCase

from routing.models import DataVersion, FuelStation
from routing.services.corridor import (
    EARTH_RADIUS_MILES, StationIndex, chord_to_miles, haversine_miles, miles_to_chord,
    resample_route, stations_along, to_unit_vectors,
)

MILES_PER_DEGREE = np.pi / 180 * EARTH_RADIUS_MILES  # ~69.09

# Due north along -90 from lat 40 to 42: about 138 miles.
NORTH_ROUTE = [(40.0, -90.0), (41.0, -90.0), (42.0, -90.0)]


def make_station(opis_id, lat, lng, price='3.00'):
    price = Decimal(price)
    return FuelStation.objects.create(
        opis_id=opis_id, name=f'Stop {opis_id}', address='I-1, EXIT 1', city='Town', state='IL',
        rack_id=1, price=price, min_price=price, max_price=price, lat=lat, lng=lng)


class GeometryTests(SimpleTestCase):
    def test_chord_round_trip(self):
        for miles in (0.5, 10, 500, 2500):
            self.assertAlmostEqual(float(chord_to_miles(miles_to_chord(miles))), miles, places=6)

    def test_chord_matches_haversine(self):
        a = to_unit_vectors([41.88], [-87.63])[0]   # Chicago
        b = to_unit_vectors([34.05], [-118.24])[0]  # Los Angeles
        self.assertAlmostEqual(float(chord_to_miles(np.linalg.norm(a - b))),
                               float(haversine_miles(41.88, -87.63, 34.05, -118.24)), places=6)

    def test_one_degree_of_latitude(self):
        self.assertAlmostEqual(float(haversine_miles(40, -90, 41, -90)), MILES_PER_DEGREE, places=6)


class ResampleTests(SimpleTestCase):
    def test_points_every_mile_plus_the_end(self):
        route = resample_route(NORTH_ROUTE)
        total = 2 * MILES_PER_DEGREE
        self.assertAlmostEqual(route.total_miles, total, places=6)
        self.assertEqual(len(route.points), int(total) + 2)  # 0..138 plus the exact end
        self.assertTrue(np.allclose(np.diff(route.miles[:-1]), 1.0))
        self.assertEqual(tuple(route.points[0]), (40.0, -90.0))
        self.assertEqual(tuple(route.points[-1]), (42.0, -90.0))

    def test_interpolates_long_segments(self):
        route = resample_route([(40.0, -90.0), (41.0, -90.0)])
        mid = np.searchsorted(route.miles, MILES_PER_DEGREE / 2)
        self.assertAlmostEqual(route.points[mid][0], 40.0 + route.miles[mid] / MILES_PER_DEGREE, places=6)

    def test_custom_step(self):
        route = resample_route(NORTH_ROUTE, step_miles=10)
        self.assertEqual(list(route.miles[:3]), [0.0, 10.0, 20.0])

    def test_single_point_and_duplicate_points(self):
        self.assertEqual(resample_route([(40.0, -90.0)]).total_miles, 0.0)
        route = resample_route([(40.0, -90.0), (40.0, -90.0)])
        self.assertEqual((route.total_miles, len(route.points)), (0.0, 1))

    def test_empty_route_is_an_error(self):
        with self.assertRaises(ValueError):
            resample_route([])


class CorridorTests(TestCase):
    def setUp(self):
        StationIndex._cached = None
        StationIndex._version = None
        self.addCleanup(setattr, StationIndex, '_cached', None)

    def test_finds_stations_within_the_corridor_sorted_by_mile(self):
        make_station(1, 41.5, -90.1)     # ~5.2 mi east of the route, mile ~103.6
        make_station(2, 40.5, -90.05)    # ~2.6 mi, mile ~34.5
        make_station(3, 41.0, -90.3)     # ~15.6 mi: outside 10, inside 20
        make_station(4, 43.0, -90.0)     # past the finish by ~69 mi
        make_station(5, 34.05, -118.24)  # far away

        route, found = stations_along(NORTH_ROUTE, corridor_miles=10)
        self.assertEqual([c.station.opis_id for c in found], [2, 1])

        first = found[0]
        self.assertAlmostEqual(first.mile_marker, 0.5 * MILES_PER_DEGREE, delta=1.0)
        self.assertAlmostEqual(first.route_lat, 40.5, delta=1 / MILES_PER_DEGREE)
        self.assertEqual(first.route_lng, -90.0)
        self.assertAlmostEqual(first.detour_miles,
                               float(haversine_miles(40.5, -90.0, 40.5, -90.05)), delta=0.1)
        self.assertEqual(first.station.price, 3.0)

        _, wide = stations_along(NORTH_ROUTE, corridor_miles=20)
        self.assertEqual([c.station.opis_id for c in wide], [2, 3, 1])

    def test_station_at_the_finish_has_the_last_mile_marker(self):
        make_station(1, 42.0, -90.0)
        route, found = stations_along(NORTH_ROUTE, corridor_miles=10)
        self.assertEqual(found[0].mile_marker, route.total_miles)
        self.assertAlmostEqual(found[0].detour_miles, 0.0, places=6)

    def test_same_city_sorted_by_price_then_id(self):
        make_station(2, 41.0, -90.05, '3.10')
        make_station(1, 41.0, -90.05, '3.10')
        make_station(3, 41.0, -90.05, '2.90')
        _, found = stations_along(NORTH_ROUTE, corridor_miles=10)
        self.assertEqual([c.station.opis_id for c in found], [3, 1, 2])

    def test_no_stations(self):
        route, found = stations_along(NORTH_ROUTE, corridor_miles=10)
        self.assertEqual(found, [])
        self.assertGreater(route.total_miles, 0)

    def test_index_is_cached_until_the_data_version_changes(self):
        make_station(1, 41.0, -90.05)
        index = StationIndex.current()
        self.assertIs(StationIndex.current(), index)

        make_station(2, 40.5, -90.05)
        self.assertEqual(len(StationIndex.current().stations), 1)
        DataVersion.bump()
        self.assertEqual(len(StationIndex.current().stations), 2)
