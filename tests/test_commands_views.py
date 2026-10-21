from types import SimpleNamespace

import discord
import pytest

from activity.ws_manager import Viewer
from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands
from utils import ban_system
from utils.helpers import V2_TEXT_LIMIT, clip, md
from views.now_playing_controls import NowPlayingControls
from views.song_select import SongSelect
from tests.conftest import GUILD_ID, make_song

DJ_ROLE = 900


class Response:
    def __init__(self):
        self.done = False
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kwargs):
        self.done = True
        self.sent.append(kwargs)

    async def defer(self, *args, **kwargs):
        self.done = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs)


def interaction(bot, user, guild=None):
    return SimpleNamespace(
        user=user, guild=guild, guild_id=GUILD_ID, channel_id=10, response=Response(), followup=Followup(),
        delete_original_response=_noop, edit_original_response=_noop, original_response=_noop, client=bot,
    )


async def _noop(*args, **kwargs):
    return None


def channel(channel_id=5, *, members=(), connect=True, speak=True, move=False, limit=0):
    perms = SimpleNamespace(connect=connect, speak=speak, move_members=move, view_channel=True, send_messages=True)
    ch = SimpleNamespace(id=channel_id, name=f"room{channel_id}", mention=f"<#{channel_id}>", members=list(members),
                         user_limit=limit, permissions_for=lambda who: perms)
    return ch


def person(user_id, *, roles=(), admin=False, move=False, voice_channel=None, is_bot=False):
    return SimpleNamespace(
        id=user_id, bot=is_bot, display_name=f"user{user_id}", mention=f"<@{user_id}>",
        roles=[SimpleNamespace(id=r) for r in roles],
        guild_permissions=SimpleNamespace(administrator=admin, move_members=move),
        voice=SimpleNamespace(channel=voice_channel) if voice_channel else None,
    )


def fake_guild(*members, afk=None, role_ids=(DJ_ROLE,)):
    roles = {rid: SimpleNamespace(id=rid, name="DJ") for rid in role_ids}
    g = SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(id=999), afk_channel=afk, members=list(members))
    g.get_member = lambda uid: next((m for m in g.members if m.id == uid), None)
    g.get_role = lambda rid: roles.get(rid)
    return g


# Helpers

def test_markdown_in_titles_is_escaped():
    assert md("A [Remix] *x* _y_ `z`") == r"A \[Remix\] \*x\* \_y\_ \`z\`"


def test_long_text_is_clipped_to_the_limit():
    assert len(clip("x" * (V2_TEXT_LIMIT + 50), V2_TEXT_LIMIT)) == V2_TEXT_LIMIT


def test_a_malformed_ban_line_keeps_the_other_bans(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "banned_users.txt").write_text("123\nnot-an-id\n456\n")
    monkeypatch.setattr(ban_system, "_banned_cache", None)
    assert ban_system.is_banned(123) and ban_system.is_banned(456)
    ban_system.ban_user_id(789)
    assert sorted(int(x) for x in (tmp_path / "banned_users.txt").read_text().split()) == [123, 456, 789]
    monkeypatch.setattr(ban_system, "_banned_cache", None)


# Now-playing panel (#46, #392, #402)

