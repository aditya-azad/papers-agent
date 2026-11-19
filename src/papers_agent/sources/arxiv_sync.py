import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO

_DUMP_DB_ENV = "PAPERS_AGENT_ARXIV_DUMP_DB"
_DEFAULT_DUMP_DB = Path(
    "~/database/third-party-media/papers/arxiv/metadata.sqlite"
).expanduser()

SCHEMA_VERSION = "2"

_PAPER_COLUMNS = (
    "id, submitter, authors, title, comments, journal_ref, doi, report_no, "
    "categories, license, abstract, authors_parsed, versions, update_date"
)
_PAPER_COLUMNS_SET_BY_SYNC_EXCLUDING_JOIN_KEY_ID = (
    _PAPER_COLUMNS.removeprefix("id, ")
)
_BATCH_ROWS = 10_000
_PROGRESS_EVERY = 200_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
    id             TEXT PRIMARY KEY,
    submitter      TEXT,
    authors        TEXT,
    title          TEXT,
    comments       TEXT,
    journal_ref    TEXT,
    doi            TEXT,
    report_no      TEXT,
    categories     TEXT,
    license        TEXT,
    abstract       TEXT,
    authors_parsed TEXT,
    versions       TEXT,
    update_date    TEXT
);
CREATE TABLE IF NOT EXISTS paper_date (
    id       TEXT PRIMARY KEY,
    pub_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_date ON paper_date(pub_date, id);
CREATE TABLE IF NOT EXISTS paper_doi (
    doi TEXT PRIMARY KEY,
    id  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_doi_id ON paper_doi(id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE paper_fts USING fts5 (
    title,
    abstract,
    content='papers',
    content_rowid='rowid'
);
"""


def fingerprint(db_path: Path) -> str:
    stat = db_path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _published_at(
    versions_json: str | None, update_date: str | None
) -> datetime | None:
    try:
        versions = json.loads(versions_json) if versions_json else []
    except json.JSONDecodeError:
        versions = []
    created = (
        versions[0].get("created")
        if versions and isinstance(versions[0], dict)
        else None
    )
    if isinstance(created, str) and created:
        try:
            return parsedate_to_datetime(created).astimezone(UTC)
        except (TypeError, ValueError):
            pass
    if update_date:
        try:
            return datetime.fromisoformat(update_date).replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def published_date(versions_json: str | None, update_date: str | None) -> str | None:
    published = _published_at(versions_json, update_date)
    return published.date().isoformat() if published else None


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection | None]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=rwc", uri=True)
    except sqlite3.Error:
        yield None
        return
    try:
        conn.isolation_level = None
        yield conn
    finally:
        conn.close()


def _dump_path() -> Path | None:
    env = os.environ.get(_DUMP_DB_ENV)
    path = Path(env).expanduser() if env else _DEFAULT_DUMP_DB
    return path if path.exists() else None


class Syncer:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock_path = db_path.with_name(db_path.name + ".sync.lock")
        self._progress_path = db_path.with_name(db_path.name + ".sync-progress")

    def state(self) -> str:
        if not self._db_path.exists():
            return "missing"
        if self._lock_is_held():
            return "syncing"
        try:
            return "ready" if self._is_ready() else "stale"
        except sqlite3.Error:
            return "stale"

    def status(self) -> dict[str, str]:
        result = {"state": self.state()}
        if result["state"] == "missing":
            return result
        with _connect(self._db_path) as conn:
            if conn is None:
                return result
            try:
                meta = dict(
                    conn.execute("SELECT key, value FROM meta").fetchall()
                )
            except sqlite3.Error:
                return result
        for key in ("synced_at", "fingerprint", "cutoff"):
            if key in meta:
                result[key] = meta[key]
        try:
            result["progress"] = self._progress_path.read_text()
        except OSError:
            pass
        return result

    def ensure_started(self) -> None:
        if self.state() in ("ready", "syncing"):
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "papers_agent.sources.arxiv_backup",
                "--sync",
                str(self._db_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _lock_is_held(self) -> bool:
        try:
            with self._lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False

    @staticmethod
    def _sync_lock_unavailable(sync_lock: IO[str]) -> bool:
        try:
            fcntl.flock(sync_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False

    def _is_ready(self) -> bool:
        dump_path = _dump_path()
        if dump_path is None:
            return False
        try:
            dump_fp = fingerprint(dump_path)
        except OSError:
            return False
        try:
            with _connect(self._db_path) as conn:
                if conn is None:
                    return False
                meta = dict(
                    conn.execute("SELECT key, value FROM meta").fetchall()
                )
        except sqlite3.Error:
            return False
        return (
            meta.get("schema_version") == SCHEMA_VERSION
            and meta.get("fingerprint") == dump_fp
            and meta.get("fts_ready") == "1"
        )

    def run_sync(self) -> None:
        try:
            os.nice(10)
        except OSError:
            pass
        with self._lock_path.open("w") as sync_lock:
            if self._sync_lock_unavailable(sync_lock):
                return
            try:
                if self._is_ready():
                    return
                self._sync()
            finally:
                self._progress_path.unlink(missing_ok=True)

    def _sync(self) -> None:
        dump_path = _dump_path()
        if dump_path is None:
            return
        try:
            dump_fp = fingerprint(dump_path)
        except OSError:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self._db_path) as conn:
            if conn is None:
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.execute(f"ATTACH DATABASE 'file:{dump_path}?mode=ro' AS dump")
            try:
                meta = dict(
                    conn.execute("SELECT key, value FROM meta").fetchall()
                )
            except sqlite3.Error:
                meta = {}
            full_build = (
                meta.get("schema_version") != SCHEMA_VERSION
                or "fingerprint" not in meta
            )
            try:
                conn.execute("BEGIN")
                if full_build:
                    self._full_build(conn)
                else:
                    self._diff_and_patch(conn)
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                return
            conn.execute("ANALYZE main")
            cutoff = conn.execute("SELECT MAX(pub_date) FROM paper_date").fetchone()
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("fingerprint", dump_fp),
                    ("synced_at", datetime.now(UTC).isoformat()),
                    ("cutoff", cutoff[0] if cutoff else ""),
                    ("fts_ready", "1"),
                    ("sync_state", "ready"),
                ],
            )

    def _report_progress(self, text: str) -> None:
        try:
            self._progress_path.write_text(text)
        except OSError:
            pass

    def _full_build(self, conn: sqlite3.Connection) -> None:
        self._report_progress("full build: replacing papers")
        conn.execute("DROP TABLE IF EXISTS paper_fts")
        conn.execute(_FTS_SCHEMA)
        conn.execute("DELETE FROM paper_date")
        conn.execute("DELETE FROM paper_doi")
        conn.execute("DELETE FROM papers")
        conn.execute(
            f"INSERT INTO papers ({_PAPER_COLUMNS})"
            f" SELECT {_PAPER_COLUMNS} FROM dump.papers"
        )
        self._rebuild_derived_rows(
            conn,
            "SELECT id, versions, update_date, doi, title, abstract FROM papers",
        )

    def _diff_and_patch(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TEMP TABLE sync_ids AS"
            " SELECT d.id, CASE WHEN p.id IS NULL THEN 'new'"
            " ELSE 'changed' END AS kind"
            " FROM dump.papers d LEFT JOIN main.papers p ON p.id = d.id"
            " WHERE p.id IS NULL OR p.update_date IS NOT d.update_date"
            " OR p.versions IS NOT d.versions OR p.doi IS NOT d.doi"
        )
        conn.execute(
            "CREATE TEMP TABLE gone_ids AS"
            " SELECT p.id FROM main.papers p WHERE NOT EXISTS"
            " (SELECT 1 FROM dump.papers d WHERE d.id = p.id)"
        )
        conn.execute(
            "CREATE TEMP TABLE fts_delete_old_values AS"
            " SELECT p.rowid AS f_rowid, COALESCE(p.title, '') AS title,"
            " COALESCE(p.abstract, '') AS abstract"
            " FROM main.papers p WHERE p.id IN (SELECT id FROM temp.sync_ids)"
            " OR p.id IN (SELECT id FROM temp.gone_ids)"
        )
        conn.executemany(
            "INSERT INTO paper_fts(paper_fts, rowid, title, abstract)"
            " VALUES('delete', ?, ?, ?)",
            conn.execute(
                "SELECT f_rowid, title, abstract FROM temp.fts_delete_old_values"
            ).fetchall(),
        )
        conn.execute(
            "DELETE FROM paper_doi WHERE id IN (SELECT id FROM temp.sync_ids)"
            " OR id IN (SELECT id FROM temp.gone_ids)"
        )
        conn.execute(
            "DELETE FROM paper_date WHERE id IN (SELECT id FROM temp.sync_ids)"
            " OR id IN (SELECT id FROM temp.gone_ids)"
        )
        conn.execute(
            "DELETE FROM main.papers WHERE id IN (SELECT id FROM temp.gone_ids)"
        )
        conn.execute(
            f"UPDATE main.papers SET ({_PAPER_COLUMNS_SET_BY_SYNC_EXCLUDING_JOIN_KEY_ID}) ="
            " (SELECT d.submitter, d.authors, d.title, d.comments,"
            " d.journal_ref, d.doi, d.report_no, d.categories, d.license,"
            " d.abstract, d.authors_parsed, d.versions, d.update_date"
            " FROM dump.papers d WHERE d.id = main.papers.id)"
            " WHERE id IN (SELECT id FROM temp.sync_ids WHERE kind = 'changed')"
        )
        conn.execute(
            f"INSERT INTO main.papers ({_PAPER_COLUMNS})"
            f" SELECT d.id, {_PAPER_COLUMNS_SET_BY_SYNC_EXCLUDING_JOIN_KEY_ID} FROM dump.papers d"
            " JOIN temp.sync_ids s ON s.id = d.id WHERE s.kind = 'new'"
        )
        self._rebuild_derived_rows(
            conn,
            "SELECT id, versions, update_date, doi, title, abstract"
            " FROM papers WHERE id IN (SELECT id FROM temp.sync_ids)",
        )
        conn.execute("DROP TABLE IF EXISTS temp.sync_ids")
        conn.execute("DROP TABLE IF EXISTS temp.gone_ids")
        conn.execute("DROP TABLE IF EXISTS temp.fts_delete_old_values")

    def _rebuild_derived_rows(
        self, conn: sqlite3.Connection, select_sql: str
    ) -> None:
        rows = conn.execute(select_sql)
        pending_dates: list[tuple[str, str]] = []
        pending_dois: list[tuple[str, str]] = []
        pending_fts: list[tuple[str, str, str]] = []
        for processed, row in enumerate(rows, start=1):
            arxiv_id, versions_json, update_date, doi, title, abstract = row
            pub_date = published_date(versions_json, update_date)
            if pub_date:
                pending_dates.append((arxiv_id, pub_date))
            if doi:
                pending_dois.append((doi.lower(), arxiv_id))
            pending_fts.append((arxiv_id, title or "", abstract or ""))
            if len(pending_fts) >= _BATCH_ROWS:
                self._flush_derived(conn, pending_dates, pending_dois, pending_fts)
                pending_dates, pending_dois, pending_fts = [], [], []
                if processed % _PROGRESS_EVERY < _BATCH_ROWS:
                    self._report_progress(f"{processed} rows reindexed")
        self._flush_derived(conn, pending_dates, pending_dois, pending_fts)

    def _flush_derived(
        self,
        conn: sqlite3.Connection,
        dates: list[tuple[str, str]],
        dois: list[tuple[str, str]],
        fts: list[tuple[str, str, str]],
    ) -> None:
        if dates:
            conn.executemany(
                "INSERT OR REPLACE INTO paper_date (id, pub_date) VALUES (?, ?)",
                dates,
            )
        if dois:
            conn.executemany(
                "INSERT OR REPLACE INTO paper_doi (doi, id) VALUES (?, ?)",
                dois,
            )
        if fts:
            conn.executemany(
                "INSERT INTO paper_fts(rowid, title, abstract)"
                " SELECT rowid, ?, ? FROM papers WHERE id = ?",
                [(title, abstract, arxiv_id) for arxiv_id, title, abstract in fts],
            )
