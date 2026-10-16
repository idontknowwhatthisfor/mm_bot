# Bot code documentation

A detailed guide to how the Mini Motorways bot works, for anyone reading or changing the code.
The top-level [`../README.md`](../README.md) covers what the bot does and how to run it. These pages
cover how the code does it.

| Page | What it covers |
|---|---|
| [architecture.md](architecture.md) | The pieces, how a tick flows through them, and where each decision is made |
| [bridge.md](bridge.md) | The injected library, the wire protocol, path syntax and the Python client |
| [game-model.md](game-model.md) | `game.py`: coordinates, the `State` snapshot, how it's read, and every action on the game |
| [planner.md](planner.md) | `planner.py`: the cost model, the path search, every kind of plan, and the cleanups |
| [bot-loop.md](bot-loop.md) | `bot.py`: the main loop, the build order, loop guards, upgrades and the log |
| [operations.md](operations.md) | Running, testing, reading logs, tuning constants, and known limitations |
| [../notes/roundabouts.md](../notes/roundabouts.md) | How the game builds roundabouts, worked out from the game code and live tests |

## Source files

| File | Lines | Role |
|---|---|---|
| `bot.py` | ~620 | Strategy and main loop (`Bot`), command line |
| `planner.py` | ~1210 | Pure functions over a `State`: networks, the path search, plans, cleanups, traffic |
| `traffic.py` | ~240 | Pure functions over the cars: jam levels (`JamTracker`) and the route check |
| `game.py` | ~750 | `Session`: reads the game into a `State` (and the cars into a `Traffic`) and performs actions through the bridge |
| `mmbridge.py` | ~190 | TCP client for the injected bridge (`Bridge`) |
| `bridge/mmbridge.m` | ~920 | The library injected into the game: generic IL2CPP reflection over JSON |
| `launch.sh` | ~30 | Builds the bridge and launches the game with it injected |
| `tests/test_planner.py` | ~730 | Planner tests on small hand-built boards, plus a few `Bot` tests with a fake session |
| `tests/test_traffic.py` | ~200 | Jam and route-check tests on fake cars, and on a snapshot from a real game (`tests/data/`) |
| `tools/board.py`, `tools/decl.py` | | Debug map printer; Cpp2IL declaration viewer |
| `tools/cars.py` | | Live probe: every car in one request, as a table |
| `tools/routecheck.py` | | Replays the route check on saved snapshots |
| `tools/summarize.py` | | Sums up game logs: score, how a game was lost, connection trouble, routes and queues |

`planner.py` never talks to the game. Everything in it is a function of a `State` (plus terrain),
which is why it can be tested without the game running.
