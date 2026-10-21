import os

import discord

# Appearance
COLOR = 0x5865F2

# Pagination & limits
SONGS_PER_PAGE = 15
MAX_PLAYLIST_SIZE = 250
MAX_HISTORY_SIZE = 45
# Most songs one queue holds; adds and imports past it are refused
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "1000"))
# Most favorites one user keeps
MAX_FAVORITES = 500
# Most entries one playlist or set link lists (YouTube playlists hold up to 5000)
MAX_COLLECTION_ENTRIES = int(os.getenv("MAX_COLLECTION_ENTRIES", "500"))

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
    # asetrate relabels the input rate, so resample to 48 kHz first or 44.1 kHz sources play too fast or slow
    "nightcore": {
        "name": "Nightcore", "filter": "aresample=48000,asetrate=48000*1.25,aresample=48000", "speed_mult": 1.25,
    },
    "vaporwave": {
        "name": "Vaporwave", "filter": "aresample=48000,asetrate=48000*0.8,aresample=48000", "speed_mult": 0.8,
    },
    "treble_boost": {"name": "Treble Boost", "filter": "treble=g=5:f=3000:w=0.6", "speed_mult": 1.0},
    "8d": {"name": "8D Audio", "filter": "apulsator=hz=0.09", "speed_mult": 1.0},
}

# Lyrics
LYRICS_API_BASE = "https://lrclib.net/api"
LRCLIB_USER_AGENT = os.getenv(
    "LRCLIB_USER_AGENT",
    "DiscordMusicBot/1.0 (https://github.com/Tons-7/Discord-Music-Bot)",
)

# Audio file cache (persists indefinitely; no size/age eviction)
AUDIO_CACHE_DIR = "audio_cache"
# Longer songs stream without being cached (0 = cache every length)
AUDIO_CACHE_MAX_DURATION = int(os.getenv("AUDIO_CACHE_MAX_DURATION", "3600"))
AUDIO_CACHE_MAX_FILESIZE = 100 * 1024 * 1024

# Voice
# Seconds to let discord.py finish its own voice reconnect before the bot rejoins by itself
VOICE_RECONNECT_TIMEOUT = int(os.getenv("VOICE_RECONNECT_TIMEOUT", "45"))
# Pause when no listener is left in the voice channel, and leave after this many seconds (0 = never)
ALONE_TIMEOUT_SECONDS = int(os.getenv("ALONE_TIMEOUT_SECONDS", "180"))
# Show the playing song as the voice channel status
VOICE_CHANNEL_STATUS = os.getenv("VOICE_CHANNEL_STATUS", "1") != "0"
# Songs play_next may skip in a row when their video is gone, and how many other failures it tolerates
MAX_SKIPPED_SONGS = 10
MAX_TRANSIENT_FAILURES = 3

# Logging and shutdown
YTDLP_LOG_LEVEL = os.getenv("YTDLP_LOG_LEVEL", "WARNING")
# Seconds uvicorn waits for open responses (Activity audio streams) before the bot shuts down
GRACEFUL_SHUTDOWN_TIMEOUT = int(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "5"))

# Database
DB_PATH = os.getenv("DB_PATH", "music_bot.db")
DB_VERSION = 6  # bump when adding migrations
# Daily consistent copies of the database (empty = off), keeping the newest DB_BACKUP_KEEP
DB_BACKUP_DIR = os.getenv("DB_BACKUP_DIR", "")
DB_BACKUP_KEEP = int(os.getenv("DB_BACKUP_KEEP", "7"))
# Listening stats older than this many days are deleted (0 = keep forever)
STATS_RETENTION_DAYS = int(os.getenv("STATS_RETENTION_DAYS", "0"))

# Playlist collaborator access, weakest first. "owner" is implicit.
PLAYLIST_PERMISSIONS = ("view", "append", "edit")
PLAYLIST_PERMISSION_RANK = {"view": 1, "append": 2, "edit": 3, "owner": 4}

# Activity (Discord Embedded App)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
ACTIVITY_PORT = int(os.getenv("ACTIVITY_PORT", "8080"))


def get_intents():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.guilds = True
    intents.members = True
    return intents
