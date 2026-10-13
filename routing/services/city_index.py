"""Build a PlaceIndex from the CityCoord table."""

from ..models import CityCoord
from .places import Place, PlaceIndex, SOURCE_PLACE


def place_priority(place):
    """Pack a Gazetteer Place rank (minus land area) into a small integer for storage."""
    source_level, is_primary, is_incorporated, _land = place.rank
    return source_level * 4 + int(is_primary) * 2 + int(is_incorporated)


def load_city_index():
    """Return a PlaceIndex over every located CityCoord row."""
    rows = CityCoord.objects.exclude(lat=None).values_list(
        'state', 'name', 'lat', 'lng', 'source', 'priority', 'land_area')
    return PlaceIndex(
        Place(state=state, name=name, lat=lat, lng=lng, source=source,
              rank=(priority, land_area))
        for state, name, lat, lng, source, priority, land_area in rows
    )


def census_loaded():
    return CityCoord.objects.filter(source=SOURCE_PLACE).exists()
