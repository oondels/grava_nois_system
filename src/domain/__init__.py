"""Pure business concepts for the edge replay system.

This package must not depend on application or infrastructure modules.
"""

from .exceptions import DomainError, InvalidStateTransition, InvariantViolation

__all__ = ["DomainError", "InvalidStateTransition", "InvariantViolation"]
