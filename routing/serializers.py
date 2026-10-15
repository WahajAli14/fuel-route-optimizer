from django.conf import settings
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    corridor_miles = serializers.FloatField(required=False, min_value=1, max_value=50)
    stop_penalty = serializers.FloatField(required=False, min_value=0, max_value=1000)

    def validate(self, attrs):
        # Defaults come from settings at request time; the optimizer itself never reads them.
        attrs.setdefault('corridor_miles', float(settings.FUEL['CORRIDOR_MILES']))
        attrs.setdefault('stop_penalty', float(settings.FUEL['STOP_PENALTY_DOLLARS']))
        return attrs
