import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"infra_{name}", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_requirements = _load("check_requirements")

DEPS = ["aiohttp>=3.14.4", "discord-py>=2.7.1", "uvicorn[standard]>=0.54.0"]


def test_repo_requirements_txt_mirrors_pyproject():
    assert check_requirements.check() == []


def test_identical_lists_pass_in_any_order_with_comments():
    lines = ["# runtime deps", "", "uvicorn[standard]>=0.54.0  # server", "discord-py>=2.7.1", "aiohttp>=3.14.4"]
    assert check_requirements.compare(DEPS, lines) == []


def test_equivalent_spellings_pass():
    lines = ["AIOHTTP >= 3.14.4", "discord.py>=2.7.1", "Uvicorn[Standard]>=0.54.0"]
    assert check_requirements.compare(DEPS, lines) == []


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["aiohttp>=3.14.4", "discord-py>=2.7.1"], "missing from requirements.txt: uvicorn[standard]>=0.54.0"),
        ([*DEPS, "httpx>=0.28.1"], "not in [project.dependencies]: httpx>=0.28.1"),
        (
            ["aiohttp>=3.14.3", "discord-py>=2.7.1", "uvicorn[standard]>=0.54.0"],
            "differs: pyproject.toml has aiohttp>=3.14.4",
        ),
        (["aiohttp>=3.14.4", "discord-py>=2.7.1", "uvicorn>=0.54.0"], "differs: pyproject.toml has uvicorn[standard]"),
        ([*DEPS, "aiohttp>=3.14.4"], "aiohttp is listed twice"),
        ([*DEPS, "--extra-index-url https://example.com"], "pip option"),
        ([*DEPS, "not a requirement!"], "invalid requirement"),
    ],
)
def test_differences_are_reported(lines, expected):
    problems = check_requirements.compare(DEPS, lines)
    assert any(expected in problem for problem in problems), problems


def test_main_exit_codes(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["aiohttp>=3.14.4"]\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.14.4\n", encoding="utf-8")
    assert check_requirements.main(tmp_path) == 0

    (tmp_path / "requirements.txt").write_text("aiohttp>=3.14.3\n", encoding="utf-8")
    assert check_requirements.main(tmp_path) == 1
    assert "differs" in capsys.readouterr().err
