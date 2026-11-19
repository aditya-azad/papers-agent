
import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from papers_agent.types import Paper

_DEFAULT_DB_PATH = Path.home() / ".papers-agent-cache" / "cache.sqlite3"

_CACHE_TTL = timedelta(days=3)

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


def _no_extra(value: object) -> tuple:
    return ()


def _decode_paper(row: object) -> Paper | None:
    try:
        return Paper.model_validate_json(row[0])
    except ValidationError:
        return None


def _decode_urls(row: object) -> list[str] | None:
    try:
        urls = json.loads(row[0])
    except ValueError:
        return None
    return urls if isinstance(urls, list) else None


@dataclass(frozen=True)
class _Table:
    name: str
    keys: tuple[str, ...]
    content: str
    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any | None]
    extra: tuple[str, ...] = ()
    extra_of: Callable[[Any], tuple] = _no_extra


_TABLES: dict[str, _Table] = {
    "papers": _Table(
        "papers", ("source", "cache_key"), "data",
        lambda paper: paper.model_dump_json(), _decode_paper,
        extra=("doi",), extra_of=lambda paper: (paper.doi,),
    ),
    "query_results": _Table(
        "query_results", ("cache_key",), "paper_urls", json.dumps, _decode_urls,
    ),
    "text_results": _Table(
        "text_results", ("source", "kind", "doi"), "content",
        lambda text: text, lambda row: row[0],
    ),
    "pdf_results": _Table(
        "pdf_results", ("source", "doi"), "content",
        lambda blob: blob, lambda row: row[0],
    ),
}


class SourceCache:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            env_path = os.environ.get("PAPERS_AGENT_CACHE_DB")
            path = Path(env_path) if env_path else _DEFAULT_DB_PATH
        self._path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    @staticmethod
    def query_key(source: str, scope: str, params: dict[str, str]) -> str:
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
    def _where(spec: _Table) -> str:
        return " AND ".join(f"{column} = ?" for column in spec.keys)

    @staticmethod
    def _touch(
        connection: sqlite3.Connection,
        table: str,
        where: str,
        params: tuple,
    ) -> None:
        connection.execute(
            f"UPDATE {table} SET cached_at = ? WHERE {where}", (_now(), *params)
        )

    async def _get(self, table: str, key_values: tuple) -> T | None:
        spec = _TABLES[table]
        select = f"SELECT {spec.content} FROM {spec.name} WHERE {self._where(spec)}"

        def operation(connection: sqlite3.Connection) -> T | None:
            row = connection.execute(select, key_values).fetchone()
            if row is None:
                return None
            self._touch(connection, spec.name, self._where(spec), key_values)
            connection.commit()
            return spec.decode(row)

        return await self._run(operation)

    async def _put(self, table: str, key_values: tuple, value: Any) -> None:
        spec = _TABLES[table]
        columns = (*spec.keys, *spec.extra, spec.content, "cached_at")
        placeholders = ", ".join("?" for _ in columns)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"INSERT OR REPLACE INTO {spec.name} ({', '.join(columns)})"
                f" VALUES ({placeholders})",
                (*key_values, *spec.extra_of(value), spec.encode(value), _now()),
            )
            connection.commit()

        await self._run(operation)

    async def evict_stale(self) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cutoff = (datetime.now(UTC) - _CACHE_TTL).isoformat()
            for table in _TABLES:
                connection.execute(
                    f"DELETE FROM {table} WHERE cached_at < ?", (cutoff,)
                )
            connection.commit()

        await self._run(operation)

    async def get_paper_by_url(self, source: str, url: str) -> Paper | None:
        return await self._get("papers", (source, url))

    async def get_paper_by_doi(self, source: str, doi: str) -> Paper | None:
        def operation(connection: sqlite3.Connection) -> Paper | None:
            row = connection.execute(
                "SELECT cache_key, data FROM papers WHERE source = ? AND doi = ?"
                " ORDER BY cached_at DESC LIMIT 1",
                (source, doi),
            ).fetchone()
            if row is None:
                return None
            self._touch(
                connection, "papers", self._where(_TABLES["papers"]), (source, row[0])
            )
            connection.commit()
            return _decode_paper((row[1],))

        return await self._run(operation)

    async def put_paper(self, source: str, paper: Paper) -> None:
        await self._put("papers", (source, paper.url), paper)

    async def get_papers_by_urls(
        self, source: str, urls: list[str]
    ) -> dict[str, Paper]:
        spec = _TABLES["papers"]
        select = f"SELECT {spec.content} FROM {spec.name} WHERE {self._where(spec)}"
        where = self._where(spec)

        def operation(connection: sqlite3.Connection) -> dict[str, Paper]:
            papers: dict[str, Paper] = {}
            for url in urls:
                row = connection.execute(select, (source, url)).fetchone()
                if row is None:
                    continue
                paper = spec.decode(row)
                if paper is not None:
                    self._touch(connection, spec.name, where, (source, url))
                    papers[url] = paper
            if papers:
                connection.commit()
            return papers

        return await self._run(operation) or {}

    async def get_query_result(self, cache_key: str) -> list[str] | None:
        return await self._get("query_results", (cache_key,))

    async def put_query_result(self, cache_key: str, paper_urls: list[str]) -> None:
        await self._put("query_results", (cache_key,), paper_urls)

    async def get_text(self, source: str, kind: str, doi: str) -> str | None:
        return await self._get("text_results", (source, kind, doi))

    async def put_text(self, source: str, kind: str, doi: str, content: str) -> None:
        await self._put("text_results", (source, kind, doi), content)

    async def get_pdf(self, source: str, doi: str) -> bytes | None:
        return await self._get("pdf_results", (source, doi))

    async def put_pdf(self, source: str, doi: str, content: bytes) -> None:
        await self._put("pdf_results", (source, doi), content)
