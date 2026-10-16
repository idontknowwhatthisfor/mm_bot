"""Planner tests on small hand-built boards. Run: python3 -m unittest discover -s tests"""

import pathlib
import sys
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot import Bot  # noqa: E402
from game import (EDIT_ALREADY_EXISTS, EDIT_SUCCESS, Carpark, Content, Destination, House, State, Terrain,  # noqa: E402
                  E, N, S, Upgrade, W, direction_between, join_roundabouts, neighbor, opposite)
from planner import (Board, Network, Plan, RoadGraph, apply_plan, dead_ends, group_roots, is_served,  # noqa: E402
                     reclaimable_crossings,
                     junctions_by_load, plan_separation, separation_candidates,
                     mixed_tiles, plan_bypass, plan_destination, plan_feeder, plan_house, plan_shortcut, redundant_tiles, route_colours, separation_cut, trip_routes,
                     route_loads, unfed_destinations)

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

    def test_shortcut_keeps_off_a_busy_trunk(self):
        # The house's long private road joins a quiet road (row 5, then up column 9). A shortcut
        # onto the trunk along row 9 is shortest, but with the trunk busy it joins the quiet road.
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 1, -1)])
        m.road((9, 9), (9, 8), (9, 7), (9, 6), (9, 5), *[(x, 5) for x in range(8, 2, -1)])
        m.house((6, 4), N)
        m.link((6, 4), (6, 5))
        house = m.house((5, 7), W)
        m.road((4, 7), (3, 6), (3, 5))
        m.link((5, 7), (4, 7))
        st = m.state()
        graph = RoadGraph(st)
        trips, _ = graph.evaluate()
        trunk = {(x, 9) for x in range(2, 9)}
        quiet = plan_shortcut(st, m.terrain, house, set(), 0, 0, graph, trips)
        self.assertTrue(set(quiet.tiles) & trunk)  # unloaded, the trunk is the short way
        busy = plan_shortcut(st, m.terrain, house, set(), 0, 0, graph, trips, {t: 20 for t in trunk}, {})
        self.assertIsNotNone(busy)
        self.assertEqual(set(busy.tiles) & trunk, set())

    def test_house_is_joined_to_a_driveway_tile_that_already_has_road(self):
        # The house faces a tile whose road runs on to the network but doesn't point back at the
        # house. A plan running on from there must still join the house to that tile.
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((5, 9), (6, 9), (7, 9), (8, 9), (9, 9))
        house = m.house((3, 11), E)
        m.road((4, 11), (5, 10))  # starts at the house's driveway tile, not linked to the house
        st = m.state()
        net = Network(st)
        plan = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0)
        if not plan.turned_house:
            self.assertIn(((4, 11), W), plan.segments)
        after = apply_plan(st, plan)
        net = Network(after)
        self.assertTrue(is_served(next(h for h in after.houses if h.pos == (3, 11)), net, group_roots(after, net)))

    def test_plan_with_nothing_left_to_draw_is_left_alone(self):
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((7, 9), (8, 9), (9, 9))
        m.house((6, 9), E)  # a connected house at the end of the road, so it isn't a dead end
        m.link((6, 9), (7, 9))
        m.house((3, 11), E)
        st = m.state()
        session = FakeSession(m.terrain)
        bot = Bot(session)
        stale = Plan("house (3, 11)", ("house", (3, 11)), 0, [((7, 9), E), ((8, 9), E)], [(7, 9), (8, 9)], 0)
        import bot as bot_module
        real = bot_module.plan_house
        bot_module.plan_house = lambda *a, **k: stale
        try:
            bot.build(st)
        finally:
            bot_module.plan_house = real
        self.assertEqual(session.drawn, [])
        self.assertGreater(bot.skip_until[("house", (3, 11))], st.time)

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


    def _roundabout(self, diagonal_arms=()):
        """A roundabout at (5, 9) as the game leaves it on a + junction: the centre has no road, each
        ring tile keeps only its outward arm, and the ring itself isn't in the road state. Diagonal
        arms end on a corner tile pointing at the centre, as the game draws them."""
        m = Map()
        dest = m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        c = (5, 9)
        m.road((9, 9), (8, 9), (7, 9), (6, 9))  # east arm onto the east ring tile
        m.link((4, 9), (3, 9))  # west arm
        m.house((5, 11), S)  # north and south arms lead to houses
        m.link((5, 11), (5, 10))
        m.house((5, 7), N)
        m.link((5, 7), (5, 8))
        for corner in diagonal_arms:
            m.link(tuple(2 * corner[i] - c[i] for i in range(2)), corner)  # outer tile -> corner
            m.roads.setdefault(corner, [0] * 8)[direction_between(corner, c)] = 8  # corner -> centre
        m.house((3, 10), S)
        m.link((3, 10), (3, 9))
        return m, dest, c

    def _read(self, m, c):
        st = m.state()
        st.roundabouts.add(c)
        join_roundabouts(st.roads, st.roundabouts)
        return st

    def test_plus_roundabout_is_one_junction(self):
        m, dest, c = self._roundabout()
        raw = m.state()
        self.assertTrue(unfed_destinations(raw, Network(raw)))  # read raw, the west house is cut off
        st = self._read(m, c)
        self.assertEqual(st.links(c), 4)
        self.assertEqual(unfed_destinations(st, Network(st)), [])
        self.assertEqual(dead_ends(st, m.terrain), [])
        self.assertFalse(set(redundant_tiles(st, m.terrain)) & {c, (6, 9), (4, 9), (5, 10), (5, 8)})

    def test_diagonal_arms_into_the_centre_are_joined(self):
        corners = [(6, 10), (4, 10), (4, 8), (6, 8)]
        m, dest, c = self._roundabout(corners)
        st = self._read(m, c)
        self.assertEqual(st.links(c), 8)
        net = Network(st)
        self.assertEqual({net.find(t) for t in corners}, {net.find(c)})
        self.assertEqual([st.links(t) for t in corners], [2, 2, 2, 2])  # outer arm and centre

    def test_new_arm_is_drawn_into_the_centre_never_out_of_it(self):
        m, dest, c = self._roundabout()
        st = self._read(m, c)
        board = Board(st, m.terrain)
        self.assertTrue(board.move_allowed((6, 10), direction_between((6, 10), c)))  # corner -> centre
        self.assertFalse(board.move_allowed(c, direction_between(c, (6, 10))))  # centre -> unjoined corner
        self.assertFalse(board.move_allowed((5, 10), direction_between((5, 10), (6, 9))))  # ring -> ring

    def test_ring_tiles_get_no_junction_upgrade(self):
        m, dest, c = self._roundabout()
        m.link((6, 9), (7, 10))  # a second arm on the east ring tile makes it a 3-way tile
        st = self._read(m, c)
        self.assertEqual(st.links((6, 9)), 3)
        self.assertEqual(junctions_by_load(st, {(6, 9): 20, c: 20}, 1), [])

    def test_house_keeps_off_another_colours_road(self):
        # Colour 0's trunk runs along row 9; colour 1's road comes down column 5 and meets it. A new
        # colour 1 house above the trunk takes one new tile straight to its own colour's road.
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
        plan = plan_house(Board(st, m.terrain, colours=colours), house, net, group_roots(st, net), 0, 0)
        self.assertEqual(set(plan.tiles) & set(shared), set())
        self.assertEqual(plan.new_tiles, 1)


    def test_no_third_colour_on_a_road_anywhere_on_the_trip(self):
        # Two other colours already use row 9 west of the car park. A colour 2 house just above it
        # would join at (6, 9) and drive on along row 9; with the cap it builds its own way in.
        m = Map()
        m.destination(*DEST_EAST, group=2)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 1, -1)])
        house = m.house((6, 11), S, group=2)
        st = m.state()
        net = Network(st)
        shared = {(x, 9): {0: 3, 1: 3} for x in range(2, 9)}
        plain = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 0, 0)
        self.assertTrue(set(plain.tiles) & set(shared))
        plan = plan_house(Board(st, m.terrain, colours=shared), house, net, group_roots(st, net), 0, 0)
        self.assertEqual(set(plan.tiles) & set(shared), set())
        self.assertEqual(plan.tiles[-1], (9, 9))  # straight to the car park entrance


    def test_feeder_joins_a_house_network_to_a_destination_short_of_cars(self):
        m = Map()
        dest = m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road((9, 9), (8, 9), (7, 9))
        house = m.house((3, 12), S)  # on a stub of its own, away from the destination's network
        m.link((3, 12), (3, 11))
        st = m.state()
        net = Network(st)
        plan = plan_feeder(Board(st, m.terrain), house, net, dest, 0, 0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.tiles[0], (3, 11))  # drawn out of the house's driveway tile
        end = plan.tiles[-1]
        self.assertTrue(net.find(end) == net.find((10, 9)) or end == (9, 9))


    def _three_colours_on_row_9(self):
        """Colours 0 and 1 drive east along row 9 and north up column 8 to their car parks. Colour 2's
        house has a short spur onto row 9 at (4, 10) and a longer road of its own along row 12."""
        m = Map()
        m.destination(*DEST_EAST, group=2)
        m.link((9, 9), (10, 9))
        m.destination([(10, 3), (11, 3), (10, 4), (11, 4)], [(10, 5), (11, 5)], (9, 5), W, group=0)
        m.link((9, 5), (10, 5))
        m.destination([(10, 0), (11, 0), (10, 1), (11, 1)], [(10, 2), (11, 2)], (9, 2), W, group=1)
        m.link((9, 2), (10, 2))
        m.road(*[(x, 9) for x in range(2, 10)])
        m.road(*[(8, y) for y in range(9, 1, -1)], (9, 2))
        m.link((8, 5), (9, 5))
        m.house((2, 10), S, group=0)
        m.link((2, 10), (2, 9))
        m.house((3, 8), N, group=1)
        m.link((3, 8), (3, 9))
        m.house((3, 11), E, group=2)
        m.link((3, 11), (4, 11))
        m.road((4, 11), (4, 10), (4, 9))  # the short way onto the shared road
        m.road((4, 11), (4, 12), *[(x, 12) for x in range(5, 9)], (9, 11), (9, 10), (9, 9))  # its own road
        return m

    def test_cut_moves_a_third_colour_onto_its_own_road(self):
        m = self._three_colours_on_row_9()
        st = m.state()
        self.assertTrue(mixed_tiles(st))  # colour 2 takes the short way along row 9 with 0 and 1
        cut = separation_cut(st, m.terrain)
        self.assertEqual(cut[:1], [(4, 10)])
        self.assertNotIn((5, 12), redundant_tiles(st, m.terrain))  # its own road stays

    def test_colours_follow_the_drive_to_every_destination(self):
        # A colour 2 house with two destinations of its colour: the near one east, the far one west.
        # Its cars serve both, so it counts on the road west too, not only on the way east.
        m = Map()
        m.destination(*DEST_EAST, group=2)
        m.link((9, 9), (10, 9))
        m.destination([(0, 10), (1, 10), (0, 11), (1, 11)], [(1, 9), (0, 9)], (2, 9), E, group=2)
        m.link((2, 9), (1, 9))
        house = m.house((7, 12), S, group=2)
        m.road((7, 12), (7, 11), (8, 10), (9, 9))  # 2.8 tiles east
        m.road((7, 11), (6, 11), (5, 11), (4, 11), (3, 10), (2, 9))  # 5.8 tiles west
        st = m.state()
        west = [(6, 11), (5, 11), (4, 11), (3, 10)]
        nearest = route_colours(st, every_destination=False)
        every = route_colours(st)
        self.assertTrue(all(t not in nearest for t in west))
        self.assertTrue(all(every[t] == {2: 1} for t in west))
        # Two other colours on the road west make it count against joining this network.
        shared = {t: {0: 1, 1: 1} for t in west}
        _, crowded = trip_routes(st, 2, colours=shared)
        self.assertEqual(crowded[(7, 11)], len(west))
        self.assertEqual(house.access, (7, 11))

    def test_house_drives_over_its_own_network_to_avoid_a_shared_road(self):
        # Colour 2's network reaches its car park only along row 9, which colours 0 and 1 use. A
        # new colour 2 house can only leave onto that network (water all round it otherwise). It
        # must run on over its own network and build a road of its own to the car park, not just
        # join the network and drive along row 9 too.
        m = self._three_colours_on_row_9()
        for t in [(4, 12), *[(x, 12) for x in range(5, 9)], (9, 11), (9, 10)]:
            m.roads.pop(t, None)  # take colour 2's own road away
        m.roads[(4, 11)][direction_between((4, 11), (4, 12))] = 0
        m.roads[(9, 9)][direction_between((9, 9), (9, 10))] = 0
        m.road((4, 11), (3, 12), (2, 12))
        house = m.house((1, 13), direction_between((1, 13), (2, 12)), group=2)
        for t in [(0, 12), (0, 13), (0, 14), (1, 12), (1, 14), (2, 13), (2, 14)]:
            m.terrain[t] = WATER
        st = m.state()
        colours = route_colours(st)
        shared = {p for p, c in colours.items() if len({g for g in c if g != 2}) >= 2}
        self.assertTrue(shared)
        net = Network(st)
        plan = plan_house(Board(st, m.terrain, colours=colours), house, net, group_roots(st, net), 0, 0)
        self.assertIsNotNone(plan)
        self.assertEqual(set(plan.tiles) & shared, set())
        self.assertEqual(plan.tiles[-1], (9, 9))  # its own way into the car park

    def test_separate_road_then_cut_takes_a_colour_off_a_shared_road(self):
        # Colour 2 shares row 9 with colours 0 and 1 and has no road of its own. A separate road is
        # planned around row 9; once it is drawn, the cut moves colour 2 onto it and no road
        # carries three colours.
        m = self._three_colours_on_row_9()
        for t in [(4, 12), *[(x, 12) for x in range(5, 9)], (9, 11), (9, 10)]:
            m.roads.pop(t, None)
        m.roads[(4, 11)][direction_between((4, 11), (4, 12))] = 0
        m.roads[(9, 9)][direction_between((9, 9), (9, 10))] = 0
        st = m.state()
        self.assertTrue(mixed_tiles(st))
        self.assertEqual(separation_cut(st, m.terrain), [])  # nothing to cut yet: no other way round
        self.assertIn((3, 11), [h.pos for h in separation_candidates(st)])
        house = next(h for h in st.houses if h.pos == (3, 11))
        colours = route_colours(st)
        plan = plan_separation(Board(st, m.terrain, colours=colours), house, 0, 0)
        self.assertIsNotNone(plan)
        shared = {p for p, c in colours.items() if len({g for g in c if g != 2}) >= 2}
        self.assertEqual(set(plan.tiles) & shared, set())
        after = apply_plan(st, plan)
        cut = separation_cut(after, m.terrain)
        roads = {p: list(r) for p, r in after.roads.items()}
        for p in cut:
            roads[p] = [0] * 8
            for d in range(8):
                if neighbor(p, d) in roads:
                    roads[neighbor(p, d)][opposite(d)] = 0
        final = replace(after, roads=roads)
        self.assertEqual(mixed_tiles(final), set())
        _, fed = RoadGraph(final).evaluate()
        self.assertEqual(len(fed), 3)  # every destination still has its houses

    def test_refused_cut_does_not_hold_up_clearing_loops(self):
        # The game refuses the cut. Once it is backed off, the loop-clearing pass must run instead
        # of waiting for a cut that won't be made.
        m = self._three_colours_on_row_9()
        st = m.state()
        session = FakeSession(m.terrain)
        session.refuse = {(4, 10)}
        bot = Bot(session)
        for h in st.houses:
            bot.skip_until[("shortcut", h.pos)] = float("inf")
            bot.skip_until[("separate", h.pos)] = float("inf")
        bot.optimise(st, set(), {}, {})
        self.assertEqual(session.cleared, [[(4, 10)]])  # tried the cut
        st.time = 10.0
        bot.optimise(st, set(), {}, {})
        self.assertEqual(len(session.cleared), 2)
        self.assertIn((5, 12), session.cleared[1])  # then cleared the loop


