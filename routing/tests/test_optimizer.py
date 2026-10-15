import random

import numpy as np
from django.test import SimpleTestCase

from routing.services.corridor import Candidate, Station
from routing.services.optimizer import NoFeasibleRoute, _greedy, _Node, optimize

RANGE, MPG, START = 500, 10, 25


def cand(opis_id, mile, price, detour=0.0):
    station = Station(id=opis_id, opis_id=opis_id, name=f'Stop {opis_id}', address='I-1',
                      city='Town', state='TX', price=price, lat=0.0, lng=0.0)
    return Candidate(station=station, mile_marker=mile, route_lat=0.0, route_lng=0.0,
                     detour_miles=detour)


def run(candidates, total, **kwargs):
    kwargs = {'max_range_miles': RANGE, 'mpg': MPG, 'start_fuel_miles': START, **kwargs}
    return optimize(sorted(candidates, key=lambda c: c.mile_marker), total, **kwargs)


def stops(plan):
    return [(s.station.opis_id, round(s.gallons, 6)) for s in plan.stops]


def lower_bound_cost(candidates, total, step=0.1):
    """A lower bound on the fuel cost of a route (NOT a brute-force search).

    Under FIFO accounting (first bought, first burned), fuel bought at mile s
    is burned by mile s + RANGE, so fuel burned at mile x was bought somewhere
    in [x - RANGE, x] and cost at least the cheapest price there. Summing that
    over the route gives a lower bound. That the textbook greedy reaches it is
    a separate fact, checked by the test, not proven by the bound.

    Sampled on a `step`-mile grid (hence the small test delta). Ignores
    detours, the stop penalty and the start radius: the route starts at a
    station at mile 0.
    """
    miles = np.array([c.mile_marker for c in candidates])
    prices = np.array([c.station.price for c in candidates])
    cost = 0.0
    for x in np.arange(step / 2, total, step):
        ok = (miles <= x) & (miles >= x - RANGE)
        cost += prices[ok].min() * step / MPG
    return cost


