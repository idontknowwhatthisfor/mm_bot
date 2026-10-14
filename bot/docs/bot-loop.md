# The bot loop (`bot.py`)

## `Bot.run`

```
loop every tick:
    state = session.read()            # None while no game is up
    new game?  -> on_new_game         # reset memory, value upgrades for the map
    ended?     -> log score, report, stop (or wait for the next game with --keep-going)
    report(state)                     # once per game day
    keep_speed(state)                 # --speed only
    pending upgrade?  -> pick_upgrade
    else if not paused and the simulation has stepped past the last edit -> build(state)
```

Building only after the clock has passed `last_build_time` keeps the bot from planning against a
map whose last edits haven't been applied yet.

## `build`: one kind of change per tick

1. **`repair_crossings`**: complete bridges and tunnels missing a segment.
2. **`reclaim_dead_ends`**: clear roads that lead nowhere.
3. **`place_junction_upgrades`**: from `route_colours` loads, put a roundabout on the busiest
   junction carrying at least `ROUNDABOUT_MIN_LOAD` (8) houses, else a traffic light at 5+. If the
   game refuses outright (codes in `PLACEMENT_BROKEN`), that upgrade is valued at 0 for the rest of
   the game.
4. **Connections**, planned together on one `Board` so they share tiles:
   - unfed destinations first, busiest first (`plan_destination`);
   - then unconnected houses: colours with a pressured destination first, then houses with cars
     waiting (`plan_house`). Pressured networks are preferred.
   - `take()` accepts a plan only within the remaining concrete and while its key isn't backed off.
   - Plain roads go out in one batch. At most one plan that crosses water or a mountain is drawn per
     tick, segment by segment.
5. **`optimise`** when nothing needed connecting (at most once per `OPTIMISE_EVERY` = 5 sim s), doing
   the first that applies:
   1. `feed_short_destinations`: a pressured destination with pins that have no car sent, and no
      idle car of its colour on its network, gets `plan_feeder` from the nearest house of its colour
      with idle cars on another network (up to `FEEDER_CANDIDATES` = 4 tried).
   2. Bypass (`plan_bypass`) for a pressured destination with at least `BYPASS_MIN_EN_ROUTE` (3) cars
      on the way.
   3. Shortcut (`plan_shortcut`) for the houses with the longest private roads (up to 12).
   4. `separation_cut`: remove the link that puts a third colour on a road.
   5. `redundant_tiles`: clear loop tiles nobody needs.

`pressured(state)`: active destinations with `pressure ≥ PRESSURE_HIGH` (0.5), most pressed first.

## Drawing and recording

- **`draw(state, segments, sequential)`** sends only segments the map doesn't already have. The
  rest come back as `EDIT_ALREADY_EXISTS` without asking. This matters on bridges: re-sending an
  existing deck segment is refused with `CannotCreateBridge`.
- **`record(state, plan, codes)`** logs the outcome:
  - Refused moves (except codes in `NOT_ABOUT_THE_MOVE`, such as out of stock) go into
    `self.refused` for `REMEMBER_REFUSED_FOR` (60 sim s), so the search avoids them.
  - `GIVE_UP_AFTER` (5) failures back a plan's key off for `GIVE_UP_FOR` (20 sim s).
  - Successful segments are remembered in `self.built` for the dead-end guard.
- `describe_refusal` appends where the first refused segment was, what terrain it sat on, and the
  bridge and tunnel stock.

## Loop guards

Each of these exists because the bot once built something and undid it over and over:

| Guard | Trigger | Effect |
|---|---|---|
| Built then cleared | a dead-end reclaim removes a tile drawn less than `BUILT_THEN_CLEARED` (10 s) ago | that road's segments are refused for 60 s, so the next plan routes elsewhere |
| Shortcut blame | a house becomes unconnected less than `SHORTCUT_TRUST` (20 s) after its shortcut | no more shortcuts for that house this game |
| Back-off keys | repeated failures for a plan, bypass, feeder, junction or clear | `skip_until[key]` for `GIVE_UP_FOR` |
| Roundabout model | see `join_roundabouts` | the ring and centre read as one junction, so their roads aren't mistaken for dead ends |
| Separation pending | a `separation_cut` exists | `redundant_tiles` returns nothing, so the colour's own road isn't removed first |

## Upgrades

`pick_upgrade` values each offered package as `UPGRADE_VALUE[type] × amount + extra concrete`, in
road tiles, and takes the best. Bridges and tunnels are raised to `WATER_CROSSING_VALUE` (8) on maps
with water or mountains. Motorways are worth 0, since the bot never builds them.

It picks through the upgrade screen's own handler, waits until the pending count drops, and retries
if the screen ignored the click. After 15 s without a visible screen it schedules the selection
command directly.

## Speed (`--speed N`)

`keep_speed` times the game clock for 5 s at normal speed, then sets the multiplier. It checks every
tick (the game can reset its speed after pauses and upgrade screens) and logs
`game speed: N.Nx normal`. That number reads high, because the first seconds of a game run slow;
the gap between day lines is the reliable measure (about 20 s per day at 1×). On exit, `main()`
restores normal speed.

## Log format

```
HH:MM:SS week W day D: score S, a/b houses connected, c/d destinations fed, R road tiles left[, OVERCROWDED: …]
HH:MM:SS   colour G at (x, y): P pins (U no car, E car on the way) of L, A arrivals yesterday, timer T/M; colour has I/C cars idle
HH:MM:SS house (x, y) (colour G): road of N tiles (K new)[, 1 crossing(s)][, turned the house]
HH:MM:SS destination (x, y) (colour G): K/N segments refused (CannotCreateBridge, Skipped); first at … bridges B, tunnels T
HH:MM:SS roundabout at (x, y) (H houses use it)
HH:MM:SS cut 2 road tile(s) at … so no road carries more than two colours
HH:MM:SS destination (x, y) is short of cars (U pins with none sent); feeding it from house (x, y)
HH:MM:SS road just drawn at (x, y) dead-ended; avoiding it for now
HH:MM:SS week W upgrade: bridge x1 +20 roads | concrete x30 -> concrete
HH:MM:SS game over: score S in week W
```

How to read a loss: in the last destination report, look at the destination with the full timer.

- **Many "no car" pins and 0 idle cars**: the colour is short of cars. It needs more houses
  connected.
- **Most pins have a car on the way**: there are enough cars, but the route is slow or jammed.
- **Idle cars but "no car" pins**: the destination is unreachable from those houses, which points to
  a connectivity bug.
