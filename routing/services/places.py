"""US place lookup built from the Census Gazetteer files.

Two public-domain Gazetteer files give one internal point (lat/lng) per place:

* Places: incorporated cities/towns/villages and census-designated places.
* County subdivisions: townships and New England / NY / NJ / PA towns
  ("Edison township", "Bow town") that the Places file doesn't list.

Names carry a legal suffix ("Chicago city", "Abanda CDP"), sometimes an
alternate name in parentheses ("San Buenaventura (Ventura) city"), and
consolidated governments look like
"Louisville/Jefferson County metro government (balance)". We reduce every
name to a lookup key so free-text city names from the fuel CSV and from API
requests can be matched against it.
"""

import csv
import re
import unicodedata
from dataclasses import dataclass

SOURCE_PLACE = 'census_place'
SOURCE_COUSUB = 'census_cousub'

CDP_LSAD = '57'
CONSOLIDATED_LSADS = {'00', 'UG', 'MG', 'CG', 'UC', 'CN'}

# The legal suffix the Places file appends to NAME for each LSAD code. Stripping
# only the suffix for the row's own code keeps "Kansas City city" -> "Kansas City"
# and leaves "Carson City" (code 00, no suffix) alone.
LSAD_SUFFIXES = {
    '57': 'CDP', '25': 'city', '43': 'town', '47': 'village', '21': 'borough',
    '55': 'comunidad', '62': 'zona urbana', '53': 'city and borough',
    '37': 'municipality', 'UG': 'unified government',
    'CG': 'consolidated government', 'MG': 'metropolitan government',
    'UC': 'urban county', 'CN': 'corporation',
}
# Code 00 (consolidated city "balance" parts) carries a government phrase, if anything.
_GOVERNMENT_RE = re.compile(
    r'\s+(?:unified|consolidated|metro|metropolitan) government$', re.IGNORECASE)

# County subdivisions that are real towns. Statistical areas (CCD, precinct,
# district, barrio, UT, numbered divisions) are skipped.
_COUSUB_SUFFIX_RE = re.compile(
    r'\s+(charter township|township|town|city|borough|village|plantation)$', re.IGNORECASE)

_PAREN_RE = re.compile(r'\s*\(([^)]*)\)')

# Word-level spellings that differ between sources ("St. Louis" vs "Saint Louis").
_WORD_ALIASES = {
    'saint': 'st', 'ste': 'st', 'sainte': 'st',
    'fort': 'ft', 'mount': 'mt', 'mountain': 'mtn',
    'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
}


def normalize_city(name):
    """Reduce a city name to a comparison key: 'St. Louis ' -> 'st louis'."""
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    name = name.lower().replace('&', ' and ')
    name = re.sub(r"[.'`]", '', name)
    name = re.sub(r'[^a-z0-9]+', ' ', name)
    words = [_WORD_ALIASES.get(w, w) for w in name.split()]
    return ' '.join(words)


def compact_key(key):
    """'de forest' and 'deforest' both become 'deforest'."""
    return key.replace(' ', '')


def display_city(name):
    """Clean a raw city string for display: '  big cabin ' -> 'Big Cabin'."""
    return ' '.join(name.split()).title()


def place_names(raw_name, lsad):
    """Return (primary, aliases) display names for a Places-file NAME value."""
    is_balance = '(balance)' in raw_name
    name = raw_name.replace('(balance)', '').strip()
    alternates = _PAREN_RE.findall(name)
    name = _PAREN_RE.sub('', name).strip()

    suffix = LSAD_SUFFIXES.get(lsad)
    if suffix and name.lower().endswith(' ' + suffix.lower()):
        name = name[:-len(suffix) - 1].strip()
    elif lsad == '00':
        name = _GOVERNMENT_RE.sub('', name).strip()
        if is_balance and name.lower().endswith(' city'):  # "Indianapolis city (balance)"
            name = name[:-5].strip()

    aliases = [a.strip() for a in alternates if a.strip()]
    if lsad in CONSOLIDATED_LSADS:
        # "Louisville/Jefferson County" -> "Louisville", "Athens-Clarke County" -> "Athens".
        first = re.split(r'[/,-]', name)[0].strip()
        if first and first != name:
            aliases.append(first)
    if name.lower().endswith(' city'):
        # Official "Boise City" is usually called "Boise".
        aliases.append(name[:-5].strip())
    return name, aliases


def cousub_name(raw_name):
    """Return the town name for a county-subdivision NAME, or None to skip it."""
    match = _COUSUB_SUFFIX_RE.search(raw_name)
    if not match:
        return None
    return raw_name[:match.start()].strip() or None


@dataclass(frozen=True)
class Place:
    state: str
    name: str
    lat: float
    lng: float
    source: str
    # Higher ranks win when two places share a key in one state.
    rank: tuple


def _read_rows(path):
    with open(path, newline='', encoding='utf-8') as f:
        header = f.readline()
        delimiter = '|' if '|' in header else '\t'
        fields = [h.strip() for h in header.split(delimiter)]
        for row in csv.reader(f, delimiter=delimiter):
            yield {k: v.strip() for k, v in zip(fields, row)}


def read_places(path):
    """Yield Place entries (primary names and aliases) from the Places file."""
    for rec in _read_rows(path):
        primary, aliases = place_names(rec['NAME'], rec['LSAD'])
        common = dict(state=rec['USPS'], lat=float(rec['INTPTLAT']),
                      lng=float(rec['INTPTLONG']), source=SOURCE_PLACE)
        is_incorporated = rec['LSAD'] != CDP_LSAD
        land = int(rec['ALAND'] or 0)
        yield Place(name=primary, rank=(2, 1, is_incorporated, land), **common)
        for alias in aliases:
            yield Place(name=alias, rank=(2, 0, is_incorporated, land), **common)


def read_cousubs(path):
    """Yield Place entries for real towns/townships in the County Subdivisions file."""
    for rec in _read_rows(path):
        name = cousub_name(rec['NAME'])
        if name:
            yield Place(state=rec['USPS'], name=name, lat=float(rec['INTPTLAT']),
                        lng=float(rec['INTPTLONG']), source=SOURCE_COUSUB,
                        rank=(1, 1, True, int(rec['ALAND'] or 0)))


class PlaceIndex:
    """Look up (city, state) -> Place, preferring exact keys over compact ones.

    When several entries share a key in one state, the highest rank wins:
    Places file over county subdivisions, primary name over alias,
    incorporated over CDP, then larger land area.
    """

    def __init__(self, places):
        self.exact = {}
        self.compact = {}
        for place in places:
            key = normalize_city(place.name)
            self._offer(self.exact, (place.state, key), place)
            self._offer(self.compact, (place.state, compact_key(key)), place)

    @staticmethod
    def _offer(index, key, place):
        current = index.get(key)
        if current is None or place.rank > current.rank:
            index[key] = place

    def __len__(self):
        return len(self.exact)

    def lookup(self, city, state):
        key = normalize_city(city)
        state = state.strip().upper()
        return (self.exact.get((state, key))
                or self.compact.get((state, compact_key(key))))
