"""Persistent sqlite cache for successful paper source query results.

The cache lives in the user's home directory and survives server
restarts, so repeated queries and repeated requests for a specific
paper are served locally instead of hitting the network again. Every
cache lookup degrades gracefully: on any sqlite/filesystem error the
cache simply reports a miss and the caller falls back to the source.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from papers_agent.types import Paper

_DEFAULT_DB_PATH = Path.home() / ".papers-agent-cache" / "cache.sqlite3"

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    source TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    doi TEXT NOT NULL,
    data TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (source, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers (source, doi);
CREATE TABLE IF NOT EXISTS query_results (
    cache_key TEXT PRIMARY KEY,
    paper_urls TEXT NOT NULL,
    cached_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS text_results (
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    doi TEXT NOT NULL,
    content TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (source, kind, doi)
);
CREATE TABLE IF NOT EXISTS pdf_results (
    source TEXT NOT NULL,
    doi TEXT NOT NULL,
    content BLOB NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (source, doi)
);
"""

T = TypeVar("T")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SourceCache:
    """Sqlite-backed cache of papers and query results, keyed per source."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            env_path = os.environ.get("PAPERS_AGENT_CACHE_DB")
            path = Path(env_path) if env_path else _DEFAULT_DB_PATH
        self._path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    @staticmethod
    def query_key(source: str, scope: str, params: dict[str, str]) -> str:
        """Stable cache key for a query against a source."""
        canonical = json.dumps(
            [source, scope, sorted(params.items())], separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _open(self) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != _SCHEMA_VERSION:
                connection.executescript(
                    "DROP TABLE IF EXISTS papers;"
                    " DROP TABLE IF EXISTS query_results;"
                    " DROP TABLE IF EXISTS text_results;"
                    " DROP TABLE IF EXISTS pdf_results;"
                )
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            else:
                connection.executescript(_SCHEMA)
            connection.commit()
        except (OSError, sqlite3.Error):
            return None
        self._connection = connection
        return connection

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T | None:
        def task() -> T | None:
            connection = self._open()
            if connection is None:
                return None
            try:
                with self._lock:
                    return operation(connection)
            except sqlite3.Error:
                return None

        return await asyncio.to_thread(task)

    @staticmethod
    def _paper_from_row(row: object) -> Paper | None:
        try:
            return Paper.model_validate_json(row[0])
        except ValidationError:
            return None

    async def get_paper_by_url(self, source: str, url: str) -> Paper | None:
        def operation(connection: sqlite3.Connection) -> Paper | None:
            row = connection.execute(
                "SELECT data FROM papers WHERE source = ? AND cache_key = ?",
                (source, url),
            ).fetchone()
            return self._paper_from_row(row) if row is not None else None

        return await self._run(operation)

    async def get_paper_by_doi(self, source: str, doi: str) -> Paper | None:
        def operation(connection: sqlite3.Connection) -> Paper | None:
            row = connection.execute(
                "SELECT data FROM papers WHERE source = ? AND doi = ?"
                " ORDER BY cached_at DESC LIMIT 1",
                (source, doi),
            ).fetchone()
            return self._paper_from_row(row) if row is not None else None

        return await self._run(operation)

    async def put_paper(self, source: str, paper: Paper) -> None:
        data = paper.model_dump_json()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO papers (source, cache_key, doi, data,"
                " cached_at) VALUES (?, ?, ?, ?, ?)",
                (source, paper.url, paper.doi, data, _now()),
            )
            connection.commit()

        await self._run(operation)

    async def get_papers_by_urls(self, source: str, urls: list[str]) -> dict[str, Paper]:
        def operation(connection: sqlite3.Connection) -> dict[str, Paper]:
            papers: dict[str, Paper] = {}
            for url in urls:
                row = connection.execute(
                    "SELECT data FROM papers WHERE source = ? AND cache_key = ?",
                    (source, url),
                ).fetchone()
                if row is None:
                    continue
                paper = self._paper_from_row(row)
                if paper is not None:
                    papers[url] = paper
            return papers

        return await self._run(operation) or {}

    async def get_query_result(self, cache_key: str) -> list[str] | None:
        def operation(connection: sqlite3.Connection) -> list[str] | None:
            row = connection.execute(
                "SELECT paper_urls FROM query_results WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            try:
                urls = json.loads(row[0])
            except ValueError:
                return None
            return urls if isinstance(urls, list) else None

        return await self._run(operation)

    async def put_query_result(self, cache_key: str, paper_urls: list[str]) -> None:
        encoded = json.dumps(paper_urls)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO query_results (cache_key, paper_urls,"
                " cached_at) VALUES (?, ?, ?)",
                (cache_key, encoded, _now()),
            )
            connection.commit()

        await self._run(operation)

    async def get_text(self, source: str, kind: str, doi: str) -> str | None:
        def operation(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                "SELECT content FROM text_results WHERE source = ? AND kind = ?"
                " AND doi = ?",
                (source, kind, doi),
            ).fetchone()
            return row[0] if row is not None else None

        return await self._run(operation)

    async def put_text(self, source: str, kind: str, doi: str, content: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO text_results (source, kind, doi, content,"
                " cached_at) VALUES (?, ?, ?, ?, ?)",
                (source, kind, doi, content, _now()),
            )
            connection.commit()

        await self._run(operation)

    async def get_pdf(self, source: str, doi: str) -> bytes | None:
        def operation(connection: sqlite3.Connection) -> bytes | None:
            row = connection.execute(
                "SELECT content FROM pdf_results WHERE source = ? AND doi = ?",
                (source, doi),
            ).fetchone()
            return row[0] if row is not None else None

        return await self._run(operation)

    async def put_pdf(self, source: str, doi: str, content: bytes) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO pdf_results (source, doi, content,"
                " cached_at) VALUES (?, ?, ?, ?)",
                (source, doi, content, _now()),
            )
            connection.commit()

        await self._run(operation)
