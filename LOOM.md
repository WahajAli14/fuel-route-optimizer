# Loom outline (5 minutes max)

**0:00-0:30 Problem.** Start and finish in the USA -> route map, where to fuel,
total fuel cost. 500 mi range, 10 mpg, prices from the CSV, a free routing API
called as little as possible.

**0:30-2:15 Demo in Postman** (requests from `requests.http`).
1. Dallas, TX -> Houston, TX: one stop, total cost. Point at `meta`:
   `external_api_calls: 1`, timings, `ors_profile`.
2. Chicago, IL -> Los Angeles, CA: several stops; show a stop's `route_point`,
   `location` with `"precision": "city"`, `detour_miles`.
3. Send it again: `cache_hit: true`, `external_api_calls: 0`, about 1 ms.
4. Same route with `"stop_penalty": 5`: fewer stops, slightly higher fuel
   cost, and the penalty is not in `total_fuel_cost`.
5. `"Toronto, ON"`: 400 with a clear message.

**2:15-2:45 Map page.** Open `/api/route/map/?start=Chicago, IL&finish=Los
Angeles, CA`: route line, markers on the route, a popup with price, gallons and
cost.

**2:45-4:30 Code tour.**
- `routing/views.py` -> `services/planner.py::plan_route()`: the whole pipeline
  and the cache (key from resolved places; request fields rebuilt per call).
- `services/locations.py`: start/finish geocoded locally from Census data, so
  the only external call is the route.
- `services/corridor.py`: KD-tree over stations, route resampled per mile,
  nearest route point per station.
- `services/optimizer.py`: why the textbook greedy alone made 19 stops; DP on a
  0.1-gallon grid chooses stops, greedy sets exact gallons; detours paid for;
  starting-fuel rule.
- `management/commands/load_fuel_prices.py`: CSV cleaning (Canadian rows,
  duplicate IDs -> mean price).
- Tests: ORS mocked or replayed from saved responses.

**4:30-5:00 Trade-offs.** City-level station coordinates (CSV has exit text
only) -> approximate detours; what I'd do in production (daily price feed,
precise exit geocoding, negotiated prices).
