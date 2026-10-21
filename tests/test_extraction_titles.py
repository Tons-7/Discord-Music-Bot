import pytest

from services.music_service import MusicService
from utils import titles


@pytest.mark.parametrize(("name", "expected"), [
    # Real artist names that contain a marker word must survive.
    ("Post Malone", "Post Malone"),
    ("Ghost", "Ghost"),
    ("Lost Frequencies", "Lost Frequencies"),
    ("Boston", "Boston"),
    ("Frost", "Frost"),
    ("The Score", "The Score"),
    ("Mostly Autumn", "Mostly Autumn"),
    ("Kristian Kostov", "Kristian Kostov"),
    ("Theme Park", "Theme Park"),
    ("Original Broadway Cast", "Original Broadway Cast"),
    ("Lostprophets", "Lostprophets"),
    ("Roxy Music", "Roxy Music"),
    # Markers still go when bracketed or trailing.
    ("Hans Zimmer Music", "Hans Zimmer"),
    ("Kevin Penkin OST", "Kevin Penkin"),
    ("Yoko Shimomura (OST)", "Yoko Shimomura"),
    ("Joe Hisaishi [Soundtrack]", "Joe Hisaishi"),
    ("Toby Fox 【BGM】", "Toby Fox"),
])
def test_clean_artist_name_keeps_real_names(name, expected):
    assert MusicService._clean_artist_name(name) == expected


@pytest.mark.parametrize(("title", "uploader", "expected"), [
    ("Daft Punk - One More Time - Official Video", "Daft Punk", ("Daft Punk", "One More Time")),
    ("Artist - Song - Remix", "", ("Artist", "Song")),
    ("Post Malone - Circles", "PostMaloneVEVO", ("Post Malone", "Circles")),
    ("Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley", ("Rick Astley", "Never Gonna Give You Up")),
    ("Shape of You - Ed Sheeran", "Ed Sheeran", ("Ed Sheeran", "Shape of You")),
    ("Avicii - Levels (Skrillex Remix)", "Skrillex", ("Avicii", "Levels (Skrillex Remix)")),
    ("YOASOBI「夜に駆ける」Official Music Video", "Ayase / YOASOBI", ("YOASOBI", "夜に駆ける")),
    ('TWICE "Feel Special" M/V', "JYP Entertainment", ("TWICE", "Feel Special")),
    ("【MV】YOASOBI - アイドル", "Ayase / YOASOBI", ("YOASOBI", "アイドル")),
    ("Song - Official Video", "", ("", "Song")),
    ("Circles", "Post Malone - Topic", ("", "Circles")),
])
def test_split_title(title, uploader, expected):
    assert MusicService._split_title(title, uploader) == expected


def test_artist_falls_back_to_the_cleaned_uploader():
    assert MusicService._extract_artist_from_title("Circles", "Post Malone - Topic") == "Post Malone"


@pytest.mark.parametrize(("title", "expected"), [
    ("Musical Youth - Pass The Dutchie", None),
    ("Ostrich - Song", None),
    ("Themes - x", None),
    ("OST Persona 5 - Last Surprise", "Persona 5"),
    ("[Music] Celeste - Resurrections", "Celeste"),
    ("Final Fantasy VII OST - Aerith's Theme", "Final Fantasy VII"),
])
def test_extract_content_name(title, expected):
    assert MusicService._extract_content_name(title) == expected


@pytest.mark.parametrize(("uploader", "expected"), [
    ("TaylorSwiftVEVO", "Taylor Swift"),
    ("AviciiOfficialVEVO", "Avicii"),
    ("ACDCVEVO", "ACDC"),
    ("Ed Sheeran - Topic", "Ed Sheeran"),
    ("Queen Official", "Queen"),
    ("Rick Astley", "Rick Astley"),
    (None, ""),
])
def test_clean_uploader(uploader, expected):
    assert titles.clean_uploader(uploader) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("米津玄師 MV「Lemon」", ("米津玄師", "Lemon")),
    ("LiSA 『紅蓮華』 -MUSiC CLiP-", ("LiSA", "紅蓮華")),
    ("BTS (방탄소년단) 'Dynamite' Official MV", ("BTS", "Dynamite")),
    ("Ado「うっせぇわ」", ("Ado", "うっせぇわ")),
    ("Don't Stop Me Now", None),
    ("Rock 'n' Roll Star", None),
    ("Guns N' Roses - Sweet Child O' Mine", None),
])
def test_split_quoted(text, expected):
    assert titles.split_quoted(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("Uptown Funk ft. Bruno Mars", "Uptown Funk"),
    ("Song (with X)", "Song"),
    ("Dancing with a Stranger", "Dancing with a Stranger"),
    ("Gift", "Gift"),
])
def test_strip_feat(text, expected):
    assert titles.strip_feat(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("Somebody That I Used To Know (feat. Kimbra) - official music video", "Somebody That I Used To Know (feat. Kimbra)"),
    ("Song | Official Video", "Song"),
    ("Feel Special M/V", "Feel Special"),
    ("Video Games", "Video Games"),
    ("Radio/Video", "Radio/Video"),
])
def test_strip_trailing_noise(text, expected):
    assert titles.strip_trailing_noise(text) == expected
