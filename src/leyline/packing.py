"""Keep a large repository's parse output small while names are resolved.

A file's calls and field uses (and the variable declarations the parse read for the generic resolver) are most of
what an adapter returns: on a repository of millions of lines they were gigabytes as objects, and every one of them
was held from the parse to the end of resolving. They are only read a file at a time, in a few passes. So the parse
worker packs them, pickled and compressed, beside the rest of the file's result, and on a large repository they stay
packed: the indexer unpacks one file's calls when a pass reaches that file, and lets them go when it moves on.

A small repository unpacks everything at once, as before: its output is small and the passes then pay nothing.
LEYLINE_PACKED=1 packs every repository and LEYLINE_PACKED=0 none (the tests use both).
"""

from __future__ import annotations

import os
import pickle
import zlib

from .model import CallSite

PACKED_MIN_FILES = 2000   # files to parse in a repository before its calls are kept packed
_P = pickle.HIGHEST_PROTOCOL


def packs(files: int) -> bool:
    env = os.environ.get("LEYLINE_PACKED", "")
    if env in ("0", "1"):
        return env == "1"
    return files >= PACKED_MIN_FILES


def pack(res, decls) -> bytes:
    """One file's parse output as the parse worker hands it back (and the parse cache keeps it): the result without
    its calls and field uses, then those two packed together (a call can be the receiver of a field use, and the
    two must stay one object), then the declarations packed."""
    calls, uses = res.calls, res.field_uses
    res.calls = res.field_uses = None
    try:
        return pickle.dumps((res, pack_heavy(calls, uses), None if decls is None else zlib.compress(pickle.dumps(decls, _P), 1)), _P)
    finally:
        res.calls, res.field_uses = calls, uses


def unpack(blob: bytes) -> tuple:
    """(result without calls and field uses, the packed calls and field uses, the packed declarations or None)."""
    return pickle.loads(blob)


def pack_heavy(calls, uses) -> bytes:
    return zlib.compress(pickle.dumps((calls, uses), _P), 1)


def unpack_heavy(packed: bytes) -> tuple:
    return pickle.loads(zlib.decompress(packed))


def unpack_decls(packed):
    return None if packed is None else pickle.loads(zlib.decompress(packed))


def call_keys(res, index: int) -> dict:
    """id of every call site in a file's unpacked calls and field uses -> a key that names the same call each time
    the file is unpacked: its place in a walk of the calls (receivers and argument calls first), counted down from
    a range of negative numbers kept for the file. Results remembered about a call (Indexer._chain_memo) are kept
    under this key, so they outlive one unpacking as they outlived nothing before; a negative key is never an id."""
    out: dict = {}
    base = -1 - (index << 24)
    stack: list = []
    for c in reversed(res.calls):
        stack.append(c)
    roots = [u.chain for u in res.field_uses if u.chain is not None]
    n = 0
    for group in (None, roots):
        if group is not None:
            stack.extend(reversed(group))
        while stack:
            c = stack.pop()
            if id(c) in out:
                continue
            out[id(c)] = base - n
            n += 1
            more = [a for a in c.args if type(a) is CallSite]
            if c.chain is not None:
                more.insert(0, c.chain)
            stack.extend(reversed(more))
    return out
