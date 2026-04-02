import asyncio
import json
import os
import random
import time
import audioop
from collections import deque
from typing import Optional

import discord
from discord.ext import commands, tasks, voice_recv
from discord import app_commands

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv
from vosk import Model, KaldiRecognizer
from flask import Flask
from threading import Thread

print("[DEBUG] discord.py =", discord.__version__)
print("[DEBUG] voice_recv module =", getattr(voice_recv, "__file__", "unknown"))

try:
    discord.opus._load_default()
except Exception as e:
    print(f"[DEBUG] opus load warning: {e}")

# =========================================================
# ENV
# =========================================================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
PLAYLISTS_FILE = "playlists.json"

VOICE_COMMANDS_ENABLED = os.getenv("VOICE_COMMANDS_ENABLED", "false").lower() == "true"
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "").strip()
VOICE_WAKE_NAME = os.getenv("VOICE_WAKE_NAME", "addy").strip().lower()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found in .env")

voice_model = None
voice_commands_ready = False

if VOICE_COMMANDS_ENABLED and VOSK_MODEL_PATH and os.path.exists(VOSK_MODEL_PATH):
    try:
        voice_model = Model(VOSK_MODEL_PATH)
        voice_commands_ready = True
        print("[VOICE] Vosk model loaded successfully.")
    except Exception as e:
        print(f"[VOICE] Vosk model load failed: {e}")
        voice_commands_ready = False
else:
    if VOICE_COMMANDS_ENABLED:
        print("[VOICE] Voice commands enabled but VOSK_MODEL_PATH is missing/invalid.")

        app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive", 200

def run_web():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# =========================================================
# BOT
# =========================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =========================================================
# YTDL / FFMPEG / SPOTIFY
# =========================================================
YTDL_SEARCH_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "noplaylist": True,
    "extract_flat": False,
    "skip_download": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

YTDL_STREAM_OPTIONS = {
    "format": "bestaudio/best/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "extract_flat": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS)
ytdl_stream = yt_dlp.YoutubeDL(YTDL_STREAM_OPTIONS)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

spotify_enabled = False
sp = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            )
        )
        spotify_enabled = True
    except Exception as e:
        print(f"[SPOTIFY] init failed: {e}")
        spotify_enabled = False

# =========================================================
# STORAGE
# =========================================================
def load_saved_playlists():
    if not os.path.exists(PLAYLISTS_FILE):
        return {}
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_saved_playlists(data):
    with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


saved_playlists = load_saved_playlists()



@bot.event
async def on_guild_join(guild: discord.Guild):
    state = get_state(guild.id)
    channel = find_best_text_channel(guild)
    if not channel:
        print(f"[UI] No suitable channel found on guild join for guild {guild.id}")
        return

    set_text_channel(state, channel)
    state.last_action = "Ready in this server"
    persist_guild_playlist(guild.id)

    try:
        await refresh_player_message(guild)
    except Exception as e:
        print(f"[UI] on_guild_join panel error in guild {guild.id}: {e}")
# =========================================================
# HELPERS
# =========================================================
def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_progress_bar(current_seconds: int, total_seconds: int, length: int = 18) -> str:
    if not total_seconds or total_seconds <= 0:
        return "Live/Unknown"
    current_seconds = max(0, min(current_seconds, total_seconds))
    ratio = current_seconds / total_seconds
    pos = min(length - 1, int(ratio * (length - 1)))
    bar = []
    for i in range(length):
        bar.append("🔘" if i == pos else "▬")
    return "".join(bar)


def bot_member_in_guild(guild: discord.Guild):
    try:
        if bot.user is None:
            return None
        me = guild.get_member(bot.user.id)
        if me:
            return me
        return getattr(guild, "me", None)
    except Exception:
        return None


def can_send_in_channel(channel, guild: discord.Guild) -> bool:
    try:
        me = bot_member_in_guild(guild)
        if me is None:
            return False
        perms = channel.permissions_for(me)
        return (
            perms.view_channel
            and perms.send_messages
            and perms.embed_links
            and perms.read_message_history
        )
    except Exception:
        return False


def find_best_text_channel(guild: discord.Guild):
    preferred_names = ["bot-commands", "bots", "music", "commands", "general", "chat"]

    for name in preferred_names:
        for ch in guild.text_channels:
            if ch.name.lower() == name and can_send_in_channel(ch, guild):
                return ch

    for ch in guild.text_channels:
        if can_send_in_channel(ch, guild):
            return ch

    return None


def reset_player_panel_state(state):
    state.player_message = None
    state.player_message_id = None


def is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist/" in url


def is_spotify_track(url: str) -> bool:
    return "open.spotify.com/track/" in url


def is_spotify_album(url: str) -> bool:
    return "open.spotify.com/album/" in url


def is_youtube_playlist(url: str) -> bool:
    url = url.lower()
    return (
        "youtube.com/playlist" in url
        or ("list=" in url and ("youtube.com" in url or "music.youtube.com" in url))
        or "music.youtube.com/playlist" in url
    )


def is_youtube_music_url(url: str) -> bool:
    return "music.youtube.com" in url


def is_youtube_track_or_music_track(url: str) -> bool:
    url = url.lower()
    return (
        ("youtube.com/watch" in url or "youtu.be/" in url or "music.youtube.com/watch" in url)
        and "list=" not in url
    )


def is_youtube_album_or_browse(url: str) -> bool:
    url = url.lower()
    return "music.youtube.com/browse/" in url


def build_source(stream_url: str, volume_percent: int, seek_seconds: int = 0):
    before_opts = FFMPEG_BEFORE_OPTIONS
    if seek_seconds > 0:
        before_opts = f"-ss {seek_seconds} {FFMPEG_BEFORE_OPTIONS}"

    audio = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_PATH,
        before_options=before_opts,
        options=FFMPEG_OPTIONS,
    )
    source = discord.PCMVolumeTransformer(audio)
    source.volume = max(0, min(volume_percent, 200)) / 100
    return source


def normalize_voice_text(text: str) -> str:
    text = (text or "").strip().lower()
    for ch in [",", ".", "!", "?", ":", ";"]:
        text = text.replace(ch, " ")
    return " ".join(text.split())


def strip_wake_word(text: str) -> str:
    text = normalize_voice_text(text)
    wake = VOICE_WAKE_NAME

    if text.startswith(wake + " "):
        return text[len(wake):].strip()

    if text.endswith(" " + wake):
        return text[:-len(wake)].strip()

    if text == wake:
        return ""

    return text


def downmix_discord_pcm_to_mono(pcm_bytes: bytes) -> bytes:
    try:
        return audioop.tomono(pcm_bytes, 2, 0.5, 0.5)
    except Exception:
        return pcm_bytes


def convert_discord_pcm_for_vosk(pcm_bytes: bytes) -> bytes:
    """
    Discord voice recv PCM:
    - 16-bit
    - stereo
    - 48000 Hz

    Vosk works better with:
    - 16-bit
    - mono
    - 16000 Hz
    """
    try:
        mono = audioop.tomono(pcm_bytes, 2, 0.5, 0.5)
        converted, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
        return converted
    except Exception as e:
        print(f"[VOICE] PCM convert error: {e}")
        return pcm_bytes


async def recognize_voice_pcm(pcm_bytes: bytes) -> str:
    if not voice_commands_ready or not pcm_bytes:
        return ""

    try:
        processed_pcm = convert_discord_pcm_for_vosk(pcm_bytes)
        recognizer = KaldiRecognizer(voice_model, 16000)

        accepted = recognizer.AcceptWaveform(processed_pcm)

        if accepted:
            result = json.loads(recognizer.Result())
            text = normalize_voice_text(result.get("text", ""))
        else:
            partial = json.loads(recognizer.PartialResult())
            text = normalize_voice_text(partial.get("partial", ""))

        final_result = json.loads(recognizer.FinalResult())
        final_text = normalize_voice_text(final_result.get("text", ""))

        if final_text:
            text = final_text

        if text:
            print(f"[VOICE] recognized text: {text}")

        return text
    except Exception as e:
        print(f"[VOICE] recognition error: {e}")
        return ""


async def search_song(search: str):
    loop = asyncio.get_running_loop()

    def extract():
        return ytdl_search.extract_info(f"ytsearch1:{search}", download=False)

    try:
        data = await loop.run_in_executor(None, extract)
    except Exception as e:
        raise RuntimeError(f"Search failed: {e}")

    if not data:
        raise RuntimeError("No results found.")

    if "entries" in data:
        entries = [e for e in data.get("entries", []) if e]
        if not entries:
            raise RuntimeError("No results found.")
        entry = entries[0]
    else:
        entry = data

    thumbnails = entry.get("thumbnails", []) or []
    thumbnail_url = thumbnails[-1]["url"] if thumbnails else None
    uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
    webpage_url = entry.get("webpage_url")
    if not webpage_url and entry.get("id"):
        webpage_url = f"https://www.youtube.com/watch?v={entry['id']}"

    return {
        "title": entry.get("title", search),
        "url": None,
        "stream_url": None,
        "webpage_url": webpage_url,
        "duration": entry.get("duration"),
        "thumbnail": thumbnail_url,
        "uploader": uploader,
        "search_query": search,
        "requester": None,
    }


