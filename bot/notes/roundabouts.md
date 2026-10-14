# How a roundabout changes the roads

Found by reading the game's own `Motorways.Roundabout` helpers live and by placing a roundabout on a
`+` junction by hand in a Tokyo game (2026-10-14). Directions follow `game.py`: +y is north.

## Shape

The game's static helpers describe it:

- `Roundabout.GetCoordinatesOffsets()` gives the tiles **in** the roundabout: the 4 orthogonal
  neighbours of the centre, `(0,-1) (1,0) (0,1) (-1,0)`. The centre `(0,0)` is not one of them.
- `GetNeighborCoordinatesOffsets()` lists `(0,0)` with **every** node direction invalid, so the
  centre can never hold a road again.
- Each ring tile has a fixed one-way roundabout connection (`_activeRoundaboutInput/Output`, also
  returned by `GetConnectionForCoordinatesOffset`). It isn't stored in `_twoLaneRoadState`. The
  input is the neighbour the cars come from and the output is the neighbour they go to:

| ring tile | input (from) | output (to) |
|---|---|---|
| north `(0,1)` | SE = east tile | SW = west tile |
| west `(-1,0)` | NE = north tile | SE = south tile |
| south `(0,-1)` | NW = west tile | NE = east tile |
| east `(1,0)` | SW = south tile | NW = north tile |

  So traffic circles one way, N → W → S → E → N, along the 4 diagonals between the ring tiles.

So a roundabout is a **diamond ring of 4 tiles around an empty centre**, not a 1-tile junction.

## What placing one does (`+` junction, centre C)

Before: C had `N,E,S,W`; each arm tile had its straight road in and out.

After `AddRoundabout(C)`:

```
          (0,+2) S8
          (0,+1) N8  ring SE->SW
(-1,0) W8 ring NE->SE    C: no roads, _isCenterOfRoundabout    (1,0) E8 ring SW->NW
          (0,-1) S8  ring NW->NE
          (0,-2) N8
```

- The centre's two-lane roads are all removed.
- Each ring tile keeps its outward road (its arm), loses its link toward the centre, and gets the
  one-way ring connection instead.
- The 4 ring-to-ring diagonals do **not** appear in `_twoLaneRoadState`. Read the usual way, the 4
  arms look unconnected to each other, and since the arms point at nothing on the centre side, the
  bot's `links()`/`Network` sees 4 separate stubs.
- Corner tiles `(±1,±1)` aren't part of the roundabout and aren't changed.

## Joining roads to it

Each of these was validated with `TileEditor.AddRoad`; a) and b) were also scheduled:

- **Into the centre from a ring tile:** returns `EditAlreadyExists`. Nothing happens.
- **Into the centre from a corner tile (diagonally):** returns `Success`. Once scheduled, the corner
  tile gets a half-road pointing at the centre (`SW8` on `(1,1)`), and the centre stays empty. It is
  never joined. This is the stub the bot drew, saw as a dead end, cleared and drew again.
- **A new arm onto a ring tile from outside:** e.g. `(1,2)` → SW → north ring tile `(0,1)` returns
  `Success`, and both sides now point at each other (`(1,2) SW8`, `(0,1) N8,NE8`). A properly
  joined new exit.
- `Roundabout.GetValidExitsForConnection` gives each ring tile's allowed directions: the north tile
  allows `N, NE, E, W, NW`, never `S` (the centre) or `SE/SW` (its own ring links). The other three
  are the same rule rotated.

## Rules for the bot

1. **Reading the map.** For each centre C (`_isCenterOfRoundabout`), with ring tiles N=C+(0,1),
   E=C+(1,0), S=C+(0,-1), W=C+(-1,0): link N–W, W–S, S–E and E–N as roads (set both tiles' diagonal
   directions toward each other). Traffic is one-way N→W→S→E→N; for connectivity it is enough to treat
   the ring as one junction. **Don't** link the centre to anything; it holds no road.
2. **Planning.** The centre is impassable: never step onto it or plan a road ending on it. A new road
   may join a ring tile only in that tile's valid exit directions: away from the centre, and not along
   the ring diagonals. Corner tiles are ordinary tiles, but never draw from them toward the centre.
3. **Cleanup.** Never clear a ring tile. Their arms are what join the roundabout to the network, and
   with rule 1 they're no longer dead ends anyway.
4. **Placing.** `AddRoundabout(C)` needs C to be a junction; on a `+` the 4 arms become the 4 ring
   tiles. The X (diagonal) and T junction cases weren't tested: there was no free 5x5 block in the
   early Tokyo city.

## How this explains the loops seen

- `join_roundabouts` (commit c40a853) linked the **centre** to any neighbour pointing at it. That's
  wrong twice over: the centre holds no road, and only the stubs the bot itself drew from corner tiles
  point at it. A stub like that made the network look connected when the game had no link.
- Without the ring links, every arm looked like a dead end. The cleanup tore real arms out, and the
  destinations behind them were cut off: the 266 game, and on-1 in the comparison.

## How many roads can join one

Tested in a fresh Tokyo game: I built a roundabout on a `+` at (-1, 1), then drew a road from each
ring tile in every direction other than the centre and its own two ring links, and checked both
sides afterwards. All **20** joined: **5 per ring tile**. On the north tile those are N, NE, E, W
and NW; the other three tiles follow the same rule rotated. Each tile's original arm reported
`EditAlreadyExists`. All 4 ring tiles still had their ring connection afterwards. The only limit is
the rule above: no road toward the centre, and nothing along the tile's own ring diagonals.
Corner tiles can be joined from both ring tiles next to them.

## Roads aimed at the centre

Tested in a fresh Tokyo game. I placed a roundabout at (3, 3), then drew roads toward its centre the
way a drag does, one segment after another starting from the far end:

- **Diagonal (NE, SW and NW; SE was blocked by terrain):** both segments return `Success`. The corner
  tile ends up with a road pointing at the centre (e.g. `(+1,+1) NE,SW`), but the centre stays
  empty, so the road **dangles and never connects**.
- **Straight (N, E, S, W):** outer tile → ring tile returns `Success` and joins the ring tile. The
  last step, ring tile → centre, returns `EditAlreadyExists` and does nothing. The road
  **connects**, because it ends on a ring tile.

So a road joins a roundabout only where it meets a ring tile. A diagonal approach has to end on a
ring tile (e.g. into the north tile from its NE or NW). It must not run on through the corner tile
toward the centre.
