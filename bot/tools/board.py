"""Debug view of the running game: destination demand and an ASCII map of the board.

Lowercase letters are houses, uppercase letters destinations (same letter = same colour),
P car parks, ~ water, ^ mountains, t trees, road tiles drawn by their connections.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from game import Content, Session  # noqa: E402
from mmbridge import Bridge  # noqa: E402
from planner import Network, group_roots, is_served  # noqa: E402

ROAD_GLYPHS = {frozenset(): "o", frozenset("NS"): "|", frozenset("EW"): "-", frozenset("N"): "|", frozenset("S"): "|",
               frozenset("E"): "-", frozenset("W"): "-"}


def road_glyph(dirs: list[int]) -> str:
    names = frozenset(n for n, d in zip("NESW", (0, 2, 4, 6)) if dirs[d])
    return ROAD_GLYPHS.get(names, "+")


def main():
    with Bridge(timeout=30) as b:
        s = Session(b)
        st = s.read()
        if st is None:
            sys.exit("no game running")
        dests = b.get(s.h["tilemap"], ["_tiles", "*", "value._tile.ContentModel", "?DestinationModel", {
            "colour": "GroupIndex", "pins": "TotalDemand", "served": "totalServicedPins", "supply": "contributedSupply",
            "required": "RequiredSupply", "enough": "IsSupplySufficient", "crowded": "IsOvercrowding",
            "at": "TileModels.#0.Coordinates", "obj": []}], handles=False)
        seen = set()
        for d in dests:
            if d and d["obj"]["$p"] not in seen:
                seen.add(d["obj"]["$p"])
                print("destination {} colour {colour}: {pins} pins, {served} served, supply {supply:.2f}/{required:.2f}{}".format(
                    (d["at"]["m_X"], d["at"]["m_Y"]), "" if not d["crowded"] else "  OVERCROWDED", **d))
        net = Network(st)
        roots = group_roots(st, net)
        print(f"week {st.week} day {st.day}, score {st.score}, roads left {st.available[0]}, "
              f"houses connected {sum(is_served(h, net, roots) for h in st.houses)}/{len(st.houses)}, "
              f"cars waiting at houses {sum(h.waiting for h in st.houses)}")
        houses = {h.pos: h for h in st.houses}
        groups = {p: d.group for d in st.destinations.values() for p in d.tiles}
        xs = [p[0] for p in s.terrain]
        ys = [p[1] for p in s.terrain]
        for y in range(max(ys), min(ys) - 1, -1):
            row = []
            for x in range(min(xs), max(xs) + 1):
                p = (x, y)
                c = st.content.get(p, Content.NONE)
                t = s.terrain.get(p)
                if p in houses:
                    row.append("abcdefghij"[houses[p].group])
                elif c == Content.DESTINATION:
                    row.append("ABCDEFGHIJ"[groups[p]] if p in groups else "?")
                elif c == Content.CARPARK:
                    row.append("P")
                elif st.has_road(p):
                    row.append(road_glyph(st.roads[p]))
                elif t and t.water:
                    row.append("~")
                elif t and t.mountain:
                    row.append("^")
                elif c == Content.TREE:
                    row.append("t")
                else:
                    row.append("." if p in st.playable else " ")
            print("".join(row).rstrip())


if __name__ == "__main__":
    main()
