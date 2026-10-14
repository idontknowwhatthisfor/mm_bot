# Mini Motorways bot

Plays Mini Motorways (Mac App Store / Apple Arcade build) without screen reading or mouse
input. A small library injected into the game exposes the IL2CPP runtime. The bot reads the
city straight from the game's models and builds roads by calling the same code the game runs
when you draw them.

## How it works

```
bot.py ──► planner.py      which houses reach their colour, cheapest roads for the rest
   │
   └─────► game.py         reads the board each tick, builds roads, picks upgrades
              │
              └► mmbridge.py ── TCP 127.0.0.1:47811 ──► libmmbridge.dylib (inside the game)
```

- **`bridge/mmbridge.m`** is loaded into the game through `DYLD_INSERT_LIBRARIES`. The game is
  sandboxed and may only make outgoing connections, so the bridge connects out to the bot and
  requires the per-launch token from `launch.sh`. Every request runs on Unity's main thread
  between frames. The bridge is generic IL2CPP reflection (field paths, method calls,
  collection iteration) with nothing Mini Motorways-specific in it.
- **Reading state:** houses, destinations, car parks and roads come from
  `TilemapModel._tiles`. Terrain comes from `CityDefinition.TileIsDriveable` and related
  methods, the city limits from `City.IsTileInPlayableArea`, and the inventory from
  `UpgradeDatabaseModel`.
- **Building roads:** each segment is validated with `TileEditor.AddRoad`, and the resulting
  edit is scheduled on the simulation as an `EditTileCommand`. This is the same path
  `DrawRoadAction` takes, so the game enforces its own rules (road tiles, bridges, blocked
  tiles).
- **Weekly upgrades:** the bot calls the upgrade screen's own `OnUpgradeSelect` handler.
- **Turning houses:** drawing out of a house toward any of its 8 neighbours turns its
  driveway, just as dragging a house does in the game: `TileEditor.AddRoad` on the house tile
  returns an `AlignDrivewayEdit`. Each unconnected house is planned from all 8 sides, and the
  plan starts by turning the house if another side gives a shorter road.
- **Strategy:**
  - Destinations whose car park has no houses of their colour on its network are fed first,
    since they're what ends a game.
  - Every house that can't reach its colour gets the connection with the fewest new road
    tiles, to that colour's network or to a car park entrance. Driveway tiles are free, so
    turning a house to face an existing road costs nothing.
  - The search is Dijkstra over (tile, heading). Roads go in 8 directions, and a diagonal costs
    one tile like a straight step. Bends are capped at 90°, and two diagonals never cross at
    the same corner.
  - New tiles dominate the cost. Turns, reused road and trip length only break ties between
    equally short roads.
  - Bridges and tunnels are straight crossings, drawn one tile at a time because each piece
    must exist before the next validates.
  - Any move the game refuses is avoided for a minute.
  - Entering water takes a bridge from stock unless the first water tile already has road.
    Only segments the map doesn't have yet are sent, because the game refuses an existing
    bridge segment that is sent again (`CannotCreateBridge`).
  - **Traffic:** the game routes every car along the shortest free-flow road and never around a
    jam. Each road tile's load is the number of houses whose route uses it. Road carrying more
    than 6 houses costs extra to drive along or join, and so does turning a road into a new
    junction. Houses of a colour whose destination is close to overcrowding prefer to join that
    destination's network.
  - **Junction upgrades:** the busiest junctions get a roundabout (8+ houses), then a traffic
    light (5+).
  - **Bypasses:** a destination whose cars are slow to arrive gets a new road from its car park
    to a far part of its network that houses actually use.
  - Each day, and at game over, the log shows each active destination's pins (with and without
    a car on the way), arrivals and overcrowd timer.
- **Upkeep:**
  - Bridges or tunnels left with a missing piece get completed.
  - Dead-end roads, such as the stub left after a house turns, are cleared to recover their
    tiles.
- **Shortening built roads**, once nothing is waiting to be connected:
  - **Shortcuts:** a house whose private road (the stretch only it uses, up to the first
    junction) is longer than a fresh connection would be gets reconnected, usually just by
    turning it to face a nearby road. The old road is then cleared.
  - **Redundant tiles:** loops and doubled connections, such as two roads into the same car
    park, lose the tiles nobody needs.
  - **Limits:** neither may cut off a house or destination, or make any house's trip to any
    destination of its colour more than one or two tiles longer.
  - **Order:** clears go one tile at a time from the loose end, since each is validated
    against the map as it is.

## Usage

Needs Python 3.10+ (standard library only) and the Xcode command line tools to build the bridge.

```bash
cd bot
./launch.sh          # builds the bridge and (re)launches the game with it
```

Then either start a map yourself and run:

```bash
python3 bot.py
```

or let the bot start one from the main menu:

```bash
python3 bot.py --start Tokyo
```

`--maps` lists map names. `--keep-going` keeps the bot attached after a game ends, so it plays
the next game you start.

`--speed N` runs the game N times faster, for test runs:

```bash
python3 bot.py --start Tokyo --speed 4
```

The bot first times the game clock for 5 seconds at normal speed, then sets
`MotorwaysGame._debugTimescale` (the multiplier the game's developer tools use) so that, times
the game's own play/fast-forward speed, the game runs at N×. It checks the speed every tick and
sets it again if the game resets it, and it puts the game back to normal speed when it exits.
The bot also looks at the game N times as often, so it gets the same number of looks per game
second. If the game won't take the speed, the bot logs "can't change the game speed" and plays
on at normal speed.

Unity's `Time.timeScale` can't be used because its setter is stripped from this build.
`Game.SetTimeScale` with `TimeScale.FromScale` doesn't work either: `FromScale` returns
`Single` for any value.

The "game speed: N.Nx normal" line the bot logs overstates the speed, often about 2×. It
compares against the clock rate it measured in the first seconds of a game, when the game runs
slow. To see the real speed, compare the time between the daily status lines with a
normal-speed run, where a game day takes about 20 seconds.

`launch.sh` quits a running copy of the game before relaunching it. Opening the game normally,
from Launchpad or the Dock, starts it without the bridge.

## Leaderboards

When it attaches to a game, the bot replaces the game's Game Center leaderboard backend with
the game's own `NullLeaderboardBackend`, so bot-played scores are never submitted. This lasts
until the injected game process exits; a normal launch is unaffected. Achievements and map
unlocks still go to your account.

## Limitations

- Motorways are never placed, so the bot values them at 0 when picking upgrades. Congestion is
  handled only through route-load costs, roundabouts, traffic lights and bypasses, so busy
  late-game cities still jam and overflow eventually.
- Houses that are already connected are never turned, even if another side would now be
  shorter.
- `--start` only works from the main menu.
- Injection depends on the App Store build being signed without hardened runtime or library
  validation. A future game update that adds either will stop `DYLD_INSERT_LIBRARIES` from
  working.

## Tests

```bash
python3 -m unittest discover -s tests
```

These are planner tests on small hand-built boards: turning a house onto an adjacent road,
shortcutting a looping private road, dropping a doubled car park connection while keeping a
loop that's the short way to a second destination, and reclaiming dead ends. They also cover
route loads and junction picking, avoiding busy trunks, preferring a pressured destination's
network, bypasses, needing a bridge to enter water, and drawing only the missing segments over
an existing bridge.

## Tools

- `tools/board.py` prints the running game as an ASCII map, with each destination's pins and
  supply. It's useful for seeing why a game was lost.
- `tools/decl.py <Class>` prints a class from the Cpp2IL output in `../decompiled` as bare
  declarations, useful for finding new fields and methods to read.
