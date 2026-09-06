"""SQLite index over every archived version.

The extracted trees and ``manifest.json`` files on disk are the source of
truth; this database is a queryable index built from them, so it can always be
rebuilt with ``lambda-watcher reindex``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS functions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    notes       TEXT
);

-- Filename patterns that map a download onto an existing function.
CREATE TABLE IF NOT EXISTS aliases (
    id          INTEGER PRIMARY KEY,
    function_id INTEGER NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
    pattern     TEXT NOT NULL,
    is_regex    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(function_id, pattern)
);

CREATE TABLE IF NOT EXISTS versions (
    id              INTEGER PRIMARY KEY,
    function_id     INTEGER NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    tree_hash       TEXT NOT NULL,
    zip_sha256      TEXT,
    zip_size        INTEGER,
    source_name     TEXT,
    source_path     TEXT,
    source_mtime    TEXT,
    ingested_at     TEXT NOT NULL,
    dir             TEXT NOT NULL,
    runtime         TEXT,
    runtime_confidence TEXT,
    handler         TEXT,
    file_count      INTEGER NOT NULL DEFAULT 0,
    total_size      INTEGER NOT NULL DEFAULT 0,
    code_file_count INTEGER NOT NULL DEFAULT 0,
    code_size       INTEGER NOT NULL DEFAULT 0,
    code_lines      INTEGER NOT NULL DEFAULT 0,
    label           TEXT,
    UNIQUE(function_id, seq)
    -- Deliberately no UNIQUE on (function_id, tree_hash): duplicate content is
    -- caught by an explicit lookup, which `ingest --force` is allowed to skip.
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    mode        INTEGER,
    is_text     INTEGER NOT NULL DEFAULT 0,
    is_vendor   INTEGER NOT NULL DEFAULT 0,
    lang        TEXT,
    lines       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deps (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    manager     TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     TEXT,
    source      TEXT,
    is_declared INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS env_vars (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    path        TEXT,
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    service     TEXT NOT NULL,
    path        TEXT,
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    path        TEXT,
    line        INTEGER,
    detail      TEXT,
    is_vendor   INTEGER NOT NULL DEFAULT 0
);

-- Audit trail: every download seen, including ones skipped as duplicates.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    function_id INTEGER REFERENCES functions(id) ON DELETE SET NULL,
    version_id  INTEGER REFERENCES versions(id) ON DELETE SET NULL,
    source_path TEXT,
    detail      TEXT
);

-- Downloads already handled, so a restart does not re-ingest them.
CREATE TABLE IF NOT EXISTS seen_downloads (
    zip_sha256  TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    times_seen  INTEGER NOT NULL DEFAULT 1,
    source_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_version ON files(version_id);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_deps_version ON deps(version_id);
CREATE INDEX IF NOT EXISTS idx_env_version ON env_vars(version_id);
CREATE INDEX IF NOT EXISTS idx_services_version ON services(version_id);
CREATE INDEX IF NOT EXISTS idx_findings_version ON findings(version_id);
CREATE INDEX IF NOT EXISTS idx_versions_function ON versions(function_id);
CREATE INDEX IF NOT EXISTS idx_versions_tree ON versions(function_id, tree_hash);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class _LockedConnection:
    """Serialises access to one sqlite connection across threads.

    The watcher ingests on a worker thread while the main thread reads, and
    sqlite3 connections are single-threaded by default. Guarding every
    statement with one re-entrant lock keeps a single connection (and therefore
    one WAL writer) without sprinkling locks through the query methods.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        """Wrap ``conn``, serialising every statement through ``lock``."""
        self._conn = conn
        self._lock = lock

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Run one statement while holding the lock."""
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Run one statement over many parameter rows while holding the lock."""
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Run a multi-statement script while holding the lock."""
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def close(self) -> None:
        """Close the underlying connection while holding the lock."""
        with self._lock:
            self._conn.close()

    def __getattr__(self, name: str) -> Any:
        """Pass anything else through to the real connection.

        Only statement execution needs serialising, so the plain attributes and
        the methods that do no SQL of their own are forwarded unguarded.
        """
        return getattr(self._conn, name)


class Database:
    """Thin wrapper around sqlite3 with the queries the CLI needs."""

    def __init__(self, path: Path) -> None:
        """Open (or create) the index at ``path`` and bring the schema up to date.

        The connection is deliberately shared rather than per-thread:
        ``check_same_thread=False`` plus the re-entrant lock in
        :class:`_LockedConnection` gives exactly one writer, which is what the
        watcher's single ingest worker needs. ``isolation_level=None`` turns off
        Python's implicit transactions so :meth:`transaction` can own them, and WAL
        mode (set by :data:`SCHEMA`) lets readers work while that writer commits.

        Creating the file and its parent directory is part of the job — no setup
        step has to run first.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        raw = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        raw.row_factory = sqlite3.Row
        self.conn = _LockedConnection(raw, self._lock)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        """Close the connection, ignoring an already-closed one.

        Called from teardown paths that should not fail because the database went
        away first.
        """
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> Database:
        """Support ``with Database(path) as db:``."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the connection when the ``with`` block ends, error or not."""
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Hold the connection lock for the whole BEGIN..COMMIT block."""
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # -- functions -------------------------------------------------------
    def get_function_by_name(self, name: str, case_insensitive: bool = True) -> sqlite3.Row | None:
        """Look up one function by its exact name.

        Matching ignores case by default, because ``OrderProcessor`` and
        ``orderprocessor`` in two differently-cased downloads are one function, not
        two.
        """
        if case_insensitive:
            row = self.conn.execute(
                "SELECT * FROM functions WHERE lower(name) = lower(?)", (name,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM functions WHERE name = ?", (name,)).fetchone()
        return row

    def get_function(self, ident: str) -> sqlite3.Row | None:
        """Resolve by exact name, slug, or unique case-insensitive prefix."""
        row = self.get_function_by_name(ident)
        if row:
            return row
        row = self.conn.execute("SELECT * FROM functions WHERE slug = ?", (ident,)).fetchone()
        if row:
            return row
        rows = self.conn.execute(
            "SELECT * FROM functions WHERE lower(name) LIKE lower(?) OR lower(slug) LIKE lower(?)",
            (f"%{ident}%", f"%{ident}%"),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        return None

    def upsert_function(self, name: str, slug: str, now: str) -> int:
        """Return the id for ``name``, creating the function if it is new.

        Either way ``last_seen`` ends up as ``now``, which is what orders the
        dashboard. This is the only place a function row is born, so an ingest of a
        never-before-seen zip needs no separate registration step.
        """
        existing = self.get_function_by_name(name)
        if existing:
            self.conn.execute(
                "UPDATE functions SET last_seen = ? WHERE id = ?", (now, existing["id"])
            )
            return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO functions(name, slug, first_seen, last_seen) VALUES(?,?,?,?)",
            (name, slug, now, now),
        )
        return int(cur.lastrowid)

    def list_functions(self) -> list[sqlite3.Row]:
        """Every function, with its version count and latest sequence number.

        Ordered by ``last_seen`` descending, so whatever was deployed most recently
        is the first row of ``lw list``. The two counts are subqueries rather than a
        join so functions with no versions still appear.
        """
        return self.conn.execute(
            """
            SELECT f.*,
                   (SELECT COUNT(*) FROM versions v WHERE v.function_id = f.id) AS version_count,
                   (SELECT MAX(v.seq) FROM versions v WHERE v.function_id = f.id) AS latest_seq
            FROM functions f
            ORDER BY f.last_seen DESC
            """
        ).fetchall()

    def archive_totals(self) -> tuple[int, int, int]:
        """Functions, versions and indexed bytes — the three numbers `lw status` shows."""
        row = self.conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM functions)                    AS functions,
                   (SELECT COUNT(*) FROM versions)                     AS versions,
                   (SELECT COALESCE(SUM(total_size), 0) FROM versions) AS bytes
            """
        ).fetchone()
        return int(row["functions"]), int(row["versions"]), int(row["bytes"])

    def rename_function(self, function_id: int, new_name: str, new_slug: str) -> None:
        """Point a function row at a new name and slug."""
        self.conn.execute(
            "UPDATE functions SET name = ?, slug = ? WHERE id = ?", (new_name, new_slug, function_id)
        )

    def delete_function(self, function_id: int) -> None:
        """Delete a function and, by cascade, all of its versions and their rows.

        Only removes the index entries. The archived files on disk are the source of
        truth and are the caller's to remove.
        """
        self.conn.execute("DELETE FROM functions WHERE id = ?", (function_id,))

    # -- aliases ---------------------------------------------------------
    def add_alias(self, function_id: int, pattern: str, is_regex: bool = False) -> None:
        """Teach the identifier that a filename pattern belongs to this function.

        Does nothing if the same pattern is already registered, so re-running a
        setup step is harmless.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO aliases(function_id, pattern, is_regex) VALUES(?,?,?)",
            (function_id, pattern, int(is_regex)),
        )

    def list_aliases(self) -> list[sqlite3.Row]:
        """Every alias, with the name of the function it maps onto."""
        return self.conn.execute(
            "SELECT a.*, f.name AS function_name FROM aliases a JOIN functions f ON f.id = a.function_id"
        ).fetchall()

    # -- versions --------------------------------------------------------
    def next_seq(self, function_id: int) -> int:
        """The sequence number the next version of this function should get.

        Versions count from 1, so a function with nothing archived yet gets 1.
        Derived from the highest sequence in use, so pruning old versions never
        renumbers the ones that remain — these are what ``lw diff 7 8`` refers
        to. Deleting the *newest* version does hand its number back out again.
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM versions WHERE function_id = ?", (function_id,)
        ).fetchone()
        return int(row["m"]) + 1

    def find_version_by_tree_hash(self, function_id: int, tree_hash: str) -> sqlite3.Row | None:
        """The most recent version of this function with exactly this content."""
        return self.conn.execute(
            "SELECT * FROM versions WHERE function_id = ? AND tree_hash = ?"
            " ORDER BY seq DESC LIMIT 1",
            (function_id, tree_hash),
        ).fetchone()

    def insert_version(self, values: dict[str, Any]) -> int:
        """Insert a version row from a column/value dict and return its new id.

        Takes a dict rather than a long parameter list because the caller builds
        the row from a manifest, and the columns present vary with what the analysis
        found.
        """
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(
            f"INSERT INTO versions({cols}) VALUES({marks})", tuple(values.values())
        )
        return int(cur.lastrowid)

    def list_versions(self, function_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        """This function's versions, newest first, optionally capped at ``limit``."""
        sql = "SELECT * FROM versions WHERE function_id = ? ORDER BY seq DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, (function_id,)).fetchall()

    def get_version(self, function_id: int, seq: int) -> sqlite3.Row | None:
        """One specific version by its sequence number, or None."""
        return self.conn.execute(
            "SELECT * FROM versions WHERE function_id = ? AND seq = ?", (function_id, seq)
        ).fetchone()

    def latest_version(self, function_id: int) -> sqlite3.Row | None:
        """The most recently archived version of this function, or None."""
        return self.conn.execute(
            "SELECT * FROM versions WHERE function_id = ? ORDER BY seq DESC LIMIT 1", (function_id,)
        ).fetchone()

    def delete_version(self, version_id: int) -> None:
        """Delete a version and, by cascade, its files, deps, findings and the rest."""
        self.conn.execute("DELETE FROM versions WHERE id = ?", (version_id,))

    def set_version_label(self, version_id: int, label: str | None) -> None:
        """Attach a human label to a version, or clear it by passing None.

        Lets a reader mark ``v7`` as ``the one that broke checkout`` and have it show
        up in listings next to the sequence number.
        """
        self.conn.execute("UPDATE versions SET label = ? WHERE id = ?", (label, version_id))

    # -- child rows ------------------------------------------------------
    def bulk_insert(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        """Insert many rows into one table in a single statement.

        The write path adds thousands of ``files`` rows per version, and one
        ``executemany`` is dramatically faster than a loop. An empty ``rows``
        returns immediately rather than issuing a no-op statement.

        The table and column names are interpolated into the SQL, so they must stay
        internal constants — every value still goes through a placeholder.
        """
        if not rows:
            return
        marks = ", ".join("?" for _ in columns)
        self.conn.executemany(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES({marks})", rows
        )

    def files_for(self, version_id: int) -> list[sqlite3.Row]:
        """Every indexed file in a version, ordered by path."""
        return self.conn.execute(
            "SELECT * FROM files WHERE version_id = ? ORDER BY path", (version_id,)
        ).fetchall()

    def deps_for(self, version_id: int) -> list[sqlite3.Row]:
        """Every dependency recorded for a version, declared and installed alike."""
        return self.conn.execute(
            "SELECT * FROM deps WHERE version_id = ? ORDER BY manager, name", (version_id,)
        ).fetchall()

    def env_for(self, version_id: int) -> list[sqlite3.Row]:
        """Every environment variable reference recorded for a version."""
        return self.conn.execute(
            "SELECT * FROM env_vars WHERE version_id = ? ORDER BY name", (version_id,)
        ).fetchall()

    def services_for(self, version_id: int) -> list[sqlite3.Row]:
        """Every AWS service reference recorded for a version."""
        return self.conn.execute(
            "SELECT * FROM services WHERE version_id = ? ORDER BY service", (version_id,)
        ).fetchall()

    def findings_for(self, version_id: int, include_vendor: bool = False) -> list[sqlite3.Row]:
        """Security and risk findings for a version, worst severity first.

        Findings inside vendored code are hidden unless ``include_vendor`` is set:
        a scan of ``node_modules`` turns up plenty that is nobody's business here
        and would bury the handful that are.
        """
        sql = "SELECT * FROM findings WHERE version_id = ?"
        if not include_vendor:
            sql += " AND is_vendor = 0"
        sql += " ORDER BY severity, path"
        return self.conn.execute(sql, (version_id,)).fetchall()

    # -- events / dedup --------------------------------------------------
    def log_event(
        self,
        kind: str,
        ts: str,
        function_id: int | None = None,
        version_id: int | None = None,
        source_path: str | None = None,
        detail: Any = None,
    ) -> None:
        """Record something that happened, for the audit trail behind ``lw events``.

        Every download the watcher sees is logged, including the ones that produced
        no version — a duplicate download and an unchanged tree are both answers to
        "why did nothing appear when I saved that zip?". A ``detail`` that is not
        already a string is stored as JSON.
        """
        payload = detail if isinstance(detail, str) or detail is None else json.dumps(detail)
        self.conn.execute(
            "INSERT INTO events(ts, kind, function_id, version_id, source_path, detail)"
            " VALUES(?,?,?,?,?,?)",
            (ts, kind, function_id, version_id, source_path, payload),
        )

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        """The most recent events, newest first, with function and version names joined in.

        Left joins, so an event that never got as far as identifying a function
        still shows up — those are usually the interesting ones.
        """
        return self.conn.execute(
            """
            SELECT e.*, f.name AS function_name, v.seq AS version_seq
            FROM events e
            LEFT JOIN functions f ON f.id = e.function_id
            LEFT JOIN versions v ON v.id = e.version_id
            ORDER BY e.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def seen_download(self, zip_sha256: str) -> sqlite3.Row | None:
        """The record of a previously-ingested zip with this hash, or None.

        How the ingest recognises a literal re-download and skips it. Keyed on the
        zip's own hash, not the tree hash — see the two-hash note in the module
        docs.
        """
        return self.conn.execute(
            "SELECT * FROM seen_downloads WHERE zip_sha256 = ?", (zip_sha256,)
        ).fetchone()

    def mark_download_seen(self, zip_sha256: str, now: str, source_name: str) -> None:
        """Record that this zip has been handled, or bump its counter if it was already.

        ``times_seen`` counting up is normal and harmless: browsers re-download,
        and users re-save. It is kept because it answers "is this the same file
        arriving again?" when someone wonders why no new version appeared.
        """
        self.conn.execute(
            """
            INSERT INTO seen_downloads(zip_sha256, first_seen, last_seen, times_seen, source_name)
            VALUES(?,?,?,1,?)
            ON CONFLICT(zip_sha256) DO UPDATE SET
                last_seen = excluded.last_seen,
                times_seen = times_seen + 1
            """,
            (zip_sha256, now, now, source_name),
        )

    # -- search ----------------------------------------------------------
    def search_files(self, term: str, limit: int = 100) -> list[sqlite3.Row]:
        """Find archived files whose path contains ``term``, across every function.

        Answers "which of my Lambdas ships this file?" — substring matching, so
        ``handler`` finds ``src/handler.py`` too.
        """
        return self.conn.execute(
            """
            SELECT f.name AS function_name, v.seq, fi.path, fi.size
            FROM files fi
            JOIN versions v ON v.id = fi.version_id
            JOIN functions f ON f.id = v.function_id
            WHERE fi.path LIKE ?
            ORDER BY f.name, v.seq DESC LIMIT ?
            """,
            (f"%{term}%", limit),
        ).fetchall()

    def search_deps(self, term: str, limit: int = 200) -> list[sqlite3.Row]:
        """Find versions that depend on a package whose name contains ``term``.

        The query behind "which functions are still on the old ``requests``?".
        Distinct rows, so a package declared and installed at once is listed once
        per version rather than twice.
        """
        return self.conn.execute(
            """
            SELECT DISTINCT f.name AS function_name, v.seq, d.manager, d.name, d.version
            FROM deps d
            JOIN versions v ON v.id = d.version_id
            JOIN functions f ON f.id = v.function_id
            WHERE d.name LIKE ?
            ORDER BY f.name, v.seq DESC LIMIT ?
            """,
            (f"%{term}%", limit),
        ).fetchall()
