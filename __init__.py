import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
import webbrowser

from gi.repository import GLib
from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin


# METADATA

PLUGIN_NAME = "spotseek"
PLUGIN_DESCRIPTION = (
    "Downloads Spotify public playlists via soulseek.\n\n"
    "Paste your Spotify playlist URL below, enable 'Start Job', and click Apply."
)
PLUGIN_VERSION = "1.3"
PLUGIN_API_VERSION = 7
PLUGIN_AUTHORS = ["jcjakec"]


# SETTINGS

SETTINGS = {
    "playlist_url": "",
    "start_job": False,
    "preferred_format": "any",
    "min_bitrate": 128,
    "download_subfolder": "spotseek",
}

SETTINGS_METADATA = {
    "playlist_url": {
        "label": "Playlist URL",
        "description": "Public Spotify playlist link:",
        "type": "str",
    },
    "start_job": {
        "label": "Start / Stop Job",
        "description": (
            "Toggle ON and click Apply to start.\n"
            "Toggle OFF and click Apply to stop."
        ),
        "type": "bool",
    },
    "preferred_format": {
        "label": "Preferred Format",
        "description": (
            "File type preference:\n"
            "lossless | lossy | any"
        ),
        "type": "str",
    },
    "min_bitrate": {
        "label": "Minimum Bitrate (kbps)",
        "description": (
            "MP3/lossy results below this bitrate are skipped.\n"
            "Set to 0 to accept any bitrate."
        ),
        "type": "int",
    },
    "download_subfolder": {
        "label": "Download Subfolder",
        "description": (
            "Optional nicotine download subfolder:\n"
            "Leave blank to use default download location."
        ),
        "type": "str",
    },
}


# CONSTANTS

AUDIO_EXTS = {
    "mp3", "flac", "ogg", "opus", "aac", "m4a",
    "wav", "aiff", "wv", "ape", "alac",
}

LOSSLESS_EXTS = {
    "flac", "wav", "aiff", "wv", "ape", "alac",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on",
    "at", "to", "ft", "feat", "by", "with", "de", "la", "le", "el",
}

MIX_TERMS = {
    "mix", "remix", "rmx", "dub", "edit", "version", "instrumental",
    "vocal", "acapella", "pella", "vip", "bootleg", "rework", "mashup",
}

RECONNECT_GRACE = 5.0
RECONNECT_TIMEOUT = 120.0


# TEXT HELPERS

