"""Probe: every car in the running game, read in one batched request.

    python3 tools/cars.py                 # table of cars, then sanity statistics and timing
    python3 tools/cars.py --samples 3     # read 3 times, 1 s of real time apart
    python3 tools/cars.py --dump cars.json

Cars are reached through each house's ownedVehicles. A lane has no coordinates of its own, so lanes
are mapped to tiles by listing every tile's (and every diagonal corner's) roadChunk.lanes in the same
request and matching on the lane object's address ("$p").
"""
import argparse
import json
import pathlib
import statistics
import sys
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from game import Session  # noqa: E402
from mmbridge import Bridge, get_req  # noqa: E402

STATES = ["WaitingForDestination", "DrivingToDestination", "ParkingAtDestination", "ParkedAtDestination",
          "DrivingHome", "RealigningDriveway"]
OBSTACLES = ["None", "Target", "LeadingVehicle", "BlockingIntersection", "HotswappingLane"]

CAR = {"obj": [], "id": "id", "state": "behaviorState", "dest": "destination",
       "lane": "CurrentFrame.lane", "along": "CurrentFrame.distanceAlongLane", "speed": "CurrentFrame.speed",
       "acc": "CurrentFrame.acceleration", "obstacle": "CurrentFrame.nearestObstacle",
       "leader": "CurrentFrame.leadingVehicle", "gap": "CurrentFrame.distanceToLeadingVehicle",
       "path": "path.*", "path_length": "pathLength", "blocker": "blockingVehicle"}


def read(session):
    h = session.h
    requests = [
        {**get_req(h["tilemap"], ["_tiles", "*", {"x": "key.m_X", "y": "key.m_Y", "lanes": "value.roadChunk.lanes.*"}]),
         "handles": False},
        {**get_req(h["tilemap"], ["_tileCorners", "*", {"x": "key.tileCoordinate.m_X", "y": "key.tileCoordinate.m_Y",
                                                         "d": "key.cornerDirection",
                                                         "lanes": "value.roadChunk.lanes.*"}]),
         "handles": False},
        {**get_req(h["tilemap"], ["_tiles", "*", "value._tile.ContentModel", "?HouseModel",
                                  {"pos": "tileModel.Coordinates", "colour": "GroupIndex",
                                   "cars": ["ownedVehicles", "*", CAR]}]),
         "handles": False},
        get_req({"$class": "Motorways.Models.RoadChunkModel"}, "CarStoppedSpeedThreshold"),
        get_req(h["clock"], "Time"),
    ]
    started = time.perf_counter()
    tiles, corners, houses, threshold, clock = session.b.batch(requests)
    elapsed = time.perf_counter() - started
    return tiles, corners, houses, threshold, clock, elapsed


def lane_tiles(tiles, corners):
    """lane address -> tile; corner lanes go to the corner's own tile coordinate."""
    out = {}
    for t in tiles if isinstance(tiles, list) else []:
        for lane in t.get("lanes") or []:
            if isinstance(lane, dict) and "$p" in lane:
                out[lane["$p"]] = (t["x"], t["y"])
    corner_lanes = 0
    for c in corners if isinstance(corners, list) else []:
        for lane in c.get("lanes") or []:
            if isinstance(lane, dict) and "$p" in lane:
                out.setdefault(lane["$p"], (c["x"], c["y"]))
                corner_lanes += 1
    return out, corner_lanes


def path_tiles(path, lanes):
    tiles = []
    for lane in path or []:
        t = lanes.get(lane["$p"]) if isinstance(lane, dict) else None
        if t is not None and (not tiles or tiles[-1] != t):
            tiles.append(t)
    return tiles


