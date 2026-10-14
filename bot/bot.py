"""Mini Motorways bot.

Reads the game's state through the injected bridge, connects every house to a destination of its
colour by drawing roads with the game's own TileEditor, and picks the weekly upgrades.

    ./launch.sh                               # start the game with the bridge
    .venv/bin/python bot.py                   # play the game you start from the menu
    .venv/bin/python bot.py --start Tokyo     # start a new game on a map and play it
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter

from game import (EDIT_ALREADY_EXISTS, EDIT_RESULT_NAMES, EDIT_SKIPPED, EDIT_SUCCESS, GameMode, Session, State, Upgrade,
                  neighbor)
from mmbridge import Bridge, BridgeError
from planner import (MAX_DETOUR_LOOP, Board, Network, RoadGraph, dead_ends, group_roots, house_spur, is_served,
                     junctions_by_load, octile, plan_bypass, plan_destination, plan_feeder, plan_house,
                     plan_shortcut, redundant_tiles, route_colours, separation_cut, unfed_destinations,
                     unfinished_crossings)

log = logging.getLogger("bot")

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
MIN_TICK = 0.05  # real seconds between looks at the game, however fast it runs
# A destination is under pressure once its pins (plus how far its overcrowd timer has run) reach
# this share of its limit. Houses of its colour then prefer its network, loops near it are kept,
# and if its cars are slow to arrive it gets a bypass.
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
        self.speed_check: tuple[float, float] | None = None  # (real time, game time) when speed was set
        self.normal_rate: float | None = None  # game seconds per real second before speeding up
        self.normal_mark: tuple[float, float] | None = None
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
        self.values = dict(UPGRADE_VALUE)

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
                if stop_at_game_end:
                    return state.score
                time.sleep(2.0)
                continue
            self.report(state)
            self.keep_speed(state)
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
        if self.normal_rate is None:  # first, time the game clock at normal speed to compare against
            now = time.monotonic()
            if self.normal_mark is None or state.time < self.normal_mark[1]:
                self.normal_mark = (now, state.time)
            elif now - self.normal_mark[0] >= 5 and state.time > self.normal_mark[1]:
                self.normal_rate = (state.time - self.normal_mark[1]) / (now - self.normal_mark[0])
            return
        try:
            if abs(self.s.game_speed() - self.speed) > 1e-3:
                self.s.set_game_speed(self.speed)
                self.speed_check = (time.monotonic(), state.time)
                return
        except BridgeError as e:
            log.info("can't change the game speed (%s); playing at normal speed", e)
            self.speed = 1.0
            return
        if self.speed_check and time.monotonic() - self.speed_check[0] > 10:
            real, game = time.monotonic() - self.speed_check[0], state.time - self.speed_check[1]
            if game > 0:
                log.info("game speed: %.1fx normal (asked for %gx)", game / real / self.normal_rate, self.speed)
            self.speed_check = None

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
        unfed = unfed_destinations(state, net)
        log.info("week %d day %d: score %d, %d/%d houses connected, %d/%d destinations fed, %d road tiles left%s",
                 state.week, state.day, state.score, served, len(state.houses),
                 sum(d.active for d in state.destinations.values()) - len(unfed),
                 sum(d.active for d in state.destinations.values()), state.available[Upgrade.CONCRETE],
                 f", OVERCROWDED: {', '.join(f'colour {d.group} ({d.waiting} pins)' for d in crowded)}"
                 if crowded else "")
        self.report_destinations(state, "yesterday")

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
        load = {p: sum(by_colour.values()) for p, by_colour in colours.items()}
        if self.place_junction_upgrades(state, load):
            return
        net = Network(state)
        roots = group_roots(state, net)
        # Networks holding a destination close to overcrowding: new houses of its colour join these.
        pressured = {net.find(state.carparks[d.carpark].tiles[0]) for d in self.pressured(state)
                     if d.carpark in state.carparks and state.carparks[d.carpark].tiles}
        refused = {move for move, until in self.refused.items() if until > state.time}
        board = Board(state, self.s.terrain, refused, load=load, colours=colours)
        budget = {"concrete": state.available[Upgrade.CONCRETE], "bridges": state.available[Upgrade.BRIDGE],
                  "tunnels": state.available[Upgrade.TUNNEL]}
        plans = []

        def take(plan):
            if plan is None or plan.new_tiles > budget["concrete"] or self.skip_until.get(plan.key, -1) >= state.time:
                return
            budget["concrete"] -= plan.new_tiles
            budget["bridges"] -= plan.crossings  # conservative: count any crossing against bridges
            board.claim(plan)
            plans.append(plan)

        # A destination with no houses of its colour on its network gets no cars and will overflow,
        # so those come first, busiest first.
        for dest in sorted(unfed_destinations(state, net), key=lambda d: -d.waiting):
            take(plan_destination(board, dest, net, budget["bridges"], budget["tunnels"]))
        # Then houses that can't reach their colour: colours with a destination under pressure first
        # (each connected house is more cars for it), then those with cars already waiting.
        short = {d.group for d in self.pressured(state)}
        unserved = [h for h in state.houses if not is_served(h, net, roots)]
        for h in unserved:
            # A shortcut that left its house cut off would be built and undone over and over.
            if state.time - self.shortcut_at.pop(h.pos, -SHORTCUT_TRUST) < SHORTCUT_TRUST:
                log.info("shortcut for house %s cut it off; no more shortcuts for it", h.pos)
                self.skip_until[("shortcut", h.pos)] = float("inf")
        for house in sorted(unserved, key=lambda h: (h.group not in short, -h.waiting)):
            take(plan_house(board, house, net, roots, budget["bridges"], budget["tunnels"], pressured))
        if not plans:
            self.optimise(state, refused, load)
            return

        # Plain roads go out in one batch. A road over water or through a mountain is drawn one
        # tile at a time (each segment must exist before the next validates), one per tick.
        plain = [plan for plan in plans if not self.crosses(plan)]
        crossing = [plan for plan in plans if self.crosses(plan)][:1]
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
                plan = plan_feeder(board, house, net, dest, state.available[Upgrade.BRIDGE],
                                   state.available[Upgrade.TUNNEL])
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

    def optimise(self, state: State, refused: set, load: dict) -> bool:
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
        board = Board(state, terrain, refused, load=load)
        if self.feed_short_destinations(state, board, pressured):
            return True
        for dest in pressured:
            if dest.en_route < BYPASS_MIN_EN_ROUTE or self.skip_until.get(("bypass", dest.key), -1) >= state.time:
                continue
            plan = plan_bypass(board, dest, state.available[Upgrade.BRIDGE], state.available[Upgrade.TUNNEL])
            if plan is None or plan.new_tiles > state.available[Upgrade.CONCRETE]:
                self.skip_until[("bypass", dest.key)] = state.time + GIVE_UP_FOR
                continue
            self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
            self.last_build_time = state.time
            return True
        graph = RoadGraph(state)
        trips, _ = graph.evaluate()
        spurs = sorted(((len(house_spur(state, terrain, h, graph)), i, h) for i, h in enumerate(state.houses)),
                       reverse=True)
        for length, _, house in spurs[:SHORTCUT_CANDIDATES]:
            if length == 0:
                break
            if self.skip_until.get(("shortcut", house.pos), -1) >= state.time:
                continue
            plan = plan_shortcut(state, terrain, house, refused, state.available[Upgrade.BRIDGE],
                                 state.available[Upgrade.TUNNEL], graph, trips)
            if plan is None or plan.new_tiles > state.available[Upgrade.CONCRETE]:
                continue
            self.shortcut_at[house.pos] = state.time
            self.record(state, plan, self.draw(state, plan.segments, sequential=self.crosses(plan)))
            self.last_build_time = state.time
            return True

        cut = [p for p in separation_cut(state, terrain) if self.skip_until.get(("clear", p), -1) < state.time]
        if cut:
            codes = self.s.clear_tiles(cut, sequential=True)
            cleared = [p for p, c in zip(cut, codes) if c == EDIT_SUCCESS]
            for p, c in zip(cut, codes):
                if c not in (EDIT_SUCCESS, EDIT_SKIPPED):
                    self.skip_until[("clear", p)] = state.time + GIVE_UP_FOR
            if cleared:
                log.info("cut %d road tile(s) at %s so no road carries more than two colours",
                         len(cleared), ", ".join(map(str, cleared)))
            self.last_build_time = state.time
            return bool(cleared)

        # While a destination is close to overcrowding, keep every road that makes any trip shorter.
        detour = 0 if pressured else MAX_DETOUR_LOOP
        tiles = [p for p in redundant_tiles(state, terrain, detour)
                 if self.skip_until.get(("clear", p), -1) < state.time]
        if time.monotonic() - started > 1.0:
            log.info("(optimising took %.1fs)", time.monotonic() - started)
        if not tiles:
            return False
        codes = self.s.clear_tiles(tiles, sequential=True)
        cleared = [p for p, c in zip(tiles, codes) if c == EDIT_SUCCESS]
        for p, c in zip(tiles, codes):
            if c not in (EDIT_SUCCESS, EDIT_SKIPPED):
                self.skip_until[("clear", p)] = state.time + GIVE_UP_FOR
        if cleared:
            log.info("removed %d redundant road tile(s) at %s", len(cleared), ", ".join(map(str, cleared)))
        self.last_build_time = state.time
        return bool(cleared)

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
        codes = self.s.clear_tiles(tiles, sequential=True)
        cleared = [p for p, c in zip(tiles, codes) if c == EDIT_SUCCESS]
        for p in cleared:
            # A road that dead-ends as soon as it is drawn didn't join where the plan expected: keep
            # its moves out of plans for a while instead of drawing and clearing it again and again.
            when, segments = self.built.pop(p, (None, []))
            if when is not None and state.time - when < BUILT_THEN_CLEARED:
                log.info("road just drawn at %s dead-ended; avoiding it for now", p)
                for segment in segments:
                    self.refused[segment] = state.time + REMEMBER_REFUSED_FOR
        for p, c in zip(tiles, codes):
            if c not in (EDIT_SUCCESS, EDIT_SKIPPED):
                self.skip_until[("clear", p)] = state.time + GIVE_UP_FOR
        if cleared:
            log.info("reclaimed %d dead-end road tile(s) at %s", len(cleared), ", ".join(map(str, cleared)))
        self.last_build_time = state.time
        return bool(cleared)

    # Upgrades ------------------------------------------------------------------------------

    def pick_upgrade(self, state: State):
        # One pick per offered choice: wait until the last one has been applied. A click while the
        # screen is still animating in is ignored by the game, so retry after a short wait.
        if self.picked_at is not None and state.pending_choices >= self.picked_pending and \
                time.monotonic() - self.picked_at < 3:
            return
        options = self.s.upgrade_choices()
        if not options:
            return
        worth = [self.values.get(o["type"], 0.0) * o["amount"] + o["concrete"] for o in options]
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

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
