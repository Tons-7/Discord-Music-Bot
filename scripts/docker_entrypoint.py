"""Docker entrypoint: import data from the old single-file layout into /data once, then exec the command."""

import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

DB_NAME = "music_bot.db"
MARKER = ".legacy-imported"
# The only complete files the old cache layout held; anything else there is a partial download.
CACHE_FILES = {".": ".opus", "activity": ".m4a"}


class MigrationError(Exception):
    pass


def log(message: str) -> None:
    print(f"[entrypoint] {message}", file=sys.stderr, flush=True)


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise MigrationError(f"cannot read {path}: {e}") from e


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_database(src: Path, dst: Path) -> None:
    """Copy src (with any WAL or journal beside it) to dst through SQLite's backup API, atomically."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for stale in dst.parent.glob(".import-*"):
        shutil.rmtree(stale, ignore_errors=True)
    tmp = dst.with_name(dst.name + ".importing")
    with tempfile.TemporaryDirectory(dir=dst.parent, prefix=".import-") as work:
        staged = Path(work) / DB_NAME
        try:
            for suffix in ("", "-wal", "-journal"):
                part = src.with_name(src.name + suffix)
                if _stat(part):
                    shutil.copyfile(part, staged.with_name(staged.name + suffix))
            tmp.unlink(missing_ok=True)
            with (
                contextlib.closing(sqlite3.connect(staged)) as source,
                contextlib.closing(sqlite3.connect(tmp)) as target,
            ):
                result = source.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise MigrationError(f"{src} failed PRAGMA quick_check: {result}")
                source.backup(target)
            os.replace(tmp, dst)
            _fsync_dir(dst.parent)
        except (OSError, sqlite3.Error) as e:
            raise MigrationError(f"cannot import {src}: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)


def copy_cache(src: Path, dst: Path) -> int:
    """Copy finished cache files that dst lacks; returns how many were copied. Unreadable files are skipped."""
    copied = 0
    for subdir, suffix in CACHE_FILES.items():
        source_dir, target_dir = src / subdir, dst / subdir
        if not source_dir.is_dir():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for leftover in target_dir.glob("*.importing"):
            leftover.unlink(missing_ok=True)
        for path in sorted(source_dir.glob(f"*{suffix}")):
            target = target_dir / path.name
            if not path.is_file() or target.exists():
                continue
            tmp = target.with_name(target.name + ".importing")
            try:
                shutil.copyfile(path, tmp)
                os.replace(tmp, target)
                copied += 1
            except OSError as e:
                tmp.unlink(missing_ok=True)
                log(f"skipped cached file {path}: {e}")
    return copied


def migrate(data_dir: Path, db_path: Path, legacy_dir: Path) -> None:
    marker = data_dir / MARKER
    if marker.exists():
        return
    if not legacy_dir.is_dir():
        if not db_path.exists():
            log(
                f"{db_path} does not exist yet, so the bot will create an empty database. If you ran the old "
                f"compose file (which mounted ./music_bot.db), stop now and mount the project directory at "
                f"{legacy_dir} (see docker-compose.yml) so it gets imported."
            )
        return
    try:
        os.listdir(legacy_dir)
    except OSError as e:
        raise MigrationError(f"cannot list {legacy_dir}: {e}") from e

    done = []
    legacy_db = legacy_dir / DB_NAME
    legacy_stat = _stat(legacy_db)
    current = _stat(db_path)
    if current and current.st_size:
        done.append(f"kept the existing {db_path}")
    elif legacy_stat and legacy_db.is_file() and legacy_stat.st_size:
        log(f"Importing {legacy_db} into {db_path}...")
        copy_database(legacy_db, db_path)
        done.append(f"imported {legacy_db} into {db_path}")
    elif legacy_db.is_dir():
        log(f"{legacy_db} is a directory that Docker created for the old single-file mount; it holds no data.")

    legacy_cache = legacy_dir / "audio_cache"
    if legacy_cache.is_dir():
        log(f"Copying {legacy_cache} into {data_dir / 'audio_cache'}...")
        done.append(f"copied {copy_cache(legacy_cache, data_dir / 'audio_cache')} cached songs")

    marker.write_text("\n".join(done or ["nothing to import"]) + "\n", encoding="utf-8")
    if done:
        log(
            "Legacy import finished: " + "; ".join(done) + ". It will not run again. Once the bot works, you can "
            "delete ./music_bot.db and ./audio_cache on the host and drop the /legacy mount."
        )


def main(argv: list[str]) -> int:
    if not argv:
        log("usage: docker_entrypoint.py COMMAND [ARG...]")
        return 2
    data_dir = Path.cwd()
    db_path = Path(os.environ.get("DB_PATH", DB_NAME)).resolve()
    legacy_dir = Path(os.environ.get("LEGACY_DIR", "/legacy"))
    try:
        with tempfile.TemporaryFile(dir=data_dir):
            pass
    except OSError as e:
        owner = "<uid>:<gid>" if sys.platform == "win32" else f"{os.getuid()}:{os.getgid()}"
        log(
            f"ERROR: {data_dir} is not writable ({e}). With a bind mount, run `chown -R {owner} <host dir>` "
            "on the host."
        )
        return 1
    try:
        migrate(data_dir, db_path, legacy_dir)
    except MigrationError as e:
        log(
            f"ERROR: {e}. The bot was not started, so it cannot create an empty database in place of your data. "
            f"Fix the cause, or copy the database into the volume yourself, then start again."
        )
        return 1
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
