# Operations

## Running

```bash
cd bot
./launch.sh                                   # (re)launch the game with the bridge
python3 bot.py --maps                         # list map names
python3 bot.py --start Tokyo                  # start Tokyo from the main menu and play it
python3 bot.py --start Tokyo --speed 4        # the same at 4x, for test runs
python3 bot.py --keep-going                   # stay attached and play every game you start
```

- `launch.sh` quits a running copy of the game first.
- `--start` only works from the main menu. If a game is already in progress, the bot plays that one.
- The bot stops at game over (unless `--keep-going`) and restores normal speed on exit.
- Only one controller can be connected at a time: while the bot holds the connection, a debug
  script can't connect. Stop the bot first.

## Tests

```bash
cd bot
python3 -m unittest discover -s tests
```

`tests/test_planner.py` builds small boards with a `Map` helper:

- `destination(tiles, park_tiles, entrance, dir, group)` adds a destination with its car park;
- `road(*tiles)` chains tiles together;
- `link(a, b)` joins two neighbours;
- `house(pos, driveway, group)` adds a house.

The tests cover:

- turning houses, shortcuts, loops and dead ends;
- route loads and junction picking;
- colour preference, avoiding busy trunks and the two-colour cap;
- bypasses and bridge stock;
- drawing only the missing segments over an existing bridge;
- roundabouts (`+` and 8-arm), roundabout exits;
- feeders and separation cuts.

When adding a test for a fix, check that it **fails without the fix**: monkeypatch the new function
away and run the single test.

## Comparing versions

Layouts are random, so one game says little; scores on Tokyo at 4x ranged from about 340 to 870.
Compare by alternating versions over several games. `bot.baseline/` (ignored by git) holds the
original bot with `--speed` added. A run like

```bash
for n in 1 2 3; do ./launch.sh; sleep 6; python3 bot.py --start Tokyo --speed 4 > run-$n.log 2>&1; done
```

takes about 5 minutes per game. `run-*.log` files are ignored by git.

To spot loops in a log:

```bash
grep "reclaimed\|removed" run-1.log | sed 's/^[0-9:]* //' | sort | uniq -c | sort -rn | head
grep "^\S* destination" run-1.log | sed -E 's/^[0-9:]+ //; s/: .*//' | sort | uniq -c | sort -rn | head
```

Anything repeated more than a few times is a build-and-remove loop.

## Tuning

| Where | Constant | Effect of raising it |
|---|---|---|
| `planner.py` | `COST_NEW_TILE` | saves road tiles at the cost of longer drives |
| | `TURN_COST[2]` | straighter roads |
| | `COST_COLOUR_CAP` | stricter two-colour limit |
| | `COST_SHARED*` | colours kept apart more eagerly |
| | `LOAD_FREE` / `COST_LOAD` | busy trunks avoided sooner or harder |
| | `MAX_SEPARATION_DETOUR` | longer detours accepted to separate colours |
| | `MIN_BYPASS_SAVING` | fewer, more valuable bypasses |
| `bot.py` | `PRESSURE_HIGH` | destinations count as pressured later |
| | `ROUNDABOUT_MIN_LOAD`, `TRAFFIC_LIGHT_MIN_LOAD` | fewer junction upgrades |
| | `UPGRADE_VALUE` | changes upgrade picks |
| | `OPTIMISE_EVERY` | slower reshaping of built roads |

## Known limitations

- Motorways are never built.
- Traffic is predicted from shortest routes; actual lane congestion isn't read.
- Feeders need idle cars elsewhere. When every car of a colour is busy, only more houses help.
- Roundabouts on X and T junctions weren't tested live; the model assumes the same diamond ring.
- `--start` needs the main menu; injection needs the App Store build without hardened runtime.

## Debug tools

- `tools/board.py` prints the running game as an ASCII map with each destination's pins and supply.
- `tools/decl.py <Class>` prints a class from the Cpp2IL output.
- Live probes: connect with `Bridge()` and `Session(b).attach()` while the bot isn't running.
  `notes/roundabouts.md` shows examples: reading tile fields, giving stock with `SetValue`, and
  validating `AddRoad` without scheduling it.
