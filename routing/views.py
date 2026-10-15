from django.conf import settings
from django.shortcuts import render
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .serializers import RouteRequestSerializer
from .services.locations import LocationError
from .services.optimizer import NoFeasibleRoute
from .services.ors_client import RoutingError
from .services.planner import plan_route


@api_view(['GET'])
def health(request):
    return Response({
        'status': 'ok',
        'ors_key_configured': bool(settings.ORS_API_KEY),
        'ors_profile': settings.ORS_PROFILE,
    })


def _run(data):
    """Validate input and plan the route: (result, None) or (None, (status, error body))."""
    serializer = RouteRequestSerializer(data=data)
    if not serializer.is_valid():
        return None, (status.HTTP_400_BAD_REQUEST, {
            'code': 'invalid_request', 'message': 'Check the request fields.',
            'fields': serializer.errors})
    params = serializer.validated_data
    try:
        return plan_route(params['start'], params['finish'],
                          corridor_miles=params['corridor_miles'],
                          stop_penalty=params['stop_penalty']), None
    except LocationError as exc:
        return None, (status.HTTP_400_BAD_REQUEST, {
            'code': 'invalid_location', 'message': exc.message,
            'field': getattr(exc, 'field', None)})
    except NoFeasibleRoute as exc:
        return None, (status.HTTP_422_UNPROCESSABLE_ENTITY, {
            'code': 'no_feasible_plan', 'message': exc.message,
            'gap': {'from_mile': round(exc.from_mile, 1), 'to_mile': round(exc.to_mile, 1)}})
    except RoutingError as exc:
        return None, (exc.status, {
            'code': 'routing_error', 'message': exc.message,
            'provider_code': exc.provider_code})


@api_view(['POST'])
def route(request):
    """POST {"start": "Chicago, IL", "finish": "Los Angeles, CA"} -> route, fuel stops, cost."""
    result, error = _run(request.data)
    if error:
        code, body = error
        return Response({'error': body}, status=code)
    return Response(result)


def route_map(request):
    """GET /api/route/map/?start=...&finish=... -> Leaflet page for the same plan."""
    result, error = _run(request.GET)
    if error:
        code, body = error
        return render(request, 'routing/map.html', {'error': body}, status=code)
    return render(request, 'routing/map.html', {'result': result})
