from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(['GET'])
def health(request):
    return Response({
        'status': 'ok',
        'ors_key_configured': bool(settings.ORS_API_KEY),
        'ors_profile': settings.ORS_PROFILE,
    })
