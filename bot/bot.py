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

from game import EDIT_ALREADY_EXISTS, EDIT_RESULT_NAMES, EDIT_SUCCESS, GameMode, Session, State, Upgrade
from mmbridge import Bridge, BridgeError
from planner import (Board, Network, group_roots, is_served, plan_destination, plan_house, unfed_destinations,
                     unfinished_crossings)

log = logging.getLogger("bot")

# How much one unit of each upgrade is worth to this bot, in road tiles. It only draws plain
# roads, bridges and tunnels, so those dominate.
UPGRADE_VALUE = {
    Upgrade.CONCRETE: 1.0,
    Upgrade.BRIDGE: 0.5,  # raised to WATER_CROSSING_VALUE on maps with water
    Upgrade.TUNNEL: 0.5,  # raised on maps with mountains
    Upgrade.ROUNDABOUT: 1.5,
    Upgrade.MOTORWAY: 1.0,
    Upgrade.TRAFFIC_LIGHT: 0.3,
}
WATER_CROSSING_VALUE = 8.0
GIVE_UP_AFTER = 5  # failed attempts before a house is left alone for a while
GIVE_UP_FOR = 20.0  # sim seconds


class Bot:
    def __init__(self, session: Session, tick: float = 0.4):
        self.s = session
        self.tick = tick
        self.last_build_time: float | None = None
        self.failures: Counter = Counter()
        self.skip_until: dict = {}
        self.game_key = None
        self.last_report_day = None
        self.picked_at: float | None = None  # when the last upgrade was picked (monotonic)
        self.picked_pending = 0
        self.choice_seen_at: float | None = None

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
                if stop_at_game_end:
                    return state.score
                time.sleep(2.0)
                continue
            self.report(state)
            if state.pending_choices:
                self.pick_upgrade(state)
            else:
                self.picked_at = None
                # Only build once the simulation has stepped past our last edits, so nothing
                # is scheduled twice while the game is paused.
                if not state.paused and (self.last_build_time is None or state.time > self.last_build_time):
                    self.build(state)
            time.sleep(self.tick)

    def on_new_game(self, state: State):
        self.game_key = self.s.game_key
        self.last_build_time = None
        self.failures.clear()
        self.skip_until.clear()
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

    # Roads ---------------------------------------------------------------------------------

    def build(self, state: State):
        if self.repair_crossings(state):
            return  # re-read the map before planning on top of the repaired network
        net = Network(state)
        roots = group_roots(state, net)
        board = Board(state, self.s.terrain)
        budget = {"concrete": state.available[Upgrade.CONCRETE], "bridges": state.available[Upgrade.BRIDGE],
                  "tunnels": state.available[Upgrade.TUNNEL]}
        plans = []

        def take(plan):
            if plan is None or plan.new_tiles > budget["concrete"] or self.skip_until.get(plan.key, -1) >= state.time:
                return
            budget["concrete"] -= plan.new_tiles
            budget["bridges"] -= plan.crossings  # conservative: count any crossing against bridges
            for t in plan.tiles:
                board.planned[t] = plan.group
            plans.append(plan)

        # A destination with no houses of its colour on its network gets no cars and will overflow,
        # so those come first, busiest first.
        for dest in sorted(unfed_destinations(state, net), key=lambda d: -d.waiting):
            take(plan_destination(board, dest, net, budget["bridges"], budget["tunnels"]))
        # Then houses that can't reach their colour, those with cars already waiting first.
        unserved = [h for h in state.houses if not is_served(h, net, roots)]
        for house in sorted(unserved, key=lambda h: -h.waiting):
            take(plan_house(board, house, net, roots, budget["bridges"], budget["tunnels"]))
        if not plans:
            return

        # Plain roads go out in one batch. A road over water or through a mountain is drawn one
        # tile at a time (each segment must exist before the next validates), one per tick.
        def crosses(plan):
            return any(not self.s.terrain[t].driveable for t in plan.tiles if t in self.s.terrain)

        plain = [plan for plan in plans if not crosses(plan)]
        crossing = [plan for plan in plans if crosses(plan)][:1]
        results = []
        codes = self.s.add_roads([seg for plan in plain for seg in plan.segments])
        i = 0
        for plan in plain:
            results.append((plan, codes[i:i + len(plan.segments)]))
            i += len(plan.segments)
        for plan in crossing:
            results.append((plan, self.s.add_roads(plan.segments, sequential=True)))
        self.last_build_time = state.time
        for plan, mine in results:
            bad = [c for c in mine if c not in (EDIT_SUCCESS, EDIT_ALREADY_EXISTS)]
            if bad:
                self.failures[plan.key] += 1
                log.info("%s (colour %d): %d/%d segments refused (%s)", plan.label, plan.group, len(bad), len(mine),
                         ", ".join(sorted({EDIT_RESULT_NAMES.get(c, str(c)) for c in bad})))
                if self.failures[plan.key] >= GIVE_UP_AFTER:
                    self.skip_until[plan.key] = state.time + GIVE_UP_FOR
                    self.failures[plan.key] = 0
            else:
                self.failures.pop(plan.key, None)
                log.info("%s (colour %d): road of %d tiles%s", plan.label, plan.group, len(plan.tiles),
                         f", {plan.crossings} crossing(s)" if plan.crossings else "")

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
                log.info("completed crossing at %s toward %s", seg[0], "N NE E SE S SW W NW".split()[seg[1]])
            else:
                self.skip_until[("repair", seg)] = state.time + GIVE_UP_FOR
        self.last_build_time = state.time
        return built

    # Upgrades ------------------------------------------------------------------------------

    def pick_upgrade(self, state: State):
        # One pick per offered choice: wait until the last one has been applied.
        if self.picked_at is not None and state.pending_choices >= self.picked_pending and \
                time.monotonic() - self.picked_at < 10:
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
            Bot(session).run(stop_at_game_end=not args.keep_going)
        except KeyboardInterrupt:
            pass
        except BridgeError as e:
            raise SystemExit(f"bridge error: {e}")


if __name__ == "__main__":
    main()
