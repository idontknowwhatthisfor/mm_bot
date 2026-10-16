"""Traffic tests: jams and the route check, on fake car data and on a snapshot from a real game.
Run: python3 -m unittest discover -s tests"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import planner  # noqa: E402
from bot import Bot  # noqa: E402
from game import E, N, S, W, Car, CarState, Obstacle, Traffic, load_snapshot, load_traffic  # noqa: E402
from planner import Board, Network, group_roots, plan_house  # noqa: E402
from test_planner import DEST_EAST, FakeSession, Map  # noqa: E402
from traffic import JAM_LEVEL, JamTracker, check_routes, queued, stopped_cars  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parent / "data"
THRESHOLD = 0.001  # RoadChunkModel.CarStoppedSpeedThreshold as read from the game


def car(cid, tile, *, state=CarState.DRIVING_TO_DESTINATION, speed=0.0, obstacle=Obstacle.LEADING_VEHICLE,
        house=(3, 10), dest=1, path=(), path_length=0.0, in_carpark=False):
    return Car(cid, house, 0, state, dest, tile, in_carpark, speed, obstacle, None, list(path), path_length)


def queue(t, tile, n, **kw):
    return Traffic(t, [car(i, tile, **kw) for i in range(n)], THRESHOLD)


class QueueTests(unittest.TestCase):
    def test_only_driving_cars_stopped_behind_something_count(self):
        cars = [car(1, (5, 9)),  # stopped behind the car ahead
                car(2, (5, 9), obstacle=Obstacle.BLOCKING_INTERSECTION),  # waiting at a junction
                car(3, (5, 9), speed=1.2),  # following at speed
                car(4, (5, 9), obstacle=Obstacle.TARGET),  # arriving
                car(5, (5, 9), state=CarState.WAITING),  # parked at home
                car(6, None, in_carpark=True)]  # in a car park
        self.assertEqual([queued(c, THRESHOLD) for c in cars], [True, True, False, False, False, False])
        self.assertEqual(stopped_cars(Traffic(0.0, cars, THRESHOLD)), {(5, 9): 2})


class JamTrackerTests(unittest.TestCase):
    def test_one_red_light_is_not_a_jam(self):
        jams = JamTracker()
        peak = 0.0
        for t in range(0, 31):
            jams.update(queue(float(t), (5, 9), 2 if t < 3 else 0))  # two cars wait 3 s, then drive on
            peak = max(peak, jams.level.get((5, 9), 0.0))
        self.assertLess(peak, JAM_LEVEL)
        self.assertEqual(jams.jammed(), {})

    def test_cars_stuck_for_a_while_are_a_jam(self):
        jams = JamTracker()
        for t in range(0, 31):
            jams.update(queue(float(t), (5, 9), 3))
        self.assertIn((5, 9), jams.jammed())
        self.assertGreater(jams.level[(5, 9)], 2.5)

    def test_reads_while_paused_change_nothing(self):
        jams = JamTracker()
        for t in range(0, 6):
            jams.update(queue(float(t), (5, 9), 3))
        before = dict(jams.level)
        for _ in range(5):
            jams.update(queue(5.0, (5, 9), 0))  # the clock stands still
        self.assertEqual(jams.level, before)


class JamCostTests(unittest.TestCase):
    def _two_networks(self):
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((9, 9), (8, 9), (7, 9), (6, 9))
        m.destination([(2, 10), (3, 10), (2, 11), (3, 11)], [(3, 9), (2, 9)], (4, 9), E)
        m.link((4, 9), (3, 9))
        m.road((4, 9), (4, 8), (4, 7))
        house = m.house((8, 6), N)
        st = m.state()
        net = Network(st)
        return st, m, house, net, group_roots(st, net)

    def test_house_keeps_off_a_jammed_trunk(self):
        st, m, house, net, roots = self._two_networks()
        jam = {(x, 9): 4.0 for x in range(6, 10)}  # four cars stuck on each tile
        plan = plan_house(Board(st, m.terrain, jam=jam), house, net, roots, 0, 0)
        self.assertEqual(net.find(plan.tiles[-1]), net.find((3, 9)))  # one extra tile beats the queue

    def test_jam_cost_can_be_switched_off(self):
        st, m, house, net, roots = self._two_networks()
        jam = {(x, 9): 4.0 for x in range(6, 10)}
        old = planner.USE_JAM_COST
        try:
            planner.USE_JAM_COST = False
            plan = plan_house(Board(st, m.terrain, jam=jam), house, net, roots, 0, 0)
        finally:
            planner.USE_JAM_COST = old
        self.assertEqual(net.find(plan.tiles[-1]), net.find((9, 9)))  # as if there were no jam

    def test_a_short_wait_costs_nothing(self):
        st, m, house, net, roots = self._two_networks()
        board = Board(st, m.terrain, jam={(7, 9): planner.JAM_FREE})
        self.assertEqual(board.jam_cost((7, 9)), 0.0)


class RouteCheckTests(unittest.TestCase):
    def _street(self):
        """A house at (3, 10) whose only road runs east along row 9 into a car park at (10, 9), with a
        parallel side road along row 8 between (5, 9) and (8, 9)."""
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(3, 10)])
        m.house((3, 10), S)
        m.link((3, 10), (3, 9))
        m.road((5, 9), (5, 8), (6, 8), (7, 8), (8, 8), (8, 9))
        return m.state()

    def check(self, *cars):
        return [r.kind for r in check_routes(self._street(), Traffic(0.0, list(cars), THRESHOLD))]

    def test_car_on_the_planned_route_matches(self):
        self.assertEqual(self.check(car(1, (5, 9), speed=1, path=[(6, 9), (7, 9), (8, 9), (9, 9)], path_length=8.0)),
                         ["match"])

    def test_tiles_missing_from_the_game_path_are_fine(self):
        # Lanes on car park tiles and on some diagonal corners map to no tile.
        self.assertEqual(self.check(car(1, (5, 9), speed=1, path=[(7, 9), (9, 9)], path_length=8.0)), ["match"])

    def test_car_on_another_road_is_a_mismatch(self):
        kinds = self.check(car(1, (5, 9), speed=1, path=[(5, 8), (6, 8), (7, 8), (8, 8), (8, 9), (9, 9)],
                               path_length=14.0),
                           car(2, (6, 8), speed=1, path=[(7, 8), (8, 8), (8, 9), (9, 9)], path_length=9.0))
        self.assertEqual(kinds, ["different", "off route"])

    def test_car_driving_home_follows_the_route_back(self):
        home = car(1, (8, 9), state=CarState.DRIVING_HOME, speed=1,
                   path=[(7, 9), (6, 9), (5, 9), (4, 9), (3, 9), (3, 10)], path_length=12.0)
        self.assertEqual(self.check(home), ["match"])


class SnapshotRouteTests(unittest.TestCase):
    """A Tokyo game in week 9 where colour 4's cars drive through a car park with two entrances,
    shared by a colour 2 and a colour 3 destination, from (-2, 5) to (3, 5)."""

    def _results(self, drive_through):
        data = json.loads((DATA / "snapshot-tokyo-week9-drive-through.json").read_text())
        state, _ = load_snapshot(data)
        old = planner.DRIVE_THROUGH_CARPARKS
        try:
            planner.DRIVE_THROUGH_CARPARKS = drive_through
            return {r.car.id: r for r in check_routes(state, load_traffic(data))}
        finally:
            planner.DRIVE_THROUGH_CARPARKS = old

    def test_cars_through_a_two_entrance_car_park_match(self):
        results = self._results(True)
        for cid in (115, 125):
            self.assertEqual(results[cid].kind, "match")
        share = sum(r.kind == "match" for r in results.values()) / len(results)
        self.assertGreaterEqual(share, 0.95)

    def test_modelled_as_a_dead_end_those_cars_disagree(self):
        results = self._results(False)
        for cid in (115, 125):
            self.assertNotEqual(results[cid].kind, "match")


class FakeTrafficSession(FakeSession):
    """FakeSession that also hands out prepared car reads."""

    def __init__(self, terrain, reads):
        super().__init__(terrain)
        self.reads = list(reads)
        self.calls = 0

    def read_cars(self):
        self.calls += 1
        return self.reads.pop(0) if self.reads else None


class BotTrafficTests(unittest.TestCase):
    def test_cars_are_read_once_a_game_second_and_feed_the_jam_costs(self):
        m = Map()
        st = m.state()
        reads = [queue(float(t), (5, 9), 3) for t in range(0, 20)]
        bot = Bot(FakeTrafficSession(m.terrain, reads))
        for tick in range(0, 80):  # four looks per game second
            st.time = tick / 4
            bot.watch_traffic(st)
        self.assertEqual(bot.s.calls, 20)
        self.assertGreater(bot.jam_levels().get((5, 9), 0.0), 1.0)
        old = planner.USE_JAM_COST
        try:
            planner.USE_JAM_COST = False
            self.assertEqual(bot.jam_levels(), {})
        finally:
            planner.USE_JAM_COST = old


if __name__ == "__main__":
    unittest.main()
