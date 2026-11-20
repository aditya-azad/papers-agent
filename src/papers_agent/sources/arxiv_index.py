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

SCHEMA_VERSION = "3"

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


class Indexer:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock_path = db_path.with_name(db_path.name + ".index.lock")
        self._progress_path = db_path.with_name(db_path.name + ".index-progress")

    def state(self) -> str:
        if not self._db_path.exists():
            return "missing"
        if self._lock_is_held():
            return "indexing"
        try:
            return "ready" if self._is_indexed() else "unindexed"
        except sqlite3.Error:
            return "unindexed"

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
        for key in ("indexed_at", "cutoff"):
            if key in meta:
                result[key] = meta[key]
        try:
            result["progress"] = self._progress_path.read_text()
        except OSError:
            pass
        return result

    def ensure_started(self) -> None:
        if self.state() in ("ready", "indexing"):
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "papers_agent.sources.arxiv_index",
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
    def _index_lock_unavailable(index_lock: IO[str]) -> bool:
        try:
            fcntl.flock(index_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False

    def _is_indexed(self) -> bool:
        with _connect(self._db_path) as conn:
            if conn is None:
                return False
            try:
                papers = conn.execute(
                    "SELECT count(*) FROM papers"
                ).fetchone()[0]
                fts = conn.execute(
                    "SELECT count(*) FROM paper_fts"
                ).fetchone()[0]
                dates = conn.execute(
                    "SELECT count(*) FROM paper_date"
                ).fetchone()[0]
                dois = conn.execute(
                    "SELECT count(*) FROM paper_doi"
                ).fetchone()[0]
            except sqlite3.Error:
                return False
        return papers > 0 and fts == papers and dates <= papers and dois <= papers

    def run_build(self) -> None:
        try:
            os.nice(10)
        except OSError:
            pass
        with self._lock_path.open("w") as index_lock:
            if self._index_lock_unavailable(index_lock):
                return
            try:
                if self._is_indexed():
                    return
                self._build()
            finally:
                self._progress_path.unlink(missing_ok=True)

    def _build(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self._db_path) as conn:
            if conn is None:
                return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            try:
                conn.execute("BEGIN")
                self._rebuild_derived_rows(
                    conn,
                    "SELECT id, versions, update_date, doi, title, abstract"
                    " FROM papers",
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                return
            conn.execute("ANALYZE")
            cutoff = conn.execute("SELECT MAX(pub_date) FROM paper_date").fetchone()
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("indexed_at", datetime.now(UTC).isoformat()),
                    ("cutoff", cutoff[0] if cutoff else ""),
                ],
            )

    def _report_progress(self, text: str) -> None:
        try:
            self._progress_path.write_text(text)
        except OSError:
            pass

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
                    self._report_progress(f"{processed} rows indexed")
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


def main() -> None:
    if len(sys.argv) == 2:
        Indexer(Path(sys.argv[1])).run_build()
        return
    raise SystemExit("usage: python -m papers_agent.sources.arxiv_index DB")


if __name__ == "__main__":
    main()