WATER = Terrain(False, True, False, False)
MOUNTAIN = Terrain(False, False, True, False)


class FakeSession:
    """Records what the bot draws and clears; every edit succeeds."""

    def __init__(self, terrain):
        self.terrain = terrain
        self.drawn = []  # (segments, sequential) per add_roads call
        self.cleared = []  # tiles per clear_tiles call
        self.refuse = set()  # tiles the game won't clear

    def add_roads(self, segments, sequential=False):
        self.drawn.append((list(segments), sequential))
        return [EDIT_SUCCESS] * len(segments)

    def clear_tiles(self, tiles, sequential=False):
        self.cleared.append(list(tiles))
        return [8 if p in self.refuse else EDIT_SUCCESS for p in tiles]  # 8: CannotClearTile

    def add_junction_upgrade(self, kind, p):
        return EDIT_SUCCESS


class CrossingTests(unittest.TestCase):
    def test_route_needing_a_bridge_and_a_tunnel_uses_one_of_each(self):
        # A river at x=4 and a mountain ridge at x=8 run the full height of the board.
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        for y in range(16):
            m.terrain[(4, y)] = WATER
            m.terrain[(8, y)] = MOUNTAIN
        house = m.house((1, 9), E)
        st = m.state()
        net = Network(st)
        plan = plan_house(Board(st, m.terrain), house, net, group_roots(st, net), 1, 1)
        self.assertIsNotNone(plan)  # the two stocks are separate: one bridge doesn't use up the tunnel
        self.assertEqual((plan.bridges, plan.tunnels), (1, 1))

    def test_only_the_crossing_that_is_drawn_claims_tiles(self):
        # Two colours each need a bridge over the river at x=5..6. Only one crossing is drawn per
        # tick, so the second must not claim tiles that a third house would then join.
        m = Map()
        m.destination(*DEST_EAST, group=0)
        m.link((9, 9), (10, 9))
        m.destination([(10, 3), (11, 3), (10, 4), (11, 4)], [(10, 2), (11, 2)], (9, 2), W, group=1)
        m.link((9, 2), (10, 2))
        for y in range(16):
            for x in (5, 6):
                m.terrain[(x, y)] = WATER
        m.house((3, 9), E, group=0).waiting = 3
        m.house((3, 2), E, group=1).waiting = 2
        m.house((2, 4), E, group=1).waiting = 1  # next to where colour 1's bridge road would start
        st = m.state()
        st.available[Upgrade.BRIDGE] = 2
        session = FakeSession(m.terrain)
        bot = Bot(session)
        drawn = []
        bot.record = lambda state, plan, codes: drawn.append(plan)
        bot.build(st)
        self.assertEqual(sum(1 for p in drawn if p.crossings), 1)
        # Every house the bot drew a road for reaches its colour once everything drawn is built.
        roads = {p: list(r) for p, r in st.roads.items()}
        for plan in drawn:
            for p, d in plan.segments:
                q = neighbor(p, d)
                roads.setdefault(p, [0] * 8)[d] = 8
                roads.setdefault(q, [0] * 8)[opposite(d)] = 8
        after = replace(st, roads=roads)
        net = Network(after)
        roots = group_roots(after, net)
        for plan in drawn:
            if plan.key[0] == "house":
                house = next(h for h in after.houses if h.pos == plan.key[1])
                self.assertTrue(is_served(house, net, roots), f"{plan.label} joins a road that is never drawn")


