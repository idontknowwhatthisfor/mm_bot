"""A simple greedy bot, built step by step in GUIDE.md:

1. pick upgrades (bridges while short of them, else the most road tiles);
2. feed every destination: give it a road to a house of its colour;
3. connect every house to its colour's network with the fewest new road tiles (breadth-first
   search, straight bridges and tunnels included);
4. put roundabouts where cars are actually stuck.
"""

from collections import deque

from mmsim import CARDINALS, CarState, Content, Result, neighbor

RETRY_AFTER = 10.0  # game seconds before trying something that failed again
MAX_CROSSING = 4  # longest straight bridge or tunnel to consider


class Bot:
    def __init__(self):
        self.log = []  # messages; the viewer shows them
        self.retry_at = {}  # what -> game time when it may be tried again

    def step(self, game):
        if game.upgrade_choices:
            self.pick_upgrade(game)
            return
        if game.paused:
            return
        self._networks = {}  # worked out once per step, per colour
        self.feed_destinations(game)
        self.connect_houses(game)
        self.place_roundabouts(game)

    # 1. Upgrades -------------------------------------------------------------------------------

    def pick_upgrade(self, game):
        has_water = any(game.terrain((x, y)).water for x in range(game.width) for y in range(game.height))

        def worth(option):
            value = option["extra_roads"]
            if option["type"] == "roads":
                value += option["amount"]
            elif option["type"] == "bridge":
                value += 25 if has_water and game.stock["bridges"] < 2 else 5
            elif option["type"] == "roundabout":
                value += 6
            elif option["type"] == "traffic_light":
                value += 3 * option["amount"]
            return value  # tunnels and motorways: only their extra roads

        options = game.upgrade_choices
        best = max(range(len(options)), key=lambda i: worth(options[i]))
        self.log.append(f"week {game.week}: took {options[best]['type']}")
        game.choose_upgrade(best)

    # 2 and 3. Roads ---------------------------------------------------------------------------

    def networks(self, game, colour):
        """Road tiles joined to a destination of this colour, and road tiles joined to a house of it."""
        if colour in self._networks:
            return self._networks[colour]
        to_dest, to_house = set(), set()
        for d in game.destinations:
            if d.colour == colour and game.has_road(d.entrance):
                to_dest |= game.reachable(d.entrance)
        for h in game.houses:
            if h.colour == colour and game.has_road(h.access) and h.access not in to_house:
                to_house |= game.reachable(h.access)
        self._networks[colour] = (to_dest, to_house)
        return to_dest, to_house

    def feed_destinations(self, game):
        """A destination with no house of its colour on its road gets no cars: give it a road to the
        nearest house (or house network) of its colour."""
        for d in game.destinations:
            to_dest, to_house = self.networks(game, d.colour)
            houses = [h for h in game.houses if h.colour == d.colour]
            if not houses or any(h.pos in to_dest for h in houses):
                continue
            goals = {p for p in to_house if game.content(p) == Content.NONE} | {h.access for h in houses}
            self.build(game, ("destination", d.id), d.entrance, goals)

    def connect_houses(self, game):
        for h in game.houses:
            to_dest, _ = self.networks(game, h.colour)
            if h.pos in to_dest:
                continue  # its cars can reach a destination of its colour
            goals = {p for p in to_dest if game.content(p) == Content.NONE}
            goals |= {d.entrance for d in game.destinations if d.colour == h.colour}
            self.build(game, ("house", h.pos), h.access, goals)

    def build(self, game, key, start, goals):
        if self.retry_at.get(key, -1) > game.time or not goals:
            return
        path = self.find_path(game, start, goals)
        if path is None:
            self.retry_at[key] = game.time + RETRY_AFTER
            return
        new_tiles = sum(1 for p in path[1:] if not game.has_road(p) and game.terrain(p).driveable)
        if new_tiles > game.stock["roads"]:
            return  # wait for more road tiles
        if len(path) == 1:
            return
        results = game.draw_path(path)
        self._networks = {}
        if results and results[-1] not in (Result.OK, Result.EXISTS):
            self.log.append(f"{key[0]} at {start}: {results[-1].name}")
            self.retry_at[key] = game.time + RETRY_AFTER
        else:
            self.log.append(f"{key[0]} at {start}: road of {len(path)} tiles")

    def find_path(self, game, start, goals):
        """Fewest-steps path from start to any goal tile over free land and existing road, moving
        N/E/S/W. A straight run of water (mountain) can be crossed if a bridge (tunnel) is in stock."""
        if not (game.free(start) or game.has_road(start)):
            return None
        bridges, tunnels = game.stock["bridges"], game.stock["tunnels"]
        previous = {start: None}
        queue = deque([start])
        while queue:
            p = queue.popleft()
            if p in goals:
                path = [p]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                return path[::-1]
            for d in CARDINALS:
                q = neighbor(p, d)
                t = game.terrain(q)
                if t is None or q in previous:
                    continue
                if game.free(q) or q in goals:
                    previous[q] = p
                    queue.append(q)
                    continue
                if t.driveable or not game.in_city(q):
                    continue
                # Water or mountain: run straight across to land.
                if (t.water and bridges < 1) or (t.mountain and tunnels < 1):
                    continue
                span = [q]
                while len(span) <= MAX_CROSSING:
                    nxt = neighbor(span[-1], d)
                    nt = game.terrain(nxt)
                    if nt is None or not game.in_city(nxt):
                        span = None
                        break
                    if nt.driveable:
                        if (game.free(nxt) or nxt in goals) and nxt not in previous:
                            prev = p
                            for s in span:
                                previous[s] = prev
                                prev = s
                            previous[nxt] = prev
                            queue.append(nxt)
                        span = None
                        break
                    span.append(nxt)
        return None

    # 4. Traffic --------------------------------------------------------------------------------

    def place_roundabouts(self, game):
        """Cars stopped at a full junction: a roundabout lets twice as many through."""
        if game.stock["roundabouts"] < 1:
            return
        stuck = {}
        for car in game.cars:
            if car.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) and car.speed == 0 and car.path:
                stuck[car.path[0]] = stuck.get(car.path[0], 0) + 1  # the tile it can't get into
        for p, n in sorted(stuck.items(), key=lambda kv: -kv[1]):
            if n >= 2 and sum(game.joined(p, d) for d in range(8)) >= 3:
                if game.add_roundabout(p) == Result.OK:
                    self.log.append(f"roundabout at {p} ({n} cars waiting)")
                    return
