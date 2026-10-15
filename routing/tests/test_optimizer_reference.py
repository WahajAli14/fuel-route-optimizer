"""Independent check of optimize() against an exhaustive LP reference.

The reference uses neither _greedy nor the DP. For every subset of stations
(in mile order) it solves the purchases as a linear program, then takes the
cheapest subset. It models what optimize() does:

- The first stop must be within the start radius. The truck reaches it with
  just enough fuel; that approach fuel, (mile + detour) / MPG gallons, is
  billed at the first stop's price as part of its purchase and does NOT count
  against tank capacity (optimize() holds it as negative fuel).
- With no station within the start radius, the first stop is an assumed fill
  at the origin (mile 0, no detour) at the nearest station's price.
- A leg from stop a to stop b is a.detour + (b.mile - a.mile) + b.detour, so
  every stop in a subset pays its round-trip detour.
- The penalty is charged per stop in the subset. With non-negative detours
  and penalties, a subset with a station where nothing is bought is never
  better than the same subset without it, so the minimum over all subsets is
  the minimum over plans. The first stop always buys (it settles the
  approach), so dropping an idle station never breaks the start rule.

Objective compared: fuel cost + penalty x number of stops.
"""

import dataclasses
import itertools
import random
import statistics
import sys
from unittest import mock

import numpy as np
from django.test import SimpleTestCase
from scipy.optimize import linprog

from routing.services.corridor import Candidate, Station
from routing.services import optimizer
from routing.services.optimizer import NoFeasibleRoute, optimize

RANGE, MPG, START = 500.0, 10.0, 25.0
CAPACITY = RANGE / MPG
ROUTES = 50
PENALTIES = (0.0, 5.0)


def make_station(opis_id, price):
    return Station(id=opis_id, opis_id=opis_id, name=f'S{opis_id}', address='', city='',
                   state='TX', price=price, lat=0.0, lng=0.0)


def random_route(rng):
    """<= 8 stations with random detours (0-10 mi) and spacing the truck can drive."""
    n = rng.randint(2, 8)
    mile = rng.uniform(0, 40)  # sometimes past the 25 mi start radius
    candidates = []
    for i in range(n):
        if i:
            mile += rng.uniform(10, 380)
        candidates.append(Candidate(station=make_station(i + 1, round(rng.uniform(2.7, 4.2), 3)),
                                    mile_marker=round(mile, 1), route_lat=0.0, route_lng=0.0,
                                    detour_miles=round(rng.uniform(0, 10), 2)))
    total = round(mile + rng.uniform(5, 450), 1)
    origin = make_station(99, round(rng.uniform(2.7, 4.2), 3))
    return candidates, total, origin


def leg(a, b):
    """Miles from stop a to stop b; stops are (mile, detour)."""
    return a[1] + (b[0] - a[0]) + b[1]


def lp_fuel_cost(stops, prices, total, approach_gallons):
    """Cheapest purchases visiting every stop in order, or None if infeasible.

    stops: [(mile, detour)], the first being the first stop.
    """
    n = len(stops)
    legs = [leg(stops[k], stops[k + 1]) / MPG for k in range(n - 1)]
    legs.append(leg(stops[-1], (total, 0.0)) / MPG)
    if max(legs) > CAPACITY + 1e-9:
        return None
    # Tank after buying at stop k: sum(g[:k+1]) - approach - sum(legs[:k]).
    lower = np.tril(np.ones((n, n)))
    rows, bounds = [], []
    for k in range(n):
        driven = approach_gallons + sum(legs[:k])
        rows.append(lower[k]); bounds.append(CAPACITY + driven)          # tank <= capacity
        rows.append(-lower[k]); bounds.append(-(driven + legs[k]))       # reach the next stop
    result = linprog(prices, A_ub=np.array(rows), b_ub=np.array(bounds),
                     bounds=[(0, None)] * n, method='highs')
    return result.fun if result.status == 0 else None


def reference(candidates, total, origin, penalty):
    """Exhaustive minimum of fuel cost + penalty x stops; None if no plan exists."""
    nodes = [((c.mile_marker, c.detour_miles), c.station.price) for c in candidates
             if c.mile_marker <= total]
    first_ok = [i for i, ((mile, _), _) in enumerate(nodes) if mile <= START]
    best = None
    if first_ok:
        for r in range(1, len(nodes) + 1):
            for subset in itertools.combinations(range(len(nodes)), r):
                if subset[0] not in first_ok:
                    continue
                stops = [nodes[i][0] for i in subset]
                approach = (stops[0][0] + stops[0][1]) / MPG
                cost = lp_fuel_cost(stops, [nodes[i][1] for i in subset], total, approach)
                if cost is not None:
                    value = cost + penalty * r
                    best = value if best is None else min(best, value)
    else:
        origin_node = ((0.0, 0.0), origin.price)
        for r in range(0, len(nodes) + 1):
            for subset in itertools.combinations(range(len(nodes)), r):
                chosen = [origin_node] + [nodes[i] for i in subset]
                cost = lp_fuel_cost([s for s, _ in chosen], [p for _, p in chosen], total, 0.0)
                if cost is not None:
                    value = cost + penalty * (r + 1)
                    best = value if best is None else min(best, value)
    return best


