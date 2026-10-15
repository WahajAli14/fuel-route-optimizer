"""Choose where to buy fuel along a route, and how much.

The textbook answer is the greedy gas-station algorithm: at each station, if
a cheaper one is in range buy just enough to reach it, otherwise fill up and
go to the cheapest one in range. It minimizes cost on a straight line, but on
real data it stops constantly: it follows every small step of a falling
price, and tops up at stations a few miles apart. Chicago -> Los Angeles took
19 stops, some for under a gallon.

So the plan is built in two passes:

1. Choose the stops with a dynamic program over (station, fuel level), with
   fuel in 0.1-gallon steps. It minimizes fuel cost under the modeled
   distances, plus an optional fixed cost per stop (`stop_penalty`, $0 by
   default), so with a penalty a stop has to save more than that to be worth
   making. The result is approximate: legs are rounded up to the fuel grid
   and detours use city-level station locations.
2. Run the greedy on just those stops to get exact gallons. Every gallon
   burned is bought, so total gallons = (route miles + detour miles) / MPG.
   The penalty is never part of the reported fuel cost.

Detours: stations sit up to the corridor width off the route. Reaching one
and getting back costs 2 x detour_miles of driving, which counts against the
range and is paid for like any other mile. Station coordinates are
city-level, so detours are approximate.

Starting fuel: the truck starts with just enough fuel to reach the first
station; that fuel is included in the trip cost at the first station's price.
The first station must be within START_FUEL_SEARCH_MILES of the origin.
Internally the approach is a negative fuel balance (a debt) settled at the
first stop. If no station is that close, the plan assumes the truck filled up
at the origin at the price of the nearest station and flags
assumed_origin_fill.
"""

from dataclasses import dataclass, field

import numpy as np

_EPS = 1e-9
LEVEL_GALLONS = 0.1     # fuel resolution of the stop-choosing pass (1 mile)


class NoFeasibleRoute(Exception):
    """A stretch of the route is longer than the truck's range with no station."""

    def __init__(self, from_mile, to_mile, max_range_miles):
        self.from_mile = from_mile
        self.to_mile = to_mile
        self.message = (
            f'No fuel station within range: the truck cannot get past mile {from_mile:.0f} '
            f'(next station or destination at mile {to_mile:.0f}, range {max_range_miles:.0f} mi).'
        )
        super().__init__(self.message)


@dataclass(frozen=True)
class FuelStop:
    station: object         # corridor.Station
    candidate: object       # corridor.Candidate, or None for an assumed origin fill
    mile_marker: float
    price: float
    gallons: float
    cost: float
    assumed: bool = False   # True for the assumed origin fill


@dataclass
class FuelPlan:
    total_miles: float
    stops: list = field(default_factory=list)
    assumed_origin_fill: bool = False
    detour_miles: float = 0.0   # extra miles driven to reach stops (round trips)

    @property
    def total_gallons(self):
        return sum(s.gallons for s in self.stops)

    @property
    def total_cost(self):
        return sum(s.cost for s in self.stops)


@dataclass(frozen=True)
class _Node:
    mile: float
    detour: float
    price: float
    candidate: object = None    # None for the destination and an assumed origin fill


def _leg(a, b):
    """Miles driven from stop a to stop b, including leaving a and reaching b."""
    return a.detour + (b.mile - a.mile) + b.detour


def _prune(nodes):
    """Drop stations that another station at the same route point beats on both price and detour."""
    kept = []
    for n in nodes:
        if any(k.mile == n.mile and k.price <= n.price and k.detour <= n.detour for k in kept):
            continue
        kept = [k for k in kept
                if not (k.mile == n.mile and n.price <= k.price and n.detour <= k.detour)]
        kept.append(n)
    return kept


