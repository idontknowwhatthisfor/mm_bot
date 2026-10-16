"""Live view of a running Mini Motorways game, and the actions the bot can take on it.

Everything goes through the bridge: state is read straight from the game's models and roads are
built with the game's own TileEditor/EditTileCommand, exactly as if they had been drawn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

from mmbridge import Bridge, BridgeError, call_req, get_req, nested_call, typeof

Pos = tuple[int, int]

# TileDirection: North..NorthWest, clockwise. +y is north.
DIRS: list[Pos] = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]
N, NE, E, SE, S, SW, W, NW = range(8)
CARDINALS = (N, E, S, W)


def opposite(d: int) -> int:
    return (d + 4) % 8


def neighbor(pos: Pos, d: int) -> Pos:
    return pos[0] + DIRS[d][0], pos[1] + DIRS[d][1]


def direction_between(a: Pos, b: Pos) -> int:
    return DIRS.index((b[0] - a[0], b[1] - a[1]))


class Content(IntEnum):
    NONE = 0
    HOUSE = 1
    DESTINATION = 2
    CARPARK = 3
    TREE = 4
    BOAT_TERMINAL = 5


class Upgrade(IntEnum):
    CONCRETE = 0  # plain road tiles
    BRIDGE = 1
    MOTORWAY = 2
    TRAFFIC_LIGHT = 3
    ROUNDABOUT = 4
    TUNNEL = 5
    HOUSE = 6
    DESTINATION = 7
    DOUBLE_DESTINATION = 8


class GameMode(IntEnum):
    NORMAL = 0
    TUTORIAL = 1
    BACKGROUND = 2  # the city animating behind the menus
    ENDLESS = 3
    EXPERT = 4


# RoadState bit of a road being taken down: a cleared bridge or tunnel stays like this until the cars
# on it have left, then its upgrade goes back into stock. Read as no road.
MOTHBALLED = 16

EDIT_SUCCESS = 0
EDIT_ALREADY_EXISTS = 13
EDIT_SKIPPED = -2  # not attempted because an earlier segment of the same road failed
EDIT_RESULT_NAMES = {
    -1: "BridgeError", -2: "Skipped",
    0: "Success", 1: "NotInitialized", 2: "InvalidTileCoordinate", 3: "CannotConnectToCarpark",
    4: "CannotConnectHouseToBridge", 5: "NotEnoughUpgrades", 6: "NotEnoughConcrete",
    7: "NotEnoughConcreteForMotorway", 8: "CannotClearTile", 9: "MotorwayTooShort",
    10: "MotorwayBlockedByMountain", 11: "CannotConnectHouseToTunnel", 12: "ClearForSpecificTypeNotNeeded",
    13: "EditAlreadyExists", 14: "CannotCreateBridge", 15: "CannotCreateTunnel", 16: "NoDeletableRoads",
    17: "NoDeletableUpgrade", 18: "CannotConnectHouseToRail", 19: "CannotCreateCrossing",
}

EDIT_TILE_COMMAND = {"$class": "Motorways.Commands.EditTileCommand"}
SELECT_UPGRADE_COMMAND = {"$class": "Motorways.Commands.SelectUpgradeCommand"}


def _pos(v) -> Pos:
    return v["m_X"], v["m_Y"]


def _vec(p: Pos) -> dict:
    return {"x": p[0], "y": p[1]}


def _value(v, default=None):
    """A projected value, or default when the game couldn't produce it (null or {"$err": ...})."""
    if v is None or isinstance(v, dict):
        return default
    return v


def ring_tiles(centre: Pos) -> list[Pos]:
    """The 4 tiles a roundabout's one-way ring runs through: north, east, south and west of its centre."""
    return [neighbor(centre, d) for d in CARDINALS]


def join_roundabouts(roads: dict[Pos, list[int]], roundabouts: set[Pos]):
    """Model each roundabout as one junction at its centre. In the game the centre holds no road and
    the ring runs one way through the 4 tiles around it (`_activeRoundaboutInput/Output`, kept out
    of `_twoLaneRoadState`), so read raw, the roads around it look cut off from each other. Cars
    reach the ring from the ring tiles' own roads and from any neighbour whose road points at the
    centre, diagonal ones included. So the centre is joined both ways to its 4 ring tiles, and to
    every other neighbour whose road points at it."""
    for c in roundabouts:
        r = list(roads.get(c) or [0] * 8)
        for d in range(8):
            q = neighbor(c, d)
            qr = roads.get(q)
            if d in CARDINALS or (qr and qr[opposite(d)]):
                r[d] = r[d] or 8
                qr = list(qr or [0] * 8)
                qr[opposite(d)] = qr[opposite(d)] or 8
                roads[q] = qr
        roads[c] = r


