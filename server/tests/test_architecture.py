"""The dependency rule: source code dependencies point inward only.

  domain  <-  application  <-  infrastructure, interfaces  <-  main
"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
FRAMEWORKS = {"sqlalchemy", "fastapi", "aiogram", "aiohttp", "openpyxl", "cryptography", "apscheduler", "httpx",
              "starlette", "uvicorn", "psycopg"}
# what each layer may import from inside the app
ALLOWED = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "infrastructure": {"domain", "application", "infrastructure"},
    "interfaces": {"domain", "application", "interfaces", "config"},
}


def imports(path: Path):
    """(app layer or None, top-level external package or None) for every import in a file."""
    tree = ast.parse(path.read_text())
    package = path.relative_to(APP).parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield None, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                top = (node.module or "").split(".")[0]
                yield ("app-absolute", top) if top == "app" else (None, top)
            else:
                base = list(package[: len(package) - node.level + 1])
                target = base + (node.module.split(".") if node.module else [])
                yield (target[0] if target else "app-root"), None


def check(layer: str):
    problems = []
    for path in (APP / layer).rglob("*.py"):
        for inner, external in imports(path):
            rel = path.relative_to(APP)
            if inner == "app-absolute":
                problems.append(f"{rel}: use relative imports inside the app")
            elif inner is not None and inner not in ALLOWED[layer]:
                problems.append(f"{rel}: {layer} must not import {inner}")
            elif external in FRAMEWORKS and layer in ("domain", "application"):
                problems.append(f"{rel}: {layer} must not import {external}")
    return problems


def test_domain_depends_on_nothing():
    assert check("domain") == []


def test_application_depends_only_on_domain():
    assert check("application") == []


def test_adapters_point_inward():
    assert check("infrastructure") == []
    assert check("interfaces") == []


def test_interfaces_do_not_use_infrastructure():
    """Controllers reach the outside world only through use cases."""
    bad = [str(p.relative_to(APP)) for p in (APP / "interfaces").rglob("*.py")
           if any(inner == "infrastructure" for inner, _ in imports(p))]
    assert bad == []