class ScarceCrossingTests(unittest.TestCase):
    def _river(self, bridges):
        # The destination east of a river is fed by a house beside it; another house of its colour
        # sits west of the river with no road yet.
        m = Map()
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        for y in range(16):
            for x in (5, 6):
                m.terrain[(x, y)] = WATER
        m.house((8, 10), S)
        m.road((8, 10), (8, 9), (9, 9))
        m.house((3, 9), E)
        st = m.state()
        st.available[Upgrade.BRIDGE] = bridges
        session = FakeSession(m.terrain)
        Bot(session).build(st)
        return [p for segs, _ in session.drawn for p, _ in segs if p[0] < 5]  # anything west of the river

    def test_a_house_road_leaves_the_last_bridge_for_destinations(self):
        self.assertEqual(self._river(1), [])
        self.assertTrue(self._river(2))  # with a spare bridge the house is connected

    def test_first_bridge_beats_thirty_concrete_on_a_water_map(self):
        bot = Bot(FakeSession({}))
        bot.values[Upgrade.BRIDGE] = 8.0  # as on_new_game sets it on a map with water
        st = Map().state()
        bridge = {"type": Upgrade.BRIDGE, "amount": 1, "concrete": 20}
        concrete = {"type": Upgrade.CONCRETE, "amount": 30, "concrete": 0}
        for stock in (0, 1):  # below the reserve of two, the bridge wins
            st.available[Upgrade.BRIDGE] = stock
            self.assertGreater(bot.upgrade_worth(st, bridge), bot.upgrade_worth(st, concrete))
        st.available[Upgrade.BRIDGE] = 2
        self.assertLess(bot.upgrade_worth(st, bridge), bot.upgrade_worth(st, concrete))


