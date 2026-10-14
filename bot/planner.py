"""Road planning: which houses already reach a destination of their colour, and the shortest new
roads that would connect the ones that don't.

The board is a grid; a road tile stores which of its 8 directions carry road. Two tiles are joined
when both point at each other. Houses and car parks own a stub pointing at their access tile, so
they join the network through the same rule. Roads may run diagonally, and a house can be turned
to face any of its 8 neighbours (drawing out of a house turns its driveway, as a drag does).
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field, replace

from game import (DIRS, Content, Destination, House, Pos, State, Terrain, Upgrade, direction_between, neighbor,
                  opposite, ring_tiles)

BLOCKING = {Content.HOUSE, Content.DESTINATION, Content.CARPARK, Content.BOAT_TERMINAL}

# Path costs, in road tiles. New road tiles dominate everything else, so a plan never spends a
# tile it doesn't need; the small terms only break ties between equally short roads. A diagonal
# step uses one tile like a straight one, which is what makes diagonal roads shorter.
COST_EMPTY = 1.0
COST_TREE = 1.05  # building over a tree clears it; same tile cost
COST_EXISTING_ROAD = 0.1  # road that exists costs no tiles, but cutting across other networks adds junctions
# Cost of a bend, by its size in 45° steps. Cars slow down for 90° (and sharper) turns but not for
# 45° bends, so a right angle costs a good fraction of a tile while a diagonal bend is just a tie-break.
TURN_COST = (0.0, 0.01, 0.3)
COST_TURN_HOUSE = 0.05  # turning a house is free in the game, but don't churn for nothing
COST_BRIDGE = 4.0  # on top of the tiles it spans; bridges are scarce
MAX_CROSSING = 5
MAX_TURN = 2  # in 45° steps: no road bends sharper than a right angle
# Tie-break between equally short roads: join the network nearer the destination.
COST_REMAINING_TRIP = 0.01
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
# A house of a colour with a destination close to overcrowding will spend up to this many extra
# tiles to join that destination's network instead of another one of its colour.
COST_OTHER_NETWORK = 3.0
# A bypass (new road from a car park entrance straight to a far part of its network) must shorten
# some trip by at least this many tiles.
MIN_BYPASS_SAVING = 4
# Among bypasses, one more new tile is worth it for every two tiles it takes off the trip.
COST_BYPASS_TRIP = 0.5
# Optimising existing roads may lengthen a house's trip by at most this many tiles.
MAX_DETOUR_SHORTCUT = 2
MAX_DETOUR_LOOP = 1


def turn_size(a: int, b: int) -> int:
    """Angle between two directions in 45° steps (0-4)."""
    diff = (b - a) % 8
    return min(diff, 8 - diff)


class Network:
    """Union-find over tiles joined by roads (including house and car park stubs)."""

    def __init__(self, state: State):
        self._parent: dict[Pos, Pos] = {}
        for p, dirs in state.roads.items():
            for d, s in enumerate(dirs):
                if s and state.road_toward(neighbor(p, d), opposite(d)):
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
    crossings: int = 0  # bridges/tunnels started
    turned_house: bool = False
    saves: int = 0  # for shortcuts: tiles freed by replacing the old road


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
    rings: list[set[Pos]] = field(init=False, repr=False)  # each roundabout's 4 ring tiles

    def __post_init__(self):
        self.rings = [set(ring_tiles(c)) for c in self.state.roundabouts]

    def is_road(self, p: Pos) -> bool:
        return self.state.has_road(p) or p in self.planned

    def load_cost(self, p: Pos) -> float:
        return COST_LOAD * max(0, self.load.get(p, 0) - LOAD_FREE)

    def shared_cost(self, p: Pos, base: float = COST_SHARED) -> float:
        """Extra cost of putting this colour's cars on a road tile other colours use."""
        if self.group is None:
            return 0.0
        if self.planned.get(p, self.group) != self.group:
            return base
        others = sum(n for g, n in self.colours.get(p, {}).items() if g != self.group)
        return base + COST_SHARED_LOAD * others if others else 0.0

    def join_cost(self, p: Pos) -> float:
        """Extra cost of ending a new road on an existing road tile: its traffic, other colours on
        it, and a new junction."""
        if not self.state.has_road(p):
            return 0.0
        junction = COST_JUNCTION if self.state.links(p) >= 2 else 0.0
        return self.load_cost(p) + self.shared_cost(p, COST_SHARED_JOIN) + junction

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

    def step_cost(self, p: Pos) -> float:
        if self.is_road(p):
            return COST_EXISTING_ROAD + self.load_cost(p) + self.shared_cost(p)
        if self.state.content.get(p) == Content.TREE:
            return COST_TREE
        return COST_EMPTY

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