class OptimizerTests(SimpleTestCase):
    def test_trip_shorter_than_range_is_one_stop(self):
        plan = run([cand(1, 0, 3.0), cand(2, 100, 3.5)], 300)
        self.assertEqual(stops(plan), [(1, 30.0)])
        self.assertAlmostEqual(plan.total_cost, 90.0)
        self.assertFalse(plan.assumed_origin_fill)

    def test_buys_just_enough_to_reach_a_cheaper_station(self):
        plan = run([cand(1, 0, 4.0), cand(2, 200, 3.0)], 600)
        self.assertEqual(stops(plan), [(1, 20.0), (2, 40.0)])
        self.assertAlmostEqual(plan.total_cost, 20 * 4.0 + 40 * 3.0)

    def test_fills_up_when_nothing_ahead_is_cheaper(self):
        # Cheap here, pricier later: fill the tank, then top up just enough.
        plan = run([cand(1, 0, 3.0), cand(2, 400, 4.0), cand(3, 450, 3.8)], 800)
        self.assertEqual(stops(plan), [(1, 50.0), (3, 30.0)])
        self.assertAlmostEqual(plan.total_cost, 50 * 3.0 + 30 * 3.8)

    def test_all_equal_prices(self):
        plan = run([cand(i, m, 3.5) for i, m in enumerate([0, 150, 300, 450, 600, 750, 900], 1)], 1000)
        self.assertAlmostEqual(plan.total_gallons, 100.0)
        self.assertAlmostEqual(plan.total_cost, 350.0)
        self.assertLessEqual(len(plan.stops), 3)

    def test_destination_exactly_at_full_range(self):
        plan = run([cand(1, 0, 3.0)], 500)
        self.assertEqual(stops(plan), [(1, 50.0)])

    def test_gap_longer_than_range(self):
        with self.assertRaises(NoFeasibleRoute) as ctx:
            run([cand(1, 0, 3.0), cand(2, 400, 3.0), cand(3, 950, 3.0)], 1200)
        self.assertEqual((ctx.exception.from_mile, ctx.exception.to_mile), (400, 950))
        self.assertIn('mile 400', ctx.exception.message)

    def test_last_station_too_far_from_destination(self):
        with self.assertRaises(NoFeasibleRoute) as ctx:
            run([cand(1, 0, 3.0)], 501)
        self.assertEqual(ctx.exception.to_mile, 501)

    def test_stations_at_the_same_mile_marker(self):
        plan = run([cand(1, 0, 3.3), cand(2, 0, 3.1), cand(3, 0, 3.2)], 400)
        self.assertEqual(stops(plan), [(2, 40.0)])

    def test_zero_length_route(self):
        plan = run([cand(1, 0, 3.0)], 0.0)
        self.assertEqual((plan.stops, plan.total_cost), ([], 0))

    def test_first_stop_is_cheapest_within_start_radius_and_covers_the_approach(self):
        plan = run([cand(1, 5, 3.4), cand(2, 20, 3.2), cand(3, 30, 2.5)], 300)
        # Station 3 is cheaper but past the 25 mi start radius; the 20 miles
        # driven to station 2 are billed there.
        self.assertEqual(stops(plan), [(2, 3.0), (3, 27.0)])
        self.assertAlmostEqual(plan.total_gallons, 30.0)

    def test_assumed_origin_fill_when_no_station_near_the_start(self):
        origin = Station(id=99, opis_id=99, name='Nearest', address='', city='Far', state='TX',
                         price=3.6, lat=0.0, lng=0.0)
        plan = run([cand(1, 100, 3.0)], 300, origin_station=origin)
        self.assertTrue(plan.assumed_origin_fill)
        self.assertEqual(stops(plan), [(99, 10.0), (1, 20.0)])
        self.assertTrue(plan.stops[0].assumed)
        self.assertIsNone(plan.stops[0].candidate)
        self.assertEqual(plan.stops[0].mile_marker, 0.0)

    def test_no_station_near_start_and_no_fallback(self):
        with self.assertRaises(NoFeasibleRoute):
            run([cand(1, 100, 3.0)], 300)
        with self.assertRaises(NoFeasibleRoute):
            run([], 300)

    def test_detour_is_paid_for(self):
        # Station 3 is 1 cent cheaper but 10 miles off the road: 20 extra miles
        # (2 gal) cost far more than the 1 cent/gal it saves.
        plan = run([cand(1, 0, 3.0), cand(2, 450, 3.50), cand(3, 450, 3.49, detour=10)], 900)
        self.assertEqual(plan.stops[-1].station.opis_id, 2)
        self.assertEqual(plan.detour_miles, 0.0)

        # 50 cents cheaper is worth the detour, and the detour fuel is bought too.
        plan = run([cand(1, 0, 3.0), cand(2, 450, 3.50), cand(3, 450, 3.00, detour=10)], 900)
        self.assertEqual(plan.stops[-1].station.opis_id, 3)
        self.assertAlmostEqual(plan.detour_miles, 20.0)
        self.assertAlmostEqual(plan.total_gallons, 92.0)

    def test_stop_penalty_skips_tiny_top_ups(self):
        # Prices fall slowly: min-cost plans stop at every station.
        candidates = [cand(i, m, p) for i, (m, p) in enumerate(
            [(0, 3.10), (100, 3.09), (200, 3.08), (300, 3.07), (400, 3.06), (450, 3.05)], 1)]
        cheapest = run(candidates, 900)
        self.assertEqual(len(cheapest.stops), 6)
        fewer = run(candidates, 900, stop_penalty=5)
        self.assertLessEqual(len(fewer.stops), 2)
        self.assertAlmostEqual(fewer.total_gallons, 90.0)
        # The extra cost stays below what the skipped stops would have cost in penalties.
        skipped = len(cheapest.stops) - len(fewer.stops)
        self.assertLess(fewer.total_cost - cheapest.total_cost, 5 * skipped)

    def test_duplicate_stations_at_one_point_are_pruned(self):
        plan = run([cand(1, 0, 3.0)] + [cand(i, 300, 3.5 + i / 100) for i in range(2, 30)], 700)
        self.assertEqual([s.station.opis_id for s in plan.stops], [1, 2])

    def test_detour_counts_against_range(self):
        # 495 road miles + 10 off-route miles is out of range.
        with self.assertRaises(NoFeasibleRoute):
            run([cand(1, 0, 3.0), cand(2, 495, 2.0, detour=10)], 800)
        plan = run([cand(1, 0, 3.0), cand(2, 495, 2.0, detour=2)], 800)
        self.assertEqual(stops(plan), [(1, 49.7), (2, 30.7)])

    def test_greedy_reaches_lower_bound_and_optimizer_stays_close(self):
        rng = random.Random(7)
        for _ in range(40):
            total = rng.uniform(300, 2000)
            miles = sorted(rng.uniform(0, total) for _ in range(rng.randint(3, 25)))
            miles = [0.0] + [m for m in miles]
            # Make every gap feasible.
            for k in range(1, len(miles)):
                miles[k] = min(miles[k], miles[k - 1] + RANGE * 0.9)
            if total - miles[-1] > RANGE:
                total = miles[-1] + rng.uniform(0, RANGE)
            candidates = [cand(i, m, round(rng.uniform(2.7, 4.5), 3)) for i, m in enumerate(miles)]
            bound = lower_bound_cost(candidates, total)
            # The exact greedy (second pass) on every station reaches the bound.
            nodes = [_Node(c.mile_marker, 0.0, c.station.price, c) for c in candidates]
            greedy_nodes = nodes + [_Node(total, 0.0, 0.0)]
            purchases, _ = _greedy(greedy_nodes, 0.0, RANGE)
            self.assertAlmostEqual(sum(m / MPG * greedy_nodes[k].price for k, m in purchases),
                                   bound, delta=0.05)
            # The full optimizer picks stops on a 0.1 gal grid: within 0.5% of it.
            # (The bound starts at the mile-0 station, so no start radius here.)
            plan = run(candidates, total, start_fuel_miles=0)
            self.assertAlmostEqual(plan.total_gallons, total / MPG, places=6)
            self.assertGreater(plan.total_cost, bound - 0.05)
            self.assertLess(plan.total_cost, bound * 1.005)

    def test_gallons_always_cover_road_and_detours(self):
        rng = random.Random(11)
        for _ in range(40):
            total = rng.uniform(100, 2500)
            candidates = [cand(0, 0.0, 3.5, detour=rng.uniform(0, 10))]
            m = 0.0
            while m < total:
                m += rng.uniform(5, 300)
                candidates.append(cand(len(candidates), min(m, total), round(rng.uniform(2.7, 4.5), 3),
                                       detour=rng.uniform(0, 10)))
            plan = run(candidates, total)
            self.assertAlmostEqual(plan.total_gallons, (total + plan.detour_miles) / MPG, places=6)
            # Top-level detour is the round trip to each stop actually made.
            self.assertAlmostEqual(plan.detour_miles,
                                   sum(2 * s.candidate.detour_miles for s in plan.stops), places=9)
            for stop in plan.stops:
                self.assertGreater(stop.gallons, 0)
                self.assertLessEqual(stop.gallons, RANGE / MPG + 2 * 10 / MPG + 1e-9)