async def resolve_stream(song: dict) -> dict:
    if song.get("stream_url"):
        return song

    target = song.get("webpage_url") or song.get("search_query")
    if not target:
        raise RuntimeError("Could not resolve stream target.")

    loop = asyncio.get_running_loop()

    def extract_stream():
        return ytdl_stream.extract_info(target, download=False)

    try:
        data = await loop.run_in_executor(None, extract_stream)
    except Exception as e:
        raise RuntimeError(f"Could not load playable audio: {e}")

    if not data:
        raise RuntimeError("Could not load playable audio.")

    if "entries" in data:
        entries = [e for e in data.get("entries", []) if e]
        if not entries:
            raise RuntimeError("No playable result found.")
        data = entries[0]

    stream_url = data.get("url")

    if not stream_url:
        requested_formats = data.get("requested_formats") or []
        for fmt in requested_formats:
            if fmt and fmt.get("url"):
                stream_url = fmt["url"]
                break

    if not stream_url:
        formats = data.get("formats") or []
        audio_formats = [
            f for f in formats
            if f.get("url") and f.get("acodec") not in (None, "none")
        ]
        if audio_formats:
            stream_url = audio_formats[-1]["url"]

    if not stream_url:
        raise RuntimeError("No audio stream URL found.")

    thumbnails = data.get("thumbnails", []) or []
    thumbnail_url = song.get("thumbnail")
    if not thumbnail_url and thumbnails:
        thumbnail_url = thumbnails[-1]["url"]

    song["stream_url"] = stream_url
    song["url"] = stream_url
    song["webpage_url"] = data.get("webpage_url") or song.get("webpage_url")
    song["duration"] = data.get("duration") or song.get("duration")
    song["thumbnail"] = thumbnail_url
    song["uploader"] = data.get("uploader") or data.get("channel") or song.get("uploader", "Unknown")
    song["title"] = data.get("title") or song.get("title", "Unknown")
    return song

# =========================================================
# STATE
# =========================================================
class GuildMusicState:
    def __init__(self):
        self.queue = deque()
        self.history = deque(maxlen=30)
        self.current = None
        self.text_channel = None
        self.text_channel_id = None
        self.player_channel_id = None

        self.volume = 100
        self.loop = False
        self.current_seek = 0
        self.start_time = None

        self.favorite_playlist_url = None
        self.favorite_playlist_name = None
        self.favorite_playlist_tracks = []
        self.favorite_playlist_type = None
        self.active_playlist_name = None
        self.playlist_autoplay = False

        self.saved_playlist_library = {}

        self.player_message = None
        self.player_message_id = None

        self.force_song = None
        self.manual_stop = False
        self.ui_lock = asyncio.Lock()

        self.last_action = "Ready"
        self.show_playlist_selector = False
        self.show_queue_details = False

        self.voice_listener_started = False
        self.voice_sink = None


music_states = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        s = GuildMusicState()
        raw = saved_playlists.get(str(guild_id), {})
        s.favorite_playlist_url = raw.get("url")
        s.favorite_playlist_name = raw.get("name")
        s.favorite_playlist_tracks = raw.get("tracks", [])
        s.favorite_playlist_type = raw.get("type")
        s.playlist_autoplay = raw.get("autoplay", False)
        s.saved_playlist_library = raw.get("library", {})
        s.active_playlist_name = raw.get("active_playlist_name")
        s.player_message_id = raw.get("player_message_id")
        s.text_channel_id = raw.get("text_channel_id")
        s.player_channel_id = raw.get("player_channel_id")
        music_states[guild_id] = s
    return music_states[guild_id]


def persist_guild_playlist(guild_id: int):
    s = get_state(guild_id)
    saved_playlists[str(guild_id)] = {
        "url": s.favorite_playlist_url,
        "name": s.favorite_playlist_name,
        "tracks": s.favorite_playlist_tracks,
        "type": s.favorite_playlist_type,
        "autoplay": s.playlist_autoplay,
        "library": s.saved_playlist_library,
        "active_playlist_name": s.active_playlist_name,
        "player_message_id": s.player_message_id,
        "text_channel_id": s.text_channel_id,
        "player_channel_id": s.player_channel_id,
    }
    save_saved_playlists(saved_playlists)


def set_text_channel(state, channel):
    if channel is None:
        return
    state.text_channel = channel
    state.text_channel_id = channel.id
    state.player_channel_id = channel.id


def make_unique_playlist_name(state, base_name: str) -> str:
    base_name = (base_name or "Unnamed Playlist").strip()
    if base_name not in state.saved_playlist_library:
        return base_name

    count = 2
    while True:
        candidate = f"{base_name} ({count})"
        if candidate not in state.saved_playlist_library:
            return candidate
        count += 1


def create_or_update_playlist(state, playlist_name: str, tracks: list, ptype: str, url: str = None, creator: str = "manual"):
    state.saved_playlist_library[playlist_name] = {
        "url": url,
        "tracks": tracks,
        "type": ptype,
        "creator": creator,
    }

    state.favorite_playlist_url = url
    state.favorite_playlist_name = playlist_name
    state.favorite_playlist_tracks = tracks
    state.favorite_playlist_type = ptype
    state.active_playlist_name = playlist_name
    state.show_playlist_selector = True


def activate_saved_playlist_by_name(state, playlist_name: str) -> bool:
    if playlist_name not in state.saved_playlist_library:
        return False

    p = state.saved_playlist_library[playlist_name]
    state.favorite_playlist_name = playlist_name
    state.favorite_playlist_url = p.get("url")
    state.favorite_playlist_tracks = p.get("tracks", [])
    state.favorite_playlist_type = p.get("type")
    state.active_playlist_name = playlist_name
    state.show_playlist_selector = True
    return True


def delete_playlist_by_name(state, playlist_name: str) -> bool:
    if playlist_name not in state.saved_playlist_library:
        return False

    del state.saved_playlist_library[playlist_name]

    if state.active_playlist_name == playlist_name:
        state.active_playlist_name = None
        state.favorite_playlist_name = None
        state.favorite_playlist_url = None
        state.favorite_playlist_tracks = []
        state.favorite_playlist_type = None
        state.show_playlist_selector = False

    return True


def add_song_to_custom_playlist(state, playlist_name: str, song_query: str) -> bool:
    if playlist_name not in state.saved_playlist_library:
        return False

    state.saved_playlist_library[playlist_name].setdefault("tracks", []).append(song_query)

    if state.active_playlist_name == playlist_name:
        state.favorite_playlist_tracks = state.saved_playlist_library[playlist_name]["tracks"]

    return True


def remove_song_from_custom_playlist(state, playlist_name: str, index: int):
    if playlist_name not in state.saved_playlist_library:
        return False, "Playlist not found"

    tracks = state.saved_playlist_library[playlist_name].get("tracks", [])
    if index < 1 or index > len(tracks):
        return False, "Invalid song index"

    removed = tracks.pop(index - 1)

    if state.active_playlist_name == playlist_name:
        state.favorite_playlist_tracks = tracks

    return True, removed


async def build_singer_playlist(singer_name: str, limit: int = 20):
    tracks = []
    for i in range(1, limit + 1):
        tracks.append(f"{singer_name} song {i}")
    return f"{singer_name} Collection", tracks, "singer"


async def resolve_queue_song(song: dict):
    try:
        if not song.get("webpage_url") and song.get("search_query"):
            resolved = await search_song(song["search_query"])
            resolved["requester"] = song.get("requester")
            song = resolved

        song = await resolve_stream(song)
        return song
    except Exception as e:
        print(f"[resolve_queue_song] failed: {song.get('search_query') or song.get('title')}: {e}")
        return None


