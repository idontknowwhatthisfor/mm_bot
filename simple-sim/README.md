# mmsim: a simple Mini Motorways simulator for bots

A small, self-contained simulator of Mini Motorways for writing and testing bots, in pure Python
with no dependencies. It keeps the main features of the game:

- coloured houses and destinations with car parks;
- roads in 8 directions, paid for in road tiles;
- rivers that need bridges, and mountains that need tunnels;
- cars that are dispatched to pins, drive the shortest road, queue in traffic and park;
- the overcrowd timer that ends the game;
- weekly upgrade choices, roundabouts and traffic lights;
- city limits that grow every week.

```bash
cd simple-sim
python3 -m mmsim.run examples/greedy_bot.py --games 5     # play 5 seeded games
python3 -m mmsim.view examples/greedy_bot.py              # watch one in a window
python3 -m unittest discover -s tests                     # run the tests
```

**Start with [GUIDE.md](GUIDE.md).** It explains the rules and the API, and builds a bot from an
empty file step by step.

## Layout

| Path | What it is |
|---|---|
| `mmsim/game.py` | `Game`: what a bot sees and does (views, actions, helpers) |
| `mmsim/world.py` | `World`: the rules and the simulation |
| `mmsim/core.py` | positions, directions and enums |
| `mmsim/run.py` | plays a bot file over seeded games |
| `mmsim/view.py` | a tkinter window to watch a bot play |
| `examples/do_nothing.py` | the baseline: picks upgrades, builds nothing |
| `examples/greedy_bot.py` | the bot built in the guide |
| `tests/test_world.py` | rules, the `Game` interface, and the example bots |

A bot is a file with a `Bot` class whose `step(self, game)` is called every 0.4 game seconds.
Every seed is the same game every time, so two bots can be compared seed by seed.

This folder is a standalone copy of the simulator from `../bot/sim/`, with a friendlier interface
for bots. It doesn't need anything else in the repository.
