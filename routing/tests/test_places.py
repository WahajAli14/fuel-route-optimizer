from django.test import SimpleTestCase

from routing.services.places import (
    Place, PlaceIndex, cousub_name, display_city, normalize_city, place_names,
)


class NormalizeCityTests(SimpleTestCase):
    def test_spelling_variants_share_a_key(self):
        self.assertEqual(normalize_city('St. Louis'), normalize_city('Saint Louis'))
        self.assertEqual(normalize_city('Fort Worth'), normalize_city('Ft Worth'))
        self.assertEqual(normalize_city('Mount Vernon'), normalize_city('Mt. Vernon'))

    def test_accents_case_and_whitespace(self):
        self.assertEqual(normalize_city('  Cañon   City '), 'canon city')

    def test_display_city(self):
        self.assertEqual(display_city('  BIG   cabin  '), 'Big Cabin')


class PlaceNamesTests(SimpleTestCase):
    def test_strips_only_the_suffix_for_the_lsad_code(self):
        self.assertEqual(place_names('Chicago city', '25'), ('Chicago', []))
        self.assertEqual(place_names('Abanda CDP', '57'), ('Abanda', []))
        self.assertEqual(place_names('Kansas City city', '25')[0], 'Kansas City')
        self.assertEqual(place_names('Carson City', '00')[0], 'Carson City')
        self.assertEqual(place_names('Juneau city and borough', '53')[0], 'Juneau')

    def test_parenthetical_alternate_becomes_alias(self):
        self.assertEqual(place_names('San Buenaventura (Ventura) city', '25'),
                         ('San Buenaventura', ['Ventura']))

    def test_consolidated_governments(self):
        self.assertEqual(place_names('Indianapolis city (balance)', '00'), ('Indianapolis', []))
        self.assertEqual(
            place_names('Nashville-Davidson metropolitan government (balance)', '00'),
            ('Nashville-Davidson', ['Nashville']))
        self.assertEqual(
            place_names('Louisville/Jefferson County metro government (balance)', '00'),
            ('Louisville/Jefferson County', ['Louisville']))

    def test_trailing_city_alias(self):
        self.assertEqual(place_names('Boise City city', '25'), ('Boise City', ['Boise']))

    def test_cousub_names(self):
        self.assertEqual(cousub_name('Edison township'), 'Edison')
        self.assertEqual(cousub_name('Canton charter township'), 'Canton')
        self.assertEqual(cousub_name('Bow town'), 'Bow')
        self.assertIsNone(cousub_name('Autaugaville CCD'))
        self.assertIsNone(cousub_name('District 5'))


def make_place(name, state='XX', rank=(2, 1, True, 0), lat=1.0):
    return Place(state=state, name=name, lat=lat, lng=2.0, source='test', rank=rank)


class PlaceIndexTests(SimpleTestCase):
    def test_higher_rank_wins_a_shared_key(self):
        index = PlaceIndex([
            make_place('Springfield', rank=(2, 1, False, 999), lat=1.0),   # CDP
            make_place('Springfield', rank=(2, 1, True, 10), lat=2.0),     # incorporated
            make_place('Springfield', rank=(1, 1, True, 10_000), lat=3.0),  # county subdivision
        ])
        self.assertEqual(index.lookup('springfield', 'xx').lat, 2.0)

    def test_compact_fallback_matches_spacing_variants(self):
        index = PlaceIndex([make_place('DeForest')])
        self.assertIsNotNone(index.lookup('De Forest', 'XX'))
        self.assertIsNone(index.lookup('De Forest', 'YY'))