def normalise(value: str) -> str:
    if not value:
        return ""
    value = str(value)
    value = unicodedata.normalize("NFD", value)
    value = "".join(
        char for char in value if unicodedata.category(char) != "Mn"
    )
    value = re.sub(
        r"\s*[\(\[]?\s*(?:feat\.?|ft\.?)\b.*?[\)\]]?",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = value.replace("-", " ")
    value = value.replace("_", " ")
    value = value.replace(";", " ")
    value = value.replace("&", " ")
    value = value.replace(",", " ")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def strip_track_number(value: str) -> str:
    value = re.sub(r"^\s*\d+[\s\.\-_]+", "", value)
    return normalise(value)


def word_set(value: str) -> set:
    return {w for w in normalise(value).split() if w not in STOPWORDS}


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def fmt_size(size: int) -> str:
    size = safe_int(size)
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.2f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def format_track(track: dict) -> str:
    artist = (track.get("artist", "") or "").strip()
    title = (track.get("title", "") or "").strip()
    if artist and title:
        return f"{artist} - {title}"
    return artist or title or "Unknown track"


# FILEINFO HELPERS

def get_filename(fileinfo) -> str:
    if not isinstance(fileinfo, (list, tuple)) or len(fileinfo) < 2:
        return ""
    return str(fileinfo[1])


def get_filesize(fileinfo) -> int:
    if not isinstance(fileinfo, (list, tuple)) or len(fileinfo) < 3:
        return 0
    return safe_int(fileinfo[2])


def get_attributes(fileinfo) -> dict:
    if not isinstance(fileinfo, (list, tuple)):
        return {}
    if len(fileinfo) >= 5:
        attrs = fileinfo[4]
        if isinstance(attrs, dict):
            return attrs
        if isinstance(attrs, (list, tuple)):
            result = {}
            for item in attrs:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    result[item[0]] = item[1]
            return result
    return {}


def get_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower().lstrip(".")


def get_bitrate(fileinfo) -> int:
    if get_extension(get_filename(fileinfo)) in LOSSLESS_EXTS:
        return 0
    attrs = get_attributes(fileinfo)
    return safe_int(attrs.get(0, attrs.get("bitrate", attrs.get("BITRATE", 0))))


def quality_score(fileinfo) -> int:
    size = get_filesize(fileinfo)
    bitrate = get_bitrate(fileinfo)
    extension = get_extension(get_filename(fileinfo))
    if extension in LOSSLESS_EXTS:
        return 10_000_000 + size
    if extension in {"ogg", "opus", "m4a", "aac"}:
        return 5_000_000 + bitrate * 1_000 + size // 1_000
    if extension == "mp3":
        return bitrate * 10_000 + size // 1_000
    return size // 1_000


# SPOTIFY

def fetch_spotify(url: str, log_callback) -> list:
    match = re.search(
        r"spotify\.com/(?:embed/)?playlist/([A-Za-z0-9]+)",
        url, flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"spotify:playlist:([A-Za-z0-9]+)", url, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Could not parse Spotify playlist ID.")
    playlist_id = match.group(1)
    log_callback(f"Loading Spotify playlist {playlist_id}...")
    request = urllib.request.Request(
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"Failed to load Spotify playlist: {exc}") from exc
    if len(html) < 5000:
        raise ValueError(
            "Spotify returned an unexpectedly short page "
            "(embed structure may have changed, or playlist is private)."
        )
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ValueError("Spotify playlist data could not be found (embed structure may have changed).")
    try:
        data = json.loads(match.group(1))
    except Exception as exc:
        raise ValueError(f"Failed to parse Spotify playlist data: {exc}") from exc
    tracks = []
    entity = (
        data.get("props", {}).get("pageProps", {})
        .get("state", {}).get("data", {}).get("entity", {})
    )
    for item in entity.get("trackList", []):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name") or ""
        artist = item.get("subtitle") or ""
        if not artist:
            artists = item.get("artists")
            if isinstance(artists, list):
                artist = ", ".join(str(a.get("name", "")) for a in artists if isinstance(a, dict))
        if title:
            tracks.append({"title": str(title).strip(), "artist": str(artist).strip(), "album": ""})
    if not tracks:
        raise ValueError(
            "Spotify playlist loaded, but no tracks were found. "
            "The playlist may be empty, private, or the embed structure may have changed."
        )
    return tracks


# HTML REPORT

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>spotseek report - nicotine+</title>
<style>
  :root {{
    --gtk-bg: #2d2d2d;
    --gtk-header: #242424;
    --gtk-card: #353535;
    --gtk-border: #454545;
    --gtk-text: #eeeeee;
    --gtk-text-secondary: #b0b0b0;
    --gtk-accent: #3584e4;
    --gtk-green: #2ec27e;
    --gtk-yellow: #f5c211;
    --gtk-red: #e01b24;
    --gtk-row-hover: #3a3a3a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background-color: var(--gtk-bg);
    color: var(--gtk-text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
    font-size: 12px;
    line-height: 1.4;
    padding: 12px;
  }}
  a {{ color: var(--gtk-accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .nicotine-header {{
    background: var(--gtk-header);
    border: 1px solid var(--gtk-border);
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border-bottom: none;
    padding: 8px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }}
  .app-title {{
    font-weight: 700;
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }}
  .app-title img {{ height: 20px; width: auto; max-width: 120px; object-fit: contain; }}
  .gen-at {{ font-size: 11px; color: var(--gtk-text-secondary); white-space: nowrap; }}
  .stats-bar {{
    display: flex;
    gap: 16px;
    background: var(--gtk-card);
    border: 1px solid var(--gtk-border);
    border-bottom-left-radius: 6px;
    border-bottom-right-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 12px;
    font-size: 12px;
    justify-content: space-between;
  }}
  .stats-bar > div {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .stat-item {{ color: var(--gtk-text-secondary); }}
  .stat-item span {{ font-weight: bold; color: var(--gtk-text); margin-left: 4px; }}
  .stat-item.green span {{ color: var(--gtk-green); }}
  .stat-item.red span {{ color: var(--gtk-red); }}
  .stat-item.yellow span {{ color: var(--gtk-yellow); }}
  .log-block {{
    background: #1e1e1e;
    border: 1px solid var(--gtk-border);
    border-radius: 6px;
    padding: 10px;
    margin-bottom: 16px;
    margin-top: 16px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 11px;
    color: #d4d4d4;
    max-height: 150px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .toolbar {{ display: flex; gap: 6px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }}
  .btn {{
    background: var(--gtk-card);
    border: 1px solid var(--gtk-border);
    color: var(--gtk-text);
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
  }}
  .btn:hover {{ background: #404040; }}
  .btn.active {{ background: var(--gtk-accent); border-color: var(--gtk-accent); color: #fff; }}
  .search-input {{
    background: #1e1e1e;
    border: 1px solid var(--gtk-border);
    border-radius: 4px;
    color: var(--gtk-text);
    padding: 4px 8px;
    font-size: 11px;
    flex: 1;
    min-width: 200px;
    outline: none;
  }}
  .search-input:focus {{ border-color: var(--gtk-accent); }}
  .nicotine-table-wrapper {{ width: 100%; overflow-x: auto; border: 1px solid var(--gtk-border); border-radius: 6px; }}
  .nicotine-table {{
    width: 100%;
    min-width: 850px;
    border-collapse: separate;
    border-spacing: 0;
    background: var(--gtk-card);
  }}
  .nicotine-table th {{
    background: var(--gtk-header);
    padding: 6px 10px;
    text-align: left;
    font-size: 10px;
    font-weight: 600;
    color: var(--gtk-text-secondary);
    text-transform: uppercase;
    border-bottom: 1px solid var(--gtk-border);
    white-space: nowrap;
  }}
  .nicotine-table td {{ padding: 6px 10px; border-bottom: 1px solid var(--gtk-border); vertical-align: top; }}
  .nicotine-table tr:last-child td {{ border-bottom: none; }}
  .nicotine-table tr:hover td {{ background: var(--gtk-row-hover); }}
  .row-skip td {{ opacity: 0.55; }}
  .badge {{
    display: inline-block;
    padding: 2px 6px;
    font-size: 9px;
    font-weight: 700;
    border-radius: 10px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }}
  .badge-queued {{ background: rgba(46,194,126,0.15); color: var(--gtk-green); border: 1px solid var(--gtk-green); }}
  .badge-skip {{ background: rgba(224,27,36,0.15); color: var(--gtk-red); border: 1px solid var(--gtk-red); }}
  .format-badge {{
    display: inline-block;
    padding: 1px 4px;
    font-size: 9px;
    font-weight: 600;
    border-radius: 3px;
    background: #454545;
    color: #fff;
    margin-right: 5px;
  }}
  .format-badge.lossless {{ background: #26583d; color: var(--gtk-green); }}
  .file-path {{
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 10px;
    color: var(--gtk-text-secondary);
    word-break: break-all;
    margin-top: 2px;
  }}
  @media (max-width: 600px) {{
    body {{ padding: 8px; }}
    .nicotine-header {{ align-items: flex-start; flex-direction: column; }}
    .stats-bar {{ gap: 8px 16px; }}
  }}
</style>
</head>
<body>

<div class="nicotine-header">
  <div class="app-title">
    <img src="../logo.png" alt="logo" onerror="this.style.display='none'">
    spotseek
  </div>
  <div class="gen-at">{generated_at}</div>
</div>

<div class="stats-bar">
  <div>
    <div class="stat-item">Total Tracks: <span>{total}</span></div>
    <div class="stat-item green">Queued: <span>{queued}</span></div>
    <div class="stat-item{red_class}">Unavailable: <span>{failed}</span></div>
    <div class="stat-item{rate_class}">Match Rate: <span>{s_rate}%</span></div>
    <div class="stat-item yellow">Elapsed: <span>{elapsed}</span></div>
  </div>
  <a href="{playlist_url}" target="_blank" rel="noopener noreferrer">Playlist Source</a>
</div>

<div class="log-block" id="logBlock">{session_log}</div>

<div class="toolbar">
  <button class="btn active" onclick="setFilter('all', this)">All</button>
  <button class="btn" onclick="setFilter('queued', this)">Queued</button>
  <button class="btn" onclick="setFilter('skip', this)">Unavailable</button>
  <button class="btn" onclick="setFilter('lossless', this)">Lossless</button>
  <button class="btn" onclick="setFilter('lossy', this)">Lossy</button>
  <input type="text" class="search-input" id="trackSearch"
    placeholder="Filter by title, artist, or user..." autocomplete="off">
</div>

<div class="nicotine-table-wrapper">
  <table class="nicotine-table" id="trackTable">
    <thead>
      <tr>
        <th style="width:30px;">#</th>
        <th>Track Details</th>
        <th style="width:80px;">Status</th>
        <th>Match / File Path</th>
        <th style="width:110px;">Source User</th>
        <th style="width:80px;">Candidates</th>
      </tr>
    </thead>
    <tbody>{track_rows}</tbody>
  </table>
</div>

<script>
  let currentFilter = "all";
  function setFilter(filter, btn) {{
    currentFilter = filter;
    document.querySelectorAll(".btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    applyFilters();
  }}
  function applyFilters() {{
    const q = (document.getElementById("trackSearch")?.value || "").trim().toLowerCase();
    document.querySelectorAll("#trackTable tbody tr").forEach(row => {{
      const status = row.dataset.status || "";
      const format = row.dataset.format || "";
      const text   = (row.dataset.search || "").toLowerCase();
      let ok = false;
      switch (currentFilter) {{
        case "queued":   ok = status === "queued"; break;
        case "skip":     ok = status === "skip";   break;
        case "lossless": ok = format === "lossless"; break;
        case "lossy":    ok = format === "lossy";  break;
        default:         ok = true;
      }}
      row.style.display = ok && (!q || text.includes(q)) ? "" : "none";
    }});
  }}
  document.addEventListener("DOMContentLoaded", () => {{
    document.getElementById("trackSearch")?.addEventListener("input", applyFilters);
    const lb = document.getElementById("logBlock");
    if (lb) lb.scrollTop = lb.scrollHeight;
    applyFilters();
  }});
</script>
</body>
</html>"""


def _build_track_rows(track_results: list) -> str:
    rows = []
    for r in sorted(track_results, key=lambda r: r.get("index", 0)):
        num        = r.get("index", 0) + 1
        artist     = r.get("artist", "")
        title      = r.get("title", "") or r.get("track_name", "Unknown")
        status     = r.get("status", "skip")
        extension  = r.get("extension", "")
        size_str   = r.get("size_str", "")
        filename   = r.get("filename", "")
        username   = r.get("username", "")
        candidates = r.get("candidates", 0)
        query      = r.get("query", "")

        ext_lower   = extension.lower()
        ext_upper   = ext_lower.upper() if ext_lower else "SKIP"
        is_lossless = ext_lower in LOSSLESS_EXTS
        fmt_data    = "lossless" if is_lossless else ("lossy" if ext_lower else "none")
        search_data = f"{artist} {title} {query} {username} {filename}".lower()
        row_class   = "row-skip" if status == "skip" else ""

        badge = (
            '<span class="badge badge-queued">QUEUED</span>'
            if status == "queued" else
            '<span class="badge badge-skip">SKIPPED</span>'
        )

        ext_badge = (
            f'<span class="{"format-badge lossless" if is_lossless else "format-badge"}">{ext_upper}</span>'
            if ext_lower else ""
        )

        if status == "queued":
            format_cell = f'{ext_badge} {size_str}<br><div class="file-path">{filename}</div>'
            user_cell   = f'<span style="font-weight:600;">{username}</span>'
        else:
            format_cell = '<span style="color:var(--gtk-text-secondary);">No suitable match found</span>'
            user_cell   = '<span style="color:var(--gtk-text-secondary);">-</span>'

        cand_cell = (
            f'<span>{candidates} files</span>' if candidates else
            '<span style="color:var(--gtk-text-secondary);">0</span>'
        )

        rows.append(
            f'<tr class="{row_class}" data-status="{status}" data-format="{fmt_data}" data-search="{search_data}">'
            f'<td style="color:var(--gtk-text-secondary);text-align:right;">{num}</td>'
            f'<td><div style="font-weight:600;">{title}</div>'
            f'<div style="font-size:10px;color:var(--gtk-text-secondary);">{artist}</div></td>'
            f'<td>{badge}</td><td>{format_cell}</td>'
            f'<td>{user_cell}</td><td>{cand_cell}</td></tr>'
        )
    return "\n".join(rows)


def generate_html_report(
    playlist_url: str,
    version: str,
    total: int,
    queued: int,
    failed: int,
    elapsed_str: str,
    session_log: str,
    track_results: list,
) -> str:
    success_rate = int(queued / total * 100) if total > 0 else 0
    red_class  = " red" if failed > 0 else ""
    rate_class = " green" if success_rate >= 80 else (" yellow" if success_rate >= 50 else " red")
    log_escaped = session_log.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTML_TEMPLATE.format(
        playlist_url=playlist_url,
        total=total,
        queued=queued,
        failed=failed,
        s_rate=success_rate,
        elapsed=elapsed_str,
        red_class=red_class,
        rate_class=rate_class,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        session_log=log_escaped,
        track_rows=_build_track_rows(track_results),
    )


# PLUGIN

class Plugin(BasePlugin):

    settings = SETTINGS
    metasettings = SETTINGS_METADATA

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._running = False
        self._worker = None
        self._monitor_running = False
        self._monitor_thread = None
        self._events_connected = []
        self._job_started = None
        self._job_total = 0
        self._job_completed = 0
        self._job_found = 0
        self._job_queued = 0
        self._job_failed = 0
        self._session_log_lines = []
        self._track_results = []
        self._job_playlist_url = ""
        self._job_output_folder = ""
        self._active_searches = {}
        self._server_connected = True
        self._connected_event = threading.Event()
        self._connected_event.set()


# LOGGING

    def _safe_log(self, message: str):
        try:
            GLib.idle_add(self.log, str(message))
        except Exception:
            try:
                self.log(str(message))
            except Exception:
                pass

    def _log_status(self, level: str, message: str):
        line = f"{level:<9} {message}"
        self._safe_log(line)
        with self._lock:
            self._session_log_lines.append(line)

    def _log_verbose(self, level: str, message: str):
        with self._lock:
            self._session_log_lines.append(f"{level:<9} {message}")

    def _format_duration(self, seconds) -> str:
        seconds = max(0, int(seconds))
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# LIFECYCLE

    def _connect_event(self, name: str, handler):
        events.connect(name, handler)
        self._events_connected.append((name, handler))

    def loaded_notification(self, *args):
        self._log_status("START", f"spotseek {PLUGIN_VERSION} loaded")
        try:
            self._connect_event("file-search-response", self._on_search_response)
            self._connect_event("server-login",         self._on_server_login)
            self._connect_event("server-disconnect",    self._on_server_disconnect)
            self._log_status("READY", "Connected to soulseek event bus")
        except Exception as exc:
            self._log_status("ERROR", f"Could not connect to events: {type(exc).__name__}: {exc}")
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_settings_loop, name="spotseek_monitor", daemon=True
        )
        self._monitor_thread.start()

    def unloaded_notification(self, *args):
        self._log_status("STOP", "spotseek shutting down")
        self._monitor_running = False
        self._running = False
        self._connected_event.set()
        with self._lock:
            tokens = list(self._active_searches.keys())
            self._active_searches.clear()
        for token in tokens:
            try:
                self.core.search.remove_search(token)
            except Exception:
                pass
        for name, handler in self._events_connected:
            try:
                events.disconnect(name, handler)
            except Exception:
                pass
        self._events_connected.clear()


# SERVER CONNECTION

    def _on_server_disconnect(self, *args):
        with self._lock:
            already = not self._server_connected
            self._server_connected = False
        self._connected_event.clear()
        if not already and self._running:
            self._log_status("WARN", "Server disconnected — searches paused until reconnect")

    def _on_server_login(self, *args):
        with self._lock:
            was_down = not self._server_connected
            self._server_connected = True
        self._connected_event.set()
        if was_down and self._running:
            self._log_status("READY", f"Server reconnected — resuming in {RECONNECT_GRACE:.0f}s")

    def _wait_for_connection(self) -> bool:
        if self._server_connected:
            return True
        deadline = time.monotonic() + RECONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if not self._running:
                return False
            self._connected_event.wait(timeout=1.0)
            if self._server_connected:
                grace_end = time.monotonic() + RECONNECT_GRACE
                while time.monotonic() < grace_end:
                    if not self._running:
                        return False
                    time.sleep(0.25)
                return True
        self._log_status("ERROR", f"Gave up waiting for server reconnect after {RECONNECT_TIMEOUT:.0f}s")
        return False


# SETTINGS MONITOR

    def _monitor_settings_loop(self):
        while self._monitor_running:
            time.sleep(0.5)
            try:
                raw = self.settings.get("start_job", False)
                is_start = str(raw).lower() in ("true", "1", "yes", "on")
                if is_start and not self._running:
                    url = (self.settings.get("playlist_url", "") or "").strip()
                    if not url:
                        self._log_status("ERROR", "Playlist URL is empty")
                        self.settings["start_job"] = False
                    else:
                        self._start_job(url)
                elif not is_start and self._running:
                    self._log_status("STOP", "Job cancelled by user")
                    self._running = False
                    self._connected_event.set()
            except Exception as exc:
                self._log_status("ERROR", f"Settings monitor error: {type(exc).__name__}: {exc}")


# START JOB

    def _start_job(self, url: str):
        if self._worker and self._worker.is_alive():
            return
        self._running = True
        self._job_started = time.monotonic()
        self._job_total = 0
        self._job_completed = 0
        self._job_found = 0
        self._job_queued = 0
        self._job_failed = 0
        self._session_log_lines = []
        self._track_results = []
        self._job_playlist_url = url
        self._job_output_folder = ""
        with self._lock:
            self._active_searches.clear()
        self._worker = threading.Thread(
            target=self._job, args=(url,), name="spotseek_worker", daemon=True
        )
        self._worker.start()


# MAIN JOB

    def _job(self, url: str):
        try:
            self._log_status("START", "Starting playlist download job")
            tracks = self._load_playlist(url)
            if tracks is None:
                return
            for index, track in enumerate(tracks):
                if not self._running:
                    break
                if not self._wait_for_connection():
                    break
                try:
                    self._search_one(index, track)
                except Exception as exc:
                    self._record_failed(index, track, "")
                    self._log_status("ERROR", f"Track {index + 1} error: {type(exc).__name__}: {exc}")
                time.sleep(0.2)
            self._finish_job()
        except Exception as exc:
            self._log_status("ERROR", f"JOB FAILED: {type(exc).__name__}: {exc}")
        finally:
            self._running = False
            try:
                self.settings["start_job"] = False
            except Exception:
                pass


# JOB HELPERS

    def _load_playlist(self, url: str):
        url = url.strip()
        if "spotify.com" not in url.lower():
            self._log_status("ERROR", "Unsupported URL. Only Spotify playlists are supported.")
            return None
        try:
            tracks = fetch_spotify(url, lambda msg: self._log_verbose("SOURCE", msg))
        except Exception as exc:
            self._log_status("ERROR", f"Could not load playlist: {exc}")
            return None
        self._job_total = len(tracks)
        self._log_status("READY", f"{self._job_total} tracks found in playlist")
        return tracks if tracks else None

    def _record_failed(self, index: int, track: dict, query: str):
        with self._lock:
            self._job_failed += 1
            self._job_completed += 1
            self._track_results.append({
                "index": index, "track_name": format_track(track),
                "artist": track.get("artist", ""), "title": track.get("title", ""),
                "status": "skip", "extension": "", "size_str": "", "filename": "",
                "username": "", "candidates": 0, "score": 0, "query": query,
            })

    def _finish_job(self):
        elapsed = time.monotonic() - self._job_started
        if self._running:
            self._log_status("DONE", "Playlist processing complete")
            self._log_status(
                "SUMMARY",
                f"{self._job_total} tracks  |  {self._job_queued} queued  |  "
                f"{self._job_failed} unavailable  |  {self._format_duration(elapsed)} elapsed",
            )
        else:
            self._log_status("STOP", "Playlist job stopped")
            self._log_status(
                "SUMMARY",
                f"{self._job_completed}/{self._job_total} processed  |  "
                f"{self._job_queued} queued  |  {self._format_duration(elapsed)} elapsed",
            )
        self._write_html_report(elapsed)


# HTML REPORT

    def _write_html_report(self, elapsed_seconds):
        try:
            html = generate_html_report(
                playlist_url=self._job_playlist_url,
                version=PLUGIN_VERSION,
                total=self._job_total,
                queued=self._job_queued,
                failed=self._job_failed,
                elapsed_str=self._format_duration(elapsed_seconds),
                session_log="\n".join(self._session_log_lines),
                track_results=self._track_results,
            )
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            reports_dir = os.path.join(plugin_dir, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            report_path = os.path.join(reports_dir, f"spotseek_report_{stamp}.html")
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            self._log_status("REPORT", f"HTML report saved: {report_path}")
            try:
                webbrowser.open(f"file://{os.path.abspath(report_path)}")
            except Exception as exc:
                self._log_status("WARN", f"Could not open browser: {exc}")
        except Exception as exc:
            self._log_status("ERROR", f"Could not write HTML report: {type(exc).__name__}: {exc}")


# SEARCH ONE TRACK

    def _search_one(self, index: int, track: dict):
        artist = track.get("artist", "") or ""
        title  = track.get("title",  "") or ""
        first_artist = re.split(r"[,&]", artist)[0].strip()
        core_title   = re.split(r" \- |\(|\[", title)[0].strip()
        query = f"{normalise(first_artist)} {normalise(core_title)}".strip()

        if not query:
            self._record_failed(index, track, "")
            self._log_status("SKIP", "Track has no searchable artist/title")
            return

        self._log_verbose("SEARCH", f"{index + 1}/{self._job_total}  {format_track(track)}")
        self._log_verbose("QUERY",  query)

        search_data = {
            "idx": index, "track": track, "query": query,
            "title_words": word_set(title), "core_title_words": word_set(core_title),
            "artist_words": word_set(artist),
            "preferred_format": str(self.settings.get("preferred_format", "any") or "any").lower(),
            "min_bitrate": safe_int(self.settings.get("min_bitrate", 0), 0),
            "token": None, "results": [], "responses": 0, "users": set(),
            "last_hit": time.monotonic(), "search_error": None,
            "token_event": threading.Event(),
        }

        def execute_search():
            try:
                sm = getattr(self.core, "search", None)
                if sm is None:
                    raise RuntimeError("core.search is unavailable.")
                sm.do_search(query, "global")
                token = getattr(sm, "token", None)
                if token is None:
                    raise RuntimeError("Nicotine+ did not provide a search token.")
                with self._lock:
                    search_data["token"] = token
                    self._active_searches[token] = search_data
                search_data["token_event"].set()
                self._log_verbose("SEARCH", "Search active")
            except Exception as exc:
                self._log_status("ERROR", f"Could not start search: {type(exc).__name__}: {exc}")
                with self._lock:
                    search_data["search_error"] = str(exc)
                search_data["token_event"].set()
            return False

        GLib.idle_add(execute_search)
        search_data["token_event"].wait(timeout=3.0)

        max_duration = 8.0
        quiet_period = 1.0
        last_report  = 0
        start_time   = time.monotonic()

        while time.monotonic() - start_time < max_duration:
            if not self._running:
                break
            time.sleep(0.25)
            with self._lock:
                n_results = len(search_data["results"])
                n_users   = len(search_data["users"])
                last_hit  = search_data["last_hit"]
                error     = search_data.get("search_error")
            if error:
                break
            now = time.monotonic()
            if n_results > 0 and now - last_report >= 1.0:
                self._log_verbose("SEARCH", f"{n_results} candidates from {n_users} users")
                last_report = now
            if n_results > 0 and now - last_hit >= quiet_period:
                break

        with self._lock:
            results = list(search_data["results"])
            token   = search_data.get("token")
            n_users = len(search_data["users"])
            if token is not None:
                self._active_searches.pop(token, None)

        if results:
            with self._lock:
                self._job_found += 1
            self._log_verbose("FOUND", f"{len(results)} suitable files from {n_users} users")
        else:
            self._log_verbose("FOUND", "No suitable files found")

        self._pick_and_download(index, track, results, token, query, artist, title)

        with self._lock:
            completed = self._job_completed
            total     = self._job_total
        percent = int(completed / total * 100) if total > 0 else 0
        elapsed = time.monotonic() - self._job_started
        self._safe_log(
            f"[{completed}/{total}  {percent}%  {self._format_duration(elapsed)}]  {format_track(track)}"
        )


# SEARCH RESPONSE

    def _on_search_response(self, msg, *args):
        try:
            payload = msg
            if hasattr(msg, "args") and msg.args:
                payload = msg.args[0]
            elif hasattr(msg, "data"):
                payload = msg.data
            elif isinstance(msg, (list, tuple)) and msg:
                payload = msg[0]

            def gv(obj, attr, default=None):
                return obj.get(attr, default) if isinstance(obj, dict) else getattr(obj, attr, default)

            token = gv(payload, "token")
            with self._lock:
                info = self._active_searches.get(token)
            if info is None:
                return

            username     = gv(payload, "username", "")
            shares       = gv(payload, "list") or gv(payload, "files") or []
            if not shares:
                return
            free_slots   = bool(gv(payload, "freeulslots", False) or gv(payload, "has_free_upload_slot", False))
            upload_speed = safe_int(gv(payload, "ulspeed", 0) or gv(payload, "upload_speed", 0))
            queue_length = safe_int(gv(payload, "inqueue", 0) or gv(payload, "queue_length", 0))

            accepted = sum(
                1 for fi in shares
                if self._process_candidate(info, username, fi, free_slots, upload_speed, queue_length)
            )
            with self._lock:
                info["responses"] += 1
                if username:
                    info["users"].add(username)
            if accepted:
                self._log_verbose(
                    "RESULT",
                    f"{username} returned {accepted} suitable {'file' if accepted == 1 else 'files'}",
                )
        except Exception as exc:
            self._log_status("ERROR", f"Search response error: {type(exc).__name__}: {exc}")


# PROCESS CANDIDATE

    def _process_candidate(
        self, info, username, fileinfo, free_slots, upload_speed, queue_length
    ) -> bool:
        full_path = get_filename(fileinfo)
        size      = get_filesize(fileinfo)
        if not full_path or size < 100_000:
            return False
        filename_only = os.path.basename(full_path)
        if filename_only.startswith("._"):
            return False
        extension = get_extension(filename_only)
        if extension not in AUDIO_EXTS:
            return False
        pref = (info.get("preferred_format", "any") or "any").lower()
        if pref == "lossless" and extension not in LOSSLESS_EXTS:
            return False
        if pref == "lossy" and extension in LOSSLESS_EXTS:
            return False
        bitrate     = get_bitrate(fileinfo)
        min_bitrate = safe_int(info.get("min_bitrate", 0))
        if min_bitrate > 0 and extension == "mp3" and 0 < bitrate < min_bitrate:
            return False
        fname_words      = word_set(strip_track_number(filename_only))
        full_path_words  = word_set(full_path)
        title_words      = info.get("title_words", set())
        core_title_words = info.get("core_title_words", set())
        artist_words     = info.get("artist_words", set())
        if core_title_words and not core_title_words.issubset(fname_words):
            return False
        if artist_words and not (artist_words & full_path_words):
            return False
        score = quality_score(fileinfo)
        score += len(title_words & fname_words) * 15_000_000
        if title_words and title_words.issubset(fname_words):
            score += 50_000_000
        if (fname_words & MIX_TERMS) - title_words:
            score -= 20_000_000
        score += len(artist_words & fname_words) * 500_000
        if free_slots:             score += 500_000
        if upload_speed > 100_000: score += 200_000
        if queue_length:           score -= queue_length * 100
        with self._lock:
            if any(c[1] == username and get_filename(c[2]) == full_path for c in info["results"]):
                return False
            info["results"].append((score, username, fileinfo))
            info["last_hit"] = time.monotonic()
        return True


# SELECT AND DOWNLOAD

    def _pick_and_download(self, index, track, results, token, query, artist, title):
        track_name = format_track(track)

        if not results:
            self._log_status("SKIP", f"{track_name} : no match found")
            if token is not None:
                GLib.idle_add(self._close_search, token)
            self._record_failed(index, track, query)
            return

        results.sort(key=lambda x: x[0], reverse=True)
        score, username, fileinfo = results[0]
        filename  = get_filename(fileinfo)
        size      = get_filesize(fileinfo)
        extension = get_extension(filename)

        quality_type = "LOSSLESS" if extension in LOSSLESS_EXTS else "LOSSY"
        self._log_status("BEST", f"{extension.upper()}  {fmt_size(size)}  {quality_type}  -  {username}")
        self._log_verbose("FILE", filename)

        subfolder = (self.settings.get("download_subfolder", "") or "").strip()
        try:
            base_folder = self.core.downloads.get_default_download_folder(username)
        except Exception:
            try:
                base_folder = self.config.data["transfers"]["downloaddir"]
            except Exception:
                base_folder = os.path.expanduser("~/Music")

        output_folder = os.path.join(base_folder, subfolder) if subfolder else base_folder

        with self._lock:
            if not self._job_output_folder:
                self._job_output_folder = output_folder

        attributes = get_attributes(fileinfo)

        with self._lock:
            self._track_results.append({
                "index": index, "track_name": track_name, "artist": artist, "title": title,
                "status": "queued", "extension": extension, "size_str": fmt_size(size),
                "filename": filename, "username": username, "candidates": len(results),
                "score": score, "query": query,
            })

        enqueue_done = threading.Event()
        enqueue_ok   = [False]

        def enqueue_file():
            try:
                os.makedirs(output_folder, exist_ok=True)
            except Exception as exc:
                self._log_status("WARN", f"Could not create output folder: {exc}")
            try:
                self._log_verbose("QUEUE", f"{filename}  -  {username}")
                self.core.downloads.enqueue_download(
                    username, filename,
                    folder_path=output_folder,
                    size=size,
                    file_attributes=attributes,
                )
                self._log_verbose("QUEUED", filename)
                enqueue_ok[0] = True
            except Exception as exc:
                self._log_status("ERROR", f"Could not queue download: {type(exc).__name__}: {exc}")
            finally:
                enqueue_done.set()
            return False

        GLib.idle_add(enqueue_file)
        enqueue_done.wait(timeout=5.0)

        with self._lock:
            self._job_completed += 1
            if enqueue_ok[0]:
                self._job_queued += 1
            else:
                self._job_failed += 1

        if token is not None:
            GLib.idle_add(self._close_search, token)


# CLOSE SEARCH

    def _close_search(self, token):
        try:
            self.core.search.remove_search(token)
        except Exception as exc:
            self._log_status("WARN", f"Could not close search: {type(exc).__name__}: {exc}")
        return False