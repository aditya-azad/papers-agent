import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from papers_agent.errors import PaperNotFoundError, PaperSourceError
from papers_agent.sources.arxiv_paper import (
    ARXIV_DOI_PREFIX,
    arxiv_id_from_doi,
    arxiv_id_from_url,
    paper_from_arxiv_fields,
)
from papers_agent.sources.arxiv_sync import (
    Syncer,
    _connect,
    _published_at,
)
from papers_agent.types import (
    Doi,
    LocalPath,
    NonEmptyStr,
    Paper,
    PaperSource,
    Url,
    UTCDatetime,
)

_BACKUP_DB_ENV = "PAPERS_AGENT_ARXIV_BACKUP_DB"

_CHUNK = 100

_PAPER_ROW_COLUMNS = "id, title, authors_parsed, versions, update_date, doi, abstract"
_PAPER_ROW_COLUMNS_PREFIXED = (
    "p.id, p.title, p.authors_parsed, p.versions,"
    " p.update_date, p.doi, p.abstract"
)
_PAPER_ROW_QUERY = (
    f"SELECT {_PAPER_ROW_COLUMNS} FROM papers WHERE id = ?"
)


def _default_backup_db() -> Path:
    env = os.environ.get(_BACKUP_DB_ENV)
    if env:
        return Path(env)
    data_home = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(data_home).expanduser() / "papers-agent" / "arxiv" / "metadata.sqlite"


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
        if len(parts) >= 2:
            last, forenames, *suffix = parts
            parts = [forenames, last, *suffix]
        name = " ".join(parts)
        if name:
            authors.append(name)
    return authors


def _fts_match_query(keyword: str) -> str:
    tokens = keyword.split()
    return " ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
    )


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


class ArxivBackupSource(PaperSource):
    source = "arxiv"

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = (
            db_path
            if db_path is not None
            else _default_backup_db()
        )
        self._syncer = Syncer(self._db_path)

    @property
    def syncer(self) -> Syncer:
        return self._syncer

    def prepare(self) -> None:
        self._syncer.ensure_started()

    @classmethod
    def handles_url(cls, url: Url) -> bool:
        return arxiv_id_from_url(url) is not None

    @classmethod
    def doi_for_url(cls, url: Url) -> Doi:
        arxiv_id = arxiv_id_from_url(url)
        if arxiv_id is None:
            raise PaperNotFoundError(f"no arxiv paper for url {url}")
        return f"{ARXIV_DOI_PREFIX}{arxiv_id}"

    @classmethod
    def handles_doi(cls, doi: Doi) -> bool:
        return arxiv_id_from_doi(doi) is not None

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper] | None:
        return await asyncio.to_thread(self._search_sync, keyword, limit)

    async def papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        return await asyncio.to_thread(self._papers_by_ids_sync, arxiv_ids)

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper] | None:
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

    async def download_pdf(self, doi: Doi, directory: LocalPath) -> Path:
        raise PaperSourceError(
            "ArxivBackupSource does not support pdf downloads"
        )

    async def get_pdf_text(self, doi: Doi) -> str:
        raise PaperSourceError(
            "ArxivBackupSource does not support pdf text extraction"
        )

    async def get_pdf_images(
        self, doi: Doi, directory: LocalPath
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
                        f"SELECT {_PAPER_ROW_COLUMNS}"
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
            f"SELECT {_PAPER_ROW_COLUMNS_PREFIXED}"
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
            f"SELECT {_PAPER_ROW_COLUMNS_PREFIXED}"
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


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--sync":
        Syncer(Path(sys.argv[2])).run_sync()
        return
    raise SystemExit(
        "usage: python -m papers_agent.sources.arxiv_backup --sync DB"
    )


if __name__ == "__main__":
    main()
