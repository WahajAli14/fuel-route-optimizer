import csv
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

import requests

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from routing.models import CityCoord, DataVersion, FuelStation

PLACES = """USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG
WI|5579900|x|x|Tomah city|25|A|100|0|0|0|43.98|-90.50
OK|4006400|x|x|Big Cabin town|43|A|100|0|0|0|36.54|-95.22
"""
COUSUBS = """USPS|GEOID|GEOIDFQ|ANSICODE|NAME|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG
NJ|3402320230|x|x|Edison township|A|100|0|0|0|40.50|-74.35
NJ|3402399999|x|x|Middlesex CCD|S|100|0|0|0|40.40|-74.40
"""
HEADER = ['OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City', 'State', 'Rack ID', 'Retail Price']


class LoaderCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        (self.data_dir / 'places.txt').write_text(PLACES)
        (self.data_dir / 'cousubs.txt').write_text(COUSUBS)
        overrides = override_settings(DATA_DIR=self.data_dir, CENSUS_PLACES_FILE='places',
                                      CENSUS_COUSUBS_FILE='cousubs')
        overrides.enable()
        self.addCleanup(overrides.disable)
        geocode = mock.patch('routing.services.nominatim.NominatimClient.geocode_city',
                             side_effect=self.fake_geocode)
        self.geocode = geocode.start()
        self.addCleanup(geocode.stop)

    @staticmethod
    def fake_geocode(city, state):
        return (39.99, -78.24) if city == 'Breezewood' else None

    def write_csv(self, rows, name='prices.csv'):
        path = self.data_dir / name
        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)
            writer.writerows(rows)
        return str(path)

    def run_cmd(self, *args):
        out = StringIO()
        call_command(*args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_fuel_load_requires_places(self):
        with self.assertRaisesMessage(CommandError, 'load_places'):
            self.run_cmd('load_fuel_prices', self.write_csv([]))

    def test_load_places_skips_statistical_subdivisions(self):
        self.run_cmd('load_places')
        self.assertEqual(
            set(CityCoord.objects.values_list('name', 'state', 'source')),
            {('Tomah', 'WI', 'census_place'), ('Big Cabin', 'OK', 'census_place'),
             ('Edison', 'NJ', 'census_cousub')},
        )

    def test_full_load_then_rerun(self):
        self.run_cmd('load_places')
        places_version = DataVersion.current()
        path = self.write_csv([
            [1, 'KWIK TRIP', 'I-94', 'TOMAH   ', 'WI', 420, '3.10'],
            [1, 'KWIK TRIP', 'I-94', 'TOMAH   ', 'WI', 420, '3.30'],
            [2, 'TA', 'I-76', 'Breezewood', 'PA', 1, '3.50'],
            [3, 'NOWHERE STOP', 'I-1', 'Ghost Town', 'NV', 1, '3.00'],
            [4, 'CANADA STOP', 'HWY-1', 'Calgary', 'AB', 1, '2.00'],
        ])
        out = self.run_cmd('load_fuel_prices', path)

        self.assertIn('Canadian rows dropped:     1', out)
        self.assertEqual(sorted(FuelStation.objects.values_list('opis_id', flat=True)), [1, 2])
        tomah = FuelStation.objects.get(opis_id=1)
        self.assertEqual((tomah.city, str(tomah.price), tomah.row_count, tomah.lat),
                         ('Tomah', '3.2000', 2, 43.98))
        self.assertEqual(FuelStation.objects.get(opis_id=2).lat, 39.99)
        self.assertEqual(CityCoord.objects.get(key='ghost town').source, CityCoord.SOURCE_NOT_FOUND)
        self.assertEqual(DataVersion.current(), places_version + 1)
        self.assertEqual(self.geocode.call_count, 2)

        # Rerun with a price change and a dropped station: no new geocoding.
        path = self.write_csv([[1, 'KWIK TRIP', 'I-94', 'Tomah', 'WI', 420, '2.90']], 'v2.csv')
        out = self.run_cmd('load_fuel_prices', path)
        self.assertEqual(self.geocode.call_count, 2)
        self.assertEqual(str(FuelStation.objects.get(opis_id=1).price), '2.9000')
        self.assertFalse(FuelStation.objects.filter(opis_id=2).exists())
        self.assertIn('Stations removed (not in CSV):     1', out)
        self.assertEqual(DataVersion.current(), places_version + 2)

    def test_retry_not_found_asks_again(self):
        self.run_cmd('load_places')
        path = self.write_csv([[3, 'NOWHERE STOP', 'I-1', 'Ghost Town', 'NV', 1, '3.00']])
        self.run_cmd('load_fuel_prices', path)
        self.run_cmd('load_fuel_prices', path)
        self.assertEqual(self.geocode.call_count, 1)
        self.run_cmd('load_fuel_prices', path, '--retry-not-found')
        self.assertEqual(self.geocode.call_count, 2)

    def test_station_in_csv_is_kept_when_its_city_fails_to_geocode(self):
        """Regression: pruning used to keep only stations located in this run."""
        self.run_cmd('load_places')
        price = Decimal('3.0000')
        FuelStation.objects.create(opis_id=5, name='OLD STOP', address='I-1', city='Tomah',
                                   state='WI', rack_id=1, price=price, min_price=price,
                                   max_price=price, lat=43.98, lng=-90.50)
        self.geocode.side_effect = requests.ConnectionError('Nominatim is down')
        # The station moved to a city we have never geocoded, and the lookup fails.
        path = self.write_csv([[5, 'OLD STOP', 'I-1', 'Erroville', 'WI', 1, '3.10'],
                               [1, 'KWIK TRIP', 'I-94', 'Tomah', 'WI', 420, '3.10']])
        out = self.run_cmd('load_fuel_prices', path)
        self.assertIn('Stations removed (not in CSV):     0', out)
        self.assertEqual(FuelStation.objects.get(opis_id=5).city, 'Tomah')  # previous row kept
        self.assertFalse(CityCoord.objects.filter(key='erroville').exists())  # not recorded as a miss

        # A station that is really gone from the CSV is still removed.
        self.geocode.side_effect = self.fake_geocode
        out = self.run_cmd('load_fuel_prices', self.write_csv(
            [[1, 'KWIK TRIP', 'I-94', 'Tomah', 'WI', 420, '3.10']], 'v2.csv'))
        self.assertIn('Stations removed (not in CSV):     1', out)

    def write_geocoded(self, rows):
        with open(self.data_dir / 'geocoded_cities.csv', 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['state', 'name', 'lat', 'lng', 'source'])
            writer.writerows(rows)

    def test_load_places_adds_geocoded_cities_without_overriding_census(self):
        self.write_geocoded([
            ['PA', 'Breezewood', 39.99, -78.24, 'nominatim'],
            ['NV', 'Ghost Town', '', '', 'not_found'],
            ['WI', 'Tomah', 1.0, 1.0, 'nominatim'],  # Census has Tomah: must not win
        ])
        version = DataVersion.current()
        out = self.run_cmd('load_places')
        self.assertIn('geocoded_cities.csv: 3 rows, 2 added', out)
        self.assertEqual(DataVersion.current(), version + 1)
        tomah = CityCoord.objects.get(state='WI', key='tomah')
        self.assertEqual((tomah.lat, tomah.source), (43.98, CityCoord.SOURCE_CENSUS_PLACE))
        self.assertEqual(CityCoord.objects.get(key='breezewood').source, CityCoord.SOURCE_NOMINATIM)
        self.assertEqual(CityCoord.objects.get(key='ghost town').source, CityCoord.SOURCE_NOT_FOUND)

        # With the file loaded, a full fuel load makes no Nominatim calls.
        out = self.run_cmd('load_fuel_prices', self.write_csv([
            [1, 'KWIK TRIP', 'I-94', 'Tomah', 'WI', 420, '3.10'],
            [2, 'TA', 'I-76', 'Breezewood', 'PA', 1, '3.50'],
            [3, 'NOWHERE STOP', 'I-1', 'Ghost Town', 'NV', 1, '3.00'],
        ]))
        self.assertEqual(self.geocode.call_count, 0)
        self.assertEqual(sorted(FuelStation.objects.values_list('opis_id', flat=True)), [1, 2])

        # Reloading places again keeps the same answers (idempotent).
        self.assertIn('3 rows, 0 added', self.run_cmd('load_places'))

    def test_export_round_trip(self):
        self.run_cmd('load_places')
        self.run_cmd('load_fuel_prices', self.write_csv([
            [2, 'TA', 'I-76', 'Breezewood', 'PA', 1, '3.50'],
            [3, 'NOWHERE STOP', 'I-1', 'Ghost Town', 'NV', 1, '3.00'],
        ]))
        out = self.run_cmd('export_geocoded_cities')
        self.assertIn('Wrote 2 cities', out)
        with open(self.data_dir / 'geocoded_cities.csv') as f:
            self.assertEqual(f.read().splitlines(), [
                'state,name,lat,lng,source',
                'NV,Ghost Town,,,not_found',
                'PA,Breezewood,39.99,-78.24,nominatim',
            ])
        self.assertNotIn(b'\r', (self.data_dir / 'geocoded_cities.csv').read_bytes())  # LF only
        CityCoord.objects.filter(source__in=['nominatim', 'not_found']).delete()
        self.run_cmd('load_places')
        self.assertEqual(CityCoord.objects.get(key='breezewood').lat, 39.99)
