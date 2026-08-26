"""The policy kernel must never be able to reach a model.

This is the structural enforcement of the project's headline invariant. It is not a style check:
it walks the transitive import closure of every module in ``app.kernel`` by parsing source, and
fails if any banned module is reachable from any path. Wiring a model client into the kernel for
convenience breaks the build here rather than at review time.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
KERNEL_ROOT = APP_ROOT / "kernel"

# Any model client, and any general-purpose network client that could reach one.
BANNED_PREFIXES: tuple[str, ...] = (
    "app.semantic",
    "app.escalation",
    # The buyer's agent is model-driven on purpose. It is the other side of the counter and must
    # stay there: a money decision must never be able to reach it.
    "app.buyer",
    "app.notify",
    # Agent-facing surfaces. The kernel decides about the traffic these carry; it must not be able
    # to reach them, in either direction.
    "app.mcp",
    "interop",
    "scenarios",
    "google",
    "google.genai",
    "google.generativeai",
    "openai",
    "anthropic",
    "langchain",
    "langchain_core",
    "langchain_google_genai",
    "litellm",
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "socket",
    "http.client",
    "razorpay",
)


def _module_name(path: Path) -> str:
    relative = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _path_for(module: str) -> Path | None:
    if not module.startswith("app"):
        return None
    relative = Path(*module.split(".")[1:])
    candidate = APP_ROOT / relative.with_suffix(".py")
    if candidate.exists():
        return candidate
    package = APP_ROOT / relative / "__init__.py"
    return package if package.exists() else None


def _imports_of(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                target = base[: len(base) - node.level + 1]
                prefix = ".".join([*target, node.module] if node.module else target)
            else:
                prefix = node.module or ""
            if prefix:
                yield prefix
                for alias in node.names:
                    yield f"{prefix}.{alias.name}"


def _closure(entry_points: list[Path]) -> dict[str, list[str]]:
    """Map every reachable module to the chain of modules that reached it."""
    reached: dict[str, list[str]] = {}
    queue: list[tuple[Path, list[str]]] = [(p, [_module_name(p)]) for p in entry_points]
    while queue:
        path, chain = queue.pop()
        for imported in _imports_of(path):
            if imported in reached:
                continue
            reached[imported] = chain
            child = _path_for(imported)
            if child is not None:
                queue.append((child, [*chain, imported]))
    return reached


def test_kernel_package_exists() -> None:
    modules = sorted(KERNEL_ROOT.glob("*.py"))
    assert modules, "the kernel package must contain modules"


def test_kernel_cannot_reach_any_model_client() -> None:
    modules = sorted(KERNEL_ROOT.glob("*.py"))
    reached = _closure(modules)

    violations: list[str] = []
    for module, chain in sorted(reached.items()):
        for banned in BANNED_PREFIXES:
            if module == banned or module.startswith(banned + "."):
                violations.append(f"{banned} reachable as {module} via {' -> '.join(chain)}")
    assert not violations, "the policy kernel must not be able to reach a model:\n" + "\n".join(
        violations
    )


def test_guard_detects_a_planted_violation(tmp_path: Path) -> None:
    """The guard is only worth having if it actually fails when the invariant is broken."""
    planted = KERNEL_ROOT / "_isolation_probe.py"
    planted.write_text("from google import genai\n", encoding="utf-8")
    try:
        reached = _closure([planted])
        assert any(m.startswith("google") for m in reached), (
            "the import walker failed to see a direct model import, so it proves nothing"
        )
    finally:
        planted.unlink()


def test_kernel_reaches_its_legitimate_dependencies() -> None:
    """A guard that sees nothing at all would also pass, so confirm it resolves real edges."""
    reached = _closure(sorted(KERNEL_ROOT.glob("*.py")))
    assert "app.db.models" in reached
    assert "app.ap2.constraints" in reached


@pytest.mark.parametrize(
    "banned", ["google.genai", "httpx", "app.semantic", "app.buyer", "app.mcp", "interop"]
)
def test_specific_clients_are_unreachable(banned: str) -> None:
    reached = _closure(sorted(KERNEL_ROOT.glob("*.py")))
    assert banned not in reached, f"{banned} must not be reachable from the policy kernel"
