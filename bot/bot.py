"""Mini Motorways bot.

Reads the game's state through the injected bridge, connects every house to a destination of its
colour by drawing roads with the game's own TileEditor, and picks the weekly upgrades.

    ./launch.sh                          # start the game with the bridge
    python3 bot.py                       # play the game you start from the menu
    python3 bot.py --start Tokyo         # start a new game on a map and play it
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import time
from collections import Counter

from game import (EDIT_ALREADY_EXISTS, EDIT_RESULT_NAMES, EDIT_SKIPPED, EDIT_SUCCESS, GameMode, Session, State, Upgrade,
                  neighbor, snapshot)
import planner
from mmbridge import Bridge, BridgeError
from traffic import JamTracker, TrafficDay, check_routes
from planner import (Board, Network, RoadGraph, clear_tiles, dead_ends, group_roots, house_spur, is_served,
                     reclaimable_crossings,
                     junctions_by_load, octile, plan_bypass, plan_destination, plan_feeder, plan_house,
                     plan_separation, plan_shortcut, redundant_tiles, route_colours, route_loads, separation_candidates,
                     separation_cut, unfed_destinations, unfinished_crossings)

log = logging.getLogger("bot")
SNAPSHOT_DIR = pathlib.Path(__file__).resolve().parent / "logs"

# How much one unit of each upgrade is worth to this bot, in road tiles. It draws roads, bridges and
# tunnels, and puts roundabouts and traffic lights on its busiest junctions. It never builds
# motorways, so those are worth nothing to it.
UPGRADE_VALUE = {
    Upgrade.CONCRETE: 1.0,
    Upgrade.BRIDGE: 0.5,  # raised to WATER_CROSSING_VALUE on maps with water
    Upgrade.TUNNEL: 0.5,  # raised on maps with mountains
    Upgrade.ROUNDABOUT: 3.0,  # dropped to 0 if the game refuses to place them
    Upgrade.MOTORWAY: 0.0,
    Upgrade.TRAFFIC_LIGHT: 1.0,  # likewise
}
WATER_CROSSING_VALUE = 8.0
# With none in stock on a map that has water (or mountains), a bridge (tunnel) is worth this much:
# a destination across the water can't be fed without one, and an unfed destination ends the game.
# Test games lost exactly so, after taking 30 concrete over "bridge +20 roads" with no bridge left.
FIRST_CROSSING_VALUE = 25.0
# The last bridge or tunnel in stock is kept for destinations: a house road (or a feeder) may not use it.
CROSSINGS_KEPT_FOR_DESTINATIONS = 1
# On a map with water (mountains), bridges (tunnels) are kept at this many: below it, one offered is worth
# FIRST_CROSSING_VALUE, and optimisations (bypasses, shortcuts, separate roads) may only use crossings
# beyond it. On Tokyo every new colour tends to need a crossing; test games were lost to a destination
# that spawned across a river after 30 concrete was taken over "bridge +20 roads" with one bridge left,
# or after the last bridge went on a bypass or a separate road.
CROSSING_RESERVE = 2
GIVE_UP_AFTER = 5  # failed attempts before a house is left alone for a while
GIVE_UP_FOR = 20.0  # sim seconds
REMEMBER_REFUSED_FOR = 60.0  # sim seconds a move the game refused is kept out of plans
# Refusals that say nothing about the move itself (out of stock, already there, not tried).
NOT_ABOUT_THE_MOVE = {-2, -1, 5, 6, 7, 13}
DIRECTION_NAMES = "N NE E SE S SW W NW".split()
OPTIMISE_EVERY = 5.0  # sim seconds between passes that shorten roads already built
BUILT_THEN_CLEARED = 10.0  # sim seconds: a road cleared this soon after being drawn didn't join
SHORTCUT_TRUST = 20.0  # sim seconds after a shortcut in which losing the house's connection blames it
SHORTCUT_CANDIDATES = 12  # houses with the longest private roads considered per pass
FEEDER_CANDIDATES = 4  # houses with idle cars tried per destination short of cars
SEPARATION_CANDIDATES = 4  # separate roads planned per pass, for houses on a road with 3+ colours
SEPARATE_RETRY = 120.0  # sim seconds before a house is tried for a separate road again
SEPARATION_RESERVE = 20  # road tiles a separate road must leave in stock for new connections
MIN_TICK = 0.05  # real seconds between looks at the game, however fast it runs
CAR_READ_EVERY = 1.0  # game seconds between reads of every car (Session.read_cars, ~20-40 ms each)
ROUTE_CHECK_EVERY = 5.0  # game seconds between comparisons of the cars' routes with the planner's
# A destination is under pressure once its pins (plus how far its overcrowd timer has run) reach
# this share of its limit. Houses of its colour are then connected first and prefer its network,
# it can be fed from another network's idle cars, and if its cars are slow to arrive it gets a bypass.
PRESSURE_HIGH = 0.5
BYPASS_MIN_EN_ROUTE = 3  # pins with a car already on the way before slow routes are worth a bypass
# Junctions carrying at least this many houses get a roundabout, then a traffic light.
ROUNDABOUT_MIN_LOAD = 8
TRAFFIC_LIGHT_MIN_LOAD = 5
JUNCTION_UPGRADES = ((Upgrade.ROUNDABOUT, ROUNDABOUT_MIN_LOAD), (Upgrade.TRAFFIC_LIGHT, TRAFFIC_LIGHT_MIN_LOAD))
# Codes saying a roundabout or traffic light can never go through this way (the call itself failed).
PLACEMENT_BROKEN = {-1, 1}


class Bot:
    def __init__(self, session: Session, tick: float = 0.4, speed: float = 1.0):
        self.s = session
        self.speed = speed
        # Keep the same number of looks per game second when the game runs faster.
        self.tick = max(MIN_TICK, tick / speed)
        self.last_build_time: float | None = None
        self.failures: Counter = Counter()
        self.skip_until: dict = {}
        self.refused: dict = {}  # (tile, direction) -> sim time until which it is avoided
        self.next_optimise = 0.0
        self.game_key = None
        self.last_report_day = None
        self.picked_at: float | None = None  # when the last upgrade was picked (monotonic)
        self.picked_pending = 0
        self.choice_seen_at: float | None = None
        self.served_at_day_start: dict[int, int] = {}  # destination -> pins served when the day began
        self.cannot_place: set[Upgrade] = set()  # junction upgrades the game wouldn't take
        self.shortcut_at: dict = {}  # house -> sim time of its last shortcut
        self.built: dict = {}  # tile -> (sim time, segments touching it) for roads drawn recently
        # Optimising steps that found nothing to do, and the map they looked at (see optimise).
        self.explained: dict = {}  # key -> sim time its "can't connect" reason was last logged
        self.snapshotted: set = set()  # destinations whose first overcrowded day was saved
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.found_nothing: set = set()
        self.found_nothing_for: tuple | None = None
        self.values = dict(UPGRADE_VALUE)
        self.jams = JamTracker()
        self.traffic = None  # the latest Traffic read
        self.traffic_day = TrafficDay()
        self.cars_at: float | None = None  # game time of the last car read
        self.routes_at = float("-inf")  # game time of the last route check
        self.car_read_failed = False

    def run(self, stop_at_game_end: bool = True):
        log.info("waiting for a game...")
        while True:
            state = self.s.read()
            if state is None:
                time.sleep(1.0)
                continue
            if self.s.game_key != self.game_key:
                self.on_new_game(state)
            if state.ended:
                log.info("game over: score %d in week %d", state.score, state.week)
                self.report_destinations(state, "at the end")
                self.report_traffic()
                self.save_snapshot(state, "end")
                if stop_at_game_end:
                    return state.score
                time.sleep(2.0)
                continue
            self.report(state)
            self.keep_speed(state)
            self.watch_traffic(state)
            if state.pending_choices:
                self.pick_upgrade(state)
            else:
                self.picked_at = None
                # Only build once the simulation has stepped past our last edits, so nothing
                # is scheduled twice while the game is paused.
                if not state.paused and (self.last_build_time is None or state.time > self.last_build_time):
                    self.build(state)
            time.sleep(self.tick)

    def keep_speed(self, state: State):
        """Hold the game at self.speed. The game may put its own speed back (after a pause or the
        upgrade screen), so check it each tick while the game runs and set it again if it moved."""
        if self.speed == 1.0 or state.paused:
            return
        try:
            if abs(self.s.game_speed() - self.speed) > 1e-3:
                self.s.set_game_speed(self.speed)
        except BridgeError as e:
            log.info("can't change the game speed (%s); playing at normal speed", e)
            self.speed = 1.0

    def on_new_game(self, state: State):
        self.game_key = self.s.game_key
        self.last_build_time = None
        self.failures.clear()
        self.skip_until.clear()
        self.refused.clear()
        self.next_optimise = 0.0
        self.served_at_day_start.clear()
        self.cannot_place.clear()
        self.shortcut_at.clear()
        self.built.clear()
        self.found_nothing.clear()
        self.found_nothing_for = None
        self.explained.clear()
        self.snapshotted.clear()
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.jams.reset()
        self.traffic = None
        self.traffic_day = TrafficDay()
        self.cars_at = None
        self.routes_at = float("-inf")
        water = any(t.water for t in self.s.terrain.values())
        mountains = any(t.mountain for t in self.s.terrain.values())
        self.values = dict(UPGRADE_VALUE)
        if water:
            self.values[Upgrade.BRIDGE] = WATER_CROSSING_VALUE
        if mountains:
            self.values[Upgrade.TUNNEL] = WATER_CROSSING_VALUE
        log.info("playing %s (mode %s): %d houses, %d destinations, water=%s mountains=%s",
                 self.s.b.get(self.s.h["city"], "Definition.@ToString"), self.s.mode.name,
                 len(state.houses), len(state.destinations), water, mountains)

    def report(self, state: State):
        day = (state.week, state.day)
        if day == self.last_report_day:
            return
        self.last_report_day = day
        net = Network(state)
        roots = group_roots(state, net)
        served = sum(is_served(h, net, roots) for h in state.houses)
        crowded = [d for d in state.destinations.values() if d.overcrowded]
        for d in crowded:
            if d.key not in self.snapshotted:  # the first day a destination overcrowds
                self.snapshotted.add(d.key)
                self.save_snapshot(state, f"overcrowded-colour{d.group}")
        unfed = unfed_destinations(state, net)
        log.info("week %d day %d: score %d, %d/%d houses connected, %d/%d destinations fed, %d road tiles left%s",
                 state.week, state.day, state.score, served, len(state.houses),
                 sum(d.active for d in state.destinations.values()) - len(unfed),
                 sum(d.active for d in state.destinations.values()), state.available[Upgrade.CONCRETE],
                 f", OVERCROWDED: {', '.join(f'colour {d.group} ({d.waiting} pins)' for d in crowded)}"
                 if crowded else "")
        self.report_destinations(state, "yesterday")
        self.report_traffic()

    def watch_traffic(self, state: State):
        """Read every car once a game second: feed the jam averages, and every few seconds compare the
        game's routes with the planner's. A failed read is logged once and the bot plays on without."""
        if state.paused or self.car_read_failed:
            return
        if self.cars_at is not None and state.time - self.cars_at < CAR_READ_EVERY:
            return
        try:
            traffic = self.s.read_cars()
        except BridgeError as e:
            log.info("can't read the cars (%s); playing on without traffic data", e)
            self.car_read_failed = True
            return
        if traffic is None:
            return
        self.cars_at = state.time
        self.traffic = traffic
        self.jams.update(traffic)
        self.traffic_day.add_read(traffic)
        if state.time - self.routes_at >= ROUTE_CHECK_EVERY:
            self.routes_at = state.time
            self.traffic_day.add_routes(check_routes(state, traffic))

    def jam_levels(self) -> dict:
        """Jam level per road tile for the planner's costs (empty with the jam cost off)."""
        return dict(self.jams.level) if planner.USE_JAM_COST else {}

    def report_traffic(self):
        """The day's car reads, queues, jammed tiles and route check, then start a new day."""
        if self.traffic_day.reads:
            for line in self.traffic_day.summary(self.jams.jammed()):
                log.info("%s", line)
        self.traffic_day = TrafficDay()

    def save_snapshot(self, state: State, event: str):
        """Write the map to logs/ so the situation can be replayed offline (game.load_snapshot)."""
        try:
            SNAPSHOT_DIR.mkdir(exist_ok=True)
            path = SNAPSHOT_DIR / f"snapshot-{self.started}-week{state.week}-day{state.day}-{event}.json"
            path.write_text(json.dumps(snapshot(state, self.s.terrain, self.traffic)))
            log.info("saved the map to %s", path.relative_to(SNAPSHOT_DIR.parent))
        except (OSError, TypeError, ValueError) as e:
            log.info("couldn't save the map (%s)", e)

    def report_destinations(self, state: State, when: str):
        """Per destination: pins with no car yet (the colour is short of cars), pins with a car on
        the way (its routes are slow), arrivals, and the overcrowd timer. Busiest first."""
        idle = Counter()
        cars = Counter()
        for h in state.houses:
            idle[h.group] += h.waiting
            cars[h.group] += h.cars
        for d in sorted((d for d in state.destinations.values() if d.active), key=lambda d: -d.pressure):
            arrived = d.served - self.served_at_day_start.get(d.key, d.served)
            self.served_at_day_start[d.key] = d.served
            timer = f", timer {d.overcrowd_time:.0f}/{d.max_overcrowd_time:.0f}" if d.max_overcrowd_time else ""
            log.info("  colour %d at %s: %d pins (%d no car, %d car on the way) of %d, %d arrivals %s%s; "
                     "colour has %d/%d cars idle",
                     d.group, d.tiles[0], d.unassigned + d.en_route, d.unassigned, d.en_route, d.pin_limit,
                     arrived, when, timer, idle[d.group], cars[d.group])

    # Roads ---------------------------------------------------------------------------------

    def build(self, state: State):
        # Each upkeep step changes the map, so plan only on a fresh read afterwards.
        if self.repair_crossings(state) or self.reclaim_dead_ends(state):
            return
        graph = RoadGraph(state)
        colours = route_colours(state, graph)
        load = route_loads(state, graph)
        if self.place_junction_upgrades(state, load):
            return
        net = Network(state)
        roots = group_roots(state, net)
        under_pressure = self.pressured(state)
        # Networks holding a destination close to overcrowding: new houses of its colour join these.
        pressured = {net.find(state.carparks[d.carpark].tiles[0]) for d in under_pressure
                     if d.carpark in state.carparks and state.carparks[d.carpark].tiles}
        refused = {move for move, until in self.refused.items() if until > state.time}
        board = Board(state, self.s.terrain, refused, load=load, colours=colours, jam=self.jam_levels())
        budget = {"concrete": state.available[Upgrade.CONCRETE], "bridges": state.available[Upgrade.BRIDGE],
                  "tunnels": state.available[Upgrade.TUNNEL]}
        plans = []

        def take(plan, key, label):
            if self.skip_until.get(key, -1) >= state.time:
                return
            if plan is None:
                self.explain(state, key, f"{label}: no road found")
                return
            if plan.new_tiles > budget["concrete"]:
                self.explain(state, key, f"{label}: needs {plan.new_tiles} new road tiles, "
                                         f"{budget['concrete']} left")
                return
            # Only one road over water or through a mountain is drawn per tick (below), so a second
            # one mustn't claim tiles or stock: later plans would join a road that is never drawn.
            if self.crosses(plan) and any(self.crosses(p) for p in plans):
                return
            if all(state.joined(p, d) for p, d in plan.segments) and not any(t in board.planned for t in plan.tiles):
                # Everything it would draw is there already (and it doesn't join a road another plan
                # draws this tick), yet the planner still sees the house or destination as
                # unconnected: drawing it again would only repeat every tick and keep optimise from
                # ever running. Leave it alone for a while.
                log.info("%s: nothing left to draw, but still not connected; leaving it for now", plan.label)
                self.skip_until[plan.key] = state.time + GIVE_UP_FOR
                return
            budget["concrete"] -= plan.new_tiles
            budget["bridges"] -= plan.bridges
            budget["tunnels"] -= plan.tunnels
            board.claim(plan)
            plans.append(plan)

        # A destination with no houses of its colour on its network gets no cars and will overflow,
        # so those come first, busiest first.
        unfed = unfed_destinations(state, net)
        stranded = []  # unfed destinations with no road at all
        for dest in sorted(unfed, key=lambda d: -d.waiting):
            plan = plan_destination(board, dest, net, budget["bridges"], budget["tunnels"])
            if plan is None:
                stranded.append(dest)
            take(plan, ("destination", dest.key), f"destination {dest.tiles[0]} (colour {dest.group})")
        # One of them cut off by water or a mountain with nothing in stock to cross it ends the game, so
        # an existing bridge or tunnel nobody needs is taken down to get its upgrade back. Nothing else
        # is drawn this tick: other plans could use the crossing being taken down.
        if stranded and self.reclaim_crossing(state, stranded, refused, load, colours):
            return
        # Then houses that can't reach their colour: colours with a destination under pressure first
        # (each connected house is more cars for it), then those with cars already waiting.
        short = {d.group for d in under_pressure}
        unserved = [h for h in state.houses if not is_served(h, net, roots)]
        for h in unserved:
            # A shortcut that left its house cut off would be built and undone over and over.
            if state.time - self.shortcut_at.pop(h.pos, -SHORTCUT_TRUST) < SHORTCUT_TRUST:
                log.info("shortcut for house %s cut it off; no more shortcuts for it", h.pos)
                self.skip_until[("shortcut", h.pos)] = float("inf")
        keep = CROSSINGS_KEPT_FOR_DESTINATIONS
        for house in sorted(unserved, key=lambda h: (h.group not in short, -h.waiting)):
            bridges, tunnels = max(0, budget["bridges"] - keep), max(0, budget["tunnels"] - keep)
            kept = " (the last bridge/tunnel is kept for destinations)" if (bridges, tunnels) != (
                budget["bridges"], budget["tunnels"]) else ""
            take(plan_house(board, house, net, roots, bridges, tunnels, pressured),
                 ("house", house.pos), f"house {house.pos} (colour {house.group}){kept}")
        if not plans:
            # Separate roads only once every destination has its houses and none is close to
            # overcrowding: until then the concrete is needed for connections.
            self.optimise(state, refused, load, colours, separate=not unfed and not under_pressure)
            return

        # Plain roads go out in one batch. A road over water or through a mountain is drawn one
        # tile at a time (each segment must exist before the next validates); take() lets at most
        # one through per tick.
        plain = [plan for plan in plans if not self.crosses(plan)]
        crossing = [plan for plan in plans if self.crosses(plan)]
        results = []
        codes = self.draw(state, [seg for plan in plain for seg in plan.segments])
        i = 0
        for plan in plain:
            results.append((plan, codes[i:i + len(plan.segments)]))
            i += len(plan.segments)
        for plan in crossing:
            results.append((plan, self.draw(state, plan.segments, sequential=True)))
        self.last_build_time = state.time
        for plan, mine in results:
            self.record(state, plan, mine)

    @staticmethod
    def pressured(state: State) -> list:
        """Active destinations close to overcrowding, most pressed first."""
        return sorted((d for d in state.destinations.values() if d.active and d.pressure >= PRESSURE_HIGH),
                      key=lambda d: -d.pressure)

    def place_junction_upgrades(self, state: State, load: dict) -> bool:
        """Put a roundabout, else a traffic light, on the busiest junction that lacks one. Turns and
        junctions slow cars and the game never routes around a jam, so the busiest junctions are
        where cars back up. Returns whether one was placed."""
        for kind, min_load in JUNCTION_UPGRADES:
            if kind in self.cannot_place or state.available[kind] <= 0:
                continue
            for p in junctions_by_load(state, load, min_load):
                if self.skip_until.get((kind, p), -1) >= state.time:
                    continue
                code = self.s.add_junction_upgrade(kind, p)
                if code == EDIT_SUCCESS:
                    log.info("%s at %s (%d houses use it)", kind.name.lower().replace("_", " "), p, load[p])
                    self.last_build_time = state.time
                    return True
                if code in PLACEMENT_BROKEN:
                    log.info("can't place %ss (%s); valuing them at 0 from now on", kind.name.lower(),
                             EDIT_RESULT_NAMES.get(code, code))
                    self.cannot_place.add(kind)
                    self.values[kind] = 0.0
                    break
                self.skip_until[(kind, p)] = state.time + GIVE_UP_FOR
        return False

    def crosses(self, plan) -> bool:
        return any(not self.s.terrain[t].driveable for t in plan.tiles if t in self.s.terrain)

    def record(self, state: State, plan, mine: list[int]) -> bool:
        """Log a plan's outcome and remember refused moves. Returns whether it all went through."""
        bad = [c for c in mine if c not in (EDIT_SUCCESS, EDIT_ALREADY_EXISTS)]
        for segment, code in zip(plan.segments, mine):
            if code not in NOT_ABOUT_THE_MOVE and code != EDIT_SUCCESS:
                self.refused[segment] = state.time + REMEMBER_REFUSED_FOR
        if bad:
            self.failures[plan.key] += 1
            log.info("%s (colour %d): %d/%d segments refused (%s)%s", plan.label, plan.group, len(bad), len(mine),
                     ", ".join(sorted({EDIT_RESULT_NAMES.get(c, str(c)) for c in bad})),
                     self.describe_refusal(state, plan, mine))
            if self.failures[plan.key] >= GIVE_UP_AFTER:
                self.skip_until[plan.key] = state.time + GIVE_UP_FOR
                self.failures[plan.key] = 0
            return False
        self.failures.pop(plan.key, None)
        for p, d in plan.segments:
            for t in (p, neighbor(p, d)):
                self.built.setdefault(t, (state.time, []))[1].append((p, d))
        log.info("%s (colour %d): road of %d tiles (%d new)%s%s%s", plan.label, plan.group, len(plan.tiles),
                 plan.new_tiles, f", {plan.crossings} crossing(s)" if plan.crossings else "",
                 ", turned the house" if plan.turned_house else "",
                 f", frees {plan.saves} old tile(s)" if plan.saves else "")
        return True

    def draw(self, state: State, segments: list, sequential: bool = False) -> list[int]:
        """Send the game the segments the map doesn't have yet; the rest come back as
        EDIT_ALREADY_EXISTS without asking. Re-sending an existing segment is harmless on land, but
        on a bridge deck the game counts the road already there as a junction on water and refuses
        it (CannotCreateBridge), which used to stop every plan that crossed an existing bridge."""
        codes = [EDIT_ALREADY_EXISTS] * len(segments)
        todo = [i for i, (p, d) in enumerate(segments) if not state.joined(p, d)]
        if todo:
            for i, code in zip(todo, self.s.add_roads([segments[i] for i in todo], sequential=sequential)):
                codes[i] = code
        return codes

    def describe_refusal(self, state: State, plan, mine: list[int]) -> str:
        """Where the first refused segment was, for refusals that depend on the terrain."""
        for (p, d), code in zip(plan.segments, mine):
            if code in (EDIT_SUCCESS, EDIT_ALREADY_EXISTS) or code in NOT_ABOUT_THE_MOVE:
                continue
            q = neighbor(p, d)

            def kind(t):
                ter = self.s.terrain.get(t)
                if ter is None:
                    return "off-map"
                return ("water" if ter.water else "mountain" if ter.mountain else "land") + \
                    ("" if ter.driveable else "/undriveable") + f"/{state.links(t)} links"
            return (f"; first at {p} {DIRECTION_NAMES[d]} ({kind(p)} -> {kind(q)}), "
                    f"bridges {state.available[Upgrade.BRIDGE]}, tunnels {state.available[Upgrade.TUNNEL]}")
        return ""

    def feed_short_destinations(self, state: State, board: Board, pressured: list) -> bool:
        """A destination under pressure whose pins wait with no car sent is short of cars: unless the
        colour has idle cars it can already reach, connect the nearest house of its colour that has
        idle cars but sits on another network. (When every pin has a car on the way, the cars are
        there but slow, and the bypass below reworks the roads instead.)"""
        net = Network(state)
        for dest in pressured:
            if dest.unassigned == 0 or self.skip_until.get(("feeder", dest.key), -1) >= state.time:
                continue
            cp = state.carparks.get(dest.carpark)
            if not cp or not cp.tiles:
                continue
            target = net.find(cp.tiles[0])
            same = [h for h in state.houses if h.group == dest.group]
            if any(h.waiting for h in same if net.find(h.pos) == target):
                continue  # idle cars already reach it: not a supply problem
            unused = sorted((h for h in same if h.waiting and net.find(h.pos) != target),
                            key=lambda h: octile(h.pos, cp.tiles[0]))
            for house in unused[:FEEDER_CANDIDATES]:
                plan = plan_feeder(board, house, net, dest, *self.spare_crossings(state, CROSSINGS_KEPT_FOR_DESTINATIONS))
                if plan is None or plan.new_tiles > state.available[Upgrade.CONCRETE]:
                    continue
                log.info("destination %s is short of cars (%d pins with none sent); feeding it from house %s",
                         dest.tiles[0], dest.unassigned, house.pos)
                self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
                self.skip_until[("feeder", dest.key)] = state.time + GIVE_UP_FOR
                self.last_build_time = state.time
                return True
            self.skip_until[("feeder", dest.key)] = state.time + GIVE_UP_FOR
        return False

    def explain(self, state: State, key, message: str):
        """Log why something couldn't be connected, at most once per GIVE_UP_FOR for each key."""
        if self.explained.get(key, -GIVE_UP_FOR) + GIVE_UP_FOR <= state.time:
            self.explained[key] = state.time
            log.info("%s", message)

    def optimise(self, state: State, refused: set, load: dict, colours: dict, separate: bool = True) -> bool:
        """Improve roads already built. First, a destination whose cars are slow to arrive gets a
        bypass from its car park to the far side of its network. Then houses whose private road is
        longer than a fresh connection would be get reconnected (the old road then dead-ends and is
        reclaimed). Then loops and doubled connections lose the tiles nobody needs."""
        if state.time < self.next_optimise:
            return False
        self.next_optimise = state.time + OPTIMISE_EVERY
        terrain = self.s.terrain
        started = time.monotonic()
        pressured = self.pressured(state)
        board = Board(state, terrain, refused, load=load, colours=colours, jam=self.jam_levels())
        if self.feed_short_destinations(state, board, pressured):
            return True
        for dest in pressured:
            if dest.en_route < BYPASS_MIN_EN_ROUTE or self.skip_until.get(("bypass", dest.key), -1) >= state.time:
                continue
            plan = plan_bypass(board, dest, *self.spare_crossings(state, CROSSING_RESERVE))
            if plan is None or plan.new_tiles > state.available[Upgrade.CONCRETE]:
                self.skip_until[("bypass", dest.key)] = state.time + GIVE_UP_FOR
                continue
            self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
            self.last_build_time = state.time
            return True

        # Shortcuts, cuts, separate roads and loop clearing depend only on the map (roads, houses,
        # destinations, stock, refused moves). While it hasn't changed, the ones that found nothing
        # last pass aren't worked out again: they are the slow part of a pass.
        key = self.map_key(state, refused)
        if key != self.found_nothing_for:
            self.found_nothing_for, self.found_nothing = key, set()
        nothing = self.found_nothing

        graph = RoadGraph(state)
        trips, _ = graph.evaluate()
        spurs = sorted(((len(house_spur(state, terrain, h, graph)), i, h) for i, h in enumerate(state.houses)),
                       reverse=True)
        for length, _, house in spurs[:SHORTCUT_CANDIDATES]:
            if length == 0:
                break
            if self.skip_until.get(("shortcut", house.pos), -1) >= state.time or ("shortcut", house.pos) in nothing:
                continue
            plan = plan_shortcut(state, terrain, house, refused, *self.spare_crossings(state, CROSSING_RESERVE),
                                 graph, trips, load, colours, self.jam_levels())
            if plan is None or plan.new_tiles > state.available[Upgrade.CONCRETE]:
                nothing.add(("shortcut", house.pos))
                continue
            self.shortcut_at[house.pos] = state.time
            self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
            self.last_build_time = state.time
            return True

        # A cut is cleared as a whole (the cut tile, then the chain it leaves), so it is backed off
        # as a whole, by its cut tile, if the game refuses any of it.
        cut = [] if "cut" in nothing else separation_cut(state, terrain)
        if not cut:
            nothing.add("cut")
        if cut and self.skip_until.get(("cut", cut[0]), -1) >= state.time:
            cut = []
        if cut:
            cleared = self.clear(state, cut, "cut %d road tile(s) at %s so no road carries more than two colours")
            if len(cleared) < len(cut):
                self.skip_until[("cut", cut[0])] = state.time + GIVE_UP_FOR
            return bool(cleared)

        # No cut to make: give a colour that shares a road with two others a road of its own (the
        # next pass then cuts the old link if cars still take it).
        concrete_free = state.available[Upgrade.CONCRETE] - SEPARATION_RESERVE
        if not separate or concrete_free <= 0:
            candidates = []  # not now; this says nothing about the map, so nothing is noted
        else:
            candidates = [] if "separate" in nothing else separation_candidates(state)
            if not candidates:
                nothing.add("separate")
        tried = 0
        for house in candidates:
            if self.skip_until.get(("separate", house.pos), -1) >= state.time or ("separate", house.pos) in nothing:
                continue
            if tried == SEPARATION_CANDIDATES:
                break
            tried += 1
            self.skip_until[("separate", house.pos)] = state.time + SEPARATE_RETRY
            plan = plan_separation(board, house, *self.spare_crossings(state, CROSSING_RESERVE))
            if plan is None or plan.new_tiles > concrete_free:
                nothing.add(("separate", house.pos))
                continue
            self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
            self.last_build_time = state.time
            return True

        # cut is [] here, so a cut the game refused doesn't hold up clearing loops.
        redundant = [] if "redundant" in nothing else redundant_tiles(state, terrain, cut=cut)
        if not redundant:
            nothing.add("redundant")
        tiles = [p for p in redundant if self.skip_until.get(("clear", p), -1) < state.time]
        if time.monotonic() - started > 1.0:
            log.info("(optimising took %.1fs)", time.monotonic() - started)
        if not tiles:
            return False
        return bool(self.clear(state, tiles, "removed %d redundant road tile(s) at %s"))

    @staticmethod
    def map_key(state: State, refused: set) -> tuple:
        """What the optimising steps depend on: roads, houses, active destinations, stock and the
        moves the game refused."""
        return (frozenset((p, tuple(r)) for p, r in state.roads.items() if any(r)),
                tuple((h.pos, h.driveway, h.group) for h in state.houses),
                tuple(sorted(d.key for d in state.destinations.values() if d.active)),
                tuple(state.available), frozenset(refused))

    def clear(self, state: State, tiles: list, message: str) -> list:
        """Clear road tiles one at a time, in order (loose end first), and log message % (count,
        tiles). A tile the game refuses to clear is left alone for GIVE_UP_FOR. Returns the tiles
        cleared."""
        codes = self.s.clear_tiles(tiles, sequential=True)
        cleared = [p for p, c in zip(tiles, codes) if c == EDIT_SUCCESS]
        for p, c in zip(tiles, codes):
            if c not in (EDIT_SUCCESS, EDIT_SKIPPED):
                self.skip_until[("clear", p)] = state.time + GIVE_UP_FOR
        if cleared:
            log.info(message, len(cleared), ", ".join(map(str, cleared)))
        self.last_build_time = state.time
        return cleared

    def reclaim_crossing(self, state: State, stranded: list, refused: set, load: dict, colours: dict) -> bool:
        """For a destination that can't be fed without a bridge (tunnel) when none is in stock or on
        its way back: take down the bridge (tunnel) that cuts nobody off and lengthens trips least, if
        that lets the destination be connected. Clearing one deck tile takes the whole crossing down;
        the game keeps it as mothballed road until the cars on it have left, then puts the upgrade
        back in stock, and the destination's own plan builds the new crossing. Returns whether one
        was taken down."""
        terrain = self.s.terrain
        candidates = None
        for kind in (Upgrade.BRIDGE, Upgrade.TUNNEL):
            if state.available[kind] or state.mothballed[kind]:
                continue  # one in stock, or on its way back
            extra = (1, state.available[Upgrade.TUNNEL]) if kind == Upgrade.BRIDGE else (state.available[Upgrade.BRIDGE], 1)
            board = Board(state, terrain, refused, load=load, colours=colours, jam=self.jam_levels())
            net = Network(state)
            needy = [d for d in stranded if plan_destination(board, d, net, *extra) is not None]
            if not needy:
                continue  # one more wouldn't connect any of them
            if candidates is None:
                candidates = reclaimable_crossings(state, terrain)
            for deck, deck_kind, lost, longer in candidates:
                key = ("reclaim", min(deck))
                if deck_kind != kind or self.skip_until.get(key, -1) >= state.time:
                    continue
                reduced = clear_tiles(state, sorted(deck))
                rboard = Board(reduced, terrain, refused, load=load, colours=colours, jam=self.jam_levels())
                plan = plan_destination(rboard, needy[0], Network(reduced), *extra)
                if plan is None or set(plan.tiles) & deck:
                    continue  # it wouldn't help, or the new crossing would go right back there
                code = self.s.clear_tiles([min(deck)])[0]
                if code != EDIT_SUCCESS:
                    self.skip_until[key] = state.time + GIVE_UP_FOR
                    continue
                self.last_build_time = state.time
                what = "bridge" if kind == Upgrade.BRIDGE else "tunnel"
                log.info("no %s left for destination %s (colour %d): taking down the %s at %s (%d tiles) to "
                         "build one there; trips up to %.1f tiles longer%s", what, needy[0].tiles[0], needy[0].group,
                         what, min(deck), len(deck), longer, f", {lost} drives to a destination lost" if lost else "")
                return True
        return False

    def repair_crossings(self, state: State) -> bool:
        """Finish bridges/tunnels left with a missing segment. Returns whether anything was built."""
        todo = [seg for seg in unfinished_crossings(state, self.s.terrain)
                if self.skip_until.get(("repair", seg), -1) < state.time]
        if not todo:
            return False
        codes = self.s.add_roads(todo, sequential=False)
        built = False
        for seg, code in zip(todo, codes):
            if code in (EDIT_SUCCESS, EDIT_ALREADY_EXISTS):
                built = True
                log.info("completed crossing at %s toward %s", seg[0], DIRECTION_NAMES[seg[1]])
            else:
                self.skip_until[("repair", seg)] = state.time + GIVE_UP_FOR
        self.last_build_time = state.time
        return built

    def reclaim_dead_ends(self, state: State) -> bool:
        """Clear roads that lead nowhere so their tiles can be used elsewhere."""
        tiles = [p for p in dead_ends(state, self.s.terrain) if self.skip_until.get(("clear", p), -1) < state.time]
        if not tiles:
            return False
        cleared = self.clear(state, tiles, "reclaimed %d dead-end road tile(s) at %s")
        for p in cleared:
            # A road that dead-ends as soon as it is drawn didn't join where the plan expected: keep
            # its moves out of plans for a while instead of drawing and clearing it again and again.
            when, segments = self.built.pop(p, (None, []))
            if when is not None and state.time - when < BUILT_THEN_CLEARED:
                log.info("road just drawn at %s dead-ended; avoiding it for now", p)
                for segment in segments:
                    self.refused[segment] = state.time + REMEMBER_REFUSED_FOR
        return bool(cleared)

    # Upgrades ------------------------------------------------------------------------------

    def upgrade_worth(self, state: State, option: dict) -> float:
        """An offered package's worth in road tiles. On a map that needs bridges (tunnels), one is
        worth FIRST_CROSSING_VALUE while fewer than CROSSING_RESERVE are in stock."""
        kind, amount = option["type"], option["amount"]
        value = self.values.get(kind, 0.0) * amount
        if kind in (Upgrade.BRIDGE, Upgrade.TUNNEL) and self.values.get(kind, 0.0) >= WATER_CROSSING_VALUE \
                and state.available[kind] < CROSSING_RESERVE and amount > 0:
            value += FIRST_CROSSING_VALUE - self.values[kind]
        return value + option["concrete"]

    @staticmethod
    def spare_crossings(state: State, keep: int) -> tuple[int, int]:
        """Bridges and tunnels a plan may start, keeping `keep` of each in stock."""
        return (max(0, state.available[Upgrade.BRIDGE] - keep), max(0, state.available[Upgrade.TUNNEL] - keep))

    def pick_upgrade(self, state: State):
        # One pick per offered choice: wait until the last one has been applied. A click while the
        # screen is still animating in is ignored by the game, so retry after a short wait.
        if self.picked_at is not None and state.pending_choices >= self.picked_pending and \
                time.monotonic() - self.picked_at < 3:
            return
        options = self.s.upgrade_choices()
        if not options:
            return
        worth = [self.upgrade_worth(state, o) for o in options]
        best = max(range(len(options)), key=worth.__getitem__)
        # Normally through the upgrade screen; if it never shows up, schedule the command directly.
        waited = time.monotonic() - (self.choice_seen_at or time.monotonic())
        if self.choice_seen_at is None:
            self.choice_seen_at = time.monotonic()
        before = state.pending_choices
        if not self.s.choose_upgrade(best, allow_command=waited > 15):
            return
        applied = False
        for _ in range(20):  # the pick lands when the simulation next steps
            time.sleep(0.25)
            after = self.s.read()
            if after is None or after.pending_choices < before:
                applied = True
                break
        log.info("week %d upgrade: %s -> %s%s", state.week,
                 " | ".join(f"{o['type'].name.lower()} x{o['amount']}" + (f" +{o['concrete']} roads" if o["concrete"] else "")
                            for o in options),
                 options[best]["type"].name.lower(), "" if applied else " (not applied yet, will retry)")
        self.picked_at = time.monotonic()
        self.picked_pending = state.pending_choices
        self.choice_seen_at = None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", metavar="MAP", help="start a new game on this map first (e.g. LosAngeles)")
    parser.add_argument("--maps", action="store_true", help="list map names and exit")
    parser.add_argument("--keep-going", action="store_true", help="keep running after the game ends")
    parser.add_argument("--port", type=int, default=None, help="bridge port (default 47811)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="run the game this many times faster, for test runs (e.g. 4)")
    parser.add_argument("--no-jam-cost", action="store_true",
                        help="don't add measured jams to route costs (cars are still read and logged)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.no_jam_cost:
        planner.USE_JAM_COST = False
    log.info("jam cost %s", "on" if planner.USE_JAM_COST else "off")

    with Bridge(**({"port": args.port} if args.port else {}), timeout=120) as bridge:
        session = Session(bridge)
        if args.maps:
            print("\n".join(session.map_names()))
            return
        if args.start:
            running = [g for g in session.games() if g["mode"] != GameMode.BACKGROUND and not g["ended"]]
            if running:
                # GameStarter only works from the main menu.
                log.info("a game is already in progress; playing it instead of starting %s", args.start)
            elif not session.start_game(args.start, GameMode.NORMAL):
                raise SystemExit(f"could not start {args.start}")
        try:
            Bot(session, speed=args.speed).run(stop_at_game_end=not args.keep_going)
        except KeyboardInterrupt:
            pass
        except BridgeError as e:
            raise SystemExit(f"bridge error: {e}")
        finally:
            if args.speed != 1.0:
                try:
                    session.set_game_speed(1.0)  # leave the game at normal speed
                except BridgeError:
                    pass


if __name__ == "__main__":
    main()
