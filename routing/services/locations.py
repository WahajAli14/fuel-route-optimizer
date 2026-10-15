"""Resolve request start/finish text to coordinates without any API call.

Accepted inputs:
    "Chicago, IL"            city + state code
    "Chicago, Illinois"      city + state name
    "Chicago, IL, USA"       trailing country is ignored
    "41.8781,-87.6298"       raw lat,lng (must be inside the US)
"""

import re
import threading
from dataclasses import dataclass

from ..models import DataVersion
from . import us_boundary
from .city_index import load_city_index

US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California',
    'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware', 'DC': 'District of Columbia',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois',
    'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana',
    'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan',
    'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana',
    'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey',
    'NM': 'New Mexico', 'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota',
    'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania',
    'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee',
    'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}
_COUNTRY_SUFFIXES = {'usa', 'us', 'u.s.', 'u.s.a.', 'united states', 'united states of america'}
_LATLNG_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$')

# (min_lat, max_lat, min_lng, max_lng): lower 48, Alaska, Hawaii. A fast pre-filter
# only: the boxes also cover parts of Canada and Mexico.
_US_BOXES = [
    (24.4, 49.5, -125.0, -66.9),
    (51.0, 71.5, -180.0, -129.0),
    (18.9, 22.3, -160.3, -154.8),
]


class LocationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Location:
    query: str
    label: str
    lat: float
    lng: float
    state: str = ''
    source: str = ''


def in_us(lat, lng):
    """Inside the US boundary (boxes first, then the Census polygon)."""
    if not any(a <= lat <= b and c <= lng <= d for a, b, c, d in _US_BOXES):
        return False
    return us_boundary.contains(lat, lng)


def parse_state(text):
    text = text.strip().rstrip('.')
    if text.upper() in US_STATES:
        return text.upper()
    return _STATE_BY_NAME.get(text.lower())


class LocationResolver:
    """Resolves locations against an in-memory CityCoord index.

    The index is built once per process and rebuilt when the data version
    changes (every loader run bumps it).
    """

    _lock = threading.Lock()
    _index = None
    _version = None

    @classmethod
    def index(cls):
        version = DataVersion.current()
        if cls._index is None or cls._version != version:
            with cls._lock:
                if cls._index is None or cls._version != version:
                    cls._index = load_city_index()
                    cls._version = version
        return cls._index

    def resolve(self, text):
        if not isinstance(text, str) or not text.strip():
            raise LocationError('Location is required, e.g. "Chicago, IL".')
        query = ' '.join(text.split())

        match = _LATLNG_RE.match(query)
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise LocationError(f'"{query}" is not a valid lat,lng pair.')
            if not in_us(lat, lng):
                raise LocationError(f'"{query}" is outside the USA. Start and finish must both be in the USA.')
            return Location(query=query, label=f'{lat:.5f}, {lng:.5f}', lat=lat, lng=lng,
                            source='coordinates')

        parts = [p.strip() for p in query.split(',') if p.strip()]
        if parts and parts[-1].lower() in _COUNTRY_SUFFIXES:
            parts = parts[:-1]
        if len(parts) < 2:
            raise LocationError(f'Could not read "{query}". Use "City, ST", e.g. "Chicago, IL".')

        city, state_text = ', '.join(parts[:-1]), parts[-1]
        state = parse_state(state_text)
        if state is None:
            raise LocationError(
                f'"{state_text}" is not a US state. Start and finish must both be in the USA.')

        place = self.index().lookup(city, state)
        if place is None:
            raise LocationError(f'Could not find "{city}" in {US_STATES[state]}.')
        return Location(query=query, label=f'{place.name}, {state}', lat=place.lat,
                        lng=place.lng, state=state, source=place.source)
