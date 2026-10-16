"""Sum up bot game logs: score, how the game was lost, connection trouble, routes and queues.

    python3 tools/summarize.py logs/test-*.log
    python3 tools/summarize.py --group on logs/a-*.log --group off logs/b-*.log   # compare settings

Counts come straight from the log lines the bot writes:
  could not connect   "no road found" (logged at most once per house or destination in a while)
  short of concrete   "needs N new road tiles, M left"
  stuck plan          "nothing left to draw, but still not connected"
  stranded            destinations still without any road ("no road found") on the last game day
  crossings reclaimed "taking down the bridge/tunnel" (to rebuild it for a cut-off destination)
  lost to             colours overcrowded on the last daily line, and the first destination to
                      overcrowd (its snapshot is saved as snapshot-...-overcrowded-colourN.json)
"""
import argparse
import re
import statistics
import sys
from pathlib import Path

GAME_OVER = re.compile(r"game over: score (\d+) in week (\d+)")
DAILY = re.compile(r"week (\d+) day (\d+): score (\d+).*?(?:, OVERCROWDED: (.*))?$")
FIRST_CROWD = re.compile(r"saved the map to (\S*week(\d+)-day(\d+)-overcrowded-colour(\d+)\.json)")
ROUTES = re.compile(r"routes: (\d+) checks, (\d+)% match")
TRAFFIC = re.compile(r"traffic: (\d+) cars, \d+ reads \((\d+) ms avg, (\d+) max\), up to (\d+) stopped in queues; "
                     r"jammed tiles: (.*)$")
JAM_LEVEL = re.compile(r"\) ([\d.]+)")


def summarize(path: Path) -> dict:
    text = path.read_text(errors="replace").splitlines()
    out = {"log": path.name, "score": None, "week": None, "lost_to": "", "first_crowd": "",
           "no_road": 0, "short": 0, "stuck": 0, "checks": 0, "matched": 0.0, "peak_queue": 0,
           "jam_days": 0, "max_jam": 0.0, "read_ms": [], "max_read_ms": 0, "jam_cost": "?", "error": "",
           "reclaimed": 0, "bridge_picks": 0, "stranded": set()}
    last_crowded = ""
    no_road_today: set = set()
    for line in text:
        if "jam cost on" in line:
            out["jam_cost"] = "on"
        elif "jam cost off" in line:
            out["jam_cost"] = "off"
        if m := GAME_OVER.search(line):
            out["score"], out["week"] = int(m.group(1)), int(m.group(2))
        if (m := DAILY.search(line)) and " traffic:" not in line:
            last_crowded = m.group(4) or ""
            out["stranded"], no_road_today = no_road_today, set()
        if m := re.search(r"(destination \(-?\d+, -?\d+\)) \(colour (\d+)\): no road found", line):
            no_road_today.add(f"{m.group(1)} colour {m.group(2)}")
        out["reclaimed"] += bool(re.search(r"taking down the (bridge|tunnel)", line))
        out["bridge_picks"] += bool(re.search(r"upgrade: .* -> bridge$", line))
        if (m := FIRST_CROWD.search(line)) and not out["first_crowd"]:
            out["first_crowd"] = f"colour {m.group(4)} (week {m.group(2)} day {m.group(3)})"
        out["no_road"] += "no road found" in line
        out["short"] += bool(re.search(r"needs \d+ new road tiles, \d+ left", line))
        out["stuck"] += "nothing left to draw" in line
        if m := ROUTES.search(line):
            n = int(m.group(1))
            out["checks"] += n
            out["matched"] += n * int(m.group(2)) / 100
        if m := TRAFFIC.search(line):
            out["read_ms"].append(int(m.group(2)))
            out["max_read_ms"] = max(out["max_read_ms"], int(m.group(3)))
            out["peak_queue"] = max(out["peak_queue"], int(m.group(4)))
            if m.group(5) != "none":
                out["jam_days"] += 1
                out["max_jam"] = max([out["max_jam"], *map(float, JAM_LEVEL.findall(m.group(5)))])
        if "Traceback" in line and not out["error"]:
            out["error"] = "crashed (Traceback)"
    out["lost_to"] = last_crowded
    out["stranded"] = sorted(out["stranded"] | no_road_today)
    out["match_pct"] = 100 * out["matched"] / out["checks"] if out["checks"] else None
    return out


def show(rows: list[dict], label: str = ""):
    if label:
        print(f"\n== {label} ==")
    for r in rows:
        score = "no game over line" if r["score"] is None else f"score {r['score']:>5} week {r['week']:>2}"
        match = "-" if r["match_pct"] is None else f"{r['match_pct']:.0f}%"
        ms = f"{statistics.mean(r['read_ms']):.0f}/{r['max_read_ms']}" if r["read_ms"] else "-"
        print(f"{r['log']}: jam cost {r['jam_cost']}, {score}; first overcrowded {r['first_crowd'] or '-'}; "
              f"overcrowded at the end: {r['lost_to'] or '-'}")
        print(f"    stranded at the end: {', '.join(r['stranded']) or 'none'}; crossings reclaimed {r['reclaimed']}; "
              f"bridges picked {r['bridge_picks']}")
        print(f"    could not connect {r['no_road']}, short of concrete {r['short']}, stuck plan {r['stuck']}; "
              f"routes match {match} of {r['checks']} checks; peak queue {r['peak_queue']} cars, "
              f"{r['jam_days']} days with jammed tiles (max level {r['max_jam']:.1f}); car read ms avg/max {ms}"
              f"{'; ' + r['error'] if r['error'] else ''}")
    done = [r for r in rows if r["score"] is not None]
    if done:
        scores = [r["score"] for r in done]
        print(f"  {len(done)} games: score mean {statistics.mean(scores):.0f}, median {statistics.median(scores):.0f}, "
              f"min {min(scores)}, max {max(scores)}; week mean {statistics.mean(r['week'] for r in done):.1f}; "
              f"could not connect {sum(r['no_road'] for r in rows)}, stuck plan {sum(r['stuck'] for r in rows)}, "
              f"short of concrete {sum(r['short'] for r in rows)}; lost with a destination stranded "
              f"{sum(1 for r in done if r['stranded'])}; crossings reclaimed {sum(r['reclaimed'] for r in rows)}; "
              f"peak queue mean "
              f"{statistics.mean(r['peak_queue'] for r in rows):.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="*", type=Path)
    parser.add_argument("--group", nargs="+", action="append", metavar=("NAME", "LOG"),
                        help="a named group of logs, to compare settings side by side")
    args = parser.parse_args()
    if args.group:
        for name, *logs in args.group:
            show([summarize(Path(p)) for p in logs], name)
    if args.logs:
        show([summarize(p) for p in args.logs])
    if not args.group and not args.logs:
        parser.print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
