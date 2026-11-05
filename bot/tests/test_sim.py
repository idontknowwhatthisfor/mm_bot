"""Simulator tests: game rules on a hand-placed world, and the bot playing it.
Run: python3 -m unittest discover -s tests"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from game import E, N, NE, S, W, CarState, Content, Terrain, Upgrade  # noqa: E402
from sim import world as sw  # noqa: E402
from sim.run import play  # noqa: E402
from sim.session import SimSession  # noqa: E402
from sim.world import MapStyle, World  # noqa: E402


def blank(water=()):
    """A 20x12 world, all inside the city limits, with no buildings; `water` tiles are river."""
    w = World(0, MapStyle(width=20, height=12, rivers=0, start=(20, 12)))
    w.houses.clear(), w.destinations.clear(), w.cars.clear(), w.content.clear(), w.roads.clear()
    for p in water:
        w.terrain[p] = Terrain(False, True, False, False)
    w._dest_clock = w._house_clock = 1e9  # nothing spawns on its own
    w.available[Upgrade.CONCRETE] = 50
    return w


def place_destination(w, x, y, group=0):
    """Building at (x..x+1, y..y+1), car park below it, entrance west of (x, y-1)."""
    d = sw.Destination(w._next_key, group, [(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)],
                       [(x, y - 1), (x + 1, y - 1)], (x - 1, y - 1), W, pin_clock=1e9)
    w._next_key += 1
    for t in d.tiles:
        w.content[t] = Content.DESTINATION
    for t in d.park:
        w.content[t] = Content.CARPARK
    w.roads[d.park[0]] = [0] * 8
    w.roads[d.park[0]][W] = sw.ACTIVE
    w.destinations[d.key] = d
    return d


def place_house(w, pos, driveway, group=0):
    h = sw.House(pos, group, driveway)
    w.houses.append(h)
    w.content[pos] = Content.HOUSE
    w.roads[pos] = [0] * 8
    w.roads[pos][driveway] = sw.ACTIVE
    for _ in range(2):
        w.cars.append(sw.SimCar(w._next_car, h, at=pos))
        w._next_car += 1
    return h


def draw(w, *tiles):
    from game import direction_between
    return [w.add_road(a, direction_between(a, b)) for a, b in zip(tiles, tiles[1:])]


class RoadTests(unittest.TestCase):
    def test_segments_join_both_sides_and_cost_a_tile_each(self):
        w = blank()
        self.assertEqual(draw(w, (2, 2), (3, 2), (4, 2)), [sw.OK, sw.OK])
        self.assertTrue(w.joined((3, 2), E) and w.joined((3, 2), W))
        self.assertEqual(w.available[Upgrade.CONCRETE], 47)
        self.assertEqual(draw(w, (2, 2), (3, 2)), [sw.EXISTS])

    def test_house_driveway_tile_is_free_and_joins_the_house(self):
        w = blank()
        h = place_house(w, (5, 5), E)
        self.assertEqual(draw(w, (6, 5), (7, 5)), [sw.OK])
        self.assertEqual(w.available[Upgrade.CONCRETE], 49)  # only (7, 5) cost a tile
        self.assertTrue(w.joined(h.pos, E))

    def test_drawing_out_of_a_house_turns_it(self):
        w = blank()
        h = place_house(w, (5, 5), E)
        self.assertEqual(w.add_road((5, 5), N), sw.OK)
        self.assertEqual(h.driveway, N)
        self.assertEqual(w.roads[(5, 5)], [8, 0, 0, 0, 0, 0, 0, 0])

    def test_car_parks_only_join_through_their_entrance(self):
        w = blank()
        d = place_destination(w, 10, 5)
        self.assertEqual(w.add_road((10, 3), N), sw.NO_CARPARK)  # from below the car park
        draw(w, (8, 4), (9, 4))
        self.assertTrue(w.joined(d.entrance, E))  # the entrance joins the car park by itself

    def test_bridge_is_taken_from_stock_and_comes_back_when_cleared(self):
        w = blank(water=[(5, y) for y in range(12)])
        w.available[Upgrade.BRIDGE] = 0
        self.assertEqual(draw(w, (4, 3), (5, 3)), [sw.NO_BRIDGE])
        w.available[Upgrade.BRIDGE] = 1
        self.assertEqual(draw(w, (3, 3), (4, 3), (5, 3), (6, 3), (7, 3)), [sw.OK] * 4)
        self.assertEqual(w.available[Upgrade.BRIDGE], 0)
        self.assertEqual(w.add_road((4, 4), NE), sw.CROSSING)  # no diagonal onto the river
        self.assertEqual(w.clear_tile((5, 3)), sw.OK)
        self.assertEqual(w.mothballed[Upgrade.BRIDGE], 1)
        w.advance(sw.MOTHBALL_TIME + 0.2)
        self.assertEqual((w.available[Upgrade.BRIDGE], w.mothballed[Upgrade.BRIDGE]), (1, 0))


class TrafficTests(unittest.TestCase):
    def _street(self):
        w = blank()
        d = place_destination(w, 12, 5)
        h = place_house(w, (3, 4), E)
        draw(w, *[(x, 4) for x in range(4, 12)])
        return w, d, h

    def test_a_car_serves_a_pin_and_comes_home(self):
        w, d, h = self._street()
        d.unassigned = 1
        w.advance(1.0)
        self.assertEqual(d.en_route, 1)
        w.advance(30.0)
        self.assertEqual((w.score, d.served, d.unassigned, d.en_route), (1, 1, 0, 0))
        self.assertTrue(all(c.state == CarState.WAITING for c in w.cars))

    def test_a_full_junction_holds_cars_back(self):
        w, d, h = self._street()
        for x in (6, 7):
            w.add_road((x, 4), S)  # side roads make (6, 4) and (7, 4) junctions
        self.assertEqual(w.capacity((6, 4)), sw.CAPACITY_JUNCTION)
        w.available[Upgrade.ROUNDABOUT] = 1
        self.assertEqual(w.add_junction_upgrade(Upgrade.ROUNDABOUT, (6, 4)), sw.OK)
        self.assertEqual(w.capacity((6, 4)), sw.CAPACITY_UPGRADED)

    def test_too_many_pins_for_too_long_ends_the_game(self):
        w, d, h = self._street()
        w.clear_tile((8, 4))  # the house can't reach it any more
        d.unassigned = sw.PIN_LIMIT + 1
        w.advance(sw.MAX_OVERCROWD + 1)
        self.assertTrue(w.ended)


class ClockTests(unittest.TestCase):
    def test_week_end_offers_an_upgrade_and_pauses(self):
        w = blank()
        w.advance(sw.WEEK + 1)
        self.assertTrue(w.paused)
        self.assertEqual(len(w.choices[0]), 2)
        stock = sum(w.available)
        w.choose(0)
        self.assertFalse(w.paused)
        self.assertGreater(sum(w.available), stock)

    def test_same_seed_same_game(self):
        a, b = World(4), World(4)
        for w in (a, b):
            w.advance(60)
        self.assertEqual(SimSession(a).read().content, SimSession(b).read().content)
        self.assertEqual([d.unassigned for d in a.destinations.values()],
                         [d.unassigned for d in b.destinations.values()])


class BotOnSimTests(unittest.TestCase):
    def test_the_bot_plays_two_weeks(self):
        r = play(seed=1, style="river", max_weeks=2)
        self.assertGreater(r["score"], 0)
        self.assertEqual(r["unreached"], 0)


if __name__ == "__main__":
    unittest.main()