class ReclaimCrossingTests(unittest.TestCase):
    """Two rivers, down columns 5-6 (A) and 14-15 (B). A colour 0 house west of A reaches its
    destination east of A over a bridge on row 9 (and, when `loop`, also over a second bridge on row
    12). A colour 1 destination west of B has its only house east of B, and no bridge is left."""

    def _map(self, loop=True):
        m = Map(size=20)
        for y in range(20):
            for x in (5, 6, 14, 15):
                m.terrain[(x, y)] = WATER
        m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.house((2, 9), E)
        m.road((2, 9), (3, 9), (4, 9), (5, 9), (6, 9), (7, 9), (8, 9), (9, 9))
        if loop:
            m.road((3, 9), (3, 10), (3, 11), (3, 12), (4, 12), (5, 12), (6, 12), (7, 12), (8, 12), (8, 11),
                   (8, 10), (8, 9))
        m.destination([(11, 3), (12, 3), (11, 4), (12, 4)], [(12, 2), (11, 2)], (13, 2), E, group=1)
        m.house((17, 3), W, group=1)
        st = m.state()
        st.available[Upgrade.BRIDGE] = 0
        return m, st

    def test_the_bridge_nobody_needs_comes_first(self):
        m, st = self._map()
        found = reclaimable_crossings(st, m.terrain)
        self.assertEqual([sorted(deck) for deck, *_ in found], [[(5, 12), (6, 12)], [(5, 9), (6, 9)]])
        self.assertEqual(found[0][3], 0.0)  # taking down the loop's bridge makes no trip longer
        self.assertGreater(found[1][3], 0.0)

    def test_stranded_destination_gets_a_bridge_back(self):
        m, st = self._map()
        session = FakeSession(m.terrain)
        Bot(session).build(st)
        self.assertEqual(session.cleared, [[(5, 12)]])  # one deck tile takes the whole bridge down
        self.assertEqual(session.drawn, [])  # nothing else is drawn while it comes down

    def test_the_only_bridge_a_house_uses_stays(self):
        m, st = self._map(loop=False)
        session = FakeSession(m.terrain)
        Bot(session).build(st)
        self.assertEqual(session.cleared, [])

    def test_no_second_bridge_taken_while_one_is_on_its_way_back(self):
        m, st = self._map()
        st.mothballed[Upgrade.BRIDGE] = 1
        session = FakeSession(m.terrain)
        Bot(session).build(st)
        decks = {(5, 9), (6, 9), (5, 12), (6, 12)}
        self.assertFalse([t for tiles in session.cleared for t in tiles if t in decks])  # (loop clearing may run)


