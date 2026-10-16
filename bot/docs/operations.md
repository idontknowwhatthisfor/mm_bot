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
- **Stopping a bot started in the background from a script:** use `kill -TERM`. A process
  backgrounded from a non-interactive shell starts with SIGINT ignored, so `kill -INT` (Ctrl-C)
  does nothing to it. After SIGTERM the game stays at the test speed.
- `--no-jam-cost` leaves measured jams out of the route costs. Cars are still read and logged.

## Tests

```bash
cd bot
python3 -m unittest discover -s tests
```

`tests/test_traffic.py` uses fake cars (`Car`, `Traffic`). It covers:

- **Queues:** what counts as stopped in a queue, and that one red light isn't a jam while cars
  stuck for 30 s are.
- **Paused reads:** they change nothing.
- **Jam cost:** it steers a new house off a jammed trunk, and the switch turns it off.
- **Route check:** matches, gaps in the game's path, mismatches, and the drive home.
- **Bot schedule:** the bot reads cars once a game second (with a `FakeSession` that also returns
  car reads).
- **A real game:** `tests/data/snapshot-tokyo-week9-drive-through.json` comes from a Tokyo game
  where colour 4's cars drive through a shared car park with two entrances. With drive-through
  modelled, cars 115 and 125 match their routes (and ≥95% of all cars do); modelled as a dead end,
  they don't.

`tests/test_planner.py` builds small boards with a `Map` helper:

- `destination(tiles, park_tiles, entrance, dir, group)` adds a destination with its car park;
- `road(*tiles)` chains tiles together;
- `link(a, b)` joins two neighbours;
- `house(pos, driveway, group)` adds a house.

The tests cover:

- turning houses, shortcuts, loops and dead ends;
- route loads and junction picking;
- colour preference, avoiding busy trunks and the two-colour cap;
- bypasses, bridge stock, and bridge and tunnel stock counted separately;
- the bridge reserve:
  - one bridge below the reserve of two beats 30 concrete;
  - a bypass only gets bridges beyond the reserve;
- reclaiming:
  - a stranded destination gets the bridge nobody needs taken down;
  - the only bridge a house uses stays;
  - nothing is taken down while a bridge is on its way back;
- one crossing per tick, drawing only the missing segments over an existing bridge;
- roundabouts (`+` and 8-arm), roundabout exits, no junction upgrade on ring tiles;
- feeders, separation cuts, and a refused cut not holding up loop clearing;
- shortcuts keeping off busy trunks;
- colour sharing on the drive to every destination, driving over the house's own network to avoid
  a shared road, a separate road plus cut taking a colour off a shared road, joining a house to a
  driveway tile that already has road, and backing off a plan with nothing left to draw.

When adding a test for a fix, check that it **fails without the fix**: monkeypatch the new function
away and run the single test.

## Comparing versions

Layouts are random, so one game says little; scores on Tokyo at 4x ranged from about 340 to 870.
Compare by alternating versions over several games. A run like

```bash
mkdir -p logs
for n in 1 2 3; do ./launch.sh; sleep 6; python3 bot.py --start Tokyo --speed 4 > logs/run-$n.log 2>&1; done
```

takes about 5 minutes per game. `./run-tests.sh [map] [games] [speed] [bot options…]` does the
same (default 3 games of Tokyo at 4x). Anything after the speed goes to `bot.py`, for example
`./run-tests.sh Tokyo 6 4 --no-jam-cost`. It writes `logs/test-<time>-<n>.log` and prints each
game's score with counts of separate roads, cuts, stuck plans, slow optimise passes and refusals.
Then it runs `tools/summarize.py` over the logs.

`tools/summarize.py` reports, for each game:

- score and week;
- the first destination to overcrowd, and the colours overcrowded at the end;
- "could not connect" (no road found), short of concrete, and stuck plans ("nothing left to draw");
- the route-match share;
- the peak queue, days with jammed tiles, and car read times.

`--group NAME LOGS…` compares settings side by side. Logs of earlier runs are kept in `logs/`.

`tools/routecheck.py logs/snapshot-*.json` replays the route check on every snapshot that has cars
in it. `--park-factor 1 2 3 --` compares `PARK_DRIVE_FACTOR` values, and `--no-drive-through` gives
the old model.

### Jam cost on vs off (Tokyo, 6 + 6 games at 4×, 2026-10-15)

Logs: `logs/test-20261015-2315-*.log` (on) and `logs/test-20261015-2353-*.log` (off); summary in
`logs/run-jam-on.log` and `logs/run-jam-off.log`.

| | Jam cost on | Jam cost off |
|---|---|---|
| Scores | 341, 1336, 651, 727, 335, 616 | 431, 1036, 362, 1161, 542, 1184 |
| Mean / median score | 668 / 634 | 786 / 789 |
| Mean final week | 8.0 | 8.3 |
| "Could not connect" lines | 52 | 100 |
| Short of concrete | 61 | 389 |
| Stuck plans ("nothing left to draw") | 0 | 0 |
| Mean peak of cars stopped in queues | 16.3 | 6.3 |
| Route checks matching the planner | 91.6% of 12,681 | 93.8% of 11,936 |

