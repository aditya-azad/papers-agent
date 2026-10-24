class PaperSourceError(Exception):
    """Raised when a paper source API call fails."""


class PaperNotFoundError(PaperSourceError):
    """Raised when no paper matches the requested url or doi."""
