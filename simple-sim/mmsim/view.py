"""Watch a bot play.

    python3 -m mmsim.view examples/greedy_bot.py
    python3 -m mmsim.view examples/greedy_bot.py --seed 4 --map two-rivers
    python3 -m mmsim.view                  # no bot: just the world, nothing gets built

Space pauses, S steps once, + / - change the speed, N starts the next seed.
"""

from __future__ import annotations

import argparse
import collections
import tkinter as tk

from .core import CarState, Upgrade, neighbor
from .game import Game
from .run import LOOK_EVERY, load_bot
from .world import MAPS, MAX_OVERCROWD, PIN_LIMIT

CELL = 22
PANEL = 330
FRAME_MS = 50
SPEEDS = [1, 2, 4, 8, 16, 32]  # bot looks (0.4 game s each) per frame
COLOURS = ["#e8555a", "#4aa3df", "#f2b33d", "#58c27d", "#a77be0", "#ef7fb4"]
LAND, OUTSIDE, WATER, ROCK, ROAD = "#2d323c", "#1f2229", "#355e8c", "#6e5a48", "#9aa0aa"
TEXT, DIM = "#e6e6e6", "#8a8f99"


class LogTail:
    """The bot's latest messages: anything it puts in `self.log` (a list of strings), if it has one."""

    def __init__(self, lines: int = 14):
        self.lines = collections.deque(maxlen=lines)


