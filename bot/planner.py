"""Road planning: which houses already reach a destination of their colour, and the shortest new
roads that would connect the ones that don't.

The board is a grid; a road tile stores which of its 8 directions carry road. Two tiles are joined
when both point at each other. Houses and car parks own a stub pointing at their access tile, so
they join the network through the same rule. Roads may run diagonally, and a house can be turned
to face any of its 8 neighbours (drawing out of a house turns its driveway, as a drag does).
"""

from __future__ import annotations

import collections
import heapq
import math
import itertools
from dataclasses import dataclass, field, replace

from game import (DIRS, Content, Destination, House, Pos, State, Terrain, Upgrade, direction_between, neighbor,
                  opposite, ring_tiles)

BLOCKING = {Content.HOUSE, Content.DESTINATION, Content.CARPARK, Content.BOAT_TERMINAL}

# Path costs, in tiles of driving. What matters is how long cars take to reach their destination,
# so every tile a car drives costs COST_DRIVE (a diagonal step is DIAGONAL tiles long), and turning
# costs a little more. Building a road tile costs COST_NEW_TILE on top: road tiles are still worth
# saving, but less than the cars' time.
DIAGONAL = math.sqrt(2)
COST_DRIVE = 1.0
COST_NEW_TILE = 0.5
COST_TREE = 0.05  # building over a tree clears it; slightly worse than open ground
# Cost of a bend, by its size in 45° steps. Cars slow down for 90° (and sharper) turns but not for
# 45° bends.
TURN_COST = (0.0, 0.05, 0.5)
COST_TURN_HOUSE = 0.05  # turning a house is free in the game, but don't churn for nothing
COST_BRIDGE = 4.0  # on top of the tiles it spans; bridges are scarce
MAX_CROSSING = 5
MAX_TURN = 2  # in 45° steps: no road bends sharper than a right angle
# The rest of the trip, from where a new road joins the network to the destination, counts like the
# new road itself.
COST_REMAINING_TRIP = COST_DRIVE
# Cars never route around traffic: every car of a colour takes the same free-flow route, so a trunk
# carrying many houses jams. Road carrying more than LOAD_FREE houses costs COST_LOAD per extra house
# to drive along or join, so new houses get their own way in rather than piling onto the busiest road.
LOAD_FREE = 6
COST_LOAD = 0.15
# Joining a road where it would become a junction slows every car through it.
COST_JUNCTION = 0.2
# Colours are kept on separate roads where possible: a car of one colour on another colour's road
# adds to that road's queue and junctions without serving anyone there. Driving along road that
# other colours' houses use costs COST_SHARED a tile, plus COST_SHARED_LOAD per such house; ending a
# new road on one costs COST_SHARED_JOIN, so a house spends up to about one extra tile to join a
# road of its own colour instead.
COST_SHARED = 0.5
COST_SHARED_JOIN = 1.0
COST_SHARED_LOAD = 0.05
# At most two colours share a road: a road tile that two other colours already use costs this much
# to drive along, on the new road or anywhere on the rest of the trip. It is avoided whenever there
# is any other way, but never makes a house impossible to connect.
COST_COLOUR_CAP = 25.0
# A house of a colour with a destination close to overcrowding will spend up to this many extra
# tiles to join that destination's network instead of another one of its colour.
COST_OTHER_NETWORK = 3.0
# A bypass (new road from a car park entrance straight to a far part of its network) must shorten
# some trip by at least this many tiles.
MIN_BYPASS_SAVING = 4
# Among bypasses, one more new tile is worth it for every two tiles it takes off the trip.
COST_BYPASS_TRIP = 0.5
# Optimising existing roads (shortcuts, clearing loops) may never lengthen any house's trip.
MAX_DETOUR = 0
# Cars of a colour serve every destination of that colour, not only the nearest, so colour sharing
# (the cap, mixed_tiles, separation) follows each house's drive to every one of them. False counts
# only the drive to the nearest destination, as before.
COUNT_EVERY_DESTINATION = True
# A road built to take a colour off a road two other colours use may have at most this many new tiles.
MAX_SEPARATION_ROAD = 10
# To keep colours apart, cutting a shortcut onto a shared road may lengthen a trip by this many tiles.
MAX_SEPARATION_DETOUR = 8.0
TRIP_EPSILON = 1e-6
# Cars may drive through a car park with two entrances (False: model every car park as a dead end, as
# before cars were read).
DRIVE_THROUGH_CARPARKS = True
# Its lanes are slow (entrance lanes run at
# half speed, the lanes past the spaces at 1/6 to full speed), and the game routes by travel time
# (LaneModel.PathfindingCost = 10 x length / speed limit). So a tile driven inside a car park costs
# this many tiles of open road. Calibrated against the cars' own routes (tools/routecheck.py): over
# 2,601 car checks from 53 snapshots, 3.0 matched 84.7%, 2.0 84.1%, 4.0 and up 84.4%, and treating every
# car park as a dead end 84.3%.
PARK_DRIVE_FACTOR = 3.0
# Real jams, read from the cars (traffic.JamTracker): a road tile's jam level is the average number
# of cars stopped in a queue on it. Driving along or joining a road tile costs COST_JAM per stopped
# car above JAM_FREE, so new roads, shortcuts, bypasses and separate roads go round real queues and
# not only predicted load. USE_JAM_COST = False switches it off (bot.py --no-jam-cost).
USE_JAM_COST = True
JAM_FREE = 0.5
COST_JAM = 1.0


def step_length(d: int) -> float:
    return DIAGONAL if d % 2 else 1.0


def drive_length(a: Pos, b: Pos) -> float:
    """Length of the drive between two neighbouring tiles."""
    return DIAGONAL if a[0] != b[0] and a[1] != b[1] else 1.0


def octile(a: Pos, b: Pos) -> float:
    """Shortest possible drive between two tiles, straight and diagonal steps combined."""
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return max(dx, dy) + (DIAGONAL - 1) * min(dx, dy)


def turn_size(a: int, b: int) -> int:
    """Angle between two directions in 45° steps (0-4)."""
    diff = (b - a) % 8
    return min(diff, 8 - diff)


class Network:
    """Union-find over tiles joined by roads (including house and car park stubs)."""

    def __init__(self, state: State):
        self._parent: dict[Pos, Pos] = {}
        for p in state.roads:
            for d in range(8):
                if state.joined(p, d):
                    self.union(p, neighbor(p, d))
        for cp in state.carparks.values():
            for t in cp.tiles[1:]:
                self.union(cp.tiles[0], t)

    def find(self, p: Pos) -> Pos:
        root = p
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(p, p) != root:  # path compression
            self._parent[p], p = root, self._parent[p]
        return root

    def union(self, a: Pos, b: Pos):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


@dataclass
class Plan:
    label: str  # for logs
    key: tuple  # identifies what is being connected, for retry bookkeeping
    group: int  # colour
    segments: list[tuple[Pos, int]]  # (from tile, direction) in drawing order
    tiles: list[Pos]  # every tile the new road touches
    new_tiles: int  # tiles that get road for the first time (≈ concrete needed)
    bridges: int = 0  # bridges started (each takes one from stock)
    tunnels: int = 0  # tunnels started
    turned_house: bool = False
    saves: int = 0  # for shortcuts: tiles freed by replacing the old road

    @property
    def crossings(self) -> int:
        return self.bridges + self.tunnels


