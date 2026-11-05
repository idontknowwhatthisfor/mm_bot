"""Play the bot against the simulator, as fast as the CPU allows.

    python3 -m sim.run                          # 5 games on the "river" map, seeds 1-5
    python3 -m sim.run --games 20 --map two-rivers
    python3 -m sim.run --seed 7 --verbose       # one game with the bot's full log
    python3 -m sim.run --no-jam-cost

Maps: river, two-rivers, plain, mountains (sim/world.py MAPS). Each seed gives the same game for
the same bot, so two versions of the bot can be compared seed by seed.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import planner  # noqa: E402
from bot import Bot  # noqa: E402
from sim.session import SimSession  # noqa: E402
from sim.world import MAPS, WEEK, World  # noqa: E402

LOOK_EVERY = 0.4  # game seconds between the bot's looks, as at normal speed in the real game
MAX_WEEKS = 30  # stop a game that hasn't ended by then


def play(seed: int, style: str = "river", max_weeks: int = MAX_WEEKS) -> dict:
    world = World(seed, style)
    session = SimSession(world, f"sim {style} seed {seed}")
    bot = Bot(session)
    bot.sleep = lambda seconds: None
    bot.snapshot_dir = None
    started = time.perf_counter()
    while True:
        score = bot.step(session.read())
        if score is not None or world.time >= max_weeks * WEEK:
            break
        world.advance(LOOK_EVERY)
    lost = [d for d in world.destinations.values() if d.overcrowd_time >= 50]
    return {"seed": seed, "score": world.score, "week": world.week, "day": world.day,
            "ended": world.ended, "houses": len(world.houses), "destinations": len(world.destinations),
            "lost_to": [f"colour {d.group} at {d.tiles[0]}" for d in lost],
            "unreached": sum(1 for d in world.destinations.values() if d.served == 0),
            "seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1, help="first seed; games use seed, seed+1, ...")
    parser.add_argument("--map", default="river", choices=sorted(MAPS))
    parser.add_argument("--max-weeks", type=int, default=MAX_WEEKS)
    parser.add_argument("--no-jam-cost", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="show the bot's log")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s")
    if args.no_jam_cost:
        planner.USE_JAM_COST = False
    results = []
    for seed in range(args.seed, args.seed + args.games):
        r = play(seed, args.map, args.max_weeks)
        results.append(r)
        print(f"seed {seed:>3}: score {r['score']:>5}, week {r['week']:>2} day {r['day']:>3}, "
              f"{'lost to ' + ', '.join(r['lost_to']) if r['ended'] else 'still running at the week limit'}; "
              f"{r['houses']} houses, {r['destinations']} destinations ({r['unreached']} never reached); "
              f"{r['seconds']:.0f} s", flush=True)
    scores = [r["score"] for r in results]
    print(f"{len(results)} games on {args.map}: score mean {statistics.mean(scores):.0f}, median "
          f"{statistics.median(scores):.0f}, min {min(scores)}, max {max(scores)}; "
          f"week mean {statistics.mean(r['week'] for r in results):.1f}")


if __name__ == "__main__":
    main()
