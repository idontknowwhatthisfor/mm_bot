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
at the other** (`State.joined(p, d)`). A house or car park has a stub pointing at its access tile,
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
| `pending_choices` | `pendingUpgradeChoices._size` (an upgrade screen is waiting) |
| `week, day, hour, time, expansion_time, paused` | `ClockModel` |
| `score`, `ended` | `ScoreModel.Score`, `Game.HasGameEnded` |
| `playable` | `City.IsTileInPlayableArea`, re-evaluated only when the limits move |

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