def buttons(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


async def test_the_panel_escapes_titles_and_shows_state(bot, guild_data):
    guild_data["current"] = make_song("a", title="Song *bold* [x]")
    guild_data["shuffle"] = True
    view = NowPlayingControls(bot, GUILD_ID, current_position=10, is_paused=True)
    text = "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
    assert r"Song \*bold\* \[x\]" in text
    by_id = {b.custom_id: b for b in buttons(view)}
    assert str(by_id["np_play_pause"].emoji) == "▶️"
    assert by_id["np_shuffle"].style is discord.ButtonStyle.success


async def test_nowplaying_layout_has_no_buttons(bot, guild_data):
    guild_data["current"] = make_song("a")
    assert buttons(NowPlayingControls(bot, GUILD_ID, controls=False)) == []


async def test_panel_buttons_need_the_dj_role(bot, guild_data):
    room = channel()
    guild_data["current"] = make_song("a")
    guild_data["voice_client"] = SimpleNamespace(channel=room, is_playing=lambda: True)
    guild_data["dj_role_id"] = DJ_ROLE
    listener = person(1, voice_channel=room)
    dj = person(2, roles=[DJ_ROLE], voice_channel=room)
    guild = fake_guild(listener, dj)
    view = NowPlayingControls(bot, GUILD_ID)

    refused = interaction(bot, listener, guild)
    allowed = interaction(bot, dj, guild)
    assert await view.interaction_check(refused) is False
    assert "DJ" in refused.response.sent[0]["embed"].description
    assert await view.interaction_check(allowed) is True


def test_search_picker_offers_every_requested_result():
    songs = [{"title": f"Song {i}", "uploader": "x", "duration": 60} for i in range(25)]
    assert len(SongSelect(songs).options) == 25


# Voice joins and moves (#2000, #2006, #2017)

async def test_join_problems(bot):
    afk = channel(1)
    guild = fake_guild(afk=afk)
    cases = {
        "AFK": afk,
        "permission to join": channel(2, connect=False),
        "permission to speak": channel(3, speak=False),
        "full": channel(4, members=[1, 2], limit=2),
    }
    ok = channel(6)
    for ch in [*cases.values(), ok]:
        ch.guild = guild
    for expected, ch in cases.items():
        assert expected in MusicCommands._join_problem(ch)
    assert MusicCommands._join_problem(ok) is None


async def test_moving_away_from_listeners_needs_a_dj(bot, guild_data):
    cog = MusicCommands(bot)
    here, there = channel(1), channel(2)
    there.members = [person(5)]
    guild_data["current"] = make_song("a")
    voice_client = SimpleNamespace(channel=there)
    caller = person(1, voice_channel=here)
    assert "listener" in cog._move_problem(interaction(bot, caller, fake_guild(caller)), voice_client)
    mover = person(1, voice_channel=here, move=True)
    assert cog._move_problem(interaction(bot, mover, fake_guild(mover)), voice_client) is None


async def test_joining_from_another_channel_keeps_the_activity_session(bot, guild_data, monkeypatch):
    cog = MusicCommands(bot)
    guild_data["current"] = make_song("a")
    viewers = [Viewer(user_id=7, channel_id=1)]
    bot.ws_manager = SimpleNamespace(has_connections=lambda gid: True, get_viewers=lambda gid: viewers)
    assert "Activity" in cog._activity_conflict(GUILD_ID, channel(2))
    assert cog._activity_conflict(GUILD_ID, channel(1)) is None


def test_lyrics_pages_stay_under_the_limit():
    lyrics = "\n".join(f"line {i} " + "la" * 40 for i in range(400))
    pages = MusicCommands._lyrics_pages("Title", "Artist", lyrics)
    assert len(pages) > 1 and all(len(page) <= V2_TEXT_LIMIT for page in pages)


# Playlist commands (#59, #53, #196)

async def test_history_show_lists_newest_first_and_play_takes_that_numbering(bot, guild_data):
    cog = PlaylistCommands(bot)
    guild_data["history"] = [make_song("old"), make_song("new")]
    shown = interaction(bot, person(1), fake_guild())
    await PlaylistCommands.history_show.callback(cog, shown, 1)
    first_page = shown.response.sent[0]["view"]
    text = "\n".join(i.content for i in first_page.walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert text.index("Song new") < text.index("Song old")


async def test_clearing_history_needs_the_dj_role(bot, guild_data):
    music = MusicCommands(bot)
    cog = PlaylistCommands(bot)
    bot.get_cog = lambda name: music if name == "MusicCommands" else None
    guild_data["history"] = [make_song("a")]
    guild_data["dj_role_id"] = DJ_ROLE
    listener = person(1)
    clear = interaction(bot, listener, fake_guild(listener))
    await PlaylistCommands.history_clear.callback(cog, clear)
    assert guild_data["history"] and "DJ" in clear.response.sent[0]["embed"].title


@pytest.mark.parametrize("name", ["Chill/2", "x" * 51, "  "])
async def test_unusable_names_are_refused_by_the_command(bot, name):
    cog = PlaylistCommands(bot)
    create = interaction(bot, person(1), fake_guild())
    await cog._handle_create(create, name)
    assert create.response.sent[0]["ephemeral"] is True