@dataclass
class Board:
    """Per-tick view used by the path search, with an overlay for roads planned this tick."""

    state: State
    terrain: dict[Pos, Terrain]
    refused: set[tuple[Pos, int]] = field(default_factory=set)  # moves the game recently rejected
    planned: dict[Pos, int] = field(default_factory=dict)  # tile -> colour whose network it joins
    planned_corners: set[frozenset] = field(default_factory=set)  # corners crossed by planned diagonals
    load: dict[Pos, int] = field(default_factory=dict)  # houses whose route uses each road tile
    colours: dict[Pos, dict[int, int]] = field(default_factory=dict)  # tile -> colour -> houses routed over it
    group: int | None = None  # colour of the road being planned, for the shared-road cost
    # When set, the only existing road tiles a search may step on (for roads that must be all new).
    road_allowed: set[Pos] | None = None
    jam: dict[Pos, float] = field(default_factory=dict)  # road tile -> average cars stopped in a queue on it
    rings: list[set[Pos]] = field(init=False, repr=False)  # each roundabout's 4 ring tiles

    def __post_init__(self):
        self.rings = [set(ring_tiles(c)) for c in self.state.roundabouts]

    def is_road(self, p: Pos) -> bool:
        return self.state.has_road(p) or p in self.planned

    def load_cost(self, p: Pos) -> float:
        return COST_LOAD * max(0, self.load.get(p, 0) - LOAD_FREE)

    def jam_cost(self, p: Pos) -> float:
        """Extra cost of putting more cars on a road tile where cars already queue."""
        if not USE_JAM_COST:
            return 0.0
        return COST_JAM * max(0.0, self.jam.get(p, 0.0) - JAM_FREE)

    def shared_cost(self, p: Pos, base: float = COST_SHARED) -> float:
        """Extra cost of putting this colour's cars on a road tile other colours use."""
        if self.group is None:
            return 0.0
        if self.planned.get(p, self.group) != self.group:
            return base
        others = sum(n for g, n in self.colours.get(p, {}).items() if g != self.group)
        return base + COST_SHARED_LOAD * others if others else 0.0

    def capped(self, p: Pos) -> bool:
        """Whether this colour on road tile p would make three colours share it."""
        if self.group is None:
            return False
        others = {g for g in self.colours.get(p, {}) if g != self.group}
        planned = self.planned.get(p)
        if planned is not None and planned != self.group:
            others.add(planned)
        return len(others) >= 2

    def join_cost(self, p: Pos) -> float:
        """Extra cost of ending a new road on an existing road tile: its traffic, other colours on
        it, and a new junction."""
        if not self.state.has_road(p):
            return 0.0
        junction = COST_JUNCTION if self.state.links(p) >= 2 else 0.0
        return self.load_cost(p) + self.jam_cost(p) + self.shared_cost(p, COST_SHARED_JOIN) + junction

    def passable(self, p: Pos) -> bool:
        if not self.state.in_playable(p):
            return False
        if self.road_allowed is not None and self.is_road(p) and p not in self.road_allowed:
            return False
        t = self.terrain.get(p)
        if t is None or not t.driveable:
            return False
        return self.state.content.get(p, Content.NONE) not in BLOCKING

    def crossable(self, p: Pos, kind: Upgrade) -> bool:
        """Water (bridge) or mountain (tunnel) tile inside the city limits."""
        t = self.terrain.get(p)
        if t is None or t.driveable or not self.state.in_playable(p):
            return False
        return t.water if kind == Upgrade.BRIDGE else t.mountain

    def step_cost(self, p: Pos, d: int) -> float:
        """Cost of driving onto tile p in direction d, plus building it if it has no road yet."""
        drive = COST_DRIVE * step_length(d)
        if self.is_road(p):
            return (drive + self.load_cost(p) + self.jam_cost(p) + self.shared_cost(p)
                    + (COST_COLOUR_CAP if self.capped(p) else 0.0))
        if self.state.content.get(p) == Content.TREE:
            return drive + COST_NEW_TILE + COST_TREE
        return drive + COST_NEW_TILE

    @staticmethod
    def corner(p: Pos, d: int) -> frozenset:
        """A diagonal step passes the corner it shares with the two tiles beside it; those two
        tiles identify the corner (and the other diagonal through it)."""
        dx, dy = DIRS[d]
        return frozenset({(p[0] + dx, p[1]), (p[0], p[1] + dy)})

    def move_allowed(self, p: Pos, d: int) -> bool:
        if (p, d) in self.refused:
            return False
        # A roundabout joins any neighbour whose road is drawn toward its centre, but a road can't be
        # drawn out of the centre, and two of its ring tiles are already linked by the ring.
        q = neighbor(p, d)
        if p in self.state.roundabouts and not self.state.joined(p, d):
            return False
        if any(p in ring and q in ring for ring in self.rings):
            return False
        if d % 2:  # diagonal: may not cross another diagonal at the same corner
            corner = self.corner(p, d)
            if corner in self.planned_corners:
                return False
            a, b = tuple(corner)
            if self.state.road_toward(a, direction_between(a, b)):
                return False
        return True

    def claim(self, plan: Plan):
        for t in plan.tiles:
            self.planned[t] = plan.group
        for p, d in plan.segments:
            if d % 2:
                self.planned_corners.add(self.corner(p, d))


def group_roots(state: State, net: Network) -> dict[int, set[Pos]]:
    """For each colour, the network components that contain one of its active destinations."""
    roots: dict[int, set[Pos]] = {}
    for dest in state.destinations.values():
        cp = state.carparks.get(dest.carpark)
        if dest.active and cp and cp.tiles:
            roots.setdefault(dest.group, set()).add(net.find(cp.tiles[0]))
    return roots


def is_served(house: House, net: Network, roots: dict[int, set[Pos]]) -> bool:
    return net.find(house.pos) in roots.get(house.group, set())


def entrances_for(state: State, group: int, only: Destination | None = None) -> list[tuple[Pos, Pos, int]]:
    """(outside tile, car park tile, direction outside->car park) for active destinations of a colour."""
    out = []
    for dest in state.destinations.values():
        cp = state.carparks.get(dest.carpark)
        if not (dest.active and dest.group == group and cp) or (only is not None and dest.key != only.key):
            continue
        for outside, d in cp.entrances:
            out.append((outside, neighbor(outside, opposite(d)), opposite(d)))
    return out


# Search -------------------------------------------------------------------------------------

Start = tuple[Pos, int, float]  # tile, heading on arrival, initial cost


