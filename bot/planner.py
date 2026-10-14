"""Road planning: which houses already reach a destination of their colour, and the cheapest new
roads that would connect the ones that don't.

The board is a grid; a road tile stores which of its 8 directions carry road. Two tiles are joined
when both point at each other. Houses and car parks own a stub pointing at their access tile, so
they join the network through the same rule.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from game import (CARDINALS, Content, Destination, House, Pos, State, Terrain, Upgrade, direction_between,
                  neighbor, opposite)

BLOCKING = {Content.HOUSE, Content.DESTINATION, Content.CARPARK, Content.BOAT_TERMINAL}

# Path costs, in "road tiles". Reusing road is cheap but not free so paths stay short.
COST_EMPTY = 1.0
COST_TREE = 1.2
COST_EXISTING_ROAD = 0.3
COST_BRIDGE = 4.0  # on top of the tiles it spans; bridges are scarce
MAX_CROSSING = 5
# Joining a colour's network far from its destinations makes every trip long, so each tile still
# to drive on existing road to the nearest destination counts this much at the join point.
COST_REMAINING_TRIP = 0.5


class Network:
    """Union-find over tiles joined by roads (including house and car park stubs)."""

    def __init__(self, state: State, extra: dict[Pos, Pos] | None = None):
        self._parent: dict[Pos, Pos] = {}
        for p, dirs in state.roads.items():
            for d, s in enumerate(dirs):
                if not s:
                    continue
                q = neighbor(p, d)
                back = state.roads.get(q)
                if back and back[opposite(d)]:
                    self.union(p, q)
        for cp in state.carparks.values():
            for t in cp.tiles[1:]:
                self.union(cp.tiles[0], t)
        for a, b in (extra or {}).items():
            self.union(a, b)

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
    crossings: int = 0  # bridges/tunnels used


@dataclass
class Board:
    """Static-per-tick view used by the path search, with an overlay for roads planned this tick."""

    state: State
    terrain: dict[Pos, Terrain]
    planned: dict[Pos, int] = field(default_factory=dict)  # tile -> group whose network it joins

    def is_road(self, p: Pos) -> bool:
        return self.state.has_road(p) or p in self.planned

    def passable(self, p: Pos) -> bool:
        if not self.state.in_playable(p):
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
            return COST_EXISTING_ROAD
        if self.state.content.get(p) == Content.TREE:
            return COST_TREE
        return COST_EMPTY


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


def entrances_for(state: State, group: int) -> list[tuple[Pos, Pos, int]]:
    """(outside tile, car park tile, direction outside->car park) for active destinations of a colour."""
    out = []
    for dest in state.destinations.values():
        cp = state.carparks.get(dest.carpark)
        if not (dest.active and dest.group == group and cp):
            continue
        for outside, d in cp.entrances:
            out.append((outside, neighbor(outside, opposite(d)), opposite(d)))
    return out


def search(board: Board, starts: list[Pos], is_goal, bridges: int, tunnels: int, goal_cost=None):
    """Dijkstra over the grid from any start tile to a goal tile. Steps are cardinal; a straight
    run of water or mountain tiles can be crossed when a bridge or tunnel is left. With
    goal_cost(tile), the goal minimising path cost + goal_cost is chosen instead of the nearest.
    Returns (path including crossed tiles, crossings used) or None."""
    counter = itertools.count()
    heap, best, prev, crossings = [], {}, {}, {}
    for s in starts:
        if board.passable(s):
            best[s], crossings[s] = 0.0, 0
            heapq.heappush(heap, (0.0, next(counter), s))
    found, found_total = None, float("inf")
    while heap:
        cost, _, p = heapq.heappop(heap)
        if cost > best.get(p, float("inf")):
            continue
        if cost >= found_total:
            break  # nothing cheaper can follow
        if is_goal(p):
            total = cost + (goal_cost(p) if goal_cost else 0.0)
            if total < found_total:
                found, found_total = p, total
            if goal_cost is None:
                break
            continue
        for d in CARDINALS:
            q = neighbor(p, d)
            if board.passable(q):
                relax(heap, counter, best, prev, crossings, p, q, [], cost + board.step_cost(q), crossings[p])
                continue
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
                # Deck already on the span: finishing a started bridge needs no new one.
                started = any(r and any(r) for r in roads)
                if not started and crossings[p] >= budget:
                    continue
                span_cost = sum(COST_EXISTING_ROAD if r and any(r) else COST_EMPTY for r in roads)
                step = cost + (0.0 if started else COST_BRIDGE) + span_cost + board.step_cost(landing)
                relax(heap, counter, best, prev, crossings, p, landing, spanned, step,
                      crossings[p] + (0 if started else 1))
    if found is None:
        return None
    path = [found]
    while path[-1] in prev:
        before, spanned = prev[path[-1]]
        path.extend(reversed(spanned))
        path.append(before)
    path.reverse()
    return path, crossings[found]


def relax(heap, counter, best, prev, crossings, p, q, spanned, cost, used):
    if cost < best.get(q, float("inf")):
        best[q] = cost
        prev[q] = (p, spanned)
        crossings[q] = used
        heapq.heappush(heap, (cost, next(counter), q))


def _road_segments(path: list[Pos]) -> list[tuple[Pos, int]]:
    return [(a, direction_between(a, b)) for a, b in zip(path, path[1:])]


def _new_tiles(board: Board, path: list[Pos]) -> int:
    return sum(1 for t in path if not board.is_road(t))


def plan_house(board: Board, house: House, net: Network, roots: dict[int, set[Pos]],
               bridges: int, tunnels: int) -> Plan | None:
    """Cheapest road from a house's access tile to its colour's network or a car park entrance."""
    state = board.state
    goal_roots = roots.get(house.group, set())
    entrances = {outside: d for outside, _, d in entrances_for(state, house.group)}

    def on_network(p: Pos) -> bool:
        if board.planned.get(p) == house.group:
            return True
        return state.has_road(p) and net.find(p) in goal_roots and state.content.get(p) not in BLOCKING

    trips = trip_lengths(state, house.group)

    def remaining_trip(p: Pos) -> float:
        return 0.0 if p in entrances else COST_REMAINING_TRIP * trips.get(p, 0)

    found = search(board, [house.access], lambda p: on_network(p) or p in entrances, bridges, tunnels,
                   goal_cost=remaining_trip)
    if found is None:
        return None
    path, crossings = found
    segments = _road_segments(path)
    end = path[-1]
    if not on_network(end):
        segments.append((end, entrances[end]))  # into the car park
    if len(path) == 1:
        # The access tile already reaches the network; just join the house's driveway to it.
        segments.append((house.access, opposite(house.driveway)))
    return Plan(f"house {house.pos}", ("house", house.pos), house.group, segments, path,
                _new_tiles(board, path), crossings)


def trip_lengths(state: State, group: int) -> dict[Pos, int]:
    """Tiles of existing road to drive from each road tile to an active car park of a colour."""
    frontier = []
    dist: dict[Pos, int] = {}
    for outside, park, _ in entrances_for(state, group):
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


def plan_destination(board: Board, dest: Destination, net: Network, bridges: int, tunnels: int) -> Plan | None:
    """Cheapest road from a destination's car park entrance to a network holding houses of its
    colour, or straight to one of those houses."""
    state = board.state
    cp = state.carparks[dest.carpark]
    into_park = {outside: opposite(d) for outside, d in cp.entrances}
    houses = [h for h in state.houses if h.group == dest.group]
    if not houses:
        return None
    house_roots = {net.find(h.pos) for h in houses}
    access_tiles = {h.access for h in houses}

    def is_goal(p: Pos) -> bool:
        if p in access_tiles or board.planned.get(p) == dest.group:
            return True
        return state.has_road(p) and net.find(p) in house_roots and state.content.get(p) not in BLOCKING

    found = search(board, list(into_park), is_goal, bridges, tunnels)
    if found is None:
        return None
    path, crossings = found
    entrance = path[0]
    segments = [(entrance, into_park[entrance])] + _road_segments(path)
    return Plan(f"destination {dest.tiles[0]}", ("destination", dest.key), dest.group, segments, path,
                _new_tiles(board, path), crossings)