def _choose_stops(nodes, starts, max_range_miles, mpg, stop_penalty):
    """Pick the stop sequence minimizing fuel cost + stop_penalty per stop.

    nodes: stations plus the destination (last, price 0), sorted by mile.
    starts: {node index: cost of the approach fuel} for allowed first stops,
        which are reached with no fuel to spare (fuel level 0).
    Returns the chosen node indices in order (destination excluded).
    """
    n = len(nodes)
    levels = int(round(max_range_miles / mpg / LEVEL_GALLONS))
    miles_per_level = max_range_miles / levels
    size = levels + 1
    f = np.arange(size)

    cost = np.full((n, size), np.inf)          # best cost arriving at node with f levels
    prev_node = np.full((n, size), -1)
    prev_level = np.full((n, size), -1)
    buy_from = np.full((n, size), -1)          # departing level g was bought up from this level
    for i, owed in starts.items():
        cost[i, 0] = owed

    mile = np.array([x.mile for x in nodes])
    detour = np.array([x.detour for x in nodes])

    for i in range(n - 1):
        arrive = cost[i]
        if not np.isfinite(arrive).any():
            continue
        # Departing with g levels: either buy nothing, or buy up from the best f < g.
        price_per_level = nodes[i].price * LEVEL_GALLONS
        base = arrive - f * price_per_level
        best = np.minimum.accumulate(base)
        best_at = np.maximum.accumulate(np.where(base == best, f, 0))
        before = np.concatenate(([np.inf], best[:-1]))
        before_at = np.concatenate(([-1], best_at[:-1]))
        bought = before + f * price_per_level + stop_penalty
        depart = np.minimum(arrive, bought)
        buy_from[i] = np.where(bought < arrive, before_at, -1)

        # Every later node in range. Legs round up to whole levels so the plan
        # never counts on fuel it doesn't have (rounding to nearest could pass
        # several stations on fuel that doesn't exist); at 0.1 gal that costs at
        # most a mile per leg, and the second pass computes exact gallons.
        j = np.arange(i + 1, n)
        j = j[mile[j] - mile[i] <= max_range_miles + _EPS]
        need = detour[i] + (mile[j] - mile[i]) + detour[j]
        need_levels = np.ceil(need / miles_per_level - 1e-9).astype(int)
        ok = need_levels <= levels
        j, need_levels = j[ok], need_levels[ok]
        if len(j) == 0:
            continue
        g = f[None, :] + need_levels[:, None]             # departing level for each arrival f
        padded = np.concatenate((depart, [np.inf]))
        offer = padded[np.minimum(g, size)]
        better = offer < cost[j]
        rows, cols = np.nonzero(better)
        cost[j[rows], cols] = offer[rows, cols]
        prev_node[j[rows], cols] = i
        prev_level[j[rows], cols] = g[rows, cols]

    last = n - 1
    if not np.isfinite(cost[last]).any():
        reached = [k for k in range(n) if np.isfinite(cost[k]).any()]
        stuck = max(reached, key=lambda k: nodes[k].mile) if reached else 0
        following = next((nodes[k].mile for k in range(stuck + 1, n)
                          if nodes[k].mile > nodes[stuck].mile), nodes[last].mile)
        raise NoFeasibleRoute(nodes[stuck].mile, following, max_range_miles)

    chosen = []
    node, level = last, int(np.argmin(cost[last]))
    while True:
        i, g = prev_node[node, level], prev_level[node, level]
        if i < 0:
            break
        start_level = buy_from[i, g]
        if start_level >= 0:
            chosen.append(int(i))
        node, level = int(i), int(start_level if start_level >= 0 else g)
    chosen.reverse()
    if not chosen or chosen[0] != node:
        chosen.insert(0, node)  # the first stop is visited even if the DP bought nothing there
    return chosen


def _greedy(nodes, fuel, max_range_miles):
    """Exact purchases (in miles of fuel) over a fixed list of stops ending at the destination.

    nodes[0] is the first stop, reached with `fuel` miles in the tank (negative
    when the approach is still owed). Returns [(node, miles bought)] and the
    detour miles driven.
    """
    purchases, detour = [], 2 * nodes[0].detour
    i = 0
    while i < len(nodes) - 1:
        here = nodes[i]
        reachable = [(j, _leg(here, nodes[j])) for j in range(i + 1, len(nodes))
                     if _leg(here, nodes[j]) <= max_range_miles + _EPS]
        cheaper = next(((j, need) for j, need in reachable if nodes[j].price < here.price), None)
        if cheaper:
            target, need = cheaper
            amount = max(0.0, need - fuel)
        else:
            target, need = min(reachable, key=lambda r: (nodes[r[0]].price, -r[0]))
            amount = max_range_miles - fuel   # a negative fuel (approach debt) is settled too
        if amount > _EPS:
            purchases.append((here, amount))
        fuel += amount - need
        detour += 2 * nodes[target].detour
        i = target
    return purchases, detour


def optimize(candidates, total_miles, *, max_range_miles, mpg, start_fuel_miles,
             origin_station=None, stop_penalty=0.0):
    """Return a FuelPlan for a route of `total_miles`.

    candidates: corridor.Candidate list sorted by mile marker.
    origin_station: the station nearest the origin, used only when no candidate
        is within `start_fuel_miles`.
    stop_penalty: dollars a stop must save to be worth making.
    Raises NoFeasibleRoute when some gap is longer than the range.
    """
    plan = FuelPlan(total_miles=total_miles)
    if total_miles <= _EPS:
        return plan

    nodes = _prune([_Node(c.mile_marker, c.detour_miles, c.station.price, c)
                    for c in candidates if c.mile_marker <= total_miles])
    starts = {i: (n.mile + n.detour) / mpg * n.price
              for i, n in enumerate(nodes) if n.mile <= start_fuel_miles}
    if not starts:
        if origin_station is None:
            raise NoFeasibleRoute(0.0, nodes[0].mile if nodes else total_miles, max_range_miles)
        plan.assumed_origin_fill = True
        nodes = [_Node(0.0, 0.0, origin_station.price)] + nodes
        starts = {0: 0.0}
    nodes.append(_Node(total_miles, 0.0, 0.0))  # destination: free, so never overbuy

    chosen = _choose_stops(nodes, starts, max_range_miles, mpg, stop_penalty)
    first = nodes[chosen[0]]
    route = [nodes[k] for k in chosen] + [nodes[-1]]
    purchases, plan.detour_miles = _greedy(route, -(first.mile + first.detour), max_range_miles)

    for node, miles in purchases:
        gallons = miles / mpg
        assumed = node.candidate is None
        plan.stops.append(FuelStop(
            station=origin_station if assumed else node.candidate.station,
            candidate=node.candidate, mile_marker=node.mile, price=node.price,
            gallons=gallons, cost=gallons * node.price, assumed=assumed))
    return plan