def num(v, default=None):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def show(sample):
    tiles, corners, houses, threshold, clock, elapsed = sample
    for name, value in (("tiles", tiles), ("corners", corners), ("houses", houses)):
        if isinstance(value, Exception):
            print(f"{name}: request failed: {value}")
            return
    lanes, corner_lanes = lane_tiles(tiles, corners)
    rows = []
    for h in houses:
        if not h:
            continue
        pos = (h["pos"]["m_X"], h["pos"]["m_Y"]) if isinstance(h.get("pos"), dict) and "m_X" in h["pos"] else None
        for c in h.get("cars") or []:
            rows.append((pos, h.get("colour"), c))
    print(f"game time {clock:.1f}s; {len(tiles)} tiles, {len(lanes)} lanes ({corner_lanes} on corners); "
          f"{len(rows)} cars; CarStoppedSpeedThreshold = {threshold}; one batched read took {elapsed * 1000:.0f} ms")
    print(f"{'house':>10} col {'state':<22} {'tile':>10} {'speed':>6} {'obstacle':<20} {'gap':>6} path")
    for pos, colour, c in sorted(rows, key=lambda r: (r[1] or 0, r[0] or (0, 0))):
        lane = c.get("lane")
        tile = lanes.get(lane["$p"]) if isinstance(lane, dict) else None
        state = num(c.get("state"))
        obstacle = num(c.get("obstacle"))
        speed, gap = num(c.get("speed")), num(c.get("gap"))
        print(f"{str(pos):>10} {colour!s:>3} {STATES[state] if state is not None else c.get('state')!s:<22} "
              f"{str(tile) if tile else ('-' if lane is None else 'unmapped'):>10} "
              f"{'' if speed is None else f'{speed:6.2f}':>6} "
              f"{OBSTACLES[obstacle] if obstacle is not None else c.get('obstacle')!s:<20} "
              f"{'' if gap is None else f'{gap:6.2f}':>6} {len(path_tiles(c.get('path'), lanes))}")

    # Sanity: are moving cars fast and queued cars stopped?
    states = Counter(num(c.get("state")) for _, _, c in rows)
    print("\nby state:", {STATES[s] if s is not None else "?": n for s, n in sorted(states.items(), key=str)})
    driving = [c for _, _, c in rows if num(c.get("state")) in (1, 4)]
    speeds = [num(c.get("speed")) for c in driving if num(c.get("speed")) is not None]
    if speeds:
        print(f"driving cars: {len(driving)}; speed min {min(speeds):.3f} median {statistics.median(speeds):.3f} "
              f"max {max(speeds):.3f}")
        if isinstance(threshold, (int, float)):
            stopped = [c for c in driving if num(c.get("speed"), 1e9) < threshold]
            print(f"  stopped (speed < threshold): {len(stopped)}; by obstacle: "
                  f"{dict(Counter(OBSTACLES[num(c.get('obstacle'), 0)] for c in stopped))}")
            queued = [c for c in driving if num(c.get("obstacle")) == 2]
            if queued:
                q_speeds = [num(c.get("speed"), 0) for c in queued]
                print(f"  behind a leading vehicle: {len(queued)}, median speed {statistics.median(q_speeds):.3f}, "
                      f"median gap {statistics.median(num(c.get('gap'), 0) for c in queued):.3f}")
    unmapped = sum(1 for _, _, c in rows if isinstance(c.get("lane"), dict) and c["lane"]["$p"] not in lanes)
    no_lane = sum(1 for _, _, c in rows if c.get("lane") is None)
    errors = Counter(k for _, _, c in rows for k, v in c.items() if isinstance(v, dict) and "$err" in v)
    print(f"cars with no lane: {no_lane}; lane not found on any tile: {unmapped}; unreadable fields: {dict(errors)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--dump", metavar="FILE", help="write the raw reads as JSON")
    args = parser.parse_args()
    with Bridge(timeout=30) as b:
        session = Session(b)
        if not session.attach():
            sys.exit("no game running")
        samples = []
        for i in range(args.samples):
            if i:
                time.sleep(1.0)
                print("\n" + "-" * 100)
            sample = read(session)
            samples.append(sample)
            show(sample)
        if args.dump:
            pathlib.Path(args.dump).write_text(json.dumps(
                [{"tiles": s[0], "corners": s[1], "houses": s[2], "threshold": s[3], "time": s[4], "ms": s[5] * 1000}
                 for s in samples], default=str))


if __name__ == "__main__":
    main()
