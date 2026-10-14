from decimal import Decimal

from django.test import SimpleTestCase

from routing.services.fuel_csv import clean_rows


def row(opis_id, price, name='PILOT #1', city='Tomah  ', state='WI', rack='420'):
    return {
        'OPIS Truckstop ID': str(opis_id), 'Truckstop Name': name,
        'Address': ' I-94, EXIT 143 ', 'City': city, 'State': state,
        'Rack ID': rack, 'Retail Price': str(price),
    }


class CleanRowsTests(SimpleTestCase):
    def test_strips_whitespace_and_title_cases_city(self):
        stations, _ = clean_rows([row(1, '3.1', city='  BIG   CABIN   ')])
        self.assertEqual(stations[0].city, 'Big Cabin')
        self.assertEqual(stations[0].address, 'I-94, EXIT 143')

    def test_drops_canadian_provinces(self):
        stations, stats = clean_rows([row(1, '3.1'), row(2, '3.2', state='ON'), row(3, '3.3', state='bc')])
        self.assertEqual([s.opis_id for s in stations], [1])
        self.assertEqual(stats.canadian_dropped, 2)

    def test_merges_duplicates_with_mean_min_max_and_common_name(self):
        stations, stats = clean_rows([
            row(7, '3.00', name='PILOT #1243'),
            row(7, '3.30', name='PILOT TRAVEL CENTER #1243'),
            row(7, '3.60', name='PILOT TRAVEL CENTER #1243'),
        ])
        (s,) = stations
        self.assertEqual(s.price, Decimal('3.3000'))
        self.assertEqual((s.min_price, s.max_price), (Decimal('3.0000'), Decimal('3.6000')))
        self.assertEqual(s.name, 'PILOT TRAVEL CENTER #1243')
        self.assertEqual(s.row_count, 3)
        self.assertEqual(stats.duplicate_rows_merged, 2)

    def test_name_tie_keeps_first_spelling(self):
        stations, _ = clean_rows([row(7, '3', name='A'), row(7, '3', name='B')])
        self.assertEqual(stations[0].name, 'A')

    def test_invalid_rows_are_counted_not_fatal(self):
        stations, stats = clean_rows([row(1, '3.1'), row('x', '3.1'), row(2, 'n/a'), row(3, '-1')])
        self.assertEqual([s.opis_id for s in stations], [1])
        self.assertEqual(stats.invalid_dropped, 3)
        self.assertEqual([line for line, _ in stats.invalid_rows], [3, 4, 5])
