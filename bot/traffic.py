"""What the cars actually do, from Session.read_cars: where they queue (jams), and whether the game
sends them the way the planner's route model says it does (route check).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from game import Car, CarState, Obstacle, Pos, State, Traffic, ring_tiles
from planner import RoadGraph, drive_length, house_routes, octile

# A car is stopped in a queue when it drives slower than the game's own CarStoppedSpeedThreshold
# while it waits behind another car or at a junction it may not enter yet.
QUEUE_OBSTACLES = (Obstacle.LEADING_VEHICLE, Obstacle.BLOCKING_INTERSECTION)
# The jam level of a tile is a rolling average of its stopped cars over about this many game
# seconds, so a few cars waiting out one red light don't count as a jam.
JAM_WINDOW = 10.0
# Average stopped cars on a tile from which it counts as jammed (for the logs).
JAM_LEVEL = 1.5


def queued(car: Car, threshold: float) -> bool:
    """Driving on a road tile, stopped behind a car or at a junction."""
    return (car.driving and car.tile is not None and not car.in_carpark
            and car.stopped(threshold) and car.obstacle in QUEUE_OBSTACLES)


def stopped_cars(traffic: Traffic) -> Counter:
    """Road tile -> cars stopped in a queue on it right now."""
    return Counter(c.tile for c in traffic.cars if queued(c, traffic.stopped_threshold))


class JamTracker:
    """Per road tile, a time-weighted rolling average of the cars stopped in a queue on it."""

    def __init__(self, window: float = JAM_WINDOW):
        self.window = window
        self.level: dict[Pos, float] = {}
        self.time: float | None = None

    def reset(self):
        self.level.clear()
        self.time = None

    def update(self, traffic: Traffic):
        if self.time is None:
            self.time = traffic.time
            return
        dt = traffic.time - self.time
        if dt <= 0:
            return  # the clock didn't move (paused): nothing new to average in
        self.time = traffic.time
        weight = 1.0 - math.exp(-dt / self.window)
        now = stopped_cars(traffic)
        for p in set(self.level) | set(now):
            level = self.level.get(p, 0.0)
            level += weight * (now.get(p, 0) - level)
            if level < 0.01:
                self.level.pop(p, None)
            else:
                self.level[p] = level

    def jammed(self, min_level: float = JAM_LEVEL) -> dict[Pos, float]:
        return {p: v for p, v in self.level.items() if v >= min_level}


# Route check ----------------------------------------------------------------------------------

# Lane lengths are in world units; a tile is 2.0 long (a straight lane measures 2.0, a diagonal step
# 2.0 + a 0.828 corner lane = 2*sqrt(2)).
TILE_WORLD_LENGTH = 2.0
# Two routes this close in length (in tiles) count as equally short alternatives.
TIE_TILES = 1.0


@dataclass
class RouteResult:
    car: Car
    kind: str  # "match", "tie" (other tiles, as short), "different", "off route"
    game: list[Pos]  # the car's remaining route as tiles (car park tiles left out, roundabouts collapsed)
    planned: list[Pos]  # the planner's route from the car's tile to the same place, likewise
    game_length: float  # tiles, from the game's own pathLength
    planned_length: float  # tiles
    on_house_routes: bool  # every tile of the game's route lies on planner.house_routes for the house

    @property
    def difference(self) -> int:
        """Tiles the car drives that the planner didn't predict."""
        return len(set(self.game) - set(self.planned))


def collapse_roundabouts(route: list[Pos], state: State) -> list[Pos]:
    """The planner models a roundabout as one junction at its centre; cars drive round its ring (and
    the game keeps the ring's lanes on the centre tile). Map ring tiles to the centre and drop repeats
    so both routes read the same through one."""
    ring = {t: c for c in state.roundabouts for t in [c, *ring_tiles(c)]}
    out: list[Pos] = []
    for t in route:
        t = ring.get(t, t)
        if not out or out[-1] != t:
            out.append(t)
    return out


def _route_length(route: list[Pos]) -> float:
    return sum(drive_length(a, b) if max(abs(a[0] - b[0]), abs(a[1] - b[1])) == 1 else octile(a, b)
               for a, b in zip(route, route[1:]))


def _follows(game: list[Pos], planned: list[Pos]) -> bool:
    """Whether the car's tiles all lie on the planned route, in order. The game's path can leave
    tiles out (lanes in car parks and on some diagonal corners map to no tile), so a gap in it is
    fine; a tile off the planned route isn't."""
    i = 0
    for t in game:
        try:
            i = planned.index(t, i) + 1
        except ValueError:
            return False
    return True


