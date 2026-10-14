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
| `bot.py` | ~570 | Strategy and main loop (`Bot`), command line |
| `planner.py` | ~1000 | Pure functions over a `State`: networks, the path search, plans, cleanups, traffic |
| `game.py` | ~590 | `Session`: reads the game into a `State` and performs actions through the bridge |
| `mmbridge.py` | ~190 | TCP client for the injected bridge (`Bridge`) |
| `bridge/mmbridge.m` | ~920 | The library injected into the game: generic IL2CPP reflection over JSON |
| `launch.sh` | ~30 | Builds the bridge and launches the game with it injected |
| `tests/test_planner.py` | ~430 | Planner tests on small hand-built boards |
| `tools/board.py`, `tools/decl.py` | | Debug map printer; Cpp2IL declaration viewer |

`planner.py` never talks to the game. Everything in it is a function of a `State` (plus terrain),
which is why it can be tested without the game running.
