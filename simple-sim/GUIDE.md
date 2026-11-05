# Building a Mini Motorways bot from scratch

This guide teaches you how to write a bot for **mmsim**, a small simulator of Mini Motorways. You
start with an empty file and end up with `examples/greedy_bot.py`. Then you'll measure it and make
it better.

You need Python 3.10 or newer and nothing else. Every command below runs from the `simple-sim/`
folder.

## Contents

1. [The game in two minutes](#1-the-game-in-two-minutes)
2. [Coordinates and directions](#2-coordinates-and-directions)
3. [How a bot runs](#3-how-a-bot-runs)
4. [The API](#4-the-api)
5. [Building the bot, step by step](#5-building-the-bot-step-by-step)
6. [Watching, measuring and testing](#6-watching-measuring-and-testing)
7. [Making it better](#7-making-it-better)
8. [Rules reference](#8-rules-reference)

---

## 1. The game in two minutes

The map is a grid of tiles with rivers (and sometimes a mountain) on it. Things appear on their
own:

- **Destinations:** a 2×2 coloured building with a 2-tile **car park** below it. The car park
  has one **entrance**, a road tile to its left or right. New destinations keep coming, often in a
  new colour (up to 6).
- **Houses:** single coloured tiles, each with **2 cars** and a **driveway** pointing at one of
  its four neighbours, the **access tile**. Houses mostly appear near a destination of their
  colour, but a quarter of them appear anywhere.
- **Pins:** demand, appearing on destinations faster and faster as the weeks go by.

Your job is to **draw roads**. When a destination has a pin, an idle car from a house **of the same
colour that's connected to it by road** drives there along the shortest road, parks, and scores
**1 point**. Then it drives home.

- **Losing:** if a destination has more than **6 pins** (unassigned plus on their way), its
  **overcrowd timer** runs. When the timer reaches **50 game seconds**, the game is over. It runs
  back down when the destination is under the limit.
- **Road tiles:** each new tile of road costs one from your stock. You start with 30.
- **Weekly upgrades:** every week (140 game seconds) the game pauses and offers 2 packages, such as
  "30 road tiles" or "a bridge + 20 road tiles". The game waits until you pick one.
- **Water:** a bridge is needed to cross a river, a tunnel to cross a mountain.
- **Traffic:** a tile only holds a few cars, so busy roads and junctions queue. A roundabout or
  traffic light on a junction lets more cars through.
- **City limits:** the area where roads may go starts at 16×10 tiles in the middle of a 36×24 map,
  and grows by 2 tiles on each side every week.

A bot that does nothing loses on day 8 with 0 points. The example greedy bot lasts about 4–6
weeks.

## 2. Coordinates and directions

A position is a tuple `(x, y)`. **x grows to the east and y grows to the north**: `(3, 5)` is one
tile north of `(3, 4)`.

Directions are numbers 0–7, clockwise from north:

```
        NW(7)  N(0)  NE(1)
        W(6)   tile  E(2)
        SW(5)  S(4)  SE(3)
```

```python
from mmsim import N, NE, E, SE, S, SW, W, NW, CARDINALS, DIRS, neighbor, opposite, direction_between

neighbor((3, 4), N)                    # (3, 5)
neighbor((3, 4), SE)                   # (4, 3)
opposite(E)                            # W
direction_between((3, 4), (4, 4))      # E
CARDINALS                              # (N, E, S, W)
DIRS[NE]                               # (1, 1), the step for each direction
```

**How roads are stored.** Every tile remembers, for each of its 8 directions, whether it has a
half-road pointing that way. Two neighbouring tiles are **joined** only when both point at each
other.

When you draw a segment from `a` to its neighbour `b`, both halves are set. A house has a stub
pointing at its access tile, and a car park has a stub pointing at its entrance. These join up
automatically as soon as that tile has road.

## 3. How a bot runs

A bot is a Python file with a class called `Bot` that has a `step` method:

```python
class Bot:
    def step(self, game):
        ...  # look at `game`, draw roads, pick upgrades
```

The runner makes a new `Bot` for every game. It calls `step(game)`, then lets the game run for 0.4
game seconds, and repeats until the game is over:

```
game = Game(seed, map)
bot = Bot()
while not game.over:
    bot.step(game)
    game.advance(0.4)
```

- Your bot acts **between** moments of game time. Everything you draw during `step` is there before
  the next car moves.
- While an upgrade choice is pending, `game.paused` is true and time doesn't move until you choose.
- `step` should be quick: a few milliseconds is fine, half a second makes runs slow.

Run a bot over several games:

```bash
python3 -m mmsim.run examples/greedy_bot.py --games 10
python3 -m mmsim.run my_bot.py --games 20 --map two-rivers --seed 100
```

The same seed is always the same game for the same bot, so you can compare two bots seed by seed.

## 4. The API

Everything comes from `game`, a `mmsim.Game`.

### Clock and score

| | |
|---|---|
| `game.time` | game seconds since the start (a day is 20 s, a week 140 s) |
| `game.week`, `game.day` | |
| `game.score` | pins served |
| `game.over` | the game has ended |
| `game.paused` | waiting for an upgrade choice (or over) |

### The map

| | |
|---|---|
| `game.width`, `game.height` | map size (36×24) |
| `game.terrain(p)` | `Terrain(driveable, water, mountain, buildable)`, or `None` off the map |
| `game.in_city(p)` / `game.city()` | inside the city limits / the set of all such tiles |
| `game.content(p)` | `Content.NONE`, `HOUSE`, `DESTINATION` or `CARPARK` |
| `game.free(p)` | plain land in the city with no building: a road may go here |
| `game.has_road(p)` | the tile has any road |
| `game.roads(p)` | directions in which the tile has a half-road |
| `game.joined(p, d)` | a road runs from `p` to its neighbour in direction `d` |
| `game.reachable(p)` | every tile joined to `p` by road |
| `game.route(a, b)` | the road a car would drive from `a` to `b` (tiles after `a`), or `None` |
| `game.capacity(p)` | cars that fit on that road tile at once |

### Things on the map

`game.houses` is a list of `HouseView`:

| field | |
|---|---|
| `pos` | the house tile |
| `colour` | 0–5 |
| `driveway` | direction from the house to its access tile |
| `access` | **the tile a road must reach** to connect the house |
| `idle_cars`, `cars` | cars at home, cars owned (2) |

`game.destinations` is a list of `DestinationView`:

| field | |
|---|---|
| `id`, `colour` | |
| `tiles` | the 4 building tiles |
| `car_park` | its 2 tiles |
| `entrance` | **the tile a road must reach** to connect the destination |
| `pins` | `unassigned` + `en_route` |
| `unassigned` | pins no car has been sent to: the colour has no idle connected car |
| `en_route` | pins with a car on the way |
| `served` | pins served so far |
| `timer`, `max_timer` | the overcrowd timer and its limit (50) |
| `pin_limit` | 6 |

`game.cars` is a list of `CarView`: `id`, `colour`, `home`, `state` (`CarState.WAITING`,
`DRIVING_TO_DESTINATION`, `PARKING` or `DRIVING_HOME`), `destination` (an id), `tile`, `speed` (0
when stopped), `obstacle` (`Obstacle.LEADING_VEHICLE` or `BLOCKING_INTERSECTION` when queueing),
and `path` (tiles still to drive).

`game.stock` is a dict:

```python
{"roads": 30, "bridges": 1, "tunnels": 0, "roundabouts": 0, "traffic_lights": 0,
 "bridges_coming_back": 0, "tunnels_coming_back": 0}
```

### Upgrades

```python
game.upgrade_choices   # [] normally; while paused, two packages such as
                       # [{"type": "roads", "amount": 30, "extra_roads": 0},
                       #  {"type": "bridge", "amount": 1, "extra_roads": 20}]
game.choose_upgrade(1) # take the second one; the game carries on
```

Package types: `roads`, `bridge`, `tunnel`, `roundabout`, `traffic_light`, `motorway`. Motorways
can't be built in the simulator, so they're only worth their extra roads.

### Actions

Every action returns a `Result`. Only `Result.OK` changed anything.

| | |
|---|---|
| `game.draw_road(p, d)` | one segment from `p` to its neighbour in direction `d` |
| `game.draw_path([p0, p1, p2, …])` | segments along neighbouring tiles; stops at the first failure (`EXISTS` counts as fine) |
| `game.clear(p)` | remove the roads on a tile and get its road tile back. On a bridge, the whole bridge goes and comes back to stock 3 s later |
| `game.add_roundabout(p)`, `game.add_traffic_light(p)` | on a junction (3+ joined roads); it then holds 4 cars instead of 2 |

The rules `draw_road` checks:

- Both tiles must be inside the city limits, and not on a building, otherwise `INVALID`.
- A new tile of road costs 1 road from stock (`NO_CONCRETE` if you're out). **A house's access
  tile is free.**
- **Drawing out of a house turns it.** `draw_road(house.pos, N)` points its driveway north. Only
  N/E/S/W, and only onto free land.
- A car park joins only from its entrance tile (`NO_CARPARK`). You don't need to draw into it
  yourself: the entrance joins up as soon as it has road.
- **Bridges and tunnels:** stepping from land onto water takes a bridge from stock (`NO_BRIDGE` if
  there's none). A deck must run straight, N/E/S/W only, and the road must continue straight to
  land. A mountain needs a tunnel in the same way.
- Diagonal roads are allowed on land, but two diagonals may not cross at the same corner
  (`CROSSING`).
- Drawing a segment that's already there gives `EXISTS`.

---

## 5. Building the bot, step by step

Create `my_bot.py` next to the `examples/` folder.

### Step 0: survive the first week

The game pauses at the end of each week until you pick an upgrade. A bot that never picks stops the
clock forever. So the smallest working bot is:

```python
class Bot:
    def step(self, game):
        if game.upgrade_choices:
            game.choose_upgrade(0)
```

```bash
python3 -m mmsim.run my_bot.py --games 3
```

It scores 0 and loses on day 8, when the first destination has had too many pins for too long.
That's the baseline (`examples/do_nothing.py`).

### Step 1: pick upgrades on purpose

Give each package a value in "road tiles" and take the best:

```python
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
        return value

    options = game.upgrade_choices
    best = max(range(len(options)), key=lambda i: worth(options[i]))
    game.choose_upgrade(best)
```

**Why bridges are worth so much while you have fewer than 2:** a destination that spawns across a
river from its houses can't be connected without one. That ends the game, while 10 fewer road
tiles rarely does.

The step method now looks like this:

```python
def step(self, game):
    if game.upgrade_choices:
        self.pick_upgrade(game)
        return
    if game.paused:
        return
    ...  # the rest of this guide goes here
```

### Step 2: know what's connected

A house is served when its cars can reach a destination of its colour. Find every road tile joined
to the colour's destinations; the house is connected if its tile is among them (the house joins its
access tile by itself):

```python
def networks(self, game, colour):
    """Road tiles joined to a destination of this colour, and road tiles joined to a house of it."""
    to_dest, to_house = set(), set()
    for d in game.destinations:
        if d.colour == colour and game.has_road(d.entrance):
            to_dest |= game.reachable(d.entrance)
    for h in game.houses:
        if h.colour == colour and game.has_road(h.access) and h.access not in to_house:
            to_house |= game.reachable(h.access)
    return to_dest, to_house
```

`reachable` walks the road network, so working this out for every house on every step gets slow.
`greedy_bot.py` keeps the result in a dict for the rest of the step (`self._networks`), and empties
it whenever it draws a road.

### Step 3: find a path for a new road

You want the fewest new road tiles from a start tile (a house's access tile) to any **goal tile**
(a road tile on the colour's network, or a destination's entrance). Use a breadth-first search over
the grid:

```python
from collections import deque
from mmsim import CARDINALS, neighbor

def find_path(self, game, start, goals):
    if not (game.free(start) or game.has_road(start)):
        return None
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
            if q in previous:
                continue
            if game.free(q) or q in goals:
                previous[q] = p
                queue.append(q)
    return None
```

- `game.free(q)` covers both empty land and land that already has road, since road isn't a
  building. So the search may run along other roads, which costs nothing.
- It only moves N/E/S/W. Diagonals are an improvement for later (section 7).

### Step 4: draw it and check the result

```python
def build(self, game, key, start, goals):
    if self.retry_at.get(key, -1) > game.time or not goals:
        return
    path = self.find_path(game, start, goals)
    if path is None:
        self.retry_at[key] = game.time + 10      # don't search again every step
        return
    new_tiles = sum(1 for p in path[1:] if not game.has_road(p) and game.terrain(p).driveable)
    if new_tiles > game.stock["roads"]:
        return                                   # wait for more road tiles
    if len(path) == 1:
        return
    results = game.draw_path(path)
    if results and results[-1] not in (Result.OK, Result.EXISTS):
        self.retry_at[key] = game.time + 10      # something refused: back off
```

Three habits that matter:

- **Check the stock before you draw.** A road that runs out halfway helps nobody.
- **Read the results.** When something is refused, back off for a while instead of retrying it on
  every step.
- **Remember failures by key**, for example `("house", house.pos)`, so one stuck house doesn't
  cost the search time on every step.

### Step 5: connect houses, and feed destinations first

```python
def connect_houses(self, game):
    for h in game.houses:
        to_dest, _ = self.networks(game, h.colour)
        if h.pos in to_dest:
            continue                                   # already served
        goals = {p for p in to_dest if game.content(p) == Content.NONE}
        goals |= {d.entrance for d in game.destinations if d.colour == h.colour}
        self.build(game, ("house", h.pos), h.access, goals)
```

That alone isn't enough. Say the houses all join the first red destination, and then a second red
destination appears. No red car can reach the second one, so it overflows and you lose. Look for
destinations with no house of their colour on their network, and build them a road toward one,
**before** connecting houses:

```python
def feed_destinations(self, game):
    for d in game.destinations:
        to_dest, to_house = self.networks(game, d.colour)
        houses = [h for h in game.houses if h.colour == d.colour]
        if not houses or any(h.pos in to_dest for h in houses):
            continue
        goals = {p for p in to_house if game.content(p) == Content.NONE} | {h.access for h in houses}
        self.build(game, ("destination", d.id), d.entrance, goals)
```

### Step 6: cross rivers

Without bridges, any colour split by the river can never be connected. Teach the search to jump
straight across water when a bridge is in stock. From a land tile, going N/E/S/W into water, keep
going straight until land, and treat that run as one step:

```python
t = game.terrain(q)
if not t.driveable and game.in_city(q) and ((t.water and bridges) or (t.mountain and tunnels)):
    span = [q]
    while len(span) <= 4:
        nxt = neighbor(span[-1], d)
        nt = game.terrain(nxt)
        if nt is None or not game.in_city(nxt):
            break                                  # runs off the map: no crossing here
        if nt.driveable:
            if game.free(nxt) or nxt in goals:     # landed: link the deck tiles into the path
                ...
            break
        span.append(nxt)
```

See `find_path` in `examples/greedy_bot.py` for the full version. When you draw a path with a
bridge in it, `draw_path` draws the deck segments in order, and the bridge is taken from stock on
the first one.

### Step 7: use real traffic

Cars queue when the tile ahead is full. `game.cars` tells you exactly where:

```python
def place_roundabouts(self, game):
    if game.stock["roundabouts"] < 1:
        return
    stuck = {}
    for car in game.cars:
        if car.state in (CarState.DRIVING_TO_DESTINATION, CarState.DRIVING_HOME) and car.speed == 0 and car.path:
            stuck[car.path[0]] = stuck.get(car.path[0], 0) + 1       # the tile it can't get into
    for p, n in sorted(stuck.items(), key=lambda kv: -kv[1]):
        if n >= 2 and sum(game.joined(p, d) for d in range(8)) >= 3:  # a junction
            if game.add_roundabout(p) == Result.OK:
                return
```

The finished `step`:

```python
def step(self, game):
    if game.upgrade_choices:
        self.pick_upgrade(game)
        return
    if game.paused:
        return
    self._networks = {}
    self.feed_destinations(game)
    self.connect_houses(game)
    self.place_roundabouts(game)
```

That's `examples/greedy_bot.py`:

```bash
python3 -m mmsim.run examples/greedy_bot.py --games 6
```

On seeds 1–6 of the river map it scores 143–514 (mean 262) and loses in weeks 3–6.

---

## 6. Watching, measuring and testing

### Watching

```bash
python3 -m mmsim.view my_bot.py --seed 4
```

The window shows:

- **The map:** roads, houses and destinations in their colours, and each destination's pin count.
  A red ring fills up as its overcrowd timer runs.
- **Cars:** stopped ones are outlined in red.
- **Your bot's log:** if your `Bot` has a list called `self.log`, anything you append to it shows
  in the side panel.

Keys: **space** pauses, **s** steps once, **+** and **-** change the speed, and **n** starts the
next seed. Run it without a bot (`python3 -m mmsim.view`) to watch the world on its own.

### Measuring

One game says little, because layouts are random. Compare bots over the **same seeds**:

```bash
python3 -m mmsim.run examples/greedy_bot.py --games 20 --seed 1 > before.txt
python3 -m mmsim.run my_bot.py              --games 20 --seed 1 > after.txt
```

Look at the mean, and at the worst games: a change that fixes your worst losses is worth more than
one that pushes up a high score. The end of each line says how the game was lost, for example
`lost to colour 2 at (26, 6)`. Watch that seed in the viewer to see why.

### Testing

Rules are easiest to check on a small, hand-placed world. `tests/test_world.py` builds one with
`blank()`, `place_house()` and `place_destination()`. You can write bot tests the same way: build a
world where a house sits across a river, call `bot.step(Game(world=w))`, and assert on what got
drawn. `play()` from `mmsim.run` runs a whole game in a test:

```python
from mmsim.run import load_bot, play
result = play(load_bot("my_bot.py"), seed=1, max_weeks=3)
assert result["score"] > 0
```

Run the tests with:

```bash
python3 -m unittest discover -s tests
```

## 7. Making it better

Ideas, roughly in the order they pay off:

1. **Turn houses.** A house can face any free side (`draw_road(house.pos, d)`). Search from all 4
   sides of the house, and turn it toward the shortest one. Often that's 0 new tiles.
2. **Diagonal roads.** One diagonal tile covers a step in x and in y. Add the 4 diagonal
   directions to the search (not over water), and avoid crossing another diagonal at the same
   corner.
3. **Cost, not steps.** Switch from breadth-first search to Dijkstra or A\*, where a new tile costs
   more than driving on existing road. Then you can trade a slightly longer road for a shorter
   trip.
4. **Short trips.** Cars take the shortest road (`game.route`). A house joined to its colour's
   network far from the destination makes every trip long. Add the remaining drive to the goal's
   cost.
5. **Keep colours apart.** Every car on a road adds to its queues. Roads shared by many colours
   jam first. Count, for each road tile, how many houses drive over it (follow `game.route` from
   each house to each destination of its colour), and make busy tiles cost more.
6. **Bridges are precious.** Never spend the last one on a house. Keep it for a destination.
7. **Clean up.** Dead-end roads waste tiles. A road tile with one joined neighbour that isn't a
   house's access tile or an entrance can be cleared (`game.clear`) to get its tile back.
8. **Take bridges back.** If a destination is cut off by water and no bridge is in stock, clear a
   bridge nobody needs. It comes back to stock 3 seconds later.
9. **More room at junctions.** Place roundabouts and lights where queues form most over time, not
   just at one moment.

The bot in the parent project (`../bot/`) does all of these against the real game, and scores about
4–5 times the greedy bot on this simulator.

## 8. Rules reference

| Rule | Value |
|---|---|
| Map | 36×24 tiles. City limits start at 16×10 in the middle and grow 2 tiles per side each week |
| Maps | `river` (one river), `two-rivers`, `plain`, `mountains` (one river and a mountain) |
| Time | a day is 20 game s, a week 140 s. Weekly upgrade choice: 2 packages; the game pauses until you pick |
| Start | 1 destination, 2 houses; 30 road tiles; 1 bridge if the map has water, 1 tunnel if it has a mountain |
| New destinations | every 0.6–1 week; a new colour 60% of the time (always until there are 3), up to 6 colours |
| New houses | one every 25 s at first, faster each week down to one every 10 s. Each goes to the colour with the most destinations per house; 75% are within 6 tiles of one of its destinations |
| Pins | at each destination, one per day at first; every 20 / (1 + 0.4 × weeks) seconds |
| Losing | more than 6 pins at a destination runs its timer; 50 s of it ends the game. Under the limit, it runs back down |
| Cars | 2 per house. Idle cars of the colour are sent to unassigned pins, nearest first. 1 tile/s on straights (a diagonal step is 1.41 tiles), 0.6 tiles/s through bends of 90° or more. 1.5 s parking, then 1 point, then home |
| Capacity | 3 cars per road tile, 2 on a junction (3+ joined roads), 4 on a junction with a roundabout or lights. A car facing a full tile waits there, and squeezes in after 5 s |
| Routing | cars take the shortest drive, never through houses or car parks. They re-route when the roads change; a car cut off from its pin gives it back and drives home |
| Road tiles | 1 per new tile of road, none for a house's access tile. Clearing a tile gives it back |
| Bridges and tunnels | one per crossing started from land. Straight decks only. Clearing one removes the whole crossing; it returns to stock 3 s later |
| Upgrade packages | roads ×30; bridge +20 roads; tunnel +20 (maps with a mountain); roundabout +20; lights ×2 +20; motorway +10 |

### Differences from the real game

The simulator is simpler on purpose:

- **Traffic:** tile capacity stands in for real lanes and car following.
- **Motorways:** they can't be built.
- **Roundabouts and lights:** they only add room.
- **Car parks:** they have one entrance, so they can't be driven through.
- **Missing:** trains, boats, trees, and the game's real spawn rules.

Ideas that work here usually work in the real game. The exact numbers don't carry over.