**How the games were lost:**

- **Four games (two from each group) ended in weeks 6–7 on colour 5, with no queues at all.** Its
  houses at the map's east edge were cut off from their destination by Tokyo's rivers. Three had
  no bridge left (`no road found`, idle cars, pins with no car sent). In one (on, game 1) the only
  route was 37–53 tiles long, and no car arrived in time.
- **The other eight ended in weeks 8–11** with several destinations overcrowded at once.
- **Jams formed late.** Jammed tiles appeared only in the last 1–8 game days of the games that had
  any; six of the twelve never had one. So the jam cost had little time to act.

**Verdict:** the spread within each group is larger than the gap between them. These 12 games
don't show the jam cost helping, and don't show it hurting either. Telling them apart would take
many more games.

### Bridge reserve and reclaiming (Tokyo, 8 games at 4×, 2026-10-16)

The run used the reserve of two and reclaiming, with the jam cost on. Logs:
`logs/test-20261016-0800-*.log`, summary in `logs/run-bridge-fix.log`.

| | Before (the 12 games above) | After (8 games) |
|---|---|---|
| Lost with a destination still without any road | 3 | 0 |
| Lost before week 8 | 4 | 0 |
| Score mean / median / min | 727 / 634 / 335 | 814 / 754 / 565 |
| Mean final week | 8.2 | 8.8 |
| Bridges picked per game | 1–3 | 3–5 |
| "Short of concrete" lines per game | 37.5 | 45 |

Two games took a bridge back. In one, the destination's new crossing was built 3 s (real) later.
In the other, its 31-segment road came up one concrete tile short for a few ticks, and the
destination was fed by the next game day.

All 8 games ended in weeks 8–11 with several destinations overcrowded at once.

The bot's concrete estimate sometimes comes in under what the game charges. A plan short by one
tile is then redrawn every tick until it backs off, and the logs show this with and without bridges
involved.

The bot saves the map to `logs/snapshot-<start>-week<W>-day<D>-<event>.json` the first day each
destination overcrowds and at game over. `game.load_snapshot(json.load(f))` gives back the
`(state, terrain)`, so the planner can be rerun on exactly that situation without the game.
Snapshots also hold the latest car read: `game.load_traffic(data)`.

To spot loops in a log:

```bash
grep "reclaimed\|removed" logs/run-1.log | sed 's/^[0-9:]* //' | sort | uniq -c | sort -rn | head
grep "^\S* destination" logs/run-1.log | sed -E 's/^[0-9:]+ //; s/: .*//' | sort | uniq -c | sort -rn | head
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
| | `MAX_DETOUR` | shortcuts and loop clearing may lengthen trips (0: never) |
| | `MAX_SEPARATION_ROAD` | longer separate roads built to take a colour off a shared road |
| | `COUNT_EVERY_DESTINATION` | `False` checks colour sharing on the drive to the nearest destination only |
| | `MIN_BYPASS_SAVING` | fewer, more valuable bypasses |
| | `COST_JAM` / `JAM_FREE` | measured queues avoided harder / only longer queues count |
| | `PARK_DRIVE_FACTOR` | cars predicted to drive through two-entrance car parks less often |
| `bot.py` | `CROSSING_RESERVE` | more bridges and tunnels kept in stock (upgrade picks, optimisations) |
| `traffic.py` | `JAM_WINDOW` | jam levels react more slowly (longer memory) |
| | `JAM_LEVEL` | fewer tiles logged as jammed (the planner's cost uses the level itself) |
| `bot.py` | `PRESSURE_HIGH` | destinations count as pressured later |
| | `ROUNDABOUT_MIN_LOAD`, `TRAFFIC_LIGHT_MIN_LOAD` | fewer junction upgrades |
| | `UPGRADE_VALUE` | changes upgrade picks |
| | `OPTIMISE_EVERY` | slower reshaping of built roads |

## Known limitations

- Motorways are never built.
- Car routes are predicted from shortest routes by tile length. The game routes by lane travel
  time (`PathfindingCost`), so routes over half-speed lanes can differ, and a car keeps the route it
  set out with after the roads change. Measured jams feed the route costs of new roads only; the
  predicted loads still come from the predicted routes.
- Feeders need idle cars elsewhere. When every car of a colour is busy, only more houses help.
- Roundabouts on X and T junctions weren't tested live; the model assumes the same diamond ring.
- `--start` needs the main menu; injection needs the App Store build without hardened runtime.

## Debug tools

- `tools/board.py` prints the running game as an ASCII map with each destination's pins and supply.
- `tools/cars.py [--samples N] [--dump FILE]` reads every car in one request and prints a table:
  house, colour, state, tile, speed, obstacle, gap and path length. It then prints speed and queue
  statistics and the read time.
- `tools/decl.py <Class>` prints a class from the Cpp2IL output.
- Live probes: connect with `Bridge()` and `Session(b).attach()` while the bot isn't running.
  `notes/roundabouts.md` shows examples: reading tile fields, giving stock with `SetValue`, and
  validating `AddRoad` without scheduling it.
