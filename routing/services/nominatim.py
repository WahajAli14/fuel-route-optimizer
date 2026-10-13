"""Minimal Nominatim client for the one-time loader fallback.

Usage policy (https://operations.osmfoundation.org/policies/nominatim/):
max 1 request/second, an identifying User-Agent, and cache the results.
We only call it from `load_fuel_prices` for station cities the Census files
don't list, store every answer (including misses) in CityCoord, and never
call it while serving an API request.
"""

import time

import requests

SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
MIN_INTERVAL_SECONDS = 1.1


class NominatimClient:
    def __init__(self, user_agent, timeout=10, session=None):
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = user_agent
        self.timeout = timeout
        self._last_call = 0.0

    def _throttle(self):
        wait = self._last_call + MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def geocode_city(self, city, state):
        """Return (lat, lng) for a US settlement, or None if not found.

        The result must be in the requested state, so "Jean, NV" can't resolve
        to a Jean somewhere else.
        """
        self._throttle()
        response = self.session.get(SEARCH_URL, timeout=self.timeout, params={
            'q': f'{city}, {state}',
            'countrycodes': 'us',
            'featureType': 'settlement',
            'addressdetails': 1,
            'format': 'jsonv2',
            'limit': 5,
        })
        response.raise_for_status()
        for result in response.json():
            region = result.get('address', {}).get('ISO3166-2-lvl4', '')
            if region.upper() == f'US-{state.upper()}':
                return float(result['lat']), float(result['lon'])
        return None
