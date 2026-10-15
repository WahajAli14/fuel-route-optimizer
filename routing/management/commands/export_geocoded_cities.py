from django.conf import settings
from django.core.management.base import BaseCommand

from routing.services.geocoded_cities import export_rows


class Command(BaseCommand):
    help = ('Export Nominatim-resolved cities and recorded misses to data/geocoded_cities.csv, '
            'so a fresh setup needs no Nominatim calls.')

    def add_arguments(self, parser):
        parser.add_argument('--output', default=None,
                            help='Output path (default: DATA_DIR/GEOCODED_CITIES_FILE).')

    def handle(self, *args, **options):
        path = options['output'] or settings.DATA_DIR / settings.GEOCODED_CITIES_FILE
        count = export_rows(path)
        self.stdout.write(self.style.SUCCESS(f'Wrote {count:,} cities to {path}.'))
