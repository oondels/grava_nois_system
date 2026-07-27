"""Exceptions raised when domain invariants are violated."""


class DomainError(Exception):
    """Base class for business-rule failures."""


class InvariantViolation(DomainError, ValueError):
    """A value or operation violates a domain invariant."""


class InvalidStateTransition(DomainError):
    """A state transition is not allowed by the aggregate."""
