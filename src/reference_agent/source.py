from abc import ABC, abstractmethod
from typing import ClassVar

from reference_agent.sources.schemas import (
    Doi,
    Paper,
    PaperAuthors,
    PaperDateRangeQuery,
    PaperDescription,
    PaperSearchQuery,
    PaperSourceName,
)
from reference_agent.types import Url


class PaperSource(ABC):
    source: ClassVar[PaperSourceName]

    @abstractmethod
    async def search(
        self, query: PaperSearchQuery
    ) -> list[Paper]: ...

    @abstractmethod
    async def get_papers_in_date_range(
        self, query: PaperDateRangeQuery
    ) -> list[Paper]: ...

    @abstractmethod
    async def get_paper_by_url(self, url: Url) -> Paper: ...

    @abstractmethod
    async def get_paper_by_doi(self, doi: Doi) -> Paper: ...

    async def get_authors(self, doi: Doi) -> PaperAuthors:
        return (await self.get_paper_by_doi(doi)).authors

    async def get_description(self, doi: Doi) -> PaperDescription:
        return (await self.get_paper_by_doi(doi)).description
