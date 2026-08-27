"""
One database layer, two backends: SQLite on a laptop, Postgres on Railway.

Which one is decided by a single environment variable. No DATABASE_URL means SQLite,
exactly as before; a DATABASE_URL means Postgres. Nothing else in the codebase has to
know, and local development needs no setup at all.

WHAT IS TRANSLATED AUTOMATICALLY, AND WHAT IS NOT.

`?` placeholders become `%s`. That is unambiguous -- the count and order are unchanged and
there is exactly one correct answer -- so doing it silently is safe.

`INSERT OR REPLACE` is NOT translated. Its Postgres equivalent is
`INSERT ... ON CONFLICT (cols) DO UPDATE`, which needs to know WHICH columns conflict, and
a translator would have to infer that from the schema. Infer it wrongly and the statement
still runs: it inserts a duplicate instead of replacing one. No error, no warning, just
quietly doubled rows -- in a project where double counting is the worst bug available.

So those get `upsert()` instead, where the conflict columns are written out in the calling
code and can be read in a diff. Slightly more to type, and impossible to get silently
wrong.

PRAGMA statements are dropped on Postgres rather than failing, because they are SQLite
tuning hints with no meaning elsewhere.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Iterable, Sequence

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = bool(DATABASE_URL)

_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.I)
_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\b", re.I)


class Placeholders:
    """`?` -> `%s`, but never inside a string literal or a comment.

    'what?' is text and must survive; the ? outside quotes is a parameter.

    COMMENTS MATTER, and not obviously. A `--` comment containing an apostrophe -- as in
    "an arbitrary row's value" -- would otherwise open a quote that never closes, and
    every placeholder after it in the query would be treated as literal text. The symptom
    is "the query has 0 placeholders but 1 parameters were passed", a long way from the
    comment that caused it. This walks the string rather than using a regular expression
    precisely because these cases have to be handled explicitly.
    """

    @staticmethod
    def convert(sql: str) -> str:
        out = []
        i, n = 0, len(sql)
        quote = None
        while i < n:
            ch = sql[i]
            if quote:
                out.append(ch)
                if ch == quote:
                    quote = None
                i += 1
            elif ch == "-" and sql.startswith("--", i):
                end = sql.find(chr(10), i)
                end = n if end == -1 else end
                out.append(sql[i:end])          # a line comment, copied verbatim
                i = end
            elif ch == "/" and sql.startswith("/*", i):
                end = sql.find("*/", i + 2)
                end = n if end == -1 else end + 2
                out.append(sql[i:end])          # a block comment, copied verbatim
                i = end
            elif ch in ("'", '"'):
                quote = ch
                out.append(ch)
                i += 1
            elif ch == "?":
                out.append("%s")
                i += 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)


class Row(dict):
    """A row that answers to both a column name and a position, like sqlite3.Row.

    The codebase uses r["artikelnr"] in some places and r[0] in others, and both are
    reasonable. psycopg offers one or the other, so this offers both -- otherwise every
    positional read would have to be hunted down and rewritten, and the ones missed would
    fail at runtime on Postgres only.
    """

    __slots__ = ("_values",)

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    cols = [d.name for d in (cursor.description or [])]
    return lambda values: Row(cols, values)


class Cursor:
    """A cursor that speaks SQLite's dialect to whichever backend is underneath."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, args: Sequence = ()):
        if _PRAGMA.match(sql):
            return self                       # a SQLite tuning hint; meaningless here
        if _OR_REPLACE.search(sql):
            raise ValueError(
                "INSERT OR REPLACE / OR IGNORE has no safe automatic translation to "
                "Postgres -- the conflict columns cannot be inferred without risking a "
                "silent duplicate. Use store.upsert(...) and name them.\n  " + sql[:120])
        self._cur.execute(Placeholders.convert(sql), tuple(args))
        return self

    def executemany(self, sql: str, rows: Iterable[Sequence]):
        if _OR_REPLACE.search(sql):
            raise ValueError("use store.upsert(...) instead of INSERT OR REPLACE")
        self._cur.executemany(Placeholders.convert(sql), [tuple(r) for r in rows])
        return self

    def executescript(self, script: str):
        """SQLite's multi-statement helper. Postgres runs the whole thing at once."""
        self._cur.execute(script)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size: int | None = None):
        return self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()

    def close(self):
        # pandas.read_sql closes the cursor it was handed. Without this the whole
        # analysis dies on Postgres with an AttributeError, several frames from the
        # query that actually mattered.
        self._cur.close()

    @property
    def arraysize(self):
        return getattr(self._cur, "arraysize", 1)

    @arraysize.setter
    def arraysize(self, n):
        self._cur.arraysize = n

    def __iter__(self):
        return iter(self._cur)

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount


class Connection:
    """Enough of the sqlite3 connection surface for this codebase, backed by Postgres."""

    def __init__(self, raw):
        self._raw = raw
        self.row_factory = None

    def cursor(self) -> Cursor:
        # row_factory mirrors sqlite3: unset gives plain tuples, set gives rows that
        # answer to a column name as well as a position.
        if self.row_factory is not None:
            return Cursor(self._raw.cursor(row_factory=_row_factory))
        return Cursor(self._raw.cursor())

    def execute(self, sql: str, args: Sequence = ()) -> Cursor:
        return self.cursor().execute(sql, args)

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> Cursor:
        return self.cursor().executemany(sql, rows)

    def executescript(self, script: str) -> Cursor:
        return self.cursor().executescript(script)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def connect(sqlite_path: str | None = None, url: str | None = None, rows: bool = False):
    """A connection to whichever backend is configured.

    `sqlite_path` is used only when there is no DATABASE_URL, so a caller can keep passing
    the path it always passed and stop thinking about it. `rows=True` asks for rows that
    answer to a column name, which is sqlite3.Row on one backend and store.Row on the
    other -- set here so no caller has to branch on which backend it got.
    """
    target = (url or DATABASE_URL).strip()
    if not target:
        con = sqlite3.connect(sqlite_path, timeout=30)
        if rows:
            con.row_factory = sqlite3.Row
        return con
    import psycopg
    con = Connection(psycopg.connect(target, autocommit=False))
    if rows:
        con.row_factory = True
    return con


def columns(con, table: str) -> set[str]:
    """The column names of a table, on either backend.

    PRAGMA table_info is SQLite-only and this layer drops PRAGMA on Postgres, so a
    migration that asked "does this column exist yet?" would get an empty answer, decide
    every column was missing, and try to add columns that are already there. That fails
    loudly rather than silently, but it fails on the first deploy -- which is the worst
    time to find out.
    """
    if IS_POSTGRES:
        got = con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()
        return {r[0] for r in got}
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def table_exists(con, table: str) -> bool:
    if IS_POSTGRES:
        return bool(con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,)).fetchone())
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


# --------------------------------------------------------------------------- upsert
def upsert(con, table: str, columns: Sequence[str], rows: Iterable[Sequence],
           conflict: Sequence[str], update: bool = True) -> int:
    """INSERT, and on a clash either replace the row or leave it alone.

    `conflict` names the columns that decide whether a row already exists -- normally the
    primary key. It is required and has no default: this is the operation where guessing
    wrongly means a duplicate rather than an error, so the caller states it and a reviewer
    can see it.

    update=True  replaces the existing row   (SQLite: INSERT OR REPLACE)
    update=False leaves the existing row     (SQLite: INSERT OR IGNORE)
    """
    rows = [tuple(r) for r in rows]
    if not rows:
        return 0
    if not conflict:
        raise ValueError("upsert needs the conflict columns; it will not guess them")
    cols = ", ".join(columns)

    if not IS_POSTGRES:
        verb = "INSERT OR REPLACE" if update else "INSERT OR IGNORE"
        sql = f"{verb} INTO {table} ({cols}) VALUES ({', '.join('?' * len(columns))})"
        con.executemany(sql, rows)
        return len(rows)

    marks = ", ".join(["%s"] * len(columns))
    target = ", ".join(conflict)
    if update:
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in columns if c not in conflict)
        tail = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
    else:
        tail = "DO NOTHING"
    sql = (f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
           f"ON CONFLICT ({target}) {tail}")
    con.executemany(sql, rows)
    return len(rows)


def backend() -> str:
    return "postgres" if IS_POSTGRES else "sqlite"


if __name__ == "__main__":
    print(f"backend: {backend()}")
    if IS_POSTGRES:
        with connect() as c:
            print("  ", c.execute("select version()").fetchone()[0][:60])
