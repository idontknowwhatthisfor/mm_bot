# The bridge

## Injection and connection

`bridge/mmbridge.m` builds to `bridge/build/libmmbridge.dylib`. The build output is gitignored;
`launch.sh` runs `make`, which rebuilds only when `mmbridge.m` is newer. A constructor function
starts a background thread when the library loads. That thread:

- reads `MMBRIDGE_TOKEN` and `MMBRIDGE_PORT` from the environment (without a token the bridge stays
  off);
- connects to `127.0.0.1:<port>` every 250 ms until something listens, so the bot can start before
  or after the game;
- requires the token as the first line, then serves requests until the connection closes.

Each request is decoded on the bridge thread and run on the Unity main thread between frames, so
game state is never read mid-update. Before IL2CPP is ready every request returns
`{"error": "il2cpp not ready"}`. `Session.__init__` retries for up to two minutes while the app
boots.

## Values on the wire

| JSON | Meaning |
|---|---|
| numbers, bools, strings, null | primitives, enums (as ints), `System.String`, `Fix64` (as a double) |
| `{"$t": type, field: …}` | a struct, expanded up to `depth` levels |
| `{"$h": n, "$t": type, "$p": addr}` | a managed object, referenced by handle `n` until released |
| `{"$class": "Ns.Name"}` | (root) the static side of a class |
| `{"$type": "Ns.Name"}` | (argument) the `System.Type` for a class |
| `{"$box": "Ns.Name", "v": x}` | (argument) a boxed value type, for `object` parameters |
| `{"$call": {root, path, method, args, sig, then}}` | (argument) the result of a nested call, evaluated in the game |

**Handles** pin objects against garbage collection. `release(keep)` frees every handle numbered
`keep` or above. `Session` records the handle count after attaching to a game and releases back to
it on every read, so only the game's service handles stay pinned.

## Paths

A path walks from a root object. In Python it's a dotted string or a list of steps:

| Step | Meaning |
|---|---|
| `name` | field or property |
| `@Method` | call a zero-argument method |
| `#i` | index into a list or array (`#-1` = last) |
| `*` | map the rest of the path over every element of a collection or dictionary |
| `?Type` | continue only for objects of that class, else null |
| `{key: subpath, …}` | project several values at once (one round trip) |

Example, from `Session.read`, which fetches every tile in one request:

```python
get_req(h["tilemap"], ["_tiles", "*", {
    "x": "key.m_X", "y": "key.m_Y",
    "r": "value._tile._twoLaneRoadState.*",
    "h": ["value._tile.ContentModel", "?HouseModel", {"g": "GroupIndex", ...}],
}])
```

Arrays can't be written element by element with `set`. Use the array's own `SetValue` instead:
`b.call(array_handle, "SetValue", box("System.Int32", 3), 4)`.

## Operations

| op | Python | Purpose |
|---|---|---|
| `get` | `Bridge.get(root, path, depth, handles)` | Read a value. `handles=False` returns objects as `{"$t","$p"}` without pinning |
| `set` | `Bridge.set(root, path, value)` | Write a field or property |
| `call` | `Bridge.call(root, method, *args, path, sig, then)` | Call a method; `sig` picks an overload by parameter types; `then` walks the result |
| `call_many` | `Bridge.call_many(root, method, args_list)` | One method, many argument lists; failures come back as `{"$err": …}` |
| `batch` | `Bridge.batch([get_req(...), call_req(...)])` | Several requests in one main-thread hop; a failure becomes a `BridgeError` in its slot |
| `new` | `Bridge.new(class, *args, sig)` | Construct an object |
| `find_objects` | `Bridge.find_objects(class)` | Live scene objects of a type |
| `find_all` | `Bridge.find_all(class)` | Every loaded object of a type, assets included (e.g. `MapLibrary`) |
| `describe` | `Bridge.describe(root, path)` | Class layout of an object (fields, methods) |
| `class` | `Bridge.class_info(name, inherited)` | Class layout by name |
| `release` | `Bridge.release(keep)` | Free handles |
| `ping` | `Bridge.ping()` | Liveness and handle count |

`nested_call`, `typeof`, `box` and `cls` (in `mmbridge.py`) build the special argument forms.

## Finding things in the game

`decompiled/DiffableCs` holds Cpp2IL output: declarations, field offsets and method RVAs, with no
method bodies. `tools/decl.py <Class>` prints a class from it without the attribute noise. To see
what a method actually does, disassemble the arm64 slice of `Frameworks/GameAssembly.dylib` at its
RVA. The game code lives in the `il2cpp` section; `objdump --start-address/--stop-address` works.
Tracing `bl` targets back to RVAs in the decompile names the callees. That is how the
bridge-placement and roundabout rules were worked out.