class CrossingReserveTests(unittest.TestCase):
    def test_bypass_only_uses_bridges_beyond_the_reserve(self):
        m = Map()
        dest = m.destination(*DEST_EAST)
        m.link((9, 9), (10, 9))
        m.road(*[(x, 9) for x in range(9, 2, -1)])
        m.house((3, 10), S)
        m.link((3, 10), (3, 9))
        dest.unassigned, dest.en_route = 3, 3  # under pressure, with cars on the way
        given = []

        def record(board, d, bridges, tunnels):
            given.append(bridges)
            return None

        for stock in (2, 3):
            st = m.state()
            st.available[Upgrade.BRIDGE] = stock
            bot = Bot(FakeSession(m.terrain))
            with mock.patch("bot.plan_bypass", side_effect=record):
                bot.optimise(st, set(), {}, {})
        self.assertEqual(given, [0, 1])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip_replays_the_same_plans(self):
        import json
        from game import load_snapshot, snapshot
        m = TrafficTests()._three_colours_on_row_9()
        m.terrain[(0, 0)] = WATER
        st = m.state()
        st.roundabouts.add((5, 5))
        again, terrain = load_snapshot(json.loads(json.dumps(snapshot(st, m.terrain))))
        self.assertEqual(terrain, m.terrain)
        self.assertEqual(again.houses, st.houses)
        self.assertEqual(again.destinations, st.destinations)
        self.assertEqual(again.carparks, st.carparks)
        self.assertEqual({p: r for p, r in again.roads.items()}, {p: r for p, r in st.roads.items() if any(r)})
        self.assertEqual(route_colours(again), route_colours(st))
        self.assertEqual(separation_cut(again, terrain), separation_cut(st, m.terrain))


if __name__ == "__main__":
    unittest.main()