def search(board: Board, starts: list[Start], is_goal, bridges: int, tunnels: int, goal_cost=None,
           through_goals: bool = False, bound=None):
    """Dijkstra over (tile, heading), so bends can be limited. Steps go in all 8 directions; a
    straight cardinal run of water or mountain tiles can be crossed with a bridge or tunnel, or for
    free where a deck is already started. With goal_cost(tile) the goal minimising
    path cost + goal_cost wins, otherwise the nearest one. Bridges and tunnels are counted
    separately, each against its own stock. With through_goals the search also drives on past goal
    tiles, so a road can run over its own network to a better place to join it (otherwise the first
    network tile reached ends every path, and a house inside its colour's network can only join
    where that network already is).
    bound(tile), if given, must never exceed the cheapest rest of the way from that tile, goal cost
    included (see entrance_bound); the search then looks there first and stops sooner (A*).
    Returns (path including crossed tiles, (bridges started, tunnels started)) or None."""
    counter = itertools.count()
    heap, best, prev, used = [], {}, {}, {}
    h = bound or (lambda p: 0.0)
    for pos, heading, initial in starts:
        key = (pos, heading)
        if board.passable(pos) and initial < best.get(key, float("inf")):
            best[key], used[key] = initial, (0, 0)
            heapq.heappush(heap, (initial + h(pos), next(counter), initial, key))

    def relax(from_key, to_key, spanned, cost, crossings):
        if cost < best.get(to_key, float("inf")):
            best[to_key], prev[to_key], used[to_key] = cost, (from_key, spanned), crossings
            heapq.heappush(heap, (cost + h(to_key[0]), next(counter), cost, to_key))

    found, found_total = None, float("inf")
    while heap:
        estimate, _, cost, key = heapq.heappop(heap)
        if cost > best.get(key, float("inf")):
            continue
        if estimate >= found_total:
            break  # nothing cheaper can follow
        p, heading = key
        if is_goal(p):
            total = cost + (goal_cost(p) if goal_cost else 0.0)
            if total < found_total:
                found, found_total = key, total
            if goal_cost is None:
                break
            if not through_goals:
                continue
        for d in range(8):
            bend = turn_size(heading, d)
            if bend > MAX_TURN or not board.move_allowed(p, d):
                continue
            q = neighbor(p, d)
            base = cost + TURN_COST[bend]
            if board.passable(q):
                relax(key, (q, d), [], base + board.step_cost(q, d), used[key])
                continue
            if d % 2:
                continue  # bridges and tunnels run straight along an axis
            for i, (kind, budget) in enumerate(((Upgrade.BRIDGE, bridges), (Upgrade.TUNNEL, tunnels))):
                if not board.crossable(q, kind):
                    continue
                spanned = [q]
                landing = neighbor(q, d)
                while board.crossable(landing, kind) and len(spanned) < MAX_CROSSING:
                    spanned.append(landing)
                    landing = neighbor(landing, d)
                if not board.passable(landing):
                    continue
                roads = [board.state.roads.get(t) for t in spanned]
                if any(r and any(r) and not (r[d] or r[opposite(d)]) for r in roads):
                    continue  # another crossing runs across this one
                # Stepping onto water takes a bridge from stock unless that first water tile already
                # has road (joining a deck that exists); further deck tiles never need one.
                started = bool(roads[0] and any(roads[0]))
                if not started and used[key][i] >= budget:
                    continue
                span_cost = sum(COST_DRIVE + (0.0 if r and any(r) else COST_NEW_TILE) for r in roads)
                step = base + (0.0 if started else COST_BRIDGE) + span_cost + board.step_cost(landing, d)
                after = used[key] if started else tuple(n + (j == i) for j, n in enumerate(used[key]))
                relax(key, (landing, d), spanned, step, after)

    if found is None:
        return None
    path, key = [found[0]], found
    while key in prev:
        key, spanned = prev[key]
        path.extend(reversed(spanned))
        path.append(key[0])
    path.reverse()
    return path, used[found]


def entrance_bound(state: State, group: int, also: list[Pos] = ()):
    """A lower bound on the rest of a trip for search(): wherever a road ends, the cars still have
    to get from here to a car park of the colour, and no drive is shorter than the straight-line
    (octile) distance. Every step costs at least COST_DRIVE per tile driven, so this never
    overestimates. `also` lists goal tiles whose goal cost may be below their distance to a car
    park (they count as car parks here)."""
    targets = [t for outside, park, _ in entrances_for(state, group) for t in (outside, park)] + list(also)
    if not targets:
        return None
    cache: dict[Pos, float] = {}

    def bound(p: Pos) -> float:
        if p not in cache:
            cache[p] = COST_DRIVE * min(octile(p, t) for t in targets)
        return cache[p]
    return bound


def _road_segments(path: list[Pos]) -> list[tuple[Pos, int]]:
    return [(a, direction_between(a, b)) for a, b in zip(path, path[1:])]


def _new_tiles(board: Board, path: list[Pos], free: set[Pos] = frozenset()) -> int:
    """Road tiles a path will consume. A house's driveway tile is free."""
    return sum(1 for t in path if not board.is_road(t) and t not in free)


def _house_starts(board: Board, house: House) -> list[Start]:
    """The tiles a house could drive out onto: any neighbour it can be turned to face."""
    starts = []
    for d in range(8):
        if (house.pos, d) in board.refused:
            continue
        if d != house.driveway and not board.move_allowed(house.pos, d):
            continue
        starts.append((neighbor(house.pos, d), d, 0.0 if d == house.driveway else COST_TURN_HOUSE))
    return starts


# Plans --------------------------------------------------------------------------------------

def plan_house(board: Board, house: House, net: Network, roots: dict[int, set[Pos]],
               bridges: int, tunnels: int, pressured: set[Pos] = frozenset(),
               through_goals: bool = True) -> Plan | None:
    """Shortest road from a house (turned to whichever side is best) to its colour's network or
    to a car park entrance of its colour. With pressured (network roots holding a destination close
    to overcrowding), joining one of those networks is preferred by up to COST_OTHER_NETWORK tiles,
    since cars only reach destinations on their own network. through_goals lets the road run on
    over the colour's network to a better join (see search); shortcuts leave it off, for speed."""
    board = replace(board, group=house.group)
    state = board.state
    goal_roots = roots.get(house.group, set())
    own_entrances = entrances_for(state, house.group)
    entrances = {outside: d for outside, _, d in own_entrances}
    entrance_root = {outside: net.find(park) for outside, park, _ in own_entrances}
    prefer = goal_roots & set(pressured)

    def network_cost(p: Pos) -> float:
        if not prefer:
            return 0.0
        root = entrance_root[p] if p in entrances else net.find(p)
        return 0.0 if root in prefer else COST_OTHER_NETWORK

    def on_network(p: Pos) -> bool:
        if board.planned.get(p) == house.group:
            return True
        return state.has_road(p) and net.find(p) in goal_roots and state.content.get(p) not in BLOCKING

    trips, crowded = trip_routes(state, house.group, colours=board.colours)

    def remaining_trip(p: Pos) -> float:
        if board.planned.get(p) == house.group and not state.has_road(p):
            return 0.0
        trip = 0.0 if p in entrances else COST_REMAINING_TRIP * trips.get(p, 0.0)
        # An entrance has no capped tiles on the way to its own car park, but may on the way to the
        # colour's other destinations.
        return trip + COST_COLOUR_CAP * crowded.get(p, 0) + board.join_cost(p) + network_cost(p)

    # Goals whose remaining trip isn't known count it as 0, so the bound must too.
    unknown = [p for p, g in board.planned.items() if g == house.group and not state.has_road(p)]
    unknown += [p for p in state.roads if p not in trips and on_network(p)]
    found = search(board, _house_starts(board, house), lambda p: on_network(p) or p in entrances,
                   bridges, tunnels, goal_cost=remaining_trip, through_goals=through_goals,
                   bound=entrance_bound(state, house.group, unknown))
    if found is None:
        return None
    path, (bridges_used, tunnels_used) = found
    facing = direction_between(house.pos, path[0])
    turned = facing != house.driveway
    segments = [(house.pos, facing)] if turned else []  # drawing out of the house turns it
    segments += _road_segments(path)
    end = path[-1]
    if not on_network(end):
        segments.append((end, entrances[end]))  # into the car park
    if not turned and not state.joined(path[0], opposite(facing)):
        # Join the house to its driveway tile: that tile may already have road (it may even be all
        # the plan needs), and a road over it doesn't join the house by itself.
        segments.append((path[0], opposite(facing)))
    return Plan(f"house {house.pos}", ("house", house.pos), house.group, segments, path,
                _new_tiles(board, path, {path[0]}), bridges_used, tunnels_used, turned)


