"""A small Mini Motorways simulation for running and testing bots offline.

It keeps the parts of the game the bot plays against:

- a grid with rivers (and optionally mountains) and city limits that grow every week;
- coloured destinations (2x2 building, 2-tile car park, one entrance) and houses with a driveway;
- roads drawn as segments between neighbouring tiles in 8 directions, joined only when both sides
  point at each other, paid for in road tiles; straight crossings over water or under a mountain
  take a bridge or tunnel; diagonals may not cross at a corner;
- pins that appear at destinations, cars sent from idle houses of the same colour, driving the
  shortest road there and back, queueing when a tile is full, parking, and scoring one point
  per pin served;
- the overcrowd timer that ends the game, weekly upgrade choices, roundabouts and traffic lights
  (more room at a junction), and bridges taken down coming back to stock once mothballed.

It is a simplification, not a copy: tile capacity stands in for lanes, and all the numbers are
tuned so a reasonable bot lasts several weeks, not taken from the game.

Coordinates, directions (TileDirection, +y north) and road states (8 = active) match game.py.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field

from game import (CARDINALS, DIRS, E, W, CarState, Content, Obstacle, Pos, Terrain, Upgrade,
                  direction_between, neighbor, opposite)

ACTIVE = 8

# Result codes, as the game's TileEditResultCode (see game.EDIT_RESULT_NAMES).
OK, INVALID, NO_CARPARK, NO_UPGRADES, NO_CONCRETE, CANT_CLEAR = 0, 2, 3, 5, 6, 8
EXISTS, NO_BRIDGE, NO_TUNNEL, NO_ROADS, CROSSING = 13, 14, 15, 16, 19

# Time
DAY = 20.0  # game seconds per day
WEEK = 7 * DAY
SUBSTEP = 0.1

# Cars
CAR_SPEED = 1.0  # tiles per game second on a straight
TURN_SPEED = 0.6  # through a bend of 90 degrees or more
PARK_TIME = 1.5
CARS_PER_HOUSE = 2
DEADLOCK_TIMEOUT = 5.0  # a car stuck this long squeezes into the full tile anyway
DISPATCH_EVERY = 0.5
# Cars on one tile at once: a plain road, a junction (3+ roads), a junction with a roundabout or lights.
CAPACITY_ROAD, CAPACITY_JUNCTION, CAPACITY_UPGRADED = 3, 2, 4

# Destinations
PIN_LIMIT = 6
MAX_OVERCROWD = 50.0
MAX_COLOURS = 6

# Stock and upgrades
START_CONCRETE = 30
MOTHBALL_TIME = 3.0


def drive_length(a: Pos, b: Pos) -> float:
    return math.sqrt(2) if a[0] != b[0] and a[1] != b[1] else 1.0


@dataclass
class House:
    pos: Pos
    group: int
    driveway: int

    @property
    def access(self) -> Pos:
        return neighbor(self.pos, self.driveway)


@dataclass
class Destination:
    key: int
    group: int
    tiles: list[Pos]
    park: list[Pos]  # car park tiles; park[0] holds the entrance stub
    entrance: Pos  # the road tile outside the car park
    entrance_dir: int  # direction from park[0] to the entrance
    unassigned: int = 0
    en_route: int = 0
    served: int = 0
    overcrowd_time: float = 0.0
    pin_clock: float = 0.0  # seconds until the next pin


@dataclass
class SimCar:
    id: int
    house: House
    state: CarState = CarState.WAITING
    destination: int | None = None
    at: Pos | None = None  # tile it's on (None while parked)
    path: list[Pos] = field(default_factory=list)  # tiles still to drive, target last
    progress: float = 0.0  # toward path[0]
    speed: float = 0.0
    obstacle: Obstacle = Obstacle.NONE
    blocked_for: float = 0.0
    park_timer: float = 0.0
    heading: int | None = None


@dataclass
class MapStyle:
    width: int = 36
    height: int = 24
    rivers: int = 1  # 0-2 rivers across the map
    mountains: bool = False
    start: tuple[int, int] = (16, 10)  # starting city limits, centred
    grow: int = 2  # tiles the limits grow on each side per week


MAPS = {
    "river": MapStyle(),
    "two-rivers": MapStyle(rivers=2),
    "plain": MapStyle(rivers=0),
    "mountains": MapStyle(rivers=1, mountains=True),
}


class World:
    def __init__(self, seed: int = 0, style: MapStyle | str = "river"):
        self.rng = random.Random(seed)
        self.style = MAPS[style] if isinstance(style, str) else style
        self.time = 0.0
        self.score = 0
        self.ended = False
        self.roads: dict[Pos, list[int]] = {}
        self.content: dict[Pos, Content] = {}
        self.houses: list[House] = []
        self.destinations: dict[int, Destination] = {}
        self.cars: list[SimCar] = []
        self.available = [0] * 9
        self.available[Upgrade.CONCRETE] = START_CONCRETE
        self.mothballed = [0] * 9
        self.refunds: list[tuple[float, Upgrade]] = []  # (game time, upgrade) coming back to stock
        self.roundabouts: set[Pos] = set()
        self.lights: set[Pos] = set()
        self.choices: list[list[dict]] = []  # pending weekly upgrade choices
        self.week_done = 0
        self.version = 0  # bumps whenever the roads change, so cars re-route
        self._seen_version = 0
        self._next_key = 1
        self._next_car = 1
        self._house_clock = 20.0
        self._dest_clock = WEEK
        self._dispatch_clock = 0.0
        self.terrain = self._make_terrain()
        has_water = any(t.water for t in self.terrain.values())
        has_rock = any(t.mountain for t in self.terrain.values())
        self.available[Upgrade.BRIDGE] = 1 if has_water else 0
        self.available[Upgrade.TUNNEL] = 1 if has_rock else 0
        dest = self._spawn_destination(0)
        for _ in range(2):
            self._spawn_house(dest.group)

    # Map ----------------------------------------------------------------------------------------

    def _make_terrain(self) -> dict[Pos, Terrain]:
        w, h = self.style.width, self.style.height
        water: set[Pos] = set()
        rock: set[Pos] = set()
        for n in range(self.style.rivers):
            if n == 0:  # down the map, off the centre so the start is on land
                x = self.rng.choice([w // 2 - 6, w // 2 + 5])
                for y in range(h):
                    water |= {(x, y), (x + 1, y)}
                    if self.rng.random() < 0.3:
                        x = min(w - 3, max(1, x + self.rng.choice((-1, 1))))
            else:  # across the map
                y = self.rng.choice([h // 2 - 5, h // 2 + 4])
                for x in range(w):
                    water |= {(x, y), (x, y + 1)}
                    if self.rng.random() < 0.3:
                        y = min(h - 3, max(1, y + self.rng.choice((-1, 1))))
        if self.style.mountains:
            cx, cy = self.rng.choice([(5, 5), (w - 6, h - 6), (5, h - 6), (w - 6, 5)])
            rock = {(x, y) for x in range(w) for y in range(h)
                    if (x - cx) ** 2 + (y - cy) ** 2 <= 9 and (x, y) not in water}
        return {(x, y): Terrain((x, y) not in water and (x, y) not in rock, (x, y) in water, (x, y) in rock,
                                (x, y) not in water and (x, y) not in rock)
                for x in range(w) for y in range(h)}

    @property
    def week(self) -> int:
        return int(self.time // WEEK)

    @property
    def day(self) -> int:
        return int(self.time // DAY)

    def playable(self) -> frozenset[Pos]:
        w, h = self.style.width, self.style.height
        grow = self.style.grow * self.week
        pw, ph = min(w, self.style.start[0] + 2 * grow), min(h, self.style.start[1] + 2 * grow)
        x0, y0 = (w - pw) // 2, (h - ph) // 2
        return frozenset((x, y) for x in range(x0, x0 + pw) for y in range(y0, y0 + ph))

    def _free(self, p: Pos, area: frozenset[Pos]) -> bool:
        t = self.terrain.get(p)
        return (p in area and t is not None and t.buildable and p not in self.content
                and not any(self.roads.get(p, ())) and not self._is_entrance(p))

    def _is_entrance(self, p: Pos) -> bool:
        return any(d.entrance == p for d in self.destinations.values())

    # Spawning ----------------------------------------------------------------------------------

    def _spawn_destination(self, group: int) -> Destination | None:
        area = self.playable()
        spots = []
        for (x, y) in area:
            tiles = [(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)]
            park = [(x, y - 1), (x + 1, y - 1)]
            side = self.rng.choice((0, 1))  # entrance west of park[0], or east of it
            if side:
                park.reverse()
            entrance = neighbor(park[0], W if not side else E)
            around = [neighbor(t, d) for t in tiles + park for d in range(8)]
            if all(self._free(t, area) for t in tiles + park + [entrance]) and \
                    not any(self.content.get(a) for a in around):
                spots.append((tiles, park, entrance, W if not side else E))
        if not spots:
            return None
        tiles, park, entrance, d = self.rng.choice(sorted(spots))
        dest = Destination(self._next_key, group, tiles, park, entrance, d, pin_clock=self._pin_interval())
        self._next_key += 1
        for t in tiles:
            self.content[t] = Content.DESTINATION
        for t in park:
            self.content[t] = Content.CARPARK
        self.roads[park[0]] = [0] * 8
        self.roads[park[0]][d] = ACTIVE  # the car park's stub toward its entrance
        self.destinations[dest.key] = dest
        return dest

    def _spawn_house(self, group: int) -> House | None:
        area = self.playable()
        mine = [d for d in self.destinations.values() if d.group == group]
        near = self.rng.random() < 0.75 and mine
        candidates = []
        for p in area:
            if not self._free(p, area):
                continue
            if near and min(abs(p[0] - t[0]) + abs(p[1] - t[1]) for d in mine for t in d.tiles) > 6:
                continue
            ways = [d for d in CARDINALS if self._free(neighbor(p, d), area) or
                    (any(self.roads.get(neighbor(p, d), ())) and self.content.get(neighbor(p, d)) is None)]
            if ways:
                candidates.append((p, ways))
        if not candidates:
            return None
        p, ways = self.rng.choice(sorted(candidates))
        house = House(p, group, self.rng.choice(ways))
        self.houses.append(house)
        self.content[p] = Content.HOUSE
        self.roads[p] = [0] * 8
        self.roads[p][house.driveway] = ACTIVE  # driveway stub
        self._join_access(house)
        for _ in range(CARS_PER_HOUSE):
            self.cars.append(SimCar(self._next_car, house, at=p))
            self._next_car += 1
        self.version += 1
        return house

    def _pin_interval(self) -> float:
        """Seconds between pins at one destination: one a day at first, about one every 5 s by week 8."""
        return DAY / (1.0 + 0.4 * self.time / WEEK)

    def _spawn_tick(self, dt: float):
        self._dest_clock -= dt
        if self._dest_clock <= 0:
            self._dest_clock = WEEK * self.rng.uniform(0.6, 1.0)
            colours = sorted({d.group for d in self.destinations.values()})
            if len(colours) < MAX_COLOURS and (self.rng.random() < 0.6 or len(colours) < 3):
                group = len(colours)
                if self._spawn_destination(group):
                    self._spawn_house(group)
                    self._spawn_house(group)
            else:
                self._spawn_destination(self.rng.choice(colours))
        self._house_clock -= dt
        if self._house_clock <= 0:
            self._house_clock = max(10.0, 25.0 - 2.0 * self.week)
            # The colour whose destinations have the most demand for its cars gets the house.
            need: dict[int, float] = {}
            for d in self.destinations.values():
                need[d.group] = need.get(d.group, 0.0) + 1.0
            for h in self.houses:
                need[h.group] = need.get(h.group, 0.0) - 0.3
            group = max(sorted(need), key=lambda g: need[g] + self.rng.random())
            self._spawn_house(group)

    # Roads ------------------------------------------------------------------------------------

    def joined(self, p: Pos, d: int) -> bool:
        q = neighbor(p, d)
        return bool(self.roads.get(p, [0] * 8)[d]) and bool(self.roads.get(q, [0] * 8)[opposite(d)])

    def links(self, p: Pos) -> int:
        return sum(self.joined(p, d) for d in range(8))

    def _set(self, p: Pos, d: int, on: bool):
        r = self.roads.setdefault(p, [0] * 8)
        r[d] = ACTIVE if on else 0

    def _join_access(self, house: House):
        """A house's driveway tile joins the house as soon as it has road (as in the game)."""
        a = house.access
        if any(self.roads.get(a, ())):
            self._set(a, opposite(house.driveway), True)

    def _join_entrances(self):
        for d in self.destinations.values():
            if any(self.roads.get(d.entrance, ())):
                self._set(d.entrance, opposite(d.entrance_dir), True)

    def _house_at(self, p: Pos) -> House | None:
        return next((h for h in self.houses if h.pos == p), None)

    def _crossing_kind(self, p: Pos) -> Upgrade | None:
        t = self.terrain.get(p)
        if t is None or t.driveable:
            return None
        return Upgrade.BRIDGE if t.water else Upgrade.TUNNEL

    def add_road(self, p: Pos, d: int) -> int:
        """Draw one segment from tile p toward d, as TileEditor.AddRoad + EditTileCommand do."""
        q = neighbor(p, d)
        area = self.playable()
        if p not in area or q not in area:
            return INVALID
        house = self._house_at(p)
        if house is not None:  # drawing out of a house turns its driveway
            if d == house.driveway:
                return OK
            if d % 2 or self.content.get(q) is not None or not self.terrain[q].driveable:
                return INVALID
            self.roads[p] = [0] * 8
            self.roads[p][d] = ACTIVE
            house.driveway = d
            self._join_access(house)
            self.version += 1
            return OK
        if self.joined(p, d):
            return EXISTS
        for a, b in ((p, q), (q, p)):
            c = self.content.get(b)
            if c in (Content.DESTINATION,):
                return INVALID
            if c == Content.HOUSE:
                h = self._house_at(b)
                if h.access != a:
                    return INVALID
            if c == Content.CARPARK:
                dest = next(x for x in self.destinations.values() if b in x.park)
                if b != dest.park[0] or a != dest.entrance:
                    return NO_CARPARK
        if d % 2:  # diagonal: no crossing over water, and no other diagonal through the same corner
            if self._crossing_kind(p) or self._crossing_kind(q):
                return CROSSING
            a, b = (p[0] + DIRS[d][0], p[1]), (p[0], p[1] + DIRS[d][1])
            if self.roads.get(a, [0] * 8)[direction_between(a, b)]:
                return CROSSING
        # Bridges and tunnels: straight decks, one upgrade for each one started from land.
        for t in (p, q):
            kind = self._crossing_kind(t)
            if kind is None:
                continue
            r = self.roads.get(t, [0] * 8)
            if any(r) and not (r[d] or r[opposite(d)]):
                return NO_BRIDGE if kind == Upgrade.BRIDGE else NO_TUNNEL
        start = self._crossing_kind(q) if (self._crossing_kind(p) is None and not any(self.roads.get(q, ()))) else None
        if start is not None and self.available[start] <= 0:
            return NO_BRIDGE if start == Upgrade.BRIDGE else NO_TUNNEL
        free = {h.access for h in self.houses}
        cost = sum(1 for t in (p, q) if not any(self.roads.get(t, ())) and t not in free
                   and self.content.get(t) is None)
        if cost > self.available[Upgrade.CONCRETE]:
            return NO_CONCRETE
        self.available[Upgrade.CONCRETE] -= cost
        if start is not None:
            self.available[start] -= 1
        self._set(p, d, True)
        self._set(q, opposite(d), True)
        for h in self.houses:
            self._join_access(h)
        self._join_entrances()
        self.version += 1
        return OK

    def clear_tile(self, p: Pos) -> int:
        """Remove every road on a tile (TileEditor.ClearTile). On a bridge or tunnel deck the whole
        crossing goes, and its upgrade comes back after MOTHBALL_TIME."""
        if self.content.get(p) is not None:
            return CANT_CLEAR
        if not any(self.roads.get(p, ())):
            return NO_ROADS
        kind = self._crossing_kind(p)
        tiles = {p}
        if kind is not None:  # the whole deck
            stack = [p]
            while stack:
                t = stack.pop()
                for d in range(8):
                    u = neighbor(t, d)
                    if u not in tiles and self.joined(t, d) and self._crossing_kind(u) is not None:
                        tiles.add(u)
                        stack.append(u)
            self.mothballed[kind] += 1
            self.refunds.append((self.time + MOTHBALL_TIME, kind))
        for t in tiles:
            if self._crossing_kind(t) is None:
                self.available[Upgrade.CONCRETE] += 1
            self.roads[t] = [0] * 8
            for d in range(8):
                u = neighbor(t, d)
                if u in self.roads:
                    self.roads[u][opposite(d)] = 0
        for h in self.houses:  # a house keeps its driveway stub
            self.roads.setdefault(h.pos, [0] * 8)[h.driveway] = ACTIVE
        for dest in self.destinations.values():
            self.roads.setdefault(dest.park[0], [0] * 8)[dest.entrance_dir] = ACTIVE
        self.roundabouts -= tiles
        self.lights -= tiles
        self.version += 1
        return OK

    def add_junction_upgrade(self, kind: Upgrade, p: Pos) -> int:
        if self.available[kind] <= 0:
            return NO_UPGRADES
        if self.links(p) < 3 or self.content.get(p) is not None or p in self.roundabouts | self.lights:
            return INVALID
        self.available[kind] -= 1
        (self.roundabouts if kind == Upgrade.ROUNDABOUT else self.lights).add(p)
        return OK

    # Upgrades -----------------------------------------------------------------------------------

    def _offer(self):
        has_water = any(t.water for t in self.terrain.values())
        has_rock = any(t.mountain for t in self.terrain.values())
        packages = [{"type": Upgrade.CONCRETE, "amount": 30, "concrete": 0},
                    {"type": Upgrade.ROUNDABOUT, "amount": 1, "concrete": 20},
                    {"type": Upgrade.TRAFFIC_LIGHT, "amount": 2, "concrete": 20},
                    {"type": Upgrade.MOTORWAY, "amount": 1, "concrete": 10}]
        if has_water:
            packages.append({"type": Upgrade.BRIDGE, "amount": 1, "concrete": 20})
        if has_rock:
            packages.append({"type": Upgrade.TUNNEL, "amount": 1, "concrete": 20})
        self.choices.append(self.rng.sample(packages, 2))

    def choose(self, index: int):
        if not self.choices:
            return
        pick = self.choices.pop(0)[index]
        self.available[pick["type"]] += pick["amount"]
        self.available[Upgrade.CONCRETE] += pick["concrete"]

    # Cars ---------------------------------------------------------------------------------------

    def route(self, start: Pos, goal: Pos) -> list[Pos] | None:
        """Shortest drive over joined road from start to goal (tiles after start). Cars don't drive
        through houses or car parks other than their own start and goal."""
        stops = {h.pos for h in self.houses} | {t for d in self.destinations.values() for t in d.park}
        dist = {start: 0.0}
        prev: dict[Pos, Pos] = {}
        heap = [(0.0, start)]
        while heap:
            cost, p = heapq.heappop(heap)
            if p == goal:
                break
            if cost > dist[p] or (p in stops and p != start):
                continue
            for d in range(8):
                if not self.joined(p, d):
                    continue
                q = neighbor(p, d)
                nd = cost + drive_length(p, q)
                if nd < dist.get(q, math.inf):
                    dist[q], prev[q] = nd, p
                    heapq.heappush(heap, (nd, q))
        if goal not in dist:
            return None
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        return path[-2::-1]

    def capacity(self, p: Pos) -> int:
        if p in self.roundabouts or p in self.lights:
            return CAPACITY_UPGRADED
        return CAPACITY_JUNCTION if self.links(p) >= 3 else CAPACITY_ROAD

    def _dispatch(self):
        for dest in sorted(self.destinations.values(), key=lambda d: -d.unassigned):
            while dest.unassigned:
                best = None
                for car in self.cars:
                    if car.state != CarState.WAITING or car.house.group != dest.group:
                        continue
                    path = self.route(car.house.pos, dest.park[0])
                    if path and (best is None or len(path) < len(best[1])):
                        best = (car, path)
                if best is None:
                    break
                car, path = best
                dest.unassigned -= 1
                dest.en_route += 1
                car.state, car.destination, car.path, car.progress = CarState.DRIVING_TO_DESTINATION, dest.key, path, 0.0
                car.at, car.heading = car.house.pos, None

    def _reroute(self, car: SimCar):
        dest = self.destinations.get(car.destination)
        if car.state == CarState.DRIVING_TO_DESTINATION and dest:
            path = self.route(car.at, dest.park[0])
            if path:
                car.path, car.progress = path, 0.0
                return
            dest.en_route -= 1  # give the pin back; the car goes home
            dest.unassigned += 1
            car.state = CarState.DRIVING_HOME
        path = self.route(car.at, car.house.pos)
        if path:
            car.path, car.progress = path, 0.0
        else:  # cut off from home: it finds its way back
            car.state, car.at, car.path, car.destination = CarState.WAITING, car.house.pos, [], None

    def _move_cars(self, dt: float):
        occupancy: dict[Pos, int] = {}
        for c in self.cars:
            if c.at is not None and c.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME):
                occupancy[c.at] = occupancy.get(c.at, 0) + 1
        for car in self.cars:
            if car.state == CarState.PARKING:
                car.park_timer -= dt
                if car.park_timer <= 0:
                    dest = self.destinations[car.destination]
                    dest.en_route -= 1
                    dest.served += 1
                    self.score += 1
                    car.state, car.at = CarState.DRIVING_HOME, dest.park[0]
                    self._reroute(car)
                continue
            if car.state not in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) or not car.path:
                continue
            nxt = car.path[0]
            d = direction_between(car.at, nxt) if max(abs(car.at[0] - nxt[0]), abs(car.at[1] - nxt[1])) == 1 else None
            if d is None or not self.joined(car.at, d):
                self._reroute(car)
                continue
            bend = 0 if car.heading is None else min((d - car.heading) % 8, (car.heading - d) % 8)
            speed = TURN_SPEED if bend >= 2 else CAR_SPEED
            step = speed * dt / drive_length(car.at, nxt)
            target_end = nxt == car.path[-1]
            if car.progress + step >= 1.0 and not target_end and occupancy.get(nxt, 0) >= self.capacity(nxt) \
                    and car.blocked_for < DEADLOCK_TIMEOUT:
                car.progress, car.speed = 0.95, 0.0
                car.blocked_for += dt
                car.obstacle = Obstacle.BLOCKING_INTERSECTION if self.links(nxt) >= 3 else Obstacle.LEADING_VEHICLE
                continue
            car.blocked_for, car.speed = 0.0, speed
            car.obstacle = Obstacle.TARGET if target_end else Obstacle.NONE
            car.progress += step
            if car.progress < 1.0:
                continue
            occupancy[car.at] = occupancy.get(car.at, 1) - 1
            car.at, car.heading, car.progress = nxt, d, 0.0
            car.path.pop(0)
            occupancy[nxt] = occupancy.get(nxt, 0) + 1
            if car.path:
                continue
            if car.state == CarState.DRIVING_TO_DESTINATION:  # into the car park
                car.state, car.park_timer, car.at = CarState.PARKING, PARK_TIME, None
                occupancy[nxt] -= 1
            else:  # home
                car.state, car.destination, car.speed, car.obstacle = CarState.WAITING, None, 0.0, Obstacle.NONE
                occupancy[nxt] -= 1

    # Clock --------------------------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return bool(self.choices) or self.ended

    def advance(self, seconds: float):
        """Run the game for this many game seconds (not while an upgrade choice is pending)."""
        left = seconds
        while left > 1e-9 and not self.paused:
            dt = min(SUBSTEP, left)
            left -= dt
            self._step(dt)

    def _step(self, dt: float):
        before = self.week
        self.time += dt
        for t, kind in list(self.refunds):
            if t <= self.time:
                self.refunds.remove((t, kind))
                self.mothballed[kind] -= 1
                self.available[kind] += 1
        self._spawn_tick(dt)
        for dest in self.destinations.values():
            dest.pin_clock -= dt
            if dest.pin_clock <= 0:
                dest.pin_clock += self._pin_interval()
                dest.unassigned += 1
            if dest.unassigned + dest.en_route > PIN_LIMIT:
                dest.overcrowd_time += dt
                if dest.overcrowd_time >= MAX_OVERCROWD:
                    self.ended = True
            else:
                dest.overcrowd_time = max(0.0, dest.overcrowd_time - dt)
        self._dispatch_clock -= dt
        if self._dispatch_clock <= 0:
            self._dispatch_clock = DISPATCH_EVERY
            self._dispatch()
        if self.version != self._seen_version:  # roads changed since the last step: cars re-route
            self._seen_version = self.version
            self.reroute_all()
        self._move_cars(dt)
        if self.week > before:
            self._offer()

    def reroute_all(self):
        for car in self.cars:
            if car.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME):
                self._reroute(car)