def ensure_guild_interaction(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None

# =========================================================
# VOICE COMMAND HANDLER
# =========================================================
async def handle_voice_command(guild: discord.Guild, member: discord.Member, spoken_text: str):
    state = get_state(guild.id)
    vc = guild.voice_client

    if not guild or not member or not spoken_text:
        return

    if not vc or not member.voice or member.voice.channel != vc.channel:
        print("[VOICE] ignored command because member is not in same voice channel.")
        return

    original_text = normalize_voice_text(spoken_text)

    wake_present = (
        VOICE_WAKE_NAME in original_text.split()
        or original_text.startswith(VOICE_WAKE_NAME)
        or original_text.endswith(VOICE_WAKE_NAME)
    )

    if not wake_present:
        print(f"[VOICE] wake word not found in: {original_text}")
        return

    cmd_text = strip_wake_word(original_text)
    if not cmd_text:
        print("[VOICE] wake word detected but command empty.")
        return

    print(f"[VOICE] {member.display_name}: {original_text} -> {cmd_text}")

    if cmd_text.startswith("play ") or cmd_text.startswith("play song "):
        if cmd_text.startswith("play "):
            query = cmd_text[5:].strip()
        elif cmd_text.startswith("play song "):
            query = cmd_text[10:].strip()
        else:
            query = ""

        if not query:
            return

        if not state.text_channel:
            state.text_channel = find_best_text_channel(guild)

        set_text_channel(state, state.text_channel or find_best_text_channel(guild))

        try:
            song = await search_song(query)
            song["requester"] = member
        except Exception as e:
            state.last_action = f"Voice search error: {e}"
            await refresh_player_message(guild)
            return

        if not vc.is_playing() and not vc.is_paused() and state.current is None:
            state.last_action = f"Voice play: {song['title'][:80]}"
            persist_guild_playlist(guild.id)
            await play_song(guild, song)
        else:
            state.queue.append(song)
            state.last_action = f"Voice queued: {song['title'][:80]}"
            persist_guild_playlist(guild.id)
            await refresh_player_message(guild)
        return

    if cmd_text in ("stop", "stop music", "music stop"):
        await stop_all(guild)
        state.last_action = f"Voice stop by {member.display_name}"
        await refresh_player_message(guild)
        return

    if cmd_text in ("pause", "pause music", "music pause"):
        if vc and vc.is_playing():
            vc.pause()
            state.last_action = f"Voice pause by {member.display_name}"
            await refresh_player_message(guild)
        return

    if cmd_text in ("resume", "resume music", "music resume"):
        if vc and vc.is_paused():
            vc.resume()
            state.last_action = f"Voice resume by {member.display_name}"
            await refresh_player_message(guild)
        return

    if cmd_text in ("skip", "next", "next song", "skip song"):
        if vc and (vc.is_playing() or vc.is_paused()):
            state.last_action = f"Voice skip by {member.display_name}"
            vc.stop()
        return

    if cmd_text in ("leave", "disconnect", "leave channel"):
        state.last_action = f"Voice disconnect by {member.display_name}"
        state.current = None
        state.start_time = None
        state.current_seek = 0
        state.voice_listener_started = False
        state.voice_sink = None
        await safe_stop_listening(guild)
        await vc.disconnect()
        await refresh_player_message(guild)
        return

    if cmd_text.startswith("volume "):
        number_part = cmd_text.replace("volume ", "").strip()
        try:
            vol = int(number_part)
        except Exception:
            print(f"[VOICE] invalid volume phrase: {cmd_text}")
            return

        vol = max(0, min(vol, 200))
        state.volume = vol
        if vc and vc.source and hasattr(vc.source, "volume"):
            vc.source.volume = vol / 100
        state.last_action = f"Voice volume {vol}% by {member.display_name}"
        await refresh_player_message(guild)
        return

    if cmd_text in ("loop", "toggle loop"):
        state.loop = not state.loop
        state.last_action = f"Voice loop {'enabled' if state.loop else 'disabled'}"
        await refresh_player_message(guild)
        return

    print(f"[VOICE] command not matched: {cmd_text}")


async def ensure_voice(ctx_or_interaction, member):
    guild = ctx_or_interaction.guild
    if guild is None:
        return None, "This command can only be used inside a server."

    if not member.voice or not member.voice.channel:
        return None, "Please join a voice channel first."

    user_channel = member.voice.channel
    vc = guild.voice_client

    try:
        if vc:
            if vc.channel.id != user_channel.id:
                await vc.move_to(user_channel)
        else:
            if voice_commands_ready:
                print("[VOICE] connecting with VoiceRecvClient...")
                vc = await user_channel.connect(
                    timeout=20,
                    reconnect=True,
                    cls=voice_recv.VoiceRecvClient
                )
            else:
                print("[VOICE] connecting with normal voice client...")
                vc = await user_channel.connect(timeout=20, reconnect=True)
    except discord.Forbidden:
        return None, "I don't have permission to join or move in that voice channel."
    except asyncio.TimeoutError:
        return None, "Voice connection timed out. Try again."
    except Exception as e:
        return None, f"Voice connection error: {e}"

    try:
        await start_voice_listener_if_possible(guild)
    except Exception as e:
        print(f"[VOICE] auto listener error: {e}")

    return vc, None


async def ensure_voice_from_component(interaction: discord.Interaction):
    if not interaction.guild:
        return None, "This interaction can only be used inside a server."

    member = interaction.user
    if not member or not getattr(member, "voice", None) or not member.voice.channel:
        return None, "Please join a voice channel first."

    vc, error = await ensure_voice(interaction, member)
    if error:
        return None, error

    return vc, None

# =========================================================
# VOICE RECEIVE
# =========================================================
class VoiceCommandSink(voice_recv.AudioSink):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.guild = guild
        self.user_buffers = {}
        self.user_last_time = {}
        self.silence_seconds = 1.2
        self.max_buffer_seconds = 6.0

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        if user is None or data is None or getattr(data, "pcm", None) is None:
            return

        now = time.monotonic()
        uid = user.id

        if uid not in self.user_buffers:
            self.user_buffers[uid] = bytearray()
            self.user_last_time[uid] = now

        last_time = self.user_last_time.get(uid, now)

        if self.user_buffers[uid] and (now - last_time) >= self.silence_seconds:
            pcm_blob = bytes(self.user_buffers[uid])
            self.user_buffers[uid].clear()
            try:
                asyncio.run_coroutine_threadsafe(
                    self.process_user_audio(user, pcm_blob),
                    bot.loop
                )
            except Exception as e:
                print(f"[VOICE] schedule error: {e}")

        self.user_buffers[uid].extend(data.pcm)
        self.user_last_time[uid] = now

        approx_seconds = len(self.user_buffers[uid]) / 192000.0
        if approx_seconds >= self.max_buffer_seconds:
            pcm_blob = bytes(self.user_buffers[uid])
            self.user_buffers[uid].clear()
            try:
                asyncio.run_coroutine_threadsafe(
                    self.process_user_audio(user, pcm_blob),
                    bot.loop
                )
            except Exception as e:
                print(f"[VOICE] forced schedule error: {e}")

    async def process_user_audio(self, user, pcm_blob: bytes):
        try:
            print(f"[VOICE] processing audio from {user.display_name}, bytes={len(pcm_blob)}")
            text = await recognize_voice_pcm(pcm_blob)
            if text:
                print(f"[VOICE] recognized from {user.display_name}: {text}")
                await handle_voice_command(self.guild, user, text)
        except Exception as e:
            print(f"[VOICE] process_user_audio error: {e}")

    def cleanup(self):
        try:
            print("[VOICE] sink cleanup called.")
            for uid, buf in list(self.user_buffers.items()):
                if buf:
                    user = self.guild.get_member(uid)
                    if user:
                        pcm_blob = bytes(buf)
                        try:
                            asyncio.run_coroutine_threadsafe(
                                self.process_user_audio(user, pcm_blob),
                                bot.loop
                            )
                        except Exception as e:
                            print(f"[VOICE] cleanup schedule error: {e}")
        except Exception as e:
            print(f"[VOICE] cleanup error: {e}")


def make_voice_after(guild: discord.Guild):
    def _after(error):
        if error:
            print(f"[VOICE] listener after error in guild {guild.id}: {repr(error)}")
        else:
            print(f"[VOICE] listener stopped cleanly in guild {guild.id}")

        state = get_state(guild.id)
        state.voice_listener_started = False
        state.voice_sink = None

        async def _restart():
            await asyncio.sleep(2)
            current_vc = guild.voice_client
            if not current_vc:
                return
            try:
                await restart_voice_listener(guild)
            except Exception as e:
                print(f"[VOICE] auto restart failed in guild {guild.id}: {e}")

        try:
            asyncio.run_coroutine_threadsafe(_restart(), bot.loop)
        except Exception as e:
            print(f"[VOICE] could not schedule listener restart: {e}")

    return _after


async def safe_stop_listening(guild: discord.Guild):
    vc = guild.voice_client
    state = get_state(guild.id)

    if not vc:
        state.voice_listener_started = False
        state.voice_sink = None
        return

    try:
        if hasattr(vc, "stop_listening"):
            vc.stop_listening()
            print(f"[VOICE] stop_listening called in guild {guild.id}")
    except Exception as e:
        print(f"[VOICE] stop_listening error in guild {guild.id}: {e}")

    state.voice_listener_started = False
    state.voice_sink = None


async def start_voice_listener_if_possible(guild: discord.Guild):
    state = get_state(guild.id)

    if not voice_commands_ready:
        state.voice_listener_started = False
        state.voice_sink = None
        return False

    vc = guild.voice_client
    if not vc:
        state.voice_listener_started = False
        state.voice_sink = None
        return False

    if not hasattr(vc, "listen"):
        print(f"[VOICE] Voice client in guild {guild.id} does not support listen()")
        state.voice_listener_started = False
        state.voice_sink = None
        return False

    if state.voice_listener_started and state.voice_sink is not None:
        return True

    try:
        sink = VoiceCommandSink(guild)
        vc.listen(sink, after=make_voice_after(guild))
        state.voice_listener_started = True
        state.voice_sink = sink
        print(f"[VOICE] Listener started in guild {guild.id}")
        return True
    except Exception as e:
        print(f"[VOICE] Listener start failed in guild {guild.id}: {e}")
        state.voice_listener_started = False
        state.voice_sink = None
        return False


async def restart_voice_listener(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client

    if not voice_commands_ready:
        state.voice_listener_started = False
        state.voice_sink = None
        return False

    if not vc or not hasattr(vc, "listen"):
        state.voice_listener_started = False
        state.voice_sink = None
        return False

    await safe_stop_listening(guild)
    await asyncio.sleep(1)

    vc = guild.voice_client
    if not vc:
        return False

    try:
        sink = VoiceCommandSink(guild)
        vc.listen(sink, after=make_voice_after(guild))
        state.voice_listener_started = True
        state.voice_sink = sink
        print(f"[VOICE] Listener restarted in guild {guild.id}")
        return True
    except Exception as e:
        print(f"[VOICE] Listener restart failed in guild {guild.id}: {e}")
        state.voice_listener_started = False
        state.voice_sink = None
        return False

# =========================================================
# UI BUILDERS
# =========================================================
def build_now_playing_embed(guild: discord.Guild):
    state = get_state(guild.id)
    song = state.current
    vc = guild.voice_client

    if not song:
        embed = discord.Embed(
            title="🎵 ADDY MUSIC PLAYER",
            description="Nothing is playing right now.",
            color=discord.Color.dark_grey(),
        )

        if state.favorite_playlist_name:
            embed.add_field(
                name="Active Playlist",
                value=f"{state.favorite_playlist_name} ({len(state.favorite_playlist_tracks)} songs)",
                inline=False,
            )

            preview_lines = []
            for i, track in enumerate(state.favorite_playlist_tracks[:10], start=1):
                preview_lines.append(f"{i}. {track[:90]}")
            if preview_lines:
                embed.add_field(
                    name="Playlist Songs",
                    value="\n".join(preview_lines),
                    inline=False,
                )

        if state.saved_playlist_library:
            names = list(state.saved_playlist_library.keys())[:10]
            embed.add_field(
                name="Saved Playlists",
                value="\n".join(f"• {name}" for name in names),
                inline=False,
            )

        embed.set_footer(text=state.last_action)
        return embed

    elapsed = state.current_seek
    if state.start_time is not None and vc and (vc.is_playing() or vc.is_paused()):
        elapsed = int(asyncio.get_running_loop().time() - state.start_time) + state.current_seek

    duration = song.get("duration") or 0
    progress = build_progress_bar(elapsed, duration)

    queue_preview = []
    queue_items = list(state.queue)
    preview_limit = 8 if state.show_queue_details else 4
    for index, item in enumerate(queue_items[:preview_limit], start=1):
        title = item.get("title") or item.get("search_query", "Unknown")
        dur = format_duration(item.get("duration"))
        prefix = "Next" if index == 1 else f"{index}."
        queue_preview.append(f"**{prefix}:** {title} ({dur})")

    embed = discord.Embed(title="🎵 ADDY MUSIC PLAYER", color=discord.Color.blurple())
    embed.add_field(name="Song", value=f"**{song['title']}**", inline=False)
    embed.add_field(name="Artist / Channel", value=song.get("uploader", "Unknown"), inline=True)
    embed.add_field(name="Volume", value=f"{state.volume}%", inline=True)
    embed.add_field(name="Loop", value="On" if state.loop else "Off", inline=True)

    if song.get("webpage_url"):
        embed.add_field(name="Link", value=song["webpage_url"], inline=False)

    embed.add_field(
        name="Progress",
        value=f"{progress}\n`{format_duration(elapsed)} / {format_duration(song.get('duration'))}`",
        inline=False,
    )

    if state.favorite_playlist_name:
        embed.add_field(
            name="Playlist",
            value=f"{state.favorite_playlist_name} ({len(state.favorite_playlist_tracks)} songs)",
            inline=False,
        )

        preview_lines = []
        current_title = (state.current.get("title", "").lower() if state.current else "")
        for i, track in enumerate(state.favorite_playlist_tracks[:10], start=1):
            marker = "▶ " if current_title and track.lower() in current_title else ""
            preview_lines.append(f"{marker}{i}. {track[:85]}")
        if preview_lines:
            embed.add_field(
                name="Playlist Songs",
                value="\n".join(preview_lines),
                inline=False,
            )

    if queue_preview:
        embed.add_field(name="Queue", value="\n".join(queue_preview), inline=False)
    else:
        embed.add_field(name="Queue", value="No upcoming songs.", inline=False)

    requester = song.get("requester")
    requester_text = requester.mention if requester and hasattr(requester, "mention") else "Unknown"

    status_parts = []
    if vc:
        if vc.is_paused():
            status_parts.append("Paused")
        elif vc.is_playing():
            status_parts.append("Playing")
        else:
            status_parts.append("Idle")
    status_parts.append(f"Requested by {requester_text}")
    status_parts.append(state.last_action)
    embed.set_footer(text=" | ".join(status_parts)[-2048:])

    if song.get("thumbnail"):
        embed.set_image(url=song["thumbnail"])

    return embed

# =========================================================
# DISCORD UI
# =========================================================
class InlinePlaylistSelect(discord.ui.Select):
    def __init__(self, guild_id: int, songs: list[str]):
        self.guild_id = guild_id
        options = []

        for i, song in enumerate(songs[:25], start=1):
            label = f"{i}. {song}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description="Play this song now",
                    value=str(i - 1),
                )
            )

        super().__init__(
            placeholder="Choose a song from the active playlist...",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.defer()
            return

        state = get_state(interaction.guild.id)
        vc, error = await ensure_voice_from_component(interaction)
        if error:
            state.last_action = error
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return

        idx = int(self.values[0])
        if idx < 0 or idx >= len(state.favorite_playlist_tracks):
            state.last_action = "Invalid playlist selection"
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return

        query = state.favorite_playlist_tracks[idx]
        set_text_channel(state, interaction.channel)
        state.show_playlist_selector = True

        stub = {
            "title": query,
            "url": None,
            "stream_url": None,
            "webpage_url": None,
            "duration": None,
            "thumbnail": None,
            "uploader": "Playlist",
            "search_query": query,
            "requester": interaction.user,
        }

        await interaction.response.defer()
        try:
            state.last_action = f"Selected: {query[:80]}"
            await force_play_song(interaction.guild, stub)
            await refresh_player_message(interaction.guild)
        except Exception as e:
            state.last_action = f"Song load error: {e}"
            await refresh_player_message(interaction.guild)


class PlaylistLibrarySelect(discord.ui.Select):
    def __init__(self, guild_id: int, playlist_names: list[str]):
        self.guild_id = guild_id
        options = []

        for name in playlist_names[:25]:
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=name
                )
            )

        super().__init__(
            placeholder="Choose a saved playlist folder...",
            min_values=1,
            max_values=1,
            options=options,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.defer()
            return

        state = get_state(interaction.guild.id)
        vc, error = await ensure_voice_from_component(interaction)
        if error:
            state.last_action = error
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return

        name = self.values[0]

        if not activate_saved_playlist_by_name(state, name):
            state.last_action = "Playlist not found"
            set_text_channel(state, interaction.channel)
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return

        set_text_channel(state, interaction.channel)

        if not state.favorite_playlist_tracks:
            state.last_action = f"{name} is empty"
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return

        state.show_playlist_selector = True
        state.queue.clear()
        state.force_song = None
        await refill_from_favorite_playlist(interaction.guild.id)

        state.last_action = f"Playlist loaded: {name}"
        persist_guild_playlist(interaction.guild.id)

        await interaction.response.defer()
        if vc and not vc.is_playing() and not vc.is_paused():
            await maybe_start_playback(interaction.guild)
        await refresh_player_message(interaction.guild)


class PlayerControls(discord.ui.View):
    def __init__(self, guild_id: int, timeout: int = 600):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.message = None

        state = get_state(guild_id)

        if state.favorite_playlist_tracks:
            try:
                self.add_item(InlinePlaylistSelect(guild_id, state.favorite_playlist_tracks))
            except Exception as e:
                print(f"[UI] Failed to add InlinePlaylistSelect: {e}")

        if state.saved_playlist_library:
            try:
                self.add_item(PlaylistLibrarySelect(guild_id, list(state.saved_playlist_library.keys())))
            except Exception as e:
                print(f"[UI] Failed to add PlaylistLibrarySelect: {e}")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.defer()
            return False

        if not interaction.user.voice or not interaction.user.voice.channel:
            state = get_state(interaction.guild.id)
            state.last_action = "Join a voice channel to use controls"
            await interaction.response.defer()
            await refresh_player_message(interaction.guild)
            return False

        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="▶ Play", style=discord.ButtonStyle.success, row=0)
    async def play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()

        vc, error = await ensure_voice_from_component(interaction)
        if error:
            state.last_action = error
            await refresh_player_message(interaction.guild)
            return

        if state.favorite_playlist_tracks:
            set_text_channel(state, interaction.channel)
            state.force_song = None
            await refill_from_favorite_playlist(interaction.guild.id)
            state.last_action = f"Starting playlist: {state.favorite_playlist_name or 'Playlist'}"
            persist_guild_playlist(interaction.guild.id)

            if vc and (vc.is_playing() or vc.is_paused()):
                state.loop = False
                vc.stop()
            else:
                await maybe_start_playback(interaction.guild)

            await refresh_player_message(interaction.guild)
        elif state.queue:
            started = await maybe_start_playback(interaction.guild)
            state.last_action = "Playback started" if started else "Nothing to play"
            await refresh_player_message(interaction.guild)
        else:
            state.last_action = "No active playlist or queued song found"
            await refresh_player_message(interaction.guild)

    @discord.ui.button(label="⏮ Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        _, msg = await play_previous_song(interaction.guild)
        state.last_action = msg.replace("**", "") if msg else "Previous"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="⏯ Pause/Resume", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        await interaction.response.defer()

        if vc and vc.is_playing():
            vc.pause()
            state.last_action = "Paused"
        elif vc and vc.is_paused():
            vc.resume()
            state.last_action = "Resumed"
        else:
            state.last_action = "Nothing is playing"

        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="⏭ Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        await interaction.response.defer()

        if vc and (vc.is_playing() or vc.is_paused()):
            state.last_action = "Skipped to next"
            vc.stop()
        else:
            started = await maybe_start_playback(interaction.guild)
            state.last_action = "Started next song" if started else "No next song found"

        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.success, row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        state.loop = not state.loop
        state.last_action = f"Loop {'enabled' if state.loop else 'disabled'}"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        await stop_all(interaction.guild)
        state.last_action = "Stopped and cleared queue"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="🔉 Vol -", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        state.volume = max(0, state.volume - 10)
        vc = interaction.guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            vc.source.volume = state.volume / 100
        state.last_action = f"Volume {state.volume}%"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="🔊 Vol +", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        state.volume = min(200, state.volume + 10)
        vc = interaction.guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            vc.source.volume = state.volume / 100
        state.last_action = f"Volume {state.volume}%"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="📜 Queue", style=discord.ButtonStyle.secondary, row=2)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()
        state.show_queue_details = not state.show_queue_details
        state.last_action = f"Queue {'expanded' if state.show_queue_details else 'collapsed'}"
        await refresh_player_message(interaction.guild)

    @discord.ui.button(label="📂 Playlist", style=discord.ButtonStyle.success, row=2)
    async def playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await interaction.response.defer()

        if not state.favorite_playlist_tracks and not state.saved_playlist_library:
            state.last_action = "No saved playlist found"
        else:
            state.show_playlist_selector = True
            state.last_action = "Playlist section opened"

        await refresh_player_message(interaction.guild)

