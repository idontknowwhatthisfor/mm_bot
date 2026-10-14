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
- **`RoadGraph(state)`**: adjacency over joined tiles plus car park tiles. Houses and car parks are
  endpoints: cars don't drive through them.
  - `shortest(sources)` returns driving distances and next-tile pointers, by Dijkstra with
    `drive_length` edge weights.
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
  load, shared and cap costs for existing road.
- **`capped(p)`**: true when this colour on tile p would make three colours share it.
- **`join_cost(p)`**: load, shared cost and junction cost of ending a new road on p.

## The search

`search(board, starts, is_goal, bridges, tunnels, goal_cost)` is Dijkstra over `(tile, heading)`,
so bends can be priced and limited.

- Steps go in 8 directions. A straight cardinal run of water or mountain tiles (up to
  `MAX_CROSSING` = 5) can be spanned with a bridge or tunnel. Stepping onto a deck that already has
  road on its first water tile is free and needs no stock. Spans that cross another deck are
  refused.
- With `goal_cost`, the goal minimising `path cost + goal_cost(goal)` wins (the search keeps going
  until nothing cheaper can follow). Without it, the nearest goal wins.
- Returns the path (crossed tiles included) and the number of crossings started.

## Plans

A `Plan` carries a label, a retry key, a colour, the `segments` to draw (in order), every tile
touched, the number of new tiles (≈ concrete), crossings, whether a house is turned, and tiles freed
(shortcuts).

| Function | Builds | Goal and cost |
|---|---|---|
| `plan_house` | road from an unconnected house, turned to any of its 8 sides (`_house_starts`) | its colour's network or car park entrance. `goal_cost` = remaining drive, plus 25 per capped tile on it, plus join cost, plus `COST_OTHER_NETWORK` if a pressured network is preferred |
| `plan_destination` | road from a car park entrance to the houses of its colour | a tile on a network with its houses, or a tile an unconnected house could face. `goal_cost` = the colour's houses' average drive to that tile |
| `plan_feeder` | road from a house's driveway tile (house on another network) to a destination's network | used for destinations short of cars. Colour costs are off |
| `plan_bypass` | all-new road from a car park entrance to a far tile of its network that houses drive through | must take `MIN_BYPASS_SAVING` (4) tiles off some trip |
| `plan_shortcut` | reconnection for a connected house whose private road (`house_spur`) is longer than a fresh one | needs fewer new tiles than it frees; no trip may get longer; no tile may gain a third colour |

`Board.claim(plan)` reserves a plan's tiles so later plans in the same tick share them instead of
colliding.

## Cleanups

| Function | Removes | Safeguards |
|---|---|---|
| `dead_ends` | chains ending in a tile with one link, loose end first | keeps house access tiles, car park entrances, roundabout centres, ring tiles, traffic lights, and bridges and tunnels with the tiles next to them |
| `redundant_tiles` | the extra side of a loop, doubled car park links, and what dead-ends after | every destination stays fed; no trip gets longer (`MAX_DETOUR_LOOP` = 0); no tile gains a third colour; waits while a `separation_cut` is pending |
| `separation_cut` | the road tile (plus its dead chain) whose removal leaves the fewest tiles with three or more colours | every house keeps every destination it reached; no trip grows by more than `MAX_SEPARATION_DETOUR` (8) |
| `unfinished_crossings` | (repairs) segments missing from bridges and tunnels | |

`_protected()` is the shared keep-set for the optimising passes.

## Traffic

- **`route_colours(state, graph)`**: for every road tile, which colours' houses drive over it, and
  how many. It follows each house's shortest drive to the nearest destination of its colour, so it
  predicts real car routes: the game never routes around jams. `route_loads` sums it.
- **`mixed_tiles`**: tiles with more than two colours.
- **`junctions_by_load(state, load, min_load)`**: plain junction tiles (3+ links, no upgrade yet)
  carrying at least `min_load` houses, busiest first.
- **`trip_routes(state, group, dest, colours)`**: the existing-road drive from every road tile to
  the colour's car parks, plus how many capped tiles lie on that drive.
