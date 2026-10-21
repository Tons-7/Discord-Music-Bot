"""Parsing shared by autoplay and lyrics: YouTube video titles and uploader names."""

import re

DASHES = (" - ", " – ", " — ")
# Weaker separators, used only when a title has no dash ("Song | Artist").
PIPES = (" | ", " // ", " ｜ ")

# Japanese/Chinese tag brackets: 【MV】, 〔公式〕, ［Official］.
_TAG_BRACKETS = re.compile(r'[【〔［][^】〕］]*[】〕］]')
# Japanese quote brackets hold the song name: 米津玄師 MV「Lemon」.
_CJK_QUOTE = re.compile(r'「([^」]+)」|『([^』]+)』')
# K-pop style quotes: TWICE "Feel Special" M/V, BTS 'Dynamite'. The opening quote must start a word,
# so apostrophes (Don't, Guns N' Roses) never open one.
_PLAIN_QUOTE = re.compile(
    r'(?:^|(?<=[\s(（]))(?:"([^"]{2,}?)"|“([^”]{2,}?)”|\'([^\']{2,}?)\'|‘([^’]{2,}?)’)'
    r'(?=$|[\s)\]（(\[,.!?:-])'
)
_TRAILING_PARENS = re.compile(r'\s+[(\[（][^)\]）]*[)\]）]\s*$')
_FEAT_BRACKETED = re.compile(r'\s*[(\[（]\s*(?:feat\.?|ft\.?|featuring|with)\s[^)\]）]*[)\]）]', re.IGNORECASE)
_FEAT_BARE = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$', re.IGNORECASE)
# Upload labels that are never part of a song name when they stand alone.
_BARE_NOISE = re.compile(
    r'^(?:official(?:\s+(?:music\s+)?(?:video|audio|mv|m/v|lyric\s+video|visuali[sz]er))?|music\s+video|'
    r'lyric\s+video|lyrics?(?:\s+video)?|m/v|mv|audio|video|visuali[sz]er|music\s+clip|hd|hq|4k)$',
    re.IGNORECASE,
)
_TRAILING_NOISE = re.compile(
    r'(?:\s+[-|]\s*|\s+)(?:official\s+(?:music\s+)?(?:video|audio|mv|m/v)|official\s+lyric\s+video|'
    r'music\s+video|lyric\s+video|m/v|mv|-?\s*music\s+clip\s*-?)\s*$',
    re.IGNORECASE,
)
_VEVO_SUFFIX = re.compile(r'VEVO$', re.IGNORECASE)
_TOPIC_SUFFIX = re.compile(r'\s*[-–]\s*Topic$', re.IGNORECASE)
_OFFICIAL_SUFFIX = re.compile(r'\s*Official$', re.IGNORECASE)
_CAMEL_GAP = re.compile(r'(?<=[a-z])(?=[A-Z])')


def collapse(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def strip_tag_brackets(text: str) -> str:
    """Remove 【MV】-style tags, leaving a space so neighbouring words don't fuse."""
    return collapse(_TAG_BRACKETS.sub(' ', text))


def clean_uploader(uploader: str | None) -> str:
    """Bare artist name from a channel name: strips VEVO, '- Topic' and 'Official'."""
    original = (uploader or "").strip()
    name = original
    vevo = bool(_VEVO_SUFFIX.search(name))
    name = _VEVO_SUFFIX.sub('', name).strip()
    name = _TOPIC_SUFFIX.sub('', name).strip()
    name = _OFFICIAL_SUFFIX.sub('', name).strip()
    if vevo and ' ' not in name:
        name = _CAMEL_GAP.sub(' ', name)
    return name or original


def is_artist_channel(uploader: str | None) -> bool:
    """YouTube Music '- Topic' and VEVO channels always belong to the artist."""
    name = (uploader or "").strip()
    return bool(_TOPIC_SUFFIX.search(name) or _VEVO_SUFFIX.search(name))


def name_match(a: str, b: str) -> bool:
    """Case/space-insensitive name matching."""
    a, b = a.lower().replace(" ", ""), b.lower().replace(" ", "")
    return bool(a and b and (a == b or a in b or b in a))


def strip_feat(text: str) -> str:
    """Drop a featured-artist credit: 'Song (feat. X)', 'Artist ft. X'."""
    stripped = _FEAT_BARE.sub('', _FEAT_BRACKETED.sub('', text)).strip()
    return stripped or text


def is_noise(text: str) -> bool:
    return bool(_BARE_NOISE.match(text.strip(' -|:')))


def strip_trailing_noise(text: str) -> str:
    """'Song - Official Music Video' -> 'Song'; leaves titles like 'Video Games' alone."""
    stripped = _TRAILING_NOISE.sub('', text).strip(' -|')
    return stripped or text


def quoted(text: str) -> tuple[str, str, str] | None:
    """(before, inside, after) around the first quoted song name, or None."""
    match = _CJK_QUOTE.search(text) or _PLAIN_QUOTE.search(text)
    if not match:
        return None
    inside = next(group for group in match.groups() if group)
    return text[:match.start()].strip(), inside.strip(), text[match.end():].strip()


def unquote(text: str) -> str:
    """The quoted name when everything around it is noise ("'TOMBOY' M/V"), otherwise the text
    with quoted asides removed ('一途「呪術廻戦」' -> '一途')."""
    parts = quoted(text)
    if not parts:
        return text
    before, inside, after = parts
    rest = collapse(f"{before} {after}")
    if not rest or is_noise(rest) or not re.sub(r'[\W_]+', '', rest):
        return inside
    return collapse(_CJK_QUOTE.sub(' ', text)) or text


def clean_artist_side(text: str) -> str:
    """An artist taken from a title: no feat credit, quotes or trailing '(방탄소년단)' aside."""
    cleaned = strip_feat(text)
    cleaned = _TRAILING_PARENS.sub('', cleaned).strip()
    if len(cleaned) > 2 and cleaned[0] in '"\'“‘' and cleaned[-1] in '"\'”’':
        cleaned = cleaned[1:-1].strip()
    return cleaned or text.strip()


def core(text: str) -> str:
    """A title side reduced to its name, used only for comparisons with the uploader."""
    cleaned = re.sub(r'\s*[(\[（][^)\]）]*[)\]）]', '', unquote(text))
    return strip_trailing_noise(strip_feat(cleaned)).strip() or text


def split_quoted(text: str) -> tuple[str, str] | None:
    """(artist, title) for a dash-less 'ARTIST "TITLE" M/V' or 'ARTIST「TITLE」' title."""
    parts = quoted(text)
    if not parts:
        return None
    before, inside, _ = parts
    artist = clean_artist_side(collapse(re.sub(r'\b(?:m/v|mv|official)\b', ' ', before, flags=re.IGNORECASE)))
    artist = artist.strip(' -|:')
    if is_noise(artist):
        artist = ""
    return artist, inside
