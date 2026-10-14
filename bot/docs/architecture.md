# Architecture

## The pieces

```
 ┌───────────── Mini Motorways (sandboxed app) ─────────────┐
 │  IL2CPP game code: TilemapModel, TileEditor, ISimulation…   │
 │        ▲  reflection on the Unity main thread              │
 │  libmmbridge.dylib (bridge/mmbridge.m)                     │
 └────────┬───────────────────────────────────────────────────┘
          │ TCP 127.0.0.1:47811, one JSON object per line
          │ (the game connects out: the sandbox allows outgoing connections only)
 ┌────────▼─────────── Python ────────────────────────────────┐
 │  mmbridge.Bridge   requests, batches, handles               │
 │  game.Session      State snapshots + actions                │
 │  planner.*         networks, search, plans, cleanups        │
 │  bot.Bot           strategy loop, upgrades, logging         │
 └─────────────────────────────────────────────────────────────┘
```

- **`launch.sh`** compiles the bridge if `mmbridge.m` changed, copies it to
  `~/Library/Application Support/mmbot/`, writes a fresh random token to `bot/.mmbridge_token`, and
  starts the game binary with `DYLD_INSERT_LIBRARIES`, `MMBRIDGE_TOKEN` and `MMBRIDGE_PORT` set.
- **The bridge** knows nothing about Mini Motorways. It resolves classes, fields, properties and
  methods by name through the IL2CPP API and runs every request on Unity's main thread.
- **`Bridge`** (Python) listens on the port, accepts the game's connection, sends the token, then
  exchanges JSON requests and replies.
- **`Session`** knows the game's model types. It turns a few batched bridge reads into a `State`,
  and turns actions (draw road, clear tile, place roundabout, pick upgrade, set speed) into the
  game's own editor calls and simulation commands.
- **`planner`** decides *where* roads go. It's pure Python over a `State`.
- **`Bot`** decides *what* to do each tick, and in what order.

## One tick

`Bot.run` loops every `tick` seconds (0.4 s at normal speed, divided by `--speed`, at least 0.05 s):

1. `Session.read()` returns a fresh `State`: every tile's roads and content, houses, destinations,
   car parks, roundabouts, traffic lights, inventory, clock and score.
2. On a new game it resets per-game memory and values upgrades for the map (water makes bridges
   valuable).
3. Once per game day it logs a status line and a per-destination report.
4. `keep_speed` holds the requested game speed.
5. If an upgrade choice is pending, it picks one. Otherwise, if the simulation has stepped past the
   last edit, it calls `build`.
6. `build` does **at most one kind of change per tick**, because every change alters the map and
   the next plan must see it:
   1. finish half-built bridges and tunnels (`repair_crossings`);
   2. clear dead-end roads (`reclaim_dead_ends`);
   3. place a roundabout or traffic light on the busiest junction (`place_junction_upgrades`);
   4. connect unfed destinations, then unconnected houses (one batch of plans);
   5. if there was nothing to connect: `optimise` (every 5 sim s), which does at most one of: feed a
      destination short of cars, build a bypass, build a shortcut, cut a link that puts a third
      colour on a road, or clear redundant loop tiles.

Edits are validated with the game's `TileEditor` and scheduled as `EditTileCommand`s on the
simulation, the same path the game's own drawing tools use. The game therefore enforces all its
rules (stock, terrain, bridges, junction limits), and the bot reads back each result code.

## Where decisions live

| Decision | Code |
|---|---|
| Which houses and destinations are connected | `planner.Network`, `group_roots`, `is_served`, `unfed_destinations` |
| Where a new road goes | `planner.search` with costs from `Board` |
| Which roads cars actually use | `planner.RoadGraph.shortest`, `route_colours` |
| What to remove | `dead_ends`, `redundant_tiles`, `separation_cut`, `plan_shortcut` |
| Where a roundabout or light goes | `planner.junctions_by_load`, `Bot.place_junction_upgrades` |
| Which upgrade to take | `Bot.pick_upgrade` with `UPGRADE_VALUE` |
| How fast the game runs | `Bot.keep_speed`, `Session.set_game_speed` |