def plan_feeder(board: Board, house: House, net: Network, dest: Destination, bridges: int,
                tunnels: int) -> Plan | None:
    """A road from a house on another network (its driveway tile) to a destination's network, for a
    destination short of cars: the house's network then shares its cars with the destination. Cars
    must arrive, so the two-colour limit doesn't apply."""
    state = board.state
    cp = state.carparks.get(dest.carpark)
    if not cp or not cp.tiles:
        return None
    target = net.find(cp.tiles[0])
    if net.find(house.pos) == target or not state.has_road(house.access):
        return None
    board = replace(board, group=None)
    entrances = {outside: d for outside, _, d in entrances_for(state, dest.group, dest)}
    trips = trip_lengths(state, dest.group, dest)

    def is_goal(p: Pos) -> bool:
        if p in entrances:
            return True
        return state.has_road(p) and net.find(p) == target and state.content.get(p) not in BLOCKING

    def goal_cost(p: Pos) -> float:
        return 0.0 if p in entrances else COST_REMAINING_TRIP * trips.get(p, 0.0) + board.join_cost(p)

    found = search(board, [(house.access, house.driveway, 0.0)], is_goal, bridges, tunnels, goal_cost=goal_cost)
    if found is None:
        return None
    path, (bridges_used, tunnels_used) = found
    if len(path) < 2:
        return None
    segments = _road_segments(path)
    if not (state.has_road(path[-1]) and net.find(path[-1]) == target):
        segments.append((path[-1], entrances[path[-1]]))  # into the car park
    return Plan(f"feeder from house {house.pos} to destination {dest.tiles[0]}", ("feeder", dest.key), dest.group,
                segments, path, _new_tiles(board, path), bridges_used, tunnels_used)


def plan_destination(board: Board, dest: Destination, net: Network, bridges: int, tunnels: int) -> Plan | None:
    """Shortest road from a destination's car park entrance to a network holding houses of its
    colour, or to a house of its colour (turning it to face the road if needed)."""
    board = replace(board, group=dest.group)
    state = board.state
    cp = state.carparks[dest.carpark]
    into_park = {outside: opposite(d) for outside, d in cp.entrances}
    houses = [h for h in state.houses if h.group == dest.group]
    if not houses:
        return None
    house_roots = {net.find(h.pos) for h in houses}
    # Tiles where reaching an unconnected house is enough: its driveway tile, or any neighbour it
    # could be turned to face.
    house_at: dict[Pos, tuple[House, int]] = {}
    for h in houses:
        if net.find(h.pos) == h.pos:  # not on any road yet
            for tile, d, _ in _house_starts(board, h):
                house_at.setdefault(tile, (h, d))
        house_at[h.access] = (h, h.driveway)

    def is_goal(p: Pos) -> bool:
        if p in house_at or board.planned.get(p) == dest.group:
            return True
        return state.has_road(p) and net.find(p) in house_roots and state.content.get(p) not in BLOCKING

    # Where the road meets the houses' network, every car still has to drive from its house to that
    # point: join where that drive, averaged over the colour's connected houses, is shortest.
    graph = RoadGraph(state)
    from_houses: dict[Pos, list[float]] = {}
    for h in houses:
        if net.find(h.pos) != h.pos:
            for t, dist in graph.distances([h.pos]).items():
                from_houses.setdefault(t, []).append(dist)

    def house_trip(p: Pos) -> float:
        if p in house_at or p not in from_houses:
            return 0.0
        return COST_REMAINING_TRIP * sum(from_houses[p]) / len(from_houses[p])

    starts = [(outside, opposite(into), 0.0) for outside, into in into_park.items()]
    found = search(board, starts, is_goal, bridges, tunnels, goal_cost=house_trip, through_goals=True)
    if found is None:
        return None
    path, (bridges_used, tunnels_used) = found
    entrance, end = path[0], path[-1]
    segments = [(entrance, into_park[entrance])] + _road_segments(path)
    turned = False
    if end in house_at and not (state.has_road(end) and net.find(end) in house_roots):
        house, facing = house_at[end]
        if facing != house.driveway:
            segments.append((house.pos, facing))
            turned = True
        elif not state.joined(end, opposite(facing)):
            segments.append((end, opposite(facing)))  # join the house to its driveway tile
    return Plan(f"destination {dest.tiles[0]}", ("destination", dest.key), dest.group, segments, path,
                _new_tiles(board, path), bridges_used, tunnels_used, turned)


# Network upkeep -----------------------------------------------------------------------------

def trip_routes(state: State, group: int, dest: Destination | None = None,
                colours: dict[Pos, dict[int, int]] | None = None) -> tuple[dict[Pos, float], dict[Pos, int]]:
    """Driving distance over existing road from each road tile to the nearest active car park of a
    colour (or to one destination's car park), and how many tiles that two other colours already
    use lie on the drives from it. With COUNT_EVERY_DESTINATION (and no single dest), those are the
    drives to every active destination of the colour, added up; otherwise the drive to the nearest."""
    dist, crowded = _drive(state, group, entrances_for(state, group, dest), colours)
    if dest is None and colours is not None and COUNT_EVERY_DESTINATION:
        crowded = {}
        for d in state.destinations.values():
            if d.active and d.group == group:
                for p, n in _drive(state, group, entrances_for(state, group, d), colours)[1].items():
                    crowded[p] = crowded.get(p, 0) + n
    return dist, crowded


def _drive(state: State, group: int, entrances: list[tuple[Pos, Pos, int]],
           colours: dict[Pos, dict[int, int]] | None) -> tuple[dict[Pos, float], dict[Pos, int]]:
    """trip_routes for one set of car park entrances: driving distance to the nearest of them, and
    how many tiles two other colours use on that drive."""
    dist: dict[Pos, float] = {}
    crowded: dict[Pos, int] = {}
    heap = []
    for outside, park, _ in entrances:
        for t in (outside, park):
            if state.has_road(t) and t not in dist:
                dist[t], crowded[t] = 0.0, 0
                heapq.heappush(heap, (0.0, t))

    def is_capped(t: Pos) -> bool:
        return colours is not None and len({g for g in colours.get(t, {}) if g != group}) >= 2

    while heap:
        cost, p = heapq.heappop(heap)
        if cost > dist[p]:
            continue
        for d in range(8):
            if not state.joined(p, d):
                continue
            q = neighbor(p, d)
            nd = cost + step_length(d)
            if nd < dist.get(q, float("inf")) - TRIP_EPSILON:
                dist[q] = nd
                crowded[q] = crowded[p] + (1 if is_capped(q) else 0)
                heapq.heappush(heap, (nd, q))
    return dist, crowded


