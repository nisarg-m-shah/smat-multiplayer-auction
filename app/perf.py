"""Lightweight per-request database instrumentation.

Counts every statement / COMMIT executed on the current thread between begin()
and end(), and (optionally) records how long each one took. This is how
`bid_timing` reports `db_stmts` and, when BID_TRACE_SQL=1, a per-statement
breakdown. Overhead is a couple of list appends per statement.
"""
import os, re, threading, time
from sqlalchemy import event

TRACE_SQL = os.getenv("BID_TRACE_SQL", "0") == "1"
_tls = threading.local()


def _label(statement: str) -> str:
    s = " ".join(statement.split())
    low = s.lower()
    if low.startswith("with "):
        return "FUSED " + " ".join(re.findall(r"\b(?:UPDATE|INSERT INTO)\s+(\w+)", s))
    if low.startswith("insert into"):
        return "INSERT " + s.split()[2]
    if low.startswith("update"):
        return "UPDATE " + s.split()[1]
    if low.startswith("delete from"):
        return "DELETE " + s.split()[2]
    if low.startswith("select"):
        depth = 0
        for m in re.finditer(r"[()]|\bFROM\s+(\w+)", s, re.I):   # first top-level FROM
            tok = m.group(0)
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            elif depth == 0:
                return "SELECT " + m.group(1)
        return "SELECT"
    return s.split()[0] if s else "?"


def begin():
    _tls.rows = []
    _tls.on = True
    _tls.t0 = None


def end():
    """Stop tracing on this thread and return [(label, ms), ...]."""
    _tls.on = False
    rows = getattr(_tls, "rows", [])
    _tls.rows = []
    return rows


def install(engine):
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if getattr(_tls, "on", False):
            _tls.t0 = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        if getattr(_tls, "on", False):
            t0 = getattr(_tls, "t0", None)
            ms = (time.perf_counter() - t0) * 1000 if t0 else 0.0
            _tls.rows.append((_label(statement), ms))

    @event.listens_for(engine, "commit")
    def _commit(conn):
        # Time of the COMMIT itself is captured by the caller (commit_ms); count it here.
        if getattr(_tls, "on", False):
            _tls.rows.append(("COMMIT", 0.0))


def summarise(rows):
    """('7', 'SELECT rooms 75.2ms, UPDATE lots 74.9ms, ...') for log lines."""
    n = len(rows)
    if not TRACE_SQL:
        return n, ""
    return n, ",".join(f"{label.replace(' ', '_')}:{ms:.0f}ms" for label, ms in rows)
