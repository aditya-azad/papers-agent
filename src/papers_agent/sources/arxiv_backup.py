"""Local arxiv metadata serving database, kept in sync with an upstream dump.

The serving database (e.g. a copy of the Kaggle arxiv metadata dump) is a
normal writable sqlite file holding the dump's papers plus derived indexes:
a doi map, first-submission dates and a full-text search index. It is the
living database — an upstream dump is only diff input; the file is never
replaced.

`ArxivBackupSource` serves lookups from it: keyword search via FTS5, exact
lookups by id/doi, and date-range queries. Every method returns None (or an
empty list/dict) when the database cannot answer a query — missing, syncing,
stale, or an error — so a chain can fall back to the live arxiv api.

The syncer runs as a detached subprocess. On first use the fingerprint of
the upstream dump is compared against the one recorded in the database; on
mismatch the dump is diffed against the database and only changed/new/
vanished rows are patched, including their derived rows and full-text
entries. The first run — or a database without recorded metadata — performs
a one-time full index build. A flock guard makes concurrent syncs
impossible, and `backup_status` reports progress.
"""

import asyncio
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

from papers_agent.errors import PaperNotFoundError, PaperSourceError
from papers_agent.sources.arxiv_paper import (
    ARXIV_DOI_PREFIX,
    arxiv_id_from_doi,
    arxiv_id_from_url,
    paper_from_arxiv_fields,
)
from papers_agent.types import (
    Doi,
    NonEmptyStr,
    Paper,
    PaperSource,
    Url,
    UTCDatetime,
)

_BACKUP_DB_ENV = "PAPERS_AGENT_ARXIV_BACKUP_DB"
_DEFAULT_BACKUP_DB = (
    Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
    / "papers-agent" / "arxiv" / "metadata.sqlite"
)
DEFAULT_BACKUP_DB = _DEFAULT_BACKUP_DB
_DUMP_DB_ENV = "PAPERS_AGENT_ARXIV_DUMP_DB"
_DEFAULT_DUMP_DB = Path(
    "~/database/third-party-media/papers/arxiv/metadata.sqlite"
).expanduser()

SCHEMA_VERSION = "2"

_PAPER_COLUMNS = (
    "id, submitter, authors, title, comments, journal_ref, doi, report_no, "
    "categories, license, abstract, authors_parsed, versions, update_date"
)
# The papers columns the syncer sets from the dump; id is the join key.
_PAPER_SETTABLE_COLUMNS = _PAPER_COLUMNS.removeprefix("id, ")
_CHUNK = 100
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
    """Cheap change marker for the dump file: size plus mtime."""
    stat = db_path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _published_at(
    versions_json: str | None, update_date: str | None
) -> datetime | None:
    """The paper's first submission date as a UTC datetime.

    Prefers the created stamp of the first version and falls back to the
    bare update_date (interpreted as UTC); None when neither parses.
    """
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
    """The paper's first submission date as an ISO YYYY-MM-DD string."""
    published = _published_at(versions_json, update_date)
    return published.date().isoformat() if published else None


def _authors_from_json(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value) if value else []
    except json.JSONDecodeError:
        return []
    authors: list[str] = []
    for entry in parsed:
        if not isinstance(entry, (list, tuple)):
            continue
        parts = [str(part).strip() for part in entry if str(part or "").strip()]
        # authors_parsed entries are [last, forenames, suffix]; the
        # suffix must stay last, so only the first two are reordered.
        if len(parts) >= 2:
            parts = [parts[1], parts[0], *parts[2:]]
        name = " ".join(parts)
        if name:
            authors.append(name)
    return authors


