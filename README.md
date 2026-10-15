# Fuel Route Optimizer

A Django API that takes a start and finish location in the USA and returns the
route, where to buy fuel along it, and the total fuel cost, for a truck with a
500-mile range at 10 miles per gallon. Fuel prices come from the provided CSV
(`data/fuel-prices-for-be-assessment.csv`); routing uses the free
[OpenRouteService](https://openrouteservice.org/) directions API.

- `POST /api/route/` returns JSON: route geometry, fuel stops, gallons, cost.
- `GET /api/route/map/?start=...&finish=...` shows the same plan on a map.
- **One routing-provider call per uncached request, zero on a cache hit.**
  Start and finish are geocoded locally from Census data, and stations are
  matched to the route in memory.

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then set ORS_API_KEY (free key from openrouteservice.org)
python manage.py migrate

# US places from the 2026 Census Gazetteer (downloads into data/, about 6 s)
python manage.py load_places

# Fuel stations from the CSV
python manage.py load_fuel_prices data/fuel-prices-for-be-assessment.csv --skip-nominatim

python manage.py runserver
```

`--skip-nominatim` keeps setup offline apart from the Census download. In this
mode the 197 stations (3% of 6,626) in the 143 cities that the Census files
don't list are skipped, leaving 6,429. Without the flag, the loader looks those cities up
once on OpenStreetMap Nominatim (1 request per second, about 2.5 minutes) and
stores the answers, so later loads make no calls.

Run the tests with `python manage.py test routing`. They never call ORS: they
use a mocked client or the saved responses in `routing/tests/fixtures/`.

## API

### `POST /api/route/`

```json
{"start": "Dallas, TX", "finish": "Houston, TX"}
```

| Field | Required | Notes |
|---|---|---|
| `start`, `finish` | yes | `"City, ST"`, `"City, State"`, an optional trailing `", USA"`, or `"lat,lng"`. Must be in the USA. |
| `corridor_miles` | no | How far off the route a station may be, 1-50. Default 10. |
| `stop_penalty` | no | Dollars a stop must save to be worth making, >= 0. Default 0 (pure fuel cost). See [Assumptions](#assumptions). |

Response (real output for this request; the route geometry is shortened here):

```json
{
  "start": {"query": "Dallas, TX", "label": "Dallas, TX", "lat": 32.793333, "lng": -96.766513},
  "finish": {"query": "Houston, TX", "label": "Houston, TX", "lat": 29.785743, "lng": -95.388806},
  "route_miles": 238.7,
  "detour_miles": 0.3,
  "total_driving_miles": 238.9,
  "route_geojson": {"type": "LineString", "coordinates": [[-96.76642, 32.79325], [-96.77048, 32.78195], "...", [-95.38881, 29.78576]]},
  "fuel_stops": [
    {
      "name": "One9 #1248",
      "address": "I-45, EXIT 271",
      "city": "Wilmer",
      "state": "TX",
      "opis_id": 63669,
      "location": {"lat": 32.597428, "lng": -96.680905, "precision": "city"},
      "route_point": {"lat": 32.59555, "lng": -96.68154, "mile_marker": 16.0},
      "detour_miles": 0.1,
      "price": 2.756,
      "gallons": 23.89,
      "cost": 65.84,
      "assumed": false
    }
  ],
  "total_gallons": 23.89,
  "total_fuel_cost": 65.84,
  "assumed_origin_fill": false,
  "meta": {
    "external_api_calls": 1,
    "cache_hit": false,
    "elapsed_ms": 186.6,
    "timings": {"resolve_ms": 0.3, "cache_get_ms": 0.0, "routing_ms": 175.8, "corridor_ms": 3.4, "optimize_ms": 3.0},
    "data_version": 4,
    "stop_penalty_dollars": 0.0,
    "corridor_miles": 10.0,
    "ors_profile": "driving-hgv",
    "ors_summary_miles": 238.7,
    "route_warnings": []
  }
}
```

| Field | Meaning |
|---|---|
| `route_miles` | Driving distance along the route, measured on the ORS route polyline (the distances the optimizer uses). `meta.ors_summary_miles` is ORS's own figure; on the measured routes they agree to 0.1 mi. |
| `fuel_stops[].detour_miles` | **One way**: approximate straight-line distance from the station's city to its nearest route point. |
| `detour_miles` | **Sum of round trips** (2 x one-way) over the stops made. |
| `total_driving_miles` | `route_miles + detour_miles`. `total_gallons` = this / 10. |
| `fuel_stops[].location` | The station's city coordinates (`"precision": "city"`). |
| `fuel_stops[].route_point` | Nearest point on the route, with its mile marker. The map puts markers here. |
| `total_fuel_cost` | Fuel spending only, in dollars. Never includes the stop penalty. |
| `assumed_origin_fill` | `true` when no station is within 25 miles of the start (see Assumptions). |
| `route_geojson` | GeoJSON LineString, `[lng, lat]`, about one point per mile. |

Errors are JSON, `{"error": {"code": ..., "message": ...}}`:

| Status | When |
|---|---|
| 400 `invalid_request` | Missing or out-of-range fields (`fields` lists them). |
| 400 `invalid_location` | Unknown place, not a US state, or coordinates outside the USA (`field` says which). |
| 422 `no_feasible_plan` | Some stretch is longer than the range with no station in the corridor (`gap.from_mile`/`to_mile`). |
| 422 / 502 / 503 / 504 `routing_error` | No drivable route, provider failure, rate limit, timeout. |

### `GET /api/route/map/?start=Chicago, IL&finish=Los Angeles, CA`

A Leaflet page with the route and a marker per fuel stop (popups show station,
price, gallons and cost). It accepts the same optional `corridor_miles` and
`stop_penalty` and uses the same cache as the JSON endpoint. The map tiles and
the Leaflet library load in the browser and are not part of the routing-call
budget above.

Example requests for an HTTP client are in [`requests.http`](requests.http).

## How it works

```
POST /api/route/                         routing/views.py: route()
  -> validate input                      routing/serializers.py
  -> plan_route()                        routing/services/planner.py
       resolve start/finish locally      routing/services/locations.py
       cache lookup                      (hit: skip to the response)
       one ORS directions call           routing/services/ors_client.py
       stations within the corridor      routing/services/corridor.py
       choose stops and gallons          routing/services/optimizer.py
  -> JSON (or the map page)              routing/templates/routing/map.html
```

- **Data loading** (`load_places`, `load_fuel_prices`): Census place and county
  subdivision names are parsed and normalized (e.g. "Kansas City city" becomes
  "Kansas City", "St"/"Saint" match). The CSV is cleaned: Canadian rows are
  dropped and duplicate station IDs are merged. Each reload bumps a data version,
  which rebuilds the in-memory indexes and invalidates cached routes.
- **Station matching**: stations live in a KD-tree over 3D unit vectors, so
  distances are correct anywhere in the US. The route is resampled to one point
  per mile with a mile marker each; every station within the corridor gets its
  nearest route point.
- **Optimizer**: a dynamic program over (station, fuel level on a 0.1-gallon
  grid) chooses the stops, minimizing fuel cost (plus the optional per-stop
  penalty). The classic greedy (buy just enough to reach a cheaper station,
  otherwise fill up) then sets the exact gallons at those stops.
- **Cache**: Django's cache (Redis when `REDIS_URL` is set, otherwise
  in-memory), keyed on the resolved places plus every input that changes the
  plan, so "Chicago, IL" and "chicago, illinois" share an entry.

## Performance

Measured through the real endpoint on a local development machine (in-memory
cache, ORS free tier, `driving-hgv` profile):

| Route | Miles | Cold | of which ORS | Cached |
|---|---|---|---|---|
| Chicago, IL -> Los Angeles, CA | 2,027 | 1,410 ms* | 1,106 ms | 1.2 ms |
| New York, NY -> Miami, FL | 1,289 | 416 ms | 356 ms | 1.1 ms |
| Dallas, TX -> Houston, TX | 239 | 187 ms | 176 ms | 1.4 ms |

\* The first request after startup also builds the city and station indexes
(about 270 ms, once per process and data version).

Everything except the ORS call takes about 10-60 ms per request; the provider
call dominates. `python manage.py replay_routes --penalty 0 5` re-runs these
routes from the saved responses, with no ORS calls.

## Assumptions

- **Prices:** the CSV repeats some station IDs with different prices (probably
  different products, which the file doesn't say). Each station's price is the
  mean of its rows.
- **Starting fuel:** the truck starts with just enough fuel to reach the first
  station; that fuel is included in the trip cost at the first station's price.
  The first station must be within 25 miles of the start. If none is, the plan
  assumes a fill-up at the start at the nearest station's price and sets
  `assumed_origin_fill`.
- **Corridor:** stations up to 10 miles from the route count (adjustable per
  request).
- **Station locations are city-level:** the CSV gives highway/exit text such as
  `I-45, EXIT 271`, not coordinates, so each station is placed at its city's
  coordinates and snapped to the route for display. Detours are straight-line
  distances from those city points, so they are approximate, and a plan's range
  feasibility is checked on these modeled distances rather than on real roads.
- **Detours are paid for:** reaching a stop and returning to the route costs 2 x
  its one-way detour, which counts against the range and is included in the
  gallons and the cost.
- **Stop penalty (optional):** `stop_penalty` trades a little fuel cost for
  fewer, larger stops (for example, $5 turns 9 stops into 6 on Chicago -> Los
  Angeles for $2.94 more fuel). It only influences which stops are chosen and
  never appears in `total_fuel_cost`. The default is 0.
- **The optimizer is approximate:** stops are chosen on a 0.1-gallon fuel grid
  with each leg rounded up, which keeps every plan feasible within the model.
  That rounding is a feasibility safeguard, not an optimality bound.

## In production I would...

- Load prices from a daily feed keyed by station + product + date, on a
  schedule, instead of a one-off CSV, and use the fleet's negotiated prices.
- Geocode stations precisely (e.g. OpenStreetMap motorway junctions for the
  exit numbers, or a data provider with station coordinates), so detours and
  range checks use real positions.
- Use real road detour distances to the shortlisted stations, and the
  truck's actual fuel level instead of a starting-fuel assumption.
- Run behind a production server with Redis as the shared cache, and a paid or
  self-hosted routing service for higher limits.
