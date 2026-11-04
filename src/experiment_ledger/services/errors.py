"""Domain errors. The API layer maps these to HTTP status codes in one place."""


class DomainError(Exception):
    """Base class; ``message`` is always safe to show to API clients."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidState(DomainError):
    pass
