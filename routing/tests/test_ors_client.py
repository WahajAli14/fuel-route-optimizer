from unittest import mock

import requests
from django.test import SimpleTestCase

from routing.services.ors_client import ORSClient, RoutingError, decode_polyline

# Google's documented example polyline: (38.5, -120.2), (40.7, -120.95), (43.252, -126.453).
POLYLINE = '_p~iF~ps|U_ulLnnqC_mqNvxq`@'


def fake_response(status, payload=None, text=''):
    response = mock.Mock(status_code=status, text=text)
    if payload is None:
        response.json.side_effect = ValueError('no JSON')
    else:
        response.json.return_value = payload
    return response


def ors_success():
    return {'routes': [{
        'geometry': POLYLINE,
        'summary': {'distance': 160934.4, 'duration': 5400.0},
        'segments': [{'steps': [
            {'name': 'Main Street'}, {'name': 'I 94'}, {'name': '-'},
            {'name': ''}, {'name': 'I 94'}, {'name': 'I 90'},
        ]}],
        'warnings': [{'code': 1, 'message': 'There may be restrictions on some roads'}],
    }]}


class DecodePolylineTests(SimpleTestCase):
    def test_decodes_google_example(self):
        self.assertEqual(decode_polyline(POLYLINE),
                         [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])

    def test_empty_string(self):
        self.assertEqual(decode_polyline(''), [])


class ORSClientTests(SimpleTestCase):
    def make_client(self, response=None, error=None):
        session = mock.Mock()
        if error is not None:
            session.post.side_effect = error
        else:
            session.post.return_value = response
        return ORSClient(api_key='test-key', session=session), session

    def test_success_builds_route(self):
        client, session = self.make_client(fake_response(200, ors_success()))
        route = client.route((41.88, -87.63), (34.05, -118.24))

        self.assertEqual(route.coords, [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])
        self.assertAlmostEqual(route.distance_miles, 100.0)
        self.assertEqual(route.duration_s, 5400.0)
        self.assertEqual(route.roads, ['Main Street', 'I 94', 'I 90'])
        self.assertEqual(route.warnings, ['There may be restrictions on some roads'])
        self.assertEqual(route.profile, 'driving-hgv')
        self.assertEqual(client.calls, 1)

        # ORS wants [lng, lat] and the key in the Authorization header.
        _, kwargs = session.post.call_args
        self.assertEqual(session.post.call_args[0][0],
                         'https://api.openrouteservice.org/v2/directions/driving-hgv')
        self.assertEqual(kwargs['json']['coordinates'], [[-87.63, 41.88], [-118.24, 34.05]])
        self.assertEqual(kwargs['headers'], {'Authorization': 'test-key'})

    def test_missing_key_makes_no_call(self):
        session = mock.Mock()
        client = ORSClient(api_key='', session=session)
        with self.assertRaises(RoutingError) as ctx:
            client.route((41.88, -87.63), (34.05, -118.24))
        self.assertEqual(ctx.exception.status, 503)
        session.post.assert_not_called()

    def assert_routing_error(self, client, status, text):
        with self.assertRaises(RoutingError) as ctx:
            client.route((41.88, -87.63), (34.05, -118.24))
        self.assertEqual(ctx.exception.status, status)
        self.assertIn(text, ctx.exception.message)
        return ctx.exception

    def test_401_rejected_key(self):
        client, _ = self.make_client(fake_response(401, {'error': 'Access to this API has been disallowed'}))
        self.assert_routing_error(client, 502, 'rejected the API key')

    def test_429_rate_limit(self):
        client, _ = self.make_client(fake_response(429, {'error': 'Rate limit exceeded'}))
        self.assert_routing_error(client, 503, 'rate limit')

    def test_no_route_error_code(self):
        client, _ = self.make_client(fake_response(404, {'error': {
            'code': 2010, 'message': 'Could not find routable point within a radius of 5000.0 meters'}}))
        error = self.assert_routing_error(client, 422, 'No drivable route')
        self.assertEqual(error.provider_code, 2010)

    def test_route_too_long(self):
        client, _ = self.make_client(fake_response(400, {'error': {
            'code': 2004, 'message': 'Request parameters exceed the server configuration limits.'}}))
        self.assert_routing_error(client, 422, 'too long')

    def test_unexpected_error_without_json(self):
        client, _ = self.make_client(fake_response(500, text='Internal Server Error'))
        self.assert_routing_error(client, 502, 'Internal Server Error')

    def test_timeout(self):
        client, _ = self.make_client(error=requests.Timeout())
        self.assert_routing_error(client, 504, 'timed out')
        self.assertEqual(client.calls, 1)

    def test_connection_error(self):
        client, _ = self.make_client(error=requests.ConnectionError('DNS failure'))
        self.assert_routing_error(client, 502, 'Could not reach')
