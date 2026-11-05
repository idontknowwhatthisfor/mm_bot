"""The interface a bot uses: `Game` wraps a simulated `World` with read-only views and actions.

    from mmsim import Game
    game = Game(seed=1, map="river")
    for house in game.houses: ...
    game.draw_path([(3, 4), (4, 4), (5, 4)])
    game.advance(0.4)

See GUIDE.md for the rules and a walkthrough.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .core import CarState, Content, Obstacle, Pos, Terrain, Upgrade, direction_between, neighbor
from .world import MAX_OVERCROWD, PIN_LIMIT, World


class Result(IntEnum):
    """What an action returned. Only OK changed the map."""
    OK = 0
    INVALID = 2  # off the map or city limits, onto a building, or turning a house the wrong way
    NO_CARPARK = 3  # a car park joins only from its entrance tile
    NO_UPGRADES = 5  # no roundabout / traffic light in stock
    NO_CONCRETE = 6  # not enough road tiles
    CANT_CLEAR = 8  # buildings can't be cleared
    EXISTS = 13  # that road is already there
    NO_BRIDGE = 14  # no bridge in stock, or a bridge that isn't straight
    NO_TUNNEL = 15
    NO_ROADS = 16  # nothing to clear
    CROSSING = 19  # diagonals may not cross each other, or cross water


@dataclass(frozen=True)
class HouseView:
    pos: Pos
    colour: int
    driveway: int  # direction from the house to its access tile
    access: Pos  # the tile its driveway leads onto; roads must reach this tile
    idle_cars: int
    cars: int


@dataclass(frozen=True)
class DestinationView:
    id: int
    colour: int
    tiles: tuple[Pos, ...]  # the 2x2 building
    car_park: tuple[Pos, ...]  # its 2 tiles; car_park[0] has the entrance
    entrance: Pos  # the road tile outside the car park
    pins: int  # unassigned + en_route
    unassigned: int  # pins no car has been sent to yet
    en_route: int  # pins with a car on the way
    served: int  # pins served so far
    timer: float  # the overcrowd timer; the game ends when it reaches max_timer
    pin_limit: int
    max_timer: float


@dataclass(frozen=True)
class CarView:
    id: int
    colour: int
    home: Pos
    state: CarState
    destination: int | None  # DestinationView.id it was sent to
    tile: Pos | None  # None while parking
    speed: float  # tiles per game second; 0 when stopped
    obstacle: Obstacle
    path: tuple[Pos, ...]  # tiles still to drive


class Game:
    def __init__(self, seed: int = 0, map: str = "river", world: World | None = None):
        self.world = world or World(seed, map)
        self.seed = seed
        self.map = map

    # Clock and score -------------------------------------------------------------------------

    @property
    def time(self) -> float:
        """Game seconds since the start. A day is 20 s, a week 140 s."""
        return self.world.time

    @property
    def week(self) -> int:
        return self.world.week

    @property
    def day(self) -> int:
        return self.world.day

    @property
    def score(self) -> int:
        return self.world.score

    @property
    def over(self) -> bool:
        return self.world.ended

    @property
    def paused(self) -> bool:
        """True while an upgrade choice waits (or the game is over): time doesn't move."""
        return self.world.paused

    def advance(self, seconds: float):
        """Run the game on. Does nothing while paused."""
        self.world.advance(seconds)

    # The map ---------------------------------------------------------------------------------

    @property
    def width(self) -> int:
        return self.world.style.width

    @property
    def height(self) -> int:
        return self.world.style.height

    def terrain(self, p: Pos) -> Terrain | None:
        return self.world.terrain.get(p)

    def in_city(self, p: Pos) -> bool:
        """Inside the city limits (they grow every week). Roads may only be drawn here."""
        return p in self.world.playable()

    def city(self) -> frozenset[Pos]:
        return self.world.playable()

    def content(self, p: Pos) -> Content:
        return self.world.content.get(p, Content.NONE)

    def has_road(self, p: Pos) -> bool:
        return any(self.world.roads.get(p, ()))

    def roads(self, p: Pos) -> list[int]:
        """Directions in which tile p has its half of a road (houses and car parks have a stub)."""
        return [d for d, s in enumerate(self.world.roads.get(p, [0] * 8)) if s]

    def joined(self, p: Pos, d: int) -> bool:
        """A road runs from p to its neighbour in direction d (both sides point at each other)."""
        return self.world.joined(p, d)

    def free(self, p: Pos) -> bool:
        """Plain land inside the city with no building on it: a road may go here."""
        t = self.terrain(p)
        return t is not None and t.driveable and self.in_city(p) and self.content(p) == Content.NONE

    # Things on the map -----------------------------------------------------------------------

    @property
    def houses(self) -> list[HouseView]:
        idle: dict[Pos, int] = {}
        for c in self.world.cars:
            if c.state == CarState.WAITING:
                idle[c.house.pos] = idle.get(c.house.pos, 0) + 1
        return [HouseView(h.pos, h.group, h.driveway, h.access, idle.get(h.pos, 0), 2) for h in self.world.houses]

    @property
    def destinations(self) -> list[DestinationView]:
        return [DestinationView(d.key, d.group, tuple(d.tiles), tuple(d.park), d.entrance, d.unassigned + d.en_route,
                                d.unassigned, d.en_route, d.served, d.overcrowd_time, PIN_LIMIT, MAX_OVERCROWD)
                for d in self.world.destinations.values()]

    @property
    def cars(self) -> list[CarView]:
        return [CarView(c.id, c.house.group, c.house.pos, c.state, c.destination,
                        None if c.state == CarState.PARKING else c.at, c.speed, c.obstacle, tuple(c.path))
                for c in self.world.cars]

    @property
    def stock(self) -> dict[str, int]:
        a = self.world.available
        return {"roads": a[Upgrade.CONCRETE], "bridges": a[Upgrade.BRIDGE], "tunnels": a[Upgrade.TUNNEL],
                "roundabouts": a[Upgrade.ROUNDABOUT], "traffic_lights": a[Upgrade.TRAFFIC_LIGHT],
                "bridges_coming_back": self.world.mothballed[Upgrade.BRIDGE],
                "tunnels_coming_back": self.world.mothballed[Upgrade.TUNNEL]}

    # Upgrades --------------------------------------------------------------------------------

    @property
    def upgrade_choices(self) -> list[dict]:
        """While paused for the weekly upgrade: the 2 packages, each
        {"type": "roads" | "bridge" | "tunnel" | "roundabout" | "traffic_light" | "motorway",
         "amount": n, "extra_roads": m}. Empty otherwise."""
        names = {Upgrade.CONCRETE: "roads", Upgrade.BRIDGE: "bridge", Upgrade.TUNNEL: "tunnel",
                 Upgrade.ROUNDABOUT: "roundabout", Upgrade.TRAFFIC_LIGHT: "traffic_light",
                 Upgrade.MOTORWAY: "motorway"}
        if not self.world.choices:
            return []
        return [{"type": names[o["type"]], "amount": o["amount"], "extra_roads": o["concrete"]}
                for o in self.world.choices[0]]

    def choose_upgrade(self, index: int):
        """Take package 0 or 1; the game carries on."""
        self.world.choose(index)

    # Actions ---------------------------------------------------------------------------------

    def draw_road(self, p: Pos, d: int) -> Result:
        """Draw one road segment from tile p to its neighbour in direction d.

        - A new tile of road costs 1 from stock["roads"]; a house's access tile is free.
        - Drawing out of a house turns its driveway to face d (cardinal directions only).
        - The access tile of a house, and a car park's entrance tile, join up as soon as they have road.
        - Stepping from land onto water takes a bridge (a mountain takes a tunnel); the deck must
          run straight (N/E/S/W) and continue straight to land on the other side.
        """
        return Result(self.world.add_road(p, d))

    def draw_path(self, tiles: list[Pos]) -> list[Result]:
        """Draw a road along neighbouring tiles, segment by segment. Stops at the first failure
        (segments already there count as fine)."""
        out = []
        for a, b in zip(tiles, tiles[1:]):
            r = self.draw_road(a, direction_between(a, b))
            out.append(r)
            if r not in (Result.OK, Result.EXISTS):
                break
        return out

    def clear(self, p: Pos) -> Result:
        """Remove every road on tile p; its road tile goes back to stock. On a bridge or tunnel the
        whole crossing goes, and the bridge/tunnel comes back to stock a few seconds later."""
        return Result(self.world.clear_tile(p))

    def add_roundabout(self, p: Pos) -> Result:
        """On a junction (3+ roads joined): it lets 4 cars through at once instead of 2."""
        return Result(self.world.add_junction_upgrade(Upgrade.ROUNDABOUT, p))

    def add_traffic_light(self, p: Pos) -> Result:
        """Same effect as a roundabout in the simulator."""
        return Result(self.world.add_junction_upgrade(Upgrade.TRAFFIC_LIGHT, p))

    # Helpers ---------------------------------------------------------------------------------

    def route(self, start: Pos, goal: Pos) -> list[Pos] | None:
        """The road a car would take from start to goal (tiles after start), or None. Cars take
        the shortest drive (a diagonal step is 1.41 tiles) and never drive through houses or car
        parks."""
        return self.world.route(start, goal)

    def reachable(self, start: Pos) -> set[Pos]:
        """Every tile joined to start by road (start included if it has road)."""
        if not self.has_road(start):
            return set()
        seen, stack = {start}, [start]
        while stack:
            p = stack.pop()
            for d in range(8):
                q = neighbor(p, d)
                if q not in seen and self.joined(p, d):
                    seen.add(q)
                    stack.append(q)
        return seen

    def capacity(self, p: Pos) -> int:
        """Cars that fit on a road tile at once: 3 on a road, 2 at a junction, 4 with a roundabout
        or lights."""
        return self.world.capacity(p)
