"""Play a bot over several seeded games and print the results.

    python3 -m mmsim.run examples/greedy_bot.py
    python3 -m mmsim.run examples/greedy_bot.py --games 20 --map two-rivers --seed 100

A bot file defines `class Bot` with a method `step(self, game)`. A new Bot is made for each game,
and step() is called every LOOK_EVERY game seconds (also while the game is paused for an upgrade
choice). Each seed is the same game every time, so bots can be compared seed by seed.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import statistics
import sys
import time

from .game import Game
from .world import MAPS, WEEK

LOOK_EVERY = 0.4  # game seconds between calls to bot.step
MAX_WEEKS = 30


def load_bot(path: str):
    """The Bot class from a bot file."""
    spec = importlib.util.spec_from_file_location(pathlib.Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "Bot"):
        raise SystemExit(f"{path} has no class Bot")
    return module.Bot


def play(bot_class, seed: int = 1, map: str = "river", max_weeks: int = MAX_WEEKS) -> dict:
    """One game; returns its result."""
    game = Game(seed, map)
    bot = bot_class()
    started = time.perf_counter()
    while not game.over and game.time < max_weeks * WEEK:
        bot.step(game)
        game.advance(LOOK_EVERY)
    return {"seed": seed, "score": game.score, "week": game.week, "day": game.day, "over": game.over,
            "houses": len(game.houses), "destinations": len(game.destinations),
            "lost_to": [f"colour {d.colour} at {d.tiles[0]}" for d in game.destinations if d.timer >= d.max_timer],
            "seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bot", help="a Python file defining class Bot")
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1, help="first seed; games use seed, seed+1, ...")
    parser.add_argument("--map", default="river", choices=sorted(MAPS))
    parser.add_argument("--max-weeks", type=int, default=MAX_WEEKS)
    args = parser.parse_args()
    bot_class = load_bot(args.bot)
    results = []
    for seed in range(args.seed, args.seed + args.games):
        r = play(bot_class, seed, args.map, args.max_weeks)
        results.append(r)
        end = f"lost to {', '.join(r['lost_to'])}" if r["over"] else "still going at the week limit"
        print(f"seed {seed:>3}: score {r['score']:>5}, week {r['week']:>2} day {r['day']:>3}, {end}; "
              f"{r['houses']} houses, {r['destinations']} destinations; {r['seconds']:.1f} s", flush=True)
    scores = [r["score"] for r in results]
    print(f"{len(results)} games on {args.map}: score mean {statistics.mean(scores):.0f}, median "
          f"{statistics.median(scores):.0f}, min {min(scores)}, max {max(scores)}; "
          f"week mean {statistics.mean(r['week'] for r in results):.1f}")


if __name__ == "__main__":
    main()
