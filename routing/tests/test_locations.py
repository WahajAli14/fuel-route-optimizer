from django.test import TestCase

from routing.models import CityCoord, DataVersion
from routing.services.locations import LocationError, LocationResolver, in_us, parse_state
from routing.services.places import normalize_city


class ParseStateTests(TestCase):
    def test_codes_and_names(self):
        self.assertEqual(parse_state('il'), 'IL')
        self.assertEqual(parse_state('Illinois'), 'IL')
        self.assertEqual(parse_state('new york'), 'NY')
        self.assertIsNone(parse_state('Ontario'))

    def test_us_boxes(self):
        self.assertTrue(in_us(41.88, -87.63))   # Chicago
        self.assertTrue(in_us(61.22, -149.90))  # Anchorage
        self.assertTrue(in_us(21.31, -157.86))  # Honolulu
        self.assertFalse(in_us(51.51, -0.13))   # London


# Inside the old rectangles but outside the US: the boundary polygon must reject them.
FOREIGN = {
    'Toronto': (43.6532, -79.3832),
    'Vancouver': (49.2827, -123.1207),
    'Windsor, ON': (42.3149, -83.0364),   # across the river from Detroit
    'Tijuana': (32.5149, -117.0382),
}
# US cities, many on the border or a coast.
US_CITIES = {
    'Detroit': (42.3314, -83.0458),
    'Buffalo': (42.8864, -78.8784),
    'Seattle': (47.6062, -122.3321),
    'San Diego': (32.7157, -117.1611),
    'El Paso': (31.7619, -106.4850),
    'Key West': (24.5551, -81.7800),
    'Anchorage': (61.2181, -149.9003),
    'Honolulu': (21.3069, -157.8583),
}


class BoundaryTests(TestCase):
    def test_foreign_border_cities_are_rejected(self):
        resolver = LocationResolver()
        for name, (lat, lng) in FOREIGN.items():
            with self.subTest(name):
                self.assertFalse(in_us(lat, lng))
                with self.assertRaises(LocationError) as ctx:
                    resolver.resolve(f'{lat},{lng}')
                self.assertIn('outside the USA', ctx.exception.message)

    def test_us_border_and_coastal_cities_are_accepted(self):
        resolver = LocationResolver()
        for name, (lat, lng) in US_CITIES.items():
            with self.subTest(name):
                self.assertTrue(in_us(lat, lng))
                self.assertEqual(resolver.resolve(f'{lat},{lng}').source, 'coordinates')


class LocationResolverTests(TestCase):
    def setUp(self):
        # The index is cached on the class; start each test from the DB.
        LocationResolver._index = None
        LocationResolver._version = None
        self.addCleanup(setattr, LocationResolver, '_index', None)
        CityCoord.objects.create(state='IL', key=normalize_city('Chicago'), name='Chicago',
                                 lat=41.8375, lng=-87.6866, source=CityCoord.SOURCE_CENSUS_PLACE)
        CityCoord.objects.create(state='NV', key=normalize_city('Ghost Town'), name='Ghost Town',
                                 source=CityCoord.SOURCE_NOT_FOUND)
        self.resolver = LocationResolver()

    def assert_chicago(self, text):
        location = self.resolver.resolve(text)
        self.assertEqual((location.label, location.lat, location.lng, location.state, location.source),
                         ('Chicago, IL', 41.8375, -87.6866, 'IL', CityCoord.SOURCE_CENSUS_PLACE))

    def test_city_and_state_code(self):
        self.assert_chicago('Chicago, IL')

    def test_city_and_state_name(self):
        self.assert_chicago('Chicago, Illinois')

    def test_trailing_country_is_ignored(self):
        self.assert_chicago('Chicago, IL, USA')
        self.assert_chicago('chicago,il, United States')

    def test_lat_lng(self):
        location = self.resolver.resolve(' 41.8781 , -87.6298 ')
        self.assertEqual((location.lat, location.lng, location.source), (41.8781, -87.6298, 'coordinates'))

    def assert_error(self, text, message):
        with self.assertRaises(LocationError) as ctx:
            self.resolver.resolve(text)
        self.assertIn(message, ctx.exception.message)

    def test_non_us_coordinates(self):
        self.assert_error('51.5074,-0.1278', 'outside the USA')

    def test_out_of_range_coordinates(self):
        self.assert_error('95,-87.6', 'not a valid lat,lng')

    def test_unknown_state(self):
        self.assert_error('Toronto, ON', 'not a US state')

    def test_unknown_city(self):
        self.assert_error('Atlantis, IL', 'Could not find "Atlantis" in Illinois')

    def test_city_without_coordinates_is_unknown(self):
        self.assert_error('Ghost Town, NV', 'Could not find')

    def test_empty_input(self):
        for text in ('', '   ', None):
            self.assert_error(text, 'Location is required')

    def test_missing_state(self):
        self.assert_error('Chicago', 'Use "City, ST"')

    def test_index_rebuilds_when_data_version_changes(self):
        self.assert_error('Springfield, IL', 'Could not find')
        CityCoord.objects.create(state='IL', key=normalize_city('Springfield'), name='Springfield',
                                 lat=39.78, lng=-89.65, source=CityCoord.SOURCE_CENSUS_PLACE)
        DataVersion.bump()
        self.assertEqual(self.resolver.resolve('Springfield, IL').lat, 39.78)
