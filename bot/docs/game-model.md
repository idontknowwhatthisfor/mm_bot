# The game model (`game.py`)

## Coordinates and directions

- A position `Pos` is `(x, y)` in tile coordinates. **+y is north.**
- Directions follow the game's `TileDirection`: `N, NE, E, SE, S, SW, W, NW = 0..7`, clockwise.
  `DIRS[d]` is the step `(dx, dy)`, `opposite(d) = (d + 4) % 8`, and `neighbor(p, d)` is the
  adjacent tile. `CARDINALS = (N, E, S, W)`.
- A diagonal step joins two tiles that only touch at a corner. On the map it's as long as √2
  straight steps. In the road inventory it costs one tile, like a straight step.

## Roads

Each tile has a road state for each of its 8 directions (`roads[p][d]`, a `RoadState`:
0 none, 2 planned, 4 pending, 8 active, 16 mothballed). **Two tiles are joined only when each points
at the other** (`State.joined(p, d)`).

**Mothballed roads are read as gone.** `read()` drops the `MOTHBALLED` (16) bit. Clearing a bridge
or tunnel deck tile makes a `RemovePassagesEdit`. The game then marks the deck and its approach
links mothballed and counts the upgrade in `UpgradeDatabase._mothballedUpgrades`
(`State.mothballed`). Once the cars on it have left (about 8 game seconds when checked live), the
roads go and the upgrade returns to `_availableUpgrades`. Read as road, a mothballed bridge would
let plans route over it again while it comes down. A house or car park has a stub pointing at its access tile,
so it joins the network by the same rule.

`State` helpers:

| Method | Meaning |
|---|---|
| `has_road(p)` | any direction set |
| `road_toward(p, d)` | p's own half-link toward d |
| `joined(p, d)` | both halves present |
| `links(p)` | number of joined directions |
| `in_playable(p)` | inside the current city limits |

## The snapshot: `State`

| Field | Source in the game |
|---|---|
| `roads`, `content` | `TilemapModel._tiles[*]._tile._twoLaneRoadState`, `.ContentType` |
| `houses` | `HouseModel`: colour `GroupIndex`, `DrivewayDirection`, idle cars `waitingVehicles`, owned cars `ownedVehicles` |
| `destinations` | `DestinationModel`, one entry per destination (it covers several tiles) |
| `carparks` | `CarparkModel`: tiles and entrances (outside tile + direction) |
| `roundabouts`, `traffic_lights` | `_isCenterOfRoundabout`, `_hasTrafficLight` |
| `available` | `UpgradeDatabaseModel._availableUpgrades`, indexed by `Upgrade` |
| `mothballed` | `UpgradeDatabaseModel._mothballedUpgrades`: taken down, not back in stock yet |
| `pending_choices` | `pendingUpgradeChoices._size` (an upgrade screen is waiting) |
| `week, day, hour, time, expansion_time, paused` | `ClockModel` |
| `score`, `ended` | `ScoreModel.Score`, `Game.HasGameEnded` |
| `playable` | `City.IsTileInPlayableArea`, re-evaluated only when the limits move |

Cars aren't part of `State`. They're read on their own schedule by `Session.read_cars()` (below).
`snapshot(state, terrain, traffic)` also saves the latest car read, and `load_traffic(data)` gives it
back.

`Destination` fields the strategy relies on:

| Field | Game field | Meaning |
|---|---|---|
| `unassigned` | `unassignedDemand._size` | pins no car has been sent to yet: the colour lacks cars |
| `en_route` | `waitingDemand._size` | pins whose car is on its way: high when routes are slow |
| `served` | `totalServicedPins` | grows by one per arrival |
| `pin_limit` | `MaximumDemandBeforeTimerStarts` | above this the overcrowd timer runs (6, or 9 upgraded) |
| `overcrowd_time`, `max_overcrowd_time` | `CurrentFrame._overcrowdingTime`, `_constants.MaxOvercrowdTime` | the game is lost when the timer reaches the maximum |
| `pressure` (property) | | `(unassigned + en_route) / pin_limit + timer / max` |

Terrain (`Terrain`: driveable, water, mountain, buildable) is read once per game for the whole
playable area, with `CityDefinition.TileIsDriveable/OverWater/UnderAMountain/Buildable`.

### How a read works

`Session.read()` re-attaches if the active game changed, releases per-tick handles, and makes
**one batched request**: all tiles through `TILE_PROJECTION`, plus the inventory, clock, score and
the game-over flag. Fields the game can't produce come back as `null` or `{"$err": …}`, and
`_value()` turns those into defaults. Then it builds the `State`.

