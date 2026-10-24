import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    model_validator,
)

from reference_agent.types import NonEmptyStr, Url, UTCDatetime

PaperSourceName = Literal["arxiv", "pubmed", "biorxiv", "openalex"]

MAX_PAPER_NAME_LENGTH = 255
MAX_PAPER_DESCRIPTION_LENGTH = 4096

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f]")


def _strip_control_characters(value: str) -> str:
    return _CONTROL_CHARACTERS.sub("", value)


def _normalize_doi(value: str) -> str:
    normalized = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    return normalized


PaperAuthorName = Annotated[
    NonEmptyStr,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_NAME_LENGTH),
]

PaperPublisher = Annotated[
    str,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_NAME_LENGTH),
]

PaperDescription = Annotated[
    str,
    BeforeValidator(_strip_control_characters),
    StringConstraints(max_length=MAX_PAPER_DESCRIPTION_LENGTH),
]


def _dedupe_authors(
    value: list[PaperAuthorName],
) -> list[PaperAuthorName]:
    seen: set[str] = set()
    deduplicated: list[PaperAuthorName] = []
    for author in value:
        if author not in seen:
            seen.add(author)
            deduplicated.append(author)
    return deduplicated


PaperAuthors = Annotated[list[PaperAuthorName], BeforeValidator(_dedupe_authors)]

Doi = Annotated[
    NonEmptyStr,
    BeforeValidator(_normalize_doi),
    Field(pattern=r"^10\.\d{4,9}/\S+$"),
]


class PaperSearchQuery(BaseModel):
    text: NonEmptyStr
    limit: int = Field(ge=1, le=100)


class PaperDateRangeQuery(BaseModel):
    start: UTCDatetime
    end: UTCDatetime
    limit: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _check_order(self) -> PaperDateRangeQuery:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Paper(BaseModel):
    source: PaperSourceName
    name: str
    url: Url
    doi: Doi
    authors: PaperAuthors
    publisher: PaperPublisher
    description: PaperDescription
    published_at: UTCDatetime | None
