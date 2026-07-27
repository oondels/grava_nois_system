"""Static dependency rules for the Clean Architecture layers.

The checks intentionally parse source instead of importing it. This keeps the
architecture gate independent from optional hardware and runtime dependencies.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

_DOMAIN_FORBIDDEN_PREFIXES = (
    "src.application",
    "src.infrastructure",
    "src.bootstrap",
)
_APPLICATION_FORBIDDEN_PREFIXES = (
    "src.infrastructure",
    "src.bootstrap",
)
_EXTERNAL_TOOL_PREFIXES = (
    "paho",
    "requests",
    "dotenv",
    "cryptography",
    "pigpio",
    "RPi",
)


def _python_files(layer: str) -> list[Path]:
    root = SRC_ROOT / layer
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def _violations(
    layer: str,
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    violations: list[str] = []
    for path in _python_files(layer):
        for line, imported_module in _imports(path):
            if imported_module.startswith(forbidden_prefixes):
                relative_path = path.relative_to(PROJECT_ROOT)
                violations.append(f"{relative_path}:{line} imports {imported_module}")
    return violations


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_domain_does_not_depend_on_outer_layers_or_external_tools(self) -> None:
        violations = _violations(
            "domain",
            _DOMAIN_FORBIDDEN_PREFIXES + _EXTERNAL_TOOL_PREFIXES,
        )
        self.assertEqual([], violations, "\n" + "\n".join(violations))

    def test_application_does_not_depend_on_infrastructure_or_external_tools(self) -> None:
        violations = _violations(
            "application",
            _APPLICATION_FORBIDDEN_PREFIXES + _EXTERNAL_TOOL_PREFIXES,
        )
        self.assertEqual([], violations, "\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