def check_routes(state: State, traffic: Traffic, graph: RoadGraph | None = None) -> list[RouteResult]:
    """Compare each driving car's remaining path (the game's own route) with the planner's route
    model: the shortest drive over joined road (RoadGraph.shortest, the same search house_routes
    uses) from the house to the car park it was sent to, or from that car park home. The car's path
    starts after the tile it is on, so it is compared with what the planned route has left from there."""
    graph = graph or RoadGraph(state)
    on_routes = house_routes(state, graph)
    parks = {t for cp in state.carparks.values() for t in cp.tiles}
    to_park: dict[int, dict] = {}  # destination -> parent pointers toward its car park
    to_home: dict[Pos, tuple] = {}  # house -> (dist, parent) toward the house
    out = []

    def clean(route: list[Pos], ends: set[Pos]) -> list[Pos]:
        return collapse_roundabouts([t for t in route if t not in ends and t not in parks], state)

    for car in traffic.cars:
        if not car.driving or car.tile is None or car.house is None:
            continue
        dest = state.destinations.get(car.destination)
        cp = state.carparks.get(dest.carpark) if dest else None
        if cp is None or not cp.tiles:
            continue
        if car.state == CarState.DRIVING_TO_DESTINATION:
            if dest.key not in to_park:
                to_park[dest.key] = graph.shortest(cp.tiles)[1]
            parent = to_park[dest.key]
            if car.house not in parent:
                continue  # the planner sees no road from this house to that car park
            planned, p = [], parent[car.house]
            while p is not None:
                planned.append(p)
                p = parent[p]
        else:
            if car.house not in to_home:
                to_home[car.house] = graph.shortest([car.house])
            dist, parent = to_home[car.house]
            start = min((t for t in cp.tiles if t in dist), key=lambda t: dist[t], default=None)
            if start is None:
                continue
            planned, p = [], parent.get(start)
            while p is not None:
                planned.append(p)
                p = parent.get(p)
        ends = {car.house, *cp.tiles}
        planned = clean(planned, ends)
        game = clean(car.path, ends)
        here = None if car.tile == car.house else clean([car.tile], ends)
        here = here[0] if here else None
        tiles = on_routes.get(car.house, set())
        on_house = all(t in tiles or t in state.roundabouts for t in game)
        game_len = car.path_length / TILE_WORLD_LENGTH
        if here is not None and here not in planned:
            out.append(RouteResult(car, "off route", game, planned, game_len, _route_length(planned), on_house))
            continue
        expected = planned[planned.index(here) + 1:] if here is not None else planned
        planned_len = _route_length(([here] if here else []) + expected)
        if game and game[0] == here:
            game = game[1:]
        if _follows(game, expected):
            kind = "match"
        elif abs(game_len - planned_len) <= TIE_TILES:
            kind = "tie"
        else:
            kind = "different"
        out.append(RouteResult(car, kind, game, expected, game_len, planned_len, on_house))
    return out


@dataclass
class TrafficDay:
    """What the bot saw of the cars over one game day, for the daily log line."""
    reads: int = 0
    read_ms: float = 0.0
    max_read_ms: float = 0.0
    cars: int = 0  # at the last read
    queued_peak: int = 0  # most cars stopped in queues at one read
    checks: Counter = field(default_factory=Counter)  # route result kind -> count
    on_house_routes: int = 0
    worst: dict[int, RouteResult] = field(default_factory=dict)  # car id -> its biggest mismatch today

    def add_read(self, traffic: Traffic):
        self.reads += 1
        self.read_ms += traffic.read_ms
        self.max_read_ms = max(self.max_read_ms, traffic.read_ms)
        self.cars = len(traffic.cars)
        self.queued_peak = max(self.queued_peak, sum(stopped_cars(traffic).values()))

    def add_routes(self, results: list[RouteResult]):
        for r in results:
            self.checks[r.kind] += 1
            self.on_house_routes += r.on_house_routes
            if r.kind in ("different", "off route"):
                best = self.worst.get(r.car.id)
                if best is None or r.difference > best.difference:
                    self.worst[r.car.id] = r

    def summary(self, jams: dict[Pos, float], worst: int = 3) -> list[str]:
        checked = sum(self.checks.values())
        lines = []
        avg = self.read_ms / self.reads if self.reads else 0.0
        jam_text = ", ".join(f"{p} {v:.1f}" for p, v in sorted(jams.items(), key=lambda kv: -kv[1])[:8]) or "none"
        lines.append(f"  traffic: {self.cars} cars, {self.reads} reads ({avg:.0f} ms avg, {self.max_read_ms:.0f} max), "
                     f"up to {self.queued_peak} stopped in queues; jammed tiles: {jam_text}")
        if checked:
            pct = lambda n: 100.0 * n / checked  # noqa: E731
            lines.append(f"  routes: {checked} checks, {pct(self.checks['match']):.0f}% match the planner, "
                         f"{pct(self.checks['tie']):.0f}% other equally short route, "
                         f"{pct(self.checks['different']):.0f}% different, {pct(self.checks['off route']):.0f}% off "
                         f"the planned route; {pct(self.on_house_routes):.0f}% stay on house_routes")
            for r in sorted(self.worst.values(), key=lambda r: -r.difference)[:worst]:
                c = r.car
                extra = sorted(set(r.game) - set(r.planned))
                lines.append(f"    car {c.id} (house {c.house}, colour {c.colour}, {c.state.name.lower()}) at {c.tile}: "
                             f"{r.kind}, {len(extra)} tiles off the planned route {extra[:6]}; game {r.game_length:.1f} "
                             f"tiles {r.game} vs planned {r.planned_length:.1f} {r.planned}")
        return lines
