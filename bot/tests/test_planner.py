"""Planner tests on small hand-built boards. Run: python3 -m unittest discover -s tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from game import Carpark, Content, Destination, House, State, Terrain, direction_between  # noqa: E402
from planner import (Board, Network, RoadGraph, dead_ends, group_roots, plan_house, plan_shortcut,  # noqa: E402
                     redundant_tiles)

W, S = 6, 4


class Map:
    """A blank driveable board to place buildings and roads on."""

    def __init__(self, size=16):
        self.tiles = {(x, y) for x in range(size) for y in range(size)}
        self.terrain = {p: Terrain(True, False, False, True) for p in self.tiles}
        self.roads, self.content = {}, {}
        self.houses, self.destinations, self.carparks = [], {}, {}

    def link(self, a, b):
        for p, q in ((a, b), (b, a)):
            self.roads.setdefault(p, [0] * 8)[direction_between(p, q)] = 8

    def road(self, *tiles):
        for a, b in zip(tiles, tiles[1:]):
            self.link(a, b)

    def house(self, pos, driveway, group=0):
        h = House(pos, group, driveway, 0)
        self.houses.append(h)
        self.content[pos] = Content.HOUSE
        self.roads.setdefault(pos, [0] * 8)[driveway] = 8  # the driveway stub
        return h

    def destination(self, tiles, park_tiles, entrance, entrance_dir, group=0):
        key = len(self.destinations) + 1
        for t in tiles:
            self.content[t] = Content.DESTINATION
        for t in park_tiles:
            self.content[t] = Content.CARPARK
        self.carparks[key] = Carpark(key, list(park_tiles), [(entrance, entrance_dir)])
        self.destinations[key] = Destination(key, group, True, 0, key, False, list(tiles))
        self.roads.setdefault(park_tiles[0], [0] * 8)[entrance_dir] = 8  # the car park's stub

    def state(self):
        return State(dict(self.roads), dict(self.content), list(self.houses), dict(self.destinations),
                     dict(self.carparks), [100, 0, 0, 0, 0, 0, 0, 0, 0], 0, 0, 0, 0, 0.0, 0.0, False, 0, False,
                     frozenset(self.tiles))


class RoadLengthTests(unittest.TestCase):
    def test_house_faces_an_adjacent_road_instead_of_building_one(self):
        m = Map()
        m.destination([(10, 10), (11, 10), (10, 11), (11, 11)], [(10, 9), (11, 9)], (9, 9), W)
        m.road((9, 9), (8, 9), (7, 9), (6, 9), (5, 9))
        m.link((9, 9), (10, 9))
        house = m.house((5, 10), 0)  # facing north, away from the road just below it
        st = m.state()
        net = Network(st)
        plan = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0)
        self.assertEqual(plan.new_tiles, 0)
        self.assertEqual(len(plan.segments), 1)  # just turn the house to face the road (S or SE)
        self.assertEqual(plan.segments[0][0], (5, 10))
        self.assertTrue(plan.turned_house)

    def _long_way_round(self, shared: bool):
        # Picture 5: the house reached the network the long way round.
        m = Map()
        m.destination([(10, 10), (11, 10), (10, 11), (11, 11)], [(10, 9), (11, 9)], (9, 9), W)
        m.road((9, 9), (8, 9), (7, 9), (6, 9), (5, 9), (4, 9), (3, 9))
        m.link((9, 9), (10, 9))
        house = m.house((5, 11), 0)
        m.road((5, 12), (4, 12), (3, 12), (2, 11), (2, 10), (3, 9))  # private road looping round to the west
        m.link((5, 11), (5, 12))
        if shared:  # another house uses the main road, so only the loop is private
            m.house((2, 9), 2)
            m.link((2, 9), (3, 9))
        st = m.state()
        graph = RoadGraph(st)
        trips, _ = graph.evaluate()
        return plan_shortcut(st, m.terrain, house, set(), 0, 0, graph, trips)

    def test_private_loop_replaced_by_turning_the_house(self):
        plan = self._long_way_round(shared=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.new_tiles, 0)  # turn to face (5, 10), one link down to the main road
        self.assertTrue(plan.turned_house)
        self.assertEqual(plan.saves, 5)  # the whole loop goes

    def test_unshared_road_rebuilt_shorter(self):
        plan = self._long_way_round(shared=False)
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan.new_tiles, 2)  # a short diagonal straight to the car park entrance
        self.assertGreaterEqual(plan.saves, 8)

    def test_second_road_into_a_car_park_is_redundant(self):
        # Picture 7: the car park joins the road below it twice, via a wide loop and a diagonal.
        m = Map()
        m.destination([(5, 6), (6, 6), (5, 7), (6, 7)], [(5, 5), (6, 5)], (4, 5), W)
        m.link((4, 5), (5, 5))
        m.road((4, 5), (3, 5), (3, 4), (4, 3), (5, 3), (6, 3))  # wide loop
        m.road((4, 5), (5, 4), (6, 3))  # tight connection
        m.road((6, 3), (7, 3), (8, 3))
        m.house((9, 3), W)
        m.link((9, 3), (8, 3))
        st = m.state()
        self.assertEqual(sorted(redundant_tiles(st, m.terrain)), [(3, 4), (3, 5), (4, 3), (5, 3)])

    def test_loop_kept_when_removing_it_lengthens_trips(self):
        m = Map()
        m.destination([(5, 6), (6, 6), (5, 7), (6, 7)], [(5, 5), (6, 5)], (4, 5), W)
        m.link((4, 5), (5, 5))
        m.road((4, 5), (4, 4), (4, 3), (5, 3), (6, 3), (7, 3), (8, 3), (8, 4), (8, 5))  # around the south
        m.road((4, 5), (4, 6), (4, 7), (4, 8), (5, 9), (6, 9), (7, 9), (8, 8), (8, 7), (8, 6), (8, 5))  # north
        m.house((9, 3), W, 0)
        m.link((9, 3), (8, 3))
        m.house((9, 9), W, 0)
        m.link((9, 9), (8, 9))
        m.link((8, 9), (8, 8))
        st = m.state()
        removed = redundant_tiles(st, m.terrain)
        # Each house keeps its short way round; at most the middle of the east side can go.
        for t in [(4, 4), (4, 3), (5, 3), (6, 3), (7, 3), (4, 6), (4, 7), (4, 8), (5, 9), (6, 9), (7, 9)]:
            self.assertNotIn(t, removed)

    def test_loop_kept_when_it_is_the_short_way_to_another_destination(self):
        m = Map()
        m.destination([(5, 6), (6, 6), (5, 7), (6, 7)], [(5, 5), (6, 5)], (4, 5), W)
        m.link((4, 5), (5, 5))
        m.destination([(12, 6), (13, 6), (12, 7), (13, 7)], [(12, 5), (13, 5)], (11, 5), W)
        m.link((11, 5), (12, 5))
        m.road((4, 5), (4, 4), (5, 3), (6, 3), (7, 3), (8, 3))  # house to the first destination
        m.road((8, 3), (9, 4), (10, 5), (11, 5))  # direct to the second
        m.road((8, 3), (8, 2), (9, 1), (10, 1), (11, 2), (11, 3), (11, 4), (11, 5))  # long way to the second
        m.house((8, 4), S)
        m.link((8, 4), (8, 3))
        removed = redundant_tiles(m.state(), m.terrain)
        for t in [(9, 4), (10, 5)]:
            self.assertNotIn(t, removed)  # the short way to the second destination survives
        self.assertIn((10, 1), removed)  # the long way round goes

    def test_dead_end_left_by_a_turned_house_is_reclaimed(self):
        m = Map()
        m.destination([(10, 10), (11, 10), (10, 11), (11, 11)], [(10, 9), (11, 9)], (9, 9), W)
        m.road((9, 9), (8, 9), (7, 9), (6, 9), (5, 9))
        m.link((9, 9), (10, 9))
        m.house((5, 10), S)
        m.link((5, 10), (5, 9))
        m.road((5, 11), (4, 11), (4, 12))  # the house used to face north onto this
        order = dead_ends(m.state(), m.terrain)
        self.assertEqual(sorted(order), [(4, 11), (4, 12), (5, 11)])
        self.assertNotEqual(order[0], (4, 11))  # cleared from a loose end, never the middle first


if __name__ == "__main__":
    unittest.main()
