"""Basic types shared by the simulator: positions, directions and enums.

A position is (x, y) in tiles, with +y pointing north. Directions are numbered clockwise from
north, the same way Mini Motorways numbers them:

        NW(7)  N(0)  NE(1)
        W(6)    .    E(2)
        SW(5)  S(4)  SE(3)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

Pos = tuple[int, int]

DIRS: list[Pos] = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]
N, NE, E, SE, S, SW, W, NW = range(8)
CARDINALS = (N, E, S, W)
DIRECTION_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def opposite(d: int) -> int:
    return (d + 4) % 8


def neighbor(pos: Pos, d: int) -> Pos:
    return pos[0] + DIRS[d][0], pos[1] + DIRS[d][1]


def direction_between(a: Pos, b: Pos) -> int:
    """Direction from a to its neighbour b."""
    return DIRS.index((b[0] - a[0], b[1] - a[1]))


class Content(IntEnum):
    NONE = 0
    HOUSE = 1
    DESTINATION = 2
    CARPARK = 3


class Upgrade(IntEnum):
    CONCRETE = 0  # road tiles
    BRIDGE = 1
    MOTORWAY = 2  # offered, but can't be built in the simulator
    TRAFFIC_LIGHT = 3
    ROUNDABOUT = 4
    TUNNEL = 5


class CarState(IntEnum):
    WAITING = 0  # at home
    DRIVING_TO_DESTINATION = 1
    PARKING = 2
    PARKED = 3  # (unused: parking covers it)
    DRIVING_HOME = 4


class Obstacle(IntEnum):
    """What a car is slowing down for."""
    NONE = 0
    TARGET = 1  # the end of its trip
    LEADING_VEHICLE = 2  # a full tile ahead
    BLOCKING_INTERSECTION = 3  # a full junction ahead


@dataclass(frozen=True)
class Terrain:
    driveable: bool  # plain land: roads can go here
    water: bool  # needs a bridge
    mountain: bool  # needs a tunnel
    buildable: bool  # buildings can spawn here