### Roundabouts in the snapshot

A roundabout is a one-way diamond ring through the 4 tiles N, E, S and W of its centre. The centre
holds no road, and the ring connections live outside `_twoLaneRoadState` (details in
[`notes/roundabouts.md`](../notes/roundabouts.md)). Read raw, the roads around a roundabout look
cut off from each other. `join_roundabouts()` fixes that before the `State` is built: it joins the
centre both ways to its 4 ring tiles, and to every other neighbour whose road points at the centre
(diagonal arms included). The planner then sees the roundabout as one junction at its centre.

## Cars: `Session.read_cars()`

`read_cars()` returns a `Traffic` (game time, every `Car`, the stopped-speed threshold, how long the
read took). It is **one batched request** with `handles=False`:

1. every tile's `roadChunk.lanes` (to map lanes to tiles);
2. every diagonal corner's lanes (`TilemapModel._tileCorners[*].value.roadChunk.lanes`);
3. every house's `ownedVehicles`, each projected through `CAR_PROJECTION`;
4. `RoadChunkModel.CarStoppedSpeedThreshold` (a private static, read from the `$class` root);
5. the clock.

| `Car` field | Game field | Notes |
|---|---|---|
| `id` | `VehicleModel.id` | |
| `house`, `colour` | the owning house's tile and `GroupIndex` | |
| `state` | `behaviorState` | `CarState`: 0 waiting at home, 1 driving to a destination, 2 parking, 3 parked, 4 driving home, 5 realigning on the driveway |
| `destination` | `destination` (`$p`) | the same address as `Destination.key`; also set while driving home |
| `tile` | `CurrentFrame.lane` mapped to a tile | `None` on a car park lane |
| `in_carpark` | `CurrentFrame.lane._isCarparkLane` | |
| `speed` | `CurrentFrame.speed` | world units per game second |
| `obstacle` | `CurrentFrame.nearestObstacle` | `Obstacle`: 0 none, 1 target, 2 leading vehicle, 3 blocking intersection, 4 hotswapping lane |
| `gap` | `CurrentFrame.distanceToLeadingVehicle` | `None` when nothing is ahead |
| `path` | `path` (lanes) mapped to tiles | the route **still ahead** |
| `path_length` | `pathLength` | world units still to drive |

Unreadable fields fall back to defaults through `_value()`, as in `read()`.

### What the live probes showed

These come from `tools/cars.py` and two probe sessions on Tokyo at weeks 5–8, with 100–110 cars.

- **Worked as the decompile says:**
  - `ownedVehicles`, `behaviorState`, `destination` and `pathLength`;
  - all the `CurrentFrame` fields (`CurrentFrame` works for cars as for destinations);
  - `LaneModel` `SpeedLimit`, `PathfindingCost`, `_isCarparkLane` and `connection`;
  - `CarStoppedSpeedThreshold`, which reads as 0.001.

  Every driving car's `destination` matched a destination in `State`.
- **`path` is the route still ahead.** It shrinks as the car drives and starts after the lane the
  car is on. `LastCommittedLane` is still in `path` (13 of 13 cars checked).
- **`distanceToLeadingVehicle` is 2147483648 (2³¹) when no car is ahead.** Anything at or above
  `NO_LEADER_GAP` (1e9) is read as `None`.
- **Some lanes map to no tile.**
  - **Car park lanes:** every lane that matched no tile and no corner was a car park lane. That
    covers cars parking, parked, or pulling out to drive home. They are left out of `path`.
  - **Diagonal corners:** a corner chunk is keyed by `(tileCoordinate, cornerDirection)`, and that
    tile can be one of the side tiles, not on the road. Corner lanes are left out of `path`. A car
    on a corner counts as being on the tile it drives into.
  - **Roundabouts:** the ring's lanes are on the centre tile.
  - **Gaps:** a car's `path` can therefore skip tiles, so the route check allows gaps.
- **Queues:**
  - With no jam, cars behind a leading vehicle still move: median speed 1.2, median gap 2.4–3.4.
  - In a real jam they stop completely (below 0.001). The calibration game hit 12 cars stopped at
    once near game over.
  - The median driving speed is about 1.2, and the maximum about 2.9.
- **`RoadChunkModel._laneSpeedLimitScale`** is 1 on every road tile (120 checked), so there is no
  per-tile slowdown.
