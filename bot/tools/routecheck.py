"""Replay the route check on saved snapshots (the ones with cars in them) and print how often the
planner's route model agrees with the routes the game gave its cars.

    python3 tools/routecheck.py logs/snapshot-*.json
    python3 tools/routecheck.py --park-factor 1 2 3 -- logs/snapshot-*.json   # compare settings
"""
import argparse
import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import planner  # noqa: E402
from game import load_snapshot, load_traffic  # noqa: E402
from traffic import check_routes  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("snapshots", nargs="+")
    parser.add_argument("--park-factor", type=float, nargs="*", default=[planner.PARK_DRIVE_FACTOR],
                        help="PARK_DRIVE_FACTOR values to compare")
    parser.add_argument("--worst", type=int, default=0, help="also list this many of the worst mismatches")
    parser.add_argument("--no-drive-through", action="store_true",
                        help="model every car park as a dead end (the planner before cars were read)")
    args = parser.parse_args()
    planner.DRIVE_THROUGH_CARPARKS = not args.no_drive_through
    loaded = []
    for f in args.snapshots:
        data = json.loads(pathlib.Path(f).read_text())
        traffic = load_traffic(data)
        if traffic is not None:
            loaded.append((f, *load_snapshot(data), traffic))
    print(f"{len(loaded)} of {len(args.snapshots)} snapshots have cars")
    for factor in args.park_factor:
        planner.PARK_DRIVE_FACTOR = factor
        total = Counter()
        on_routes = 0
        worst = []
        for f, state, terrain, traffic in loaded:
            for r in check_routes(state, traffic):
                total[r.kind] += 1
                on_routes += r.on_house_routes
                if r.kind in ("different", "off route"):
                    worst.append((r.difference, pathlib.Path(f).name, r))
        n = sum(total.values()) or 1
        print(f"PARK_DRIVE_FACTOR {factor}: {n} car checks, " + ", ".join(
            f"{kind} {100 * total[kind] / n:.1f}%" for kind in ("match", "tie", "different", "off route"))
              + f"; on house_routes {100 * on_routes / n:.1f}%")
        for diff, name, r in sorted(worst, key=lambda w: -w[0])[:args.worst]:
            print(f"   {name}: car {r.car.id} house {r.car.house} {r.car.state.name} at {r.car.tile}: {r.kind}, "
                  f"{diff} tiles off plan; game {r.game_length:.1f} planned {r.planned_length:.1f}")


if __name__ == "__main__":
    main()