@dataclass
class House:
    pos: Pos
    group: int
    driveway: int  # direction from the house to its access tile
    waiting: int  # idle cars at home, ready to be dispatched
    cars: int = 0  # cars the house owns

    @property
    def access(self) -> Pos:
        return neighbor(self.pos, self.driveway)


@dataclass
class Carpark:
    key: int  # object address, stable while the carpark exists
    tiles: list[Pos] = field(default_factory=list)
    # (outside tile, direction from the carpark tile to it)
    entrances: list[tuple[Pos, int]] = field(default_factory=list)


@dataclass
class Destination:
    key: int
    group: int
    active: bool
    waiting: int  # pins waiting to be served
    carpark: int | None
    overcrowded: bool
    tiles: list[Pos] = field(default_factory=list)
    unassigned: int = 0  # pins no car has been dispatched to yet (too few cars of the colour)
    en_route: int = 0  # pins whose car is on its way (slow routes keep this high)
    served: int = 0  # pins served so far (totalServicedPins); its growth is the arrival rate
    pin_limit: int = 6  # more pins than this and the overcrowd timer runs (9 once upgraded)
    overcrowd_time: float = 0.0  # the overcrowd timer; the game is lost past max_overcrowd_time
    max_overcrowd_time: float | None = None

    @property
    def pressure(self) -> float:
        """Pins as a share of the limit, plus how far the overcrowd timer has run."""
        p = (self.unassigned + self.en_route) / max(1, self.pin_limit)
        if self.max_overcrowd_time:
            p += self.overcrowd_time / self.max_overcrowd_time
        return p


@dataclass
class Terrain:
    driveable: bool
    water: bool
    mountain: bool
    buildable: bool


class CarState(IntEnum):
    """VehicleModel.BehaviorState."""
    WAITING = 0  # at home, waiting to be sent to a pin
    DRIVING_TO_DESTINATION = 1
    PARKING = 2
    PARKED = 3
    DRIVING_HOME = 4
    REALIGNING_DRIVEWAY = 5


class Obstacle(IntEnum):
    """VehicleModel.Frame.nearestObstacle: what the car is slowing down for."""
    NONE = 0
    TARGET = 1  # the end of its path
    LEADING_VEHICLE = 2  # the car ahead
    BLOCKING_INTERSECTION = 3  # a junction it may not enter yet
    HOTSWAPPING_LANE = 4


# distanceToLeadingVehicle when nothing is ahead (int.MaxValue + 1 as a Fix64).
NO_LEADER_GAP = 1e9


@dataclass
class Car:
    id: int
    house: Pos | None  # home tile
    colour: int
    state: CarState
    destination: int | None  # Destination.key it was sent to (also set on the way home)
    tile: Pos | None  # road tile it is on; None in a car park or where its lane maps to no tile
    in_carpark: bool  # on a car park lane (parking, parked, or pulling out)
    speed: float
    obstacle: Obstacle
    gap: float | None  # distance to the car ahead; None when nothing is ahead
    path: list[Pos] = field(default_factory=list)  # the route still ahead, as tiles (game-assigned)
    path_length: float = 0.0

    @property
    def driving(self) -> bool:
        return self.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME)

    def stopped(self, threshold: float) -> bool:
        return self.speed < threshold


@dataclass
class Traffic:
    """Every car at one moment (Session.read_cars)."""
    time: float  # game time of the read
    cars: list[Car]
    stopped_threshold: float  # RoadChunkModel.CarStoppedSpeedThreshold
    read_ms: float = 0.0


