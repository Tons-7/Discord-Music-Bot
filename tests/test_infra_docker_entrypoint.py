import contextlib
import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("infra_docker_entrypoint", ROOT / "scripts" / "docker_entrypoint.py")
assert _spec and _spec.loader
entrypoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entrypoint)


@pytest.fixture
def dirs(tmp_path):
    data, legacy = tmp_path / "data", tmp_path / "legacy"
    data.mkdir()
    legacy.mkdir()
    return data, data / "music_bot.db", legacy


def make_db(path: Path, *values: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    conn.commit()
    return conn


def rows(path: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return [v for (v,) in conn.execute("SELECT v FROM t ORDER BY rowid")]


def test_imports_legacy_database_once(dirs):
    data, db, legacy = dirs
    make_db(legacy / "music_bot.db", "a", "b").close()

    entrypoint.migrate(data, db, legacy)
    assert rows(db) == ["a", "b"]
    assert "imported" in (data / entrypoint.MARKER).read_text(encoding="utf-8")
    assert not list(data.glob(".import-*")) and not list(data.glob("*.importing"))

    make_db(legacy / "music_bot.db", "c").close()
    entrypoint.migrate(data, db, legacy)
    assert rows(db) == ["a", "b"]


def test_import_includes_uncheckpointed_wal(dirs):
    data, db, legacy = dirs
    conn = make_db(legacy / "music_bot.db", "checkpointed")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO t VALUES ('only-in-wal')")
    conn.commit()
    try:
        assert (legacy / "music_bot.db-wal").stat().st_size > 0
        entrypoint.migrate(data, db, legacy)
    finally:
        conn.close()
    assert rows(db) == ["checkpointed", "only-in-wal"]


def test_existing_database_is_kept(dirs):
    data, db, legacy = dirs
    make_db(db, "current").close()
    make_db(legacy / "music_bot.db", "old").close()

    entrypoint.migrate(data, db, legacy)
    assert rows(db) == ["current"]
    assert "kept" in (data / entrypoint.MARKER).read_text(encoding="utf-8")


@pytest.mark.parametrize("layout", ["missing", "empty file", "directory"])
def test_nothing_to_import(dirs, layout):
    data, db, legacy = dirs
    if layout == "empty file":
        (legacy / "music_bot.db").touch()
    elif layout == "directory":
        (legacy / "music_bot.db").mkdir()

    entrypoint.migrate(data, db, legacy)
    assert not db.exists()
    assert (data / entrypoint.MARKER).read_text(encoding="utf-8") == "nothing to import\n"


def test_corrupt_legacy_database_refuses_to_start(dirs):
    data, db, legacy = dirs
    (legacy / "music_bot.db").write_bytes(b"not a database" * 100)

    with pytest.raises(entrypoint.MigrationError):
        entrypoint.migrate(data, db, legacy)
    assert not db.exists()
    assert not (data / entrypoint.MARKER).exists()


def test_unreadable_legacy_database_refuses_to_start(dirs, monkeypatch):
    data, db, legacy = dirs
    make_db(legacy / "music_bot.db", "a").close()

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(entrypoint.shutil, "copyfile", denied)
    with pytest.raises(entrypoint.MigrationError):
        entrypoint.migrate(data, db, legacy)
    assert not db.exists()
    assert not (data / entrypoint.MARKER).exists()


def test_copies_only_finished_cache_files(dirs):
    data, db, legacy = dirs
    cache = legacy / "audio_cache"
    (cache / "activity").mkdir(parents=True)
    (cache / "song.opus").write_bytes(b"voice")
    (cache / "song.webm.part").write_bytes(b"partial")
    (cache / "activity" / "song.m4a").write_bytes(b"activity")
    (cache / "activity" / "song.webm").write_bytes(b"intermediate")
    target = data / "audio_cache"
    target.mkdir()
    (target / "kept.opus").write_bytes(b"new")
    (cache / "kept.opus").write_bytes(b"old")
    (target / "song.opus.importing").write_bytes(b"leftover")

    entrypoint.migrate(data, db, legacy)
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == [
        "activity/song.m4a",
        "kept.opus",
        "song.opus",
    ]
    assert (target / "kept.opus").read_bytes() == b"new"
    assert "copied 2 cached songs" in (data / entrypoint.MARKER).read_text(encoding="utf-8")


def test_warns_without_legacy_mount(dirs, capsys):
    data, db, legacy = dirs
    shutil.rmtree(legacy)

    entrypoint.migrate(data, db, legacy)
    assert "will create an empty database" in capsys.readouterr().err
    assert not (data / entrypoint.MARKER).exists()


def test_main_migrates_then_execs(dirs, monkeypatch):
    data, db, legacy = dirs
    make_db(legacy / "music_bot.db", "a").close()
    monkeypatch.chdir(data)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LEGACY_DIR", str(legacy))
    calls = []
    monkeypatch.setattr(entrypoint.os, "execvp", lambda file, args: calls.append((file, args)))

    entrypoint.main(["python", "/app/main.py"])
    assert calls == [("python", ["python", "/app/main.py"])]
    assert rows(db) == ["a"]


def test_main_does_not_exec_when_import_fails(dirs, monkeypatch):
    data, db, legacy = dirs
    (legacy / "music_bot.db").write_bytes(b"garbage" * 100)
    monkeypatch.chdir(data)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("LEGACY_DIR", str(legacy))
    monkeypatch.setattr(entrypoint.os, "execvp", lambda *args: pytest.fail("exec after a failed import"))

    assert entrypoint.main(["python", "/app/main.py"]) == 1
    assert entrypoint.main([]) == 2
