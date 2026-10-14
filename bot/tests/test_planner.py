"""Planner tests on small hand-built boards. Run: python3 -m unittest discover -s tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot import Bot  # noqa: E402
from game import (EDIT_ALREADY_EXISTS, EDIT_SUCCESS, Carpark, Content, Destination, House, State, Terrain,  # noqa: E402
                  direction_between, join_roundabouts)
from planner import (Board, Network, RoadGraph, dead_ends, group_roots, junctions_by_load,  # noqa: E402
                     plan_bypass, plan_destination, plan_house, plan_shortcut, redundant_tiles, route_colours,
                     route_loads, unfed_destinations)

N, E, S, W = 0, 2, 4, 6


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
        return self.destinations[key]

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



DEST_EAST = ([(10, 10), (11, 10), (10, 11), (11, 11)], [(10, 9), (11, 9)], (9, 9), W)


class TrafficTests(unittest.TestCase):
    def test_route_loads_count_houses_downstream(self):
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 2, -1)])
        m.house((3, 10), S)
        m.link((3, 10), (3, 9))
        m.house((6, 10), S)
        m.link((6, 10), (6, 9))
        m.road((6, 9), (6, 8))  # a side road nobody uses
        st = m.state()
        load = route_loads(st)
        self.assertEqual(load[(4, 9)], 1)
        self.assertEqual(load[(7, 9)], 2)
        self.assertEqual(load[(9, 9)], 2)
        self.assertNotIn((6, 8), load)
        self.assertEqual(junctions_by_load(st, load, 2), [(6, 9)])  # house, side road and trunk meet there
        st.roundabouts.add((6, 9))
        self.assertEqual(junctions_by_load(st, load, 2), [])

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

    def test_house_joins_nearest_network_normally(self):
        st, m, house, net, roots = self._two_networks()
        plan = plan_house(Board(st, m.terrain), house, net, roots, 0, 0)
        self.assertEqual(net.find(plan.tiles[-1]), net.find((9, 9)))
        self.assertEqual(plan.new_tiles, 1)

    def test_house_prefers_network_of_destination_under_pressure(self):
        st, m, house, net, roots = self._two_networks()
        plan = plan_house(Board(st, m.terrain), house, net, roots, 0, 0, pressured={net.find((3, 9))})
        self.assertEqual(net.find(plan.tiles[-1]), net.find((3, 9)))
        self.assertEqual(plan.new_tiles, 2)

    def test_house_avoids_joining_a_busy_trunk(self):
        st, m, house, net, roots = self._two_networks()
        busy = {(x, 9): 20 for x in range(6, 10)}
        plan = plan_house(Board(st, m.terrain, load=busy), house, net, roots, 0, 0)
        self.assertEqual(net.find(plan.tiles[-1]), net.find((3, 9)))  # one extra tile beats 14 extra houses

    def test_bypass_for_a_destination_reached_the_long_way_round(self):
        m = Map()
        dest = m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 1, -1)], *[(2, y) for y in range(10, 15)],
               *[(x, 15) for x in range(3, 9)])
        m.house((8, 14), N)
        m.link((8, 14), (8, 15))
        st = m.state()
        plan = plan_bypass(Board(st, m.terrain), dest, 0, 0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.tiles[0], (9, 9))
        self.assertLessEqual(plan.new_tiles, 5)  # up the east side instead of 19 tiles round the west
        self.assertEqual(plan.tiles[-1], (8, 15))  # where it saves the most trip

    def test_no_bypass_to_road_no_house_drives_on(self):
        # Same long loop, but the only house sits by the car park: a bypass would shorten nobody's
        # trip, and the loop-clearing pass would just remove it again.
        m = Map()
        dest = m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 1, -1)], *[(2, y) for y in range(10, 15)],
               *[(x, 15) for x in range(3, 9)])
        m.house((8, 10), S)
        m.link((8, 10), (8, 9))
        self.assertIsNone(plan_bypass(Board(m.state(), m.terrain), dest, 0, 0))

    def test_entering_water_needs_a_bridge_unless_the_deck_starts_there(self):
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        for y in range(16):
            for x in (5, 6, 7):
                m.terrain[(x, y)] = Terrain(False, True, False, False)  # a river three tiles wide
        m.road((4, 9), (3, 9))
        house = m.house((3, 10), S)
        m.link((3, 10), (3, 9))
        # A half-built deck: only the far water tile has road, so entering from the west needs stock.
        m.road((7, 9), (8, 9), (9, 9))
        st = m.state()
        net = Network(st)
        self.assertIsNone(plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0))
        plan = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 1, 0)
        self.assertEqual(plan.crossings, 1)

    def test_crossing_an_existing_bridge_sends_only_the_new_segments(self):
        # The game refuses a segment it already has when it lands on a bridge deck (CannotCreateBridge),
        # so a road that runs over an existing bridge must leave the bridge's segments alone.
        m = Map()
        dest = m.destination(*DEST_EAST)
        for y in range(16):
            for x in (5, 6, 7):
                m.terrain[(x, y)] = Terrain(False, True, False, False)
        m.road(*[(x, 9) for x in range(4, 9)])  # another network's bridge, (5, 9) to (7, 9)
        m.house((2, 9), E)
        st = m.state()
        plan = plan_destination(Board(st, m.terrain), dest, Network(st), 0, 0)
        self.assertIn(((6, 9), W), plan.segments)  # the road runs over the bridge
        sent = []

        class FakeSession:
            def add_roads(self, segments, sequential=False):
                sent.extend(segments)
                return [EDIT_SUCCESS] * len(segments)

        codes = Bot(FakeSession()).draw(st, plan.segments, sequential=True)
        self.assertTrue(sent)
        self.assertEqual([seg for seg in sent if st.joined(*seg)], [])
        self.assertEqual(codes.count(EDIT_ALREADY_EXISTS), len(plan.segments) - len(sent))
        self.assertEqual(codes.count(EDIT_SUCCESS), len(sent))


    def test_roundabout_centre_joins_the_roads_into_it(self):
        # The game keeps a roundabout's connections out of its centre tile's two-lane road state, so
        # the centre reads as roadless. Without joining it back, the car park beyond it looked cut off
        # (fed again every tick) and the roads into it looked like dead ends (cleared every tick).
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((9, 9), (8, 9), (7, 9), (6, 9))
        m.house((6, 10), S)
        m.link((6, 10), (6, 9))
        st = m.state()
        st.roads[(8, 9)] = [0] * 8  # the roundabout centre as the game reports it
        st.roundabouts.add((8, 9))
        self.assertTrue(unfed_destinations(st, Network(st)))
        self.assertTrue(dead_ends(st, m.terrain))
        join_roundabouts(st.roads, st.roundabouts)
        self.assertEqual(st.links((8, 9)), 2)
        self.assertEqual(unfed_destinations(st, Network(st)), [])
        self.assertEqual(dead_ends(st, m.terrain), [])
        self.assertNotIn((8, 9), redundant_tiles(st, m.terrain))

    def test_new_roads_use_only_a_roundabouts_existing_exits(self):
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((9, 9), (8, 9), (7, 9))
        house = m.house((8, 6), S)
        st = m.state()
        st.roundabouts.add((8, 9))  # joined east and west only
        net = Network(st)
        plan = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0)
        self.assertNotIn((8, 8), plan.tiles[-1:])  # never ends on the roundabout's free north side
        self.assertNotEqual(plan.tiles[-1], (8, 9))


    def test_house_keeps_off_another_colours_road(self):
        # Colour 0's trunk runs along row 9; colour 1's road comes down column 5 and meets it. A new
        # colour 1 house just above the trunk would ride colour 0's road for free, but one new tile
        # takes it straight to its own colour's road instead.
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 1, -1)])
        m.house((2, 10), N)
        m.link((2, 10), (2, 9))
        m.destination([(4, 1), (5, 1), (4, 2), (5, 2)], [(4, 3), (5, 3)], (5, 4), N, group=1)
        m.road(*[(5, y) for y in range(4, 10)])
        house = m.house((8, 7), N, group=1)
        st = m.state()
        net = Network(st)
        colours = route_colours(st)
        shared = [p for p, by_colour in colours.items() if 0 in by_colour]
        plain = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0)
        self.assertTrue(set(plain.tiles) & set(shared))  # without the colour cost it rides colour 0's road
        plan = plan_house(Board(st, m.terrain, colours=colours), house, net, group_roots(st, net), 0, 0)
        self.assertEqual(set(plan.tiles) & set(shared), set())
        self.assertEqual(plan.new_tiles, 1)


if __name__ == "__main__":
    unittest.main()
