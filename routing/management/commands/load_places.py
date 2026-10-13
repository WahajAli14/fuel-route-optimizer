import io
import itertools
import time
import zipfile

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import CityCoord
from routing.services.city_index import place_priority
from routing.services.places import PlaceIndex, read_cousubs, read_places

CENSUS_SOURCES = [CityCoord.SOURCE_CENSUS_PLACE, CityCoord.SOURCE_CENSUS_COUSUB]


class Command(BaseCommand):
    help = 'Load US city coordinates from the Census Gazetteer Places and County Subdivisions files.'

    def add_arguments(self, parser):
        parser.add_argument('--redownload', action='store_true',
                            help='Download the Gazetteer files even if they exist locally.')

    def handle(self, *args, **options):
        started = time.monotonic()
        places_path = self.ensure_file(settings.CENSUS_PLACES_FILE, options['redownload'])
        cousubs_path = self.ensure_file(settings.CENSUS_COUSUBS_FILE, options['redownload'])

        index = PlaceIndex(itertools.chain(read_places(places_path), read_cousubs(cousubs_path)))
        rows = [
            CityCoord(state=state, key=key, name=place.name, lat=place.lat, lng=place.lng,
                      source=place.source, priority=place_priority(place),
                      land_area=place.rank[3])
            for (state, key), place in index.exact.items()
        ]

        with transaction.atomic():
            # Census rows are replaced wholesale; Nominatim rows for the same key
            # are overwritten because the Census answer is authoritative.
            removed, _ = CityCoord.objects.filter(source__in=CENSUS_SOURCES).delete()
            CityCoord.objects.bulk_create(
                rows, batch_size=2000, update_conflicts=True,
                unique_fields=['state', 'key'],
                update_fields=['name', 'lat', 'lng', 'source', 'priority', 'land_area'],
            )

        by_source = {s: sum(r.source == s for r in rows) for s in CENSUS_SOURCES}
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(rows):,} city keys '
            f'({by_source[CityCoord.SOURCE_CENSUS_PLACE]:,} places, '
            f'{by_source[CityCoord.SOURCE_CENSUS_COUSUB]:,} county subdivisions; '
            f'replaced {removed:,} old rows) in {time.monotonic() - started:.1f}s.'
        ))

    def ensure_file(self, name, redownload):
        txt_path = settings.DATA_DIR / f'{name}.txt'
        if txt_path.exists() and not redownload:
            return txt_path
        url = f'{settings.CENSUS_GAZETTEER_URL}{name}.zip'
        self.stdout.write(f'Downloading {url} ...')
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f'Could not download {url}: {exc}') from exc
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next((m for m in archive.namelist() if m.endswith('.txt')), None)
            if member is None:
                raise CommandError(f'No .txt file inside {url}')
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            txt_path.write_bytes(archive.read(member))
        return txt_path