def _fts_match_query(keyword: str) -> str:
    tokens = keyword.split()
    return " ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
    )


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open the serving database in autocommit mode; None on error."""
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


def _paper_from_row(row: tuple) -> Paper | None:
    (arxiv_id, title, authors_parsed, versions_json, update_date, doi,
     abstract) = row
    try:
        return paper_from_arxiv_fields(
            base_id=arxiv_id.lower(),
            name=title,
            doi=doi or None,
            authors=_authors_from_json(authors_parsed),
            abstract=abstract or "",
            published_at=_published_at(versions_json, update_date),
        )
    except PaperSourceError:
        return None


_PAPER_ROW_QUERY = (
    "SELECT id, title, authors_parsed, versions, update_date, doi, abstract"
    " FROM papers WHERE id = ?"
)


class ArxivBackupSource(PaperSource):
    """Metadata lookups into the local arxiv serving database."""

    source = "arxiv"

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = (
            db_path
            if db_path is not None
            else Path(os.environ.get(_BACKUP_DB_ENV) or DEFAULT_BACKUP_DB)
        )
        self._syncer = Syncer(self._db_path)

    @property
    def syncer(self) -> Syncer:
        return self._syncer

    def prepare(self) -> None:
        """Start the sync subprocess when the database is stale."""
        self._syncer.ensure_started()

    @classmethod
    def handles_url(cls, url: Url) -> bool:
        return arxiv_id_from_url(url) is not None

    @classmethod
    def doi_for_url(cls, url: Url) -> Doi:
        """Derive the paper's doi from its arxiv abstract or pdf page url."""
        arxiv_id = arxiv_id_from_url(url)
        if arxiv_id is None:
            raise PaperNotFoundError(f"no arxiv paper for url {url}")
        return f"{ARXIV_DOI_PREFIX}{arxiv_id}"

    @classmethod
    def handles_doi(cls, doi: Doi) -> bool:
        return arxiv_id_from_doi(doi) is not None

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper] | None:
        """Keyword search over the serving database's full-text index."""
        return await asyncio.to_thread(self._search_sync, keyword, limit)

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        """Hydrate arxiv ids into papers from the serving database."""
        return await asyncio.to_thread(self._papers_by_ids_sync, arxiv_ids)

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper] | None:
        """Papers first submitted within [start, end], newest first.

        None when the range reaches past the database's cutoff, so the
        chain knows to fall back to the live api; an empty list means no
        matches within the covered range.
        """
        return await asyncio.to_thread(
            self._date_range_sync, start, end, limit
        )

    async def get_paper_by_url(self, url: Url) -> Paper | None:
        arxiv_id = arxiv_id_from_url(url)
        if arxiv_id is None:
            return None
        return await asyncio.to_thread(self._paper_by_id_sync, arxiv_id)

    async def get_paper_by_doi(self, doi: Doi) -> Paper | None:
        arxiv_id = arxiv_id_from_doi(doi)
        if arxiv_id is None:
            return None
        return await asyncio.to_thread(self._paper_by_id_sync, arxiv_id)

    async def download_pdf(self, doi: Doi, directory: Path) -> Path:
        raise PaperSourceError(
            "ArxivBackupSource does not support pdf downloads"
        )

    async def get_pdf_text(self, doi: Doi) -> str:
        raise PaperSourceError(
            "ArxivBackupSource does not support pdf text extraction"
        )

    async def get_pdf_images(
        self, doi: Doi, directory: Path
    ) -> list[Path]:
        raise PaperSourceError(
            "ArxivBackupSource does not support pdf image extraction"
        )

    def _paper_by_id_sync(self, arxiv_id: str) -> Paper | None:
        with _connect(self._db_path) as conn:
            if conn is None:
                return None
            try:
                row = conn.execute(_PAPER_ROW_QUERY, (arxiv_id,)).fetchone()
            except sqlite3.Error:
                return None
        return _paper_from_row(row) if row is not None else None

    def _papers_by_ids_sync(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        papers: dict[str, Paper] = {}
        for start in range(0, len(arxiv_ids), _CHUNK):
            chunk = arxiv_ids[start:start + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            with _connect(self._db_path) as conn:
                if conn is None:
                    return papers
                try:
                    rows = conn.execute(
                        "SELECT id, title, authors_parsed, versions,"
                        " update_date, doi, abstract"
                        f" FROM papers WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                except sqlite3.Error:
                    return papers
            for row in rows:
                paper = _paper_from_row(row)
                if paper is not None:
                    papers[row[0]] = paper
        return papers

    def _search_sync(self, keyword: str, limit: int) -> list[Paper] | None:
        match = _fts_match_query(keyword)
        sql = (
            "SELECT p.id, p.title, p.authors_parsed, p.versions,"
            " p.update_date, p.doi, p.abstract"
            " FROM (SELECT rowid AS f_rowid FROM paper_fts"
            " WHERE paper_fts MATCH ? ORDER BY rank LIMIT ?) m"
            " JOIN papers p ON p.rowid = m.f_rowid"
        )
        with _connect(self._db_path) as conn:
            if conn is None:
                return None
            try:
                rows = conn.execute(sql, (match, limit)).fetchall()
            except sqlite3.Error:
                return None
        return [paper for row in rows if (paper := _paper_from_row(row))]

    def _date_range_sync(
        self, start: datetime, end: datetime, limit: int
    ) -> list[Paper] | None:
        sql = (
            "SELECT p.id, p.title, p.authors_parsed, p.versions,"
            " p.update_date, p.doi, p.abstract"
            " FROM paper_date d JOIN papers p ON p.id = d.id"
            " WHERE d.pub_date >= ? AND d.pub_date <= ?"
            " ORDER BY d.pub_date DESC, d.id DESC LIMIT ?"
        )
        end_day = end.date().isoformat()
        start_day = start.date().isoformat()
        with _connect(self._db_path) as conn:
            if conn is None:
                return None
            try:
                cutoff = conn.execute(
                    "SELECT value FROM meta WHERE key = 'cutoff'"
                ).fetchone()
                if cutoff is None or end_day > cutoff[0]:
                    return None
                rows = conn.execute(sql, (start_day, end_day, limit)).fetchall()
            except sqlite3.Error:
                return None
        return [paper for row in rows if (paper := _paper_from_row(row))]


class Syncer:
    """Keeps the serving database in sync with the upstream dump."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock_path = db_path.with_name(db_path.name + ".sync.lock")
        self._progress_path = db_path.with_name(db_path.name + ".sync-progress")

    def state(self) -> str:
        """'missing', 'syncing', 'stale' or 'ready'."""
        if not self._db_path.exists():
            return "missing"
        if self._lock_is_held():
            return "syncing"
        try:
            return "ready" if self._is_ready() else "stale"
        except sqlite3.Error:
            return "stale"

    def status(self) -> dict[str, str]:
        """A summary for the backup_status tool."""
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
        """Spawn the sync subprocess when the database is stale or missing.

        Never blocks; the subprocess runs detached and takes the flock
        itself, so repeated calls are safe.
        """
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

    def _is_ready(self) -> bool:
        """True when the recorded dump fingerprint matches the dump."""
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
        """The subprocess entry point: sync once, then exit."""
        try:
            os.nice(10)
        except OSError:
            pass
        with self._lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # another sync is already running
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
        """Best-effort progress note; a plain file, not the locked db."""
        try:
            self._progress_path.write_text(text)
        except OSError:
            pass

    def _full_build(self, conn: sqlite3.Connection) -> None:
        """Rebuild every paper row and all derived indexes from the dump."""
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
        """Touch only rows that differ between the dump and the database."""
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
        # FTS external-content deletes need the old values, captured before
        # the papers rows change.
        conn.execute(
            "CREATE TEMP TABLE fts_gone AS"
            " SELECT p.rowid AS f_rowid, COALESCE(p.title, '') AS title,"
            " COALESCE(p.abstract, '') AS abstract"
            " FROM main.papers p WHERE p.id IN (SELECT id FROM temp.sync_ids)"
            " OR p.id IN (SELECT id FROM temp.gone_ids)"
        )
        conn.executemany(
            "INSERT INTO paper_fts(paper_fts, rowid, title, abstract)"
            " VALUES('delete', ?, ?, ?)",
            conn.execute(
                "SELECT f_rowid, title, abstract FROM temp.fts_gone"
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
            f"UPDATE main.papers SET ({_PAPER_SETTABLE_COLUMNS}) ="
            " (SELECT d.submitter, d.authors, d.title, d.comments,"
            " d.journal_ref, d.doi, d.report_no, d.categories, d.license,"
            " d.abstract, d.authors_parsed, d.versions, d.update_date"
            " FROM dump.papers d WHERE d.id = main.papers.id)"
            " WHERE id IN (SELECT id FROM temp.sync_ids WHERE kind = 'changed')"
        )
        conn.execute(
            f"INSERT INTO main.papers ({_PAPER_COLUMNS})"
            f" SELECT d.id, {_PAPER_SETTABLE_COLUMNS} FROM dump.papers d"
            " JOIN temp.sync_ids s ON s.id = d.id WHERE s.kind = 'new'"
        )
        self._rebuild_derived_rows(
            conn,
            "SELECT id, versions, update_date, doi, title, abstract"
            " FROM papers WHERE id IN (SELECT id FROM temp.sync_ids)",
        )
        conn.execute("DROP TABLE IF EXISTS temp.sync_ids")
        conn.execute("DROP TABLE IF EXISTS temp.gone_ids")
        conn.execute("DROP TABLE IF EXISTS temp.fts_gone")

    def _rebuild_derived_rows(
        self, conn: sqlite3.Connection, select_sql: str
    ) -> None:
        """Recompute doi/date/FTS rows for the papers the select yields."""
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


def _dump_path() -> Path | None:
    """The upstream dump to diff against; None when unset or missing."""
    env = os.environ.get(_DUMP_DB_ENV)
    path = Path(env).expanduser() if env else _DEFAULT_DUMP_DB
    return path if path.exists() else None


def main() -> None:
    """Run one sync pass and exit (the detached sync subprocess)."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--sync":
        Syncer(Path(sys.argv[2])).run_sync()
        return
    raise SystemExit(
        "usage: python -m papers_agent.sources.arxiv_backup --sync DB"
    )


if __name__ == "__main__":
    main()
