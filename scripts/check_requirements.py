"""Exit 1 unless requirements.txt (what Wispbyte pip-installs) lists the same packages and bounds as
[project.dependencies]."""

import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def _canonical(req: Requirement) -> str:
    extras = f"[{','.join(sorted(canonicalize_name(e) for e in req.extras))}]" if req.extras else ""
    target = f" @ {req.url}" if req.url else str(req.specifier)
    marker = f"; {req.marker}" if req.marker else ""
    return f"{canonicalize_name(req.name)}{extras}{target}{marker}"


def _parse(lines: list[str], source: str) -> tuple[dict[str, str], list[str]]:
    reqs: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(lines, 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        if line.startswith("-"):
            problems.append(f"{source} {number}: pip option '{line}' has no pyproject equivalent")
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement as e:
            problems.append(f"{source} {number}: invalid requirement '{line}': {e}")
            continue
        name = canonicalize_name(req.name)
        if name in reqs:
            problems.append(f"{source} {number}: {req.name} is listed twice")
        reqs[name] = _canonical(req)
    return reqs, problems


def compare(dependencies: list[str], requirement_lines: list[str]) -> list[str]:
    expected, problems = _parse(dependencies, "[project.dependencies] entry")
    actual, req_problems = _parse(requirement_lines, "requirements.txt line")
    problems += req_problems
    for name in sorted(expected.keys() | actual.keys()):
        if name not in actual:
            problems.append(f"missing from requirements.txt: {expected[name]}")
        elif name not in expected:
            problems.append(f"not in [project.dependencies]: {actual[name]}")
        elif expected[name] != actual[name]:
            problems.append(f"differs: pyproject.toml has {expected[name]}, requirements.txt has {actual[name]}")
    return problems


def check(root: Path = ROOT) -> list[str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject.get("project", {}).get("dependencies", [])
    lines = (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return compare(dependencies, lines)


def main(root: Path = ROOT) -> int:
    problems = check(root)
    if problems:
        print("requirements.txt does not mirror [project.dependencies]:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("requirements.txt mirrors [project.dependencies]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