@dataclass
class State:
    roads: dict[Pos, list[int]]  # per tile, the RoadState toward each of the 8 directions
    content: dict[Pos, Content]
    houses: list[House]
    destinations: dict[int, Destination]
    carparks: dict[int, Carpark]
    available: list[int]  # indexed by Upgrade
    pending_choices: int
    week: int
    day: int
    hour: int
    time: float
    expansion_time: float
    paused: bool
    score: int
    ended: bool
    playable: frozenset[Pos]  # tiles inside the current (growing) city limits
    roundabouts: set[Pos] = field(default_factory=set)
    traffic_lights: set[Pos] = field(default_factory=set)
    # Upgrades taken down but not back in stock yet (UpgradeDatabase._mothballedUpgrades), by Upgrade.
    mothballed: list[int] = field(default_factory=lambda: [0] * 9)

    def in_playable(self, p: Pos) -> bool:
        return p in self.playable

    def has_road(self, p: Pos) -> bool:
        return any(self.roads.get(p, ()))

    def road_toward(self, p: Pos, d: int) -> bool:
        r = self.roads.get(p)
        return bool(r and r[d])

    def joined(self, p: Pos, d: int) -> bool:
        """Road runs from p to its neighbour in direction d (both sides pointing at each other)."""
        return self.road_toward(p, d) and self.road_toward(neighbor(p, d), opposite(d))

    def links(self, p: Pos) -> int:
        """Roads actually joined to p."""
        return sum(1 for d in range(8) if self.joined(p, d))


def snapshot(state: State, terrain: dict[Pos, Terrain], traffic: Traffic | None = None) -> dict:
    """The state and terrain (and the latest car read, if any) as plain JSON data, to replay a
    situation offline (load_snapshot, load_traffic)."""
    pairs = lambda tiles: [list(p) for p in sorted(tiles)]  # noqa: E731
    cars = None
    if traffic is not None:
        cars = {"time": traffic.time, "threshold": traffic.stopped_threshold,
                "cars": [[c.id, c.house and list(c.house), c.colour, int(c.state), c.destination,
                          c.tile and list(c.tile), c.in_carpark, c.speed, int(c.obstacle), c.gap,
                          [list(t) for t in c.path], c.path_length] for c in traffic.cars]}
    return {
        "traffic": cars,
        "roads": [[*p, *r] for p, r in sorted(state.roads.items()) if any(r)],
        "content": [[*p, int(c)] for p, c in sorted(state.content.items()) if c],
        "houses": [[*h.pos, h.group, h.driveway, h.waiting, h.cars] for h in state.houses],
        # Destination and car park tiles keep their order: the first one is used as the anchor.
        "destinations": [{**{k: v for k, v in vars(d).items() if k != "tiles"}, "tiles": [list(p) for p in d.tiles]}
                         for d in state.destinations.values()],
        "carparks": [{"key": c.key, "tiles": [list(p) for p in c.tiles],
                      "entrances": [[*p, d] for p, d in c.entrances]} for c in state.carparks.values()],
        "available": list(state.available),
        "mothballed": list(state.mothballed),
        "clock": [state.pending_choices, state.week, state.day, state.hour, state.time, state.expansion_time,
                  state.paused, state.score, state.ended],
        "playable": pairs(state.playable),
        "roundabouts": pairs(state.roundabouts),
        "traffic_lights": pairs(state.traffic_lights),
        "terrain": [[*p, t.driveable, t.water, t.mountain, t.buildable] for p, t in sorted(terrain.items())],
    }


def load_snapshot(data: dict) -> tuple[State, dict[Pos, Terrain]]:
    """The (state, terrain) a snapshot() was made from."""
    tiles = lambda rows: [tuple(r) for r in rows]  # noqa: E731
    destinations = {}
    for d in data["destinations"]:
        d = dict(d, tiles=tiles(d["tiles"]))
        destinations[d["key"]] = Destination(**d)
    carparks = {c["key"]: Carpark(c["key"], tiles(c["tiles"]), [((x, y), d) for x, y, d in c["entrances"]])
                for c in data["carparks"]}
    state = State({(r[0], r[1]): list(r[2:]) for r in data["roads"]},
                  {(x, y): Content(c) for x, y, c in data["content"]},
                  [House((x, y), g, d, w, c) for x, y, g, d, w, c in data["houses"]],
                  destinations, carparks, list(data["available"]), *data["clock"],
                  frozenset(tiles(data["playable"])), set(tiles(data["roundabouts"])),
                  set(tiles(data["traffic_lights"])), mothballed=list(data.get("mothballed") or [0] * 9))
    terrain = {(x, y): Terrain(dv, w, m, b) for x, y, dv, w, m, b in data["terrain"]}
    return state, terrain


def load_traffic(data: dict) -> Traffic | None:
    """The car read saved with a snapshot, if there was one."""
    t = data.get("traffic")
    if not t:
        return None
    tile = lambda v: tuple(v) if v else None  # noqa: E731
    cars = [Car(i, tile(h), g, CarState(st), dest, tile(at), bool(cp), speed, Obstacle(obs), gap,
                [tuple(p) for p in path], length)
            for i, h, g, st, dest, at, cp, speed, obs, gap, path, length in t["cars"]]
    return Traffic(t["time"], cars, t["threshold"])


