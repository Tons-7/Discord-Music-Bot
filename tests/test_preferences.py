import sqlite3

import pytest
from pydantic import ValidationError

from activity.routes.preferences_routes import PreferencesBody, get_preferences, set_preferences
from bot import MusicBot

ALICE = {"id": "1", "username": "alice"}
BOB = {"id": "2", "username": "bob"}


async def test_no_saved_theme_means_default(bot):
    assert await get_preferences(user=ALICE, bot=bot) == {"theme": None}


async def test_saved_theme_is_returned(bot):
    await set_preferences(PreferencesBody(theme="oled"), user=ALICE, bot=bot)
    await set_preferences(PreferencesBody(theme="ocean"), user=ALICE, bot=bot)

    assert await get_preferences(user=ALICE, bot=bot) == {"theme": "ocean"}


async def test_each_user_keeps_their_own_theme(bot):
    await set_preferences(PreferencesBody(theme="oled"), user=ALICE, bot=bot)

    assert await get_preferences(user=BOB, bot=bot) == {"theme": None}


@pytest.mark.parametrize("theme", ["", "Dark Mode", "x" * 33, "<script>"])
def test_malformed_theme_ids_are_rejected(theme):
    with pytest.raises(ValidationError):
        PreferencesBody(theme=theme)


def test_existing_database_gains_the_table_on_startup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = sqlite3.connect("music_bot.db")
    old.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)")
    old.execute("INSERT INTO schema_version VALUES (1, 5)")
    old.commit()
    old.close()

    MusicBot.init_database()

    db = sqlite3.connect("music_bot.db")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    version = db.execute("SELECT version FROM schema_version").fetchone()[0]
    db.close()
    assert "user_preferences" in tables
    assert version == 6
