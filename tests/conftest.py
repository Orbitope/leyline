"""Connections a test opens on a store are closed when the test ends.

Most tests open a store to read what an index wrote and leave the connection for the garbage collector, which
closes it with a ResourceWarning. Here every connection opened from a test file (store.connect, sqlite3.connect or
diff._open called in tests/) is closed at the end of the test. A connection Leyline itself opens is not: one it leaves open
still warns, and `pytest -W error::ResourceWarning` fails on it."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

HERE = str(Path(__file__).resolve().parent)


@pytest.fixture(autouse=True)
def _close_test_connections(monkeypatch):
    from leyline import diff, store
    opened: list[sqlite3.Connection] = []

    def tracked(real):
        def connect(*a, **k):
            con = real(*a, **k)
            if sys._getframe(1).f_code.co_filename.startswith(HERE):
                opened.append(con)
            return con
        return connect
    monkeypatch.setattr(store, "connect", tracked(store.connect))
    monkeypatch.setattr(sqlite3, "connect", tracked(sqlite3.connect))
    monkeypatch.setattr(diff, "_open", tracked(diff._open))   # a snapshot, opened read-only
    yield
    for con in opened:
        con.close()
