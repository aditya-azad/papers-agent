import argparse
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

_BATCH_SIZE = 50_000
_PROGRESS_EVERY = 200_000

_DUMP_DB_NAME = "metadata.sqlite"

_SCHEMA = """
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
"""

_INSERT = (
    "INSERT OR REPLACE INTO papers"
    " (id, submitter, authors, title, comments, journal_ref, doi,"
    "  report_no, categories, license, abstract, authors_parsed,"
    "  versions, update_date)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _text(record: dict, key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def _json_text(value: object) -> str | None:
    return json.dumps(value) if value else None


def _row(record: dict) -> tuple:
    return (
        _text(record, "id"),
        _text(record, "submitter"),
        _text(record, "authors"),
        _text(record, "title"),
        _text(record, "comments"),
        _text(record, "journal-ref"),
        _text(record, "doi"),
        _text(record, "report-no"),
        _text(record, "categories"),
        _text(record, "license"),
        _text(record, "abstract"),
        _json_text(record.get("authors_parsed")),
        _json_text(record.get("versions")),
        _text(record, "update_date"),
    )


def _open_jsonl(path: Path) -> IO[bytes]:
    if path.suffix == ".zip":
        archive = zipfile.ZipFile(path)
        member = next(name for name in archive.namelist() if name.endswith(".json"))
        return archive.open(member)
    return path.open("rb")


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with _open_jsonl(path) as raw:
        for line in raw:
            line = line.strip()
            if line:
                yield json.loads(line)


def convert(input_path: Path, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(output_path)
    try:
        conn.execute(_SCHEMA)
        total = 0
        batch: list[tuple] = []
        for record in _iter_jsonl(input_path):
            batch.append(_row(record))
            if len(batch) >= _BATCH_SIZE:
                conn.executemany(_INSERT, batch)
                total += len(batch)
                batch = []
                if total % _PROGRESS_EVERY < _BATCH_SIZE:
                    print(f"{total} records imported", flush=True)
        if batch:
            conn.executemany(_INSERT, batch)
            total += len(batch)
        conn.commit()
    finally:
        conn.close()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import the Kaggle arXiv metadata snapshot"
            " (arxiv-metadata-oai-snapshot.json) into the papers-agent"
            " dump sqlite database"
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="path to the Kaggle archive.zip or the extracted .json file",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="directory to write metadata.sqlite into",
    )
    args = parser.parse_args()
    if not args.input.is_file():
        raise SystemExit(f"input file not found: {args.input}")
    output_path = args.output_dir / _DUMP_DB_NAME
    total = convert(args.input, output_path)
    print(f"done: {total} records written to {output_path}")


if __name__ == "__main__":
    main()