def search(board: Board, starts: list[Start], is_goal, bridges: int, tunnels: int, goal_cost=None):
    """Dijkstra over (tile, heading), so bends can be limited. Steps go in all 8 directions; a
    straight cardinal run of water or mountain tiles can be crossed with a bridge or tunnel, or for
    free where a deck is already started. With goal_cost(tile) the goal minimising
    path cost + goal_cost wins, otherwise the nearest one.
    Returns (path including crossed tiles, crossings started) or None."""
    counter = itertools.count()
    heap, best, prev, used = [], {}, {}, {}
    for pos, heading, initial in starts:
        key = (pos, heading)
        if board.passable(pos) and initial < best.get(key, float("inf")):
            best[key], used[key] = initial, 0
            heapq.heappush(heap, (initial, next(counter), key))

    def relax(from_key, to_key, spanned, cost, crossings):
        if cost < best.get(to_key, float("inf")):
            best[to_key], prev[to_key], used[to_key] = cost, (from_key, spanned), crossings
            heapq.heappush(heap, (cost, next(counter), to_key))

    found, found_total = None, float("inf")
    while heap:
        cost, _, key = heapq.heappop(heap)
        if cost > best.get(key, float("inf")):
            continue
        if cost >= found_total:
            break  # nothing cheaper can follow
        p, heading = key
        if is_goal(p):
            total = cost + (goal_cost(p) if goal_cost else 0.0)
            if total < found_total:
                found, found_total = key, total
            if goal_cost is None:
                break
            continue
        for d in range(8):
            bend = turn_size(heading, d)
            if bend > MAX_TURN or not board.move_allowed(p, d):
                continue
            q = neighbor(p, d)
            base = cost + TURN_COST[bend]
            if board.passable(q):
                relax(key, (q, d), [], base + board.step_cost(q), used[key])
                continue
            if d % 2:
                continue  # bridges and tunnels run straight along an axis
            for kind, budget in ((Upgrade.BRIDGE, bridges), (Upgrade.TUNNEL, tunnels)):
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
                if not started and used[key] >= budget:
                    continue
                span_cost = sum(COST_EXISTING_ROAD if r and any(r) else COST_EMPTY for r in roads)
                step = base + (0.0 if started else COST_BRIDGE) + span_cost + board.step_cost(landing)
                relax(key, (landing, d), spanned, step, used[key] + (0 if started else 1))

    if found is None:
        return None
    path, key = [found[0]], found
    while key in prev:
        key, spanned = prev[key]
        path.extend(reversed(spanned))
        path.append(key[0])
    path.reverse()
    return path, used[found]


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
               bridges: int, tunnels: int, pressured: set[Pos] = frozenset()) -> Plan | None:
    """Shortest road from a house (turned to whichever side is best) to its colour's network or
    to a car park entrance of its colour. With pressured (network roots holding a destination close
    to overcrowding), joining one of those networks is preferred by up to COST_OTHER_NETWORK tiles,
    since cars only reach destinations on their own network."""
    board = replace(board, group=house.group)
    state = board.state
    goal_roots = roots.get(house.group, set())
    entrances = {outside: d for outside, _, d in entrances_for(state, house.group)}
    prefer = goal_roots & set(pressured)
    entrance_root = {outside: net.find(park) for outside, park, _ in entrances_for(state, house.group)}

    def network_cost(p: Pos) -> float:
        if not prefer:
            return 0.0
        root = entrance_root[p] if p in entrances else net.find(p)
        return 0.0 if root in prefer else COST_OTHER_NETWORK

    def on_network(p: Pos) -> bool:
        if board.planned.get(p) == house.group:
            return True
        return state.has_road(p) and net.find(p) in goal_roots and state.content.get(p) not in BLOCKING

    trips = trip_lengths(state, house.group)

    def remaining_trip(p: Pos) -> float:
        if board.planned.get(p) == house.group and not state.has_road(p):
            return 0.0
        trip = 0.0 if p in entrances else COST_REMAINING_TRIP * trips.get(p, 0)
        return trip + board.join_cost(p) + network_cost(p)

    found = search(board, _house_starts(board, house), lambda p: on_network(p) or p in entrances,
                   bridges, tunnels, goal_cost=remaining_trip)
    if found is None:
        return None
    path, crossings = found
    facing = direction_between(house.pos, path[0])
    turned = facing != house.driveway
    segments = [(house.pos, facing)] if turned else []  # drawing out of the house turns it
    segments += _road_segments(path)
    end = path[-1]
    if not on_network(end):
        segments.append((end, entrances[end]))  # into the car park
    if len(path) == 1 and not turned:
        # The driveway tile already reaches the network; just join the house to it.
        segments.append((path[0], opposite(facing)))
    return Plan(f"house {house.pos}", ("house", house.pos), house.group, segments, path,
                _new_tiles(board, path, {path[0]}), crossings, turned)


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

    starts = [(outside, opposite(into), 0.0) for outside, into in into_park.items()]
    found = search(board, starts, is_goal, bridges, tunnels)
    if found is None:
        return None
    path, crossings = found
    entrance, end = path[0], path[-1]
    segments = [(entrance, into_park[entrance])] + _road_segments(path)
    turned = False
    if end in house_at and not (state.has_road(end) and net.find(end) in house_roots):
        house, facing = house_at[end]
        if facing != house.driveway:
            segments.append((house.pos, facing))
            turned = True
    return Plan(f"destination {dest.tiles[0]}", ("destination", dest.key), dest.group, segments, path,
                _new_tiles(board, path), crossings, turned)