class Session:
    """Connection to the running app; follows whichever real (non-menu) game is active."""

    GAME_SERVICES = {
        "city": "Motorways.City",
        "tilemap": "Motorways.Models.TilemapModel",
        "editor": "Motorways.TileEditor",
        "sim": "Server.ISimulation",
        "upgrades": "Motorways.Models.UpgradeDatabaseModel",
        "clock": "Motorways.Models.ClockModel",
        "score": "Motorways.Models.ScoreModel",
    }

    def __init__(self, bridge: Bridge):
        self.b = bridge
        runtime = None
        for _ in range(240):  # the app may still be booting: IL2CPP first, then the app runtime
            try:
                self.b.release(0)
                found = self.b.find_objects("AppRuntime")
                if found:
                    runtime = found[0]
                    break
            except BridgeError:
                pass
            time.sleep(0.5)
        if runtime is None:
            raise BridgeError("AppRuntime not found; is the game up?")
        self.runtime = runtime
        self.app_scope = self.b.get(runtime, "_container.AppScope")
        self._app_pins = self.b.ping()["handles"]
        self.game_key: int | None = None
        self.mode: GameMode | None = None
        self.h: dict = {}
        self.terrain: dict[Pos, Terrain] = {}
        self._area_key = None
        self._playable: frozenset[Pos] = frozenset()
        self._pins = self._app_pins

    # Game tracking -------------------------------------------------------------------------

    def games(self) -> list[dict]:
        return self.b.get(self.app_scope, ["_establishingObjectToChildScopes", "*",
                                           {"key": "key", "mode": "key.StartedWithGameMode",
                                            "ended": "key.HasGameEnded"}], handles=False)

    def attach(self) -> bool:
        """Point at the active non-menu game. Returns False when there is none."""
        real = [g for g in self.games() if g["mode"] != GameMode.BACKGROUND]
        if not real:
            self.game_key = None
            return False
        key = real[-1]["key"]["$p"]
        if key == self.game_key:
            return True
        # Handles made from here on stay pinned until the game changes.
        self.b.release(self._app_pins)
        children = self.b.get(self.app_scope, ["_establishingObjectToChildScopes", "*", {"k": "key", "v": "value"}])
        pick = next(c for c in children if c["k"]["$p"] == key)
        self.h = {"game": pick["k"], "scope": pick["v"]}
        for name, type_name in self.GAME_SERVICES.items():
            self.h[name] = self.b.call(self.h["scope"], "Get", typeof(type_name), sig=["System.Type"])
        self.h["citydef"] = self.b.get(self.h["city"], "Definition")
        self._pins = self.b.ping()["handles"]
        self.game_key = key
        self.mode = GameMode(self.b.get(self.h["game"], "StartedWithGameMode"))
        self.disable_leaderboards()
        self.terrain = self._scan_terrain()
        self._area_key = None
        return True

    def disable_leaderboards(self):
        """Swap the game's leaderboard backend for its built-in null backend, so bot-played
        games never post scores. Lasts until the game process exits."""
        backend = self.b.get(self.h["game"], "_leaderboardService._leaderboardBackend", handles=False)
        if backend and not backend["$t"].endswith("NullLeaderboardBackend"):
            self.b.set(self.h["game"], "_leaderboardService._leaderboardBackend",
                       self.b.new("Motorways.Leaderboards.Backends.NullLeaderboardBackend"))

    def _scan_terrain(self) -> dict[Pos, Terrain]:
        area = self.b.get(self.h["citydef"], "_playableArea")
        coords = [(x, y) for x in range(area["m_XMin"], area["m_XMin"] + area["m_Width"])
                  for y in range(area["m_YMin"], area["m_YMin"] + area["m_Height"])]
        args = [[_vec(p)] for p in coords]
        results = {}
        for name in ("TileIsDriveable", "TileIsOverWater", "TileIsUnderAMountain", "TileIsBuildable"):
            results[name] = self.b.call_many(self.h["citydef"], name, args)
        return {p: Terrain(bool(results["TileIsDriveable"][i]), bool(results["TileIsOverWater"][i]),
                           bool(results["TileIsUnderAMountain"][i]), bool(results["TileIsBuildable"][i]))
                for i, p in enumerate(coords)}

    # Reading -------------------------------------------------------------------------------

    TILE_PROJECTION = {
        "x": "key.m_X",
        "y": "key.m_Y",
        "ct": "value._tile.ContentType",
        "r": "value._tile._twoLaneRoadState.*",
        "ra": "value._tile._isCenterOfRoundabout",
        "tl": "value._tile._hasTrafficLight",
        "h": ["value._tile.ContentModel", "?HouseModel",
              {"g": "GroupIndex", "d": "DrivewayDirection", "w": "waitingVehicles._size",
               "c": "ownedVehicles._size"}],
        "d": ["value._tile.ContentModel", "?DestinationModel",
              {"obj": [], "g": "GroupIndex", "a": "isActive", "w": "TotalDemand", "cp": "Carpark",
               "o": "IsOvercrowding", "ua": "unassignedDemand._size", "er": "waitingDemand._size",
               "sp": "totalServicedPins", "lim": "MaximumDemandBeforeTimerStarts",
               "ot": "CurrentFrame._overcrowdingTime", "mot": "_constants.MaxOvercrowdTime"}],
        "p": ["value._tile.ContentModel", "?CarparkModel",
              {"obj": [], "tl": "entranceAtTopLeft", "br": "entranceAtBottomRight",
               "tlc": "TopLeftDrivewayTileCoordinates", "tld": "TopLeftDrivewayDirection",
               "brc": "BottomRightDrivewayTileCoordinates", "brd": "BottomRightDrivewayDirection"}],
    }

    def read(self) -> State | None:
        if not self.attach():
            return None
        self.b.release(self._pins)
        h = self.h
        tiles, upgrades, clock, score, ended = self.b.batch([
            {**get_req(h["tilemap"], ["_tiles", "*", self.TILE_PROJECTION]), "handles": False},
            get_req(h["upgrades"], {"available": "_availableUpgrades.*", "pending": "pendingUpgradeChoices._size",
                                    "mothballed": "_mothballedUpgrades.*"}),
            get_req(h["clock"], {"week": "Week", "day": "Day", "hour": "Hour", "time": "Time",
                                 "expansion": "ExpansionTime", "paused": "isPaused"}),
            get_req(h["score"], "Score"),
            get_req(h["game"], "HasGameEnded"),
        ])
        for r in (tiles, upgrades, clock, score, ended):
            if isinstance(r, Exception):
                raise r
        playable = self._playable_tiles(clock["expansion"])

        roads, content, houses = {}, {}, []
        roundabouts, lights = set(), set()
        destinations: dict[int, Destination] = {}
        carparks: dict[int, Carpark] = {}
        for t in tiles:
            p = (t["x"], t["y"])
            roads[p] = [v & ~MOTHBALLED for v in t["r"]] if t["r"] else [0] * 8
            content[p] = Content(t["ct"])
            if _value(t.get("ra")):
                roundabouts.add(p)
            if _value(t.get("tl")):
                lights.add(p)
            if t["h"]:
                houses.append(House(p, t["h"]["g"], t["h"]["d"], t["h"]["w"], _value(t["h"].get("c"), 0)))
            if t["d"]:
                d = t["d"]
                key = d["obj"]["$p"]
                dest = destinations.setdefault(key, Destination(
                    key, d["g"], bool(d["a"]), d["w"], d["cp"]["$p"] if d["cp"] else None, bool(d["o"]),
                    unassigned=_value(d.get("ua"), 0), en_route=_value(d.get("er"), 0),
                    served=_value(d.get("sp"), 0), pin_limit=_value(d.get("lim"), 6),
                    overcrowd_time=float(_value(d.get("ot"), 0.0)), max_overcrowd_time=_value(d.get("mot"))))
                if _value(d.get("ua")) is None:
                    dest.unassigned = dest.waiting  # split unreadable: count every pin as unassigned
                dest.tiles.append(p)
            if t["p"]:
                key = t["p"]["obj"]["$p"]
                cp = carparks.setdefault(key, Carpark(key))
                cp.tiles.append(p)
                if not cp.entrances:
                    if t["p"]["tl"]:
                        cp.entrances.append((_pos(t["p"]["tlc"]), t["p"]["tld"]))
                    if t["p"]["br"]:
                        cp.entrances.append((_pos(t["p"]["brc"]), t["p"]["brd"]))
        join_roundabouts(roads, roundabouts)
        return State(roads, content, houses, destinations, carparks, upgrades["available"], upgrades["pending"],
                     clock["week"], clock["day"], clock["hour"], clock["time"], clock["expansion"],
                     bool(clock["paused"]), score, bool(ended), playable, roundabouts, lights,
                     mothballed=list(upgrades.get("mothballed") or [0] * 9))

    CAR_PROJECTION = {
        "id": "id", "state": "behaviorState", "dest": "destination",
        "lane": "CurrentFrame.lane", "carpark": "CurrentFrame.lane._isCarparkLane",
        "speed": "CurrentFrame.speed", "obstacle": "CurrentFrame.nearestObstacle",
        "gap": "CurrentFrame.distanceToLeadingVehicle", "path": "path.*", "path_length": "pathLength",
    }

    def read_cars(self) -> Traffic | None:
        """Every car, in one batched request: each house's ownedVehicles with the car's current
        frame and its remaining path (VehicleModel.path, the lanes still ahead). A lane has no
        coordinates, so the same request lists every tile's roadChunk.lanes and lanes are matched
        to tiles by object address. Diagonal-corner and car park lanes map to no tile: a car on a
        corner counts as on the tile it drives into, and car park lanes are left out of paths."""
        if not self.attach():
            return None
        h = self.h
        started = time.perf_counter()
        tiles, corners, houses, threshold, clock = self.b.batch([
            {**get_req(h["tilemap"], ["_tiles", "*", {"x": "key.m_X", "y": "key.m_Y",
                                                       "lanes": "value.roadChunk.lanes.*"}]), "handles": False},
            {**get_req(h["tilemap"], ["_tileCorners", "*", "value.roadChunk.lanes.*"]), "handles": False},
            {**get_req(h["tilemap"], ["_tiles", "*", "value._tile.ContentModel", "?HouseModel",
                                      {"x": "tileModel.Coordinates.m_X", "y": "tileModel.Coordinates.m_Y",
                                       "g": "GroupIndex", "cars": ["ownedVehicles", "*", self.CAR_PROJECTION]}]),
             "handles": False},
            get_req({"$class": "Motorways.Models.RoadChunkModel"}, "CarStoppedSpeedThreshold"),
            get_req(h["clock"], "Time"),
        ])
        read_ms = (time.perf_counter() - started) * 1000
        for r in (tiles, houses, clock):
            if isinstance(r, Exception):
                raise r
        lane_tile: dict[int, Pos] = {}
        for t in tiles:
            for lane in t.get("lanes") or []:
                if isinstance(lane, dict) and "$p" in lane:
                    lane_tile[lane["$p"]] = (t["x"], t["y"])
        corner_lanes = set()
        for lanes in corners if isinstance(corners, list) else []:
            corner_lanes.update(lane["$p"] for lane in lanes or [] if isinstance(lane, dict) and "$p" in lane)

        cars = []
        for house in houses:
            if not house:
                continue
            home = (house["x"], house["y"]) if _value(house.get("x")) is not None else None
            for c in house.get("cars") or []:
                path: list[Pos] = []
                for lane in c.get("path") or []:
                    t = lane_tile.get(lane["$p"]) if isinstance(lane, dict) else None
                    if t is not None and (not path or path[-1] != t):
                        path.append(t)
                lane = c.get("lane")
                lane_p = lane.get("$p") if isinstance(lane, dict) else None
                tile = lane_tile.get(lane_p)
                if tile is None and lane_p in corner_lanes and path:
                    tile = path[0]  # on a diagonal corner: count it on the tile it drives into
                gap = _value(c.get("gap"))
                dest = c.get("dest")
                cars.append(Car(
                    id=_value(c.get("id"), 0), house=home, colour=_value(house.get("g"), -1),
                    state=CarState(_value(c.get("state"), 0)),
                    destination=dest.get("$p") if isinstance(dest, dict) and "$p" in dest else None,
                    tile=tile, in_carpark=bool(_value(c.get("carpark"), False)),
                    speed=float(_value(c.get("speed"), 0.0)), obstacle=Obstacle(_value(c.get("obstacle"), 0)),
                    gap=None if gap is None or gap >= NO_LEADER_GAP else float(gap),
                    path=path, path_length=float(_value(c.get("path_length"), 0.0))))
        return Traffic(float(clock), cars, float(_value(threshold, 0.001)), read_ms)

    def _playable_tiles(self, expansion_time: float) -> frozenset[Pos]:
        """The game's own IsTileInPlayableArea, re-evaluated only when the city limits move."""
        area = self.b.call(self.h["city"], "GetSimulationPlayableAreaAtTime", expansion_time, 0)
        key = (area["x"], area["y"], area["width"], area["height"])
        if key != self._area_key:
            coords = list(self.terrain)
            inside = self.b.call_many(self.h["city"], "IsTileInPlayableArea",
                                      [[_vec(p), expansion_time] for p in coords])
            self._playable = frozenset(p for p, ok in zip(coords, inside) if ok is True or ok == 1)
            self._area_key = key
        return self._playable

    # Actions -------------------------------------------------------------------------------

    def add_roads(self, segments: list[tuple[Pos, int]], sequential: bool = False) -> list[int]:
        """Draw road segments (from tile, direction) the way DrawRoadAction does: validate each
        with TileEditor.AddRoad, then schedule the resulting edit on the simulation.
        Returns the TileEditResultCode for each segment (Success also when it already existed).

        Validation looks at the map as it is, so segments that depend on each other (bridges
        and tunnels, which must be drawn tile by tile like a drag) need sequential=True: each
        one is applied before the next is validated, and the rest are skipped after a failure."""
        if sequential:
            codes = []
            for p, d in segments:
                code = self._add_roads_now([(p, d)])[0]
                codes.append(code)
                if code not in (EDIT_SUCCESS, EDIT_ALREADY_EXISTS) or not self._wait_for_road(p, d):
                    break
            return codes + [EDIT_SKIPPED] * (len(segments) - len(codes))
        return self._add_roads_now(segments)

    def _wait_for_road(self, p: Pos, d: int, timeout: float = 3.0) -> bool:
        """Wait for the simulation to apply a scheduled segment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.b.call(self.h["tilemap"], "GetTileModel", _vec(p),
                                then=["_tile", "_twoLaneRoadState", f"#{d}"])
            if state:
                return True
            time.sleep(0.05)
        return False

    def _edit(self, method: str, args: list[list]) -> list[int]:
        """Validate edits with TileEditor.<method>(tilemap, *args) in one batch, then schedule the
        valid ones on the simulation as EditTileCommands, as the game's own tools do. Returns the
        TileEditResultCode for each (-1 where the call itself failed)."""
        h = self.h
        if not args:
            return []
        checks = self.b.batch([call_req(h["editor"], method, h["tilemap"], *a) for a in args])
        codes, schedule = [], []
        for result in checks:
            if isinstance(result, Exception):
                codes.append(-1)
                continue
            codes.append(result["resultCode"])
            if result["resultCode"] == EDIT_SUCCESS and result["edit"]:
                command = nested_call(EDIT_TILE_COMMAND, "Create", h["scope"], result["edit"])
                schedule.append(call_req(h["sim"], "ScheduleCommand", command))
        if schedule:
            for r in self.b.batch(schedule):
                if isinstance(r, Exception):
                    raise r
        return codes

    def _add_roads_now(self, segments: list[tuple[Pos, int]]) -> list[int]:
        return self._edit("AddRoad", [[_vec(p), d] for p, d in segments])

    def add_junction_upgrade(self, kind: Upgrade, p: Pos) -> int:
        """Put a roundabout or a traffic light on a junction tile, the way the game's own tools do:
        TileEditor.AddRoundabout / AddTrafficLight(ITilemap, Vector2Int) validate it, and the
        resulting edit is scheduled as an EditTileCommand. Returns the TileEditResultCode."""
        method = {Upgrade.ROUNDABOUT: "AddRoundabout", Upgrade.TRAFFIC_LIGHT: "AddTrafficLight"}[kind]
        return self._edit(method, [[_vec(p)]])[0]  # a failed validation call comes back as -1

    def clear_tiles(self, tiles: list[Pos], sequential: bool = False) -> list[int]:
        """Remove the roads on tiles with TileEditor.ClearTile (as the eraser does); their road
        tiles go back to the inventory. Returns a TileEditResultCode per tile.

        Like drawing, each clear is validated against the map as it is, so a chain has to be
        cleared one tile at a time from its loose end (sequential=True); a tile that doesn't
        disappear stops the rest, which come back as EDIT_SKIPPED."""
        if sequential:
            codes = []
            for p in tiles:
                code = self._clear_tiles_now([p])[0]
                if code == EDIT_SUCCESS and not self._wait_until_clear(p):
                    code = EDIT_SKIPPED
                codes.append(code)
                if code != EDIT_SUCCESS:
                    break
            return codes + [EDIT_SKIPPED] * (len(tiles) - len(codes))
        return self._clear_tiles_now(tiles)

    def _wait_until_clear(self, p: Pos, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            roads = self.b.call(self.h["tilemap"], "GetTileModel", _vec(p), then=["_tile", "_twoLaneRoadState", "*"])
            if not roads or not any(roads):
                return True
            time.sleep(0.05)
        return False

    def _clear_tiles_now(self, tiles: list[Pos]) -> list[int]:
        return self._edit("ClearTile", [[_vec(p), 0] for p in tiles])

    def game_speed(self) -> float:
        """The game's own speed (the play / fast-forward buttons' TimeScale) times its developer
        DebugTimescale multiplier."""
        scale = self.b.call(self.h["game"], "GetTimeScale", then=["_scale"])
        debug = self.b.get(self.h["game"], "_debugTimescale")
        return scale * (debug or 1.0)

    def set_game_speed(self, speed: float) -> float:
        """Run the game `speed` times faster. The game's own speed (its TimeScale, set by the play
        and fast-forward buttons) is left alone; MotorwaysGame._debugTimescale, the multiplier the
        developer tools use, is set to speed / TimeScale so that the two together give `speed`.
        Returns the speed asked of the game."""
        # TimeScale.FromScale returns Single for any value (it maps nothing), and SetTimeScale with the
        # ExtraFast instance only reached 2.9x, so the TimeScale isn't used; _debugTimescale measured
        # exactly 4.0x when set to 4.
        scale = self.b.call(self.h["game"], "GetTimeScale", then=["_scale"]) or 1.0
        self.b.set(self.h["game"], "_debugTimescale", float(speed / scale))
        return speed

    def upgrade_choices(self) -> list[dict]:
        """Packages offered by the first pending choice: {type, amount, additionalConcrete}."""
        choices = self.b.get(self.h["upgrades"], ["pendingUpgradeChoices", "#0", "choices", "*"])
        return [{"type": Upgrade(c["type"]), "amount": c["amount"], "concrete": c["additionalConcrete"]}
                for c in choices]

    def choose_upgrade(self, index: int, allow_command: bool = False) -> bool:
        """Pick a package through the upgrade screen's own handler, as a click would; that also
        closes the screen and resumes the game. Without a visible screen, schedules the selection
        command directly if allowed. Returns whether a choice was made."""
        screens = self.b.find_objects("Motorways.Views.GameUpgradeScreen")
        live = [sc for sc in screens if self.b.get(sc, "isActiveAndEnabled")]
        if live:
            self.b.call(live[0], "OnUpgradeSelect", index)
            return True
        if allow_command:
            command = nested_call(SELECT_UPGRADE_COMMAND, "Create", self.h["scope"], index)
            self.b.call(self.h["sim"], "ScheduleCommand", command)
            return True
        return False

    def start_game(self, map_name: str, mode: GameMode = GameMode.NORMAL) -> bool:
        """Start a new game the way the map menu does (GameStarter)."""
        library = self.b.find_all("Motorways.MapLibrary")[0]
        map_def = self.b.call(library, "GetMapByName", map_name)
        if map_def is None:
            raise BridgeError(f"no map named {map_name}")
        self.wait_for_main_menu()
        starter = self.b.new("Motorways.GameStarter", self.runtime)
        if not self.b.call(starter, "StartFromMapDefinition", map_def, int(mode), 0.0, False, False):
            return False
        for _ in range(120):
            if self.b.get(starter, "CanStart"):
                break
            time.sleep(0.25)
        screens = self.b.call(self.app_scope, "Get", typeof("ScreenStack"), sig=["System.Type"])
        started = bool(self.b.call(starter, "Start", screens, self.app_scope))
        self.game_key = None  # re-attach on next read
        return started

    def wait_for_main_menu(self, timeout: float = 60.0):
        """GameStarter only works once the main menu is on top and no screen is mid-transition."""
        screens = self.b.call(self.app_scope, "Get", typeof("ScreenStack"), sig=["System.Type"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.b.get(screens, {"pending": "_pendingScreen", "in": "_screenTransitioningIn",
                                     "out": "_screensTransitioningOut._size", "top": "_screenStack.#-1"},
                           handles=False)
            settled = s["pending"] is None and s["in"] is None and not s["out"]
            if settled and s["top"] and s["top"]["$t"].endswith("MainMenuScreen"):
                return
            time.sleep(0.5)
        raise BridgeError("the main menu never settled; open it and try again")

    def map_names(self) -> list[str]:
        library = self.b.find_all("Motorways.MapLibrary")[0]
        return self.b.get(library, "_maps.*.mapName")
