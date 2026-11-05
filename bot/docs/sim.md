# The simulator (`sim/`)

A small offline version of Mini Motorways for running and testing bots without the game. The bot
plays it unchanged: `SimSession` has the same methods as `game.Session`, and returns the same
`State` and `Traffic`.

```bash
cd bot
python3 -m sim.run                         # 5 games on the "river" map, seeds 1-5
python3 -m sim.run --games 20 --map two-rivers
python3 -m sim.run --seed 7 --verbose      # one game with the bot's full log
python3 -m sim.run --no-jam-cost
```

To watch a game:

```bash
python3 -m sim.view                    # river map, seed 1
python3 -m sim.view --seed 4 --map two-rivers
```

`sim/view.py` is a tkinter window (standard library):

- **The map:** terrain and city limits, roads, houses and destinations in their colours, and each
  destination's pin count, with its overcrowd timer as a red ring.
- **The cars:** moving, with stopped ones outlined in red.
- **A side panel:** clock, score, stock, each destination's pins and timer, and the bot's latest
  log lines.
- **Keys:** space pauses, `s` steps once, `+`/`-` change the speed, and `n` starts the next seed.
- **Without the bot:** `--no-bot` shows the world with nothing built.

A seed always gives the same game for the same bot, so two versions can be compared seed by seed.
A game takes from under a minute to a few minutes; the time goes into the bot's planning.

## What it keeps

| Feature | In the simulator |
|---|---|
| Map | 36×24 grid. `river` has one meandering river, `two-rivers` two, `plain` none, and `mountains` adds a mountain. City limits start at 16×10 and grow 2 tiles per side each week |
| Destinations | a 2×2 building, a 2-tile car park below it, and one entrance (west or east). A new one comes every 0.6–1 week, often a new colour (up to 6) |
| Houses | one tile with a driveway and 2 cars, usually within 6 tiles of a destination of their colour (a quarter anywhere). One every 25 s at first, down to one every 10 s |
| Roads | 8-direction segments, joined only when both sides point at each other. One road tile per new tile; a house's driveway tile is free. Drawing out of a house turns it. Car parks join only through their entrance. Diagonals can't cross at a corner |
| Bridges and tunnels | straight decks; starting one from land takes one from stock. Clearing a deck removes the whole crossing; it's mothballed for 3 s, then back in stock |
| Pins | one per day per destination at first, then more often (about every 5 s by week 8). More than 6 runs the overcrowd timer, and 50 s of it ends the game |
| Cars | idle cars of the colour go to unassigned pins, nearest first, on the shortest road; they park for 1.5 s, score a point, and drive home. Cars re-route when the roads change |
| Traffic | a tile holds 3 cars, a junction 2, a junction with a roundabout or lights 4. A car waits at a full tile (stopped, behind a car or a junction) and squeezes in after 5 s |
| Upgrades | each week the game pauses for a choice of 2 of: concrete ×30, bridge or tunnel +20, roundabout +20, lights ×2 +20, motorway +10 |

## What it leaves out

- Lanes and real car following: tile capacity stands in for them.
- Motorways: the package is offered but can't be built.
- Turns on roundabouts and light cycles: these only add capacity.
- Drive-through car parks, boats, trains, trees, and the game's exact spawn rules.

The numbers are tuned so the current bot's games end around the same week as on Tokyo (weeks 6–11,
mean 8.2 over seeds 1–6). Scores run higher than in the real game, so compare scores within the
simulator, not against real games.

## Files

| File | Role |
|---|---|
| `sim/world.py` | `World`: the rules, `advance(seconds)`, and the edits (`add_road`, `clear_tile`, `add_junction_upgrade`, `choose`) |
| `sim/session.py` | `SimSession`: the `game.Session` interface over a `World` |
| `sim/view.py` | The tkinter viewer |
| `sim/run.py` | Plays `Bot` against seeded games; `play(seed, map)` is usable from tests |
| `tests/test_sim.py` | Road, bridge, car, overcrowding and upgrade rules, determinism, and the bot playing two weeks |

To drive it from your own bot, call `world.advance(seconds)` between looks, and `bot.step(state)`
(or your own logic) on `session.read()`. `Bot.sleep` is a no-op there, and `Bot.snapshot_dir = None`
keeps simulated games out of `logs/`.