- **Timing of one `read_cars()`:**
  - 24–39 ms with about 100 cars (357 tiles, 1,048 lanes, 732 of them on corners);
  - 27–30 ms with 154–194 cars late in a week-10 game.

  The bot reads cars once per game second (`CAR_READ_EVERY`) and checks routes every 5
  (`ROUTE_CHECK_EVERY`). `traffic.check_routes` takes about 12 ms with 194 cars and 99 houses.

### How the game picks routes

The game routes by travel time:

- **`LaneModel.PathfindingCost` = ⌊10 × length / SpeedLimit⌋** on every lane shape checked.
- **Lengths:** a tile is **2.0** world units. A straight lane measures 2.0, and a diagonal step is
  2.0 plus a 0.828 corner lane, i.e. 2√2.
- **Turning lanes:**
  - **45° bend:** speed limit 1, length 1.74–2.04, cost 17–20 (a straight tile costs 20);
  - **90° turn:** speed 0.667, length 1.33–1.86, cost 19–27;
  - **135° turn:** speed 0.333, cost 15–44;
  - **u-turn:** speed 0.333, cost 60.

  So apart from u-turns, a turn costs about as much as a straight tile.
- **Half-speed lanes:** some single straight and 45° lanes have speed limit 0.5, which doubles
  their cost. What causes it wasn't worked out.
- **Car parks** with two entrances can be driven through, and the game does route other colours'
  cars through them. Their lanes are slow: entrance lanes run at 0.5 (cost 24–26), and the lanes
  past the spaces at 0.167–1 (cost 13–72). The planner models this as `PARK_DRIVE_FACTOR` (see
  [planner.md](planner.md)).

## Attaching to a game

`Session.attach()` finds the newest non-menu game among the app scope's child scopes. It resolves
the game services through the game scope's `Get(Type)`: `City`, `TilemapModel`, `TileEditor`,
`ISimulation`, `UpgradeDatabaseModel`, `ClockModel` and `ScoreModel`. It also:

- swaps the leaderboard backend for the game's own `NullLeaderboardBackend`, so bot games never
  post scores (only while that game process runs);
- scans the terrain.

`Session.game_key` changes when a new game starts, which `Bot.run` uses to reset its memory.

## Actions

All edits go through the game's `TileEditor`, which validates them and returns a
`TileEditResult {resultCode, edit}`. A successful edit is wrapped in `EditTileCommand.Create(scope,
edit)` and scheduled with `ISimulation.ScheduleCommand`, as the drawing tools do.

| Method | Game call | Notes |
|---|---|---|
| `add_roads(segments, sequential)` | `TileEditor.AddRoad(tilemap, from, direction)` | A segment is `(from tile, direction)`. Batch mode validates every segment against the current map, then schedules them all. `sequential=True` applies one, waits until it shows up, then validates the next (needed for bridges and tunnels); after a failure the rest are `EDIT_SKIPPED` |
| `clear_tiles(tiles, sequential)` | `TileEditor.ClearTile` | Clearing a chain must go loose end first, so it's sequential |
| `add_junction_upgrade(kind, p)` | `AddRoundabout` / `AddTrafficLight` | Returns the result code |
| `choose_upgrade(i)` | `GameUpgradeScreen.OnUpgradeSelect(i)` | Falls back to scheduling a `SelectUpgradeCommand` when no screen shows |
| `set_game_speed(x)` / `game_speed()` | `MotorwaysGame._debugTimescale` | See below |
| `start_game(map)` | `GameStarter.StartFromMapDefinition` + `Start` | Only from the main menu; waits for it to settle |
| `map_names()` | `MapLibrary._maps[*].mapName` | |

Result codes are in `EDIT_RESULT_NAMES`. The ones that matter most: 0 Success, 13 EditAlreadyExists,
14 CannotCreateBridge, 15 CannotCreateTunnel, 5 NotEnoughUpgrades, 6 NotEnoughConcrete. The bot
adds -1 (bridge error) and -2 (skipped).

### Game speed

Unity's `Time.timeScale` setter is stripped from this build, and `TimeScale.FromScale` always
returns `Single`, so `Game.SetTimeScale` can't produce arbitrary speeds. The developer multiplier
`MotorwaysGame._debugTimescale` scales the simulation exactly (4 measured as 4.0×).
`set_game_speed(x)` sets it to `x / current TimeScale`. `game_speed()` returns
`TimeScale × multiplier`.
