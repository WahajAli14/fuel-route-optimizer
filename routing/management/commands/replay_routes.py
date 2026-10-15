"""Re-run the saved ORS routes through the pipeline: timings and plans, zero ORS calls."""

import statistics

from django.conf import settings
from django.core.management.base import BaseCommand

from routing.services.ors_client import ORSClient
from routing.services.ors_replay import ROUTES, ReplaySession
from routing.services.planner import plan_route


class Command(BaseCommand):
    help = 'Replay the saved ORS responses (routing/tests/fixtures) through plan_route, uncached.'

    def add_arguments(self, parser):
        parser.add_argument('--penalty', type=float, nargs='+', default=[0.0],
                            help='Stop penalties to run (dollars). Default: 0')
        parser.add_argument('--repeat', type=int, default=5, help='Runs per route (median reported)')
        parser.add_argument('--corridor', type=float, default=None, help='Corridor miles')

    def handle(self, *args, **options):
        corridor = options['corridor'] or float(settings.FUEL['CORRIDOR_MILES'])
        for name, (start, finish) in ROUTES.items():
            for penalty in options['penalty']:
                session = ReplaySession(name)
                client = ORSClient(api_key='replay', profile=settings.ORS_PROFILE, session=session)
                runs = [plan_route(start, finish, corridor_miles=corridor, stop_penalty=penalty,
                                   client=client, use_cache=False)
                        for _ in range(options['repeat'])]
                result = runs[-1]
                compute_ms = [r['meta']['elapsed_ms'] - r['meta']['timings']['routing_ms'] for r in runs]
                self.stdout.write(
                    f'{start} -> {finish} (penalty ${penalty:g}): {result["route_miles"]} mi, '
                    f'{len(result["fuel_stops"])} stops, ${result["total_fuel_cost"]}, '
                    f'{result["total_gallons"]} gal, detour {result["detour_miles"]} mi | '
                    f'our compute median {statistics.median(compute_ms):.1f} ms '
                    f'(corridor {result["meta"]["timings"]["corridor_ms"]} ms, '
                    f'optimize {result["meta"]["timings"]["optimize_ms"]} ms); '
                    f'ORS calls: 0 (replayed {session.posts}x)')