# Network upkeep -----------------------------------------------------------------------------

def trip_lengths(state: State, group: int, dest: Destination | None = None) -> dict[Pos, int]:
    """Tiles of existing road to drive from each road tile to an active car park of a colour
    (or to one destination's car park)."""
    frontier = []
    dist: dict[Pos, int] = {}
    for outside, park, _ in entrances_for(state, group, dest):
        for t in (outside, park):
            if state.has_road(t) and t not in dist:
                dist[t] = 0
                frontier.append(t)
    while frontier:
        nxt = []
        for p in frontier:
            for d, s in enumerate(state.roads.get(p, ())):
                q = neighbor(p, d)
                if s and q not in dist and state.road_toward(q, opposite(d)):
                    dist[q] = dist[p] + 1
                    nxt.append(q)
        frontier = nxt
    return dist


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
    road was refused halfway). Clearing them returns their tiles to the inventory.
    Bridges and tunnels, and the tiles next to them, are left alone."""
    keep: set[Pos] = {h.access for h in state.houses} | state.roundabouts | state.traffic_lights
    keep.update(t for c in state.roundabouts for t in ring_tiles(c))
    for cp in state.carparks.values():
        keep.update(outside for outside, _ in cp.entrances)
    for p, dirs in state.roads.items():
        t = terrain.get(p)
        if t is not None and not t.driveable and any(dirs):
            keep.add(p)
            keep.update(neighbor(p, d) for d in range(8))

    def prunable(p: Pos) -> bool:
        return (p not in keep and state.content.get(p, Content.NONE) not in BLOCKING
                and terrain.get(p) is not None and terrain[p].driveable)

    links = {p: {d for d, s in enumerate(dirs) if s and state.road_toward(neighbor(p, d), opposite(d))}
             for p, dirs in state.roads.items() if any(dirs)}
    leaves = sorted(p for p, ds in links.items() if len(ds) <= 1 and prunable(p))
    removed: list[Pos] = []  # loose end first: each tile can only be cleared once it is a dead end
    while leaves:
        p = leaves.pop()
        if p in removed:
            continue
        removed.append(p)
        for d in links[p]:
            q = neighbor(p, d)
            if q in links:
                links[q].discard(opposite(d))
                if len(links[q]) <= 1 and q not in removed and prunable(q):
                    leaves.append(q)
    return removed


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
    Cars don't drive through houses or car parks, so those are only ever endpoints."""

    def __init__(self, state: State, adj: dict[Pos, set[Pos]] | None = None):
        self.state = state
        if adj is not None:
            self.adj = adj
            return
        self.adj = {}
        for p, dirs in state.roads.items():
            for d, s in enumerate(dirs):
                q = neighbor(p, d)
                if s and state.road_toward(q, opposite(d)):
                    self.adj.setdefault(p, set()).add(q)
        for cp in state.carparks.values():
            for a in cp.tiles:
                self.adj.setdefault(a, set()).update(t for t in cp.tiles if t != a)

    def without(self, tiles: set[Pos]) -> RoadGraph:
        adj = {p: qs - tiles for p, qs in self.adj.items() if p not in tiles}
        return RoadGraph(self.state, adj)

    def endpoints(self) -> set[Pos]:
        out = {h.pos for h in self.state.houses}
        for cp in self.state.carparks.values():
            out.update(cp.tiles)
        return out

    def distances(self, sources: list[Pos]) -> dict[Pos, int]:
        stops = self.endpoints()
        dist = {s: 0 for s in sources}
        frontier = list(sources)
        while frontier:
            nxt = []
            for p in frontier:
                if p in stops and dist[p] > 0:
                    continue
                for q in self.adj.get(p, ()):
                    if q not in dist:
                        dist[q] = dist[p] + 1
                        nxt.append(q)
            frontier = nxt
        return dist

    def evaluate(self) -> tuple[dict[tuple[Pos, int], int | None], set[int]]:
        """(trip length from each house to each active destination of its colour, keyed by
        (house tile, destination); destinations that some house of their colour can reach).
        Cars serve every destination of their colour, not just the nearest."""
        state = self.state
        trips: dict[tuple[Pos, int], int | None] = {}
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
        """Drop chains that lead nowhere (tiles left with one link) except tiles in keep."""
        adj = {p: set(qs) for p, qs in self.adj.items()}
        stops = self.endpoints()
        removed: list[Pos] = []  # loose end first
        leaves = [p for p, qs in adj.items() if len(qs) <= 1 and p not in keep and p not in stops]
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
        stops = self.endpoints()
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


