# The planner (`planner.py`)

Everything here is a function of a `State` and the terrain. Nothing talks to the game.

## Connectivity

- **`Network(state)`**: union-find over joined tiles. A car park's tiles are joined to each other.
  `net.find(p)` is a component's root.
- **`group_roots(state, net)`**: for each colour, the components holding an active destination of
  that colour.
- **`is_served(house, net, roots)`**: the house's component holds a destination of its colour.
  Cars only reach destinations on their own network.
- **`unfed_destinations(state, net)`**: active destinations whose network holds no house of their
  colour. They get no cars at all.
- **`RoadGraph(state)`**: adjacency over joined tiles plus car park tiles.
  - **Endpoints (`stops()`):** houses, and car parks with one entrance. Cars drive to or from them,
    never through.
  - **Drive-through car parks:** a car park with **two entrances** can be driven through, in at one
    and out at the other. The cars' own routes showed the game sends other colours' cars through
    such car parks; see [game-model.md](game-model.md#cars-sessionread_cars).
  - **Switches:** `DRIVE_THROUGH_CARPARKS = False` puts back the old model, where every car park is
    a dead end. `endpoints()` still lists every house and car park, so `prune` never clears them.
  - `shortest(sources)` returns driving distances and next-tile pointers, by Dijkstra with
    `drive_length` edge weights. A step between two tiles of one car park costs
    `PARK_DRIVE_FACTOR` (3.0) × their octile distance, because car park lanes are slow and the game
    routes by travel time. Replayed over 2,601 car checks from 53 snapshots (`tools/routecheck.py`):

    | Model | Match rate |
    |---|---|
    | factor 2.0 | 84.1% |
    | **factor 3.0** | **84.7%** |
    | factor 4.0 and up | 84.4% |
    | every car park a dead end | 84.3% |

    At high factors drive-throughs are almost never predicted. The gain is small, but the dead-end
    model can't produce the routes cars were seen driving: colour-4 cars through a shared colour-2/3
    car park (`tests/data/`). At 2.0, the same replay found 6 cars matched only with drive-through
    and 16 matched only without. All 16 were sent through a shared car park of other colours on a
    detour the game didn't take.
  - `evaluate()` returns the trip length from every house to every active destination of its
    colour, plus which destinations some house reaches.

## The cost model

Costs are in **tiles of driving**, because what wins games is cars arriving quickly.

| Constant | Value | Meaning |
|---|---|---|
| `COST_DRIVE` | 1.0 | per tile driven; a diagonal step is `DIAGONAL` = √2 tiles |
| `COST_NEW_TILE` | 0.5 | building a road tile, on top of driving it |
| `COST_TREE` | 0.05 | extra for building over a tree |
| `TURN_COST` | (0, 0.05, 0.5) | by bend size in 45° steps; cars slow down for right angles |
| `MAX_TURN` | 2 | no bend sharper than 90° |
| `COST_BRIDGE` | 4.0 | per bridge or tunnel started (scarce) |
| `COST_REMAINING_TRIP` | 1.0 | the existing-road drive from the join point to the destination |
| `LOAD_FREE`, `COST_LOAD` | 6, 0.15 | per house over 6 using a road tile |
| `COST_JUNCTION` | 0.2 | ending a new road on a tile that becomes a junction |
| `COST_SHARED`, `COST_SHARED_JOIN`, `COST_SHARED_LOAD` | 0.5, 1.0, 0.05 | driving on or joining road other colours use |
| `COST_COLOUR_CAP` | 25 | per road tile on the trip that two other colours already use |
| `COST_OTHER_NETWORK` | 3.0 | preferring a pressured destination's network |
| `USE_JAM_COST`, `JAM_FREE`, `COST_JAM` | True, 0.5, 1.0 | measured jams (below): per car stopped in a queue on a road tile, above 0.5 on average, driving along or joining it costs one more tile |

The same `drive_length` and `step_length` are used everywhere: the search, `trip_routes`,
`RoadGraph.shortest` (predicted car routes) and bypass savings.

### `Board`

`Board` is the search's view of one tick. It holds:

- `state` and `terrain`;
- `refused`: moves the game recently rejected;
- `planned` and `planned_corners`: tiles and diagonal corners claimed by plans earlier in the same
  tick, so plans don't collide;
- `load` and `colours`: houses per road tile, in total and per colour, from `route_colours`;
- `group`: the colour being planned, which enables the shared-road and cap costs;
- `road_allowed`: when set, the only existing road the search may step on (bypasses must be all
  new road);
- `jam`: the measured jam level per road tile (`traffic.JamTracker`: the average number of cars
  stopped in a queue on it). The bot passes `{}` when the jam cost is off;
- `rings`: each roundabout's 4 ring tiles.

Main methods:

- **`passable(p)`**: inside the city limits, driveable, and not a house, destination, car park or
  boat terminal.
- **`move_allowed(p, d)`** blocks:
  - moves the game refused;
  - leaving a roundabout centre except along an existing link;
  - drawing between two ring tiles of the same roundabout;
  - a diagonal crossing another diagonal (existing or planned) at the same corner.
- **`step_cost(p, d)`**: driving cost, plus `COST_NEW_TILE` (and `COST_TREE`) for new tiles, or
  load, jam, shared and cap costs for existing road.
- **`jam_cost(p)`**: `COST_JAM × max(0, jam[p] − JAM_FREE)`, or 0 with `USE_JAM_COST = False`
  (`bot.py --no-jam-cost`).
- **`capped(p)`**: true when this colour on tile p would make three colours share it.
- **`join_cost(p)`**: load, jam, shared cost and junction cost of ending a new road on p.

The jam cost reaches every search that drives along or joins existing road: new connections,
destinations, feeders, bypasses (`replace(board, …)` keeps it), separate roads, and shortcuts
(`plan_shortcut` takes `jam=`).

## The search

`search(board, starts, is_goal, bridges, tunnels, goal_cost)` is Dijkstra over `(tile, heading)`,
so bends can be priced and limited.

- Steps go in 8 directions. A straight cardinal run of water or mountain tiles (up to
  `MAX_CROSSING` = 5) can be spanned with a bridge or tunnel. Stepping onto a deck that already has
  road on its first water tile is free and needs no stock. Spans that cross another deck are
  refused.
- With `goal_cost`, the goal minimising `path cost + goal_cost(goal)` wins (the search keeps going
  until nothing cheaper can follow). Without it, the nearest goal wins.
- With `through_goals=True` the search also drives on past goal tiles. `plan_house`,
  `plan_destination` and `plan_separation` use it: otherwise the first tile of the house's own
  network ends every path, and a house inside a network whose only way to its car park is a shared
  road could never build a way round it. Shortcuts leave it off, for speed.
- With `bound(tile)` the search is A*: `entrance_bound` gives the straight-line distance to the
  nearest car park of the colour, which no remaining trip can beat, so the search stops sooner and
  still finds the cheapest plan. Goals whose remaining trip is taken as 0 (tiles claimed this tick)
  count as car parks for the bound.
- A plan that isn't turning its house always ends with the house joined to its driveway tile, even
  when that tile already has road.
- Bridges and tunnels are counted separately, each against its own stock.
- Returns the path (crossed tiles included) and `(bridges started, tunnels started)`.

## Plans

A `Plan` carries a label, a retry key, a colour, the `segments` to draw (in order), every tile
touched, the number of new tiles (≈ concrete), bridges and tunnels started (`crossings` is their
sum), whether a house is turned, and tiles freed (shortcuts).

| Function | Builds | Goal and cost |
|---|---|---|
| `plan_house` | road from an unconnected house, turned to any of its 8 sides (`_house_starts`) | its colour's network or car park entrance. `goal_cost` = remaining drive, plus 25 per capped tile on it, plus join cost, plus `COST_OTHER_NETWORK` if a pressured network is preferred |
| `plan_destination` | road from a car park entrance to the houses of its colour | a tile on a network with its houses, or a tile an unconnected house could face. `goal_cost` = the colour's houses' average drive to that tile |
| `plan_feeder` | road from a house's driveway tile (house on another network) to a destination's network | used for destinations short of cars. Colour costs are off |
| `plan_bypass` | all-new road from a car park entrance to a far tile of its network that houses drive through | must take `MIN_BYPASS_SAVING` (4) tiles off some trip |
| `plan_separation` | a road of its own for a house whose cars share a road with two other colours, from the house (over its own network) to the car park of a destination it now reaches over such a road | uses no tile two other colours use; at most `MAX_SEPARATION_ROAD` (10) new tiles; kept only if the map has fewer 3-colour tiles once it is drawn (`apply_plan`) and the `separation_cut` it allows is made (`clear_tiles`), since a new road can also give other colours a shorter way onto shared roads |
| `plan_shortcut` | reconnection for a connected house whose private road (`house_spur`) is longer than a fresh one | searched with the same load and colour costs as `plan_house`; needs fewer new tiles than it frees; no trip may get longer (`MAX_DETOUR` = 0); no tile may gain a third colour |

`Board.claim(plan)` reserves a plan's tiles so later plans in the same tick share them instead of
colliding.

## Cleanups

| Function | Removes | Safeguards |
|---|---|---|
| `dead_ends` | chains ending in a tile with one link (or none), loose end first, via `RoadGraph.prune` | keeps the `_protected` tiles: house access tiles, car park entrances, roundabout centres, ring tiles, traffic lights, and bridges and tunnels with the tiles next to them |
| `redundant_tiles` | the extra side of a loop, doubled car park links, and what dead-ends after | every destination stays fed; no trip gets longer (`MAX_DETOUR` = 0); no tile gains a third colour; waits while a `separation_cut` is pending (the bot passes the cut it already has, `[]` once the game refused it) |
| `separation_cut` | the road tile (plus its dead chain) whose removal leaves the fewest tiles with three or more colours | every house keeps every destination it reached; no trip grows by more than `MAX_SEPARATION_DETOUR` (8) |
| `unfinished_crossings` | (repairs) segments missing from bridges and tunnels | |
| `reclaimable_crossings` | (for `Bot.reclaim_crossing`) whole bridges and tunnels (`crossings()`: joined deck tiles and their kind) whose removal keeps every fed destination fed and every connected house connected | ranked by house-to-destination drives lost, then the longest trip increase |

`_protected()` is the shared keep-set for the dead-end and optimising passes.

## Traffic

- **`house_routes(state, graph)`**: for each house, the road tiles its cars drive over: its
  shortest drive to every active destination of its colour (`COUNT_EVERY_DESTINATION`, since cars
  serve all of them), or only the nearest with `every_destination=False`.
- **`route_colours(state, graph)`**: for every road tile, which colours' houses drive over it, and
  how many, from `house_routes`. These are predicted routes. `traffic.check_routes` compares them
  with the routes the game gives its cars. Over 2,601 car checks replayed from 53 late-game
  snapshots:
  - **84.7% match**;
  - **6.2% take another route of the same length:** the game breaks ties its own way;
  - **5.6% take a different route;**
  - **3.5% are off the planned route.**

  In-game, where checks run all game long, 91.6–93.8% matched (24,617 checks over 12 games). The
  mismatches weren't worked out further. Likely causes are lanes with half the normal speed limit
  (the game routes by `PathfindingCost`, the planner by tile length), and cars keeping a route they
  set out with before the bot changed the roads.
  This feeds the colour cap, `mixed_tiles`, `separation_cut` and `separation_candidates`.
- **`route_loads(state, graph)`**: houses per road tile on their drive to the nearest destination
  only, since the traffic thresholds were tuned that way.
- **`trip_routes(...)`**: the capped-tile count it returns adds up the drives to every destination
  of the colour, so joining a network whose road to a far destination is shared costs too.
- **`separation_candidates(state)`**: houses on a road with 3+ colours, colours with the fewest such
  houses first.
- **`mixed_tiles`**: tiles with more than two colours.
- **`junctions_by_load(state, load, min_load)`**: plain junction tiles (3+ links, no upgrade yet,
  not a roundabout's centre or ring tile) carrying at least `min_load` houses, busiest first.
- **`trip_routes(state, group, dest, colours)`**: the existing-road drive from every road tile to
  the colour's car parks, plus how many capped tiles lie on that drive.

## Measured traffic (`traffic.py`)

`traffic.py` reads the cars that `Session.read_cars()` returns. It doesn't talk to the game.

- **`queued(car, threshold)`**: a car is driving, on a road tile, slower than the game's
  `CarStoppedSpeedThreshold` (0.001), and stopped behind a leading vehicle or a blocking
  intersection.
- **`JamTracker`**: per road tile, a time-weighted rolling average of queued cars, with time
  constant `JAM_WINDOW` = 10 game seconds. Reads at the same game time (while paused) change
  nothing, and levels below 0.01 are dropped. `jammed()` lists the tiles at `JAM_LEVEL` (1.5) or
  above, for the log.

  Two cars waiting 3 seconds at a light peak at about 0.5. Three cars stuck for 30 seconds reach
  about 2.9.
- **`check_routes(state, traffic)`**: for each driving car, it compares the game's remaining
  `path` with the planner's model.
  - **The planner's route:** `RoadGraph.shortest` from the house to the car park the car was sent
    to, or from that car park home, taken from the car's current tile onward.
  - **Clean-up before comparing:** car park tiles are dropped from both routes, and roundabout ring
    tiles are collapsed onto their centre.
  - **`match`:** the car's tiles lie on the planned route, in order. Gaps are fine, since the
    game's path omits some lanes.
  - **`tie`:** other tiles, but within `TIE_TILES` (1) of the same length, using the game's
    `pathLength` / 2.0.
  - **`different`:** other tiles, and a different length.
  - **`off route`:** the car's current tile isn't on the planned route at all.

  Each result also says whether the car stays on `house_routes` for its house.
- **`TrafficDay`**: collects one game day of reads and checks for the daily log lines.
