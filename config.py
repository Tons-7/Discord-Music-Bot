import discord

# Appearance
COLOR = 0x5865F2

# Pagination & limits
SONGS_PER_PAGE = 15
MAX_PLAYLIST_SIZE = 250
MAX_HISTORY_SIZE = 45

# Search
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 25

# Cache
MAX_CACHE_SIZE = 500
CACHE_TTL = 900  # seconds

# Timeouts
INACTIVE_TIMEOUT_MINUTES = 5
NOW_PLAYING_RESEND_SECONDS = 2400

# Cooldowns (seconds)
COMMAND_COOLDOWN = 3
PLAY_COOLDOWN = 2

# Audio effects (FFmpeg filter chains)
# speed_mult: how much the effect changes real-time playback speed
# (used to keep the progress bar accurate)
AUDIO_EFFECTS = {
    "none": {"name": "None", "filter": "", "speed_mult": 1.0},
    "bass_boost": {"name": "Bass Boost", "filter": "bass=g=10:f=110:w=0.6", "speed_mult": 1.0},
    "nightcore": {"name": "Nightcore", "filter": "asetrate=48000*1.25,aresample=48000", "speed_mult": 1.25},
    "vaporwave": {"name": "Vaporwave", "filter": "asetrate=48000*0.8,aresample=48000", "speed_mult": 0.8},
    "treble_boost": {"name": "Treble Boost", "filter": "treble=g=5:f=3000:w=0.6", "speed_mult": 1.0},
    "8d": {"name": "8D Audio", "filter": "apulsator=hz=0.09", "speed_mult": 1.0},
}

# Lyrics
LYRICS_API_BASE = "https://lrclib.net/api"

# Audio file cache
AUDIO_CACHE_DIR = "audio_cache"
AUDIO_CACHE_MAX_SIZE_MB = 2048  # ~400 songs
AUDIO_CACHE_MAX_AGE_HOURS = 36

# Database
DB_VERSION = 4  # bump when adding migrations


def get_intents():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.guilds = True
    intents.members = True
    return intents