def redundant_tiles(state: State, terrain: dict[Pos, Terrain], max_detour: int = MAX_DETOUR_LOOP) -> list[Pos]:
    """Road tiles that can go without any house losing its colour, any destination losing its
    houses, or any trip growing by more than max_detour tiles: the extra side of a loop, a second
    road into the same car park, and whatever dead-ends once those are gone. Returned in an order
    they can be cleared one by one: each cut, then the chain it leaves, loose end first."""
    keep = _protected(state, terrain)
    graph = RoadGraph(state)
    base_trips, base_fed = graph.evaluate()
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
            if any(t is not None and (trips.get(k) is None or trips[k] > t + max_detour)
                   for k, t in base_trips.items()):
                continue
            gain = 1 + len(chain)
            if best is None or gain > best[0]:
                best = (gain, [p] + chain, smaller)
        if best is None:
            return removed
        removed.extend(best[1])
        graph = best[2]


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
                  bridges: int, tunnels: int, graph: RoadGraph, base_trips: dict) -> Plan | None:
    """A cheaper connection for an already connected house: plan as if its private road were gone,
    and keep the plan if it needs fewer new tiles than that road has (and the trip barely grows).
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
    plan = plan_house(Board(reduced, terrain, refused), house, net, roots, bridges, tunnels)
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
    if any(trips_after.get(k) is None or trips_after[k] > t + MAX_DETOUR_SHORTCUT for k, t in mine.items()):
        return None
    plan.label = f"shortcut for house {house.pos}"
    plan.key = ("shortcut", house.pos)
    plan.new_tiles = real_new
    plan.saves = freed - real_new
    return plan


# Traffic ------------------------------------------------------------------------------------

def route_loads(state: State, graph: RoadGraph | None = None) -> dict[Pos, int]:
    """For each road tile, how many houses drive over it on their way to the nearest destination
    of their colour. The game routes every car along the free-flow shortest road and never around
    traffic, so this is where the cars actually go (shortest by tiles, as an approximation)."""
    load: dict[Pos, int] = {}
    for p, by_colour in route_colours(state, graph).items():
        load[p] = sum(by_colour.values())
    return load


def route_colours(state: State, graph: RoadGraph | None = None) -> dict[Pos, dict[int, int]]:
    """route_loads split by colour: for each road tile, colour -> houses routed over it."""
    graph = graph or RoadGraph(state)
    out: dict[Pos, dict[int, int]] = {}
    for group in {h.group for h in state.houses}:
        sources = [t for d in state.destinations.values() if d.active and d.group == group
                   for t in (state.carparks[d.carpark].tiles if d.carpark in state.carparks else ())]
        load: dict[Pos, int] = {}
        _add_routes(state, graph, group, sources, load)
        for p, n in load.items():
            out.setdefault(p, {})[group] = n
    return out


def _add_routes(state: State, graph: RoadGraph, group: int, sources: list[Pos], load: dict[Pos, int]):
    """Count, on each road tile, the houses of a colour whose shortest road to the sources uses it."""
    stops = graph.endpoints()
    parent: dict[Pos, Pos | None] = {s: None for s in sources}
    frontier = list(sources)
    while frontier:
        nxt = []
        for p in frontier:
            if p in stops and parent[p] is not None:
                continue
            for q in graph.adj.get(p, ()):
                if q not in parent:
                    parent[q] = p
                    nxt.append(q)
        frontier = nxt
    for h in state.houses:
        if h.group != group or h.pos not in parent:
            continue
        p = parent[h.pos]
        while p is not None and parent[p] is not None:
            load[p] = load.get(p, 0) + 1
            p = parent[p]


def junctions_by_load(state: State, load: dict[Pos, int], min_load: int) -> list[Pos]:
    """Junction tiles (three or more roads meet) carrying at least min_load houses, busiest first.
    Those are where a roundabout or traffic light helps."""
    out = []
    for p, n in load.items():
        if n < min_load or state.links(p) < 3 or state.content.get(p, Content.NONE) != Content.NONE:
            continue
        if p in state.roundabouts or p in state.traffic_lights:
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

    def tiles_apart(a: Pos, b: Pos) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))  # a diagonal step is one tile, like a straight one

    goals = {p for p, t in trips.items()
             if p in on_route and state.content.get(p) not in BLOCKING
             and t - min(tiles_apart(p, s[0]) for s in starts) >= MIN_BYPASS_SAVING}
    if not goals:
        return None
    longest = max(trips[p] for p in goals)
    fresh = replace(board, road_allowed=goals | {s[0] for s in starts})
    found = search(fresh, starts, lambda p: p in goals, bridges, tunnels,
                   goal_cost=lambda p: COST_BYPASS_TRIP * (longest - trips[p]))
    if found is None:
        return None
    path, crossings = found
    saving = trips[path[-1]] - (len(path) - 1)
    if saving < MIN_BYPASS_SAVING or len(path) < 3:
        return None
    return Plan(f"bypass to destination {dest.tiles[0]}", ("bypass", dest.key), dest.group, _road_segments(path),
                path, _new_tiles(board, path), crossings, saves=0)