def trip_lengths(state: State, group: int, dest: Destination | None = None) -> dict[Pos, float]:
    """Driving distance over existing road from each road tile to an active car park of a colour
    (or to one destination's car park)."""
    return trip_routes(state, group, dest)[0]


def unfinished_crossings(state: State, terrain: dict[Pos, Terrain]) -> list[tuple[Pos, int]]:
    """Segments missing from bridges and tunnels. A deck tile (over water or under a mountain)
    is always a straight pass-through, so it needs road on both sides of its axis, and each of
    its neighbours along that axis has to point back at it."""
    missing = set()
    for p, dirs in state.roads.items():
        t = terrain.get(p)
        if t is None or t.driveable or not any(dirs):
            continue
        for d, s in enumerate(dirs):
            if not s:
                continue
            if not dirs[opposite(d)]:
                outer = neighbor(p, opposite(d))
                if state.content.get(outer) not in BLOCKING:
                    missing.add((outer, d))  # from the far side into the deck
            q = neighbor(p, d)
            if not state.road_toward(q, opposite(d)) and state.content.get(q) not in BLOCKING:
                missing.add((p, d))
    return sorted(missing)


def dead_ends(state: State, terrain: dict[Pos, Terrain]) -> list[Pos]:
    """Road tiles that lead nowhere: chains ending in a tile with a single connection that isn't a
    house's driveway tile or a car park entrance (e.g. left behind after a house was turned or a
    road was refused halfway). Clearing them returns their tiles to the inventory. Tiles the
    optimising passes protect (_protected) are left alone. Returned loose end first: each tile can
    only be cleared once it is a dead end."""
    graph = RoadGraph(state)
    for p, dirs in state.roads.items():
        if any(dirs):
            graph.adj.setdefault(p, set())  # a half-road that meets nothing has no links at all
    return graph.prune(_protected(state, terrain))[1]


def unfed_destinations(state: State, net: Network) -> list[Destination]:
    """Active destinations whose car park isn't on a network with any house of their colour.
    Cars only come from connected houses, so these fill with pins until the game is lost."""
    house_roots: dict[int, set[Pos]] = {}
    for h in state.houses:
        house_roots.setdefault(h.group, set()).add(net.find(h.pos))
    out = []
    for dest in state.destinations.values():
        cp = state.carparks.get(dest.carpark)
        if dest.active and cp and cp.tiles and net.find(cp.tiles[0]) not in house_roots.get(dest.group, set()):
            out.append(dest)
    return out


# Optimising existing roads -------------------------------------------------------------------

class RoadGraph:
    """Tiles joined by road (both sides pointing at each other), plus the tiles of each car park.
    Cars don't drive through houses, or through a car park with a single entrance, so those are only
    ever endpoints. A car park with two entrances can be driven through: the game sends cars of other
    destinations in at one entrance and out at the other (seen in the cars' own paths)."""

    def __init__(self, state: State, adj: dict[Pos, set[Pos]] | None = None):
        self.state = state
        if adj is not None:
            self.adj = adj
            return
        self.adj = {}
        for p in state.roads:
            for d in range(8):
                if state.joined(p, d):
                    self.adj.setdefault(p, set()).add(neighbor(p, d))
        for cp in state.carparks.values():
            for a in cp.tiles:
                self.adj.setdefault(a, set()).update(t for t in cp.tiles if t != a)

    def without(self, tiles: set[Pos]) -> RoadGraph:
        adj = {p: qs - tiles for p, qs in self.adj.items() if p not in tiles}
        return RoadGraph(self.state, adj)

    def endpoints(self) -> set[Pos]:
        """Houses and car parks: optimising never clears them."""
        out = {h.pos for h in self.state.houses}
        for cp in self.state.carparks.values():
            out.update(cp.tiles)
        return out

    def stops(self) -> set[Pos]:
        """Tiles cars drive to or from but never through: houses, and car parks with one entrance."""
        out = {h.pos for h in self.state.houses}
        for cp in self.state.carparks.values():
            if len(cp.entrances) < 2 or not DRIVE_THROUGH_CARPARKS:
                out.update(cp.tiles)
        return out

    def park_tiles(self) -> set[Pos]:
        return {t for cp in self.state.carparks.values() for t in cp.tiles}

    def distances(self, sources: list[Pos]) -> dict[Pos, float]:
        """Driving distance from the nearest source to every tile reachable from it."""
        return self.shortest(sources)[0]

    def shortest(self, sources: list[Pos]) -> tuple[dict[Pos, float], dict[Pos, Pos | None]]:
        """Driving distances from the nearest source, and each tile's next tile toward it. Cars
        don't drive through houses or single-entrance car parks, except the source ones. Inside a
        car park the drive between two of its tiles is as long as the tiles between them."""
        stops = self.stops()
        parks = self.park_tiles()
        dist: dict[Pos, float] = {s: 0.0 for s in sources}
        parent: dict[Pos, Pos | None] = {s: None for s in sources}
        heap = [(0.0, s) for s in sources]
        heapq.heapify(heap)
        while heap:
            cost, p = heapq.heappop(heap)
            if cost > dist[p] or (p in stops and parent[p] is not None):
                continue
            for q in self.adj.get(p, ()):
                nd = cost + (PARK_DRIVE_FACTOR * octile(p, q) if p in parks and q in parks else drive_length(p, q))
                if nd < dist.get(q, float("inf")) - TRIP_EPSILON:
                    dist[q], parent[q] = nd, p
                    heapq.heappush(heap, (nd, q))
        return dist, parent

    def evaluate(self) -> tuple[dict[tuple[Pos, int], float | None], set[int]]:
        """(trip length from each house to each active destination of its colour, keyed by
        (house tile, destination); destinations that some house of their colour can reach).
        Cars serve every destination of their colour, not just the nearest."""
        state = self.state
        trips: dict[tuple[Pos, int], float | None] = {}
        fed: set[int] = set()
        for dest in state.destinations.values():
            cp = state.carparks.get(dest.carpark)
            if not (dest.active and cp):
                continue
            reach = self.distances(cp.tiles)
            for h in state.houses:
                if h.group == dest.group:
                    trips[(h.pos, dest.key)] = reach.get(h.pos)
                    if h.pos in reach:
                        fed.add(dest.key)
        return trips, fed

    def prune(self, keep: set[Pos]) -> tuple[RoadGraph, list[Pos]]:
        """Drop chains that lead nowhere (tiles left with one link) except tiles in keep and houses
        and car parks. Returns the smaller graph and the dropped tiles, loose end first."""
        adj = {p: set(qs) for p, qs in self.adj.items()}
        stops = self.endpoints()
        removed: list[Pos] = []  # loose end first
        leaves = sorted(p for p, qs in adj.items() if len(qs) <= 1 and p not in keep and p not in stops)
        while leaves:
            p = leaves.pop()
            if p in removed or p not in adj:
                continue
            removed.append(p)
            for q in adj.pop(p):
                adj[q].discard(p)
                if len(adj[q]) <= 1 and q not in keep and q not in stops and q not in removed:
                    leaves.append(q)
        return RoadGraph(self.state, adj), removed

    def on_cycle(self, p: Pos) -> bool:
        """Whether p's neighbours stay connected to each other without p."""
        others = list(self.adj.get(p, ()))
        if len(others) < 2:
            return False
        stops = self.stops()
        seen = {p, others[0]}
        frontier = [others[0]]
        while frontier:
            nxt = []
            for a in frontier:
                if a in stops and a != others[0]:
                    continue
                for b in self.adj.get(a, ()):
                    if b not in seen:
                        seen.add(b)
                        nxt.append(b)
            frontier = nxt
        return all(o in seen for o in others[1:])