# =========================================================
# PLAYER CORE
# =========================================================
async def get_saved_player_message(guild: discord.Guild):
    state = get_state(guild.id)

    if state.player_message:
        try:
            channel = state.player_message.channel
            if can_send_in_channel(channel, guild):
                return state.player_message
        except Exception:
            reset_player_panel_state(state)

    channel = None

    if state.text_channel and can_send_in_channel(state.text_channel, guild):
        channel = state.text_channel

    if channel is None:
        cid = state.player_channel_id or state.text_channel_id
        if cid:
            fetched = guild.get_channel(cid)
            if fetched and can_send_in_channel(fetched, guild):
                state.text_channel = fetched
                channel = fetched

    if channel is None:
        fallback = find_best_text_channel(guild)
        if fallback:
            state.text_channel = fallback
            state.text_channel_id = fallback.id
            state.player_channel_id = fallback.id
            persist_guild_playlist(guild.id)
            channel = fallback

    if channel is None:
        return None

    if not state.player_message_id:
        return None

    try:
        msg = await channel.fetch_message(state.player_message_id)
        state.player_message = msg
        return msg
    except Exception:
        reset_player_panel_state(state)
        persist_guild_playlist(guild.id)
        return None


async def refresh_player_message(guild: discord.Guild):
    state = get_state(guild.id)

    if not state.text_channel or not can_send_in_channel(state.text_channel, guild):
        cid = state.player_channel_id or state.text_channel_id
        if cid:
            fetched = guild.get_channel(cid)
            if fetched and can_send_in_channel(fetched, guild):
                state.text_channel = fetched

    if not state.text_channel or not can_send_in_channel(state.text_channel, guild):
        fallback = find_best_text_channel(guild)
        if fallback:
            state.text_channel = fallback
            state.text_channel_id = fallback.id
            state.player_channel_id = fallback.id
            persist_guild_playlist(guild.id)

    if not state.text_channel:
        print(f"[UI] No writable text channel found for guild {guild.id}")
        return False

    async with state.ui_lock:
        embed = build_now_playing_embed(guild)

        try:
            view = PlayerControls(guild.id, timeout=600)
        except Exception as e:
            print(f"[UI] View build failed in guild {guild.id}: {e}")
            try:
                await state.text_channel.send("UI load error: buttons/select menu render failed.")
            except Exception as inner_e:
                print(f"[UI] Could not send UI error message: {inner_e}")
            return False

        existing = await get_saved_player_message(guild)
        if existing:
            try:
                await existing.edit(embed=embed, view=view)
                view.message = existing
                state.player_message = existing
                return True
            except Exception as e:
                print(f"[UI] Existing panel edit failed in guild {guild.id}: {e}")
                reset_player_panel_state(state)
                persist_guild_playlist(guild.id)

        try:
            msg = await state.text_channel.send(embed=embed, view=view)
            view.message = msg
            state.player_message = msg
            state.player_message_id = msg.id
            state.player_channel_id = state.text_channel.id
            state.text_channel_id = state.text_channel.id
            persist_guild_playlist(guild.id)
            print(f"[UI] New player panel sent in guild {guild.id}, channel {state.text_channel.id}")
            return True
        except Exception as e:
            print(f"[UI] Player panel send error in guild {guild.id}: {e}")
            return False


