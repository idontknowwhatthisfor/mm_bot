# The bot loop (`bot.py`)

## `Bot.run`

```
loop every tick:
    state = session.read()            # None while no game is up
    new game?  -> on_new_game         # reset memory, value upgrades for the map
    ended?     -> log score, report, stop (or wait for the next game with --keep-going)
    report(state)                     # once per game day (also logs the day's traffic)
    keep_speed(state)                 # --speed only
    watch_traffic(state)              # read every car once a game second
    pending upgrade?  -> pick_upgrade
    else if not paused and the simulation has stepped past the last edit -> build(state)
```

Building only after the clock has passed `last_build_time` keeps the bot from planning against a
map whose last edits haven't been applied yet.

## Reading the cars: `watch_traffic`

- **Car reads:** once per `CAR_READ_EVERY` (1) game second, and not while paused,
  `Session.read_cars()` reads every car. Each read feeds `JamTracker` (the jam levels the planner's
  `Board.jam` uses) and `TrafficDay` (the log).
- **Route checks:** every `ROUTE_CHECK_EVERY` (5) game seconds, `check_routes` compares the cars'
  routes with the planner's.
- **Failures:** if a read fails, the bot logs "can't read the cars" once and plays on without
  traffic data.
- **`jam_levels()`:** the jam levels for `Board`, or `{}` when the jam cost is off
  (`--no-jam-cost` sets `planner.USE_JAM_COST = False`). Cars are still read and logged either way.
  All three places that build a `Board` (`build`, `optimise`, and `plan_shortcut` through it) pass
  them.
- **Snapshots:** `save_snapshot` includes the latest car read, so `tools/routecheck.py` and tests
  can replay the route check offline.

## `build`: one kind of change per tick

1. **`repair_crossings`**: complete bridges and tunnels missing a segment.
2. **`reclaim_dead_ends`**: clear roads that lead nowhere.
3. **`place_junction_upgrades`**: from `route_colours` loads, put a roundabout on the busiest
   junction carrying at least `ROUNDABOUT_MIN_LOAD` (8) houses, else a traffic light at 5+. If the
   game refuses outright (codes in `PLACEMENT_BROKEN`), that upgrade is valued at 0 for the rest of
   the game.
4. **Connections**, planned together on one `Board` so they share tiles:
   - unfed destinations first, busiest first (`plan_destination`);
   - if one of them gets no road at all and one more bridge (or tunnel) would connect it, but none
     is in stock or mothballed on its way back, **`reclaim_crossing`** takes one down instead of
     drawing anything this tick. It picks the first of `reclaimable_crossings` (least harm first)
     for which the destination's plan, made on the map without it, exists and doesn't cross at the
     same spot. It clears one deck tile, and the game removes the whole crossing. It logs "no
     bridge left for destination … taking down the bridge at …";
   - then unconnected houses: colours with a pressured destination first, then houses with cars
     waiting (`plan_house`). Pressured networks are preferred.
   - `take()` accepts a plan only within the remaining concrete and while its key isn't backed off.
   - House roads may not use the last `CROSSINGS_KEPT_FOR_DESTINATIONS` (1) bridge or tunnel in
     stock: an unfed destination across the water ends the game, one unconnected house doesn't.
     Feeders keep one as well. Bypasses, shortcuts and separate roads keep `CROSSING_RESERVE` (2),
     through `spare_crossings`.
     A plan whose segments all exist already, and that doesn't join a road another plan draws this
     tick, is backed off too (with a log line): drawing it again would repeat every tick and keep
     `optimise` from ever running.
   - When a destination or house gets no plan, or its plan needs more concrete than is left, the
     reason is logged (`… no road found`, `… needs N new road tiles, M left`), at most once per
     `GIVE_UP_FOR` each (`Bot.explain`).
   - `take()` also accepts at most one plan that crosses water or a mountain per tick, so no other
     plan claims tiles or stock for a crossing that won't be drawn. Bridges and tunnels come off
     their own budgets.
   - Plain roads go out in one batch; the crossing, if any, is drawn segment by segment.
