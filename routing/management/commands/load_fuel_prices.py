import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import CityCoord, DataVersion, FuelStation
from routing.services.city_index import census_loaded, load_city_index
from routing.services.fuel_csv import read_fuel_csv
from routing.services.nominatim import NominatimClient
from routing.services.places import normalize_city

STATION_UPDATE_FIELDS = [
    'name', 'address', 'city', 'state', 'rack_id', 'price', 'min_price',
    'max_price', 'row_count', 'lat', 'lng', 'updated_at',
]


class Command(BaseCommand):
    help = ('Load (or reload) fuel stations from the price CSV. Re-runnable: stations are '
            'upserted by OPIS ID and only cities not seen before are geocoded.')

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--skip-nominatim', action='store_true',
                            help="Don't call Nominatim for cities missing from the Census files.")
        parser.add_argument('--retry-not-found', action='store_true',
                            help='Ask Nominatim again for cities that failed on a previous run.')
        parser.add_argument('--keep-missing', action='store_true',
                            help='Keep stations that are not in this CSV (default: remove them).')

    def handle(self, *args, **options):
        started = time.monotonic()
        if not census_loaded():
            raise CommandError('No Census places loaded. Run `python manage.py load_places` first.')
        try:
            stations, stats = read_fuel_csv(options['csv_path'])
        except FileNotFoundError as exc:
            raise CommandError(f'CSV not found: {options["csv_path"]}') from exc

        coords, geo_stats, unresolved = self.resolve_cities(stations, options)

        located = []
        for s in stations:
            point = coords.get((s.city, s.state))
            if point is None:
                continue
            located.append(FuelStation(
                opis_id=s.opis_id, name=s.name, address=s.address, city=s.city,
                state=s.state, rack_id=s.rack_id, price=s.price, min_price=s.min_price,
                max_price=s.max_price, row_count=s.row_count, lat=point[0], lng=point[1],
            ))

        with transaction.atomic():
            existing = set(FuelStation.objects.values_list('opis_id', flat=True))
            FuelStation.objects.bulk_create(
                located, batch_size=1000, update_conflicts=True,
                unique_fields=['opis_id'], update_fields=STATION_UPDATE_FIELDS,
            )
            removed = 0
            if not options['keep_missing']:
                keep = {s.opis_id for s in located}
                removed, _ = FuelStation.objects.exclude(opis_id__in=keep).delete()
            version = DataVersion.bump()

        new_ids = {s.opis_id for s in located} - existing
        skipped = len(stations) - len(located)
        self.report(stats, geo_stats, unresolved, len(located), len(new_ids), skipped,
                    removed, version, time.monotonic() - started)

    def resolve_cities(self, stations, options):
        """Map (city, state) -> (lat, lng) using CityCoord, then Nominatim for misses."""
        index = load_city_index()
        not_found = set(CityCoord.objects.filter(source=CityCoord.SOURCE_NOT_FOUND)
                        .values_list('state', 'key'))
        pairs = sorted({(s.city, s.state) for s in stations})
        coords, missing = {}, []
        geo_stats = {'census_or_cached': 0, 'nominatim_found': 0,
                     'nominatim_not_found': 0, 'nominatim_errors': 0}
        for city, state in pairs:
            place = index.lookup(city, state)
            if place:
                coords[(city, state)] = (place.lat, place.lng)
                geo_stats['census_or_cached'] += 1
            else:
                missing.append((city, state))

        to_query = [p for p in missing
                    if options['retry_not_found'] or (p[1], normalize_city(p[0])) not in not_found]
        unresolved = [p for p in missing if p not in to_query]
        if to_query and options['skip_nominatim']:
            unresolved.extend(to_query)
            to_query = []
        if to_query:
            self.stdout.write(f'Geocoding {len(to_query)} cities with Nominatim '
                              f'(~{len(to_query) * 1.1:.0f}s, 1 request/second)...')
            client = NominatimClient(settings.NOMINATIM_USER_AGENT)
        for n, (city, state) in enumerate(to_query, start=1):
            try:
                point = client.geocode_city(city, state)
            except requests.RequestException as exc:
                # Transient: don't record a miss, so the next run tries again.
                self.stderr.write(f'  Nominatim error for {city}, {state}: {exc}')
                geo_stats['nominatim_errors'] += 1
                unresolved.append((city, state))
                continue
            CityCoord.objects.update_or_create(
                state=state, key=normalize_city(city),
                defaults=dict(
                    name=city, lat=point[0] if point else None, lng=point[1] if point else None,
                    source=CityCoord.SOURCE_NOMINATIM if point else CityCoord.SOURCE_NOT_FOUND,
                ),
            )
            if point:
                coords[(city, state)] = point
                geo_stats['nominatim_found'] += 1
            else:
                geo_stats['nominatim_not_found'] += 1
                unresolved.append((city, state))
            if n % 25 == 0:
                self.stdout.write(f'  {n}/{len(to_query)}')
        return coords, geo_stats, sorted(unresolved)

    def report(self, stats, geo, unresolved, saved, new, skipped, removed, version, elapsed):
        w = self.stdout.write
        w(f'Rows read:                 {stats.rows_read:,}')
        w(f'Canadian rows dropped:     {stats.canadian_dropped:,}')
        w(f'Invalid rows dropped:      {stats.invalid_dropped:,}')
        for line_no, reason in stats.invalid_rows[:10]:
            w(f'  line {line_no}: {reason}')
        w(f'Duplicate rows merged:     {stats.duplicate_rows_merged:,}')
        w(f'Unique US stations:        {stats.stations:,}')
        w(f'Cities from Census/cache:  {geo["census_or_cached"]:,}')
        w(f'Cities via Nominatim:      {geo["nominatim_found"]:,} found, '
          f'{geo["nominatim_not_found"]:,} not found, {geo["nominatim_errors"]:,} errors')
        w(f'Cities failed:             {len(unresolved):,}')
        if unresolved:
            w('  ' + '; '.join(f'{c}, {s}' for c, s in unresolved))
        w(f'Stations skipped (no coordinates): {skipped:,}')
        w(f'Stations removed (not in CSV):     {removed:,}')
        self.stdout.write(self.style.SUCCESS(
            f'Saved {saved:,} stations ({new:,} new). Data version is now v{version}. '
            f'Took {elapsed:.1f}s.'))