class Viewer:
    def __init__(self, seed: int, style: str, bot_class):
        self.seed, self.style, self.bot_class = seed, style, bot_class
        self.paused, self.speed_i = False, 2
        self.log = LogTail()
        self.root = tk.Tk()
        self.root.title("Mini Motorways simulator")
        self.root.configure(bg=OUTSIDE)
        self.new_game(seed)
        w, h = self.world.style.width, self.world.style.height
        self.canvas = tk.Canvas(self.root, width=w * CELL + PANEL, height=max(h * CELL, 560),
                                bg=OUTSIDE, highlightthickness=0)
        self.canvas.pack()
        for key, fn in (("<space>", self.toggle), ("s", self.step_once), ("S", self.step_once),
                        ("+", self.faster), ("=", self.faster), ("-", self.slower),
                        ("n", self.next_seed), ("N", self.next_seed)):
            self.root.bind(key, lambda e, fn=fn: fn())
        self.root.after(FRAME_MS, self.tick)

    # Game ---------------------------------------------------------------------------------------

    def new_game(self, seed: int):
        self.seed = seed
        self.game = Game(seed, self.style)
        self.world = self.game.world
        self.bot = self.bot_class() if self.bot_class else None
        self.final = None
        self.log.lines.clear()

    def look(self):
        """One bot look, then 0.4 game seconds."""
        if self.final is not None:
            return
        if self.bot is not None:
            self.bot.step(self.game)
            new = getattr(self.bot, "log", None)
            if isinstance(new, list) and new:
                self.log.lines.extend(str(m) for m in new)
                new.clear()
        elif self.world.choices:
            self.world.choose(0)
        if self.world.ended:
            self.final = self.world.score
            return
        self.game.advance(LOOK_EVERY)

    def tick(self):
        if not self.paused:
            for _ in range(SPEEDS[self.speed_i]):
                self.look()
                if self.final is not None:
                    break
        self.draw()
        self.root.after(FRAME_MS, self.tick)

    def toggle(self):
        self.paused = not self.paused

    def step_once(self):
        self.paused = True
        self.look()

    def faster(self):
        self.speed_i = min(len(SPEEDS) - 1, self.speed_i + 1)

    def slower(self):
        self.speed_i = max(0, self.speed_i - 1)

    def next_seed(self):
        self.new_game(self.seed + 1)

    # Drawing ------------------------------------------------------------------------------------

    def xy(self, p, frac=(0.5, 0.5)):
        h = self.world.style.height
        return (p[0] + frac[0]) * CELL, (h - 1 - p[1] + 1 - frac[1]) * CELL

    def draw(self):
        c, w = self.canvas, self.world
        c.delete("all")
        area = w.playable()
        for p, t in w.terrain.items():
            fill = WATER if t.water else ROCK if t.mountain else LAND if p in area else OUTSIDE
            if p not in area and (t.water or t.mountain):
                fill = "#26364a" if t.water else "#3d342b"
            x, y = self.xy(p, (0, 1))
            c.create_rectangle(x, y, x + CELL, y + CELL, fill=fill, outline="")
        # roads: each joined link once, plus half-links (stubs)
        for p, r in w.roads.items():
            for d in range(8):
                if not r[d]:
                    continue
                q = neighbor(p, d)
                x0, y0 = self.xy(p)
                x1, y1 = self.xy(q)
                if w.joined(p, d):
                    if p < q:
                        c.create_line(x0, y0, x1, y1, fill=ROAD, width=CELL * 0.42, capstyle="round")
                else:
                    c.create_line(x0, y0, (x0 + x1) / 2, (y0 + y1) / 2, fill=ROAD, width=CELL * 0.3)
        for p in w.roundabouts | w.lights:
            x, y = self.xy(p)
            r = CELL * 0.35
            c.create_oval(x - r, y - r, x + r, y + r, outline="#ffffff",
                          width=2, dash=(3, 2) if p in w.lights else None)
        for d in w.destinations.values():
            col = COLOURS[d.group % len(COLOURS)]
            xs = [self.xy(t, (0, 1)) for t in d.tiles]
            x0, y0 = min(x for x, _ in xs), min(y for _, y in xs)
            c.create_rectangle(x0 + 1, y0 + 1, x0 + 2 * CELL - 1, y0 + 2 * CELL - 1, fill=col, outline="")
            for t in d.park:
                x, y = self.xy(t, (0, 1))
                c.create_rectangle(x + 2, y + 2, x + CELL - 2, y + CELL - 2, outline=col, width=2)
            pins = d.unassigned + d.en_route
            c.create_text(x0 + CELL, y0 + CELL * 0.75, text=str(pins), fill="#111", font=("Helvetica", 12, "bold"))
            if d.overcrowd_time > 0:  # the timer as a ring filling up
                extent = -359.9 * d.overcrowd_time / MAX_OVERCROWD
                c.create_arc(x0 - 3, y0 - 3, x0 + 2 * CELL + 3, y0 + 2 * CELL + 3, start=90, extent=extent,
                             style="arc", outline="#ff4040", width=3)
        for h in w.houses:
            col = COLOURS[h.group % len(COLOURS)]
            x, y = self.xy(h.pos)
            s = CELL * 0.32
            c.create_rectangle(x - s, y - s, x + s, y + s, fill=col, outline="")
        for car in w.cars:
            if car.state not in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) or car.at is None:
                continue
            x0, y0 = self.xy(car.at)
            if car.path:
                x1, y1 = self.xy(car.path[0])
                x0, y0 = x0 + (x1 - x0) * car.progress, y0 + (y1 - y0) * car.progress
            r = CELL * 0.17
            col = COLOURS[car.house.group % len(COLOURS)]
            stopped = car.speed < 0.01
            c.create_oval(x0 - r, y0 - r, x0 + r, y0 + r, fill=col,
                          outline="#ff3030" if stopped else "#111", width=2 if stopped else 1)
        self.draw_panel()

    def draw_panel(self):
        c, w = self.canvas, self.world
        x = w.style.width * CELL + 14
        y = 12
        line = lambda text, colour=TEXT, size=11, bold=False, gap=17: (  # noqa: E731
            c.create_text(x, y, text=text, anchor="nw", fill=colour,
                          font=("Helvetica", size, "bold" if bold else "normal")), gap)
        rows = []
        status = "GAME OVER" if self.final is not None else "paused" if self.paused else f"x{SPEEDS[self.speed_i]}"
        rows.append((f"Seed {self.seed} · {self.style} · {status}", TEXT, 13, True, 22))
        rows.append((f"Week {w.week}  day {w.day}   score {w.score}", TEXT, 13, True, 24))
        stock = (f"roads {w.available[Upgrade.CONCRETE]}  bridges {w.available[Upgrade.BRIDGE]}"
                 f"  tunnels {w.available[Upgrade.TUNNEL]}")
        rows.append((stock, DIM, 11, False, 16))
        rows.append((f"roundabouts {w.available[Upgrade.ROUNDABOUT]}  lights {w.available[Upgrade.TRAFFIC_LIGHT]}"
                     + (f"  (+{w.mothballed[Upgrade.BRIDGE]} bridge coming back)" if w.mothballed[Upgrade.BRIDGE] else ""),
                     DIM, 11, False, 20))
        driving = sum(car.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) for car in w.cars)
        stopped = sum(car.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) and car.speed < 0.01
                      for car in w.cars)
        rows.append((f"{len(w.houses)} houses, {driving} cars driving, {stopped} stopped", TEXT, 11, False, 24))
        rows.append(("Destinations (pins / timer)", TEXT, 11, True, 18))
        for d in sorted(w.destinations.values(), key=lambda d: -(d.unassigned + d.en_route)):
            pins = d.unassigned + d.en_route
            colour = "#ff6060" if d.overcrowd_time > 0 else TEXT
            rows.append((f"  ■ colour {d.group}: {pins}/{PIN_LIMIT} pins, timer {d.overcrowd_time:.0f}/{MAX_OVERCROWD:.0f}",
                         colour, 10, False, 15))
        y += 4
        for text, colour, size, bold, gap in rows:
            item, _ = line(text, colour, size, bold)
            if text.startswith("  ■"):
                d_colour = COLOURS[int(text.split("colour ")[1].split(":")[0]) % len(COLOURS)]
                c.create_rectangle(x + 2, y + 3, x + 11, y + 12, fill=d_colour, outline="")
                c.itemconfigure(item, text="     " + text.strip()[2:])
            y += gap
        y += 10
        line("Bot log", TEXT, 11, True)
        y += 18
        for msg in self.log.lines:
            text = msg if len(msg) <= 58 else msg[:57] + "…"
            c.create_text(x, y, text=text, anchor="nw", fill=DIM, font=("Helvetica", 9))
            y += 14
        c.create_text(x, max(y + 10, w.style.height * CELL - 20), anchor="nw", fill=DIM, font=("Helvetica", 9),
                      text="space pause · s step · +/- speed · n next seed")

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bot", nargs="?", help="a Python file defining class Bot (none: just watch the world)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--map", default="river", choices=sorted(MAPS))
    args = parser.parse_args()
    Viewer(args.seed, args.map, load_bot(args.bot) if args.bot else None).run()


if __name__ == "__main__":
    main()
