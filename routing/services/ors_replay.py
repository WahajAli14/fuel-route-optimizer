"""Serve saved ORS directions responses instead of calling the API.

The fixtures in routing/tests/fixtures are raw responses from the three live
calls made while measuring timings. ReplaySession plugs into the real
ORSClient (as its HTTP session), so parsing runs exactly as in production
while no request leaves the machine. Used by tests and `replay_routes`.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures'

# fixture name -> (start, finish) as they were requested
ROUTES = {
    'chicago_los_angeles': ('Chicago, IL', 'Los Angeles, CA'),
    'new_york_miami': ('New York, NY', 'Miami, FL'),
    'dallas_houston': ('Dallas, TX', 'Houston, TX'),
}


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class ReplaySession:
    """Stands in for requests.Session: every POST returns the same saved response."""

    def __init__(self, name):
        self.name = name
        self.payload = json.loads((FIXTURE_DIR / f'ors_{name}.json').read_text())
        self.posts = 0

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts += 1
        return _Response(self.payload)
