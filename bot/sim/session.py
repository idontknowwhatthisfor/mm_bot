"""SimSession: the simulated World behind the same interface as game.Session, so bot.Bot (and
the planner and traffic code) play it unchanged."""

from __future__ import annotations

from game import (CarState, Carpark, Car, Destination, GameMode, House, Pos, State, Traffic, Upgrade,
                  join_roundabouts)
from sim.world import MAX_OVERCROWD, PIN_LIMIT, World

STOPPED_THRESHOLD = 0.001  # as RoadChunkModel.CarStoppedSpeedThreshold reads in the game
TILE_WORLD_LENGTH = 2.0  # pathLength units per tile, as in the game


class SimSession:
    def __init__(self, world: World, label: str = "sim"):
        self.world = world
        self.label = label
        self.terrain = world.terrain
        self.game_key = id(world)
        self.mode = GameMode.NORMAL
        self.h: dict = {}
        self.speed = 1.0

    # Reading ------------------------------------------------------------------------------------

    def read(self) -> State:
        w = self.world
        roads = {p: list(r) for p, r in w.roads.items()}
        join_roundabouts(roads, set(w.roundabouts))
        idle: dict[Pos, int] = {}
        for car in w.cars:
            if car.state == CarState.WAITING:
                idle[car.house.pos] = idle.get(car.house.pos, 0) + 1
        houses = [House(h.pos, h.group, h.driveway, idle.get(h.pos, 0), 2) for h in w.houses]
        destinations, carparks = {}, {}
        for d in w.destinations.values():
            destinations[d.key] = Destination(
                d.key, d.group, True, d.unassigned + d.en_route, d.key,
                d.unassigned + d.en_route > PIN_LIMIT, list(d.tiles), unassigned=d.unassigned,
                en_route=d.en_route, served=d.served, pin_limit=PIN_LIMIT, overcrowd_time=d.overcrowd_time,
                max_overcrowd_time=MAX_OVERCROWD)
            carparks[d.key] = Carpark(d.key, list(d.park), [(d.entrance, d.entrance_dir)])
        t = w.time
        return State(roads, dict(w.content), houses, destinations, carparks, list(w.available),
                     len(w.choices), w.week, int(t // 20) % 7, int(t / 0.8333) % 24, t, t, w.paused, w.score,
                     w.ended, w.playable(), set(w.roundabouts), set(w.lights), mothballed=list(w.mothballed))

    def read_cars(self) -> Traffic:
        cars = []
        for c in self.world.cars:
            parked = c.state == CarState.PARKING
            length = sum(1.0 for _ in c.path) - c.progress
            cars.append(Car(c.id, c.house.pos, c.house.group, c.state, c.destination,
                            None if parked else c.at, parked, c.speed, c.obstacle, None, list(c.path),
                            max(0.0, length) * TILE_WORLD_LENGTH))
        return Traffic(self.world.time, cars, STOPPED_THRESHOLD)

    def map_label(self) -> str:
        return self.label

    # Actions ------------------------------------------------------------------------------------

    def add_roads(self, segments: list[tuple[Pos, int]], sequential: bool = False) -> list[int]:
        return [self.world.add_road(p, d) for p, d in segments]

    def clear_tiles(self, tiles: list[Pos], sequential: bool = False) -> list[int]:
        return [self.world.clear_tile(p) for p in tiles]

    def add_junction_upgrade(self, kind: Upgrade, p: Pos) -> int:
        return self.world.add_junction_upgrade(kind, p)

    def upgrade_choices(self) -> list[dict]:
        return [dict(o) for o in self.world.choices[0]] if self.world.choices else []

    def choose_upgrade(self, index: int, allow_command: bool = False) -> bool:
        if not self.world.choices:
            return False
        self.world.choose(index)
        return True

    def game_speed(self) -> float:
        return self.speed

    def set_game_speed(self, speed: float) -> float:
        self.speed = speed  # the runner decides how fast the world advances
        return speed