def _protected(state: State, terrain: dict[Pos, Terrain]) -> set[Pos]:
    """Road tiles optimisation must not clear: driveways, car park entrances, roundabouts and
    traffic lights, bridges/tunnels and the tiles next to them, and anything that isn't plain
    driveable ground."""
    keep: set[Pos] = {h.access for h in state.houses} | state.roundabouts | state.traffic_lights
    keep.update(t for c in state.roundabouts for t in ring_tiles(c))
    for cp in state.carparks.values():
        keep.update(outside for outside, _ in cp.entrances)
    for p, dirs in state.roads.items():
        t = terrain.get(p)
        if t is None or not t.driveable:
            keep.add(p)
            if t is not None and any(dirs):
                keep.update(neighbor(p, d) for d in range(8))
        elif state.content.get(p, Content.NONE) in BLOCKING:
            keep.add(p)
    return keep


def redundant_tiles(state: State, terrain: dict[Pos, Terrain], max_detour: float = MAX_DETOUR,
                    cut: list[Pos] | None = None) -> list[Pos]:
    """Road tiles that can go without any house losing its colour, any destination losing its
    houses, or any trip growing by more than max_detour tiles: the extra side of a loop, a second
    road into the same car park, and whatever dead-ends once those are gone. Returned in an order
    they can be cleared one by one: each cut, then the chain it leaves, loose end first.
    cut is the pending separation_cut, if the caller already has it ([] when it can't be made)."""
    keep = _protected(state, terrain)
    graph = RoadGraph(state)
    base_trips, base_fed = graph.evaluate()
    base_mixed = mixed_tiles(state, graph)
    if base_mixed and (separation_cut(state, terrain) if cut is None else cut):
        return []  # a road of a colour's own looks redundant until the cut onto the shared road is made
    removed: list[Pos] = []
    while True:
        best = None
        for p in graph.adj:
            if p in keep or not graph.on_cycle(p):
                continue
            smaller, chain = graph.without({p}).prune(keep)
            trips, fed = smaller.evaluate()
            if not base_fed <= fed:
                continue
            if any(t is not None and (trips.get(k) is None or trips[k] > t + max_detour + TRIP_EPSILON)
                   for k, t in base_trips.items()):
                continue
            if len(mixed_tiles(state, smaller)) > len(base_mixed):
                continue  # a road kept to hold colours apart: without it, three share a road
            gain = 1 + len(chain)
            if best is None or gain > best[0]:
                best = (gain, [p] + chain, smaller)
        if best is None:
            return removed
        removed.extend(best[1])
        graph = best[2]


def separation_cut(state: State, terrain: dict[Pos, Terrain],
                   max_detour: float = MAX_SEPARATION_DETOUR) -> list[Pos]:
    """Cars always take the shortest road, so a road of a colour's own next to a shared main road
    only helps once the shortcut onto the main road is gone. Find the road tile (with the chain it
    leaves dead-ended) whose removal puts the fewest colours over two on any tile, as long as every
    house still reaches every destination it reached, and no trip grows by more than max_detour.
    Returns the tiles to clear, cut first, or [] when no cut separates colours."""
    keep = _protected(state, terrain)
    graph = RoadGraph(state)
    mixed = mixed_tiles(state, graph)
    if not mixed:
        return []
    base_trips, base_fed = graph.evaluate()
    best = None
    for p in graph.adj:
        if p in keep or not graph.on_cycle(p):
            continue
        smaller, chain = graph.without({p}).prune(keep)
        after = mixed_tiles(state, smaller)
        if len(after) >= len(mixed):
            continue
        trips, fed = smaller.evaluate()
        if not base_fed <= fed:
            continue
        if any(t is not None and (trips.get(k) is None or trips[k] > t + max_detour + TRIP_EPSILON)
               for k, t in base_trips.items()):
            continue
        longer = sum(trips[k] - t for k, t in base_trips.items() if t is not None)
        score = (len(after), longer)
        if best is None or score < best[0]:
            best = (score, [p] + chain)
    return best[1] if best else []


def separation_candidates(state: State, graph: RoadGraph | None = None) -> list[House]:
    """Houses whose cars drive over a road tile more than two colours use. Colours with the fewest
    such houses come first (moving them clears shared roads with the fewest new roads), and within
    a colour the houses that cross the most of those tiles."""
    routes = house_routes(state, graph)
    mixed = {p for p, by_colour in route_colours(state, graph).items() if len(by_colour) > 2}
    if not mixed:
        return []
    hits = {h: len(tiles & mixed) for h, tiles in routes.items() if tiles & mixed}
    houses = [h for h in state.houses if h.pos in hits]
    per_colour = collections.Counter(h.group for h in houses)
    return sorted(houses, key=lambda h: (per_colour[h.group], -hits[h.pos], h.pos))


def apply_plan(state: State, plan: Plan) -> State:
    """The map as it will be once a plan is drawn: its segments joined, and a house it draws out
    of turned to face the new road."""
    roads = {p: list(r) for p, r in state.roads.items()}
    houses = list(state.houses)
    for p, d in plan.segments:
        for i, h in enumerate(houses):
            if h.pos == p and h.driveway != d:
                roads[p] = [0] * 8
                houses[i] = replace(h, driveway=d)
        roads.setdefault(p, [0] * 8)[d] = 8
        roads.setdefault(neighbor(p, d), [0] * 8)[opposite(d)] = 8
    return replace(state, roads=roads, houses=houses)


def plan_separation(board: Board, house: House, bridges: int, tunnels: int,
                    max_new: int = MAX_SEPARATION_ROAD) -> Plan | None:
    """A road of its own for a house whose cars share a road with two other colours: from the
    house (turned if that helps, and over its own network) to the car park of a destination its
    cars now reach over such a road, using no road tile two other colours already use, with at
    most max_new new tiles. Kept only if fewer tiles carry three colours once it is drawn and the
    separation_cut it allows is made (cars take the shortest road, so the old link may have to go
    first)."""
    board = replace(board, group=house.group)
    state = board.state
    graph = RoadGraph(state)
    mixed = mixed_tiles(state, graph)
    bound = entrance_bound(state, house.group)
    for dest in state.destinations.values():
        cp = state.carparks.get(dest.carpark)
        if not (dest.active and dest.group == house.group and cp):
            continue
        _, parent = graph.shortest(cp.tiles)
        route, p = set(), parent.get(house.pos)
        while p is not None:
            route.add(p)
            p = parent[p]
        if route & mixed:
            plan = _separate_road(board, house, dest, bridges, tunnels, max_new, bound)
            if plan is not None:
                return plan
    return None


