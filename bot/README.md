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
- **Strategy:** every house whose network doesn't reach an active destination of its colour
  gets the cheapest connection to that colour's network or to a car park entrance. The search
  is Dijkstra over the grid; existing roads are cheap to reuse, and straight bridge or tunnel
  crossings are used when available. Houses with waiting cars go first.

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

`launch.sh` quits a running copy of the game before relaunching it. Opening the game normally,
from Launchpad or the Dock, starts it without the bridge.

## Leaderboards

When it attaches to a game, the bot replaces the game's Game Center leaderboard backend with
the game's own `NullLeaderboardBackend`, so bot-played scores are never submitted. This lasts
until the injected game process exits; a normal launch is unaffected. Achievements and map
unlocks still go to your account.

## Limitations

- Only plain roads, plus bridges and tunnels as straight crossings. Roundabouts, traffic
  lights and motorways are picked as upgrades but never placed, and the bot doesn't reroute
  congested roads, so busy late-game cities eventually jam and overflow.
- `--start` only works from the main menu.
- Injection depends on the App Store build being signed without hardened runtime or library
  validation. A future game update that adds either will stop `DYLD_INSERT_LIBRARIES` from
  working.

## Tools

- `tools/board.py` prints the running game as an ASCII map, with each destination's pins and
  supply. It's useful for seeing why a game was lost.
- `tools/decl.py <Class>` prints a class from the Cpp2IL output in `../decompiled` as bare
  declarations, useful for finding new fields and methods to read.
