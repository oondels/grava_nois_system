"""Use-case level failures independent from external tools."""


class ApplicationError(Exception):
    """Base class for application orchestration failures."""


class NotFoundError(ApplicationError):
    """A requested application resource does not exist."""


class ConflictError(ApplicationError):
    """An operation conflicts with the current durable state."""


class DeliveryStepError(ApplicationError):
    """A delivery dependency failure with an explicit retry classification."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable
