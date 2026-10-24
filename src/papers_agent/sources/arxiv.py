import asyncio
import os
import re
import secrets
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pymupdf
from defusedxml import ElementTree as DefusedElementTree
from pydantic import ValidationError
from pymupdf.mupdf import FzErrorBase

from papers_agent.errors import (
    PaperNotFoundError,
    PaperSourceError,
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

_BASE_URL = "https://export.arxiv.org/api/query"
_OAI_BASE_URL = "https://oaipmh.arxiv.org/oai"
_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
_HTML_URL_TEMPLATE = "https://arxiv.org/html/{arxiv_id}"
_PDF_MAGIC = b"%PDF"
_REQUEST_TIMEOUT_SECONDS = 30.0
_PDF_REQUEST_TIMEOUT_SECONDS = 120.0
_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_ARXIV_NAMESPACE = "http://arxiv.org/schemas/atom"
_OAI_PMH_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
_OAI_ARXIV_NAMESPACE = "http://arxiv.org/OAI/arXiv/"
_OAI_METADATA_PREFIX = "arXiv"
_OAI_IDENTIFIER_PREFIX = "oai:arXiv.org:"
_OAI_NOT_FOUND_ERROR_CODE = "idDoesNotExist"
_ARXIV_DOI_PREFIX = "10.48550/arxiv."
_PUBLISHER = "arXiv"
_USER_AGENT = "papers-agent/0.1.0"
_ARXIV_DATE_FORMAT = "%Y%m%d%H%M"

_ARXIV_ID_PATTERN_TEXT = r"(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
_ARXIV_ID_PATTERN = re.compile(_ARXIV_ID_PATTERN_TEXT, re.IGNORECASE)
_VERSION_SUFFIX_PATTERN = re.compile(r"v\d+$", re.IGNORECASE)
_ALPHAXIV_API_URL = "https://api.alphaxiv.org"
_ALPHAXIV_DISCOVER_PATH = "/search/v2/paper/discover/embedding"
_ALPHAXIV_USER_AGENT = "papers-agent/0.1.0"
_ALPHAXIV_MAX_ATTEMPTS = 3
_ALPHAXIV_MAX_RETRY_WAIT_SECONDS = 5.0
_HTTP_TOO_MANY_REQUESTS = 429
_ABS_URL_PATTERN = re.compile(
    rf"^https?://(?:www\.)?arxiv\.org/abs/({_ARXIV_ID_PATTERN_TEXT})$",
    re.IGNORECASE,
)


def _random_email() -> str:
    return f"{secrets.token_hex(12)}@example.org"


def _retry_after_seconds(value: str | None) -> float | None:
    """Retry-After as delta-seconds; http-dates fall back to the default backoff."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _arxiv_id_from_doi(doi: Doi) -> str | None:
    normalized = doi.lower()
    if not normalized.startswith(_ARXIV_DOI_PREFIX):
        return None
    arxiv_id = normalized.removeprefix(_ARXIV_DOI_PREFIX)
    if _ARXIV_ID_PATTERN.fullmatch(arxiv_id) is None:
        return None
    return arxiv_id


def _arxiv_id_for_doi(doi: Doi) -> str:
    arxiv_id = _arxiv_id_from_doi(doi)
    if arxiv_id is None:
        raise PaperNotFoundError(f"no arxiv paper for doi {doi}")
    return arxiv_id


class _TempPdf:
    """Temporary file for a downloaded pdf, removed when the context exits."""

    def __init__(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".pdf", prefix="papers-agent-")
        os.close(fd)
        self.path = Path(name)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        self.path.unlink(missing_ok=True)


class _BibliographyExtractor(HTMLParser):
    """Collect the text of the bibliography items of an arxiv html page."""

    _BIB_ITEM_CLASS = "ltx_bibitem"
    _BIB_TAG_CLASS = "ltx_tag_bibitem"
    _SEPARATING_TAGS = frozenset(
        {"p", "li", "span", "div", "td", "br", "h1", "h2", "h3", "h4"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self._in_item = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def _classes(self, attrs: list[tuple[str, str | None]]) -> list[str]:
        for name, value in attrs:
            if name == "class" and value:
                return value.split()
        return []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "li" and self._BIB_ITEM_CLASS in classes:
            self._finish_item()
            self._in_item = True
        elif self._in_item:
            if self._BIB_TAG_CLASS in classes:
                self._skip_depth += 1
            elif tag in self._SEPARATING_TAGS:
                self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_item:
            return
        if self._skip_depth and tag == "span":
            self._skip_depth -= 1
        elif tag == "li":
            self._finish_item()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._in_item and tag in self._SEPARATING_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_item and not self._skip_depth:
            self._parts.append(data)

    def _finish_item(self) -> None:
        if self._in_item:
            text = " ".join("".join(self._parts).split())
            if text:
                self.references.append(text)
        self._in_item = False
        self._skip_depth = 0
        self._parts = []


def _extract_references(html: str) -> list[str]:
    extractor = _BibliographyExtractor()
    extractor.feed(html)
    extractor._finish_item()
    return extractor.references


def _extract_pdf_text(path: Path) -> str:
    try:
        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text() for page in doc).strip()
    except Exception as exc:
        raise PaperSourceError("failed to extract text from pdf") from exc


def _extract_pdf_images(path: Path, images_dir: Path, prefix: str) -> list[Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        doc = pymupdf.open(path)
    except FzErrorBase as exc:
        raise PaperSourceError("failed to open pdf") from exc
    with doc:
        for page_number, page in enumerate(doc, start=1):
            seen_xrefs: set[int] = set()
            for image_index, image in enumerate(page.get_images(full=True)):
                xref = image[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    pixmap = pymupdf.Pixmap(doc, xref)
                except FzErrorBase:
                    continue
                try:
                    if pixmap.n - pixmap.alpha > 3:
                        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
                    file = (
                        images_dir
                        / f"{prefix}_p{page_number:03d}_i{image_index:02d}.png"
                    )
                    pixmap.save(str(file))
                    saved.append(file)
                finally:
                    pixmap = None
    return saved


class ArxivSource(PaperSource):
    source = "arxiv"

    async def search(self, keyword: NonEmptyStr, limit: int) -> list[Paper]:
        data = await self._request(
            {
                "search_query": f"all:{keyword}",
                "max_results": str(limit),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        return [self._paper_from_entry(entry) for entry in self._entries(data)]

    async def semantic_search(self, query: NonEmptyStr, limit: int) -> list[Paper]:
        """Search arxiv semantically via the alphaxiv embedding discovery api.

        alphaxiv ranks titles/abstracts by embedding similarity over its own
        fixed server-side candidate pool, so `limit` only caps the hydrated
        results. Hits carry arxiv ids but no authors, so each hit is resolved
        against the arxiv export api (one batched id_list request) to build
        full papers, preserving alphaxiv's ranking order.
        """
        arxiv_ids = (await self._alphaxiv_semantic_ids(query))[:limit]
        if not arxiv_ids:
            return []
        papers = await self._papers_by_ids(arxiv_ids)
        return [paper for arxiv_id in arxiv_ids if (paper := papers.get(arxiv_id))]

    async def _alphaxiv_semantic_ids(self, query: str) -> list[str]:
        base_url = os.environ.get("ALPHAXIV_API_URL") or _ALPHAXIV_API_URL
        headers = {"User-Agent": _ALPHAXIV_USER_AGENT}
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for attempt in range(1, _ALPHAXIV_MAX_ATTEMPTS + 1):
                try:
                    response = await client.get(
                        f"{base_url}{_ALPHAXIV_DISCOVER_PATH}",
                        params={"q": query, "prioritize": "default"},
                        headers=headers,
                    )
                except httpx.HTTPError as exc:
                    raise PaperSourceError("alphaxiv request failed") from exc
                if response.status_code != _HTTP_TOO_MANY_REQUESTS:
                    break
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if attempt == _ALPHAXIV_MAX_ATTEMPTS or (
                    retry_after is not None
                    and retry_after > _ALPHAXIV_MAX_RETRY_WAIT_SECONDS
                ):
                    break
                await asyncio.sleep(retry_after if retry_after is not None else attempt)
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise PaperSourceError(
                "alphaxiv is rate-limiting requests "
                f"(status {_HTTP_TOO_MANY_REQUESTS}); retry later"
            )
        if response.status_code != 200:
            raise PaperSourceError(f"alphaxiv returned status {response.status_code}")
        try:
            hits = response.json()
        except ValueError as exc:
            raise PaperSourceError("invalid alphaxiv response") from exc
        if not isinstance(hits, list):
            raise PaperSourceError("invalid alphaxiv response")
        arxiv_ids: list[str] = []
        for hit in hits:
            paper_id = hit.get("paperId") if isinstance(hit, dict) else None
            if not isinstance(paper_id, str) or not paper_id:
                continue
            arxiv_id = _VERSION_SUFFIX_PATTERN.sub("", paper_id.lower())
            if _ARXIV_ID_PATTERN.fullmatch(arxiv_id) and arxiv_id not in arxiv_ids:
                arxiv_ids.append(arxiv_id)
        return arxiv_ids

    async def _papers_by_ids(self, arxiv_ids: list[str]) -> dict[str, Paper]:
        data = await self._request(
            {
                "id_list": ",".join(arxiv_ids),
                "max_results": str(len(arxiv_ids)),
            }
        )
        papers: dict[str, Paper] = {}
        for entry in self._entries(data):
            paper = self._paper_from_entry(entry)
            papers[paper.url.rsplit("/", 1)[-1]] = paper
        return papers

    async def get_papers_in_date_range(
        self, start: UTCDatetime, end: UTCDatetime, limit: int
    ) -> list[Paper]:
        data = await self._request(
            {
                "search_query": (
                    f"submittedDate:[{start.strftime(_ARXIV_DATE_FORMAT)}"
                    f" TO {end.strftime(_ARXIV_DATE_FORMAT)}]"
                ),
                "max_results": str(limit),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        return [self._paper_from_entry(entry) for entry in self._entries(data)]

    @classmethod
    def handles_url(cls, url: Url) -> bool:
        return _ABS_URL_PATTERN.fullmatch(url) is not None

    @classmethod
    def handles_doi(cls, doi: Doi) -> bool:
        normalized = doi.lower()
        if not normalized.startswith(_ARXIV_DOI_PREFIX):
            return False
        arxiv_id = normalized.removeprefix(_ARXIV_DOI_PREFIX)
        return _ARXIV_ID_PATTERN.fullmatch(arxiv_id) is not None

    async def get_paper_by_url(self, url: Url) -> Paper:
        match = _ABS_URL_PATTERN.fullmatch(url)
        if match is None:
            raise PaperNotFoundError(f"no arxiv paper for url {url}")
        return await self._get_by_id(match.group(1).lower())

    async def get_paper_by_doi(self, doi: Doi) -> Paper:
        return await self._get_by_id(_arxiv_id_for_doi(doi))

    async def download_pdf(self, doi: Doi, path: LocalPath) -> Path:
        arxiv_id = _arxiv_id_for_doi(doi)
        return await self._download_pdf_by_id(arxiv_id, Path(path))

    async def get_pdf_text(self, doi: Doi) -> str:
        arxiv_id = _arxiv_id_for_doi(doi)
        with self._temp_pdf() as tmp_path:
            await self._download_pdf_by_id(arxiv_id, tmp_path)
            return await asyncio.to_thread(_extract_pdf_text, tmp_path)

    async def get_references(self, doi: Doi) -> list[str]:
        """Return the bibliography entries of the given paper as raw strings.

        Parses the html rendering arxiv generates for recent submissions.
        Papers without an html version (older submissions or pdf-only
        uploads) raise a PaperSourceError.
        """
        arxiv_id = _VERSION_SUFFIX_PATTERN.sub("", _arxiv_id_for_doi(doi).lower())
        url = _HTML_URL_TEMPLATE.format(arxiv_id=arxiv_id)
        headers = {"User-Agent": f"{_USER_AGENT} (mailto:{_random_email()})"}
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise PaperSourceError("arxiv html request failed") from exc
        if response.status_code == 404:
            raise PaperSourceError(f"arxiv has no html version for paper {arxiv_id}")
        if response.status_code != 200:
            raise PaperSourceError(f"arxiv returned status {response.status_code}")
        references = await asyncio.to_thread(_extract_references, response.text)
        if not references:
            raise PaperSourceError(
                f"no bibliography found in the arxiv html version of {arxiv_id}"
            )
        return references

    async def get_pdf_images(self, doi: Doi, path: LocalPath) -> list[Path]:
        arxiv_id = _arxiv_id_for_doi(doi)
        images_dir = Path(path)
        prefix = arxiv_id.replace("/", "-")
        with self._temp_pdf() as tmp_path:
            await self._download_pdf_by_id(arxiv_id, tmp_path)
            return await asyncio.to_thread(
                _extract_pdf_images, tmp_path, images_dir, prefix
            )

    @staticmethod
    def _temp_pdf() -> _TempPdf:
        return _TempPdf()

    async def _download_pdf_by_id(self, arxiv_id: str, path: Path) -> Path:
        url = _PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(6)}.part")
        complete = False
        try:
            headers = {"User-Agent": f"{_USER_AGENT} (mailto:{_random_email()})"}
            async with httpx.AsyncClient(
                timeout=_PDF_REQUEST_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                try:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code != 200:
                            raise PaperSourceError(
                                f"arxiv returned status {response.status_code}"
                            )
                        with tmp_path.open("wb") as fh:
                            async for chunk in response.aiter_bytes():
                                fh.write(chunk)
                except httpx.HTTPError as exc:
                    raise PaperSourceError("arxiv pdf request failed") from exc
            with tmp_path.open("rb") as fh:
                magic = fh.read(len(_PDF_MAGIC))
            if magic != _PDF_MAGIC:
                raise PaperSourceError(f"arxiv did not return a pdf for {url}")
            tmp_path.replace(path)
            complete = True
            return path
        finally:
            if not complete:
                tmp_path.unlink(missing_ok=True)

    async def _get_by_id(self, arxiv_id: str) -> Paper:
        base_id = _VERSION_SUFFIX_PATTERN.sub("", arxiv_id.lower())
        data = await self._oai_request(
            {
                "verb": "GetRecord",
                "identifier": f"{_OAI_IDENTIFIER_PREFIX}{base_id}",
                "metadataPrefix": _OAI_METADATA_PREFIX,
            }
        )
        metadata = self._oai_metadata(data, base_id)
        return self._paper_from_oai_metadata(metadata, base_id)

    async def _request(self, params: dict[str, str]) -> str:
        return await self._get(_BASE_URL, params)

    async def _oai_request(self, params: dict[str, str]) -> str:
        return await self._get(_OAI_BASE_URL, params)

    async def _get(self, url: str, params: dict[str, str]) -> str:
        headers = {"User-Agent": f"{_USER_AGENT} (mailto:{_random_email()})"}
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise PaperSourceError("arxiv request failed") from exc
        if response.status_code != 200:
            raise PaperSourceError(f"arxiv returned status {response.status_code}")
        return response.text

    def _oai_metadata(self, data: str, base_id: str) -> ET.Element:
        root = self._parse(data)
        error = root.find(f"{{{_OAI_PMH_NAMESPACE}}}error")
        if error is not None:
            code = error.get("code") or "unknown"
            if code == _OAI_NOT_FOUND_ERROR_CODE:
                raise PaperNotFoundError(f"no arxiv paper for id {base_id}")
            raise PaperSourceError(
                f"arxiv oai error {code}: {self._element_text(error)}"
            )
        path = (
            f"{{{_OAI_PMH_NAMESPACE}}}GetRecord/"
            f"{{{_OAI_PMH_NAMESPACE}}}record/"
            f"{{{_OAI_PMH_NAMESPACE}}}metadata/"
            f"{{{_OAI_ARXIV_NAMESPACE}}}arXiv"
        )
        metadata = root.find(path)
        if metadata is None:
            raise PaperSourceError(f"missing arxiv oai metadata for id {base_id}")
        return metadata

    def _parse(self, data: str) -> ET.Element:
        try:
            return DefusedElementTree.fromstring(data)
        except ET.ParseError as exc:
            raise PaperSourceError("invalid arxiv response") from exc

    def _entries(self, data: str) -> list[ET.Element]:
        return self._parse(data).findall(f"{{{_ATOM_NAMESPACE}}}entry")

    def _paper_from_oai_metadata(self, metadata: ET.Element, base_id: str) -> Paper:
        record_id = self._required_text(
            metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}id"
        ).lower()
        if record_id != base_id:
            raise PaperSourceError(
                f"arxiv oai returned id {record_id} for requested {base_id}"
            )
        doi = (
            self._optional_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}doi")
            or f"{_ARXIV_DOI_PREFIX}{base_id}"
        )
        created = self._optional_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}created")
        try:
            return Paper(
                source="arxiv",
                name=self._required_text(metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}title"),
                url=f"https://arxiv.org/abs/{base_id}",
                doi=doi,
                authors=self._oai_authors(metadata),
                publisher=_PUBLISHER,
                abstract=self._required_text(
                    metadata, f"{{{_OAI_ARXIV_NAMESPACE}}}abstract"
                ),
                published_at=self._parse_oai_date(created),
            )
        except ValidationError as exc:
            raise PaperSourceError("invalid arxiv entry data") from exc

    def _oai_authors(self, metadata: ET.Element) -> list[str]:
        authors = []
        for author in metadata.findall(
            f"{{{_OAI_ARXIV_NAMESPACE}}}authors/{{{_OAI_ARXIV_NAMESPACE}}}author"
        ):
            keyname = self._required_text(author, f"{{{_OAI_ARXIV_NAMESPACE}}}keyname")
            forenames = self._optional_text(
                author, f"{{{_OAI_ARXIV_NAMESPACE}}}forenames"
            )
            suffix = self._optional_text(author, f"{{{_OAI_ARXIV_NAMESPACE}}}suffix")
            authors.append(
                " ".join(
                    part for part in (forenames, keyname, suffix) if part is not None
                )
            )
        return authors

    @staticmethod
    def _parse_oai_date(value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value).replace(tzinfo=UTC)

    def _paper_from_entry(self, entry: ET.Element) -> Paper:
        entry_id = self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}id")
        match = _ABS_URL_PATTERN.fullmatch(entry_id)
        if match is None:
            raise PaperSourceError(f"unexpected arxiv entry id {entry_id}")
        arxiv_id = match.group(1).lower()
        base_id = _VERSION_SUFFIX_PATTERN.sub("", arxiv_id)
        doi = self._optional_text(entry, f"{{{_ARXIV_NAMESPACE}}}doi") or (
            f"{_ARXIV_DOI_PREFIX}{base_id}"
        )
        try:
            return Paper(
                source="arxiv",
                name=self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}title"),
                url=f"https://arxiv.org/abs/{base_id}",
                doi=doi,
                authors=[
                    author_name
                    for author in entry.findall(f"{{{_ATOM_NAMESPACE}}}author")
                    if (
                        author_name := self._optional_text(
                            author, f"{{{_ATOM_NAMESPACE}}}name"
                        )
                    )
                ],
                publisher=_PUBLISHER,
                abstract=self._required_text(entry, f"{{{_ATOM_NAMESPACE}}}summary"),
                published_at=self._required_text(
                    entry, f"{{{_ATOM_NAMESPACE}}}published"
                ),
            )
        except ValidationError as exc:
            raise PaperSourceError("invalid arxiv entry data") from exc

    @staticmethod
    def _element_text(node: ET.Element) -> str:
        return " ".join(node.text.split()) if node.text else ""

    @staticmethod
    def _required_text(node: ET.Element, path: str) -> str:
        child = node.find(path)
        if child is None or child.text is None:
            raise PaperSourceError(f"missing {path} in arxiv entry")
        return " ".join(child.text.split())

    @staticmethod
    def _optional_text(node: ET.Element, path: str) -> str | None:
        child = node.find(path)
        if child is None or child.text is None:
            return None
        text = " ".join(child.text.split())
        return text or None