def replay(test, plan, total):
    """Walk the plan's stops independently of the optimizer's own bookkeeping."""
    tol = 1e-6
    stops = [((0.0, 0.0) if s.assumed else (s.candidate.mile_marker, s.candidate.detour_miles), s)
             for s in plan.stops]
    first_point, first = stops[0]
    approach = 0.0 if first.assumed else (first_point[0] + first_point[1]) / MPG
    tank = first.gallons - approach  # the approach is billed here but was already burned
    test.assertGreaterEqual(tank, -tol)
    test.assertLessEqual(tank, CAPACITY + tol)
    for (prev_point, _), (point, stop) in zip(stops, stops[1:]):
        tank -= leg(prev_point, point) / MPG
        test.assertGreaterEqual(tank, -tol, 'ran dry before a stop')
        tank += stop.gallons
        test.assertLessEqual(tank, CAPACITY + tol, 'tank over capacity')
    tank -= leg(stops[-1][0], (total, 0.0)) / MPG
    test.assertGreaterEqual(tank, -tol, 'ran dry before the destination')
    detour = sum(2 * point[1] for point, _ in stops)
    test.assertAlmostEqual(plan.detour_miles, detour, places=9)
    test.assertAlmostEqual(plan.total_gallons, (total + detour) / MPG, places=6)


class OptimizerReferenceTests(SimpleTestCase):
    def test_against_exhaustive_lp_reference(self):
        rng = random.Random(2026)
        gaps = {p: [] for p in PENALTIES}
        checked = assumed = 0
        for _ in range(ROUTES):
            candidates, total, origin = random_route(rng)
            for penalty in PENALTIES:
                best = reference(candidates, total, origin, penalty)
                try:
                    plan = optimize(candidates, total, max_range_miles=RANGE, mpg=MPG,
                                    start_fuel_miles=START, origin_station=origin,
                                    stop_penalty=penalty)
                except NoFeasibleRoute:
                    self.assertIsNone(best, 'optimizer found no plan but the LP did')
                    continue
                self.assertIsNotNone(best, 'optimizer found a plan the LP calls infeasible')
                replay(self, plan, total)
                value = plan.total_cost + penalty * len(plan.stops)
                self.assertGreater(value, best - 0.01, f'optimizer below the reference: {value} < {best}')
                gaps[penalty].append((value - best) / best)
                checked += 1
                assumed += plan.assumed_origin_fill
        for penalty, values in gaps.items():
            self.assertLessEqual(max(values), 0.01, f'gap above 1% at penalty {penalty}')
        self.assertGreater(checked, ROUTES)  # most routes produce a plan at both penalties
        summary = '; '.join(f'penalty ${p:g}: n={len(v)}, max gap {max(v) * 100:.3f}%, '
                            f'mean {statistics.mean(v) * 100:.4f}%' for p, v in gaps.items())
        print(f'\n[LP reference] {checked} plans checked ({assumed} assumed origin fills). '
              f'{summary}', file=sys.stderr)


class ReferenceSanityTests(SimpleTestCase):
    """The reference and the replay must be able to fail: check that they do."""

    def run_optimizer(self, candidates, total, origin, penalty=0.0):
        return optimize(candidates, total, max_range_miles=RANGE, mpg=MPG, start_fuel_miles=START,
                        origin_station=origin, stop_penalty=penalty)

    def test_hand_computed_case(self):
        # $4 at mile 0, $3 at mile 200, 600 miles: buy 20 gal at $4 to reach the
        # cheaper station, then 40 gal at $3 to finish = $200.
        candidates = [Candidate(make_station(1, 4.0), 0.0, 0.0, 0.0, 0.0),
                      Candidate(make_station(2, 3.0), 200.0, 0.0, 0.0, 0.0)]
        self.assertAlmostEqual(reference(candidates, 600.0, make_station(9, 9.0), 0.0), 200.0,
                               places=6)
        # 600 mi needs both stops (one tank covers 500), so a $5 penalty adds 2 x $5.
        self.assertAlmostEqual(reference(candidates, 600.0, make_station(9, 9.0), 5.0), 210.0,
                               places=6)

    def test_coarse_grid_gap_is_caught(self):
        """A 2-gallon grid makes plans worse; the 1% check must notice on some route."""
        rng = random.Random(2026)
        worst = 0.0
        with mock.patch.object(optimizer, 'LEVEL_GALLONS', 2.0):
            for _ in range(ROUTES):
                candidates, total, origin = random_route(rng)
                plan = self.run_optimizer(candidates, total, origin)
                best = reference(candidates, total, origin, 0.0)
                worst = max(worst, (plan.total_cost - best) / best)
                if worst > 0.01:
                    break
        self.assertGreater(worst, 0.01)

    def test_replay_flags_an_infeasible_plan(self):
        candidates = [Candidate(make_station(1, 3.0), 0.0, 0.0, 0.0, 2.0),
                      Candidate(make_station(2, 3.5), 400.0, 0.0, 0.0, 1.0)]
        plan = self.run_optimizer(candidates, 700.0, make_station(9, 9.0))
        replay(self, plan, 700.0)  # the real plan passes
        short = dataclasses.replace(plan.stops[-1], gallons=plan.stops[-1].gallons - 5)
        plan.stops[-1] = short  # 50 miles of fuel missing
        with self.assertRaises(self.failureException):
            replay(self, plan, 700.0)
