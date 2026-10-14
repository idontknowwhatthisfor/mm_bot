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


def join_roundabouts(roads: dict[Pos, list[int]], roundabouts: set[Pos]):
    """A roundabout's centre tile keeps its connections in the roundabout, not in its two-lane road
    state, which reads as empty. Mark it as joined to every neighbour whose road points into it, so
    networks, routes and dead-end checks see a junction there rather than a gap."""
    for p in roundabouts:
        r = list(roads.get(p) or [0] * 8)
        for d in range(8):
            q = roads.get(neighbor(p, d))
            if q and q[opposite(d)]:
                r[d] = r[d] or 8
        roads[p] = r


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
            get_req(h["upgrades"], {"available": "_availableUpgrades.*", "pending": "pendingUpgradeChoices._size"}),
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
            roads[p] = t["r"] or [0] * 8
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
                     bool(clock["paused"]), score, bool(ended), playable, roundabouts, lights)

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

    def _add_roads_now(self, segments: list[tuple[Pos, int]]) -> list[int]:
        h = self.h
        if not segments:
            return []
        checks = self.b.batch([call_req(h["editor"], "AddRoad", h["tilemap"], _vec(p), d)
                               for p, d in segments])
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

    def add_junction_upgrade(self, kind: Upgrade, p: Pos) -> int:
        """Put a roundabout or a traffic light on a junction tile, the way the game's own tools do:
        TileEditor.AddRoundabout / AddTrafficLight(ITilemap, Vector2Int) validate it, and the
        resulting edit is scheduled as an EditTileCommand. Returns the TileEditResultCode."""
        method = {Upgrade.ROUNDABOUT: "AddRoundabout", Upgrade.TRAFFIC_LIGHT: "AddTrafficLight"}[kind]
        h = self.h
        try:
            result = self.b.call(h["editor"], method, h["tilemap"], _vec(p))
        except BridgeError:
            return -1
        code = result["resultCode"]
        if code == EDIT_SUCCESS and result["edit"]:
            command = nested_call(EDIT_TILE_COMMAND, "Create", h["scope"], result["edit"])
            self.b.call(h["sim"], "ScheduleCommand", command)
        return code

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
        h = self.h
        if not tiles:
            return []
        checks = self.b.batch([call_req(h["editor"], "ClearTile", h["tilemap"], _vec(p), 0) for p in tiles])
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

    TIME_SCALE = {"$class": "TimeScale"}
    TIME_SCALES = ("SingleSlow", "Single", "DoubleSlow", "Double", "ExtraFast")

    def game_speed(self) -> float:
        """The game's own speed (the play / fast-forward buttons' TimeScale) times its developer
        DebugTimescale multiplier."""
        scale = self.b.call(self.h["game"], "GetTimeScale", then=["_scale"])
        debug = self.b.get(self.h["game"], "_debugTimescale")
        return scale * (debug or 1.0)

    def set_game_speed(self, speed: float) -> float:
        """Run the game `speed` times faster: the fastest of the game's own speeds (Game.SetTimeScale
        takes one of the TimeScale instances, as the fast-forward buttons do) that doesn't overshoot,
        and the rest through MotorwaysGame.DebugTimescale, the multiplier the developer tools use.
        Returns the speed asked of the game."""
        # TimeScale.FromScale returns Single for any value (it maps nothing), and SetTimeScale with the
        # ExtraFast instance only reached 2.9x, so leave the TimeScale alone and scale the rest with
        # _debugTimescale, which measured exactly 4.0x when set to 4.
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
