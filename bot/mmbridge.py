"""Client for the mmbridge dylib injected into Mini Motorways (see bridge/mmbridge.m).

The game connects out to us, so a Bridge listens on 127.0.0.1 and waits for it. Paths are lists of
steps (or dotted strings): field/property names, "@Method" for zero-arg calls, "#i" to index, "*" to
map over a collection, "?Type" to continue only for objects of that class (else null), or a dict of
named sub-paths to project several values at once.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket

HERE = pathlib.Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".mmbridge_token"
DEFAULT_PORT = int(os.environ.get("MMBRIDGE_PORT", "47811"))


class BridgeError(RuntimeError):
    pass


def cls(name: str) -> dict:
    """Root for static fields/methods of a class."""
    return {"$class": name}


def typeof(name: str) -> dict:
    """Argument that becomes the System.Type for a class."""
    return {"$type": name}


def box(type_name: str, value) -> dict:
    """Argument that becomes a boxed value type (for `object` parameters)."""
    return {"$box": type_name, "v": value}


def nested_call(root, method: str, *args, path=None, sig=None, then=None) -> dict:
    """Argument evaluated in the game: the object returned by calling `method` on root+path."""
    spec = {"root": root, "path": steps(path), "method": method, "args": list(args)}
    if sig is not None:
        spec["sig"] = sig
    if then is not None:
        spec["then"] = steps(then)
    return {"$call": spec}


def steps(path) -> list:
    if path is None:
        return []
    if isinstance(path, str):
        return [s for s in path.split(".") if s]
    if isinstance(path, dict):
        return [_projection(path)]
    out = []
    for part in path:
        out.extend(steps(part) if isinstance(part, str) else [_projection(part)])
    return out


def _projection(spec: dict) -> dict:
    return {key: steps(sub) for key, sub in spec.items()}


class Bridge:
    def __init__(self, port: int = DEFAULT_PORT, timeout: float = 30.0):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        server.settimeout(timeout)
        try:
            self._sock, _ = server.accept()
        except socket.timeout:
            raise BridgeError("the game did not connect; is it running via launch.sh?") from None
        finally:
            server.close()
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rwb")
        self._next_id = 0
        # Read only now: launch.sh writes a fresh token for each launch, possibly after we started.
        self._send({"token": TOKEN_FILE.read_text().strip()})
        self.hello = self._recv()

    def close(self):
        self._file.close()
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, obj):
        self._file.write(json.dumps(obj).encode() + b"\n")
        self._file.flush()

    def _recv(self):
        line = self._file.readline()
        if not line:
            raise BridgeError("the game closed the connection")
        return json.loads(line)

    def request(self, op: str, **fields):
        self._next_id += 1
        self._send({"op": op, "id": self._next_id, **fields})
        reply = self._recv()
        if "error" in reply:
            raise BridgeError(reply["error"])
        return reply["ok"]

    def batch(self, requests: list[dict]) -> list:
        """Run several requests in one main-thread hop. Failed entries come back as BridgeError."""
        out = []
        for r in self.request("batch", requests=requests):
            out.append(BridgeError(r["error"]) if "error" in r else r["ok"])
        return out

    # Convenience wrappers ------------------------------------------------------------------

    def get(self, root, path=None, depth: int = 4, handles: bool = True):
        """Read a value. With handles=False, objects come back as {"$t", "$p"} without a handle."""
        return self.request("get", root=root, path=steps(path), depth=depth, handles=handles)

    def set(self, root, path, value):
        return self.request("set", root=root, path=steps(path), value=value)

    def call(self, root, method: str, *args, path=None, sig=None, then=None, depth: int = 4):
        fields = {"root": root, "path": steps(path), "method": method, "args": list(args), "depth": depth}
        if sig is not None:
            fields["sig"] = sig
        if then is not None:
            fields["then"] = steps(then)
        return self.request("call", **fields)

    def call_many(self, root, method: str, args_list, path=None, sig=None, then=None, depth: int = 4):
        """Call one method with each argument list in turn; failures come back as {"$err": ...}."""
        fields = {"root": root, "path": steps(path), "method": method,
                  "args_list": [list(a) for a in args_list], "depth": depth}
        if sig is not None:
            fields["sig"] = sig
        if then is not None:
            fields["then"] = steps(then)
        return self.request("call_many", **fields)

    def new(self, class_name: str, *args, sig=None):
        """Construct an object with the constructor taking len(args) arguments."""
        fields = {"class": class_name, "args": list(args)}
        if sig is not None:
            fields["sig"] = sig
        return self.request("new", **fields)

    def find_all(self, class_name: str):
        """Resources.FindObjectsOfTypeAll: also finds assets (ScriptableObjects) and inactive objects."""
        return self.request("find_all", **{"class": class_name})

    def find_objects(self, class_name: str):
        return self.request("find_objects", **{"class": class_name})

    def describe(self, root, path=None):
        return self.request("describe", root=root, path=steps(path))

    def class_info(self, name: str, inherited: bool = False):
        return self.request("class", name=name, inherited=inherited)

    def release(self, keep: int = 0):
        """Free handles numbered >= keep; returns how many remain."""
        return self.request("release", keep=keep)

    def ping(self):
        return self.request("ping")


def get_req(root, path=None, depth: int = 4) -> dict:
    """A "get" request for Bridge.batch."""
    return {"op": "get", "root": root, "path": steps(path), "depth": depth}


def call_req(root, method: str, *args, path=None, sig=None, then=None, depth: int = 4) -> dict:
    """A "call" request for Bridge.batch."""
    req = {"op": "call", "root": root, "path": steps(path), "method": method, "args": list(args), "depth": depth}
    if sig is not None:
        req["sig"] = sig
    if then is not None:
        req["then"] = steps(then)
    return req