5. **`optimise`** when nothing needed connecting (at most once per `OPTIMISE_EVERY` = 5 sim s), doing
   the first that applies:
   1. `feed_short_destinations`: a pressured destination with pins that have no car sent, and no
      idle car of its colour on its network, gets `plan_feeder` from the nearest house of its colour
      with idle cars on another network (up to `FEEDER_CANDIDATES` = 4 tried).
   2. Bypass (`plan_bypass`) for a pressured destination with at least `BYPASS_MIN_EN_ROUTE` (3) cars
      on the way.
   3. Shortcut (`plan_shortcut`) for the houses with the longest private roads (up to 12), searched
      with the same load and colour costs as new connections.
   4. `separation_cut`: remove the link that puts a third colour on a road. If the game refuses any
      of it, the whole cut is backed off (keyed by its cut tile) for `GIVE_UP_FOR`.
   5. Separate road (`plan_separation`) when there is no cut to make, every destination is fed and
      none is under pressure, keeping `SEPARATION_RESERVE` (20) road tiles in stock: up to
      `SEPARATION_CANDIDATES` (4) plans per pass, going down `separation_candidates`; each house is
      tried at most once per `SEPARATE_RETRY` (120 sim s). The next pass's `separation_cut` then
      removes the old link if cars still take it.

Steps 3–6 depend only on the map, so their "found nothing" results are remembered in
`found_nothing` for the map they saw (`map_key`: roads, houses, active destinations, stock,
refused moves) and not worked out again until it changes. They are the slow part of a pass.
   6. `redundant_tiles`: clear loop tiles nobody needs. It's given the cut from step 4 (`[]` when
      backed off), so a refused cut doesn't hold it up.

All clearing goes through `Bot.clear`, which clears loose end first and backs off tiles the game
refuses.

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
| Separation pending | a `separation_cut` exists and isn't backed off | `redundant_tiles` returns nothing, so the colour's own road isn't removed first |

## Upgrades

`pick_upgrade` values each offered package as `UPGRADE_VALUE[type] × amount + extra concrete`, in
road tiles, and takes the best (`upgrade_worth`). Bridges and tunnels are raised to
`WATER_CROSSING_VALUE` (8) on maps with water or mountains, and with fewer than `CROSSING_RESERVE`
(2) in stock one is worth `FIRST_CROSSING_VALUE` (25).

Test games were lost to a destination across the water when 30 concrete was taken over "bridge +20
roads" with no bridge left. Later ones were lost the same way with one bridge left: on Tokyo the
next colour usually spawns across a river, and that bridge was soon spent. Motorways are worth 0, since the bot
never builds them.

It picks through the upgrade screen's own handler, waits until the pending count drops, and retries
if the screen ignored the click. After 15 s without a visible screen it schedules the selection
command directly.

## Speed (`--speed N`)

`keep_speed` sets the multiplier from the first tick, and checks it every tick (the game can reset
its speed after pauses and upgrade screens). To check the speed, use the gap between day lines
(about 20 s per day at 1×). On exit, `main()` restores normal speed.

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
HH:MM:SS   traffic: N cars, R reads (A ms avg, M max), up to Q stopped in queues; jammed tiles: (x, y) L, …
HH:MM:SS   routes: K checks, a% match the planner, b% other equally short route, c% different, d% off the planned route; e% stay on house_routes
HH:MM:SS     car I (house (x, y), colour G, driving_to_destination) at (x, y): off route, T tiles off the planned route […]; game … vs planned …
HH:MM:SS game over: score S in week W
```

How to read a loss: in the last destination report, look at the destination with the full timer.

- **Many "no car" pins and 0 idle cars**: the colour is short of cars. It needs more houses
  connected.
- **Most pins have a car on the way**: there are enough cars, but the route is slow or jammed.
- **Idle cars but "no car" pins**: the destination is unreachable from those houses, which points to
  a connectivity bug.
- **The traffic lines show where cars actually queue.** "Up to Q stopped in queues" is the most
  cars stopped behind another car or a junction at one read that day. "Jammed tiles" are road tiles
  whose rolling average of stopped cars is at least 1.5.
