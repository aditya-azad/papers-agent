from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, HttpUrl


def _ensure_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fix_spacing(value: str) -> str:
    return " ".join(value.split()).strip()


def _require_non_empty(value: str) -> str:
    if not value:
        raise ValueError("value must not be empty")
    return value


def _validate_url(value: str) -> str:
    return str(HttpUrl(value)).rstrip("/")


ProperWhitespacedStr = Annotated[str, BeforeValidator(_fix_spacing)]
UTCDatetime = Annotated[datetime, BeforeValidator(_ensure_utc)]
NonEmptyStr = Annotated[ProperWhitespacedStr, AfterValidator(_require_non_empty)]
Url = Annotated[NonEmptyStr, AfterValidator(_validate_url)]