def _separate_road(board: Board, house: House, dest: Destination, bridges: int, tunnels: int,
                   max_new: int, bound) -> Plan | None:
    """plan_separation for one destination of the house's colour."""
    state = board.state
    entrances = {outside: d for outside, _, d in entrances_for(state, house.group, dest)}
    if not entrances:
        return None
    found = search(board, _house_starts(board, house), lambda p: p in entrances, bridges, tunnels,
                   goal_cost=board.join_cost, through_goals=True, bound=bound)
    if found is None:
        return None
    path, (bridges_used, tunnels_used) = found
    new_tiles = _new_tiles(board, path, {path[0]})
    if any(board.capped(t) for t in path if board.is_road(t)) or not 0 < new_tiles <= max_new:
        return None
    facing = direction_between(house.pos, path[0])
    turned = facing != house.driveway
    segments = ([(house.pos, facing)] if turned else []) + _road_segments(path)
    segments.append((path[-1], entrances[path[-1]]))  # into the car park (skipped if already joined)
    if not turned and not state.joined(path[0], opposite(facing)):
        segments.append((path[0], opposite(facing)))  # join the house to its driveway tile
    plan = Plan(f"separate road for house {house.pos}", ("separate", house.pos), house.group, segments,
                path, new_tiles, bridges_used, tunnels_used, turned)
    # The new road can also give other colours a shorter way onto shared roads. Keep it only if the
    # map ends up with fewer 3-colour tiles once it is drawn and the cut it allows (if any) is made.
    after = apply_plan(state, plan)
    after = clear_tiles(after, separation_cut(after, board.terrain))
    return plan if len(mixed_tiles(after)) < len(mixed_tiles(state)) else None


def clear_tiles(state: State, tiles: list[Pos]) -> State:
    """The map with the roads on these tiles cleared (and the neighbours' halves pointing at them)."""
    if not tiles:
        return state
    roads = {p: list(r) for p, r in state.roads.items()}
    for p in tiles:
        roads[p] = [0] * 8
        for d in range(8):
            q = neighbor(p, d)
            if q in roads:
                roads[q][opposite(d)] = 0
    return replace(state, roads=roads)


def crossings(state: State, terrain: dict[Pos, Terrain]) -> list[tuple[frozenset[Pos], Upgrade]]:
    """Bridges and tunnels on the map: each a set of joined deck tiles (road over water or under a
    mountain), with the upgrade it took."""
    decks = {p for p, r in state.roads.items() if any(r) and p in terrain and not terrain[p].driveable}
    out, seen = [], set()
    for start in sorted(decks):
        if start in seen:
            continue
        deck, stack = set(), [start]
        while stack:
            p = stack.pop()
            if p in deck:
                continue
            deck.add(p)
            stack += [neighbor(p, d) for d in range(8) if state.joined(p, d) and neighbor(p, d) in decks]
        seen |= deck
        out.append((frozenset(deck), Upgrade.BRIDGE if terrain[start].water else Upgrade.TUNNEL))
    return out


def reclaimable_crossings(state: State, terrain: dict[Pos, Terrain],
                          graph: RoadGraph | None = None) -> list[tuple[frozenset[Pos], Upgrade, int, float]]:
    """Bridges and tunnels that can be taken down (to get the upgrade back) without any destination
    losing the last house of its colour that reaches it, and without any connected house losing its
    last destination. Each comes with how many house-to-destination drives it ends and the longest
    trip it makes longer, least harmful first."""
    graph = graph or RoadGraph(state)
    trips, fed = graph.evaluate()
    served = {h for (h, _), t in trips.items() if t is not None}
    out = []
    for deck, kind in crossings(state, terrain):
        after, fed_after = graph.without(set(deck)).evaluate()
        if not fed <= fed_after or not served <= {h for (h, _), t in after.items() if t is not None}:
            continue
        lost = sum(1 for k, t in trips.items() if t is not None and after.get(k) is None)
        longer = max([after[k] - t for k, t in trips.items() if t is not None and after.get(k) is not None] or [0.0])
        out.append((deck, kind, lost, longer))
    return sorted(out, key=lambda c: (c[2], c[3], min(c[0])))


def house_spur(state: State, terrain: dict[Pos, Terrain], house: House, graph: RoadGraph | None = None) -> list[Pos]:
    """The road only this house uses: from its driveway tile up to the first junction."""
    graph = graph or RoadGraph(state)
    if house.pos not in graph.adj.get(house.access, ()):
        return []  # not joined to its driveway tile
    keep = _protected(state, terrain) - {house.access}
    spur, prev, cur = [], house.pos, house.access
    while cur not in keep and cur not in spur and len(graph.adj.get(cur, ())) == 2:
        spur.append(cur)
        prev, cur = cur, next(iter(graph.adj[cur] - {prev}))
    return spur


def plan_shortcut(state: State, terrain: dict[Pos, Terrain], house: House, refused: set,
                  bridges: int, tunnels: int, graph: RoadGraph, base_trips: dict,
                  load: dict[Pos, int] | None = None, colours: dict[Pos, dict[int, int]] | None = None,
                  jam: dict[Pos, float] | None = None) -> Plan | None:
    """A cheaper connection for an already connected house: plan as if its private road were gone,
    and keep the plan if it needs fewer new tiles than that road has and no trip of the house gets
    longer (MAX_DETOUR). The search uses the same load and colour costs as any new connection.
    Once built, the old road dead-ends (or forms a loop) and gets cleared."""
    mine = {k: t for k, t in base_trips.items() if k[0] == house.pos and t is not None}
    if not mine:
        return None
    spur = house_spur(state, terrain, house, graph)
    if not spur:
        return None
    spur_set = set(spur)
    roads = dict(state.roads)
    for t in spur:
        roads[t] = [0] * 8
        for d in range(8):
            q = neighbor(t, d)
            if q in roads and q not in spur_set and roads[q][opposite(d)]:
                roads[q] = list(roads[q])
                roads[q][opposite(d)] = 0
    reduced = replace(state, roads=roads)
    net = Network(reduced)
    roots = group_roots(reduced, net)
    board = Board(reduced, terrain, refused, load=dict(load or {}), colours=dict(colours or {}), jam=dict(jam or {}))
    plan = plan_house(board, house, net, roots, bridges, tunnels, through_goals=False)
    if plan is None:
        return None
    # Against the real map: tiles the new road adds (a house's driveway tile is free), and old
    # tiles it leaves unused, which get cleared afterwards.
    real_new = sum(1 for t in plan.tiles[1:] if not state.has_road(t))
    freed = len(spur_set - set(plan.tiles))
    if real_new >= freed:
        return None
    # Trips on the map after the change: the reduced map plus the new road and the house on it.
    after = RoadGraph(reduced)
    chain = [house.pos] + plan.tiles
    for a, b in zip(chain, chain[1:]):
        after.adj.setdefault(a, set()).add(b)
        after.adj.setdefault(b, set()).add(a)
    if plan.tiles[-1] in {outside for cp in state.carparks.values() for outside, _ in cp.entrances}:
        for cp in state.carparks.values():
            for outside, d in cp.entrances:
                if outside == plan.tiles[-1]:
                    park = neighbor(outside, opposite(d))
                    after.adj.setdefault(outside, set()).add(park)
                    after.adj.setdefault(park, set()).add(outside)
    trips_after, _ = after.evaluate()
    if len(mixed_tiles(state, after)) > len(mixed_tiles(state, graph)):
        return None  # it would put a third colour on a road
    if any(trips_after.get(k) is None or trips_after[k] > t + MAX_DETOUR + TRIP_EPSILON for k, t in mine.items()):
        return None
    plan.label = f"shortcut for house {house.pos}"
    plan.key = ("shortcut", house.pos)
    plan.new_tiles = real_new
    plan.saves = freed - real_new
    return plan