async def play_song(guild: discord.Guild, song: dict, seek_seconds: int = 0):
    state = get_state(guild.id)
    vc = guild.voice_client
    if not vc:
        return False

    song = await resolve_stream(song)
    source = build_source(song["stream_url"], state.volume, seek_seconds)

    def after_playing(error):
        if error:
            print(f"Player error: {error}")
        future = asyncio.run_coroutine_threadsafe(handle_after_play(guild), bot.loop)
        try:
            future.result()
        except Exception as e:
            print(f"After callback error: {e}")

    vc.play(source, after=after_playing)
    state.current = song
    state.start_time = asyncio.get_running_loop().time()
    state.current_seek = seek_seconds
    try:
        await refresh_player_message(guild)
    except Exception as e:
        print(f"[UI] refresh after play failed in guild {guild.id}: {e}")
    return True


async def handle_after_play(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client
    if not vc:
        return

    if state.manual_stop:
        state.manual_stop = False
        return

    if state.force_song:
        forced = state.force_song
        state.force_song = None
        try:
            await play_song(guild, forced)
        except Exception as e:
            print(f"[handle_after_play] forced play failed: {e}")
            await play_next(guild)
        return

    if state.loop and state.current:
        try:
            await play_song(guild, state.current)
        except Exception as e:
            print(f"[handle_after_play] loop replay failed: {e}")
            await play_next(guild)
        return

    if state.current:
        state.history.append(state.current)

    await play_next(guild)


async def play_next(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client

    if not vc:
        state.current = None
        state.start_time = None
        state.current_seek = 0
        state.last_action = "Bot is not connected"
        await refresh_player_message(guild)
        return False

    if not state.queue:
        if state.playlist_autoplay and state.favorite_playlist_tracks:
            await refill_from_favorite_playlist(guild.id)

    while state.queue:
        nxt = state.queue.popleft()

        if not nxt.get("requester"):
            nxt["requester"] = bot_member_in_guild(guild) or guild.owner or None

        resolved = await resolve_queue_song(nxt)
        if not resolved:
            print(f"[play_next] skipped broken song: {nxt.get('search_query') or nxt.get('title')}")
            continue

        try:
            state.last_action = f"Now playing: {resolved.get('title', 'Unknown')[:80]}"
            ok = await play_song(guild, resolved)
            if ok:
                return True
        except Exception as e:
            print(f"[play_next] play failed: {e}")
            continue

    state.current = None
    state.start_time = None
    state.current_seek = 0
    state.last_action = "Queue finished"
    await refresh_player_message(guild)
    return False


async def maybe_start_playback(guild: discord.Guild):
    vc = guild.voice_client
    state = get_state(guild.id)
    if not vc:
        return False
    if vc.is_playing() or vc.is_paused():
        return False
    if state.force_song:
        song = state.force_song
        state.force_song = None
        try:
            await play_song(guild, song)
            return True
        except Exception as e:
            print(f"[maybe_start_playback] force_song failed: {e}")
            return await play_next(guild)
    return await play_next(guild)


async def force_play_song(guild: discord.Guild, song: dict):
    state = get_state(guild.id)
    vc = guild.voice_client
    if not vc:
        return False

    resolved = await resolve_queue_song(song)
    if not resolved:
        state.last_action = "Selected song could not be loaded"
        await refresh_player_message(guild)
        return False

    if state.current:
        state.history.append(state.current)

    state.force_song = resolved
    state.loop = False

    if vc.is_playing() or vc.is_paused():
        vc.stop()
    else:
        state.force_song = None
        await play_song(guild, resolved)

    return True


async def play_previous_song(guild: discord.Guild):
    state = get_state(guild.id)
    if not state.history:
        return False, "No previous song was found"

    prev_song = state.history.pop()
    if state.current:
        state.queue.appendleft(state.current)
    await force_play_song(guild, prev_song)
    return True, f"Playing previous: {prev_song.get('title', prev_song.get('search_query', 'Unknown'))}"


async def stop_all(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client
    state.manual_stop = True
    state.force_song = None
    state.queue.clear()
    state.current = None
    state.start_time = None
    state.current_seek = 0
    state.show_playlist_selector = False
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()


async def refill_from_favorite_playlist(guild_id: int):
    state = get_state(guild_id)
    if not state.favorite_playlist_tracks:
        return False

    state.queue.clear()
    for track_query in state.favorite_playlist_tracks:
        state.queue.append(
            {
                "title": track_query,
                "url": None,
                "stream_url": None,
                "webpage_url": None,
                "duration": None,
                "thumbnail": None,
                "uploader": "Playlist",
                "search_query": track_query,
                "requester": None,
            }
        )
    return True

# =========================================================
# PLAYLIST LOADERS
# =========================================================
async def load_spotify_playlist(url: str):
    if not spotify_enabled:
        raise RuntimeError("Spotify support is not enabled. Add Spotify keys in the .env file.")

    tracks = []
    name = "Spotify Playlist"

    if is_spotify_playlist(url):
        playlist = sp.playlist(url)
        name = playlist["name"]
        for item in playlist["tracks"]["items"]:
            track = item.get("track")
            if not track:
                continue
            title = track["name"]
            artists = ", ".join(a["name"] for a in track["artists"])
            tracks.append(f"{title} {artists}")
    elif is_spotify_album(url):
        album = sp.album(url)
        name = album["name"]
        for track in album["tracks"]["items"]:
            title = track["name"]
            artists = ", ".join(a["name"] for a in track["artists"])
            tracks.append(f"{title} {artists}")
    elif is_spotify_track(url):
        track = sp.track(url)
        name = track["name"]
        artists = ", ".join(a["name"] for a in track["artists"])
        tracks.append(f"{name} {artists}")
    else:
        raise RuntimeError("Invalid Spotify playlist, album, or track link.")

    return name, tracks, "spotify"


async def load_youtube_media(url: str):
    loop = asyncio.get_running_loop()

    def extract_media():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        with yt_dlp.YoutubeDL(opts) as ydl_local:
            return ydl_local.extract_info(url, download=False)

    data = await loop.run_in_executor(None, extract_media)
    if not data:
        raise RuntimeError("Could not load the YouTube / YouTube Music media.")

    if "entries" in data and data.get("entries"):
        name = data.get("title", "YouTube Playlist")
        tracks = []
        for entry in data.get("entries", [])[:500]:
            if not entry:
                continue
            title = entry.get("title")
            uploader = entry.get("uploader", "") or entry.get("channel", "")
            if title:
                tracks.append(f"{title} {uploader}".strip())

        if not tracks:
            raise RuntimeError("No songs were found in that YouTube / YouTube Music playlist.")

        media_type = "youtube_music" if is_youtube_music_url(url) else "youtube"
        return name, tracks, media_type

    title = data.get("title")
    uploader = data.get("uploader", "") or data.get("channel", "")
    if not title:
        raise RuntimeError("No playable title was found.")

    media_type = "youtube_music_track" if is_youtube_music_url(url) else "youtube_track"
    return title, [f"{title} {uploader}".strip()], media_type


async def load_playlist_from_url(url: str):
    if is_spotify_playlist(url) or is_spotify_album(url) or is_spotify_track(url):
        return await load_spotify_playlist(url)

    if is_youtube_playlist(url) or is_youtube_track_or_music_track(url) or is_youtube_album_or_browse(url):
        return await load_youtube_media(url)

    raise RuntimeError("Only YouTube, YouTube Music, and Spotify playlist/album/track links are allowed.")

# =========================================================
# STATUS / EVENTS
# =========================================================
status_messages = [
    "/play song",
    "/setplaylist name url",
    "/startplaylist",
    "/playlistpanel",
    "/playlists",
    "!play song",
    "!createplaylist myfolder",
    "!singerplaylist arijit singh",
    "!setplaylist My Playlist | link",
    "Say: addy play song",
    "Say: addy stop",
]


@tasks.loop(seconds=25)
async def rotate_status():
    msg = random.choice(status_messages)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=msg))


@tasks.loop(seconds=8)
async def ui_updater():
    for guild in bot.guilds:
        try:
            state = get_state(guild.id)
            vc = guild.voice_client
            if state.player_channel_id and ((vc and (vc.is_playing() or vc.is_paused())) or state.saved_playlist_library):
                await refresh_player_message(guild)
        except Exception:
            pass


@bot.event
async def on_ready():
    if not rotate_status.is_running():
        rotate_status.start()
    if not ui_updater.is_running():
        ui_updater.start()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Slash sync error: {e}")

    print(f"{bot.user} is online!")
    print(f"[VOICE] enabled={VOICE_COMMANDS_ENABLED}, ready={voice_commands_ready}, wake_word={VOICE_WAKE_NAME}")

# =========================================================
# HELP
# =========================================================
def build_help_embed():
    embed = discord.Embed(
        title="Addy Music Bot Help",
        description="Single-message player UI with playlist controls, queue, previous, next, loop, volume, direct play, and voice commands.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Playlist Commands",
        value=(
            "`!setplaylist <playlist name> | <link>`\n"
            "`!createplaylist <name>`\n"
            "`!addtoplaylist <name> <song>`\n"
            "`!removefromplaylist <name> <index>`\n"
            "`!viewplaylist <name>`\n"
            "`!useplaylist <name>`\n"
            "`!startplaylist [name]`\n"
            "`!deleteplaylist <name>`\n"
            "`!singerplaylist <singer name>`\n"
            "`!allplaylists [page]`\n"
            "`!autoplayplaylist on/off`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Music Commands",
        value="`!join` `!play <song>` `!pause` `!resume` `!skip` `!stop` `!queue` `!now` `!loop` `!volume <0-200>` `!leave`",
        inline=False,
    )
    embed.add_field(
        name="Voice Commands",
        value=(
            "`addy play <song>`\n"
            "`addy play song <song>`\n"
            "`addy stop`\n"
            "`addy pause`\n"
            "`addy resume`\n"
            "`addy skip`\n"
            "`addy next`\n"
            "`addy leave`\n"
            "`addy volume 80`\n"
            "`addy loop`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Slash Commands",
        value=(
            "`/join` `/play` `/pause` `/resume` `/skip` `/stop` `/queue` `/now` `/volume`\n"
            "`/setplaylist` `/showplaylist` `/startplaylist` `/playlistpanel` `/playlists`\n"
            "`/allplaylists` `/useplaylist` `/deleteplaylist` `/createplaylist`\n"
            "`/addtoplaylist` `/removefromplaylist` `/viewplaylist` `/singerplaylist`"
        ),
        inline=False,
    )
    return embed

# =========================================================
# PREFIX COMMANDS
# =========================================================
@bot.command()
async def helpme(ctx):
    await ctx.send(embed=build_help_embed())


@bot.command()
async def join(ctx):
    vc, error = await ensure_voice(ctx, ctx.author)
    if error:
        await ctx.send(error)
        return

    state = get_state(ctx.guild.id)
    set_text_channel(state, ctx.channel)
    state.show_playlist_selector = True
    state.last_action = f"Joined {vc.channel.name}"
    persist_guild_playlist(ctx.guild.id)

    await start_voice_listener_if_possible(ctx.guild)

    ok = await refresh_player_message(ctx.guild)
    if not ok:
        await ctx.send("Joined voice channel, but I could not send the UI panel here. Please check bot permissions.")


@bot.command()
async def leave(ctx):
    vc = ctx.voice_client
    if not vc:
        await ctx.send("Bot is not connected.")
        return

    state = get_state(ctx.guild.id)

    await safe_stop_listening(ctx.guild)

    state.last_action = "Disconnected from voice channel"
    state.current = None
    state.start_time = None
    state.current_seek = 0
    state.voice_listener_started = False
    state.voice_sink = None

    await vc.disconnect()
    await refresh_player_message(ctx.guild)


@bot.command()
async def play(ctx, *, search):
    state = get_state(ctx.guild.id)
    vc, error = await ensure_voice(ctx, ctx.author)
    if error:
        await ctx.send(error)
        return

    set_text_channel(state, ctx.channel)
    try:
        song = await search_song(search)
    except Exception as e:
        await ctx.send(f"Song search error: {e}")
        return

    song["requester"] = ctx.author

    if not vc.is_playing() and not vc.is_paused() and state.current is None:
        state.last_action = f"Now playing: {song['title'][:80]}"
        persist_guild_playlist(ctx.guild.id)
        await play_song(ctx.guild, song)
        return

    state.queue.append(song)
    state.last_action = f"Queued: {song['title'][:80]}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def pause(ctx):
    vc = ctx.voice_client
    state = get_state(ctx.guild.id)
    if vc and vc.is_playing():
        vc.pause()
        state.last_action = "Paused"
        await refresh_player_message(ctx.guild)


@bot.command()
async def resume(ctx):
    vc = ctx.voice_client
    state = get_state(ctx.guild.id)
    if vc and vc.is_paused():
        vc.resume()
        state.last_action = "Resumed"
        await refresh_player_message(ctx.guild)


@bot.command()
async def skip(ctx):
    vc = ctx.voice_client
    state = get_state(ctx.guild.id)
    if vc and (vc.is_playing() or vc.is_paused()):
        state.last_action = "Skipped to next"
        vc.stop()


@bot.command()
async def stop(ctx):
    state = get_state(ctx.guild.id)
    await stop_all(ctx.guild)
    state.last_action = "Stopped and cleared queue"
    await refresh_player_message(ctx.guild)


@bot.command()
async def queue(ctx):
    state = get_state(ctx.guild.id)
    state.show_queue_details = not state.show_queue_details
    state.last_action = f"Queue {'expanded' if state.show_queue_details else 'collapsed'}"
    await refresh_player_message(ctx.guild)


@bot.command(name="now")
async def now_playing(ctx):
    state = get_state(ctx.guild.id)
    set_text_channel(state, ctx.channel)
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def loop(ctx):
    state = get_state(ctx.guild.id)
    state.loop = not state.loop
    state.last_action = f"Loop {'enabled' if state.loop else 'disabled'}"
    await refresh_player_message(ctx.guild)


@bot.command()
async def volume(ctx, vol: int):
    state = get_state(ctx.guild.id)
    if vol < 0 or vol > 200:
        await ctx.send("Volume must be between 0 and 200.")
        return
    state.volume = vol
    vc = ctx.voice_client
    if vc and vc.source and hasattr(vc.source, "volume"):
        vc.source.volume = vol / 100
    state.last_action = f"Volume {vol}%"
    await refresh_player_message(ctx.guild)


@bot.command()
async def setplaylist(ctx, *, input_text: str):
    state = get_state(ctx.guild.id)

    if "|" not in input_text:
        await ctx.send("Use format: `!setplaylist playlist name | playlist link`")
        return

    raw_name, raw_url = input_text.split("|", 1)
    playlist_name = raw_name.strip()
    url = raw_url.strip()

    if not playlist_name:
        await ctx.send("Please enter a playlist name before `|`.")
        return

    if not url:
        await ctx.send("Please enter a playlist link after `|`.")
        return

    try:
        _, tracks, ptype = await load_playlist_from_url(url)
    except Exception as e:
        await ctx.send(f"Playlist error: {e}")
        return

    if not tracks:
        await ctx.send("No songs were found in that playlist.")
        return

    final_name = playlist_name
    if final_name in state.saved_playlist_library:
        final_name = make_unique_playlist_name(state, final_name)

    create_or_update_playlist(
        state=state,
        playlist_name=final_name,
        tracks=tracks,
        ptype=ptype,
        url=url,
        creator="link"
    )

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.show_playlist_selector = True
    state.last_action = f"Playlist saved: {final_name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)

    await ctx.send(f"Playlist added successfully: **{final_name}** ({len(tracks)} songs)")


@bot.command()
async def createplaylist(ctx, *, playlist_name: str):
    state = get_state(ctx.guild.id)

    if playlist_name in state.saved_playlist_library:
        await ctx.send("A playlist with this name already exists.")
        return

    create_or_update_playlist(
        state=state,
        playlist_name=playlist_name,
        tracks=[],
        ptype="custom",
        url=None,
        creator="manual"
    )

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Created empty playlist: {playlist_name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def addtoplaylist(ctx, playlist_name: str, *, song_query: str):
    state = get_state(ctx.guild.id)

    if not add_song_to_custom_playlist(state, playlist_name, song_query):
        await ctx.send("Playlist not found.")
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Added song to {playlist_name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def removefromplaylist(ctx, playlist_name: str, index: int):
    state = get_state(ctx.guild.id)

    ok, msg = remove_song_from_custom_playlist(state, playlist_name, index)
    if not ok:
        await ctx.send(msg)
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Removed from {playlist_name}: {msg[:60]}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def singerplaylist(ctx, *, singer_name: str):
    state = get_state(ctx.guild.id)

    try:
        name, tracks, ptype = await build_singer_playlist(singer_name, limit=20)
    except Exception as e:
        await ctx.send(f"Singer playlist error: {e}")
        return

    create_or_update_playlist(
        state=state,
        playlist_name=name,
        tracks=tracks,
        ptype=ptype,
        url=None,
        creator="singer"
    )

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Singer playlist created: {name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def deleteplaylist(ctx, *, playlist_name: str):
    state = get_state(ctx.guild.id)

    if not delete_playlist_by_name(state, playlist_name):
        await ctx.send("Playlist not found.")
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Deleted playlist: {playlist_name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)
    await ctx.send(f"Deleted playlist: **{playlist_name}**")


@bot.command()
async def viewplaylist(ctx, *, playlist_name: str):
    state = get_state(ctx.guild.id)

    if playlist_name not in state.saved_playlist_library:
        await ctx.send("Playlist not found.")
        return

    playlist = state.saved_playlist_library[playlist_name]
    tracks = playlist.get("tracks", [])

    if not tracks:
        await ctx.send(f"Playlist **{playlist_name}** is empty.")
        return

    lines = []
    for i, song in enumerate(tracks[:20], start=1):
        lines.append(f"{i}. {song}")

    embed = discord.Embed(
        title=f"Playlist: {playlist_name}",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Total songs: {len(tracks)}")
    await ctx.send(embed=embed)


@bot.command()
async def showplaylist(ctx):
    state = get_state(ctx.guild.id)
    if not state.favorite_playlist_tracks:
        await ctx.send("No active playlist has been set.")
        return

    preview = "\n".join(f"{i + 1}. {song}" for i, song in enumerate(state.favorite_playlist_tracks[:10]))
    embed = discord.Embed(title="Active Playlist", description=f"**{state.favorite_playlist_name}**", color=discord.Color.green())
    embed.add_field(name="Type", value=state.favorite_playlist_type or "Unknown", inline=True)
    embed.add_field(name="Songs", value=str(len(state.favorite_playlist_tracks)), inline=True)
    embed.add_field(name="Autoplay", value="ON" if state.playlist_autoplay else "OFF", inline=True)
    embed.add_field(name="Preview", value=preview or "No songs", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def playlists(ctx):
    state = get_state(ctx.guild.id)
    if not state.saved_playlist_library:
        await ctx.send("No saved playlists found.")
        return
    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.show_playlist_selector = True
    state.last_action = "Playlist library opened in player"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def allplaylists(ctx, page: int = 1):
    state = get_state(ctx.guild.id)

    names = list(state.saved_playlist_library.keys())
    if not names:
        await ctx.send("No saved playlists found.")
        return

    page_size = 15
    total_pages = max(1, (len(names) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    chunk = names[start:end]

    lines = []
    for i, name in enumerate(chunk, start=start + 1):
        p = state.saved_playlist_library[name]
        ptype = p.get("type", "unknown")
        count = len(p.get("tracks", []))
        marker = " (ACTIVE)" if name == state.active_playlist_name else ""
        lines.append(f"{i}. {name} | {ptype} | {count} songs{marker}")

    embed = discord.Embed(
        title="Saved Playlists",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Page {page}/{total_pages}")
    await ctx.send(embed=embed)


@bot.command()
async def useplaylist(ctx, *, playlist_name: str):
    state = get_state(ctx.guild.id)

    if not activate_saved_playlist_by_name(state, playlist_name):
        await ctx.send("Playlist not found. Use `!allplaylists` to see saved names.")
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.last_action = f"Active playlist changed to: {playlist_name}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def playlistpanel(ctx):
    state = get_state(ctx.guild.id)
    if not state.favorite_playlist_tracks and not state.saved_playlist_library:
        await ctx.send("No active or saved playlist has been set.")
        return

    _, error = await ensure_voice(ctx, ctx.author)
    if error:
        await ctx.send(error)
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.show_playlist_selector = True
    state.last_action = f"Playlist selector opened for {state.favorite_playlist_name or 'playlist'}"
    persist_guild_playlist(ctx.guild.id)
    await refresh_player_message(ctx.guild)


@bot.command()
async def startplaylist(ctx, *, playlist_name: str = None):
    state = get_state(ctx.guild.id)
    _, error = await ensure_voice(ctx, ctx.author)
    if error:
        await ctx.send(error)
        return

    await start_voice_listener_if_possible(ctx.guild)

    if playlist_name:
        if not activate_saved_playlist_by_name(state, playlist_name):
            await ctx.send("Playlist not found.")
            return

    if not state.favorite_playlist_tracks:
        await ctx.send("Please set or create a playlist first.")
        return

    set_text_channel(state, ctx.channel)
    state.player_channel_id = ctx.channel.id
    state.current = None
    state.force_song = None
    await refill_from_favorite_playlist(ctx.guild.id)
    state.last_action = f"Starting playlist: {state.favorite_playlist_name}"
    persist_guild_playlist(ctx.guild.id)
    await maybe_start_playback(ctx.guild)


@bot.command()
async def autoplayplaylist(ctx, mode=None):
    state = get_state(ctx.guild.id)
    if mode is None:
        await ctx.send(f"Playlist autoplay is currently {'ON' if state.playlist_autoplay else 'OFF'}.")
        return

    mode = mode.lower()
    if mode == "on":
        state.playlist_autoplay = True
    elif mode == "off":
        state.playlist_autoplay = False
    else:
        await ctx.send("Use: `!autoplayplaylist on` or `!autoplayplaylist off`")
        return

    persist_guild_playlist(ctx.guild.id)
    state.last_action = f"Playlist autoplay {'ON' if state.playlist_autoplay else 'OFF'}"
    await refresh_player_message(ctx.guild)

# =========================================================
# SLASH COMMANDS
# =========================================================
@bot.tree.command(name="join", description="Join your voice channel")
async def slash_join(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    vc, error = await ensure_voice(interaction, interaction.user)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    state = get_state(interaction.guild.id)
    set_text_channel(state, interaction.channel)
    state.show_playlist_selector = True
    state.last_action = f"Joined {vc.channel.name}"
    persist_guild_playlist(interaction.guild.id)

    await start_voice_listener_if_possible(interaction.guild)

    ok = await refresh_player_message(interaction.guild)

    if ok:
        await interaction.followup.send("Joined voice channel and UI panel updated.", ephemeral=True)
    else:
        await interaction.followup.send(
            "Joined voice channel, but UI panel could not be shown. Check bot permissions.",
            ephemeral=True,
        )


@bot.tree.command(name="play", description="Play a song by name")
@app_commands.describe(search="Song name")
async def slash_play(interaction: discord.Interaction, search: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    vc, error = await ensure_voice(interaction, interaction.user)
    if error:
        state.last_action = error
        await refresh_player_message(interaction.guild)
        await interaction.followup.send(error, ephemeral=True)
        return

    set_text_channel(state, interaction.channel)

    try:
        song = await search_song(search)
    except Exception as e:
        state.last_action = f"Song search error: {e}"
        await refresh_player_message(interaction.guild)
        await interaction.followup.send(f"Song search error: {e}", ephemeral=True)
        return

    song["requester"] = interaction.user

    if not vc.is_playing() and not vc.is_paused() and state.current is None:
        state.last_action = f"Now playing: {song['title'][:80]}"
        persist_guild_playlist(interaction.guild.id)
        await play_song(interaction.guild, song)
    else:
        state.queue.append(song)
        state.last_action = f"Queued: {song['title'][:80]}"
        persist_guild_playlist(interaction.guild.id)
        await refresh_player_message(interaction.guild)

    await interaction.followup.send("Song added/started.", ephemeral=True)


@bot.tree.command(name="queue", description="Toggle queue details in player")
async def slash_queue(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    state.show_queue_details = not state.show_queue_details
    state.last_action = f"Queue {'expanded' if state.show_queue_details else 'collapsed'}"
    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send("Queue updated.", ephemeral=True)


@bot.tree.command(name="now", description="Show current song panel")
async def slash_now(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    set_text_channel(state, interaction.channel)
    persist_guild_playlist(interaction.guild.id)
    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send("Player panel updated in this channel.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause current song")
async def slash_pause(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    await interaction.response.defer()
    if vc and vc.is_playing():
        vc.pause()
        state.last_action = "Paused"
        await refresh_player_message(interaction.guild)
    await interaction.followup.send("Paused.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume current song")
async def slash_resume(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    await interaction.response.defer()
    if vc and vc.is_paused():
        vc.resume()
        state.last_action = "Resumed"
        await refresh_player_message(interaction.guild)
    await interaction.followup.send("Resumed.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip current song")
async def slash_skip(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    await interaction.response.defer()
    if vc and (vc.is_playing() or vc.is_paused()):
        state.last_action = "Skipped to next"
        vc.stop()
    await interaction.followup.send("Skipped.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear queue")
async def slash_stop(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    await interaction.response.defer()
    await stop_all(interaction.guild)
    state.last_action = "Stopped and cleared queue"
    await refresh_player_message(interaction.guild)
    await interaction.followup.send("Stopped.", ephemeral=True)


@bot.tree.command(name="volume", description="Set volume")
@app_commands.describe(vol="Volume 0 to 200")
async def slash_volume(interaction: discord.Interaction, vol: int):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    if vol < 0 or vol > 200:
        await interaction.response.send_message("Volume must be between 0 and 200.", ephemeral=True)
        return
    state.volume = vol
    vc = interaction.guild.voice_client
    if vc and vc.source and hasattr(vc.source, "volume"):
        vc.source.volume = vol / 100
    state.last_action = f"Volume {vol}%"
    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Volume set to {vol}%.", ephemeral=True)


@bot.tree.command(name="setplaylist", description="Set a YouTube, YouTube Music or Spotify playlist")
@app_commands.describe(name="Playlist save name", url="Playlist link")
async def slash_setplaylist(interaction: discord.Interaction, name: str, url: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer()
    state = get_state(interaction.guild.id)

    try:
        _, tracks, ptype = await load_playlist_from_url(url)
    except Exception as e:
        state.last_action = f"Playlist error: {e}"
        set_text_channel(state, interaction.channel)
        await refresh_player_message(interaction.guild)
        await interaction.followup.send(f"Playlist error: {e}", ephemeral=True)
        return

    if not tracks:
        state.last_action = "No songs were found in that playlist"
        set_text_channel(state, interaction.channel)
        await refresh_player_message(interaction.guild)
        await interaction.followup.send("No songs were found in that playlist.", ephemeral=True)
        return

    final_name = name.strip()
    if not final_name:
        state.last_action = "Playlist name cannot be empty"
        set_text_channel(state, interaction.channel)
        await refresh_player_message(interaction.guild)
        await interaction.followup.send("Playlist name cannot be empty.", ephemeral=True)
        return

    if final_name in state.saved_playlist_library:
        final_name = make_unique_playlist_name(state, final_name)

    create_or_update_playlist(
        state=state,
        playlist_name=final_name,
        tracks=tracks,
        ptype=ptype,
        url=url,
        creator="link"
    )

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.show_playlist_selector = True
    state.last_action = f"Playlist saved: {final_name}"
    persist_guild_playlist(interaction.guild.id)
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Playlist saved: **{final_name}**", ephemeral=True)


@bot.tree.command(name="showplaylist", description="Show active playlist")
async def slash_showplaylist(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    if not state.favorite_playlist_tracks:
        await interaction.response.send_message("No active playlist has been set.", ephemeral=True)
        return
    preview = "\n".join(f"{i + 1}. {song}" for i, song in enumerate(state.favorite_playlist_tracks[:10]))
    embed = discord.Embed(title="Active Playlist", description=f"**{state.favorite_playlist_name}**", color=discord.Color.green())
    embed.add_field(name="Type", value=state.favorite_playlist_type or "Unknown", inline=True)
    embed.add_field(name="Songs", value=str(len(state.favorite_playlist_tracks)), inline=True)
    embed.add_field(name="Autoplay", value="ON" if state.playlist_autoplay else "OFF", inline=True)
    embed.add_field(name="Preview", value=preview or "No songs", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="playlists", description="Open saved playlists in player")
async def slash_playlists(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    state = get_state(interaction.guild.id)
    if not state.saved_playlist_library:
        await interaction.response.send_message("No saved playlists found.", ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.show_playlist_selector = True
    state.last_action = "Playlist library opened in player"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    ok = await refresh_player_message(interaction.guild)

    if ok:
        await interaction.followup.send("Player panel updated in this channel.", ephemeral=True)
    else:
        await interaction.followup.send("Playlist panel could not be rendered.", ephemeral=True)


@bot.tree.command(name="allplaylists", description="Show all saved playlists")
@app_commands.describe(page="Page number")
async def slash_allplaylists(interaction: discord.Interaction, page: int = 1):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    names = list(state.saved_playlist_library.keys())
    if not names:
        await interaction.response.send_message("No saved playlists found.", ephemeral=True)
        return

    page_size = 15
    total_pages = max(1, (len(names) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    chunk = names[start:end]

    lines = []
    for i, name in enumerate(chunk, start=start + 1):
        p = state.saved_playlist_library[name]
        ptype = p.get("type", "unknown")
        count = len(p.get("tracks", []))
        marker = " (ACTIVE)" if name == state.active_playlist_name else ""
        lines.append(f"{i}. {name} | {ptype} | {count} songs{marker}")

    embed = discord.Embed(
        title="Saved Playlists",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Page {page}/{total_pages}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="useplaylist", description="Activate a saved playlist by exact name")
@app_commands.describe(playlist_name="Exact saved playlist name")
async def slash_useplaylist(interaction: discord.Interaction, playlist_name: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    if not activate_saved_playlist_by_name(state, playlist_name):
        await interaction.response.send_message("Playlist not found. Use /allplaylists to see saved names.", ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Active playlist changed to: {playlist_name}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Active playlist changed to: {playlist_name}", ephemeral=True)


@bot.tree.command(name="playlistpanel", description="Open playlist song selector in player")
async def slash_playlistpanel(interaction: discord.Interaction):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    if not state.favorite_playlist_tracks and not state.saved_playlist_library:
        await interaction.response.send_message("No active or saved playlist has been set.", ephemeral=True)
        return

    _, error = await ensure_voice(interaction, interaction.user)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.show_playlist_selector = True
    state.last_action = f"Playlist selector opened for {state.favorite_playlist_name or 'playlist'}"
    persist_guild_playlist(interaction.guild.id)
    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send("Playlist selector opened.", ephemeral=True)


@bot.tree.command(name="startplaylist", description="Start saved playlist")
@app_commands.describe(playlist_name="Optional saved playlist name")
async def slash_startplaylist(interaction: discord.Interaction, playlist_name: str = None):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    await interaction.response.defer()
    state = get_state(interaction.guild.id)
    _, error = await ensure_voice(interaction, interaction.user)
    if error:
        state.last_action = error
        await refresh_player_message(interaction.guild)
        await interaction.followup.send(error, ephemeral=True)
        return

    await start_voice_listener_if_possible(interaction.guild)

    if playlist_name:
        if not activate_saved_playlist_by_name(state, playlist_name):
            state.last_action = "Playlist not found"
            await refresh_player_message(interaction.guild)
            await interaction.followup.send("Playlist not found.", ephemeral=True)
            return

    if not state.favorite_playlist_tracks:
        state.last_action = "Please set or create a playlist first"
        await refresh_player_message(interaction.guild)
        await interaction.followup.send("Please set or create a playlist first.", ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.current = None
    state.force_song = None
    await refill_from_favorite_playlist(interaction.guild.id)
    state.last_action = f"Starting playlist: {state.favorite_playlist_name}"
    persist_guild_playlist(interaction.guild.id)
    await maybe_start_playback(interaction.guild)
    ok = await refresh_player_message(interaction.guild)

    if ok:
        await interaction.followup.send("Playlist started.", ephemeral=True)
    else:
        await interaction.followup.send("Playlist started, but UI panel could not be rendered.", ephemeral=True)


@bot.tree.command(name="autoplayplaylist", description="Turn playlist autoplay on or off")
@app_commands.describe(mode="on or off")
async def slash_autoplayplaylist(interaction: discord.Interaction, mode: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    state = get_state(interaction.guild.id)
    mode = mode.lower()

    if mode == "on":
        state.playlist_autoplay = True
    elif mode == "off":
        state.playlist_autoplay = False
    else:
        await interaction.response.send_message("Use only: on or off", ephemeral=True)
        return

    persist_guild_playlist(interaction.guild.id)
    state.last_action = f"Playlist autoplay {'ON' if state.playlist_autoplay else 'OFF'}"

    await interaction.response.defer()
    ok = await refresh_player_message(interaction.guild)

    if ok:
        await interaction.followup.send(f"Playlist autoplay {'ON' if state.playlist_autoplay else 'OFF'}", ephemeral=True)
    else:
        await interaction.followup.send("Autoplay changed, but UI panel could not be rendered.", ephemeral=True)


@bot.tree.command(name="deleteplaylist", description="Delete a saved playlist")
@app_commands.describe(playlist_name="Exact playlist name")
async def slash_deleteplaylist(interaction: discord.Interaction, playlist_name: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    if not delete_playlist_by_name(state, playlist_name):
        await interaction.response.send_message("Playlist not found.", ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Deleted playlist: {playlist_name}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Deleted playlist: {playlist_name}", ephemeral=True)


@bot.tree.command(name="createplaylist", description="Create an empty custom playlist")
@app_commands.describe(playlist_name="Playlist name")
async def slash_createplaylist(interaction: discord.Interaction, playlist_name: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    if playlist_name in state.saved_playlist_library:
        await interaction.response.send_message("A playlist with this name already exists.", ephemeral=True)
        return

    create_or_update_playlist(
        state=state,
        playlist_name=playlist_name,
        tracks=[],
        ptype="custom",
        url=None,
        creator="manual"
    )

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Created empty playlist: {playlist_name}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Created playlist: {playlist_name}", ephemeral=True)


@bot.tree.command(name="addtoplaylist", description="Add a song query to a custom playlist")
@app_commands.describe(playlist_name="Playlist name", song_query="Song name")
async def slash_addtoplaylist(interaction: discord.Interaction, playlist_name: str, song_query: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    if not add_song_to_custom_playlist(state, playlist_name, song_query):
        await interaction.response.send_message("Playlist not found.", ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Added song to {playlist_name}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Added song to {playlist_name}", ephemeral=True)


@bot.tree.command(name="removefromplaylist", description="Remove a song from a custom playlist")
@app_commands.describe(playlist_name="Playlist name", index="Song number")
async def slash_removefromplaylist(interaction: discord.Interaction, playlist_name: str, index: int):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    ok, msg = remove_song_from_custom_playlist(state, playlist_name, index)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Removed from {playlist_name}: {msg[:60]}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Removed song from {playlist_name}", ephemeral=True)


@bot.tree.command(name="viewplaylist", description="View songs in a saved playlist")
@app_commands.describe(playlist_name="Playlist name")
async def slash_viewplaylist(interaction: discord.Interaction, playlist_name: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    if playlist_name not in state.saved_playlist_library:
        await interaction.response.send_message("Playlist not found.", ephemeral=True)
        return

    playlist = state.saved_playlist_library[playlist_name]
    tracks = playlist.get("tracks", [])

    if not tracks:
        await interaction.response.send_message(f"Playlist **{playlist_name}** is empty.", ephemeral=True)
        return

    lines = []
    for i, song in enumerate(tracks[:20], start=1):
        lines.append(f"{i}. {song}")

    embed = discord.Embed(
        title=f"Playlist: {playlist_name}",
        description="\n".join(lines),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Total songs: {len(tracks)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="singerplaylist", description="Create a playlist from singer name")
@app_commands.describe(singer_name="Singer name")
async def slash_singerplaylist(interaction: discord.Interaction, singer_name: str):
    if not ensure_guild_interaction(interaction):
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return
    state = get_state(interaction.guild.id)

    try:
        name, tracks, ptype = await build_singer_playlist(singer_name, limit=20)
    except Exception as e:
        await interaction.response.send_message(f"Singer playlist error: {e}", ephemeral=True)
        return

    final_name = name
    if final_name in state.saved_playlist_library:
        final_name = make_unique_playlist_name(state, final_name)

    create_or_update_playlist(
        state=state,
        playlist_name=final_name,
        tracks=tracks,
        ptype=ptype,
        url=None,
        creator="singer"
    )

    set_text_channel(state, interaction.channel)
    state.player_channel_id = interaction.channel.id
    state.last_action = f"Singer playlist created: {final_name}"
    persist_guild_playlist(interaction.guild.id)

    await interaction.response.defer()
    await refresh_player_message(interaction.guild)
    await interaction.followup.send(f"Singer playlist created: {final_name}", ephemeral=True)

keep_alive()
bot.run(TOKEN)