# Traffic ------------------------------------------------------------------------------------

def mixed_tiles(state: State, graph: RoadGraph | None = None) -> set[Pos]:
    """Road tiles that more than two colours' cars drive over."""
    return {p for p, by_colour in route_colours(state, graph).items() if len(by_colour) > 2}


def route_loads(state: State, graph: RoadGraph | None = None,
                colours: dict[Pos, dict[int, int]] | None = None) -> dict[Pos, int]:
    """For each road tile, how many houses drive over it on their way to the nearest destination
    of their colour. The game routes every car along the free-flow shortest road and never around
    traffic, so this is where the cars actually go (shortest by tiles, as an approximation).
    Counts each house's drive to its nearest destination only (traffic thresholds are tuned to
    that). Pass colours from route_colours(..., every_destination=False) to reuse them."""
    if colours is None:
        colours = route_colours(state, graph, every_destination=False)
    return {p: sum(by_colour.values()) for p, by_colour in colours.items()}


def route_colours(state: State, graph: RoadGraph | None = None,
                  every_destination: bool | None = None) -> dict[Pos, dict[int, int]]:
    """For each road tile, colour -> houses whose cars drive over it. Cars serve every destination
    of their colour, so by default (COUNT_EVERY_DESTINATION) a house counts on its shortest drive
    to each active destination of its colour, once per tile; with every_destination=False only on
    its drive to the nearest one."""
    out: dict[Pos, dict[int, int]] = {}
    groups = {h.pos: h.group for h in state.houses}
    for house, tiles in house_routes(state, graph, every_destination).items():
        for p in tiles:
            by_colour = out.setdefault(p, {})
            by_colour[groups[house]] = by_colour.get(groups[house], 0) + 1
    return out


def house_routes(state: State, graph: RoadGraph | None = None,
                 every_destination: bool | None = None) -> dict[Pos, set[Pos]]:
    """For each house that reaches its colour, the road tiles its cars drive over (its own tile and
    the car park's left out): the shortest drive to each active destination of its colour, or with
    every_destination=False to the nearest one. Defaults to COUNT_EVERY_DESTINATION."""
    graph = graph or RoadGraph(state)
    if every_destination is None:
        every_destination = COUNT_EVERY_DESTINATION
    out: dict[Pos, set[Pos]] = {}
    for group in {h.group for h in state.houses}:
        parks = [state.carparks[d.carpark].tiles for d in state.destinations.values()
                 if d.active and d.group == group and d.carpark in state.carparks]
        source_sets = parks if every_destination else [[t for tiles in parks for t in tiles]]
        for sources in source_sets:
            _, parent = graph.shortest(sources)
            for h in state.houses:
                if h.group != group or h.pos not in parent:
                    continue
                tiles = out.setdefault(h.pos, set())
                p = parent[h.pos]
                while p is not None and parent[p] is not None:
                    tiles.add(p)
                    p = parent[p]
    return out


def _add_routes(state: State, graph: RoadGraph, group: int, sources: list[Pos], load: dict[Pos, int]):
    """Count, on each road tile, the houses of a colour whose shortest drive to the sources uses it."""
    _, parent = graph.shortest(sources)
    for h in state.houses:
        if h.group != group or h.pos not in parent:
            continue
        p = parent[h.pos]
        while p is not None and parent[p] is not None:
            load[p] = load.get(p, 0) + 1
            p = parent[p]


def junctions_by_load(state: State, load: dict[Pos, int], min_load: int) -> list[Pos]:
    """Junction tiles (three or more roads meet) carrying at least min_load houses, busiest first.
    Those are where a roundabout or traffic light helps. Tiles already part of a roundabout (its
    centre or ring) or with a traffic light are left out."""
    taken = state.roundabouts | state.traffic_lights
    taken |= {t for c in state.roundabouts for t in ring_tiles(c)}
    out = []
    for p, n in load.items():
        if n < min_load or state.links(p) < 3 or state.content.get(p, Content.NONE) != Content.NONE:
            continue
        if p in taken:
            continue
        out.append((n, p))
    return [p for _, p in sorted(out, reverse=True)]


def plan_bypass(board: Board, dest: Destination, bridges: int, tunnels: int) -> Plan | None:
    """A new road from a destination's car park entrance straight to a far part of its network,
    for a destination whose cars take too long to arrive. All the new road is fresh tiles; it ends
    on an existing road tile that houses drive through on their way to this destination, and
    shortens their trip by MIN_BYPASS_SAVING or more. (Ending anywhere else would shorten nobody's
    trip, and the loop-clearing pass would take it straight back down.)"""
    state = board.state
    trips = trip_lengths(state, dest.group, dest)
    on_route: dict[Pos, int] = {}
    cp = state.carparks.get(dest.carpark)
    if cp:
        _add_routes(state, RoadGraph(state), dest.group, cp.tiles, on_route)
    starts = [(outside, opposite(into), 0.0) for outside, _, into in entrances_for(state, dest.group, dest)
              if state.has_road(outside)]
    if not starts or not trips:
        return None

    goals = {p for p, t in trips.items()
             if p in on_route and state.content.get(p) not in BLOCKING
             and t - min(octile(p, s[0]) for s in starts) >= MIN_BYPASS_SAVING}
    if not goals:
        return None
    longest = max(trips[p] for p in goals)
    fresh = replace(board, road_allowed=goals | {s[0] for s in starts})
    found = search(fresh, starts, lambda p: p in goals, bridges, tunnels,
                   goal_cost=lambda p: COST_BYPASS_TRIP * (longest - trips[p]))
    if found is None:
        return None
    path, (bridges_used, tunnels_used) = found
    saving = trips[path[-1]] - sum(drive_length(a, b) for a, b in zip(path, path[1:]))
    if saving < MIN_BYPASS_SAVING or len(path) < 3:
        return None
    return Plan(f"bypass to destination {dest.tiles[0]}", ("bypass", dest.key), dest.group, _road_segments(path),
                path, _new_tiles(board, path), bridges_used, tunnels_used)
