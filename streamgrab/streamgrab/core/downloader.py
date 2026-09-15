"""
Core video/audio download logic built on yt-dlp.

Kept separate from the GUI so this can be tested and used
independently — the GUI just calls into this module.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

try:
    import imageio_ffmpeg
    _HAS_BUNDLED_FFMPEG = True
except ImportError:
    _HAS_BUNDLED_FFMPEG = False


def _find_ffmpeg_path() -> Optional[str]:
    """Locate an ffmpeg binary to use, preferring one already on the
    system PATH (so a user's own install/version is respected), and
    falling back to the ffmpeg binary bundled via the imageio-ffmpeg
    pip package if no system install is found. imageio-ffmpeg ships a
    real, statically-linked ffmpeg binary for Windows/Mac/Linux and is
    installed automatically as a normal dependency of this package —
    so a fresh `pip install streamgrab` works out of the box without
    the user needing to separately install ffmpeg themselves.
    Returns None if neither is available.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    if _HAS_BUNDLED_FFMPEG:
        return imageio_ffmpeg.get_ffmpeg_exe()
    return None


class _QuietLogger:
    """A yt-dlp logger that drops noise while still surfacing real
    problems.

    Filtered messages, both confirmed by tracing yt-dlp's actual source
    code (not assumed):

    1. "Deprecated Feature: Support for Python version ... has been
       deprecated" — yt-dlp's deprecated_feature() prints this via
       to_stderr(), which — when a logger is configured (as we do) —
       routes to logger.error(), not logger.warning(). Filtered on
       both methods since it can arrive through either.
    2. "YouTube Music is not directly supported. Redirecting to ..." —
       informational: yt-dlp is auto-converting a music.youtube.com
       URL and continuing normally. Not an error state.

    Any other message (real errors, real warnings) is still printed
    normally — this is a narrow, specific filter, not a blanket one.
    """

    _FILTERED_SUBSTRINGS = (
        "Deprecated Feature",
        "has been deprecated",
        "YouTube Music is not directly supported",
    )

    def _is_filtered(self, msg: str) -> bool:
        return any(s in msg for s in self._FILTERED_SUBSTRINGS)

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if self._is_filtered(msg):
            return
        print(msg)

    def error(self, msg):
        if self._is_filtered(msg):
            return
        print(msg)


# noplaylist + playlist_items='1' together are belt-and-suspenders
# against the exact class of bug that caused a real hang: pasting a
# single-video URL that also carries a playlist/mix/radio ID in its
# query string (e.g. YouTube's "&list=RDAMVM..." auto-generated radio
# mixes). Without these, yt-dlp can interpret such a URL as "fetch the
# whole playlist" and try to enumerate every entry before returning
# anything, which from the GUI looks exactly like a frozen "Fetching..."
# state even though nothing has technically crashed.
_BASE_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "logger": _QuietLogger(),
    "noplaylist": True,
    "playlist_items": "1",
    "extract_flat": False,
    "socket_timeout": 20,
}

# Hard ceiling on how long a metadata fetch is allowed to run before we
# give up and report a timeout, rather than leaving the GUI's
# "Fetching..." state up indefinitely if some site's extractor hangs
# for a reason socket_timeout doesn't catch (e.g. stuck in CPU-bound
# parsing rather than waiting on a socket).
FETCH_TIMEOUT_SECONDS = 45


def sanitize_filename(name: str) -> str:
    """Remove characters illegal in Windows/Mac/Linux filenames."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name)
    return cleaned.strip()


def is_ffmpeg_available() -> bool:
    """Check whether a usable ffmpeg is available — either a system
    install on PATH, or the bundled binary from the imageio-ffmpeg pip
    package. Audio extraction (converting to MP3) requires ffmpeg;
    plain video downloads do not. Checked up front so the GUI can warn
    clearly before the user picks an option that would fail, instead
    of surfacing yt-dlp's raw error after a failed download."""
    return _find_ffmpeg_path() is not None


@dataclass
class VideoFormat:
    """A single available quality option for a video."""
    format_id: str
    height: Optional[int]   # e.g. 1080, None for audio-only formats
    ext: str
    is_audio_only: bool
    filesize_mb: Optional[float]

    @property
    def label(self) -> str:
        if self.is_audio_only:
            return f"Audio only ({self.ext})"
        size = f" (~{self.filesize_mb:.0f} MB)" if self.filesize_mb else ""
        return f"{self.height}p{size}"

    @property
    def download_format_string(self) -> str:
        """The actual yt-dlp -f string to request. Many video formats
        (720p+) are video-only with no audio track — using the bare
        format_id silently produces a silent MP4. This merges in the
        best available audio, falling back to the format alone if
        merging isn't possible."""
        if self.is_audio_only:
            return self.format_id
        return f"{self.format_id}+bestaudio/{self.format_id}"


@dataclass
class VideoInfo:
    title: str
    video_formats: list[VideoFormat]
    audio_formats: list[VideoFormat]
    duration_seconds: Optional[float]
    thumbnail_url: Optional[str]


def _run_with_timeout(func, timeout_seconds):
    """Run func() in a background thread and enforce a hard wall-clock
    timeout, since yt-dlp itself has no universal 'give up after N
    seconds' option that covers every extractor's internal behavior —
    socket_timeout only bounds network reads, not, e.g., an extractor
    stuck parsing an unusually large playlist response.

    Raises TimeoutError if the timeout elapses. The worker thread is
    daemonized so it can't prevent the app from exiting even if it
    never returns.
    """
    result = {}
    error = {}

    def target():
        try:
            result["value"] = func()
        except Exception as e:  # noqa: BLE001 - deliberately broad, re-raised below
            error["exc"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        raise TimeoutError(
            f"This took longer than {timeout_seconds} seconds and was "
            "stopped. The site may be slow, blocking automated access, "
            "or the link may point to a large playlist/mix rather than "
            "a single video."
        )
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


def _extract_single_entry(info: dict) -> dict:
    """yt-dlp can return either a single-video dict, or (even with
    noplaylist=True, for some extractors/URL shapes) a playlist-shaped
    dict with an 'entries' list. Normalize to always return a single
    video's info dict, using only the first entry if given a playlist,
    so downstream code never has to guess which shape it received."""
    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(info.get("entries") or [])
        if not entries:
            raise ValueError(
                "This link points to a playlist or mix with no "
                "individual video found. Please paste a link to a "
                "single video instead."
            )
        return entries[0]
    return info


def fetch_video_info(url: str) -> VideoInfo:
    """Query the given URL and return available formats + metadata,
    without downloading anything. Works across any site yt-dlp supports
    (YouTube, Instagram, Facebook, Twitter/X, TikTok, Vimeo, and 1000+
    others), since yt-dlp handles per-site extraction internally.

    Always resolves to a single video's info, even if the URL also
    carries playlist/mix/radio parameters, and enforces a hard timeout
    so a slow or stuck site can't leave the caller waiting forever.

    Returns video and audio formats as two separate lists, so the GUI
    can show them in distinct tabs rather than one mixed dropdown.
    """
    ydl_opts = dict(_BASE_YDL_OPTS)

    def do_fetch():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    raw_info = _run_with_timeout(do_fetch, FETCH_TIMEOUT_SECONDS)
    info = _extract_single_entry(raw_info)

    raw_formats = info.get("formats", [])
    video_formats: list[VideoFormat] = []
    audio_formats: list[VideoFormat] = []

    seen_heights: set[int] = set()
    for fmt in raw_formats:
        has_video = fmt.get("vcodec") not in (None, "none")
        has_audio = fmt.get("acodec") not in (None, "none")
        height = fmt.get("height")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        filesize_mb = (filesize / (1024 * 1024)) if filesize else None

        if has_video and height:
            # Only keep one entry per resolution (yt-dlp often lists many
            # near-duplicate video-only streams per height); avoids a
            # cluttered, confusing dropdown in the UI.
            if height in seen_heights:
                continue
            seen_heights.add(height)
            video_formats.append(VideoFormat(
                format_id=fmt["format_id"],
                height=height,
                ext=fmt.get("ext", "mp4"),
                is_audio_only=False,
                filesize_mb=filesize_mb,
            ))
        elif has_audio and not has_video:
            audio_formats.append(VideoFormat(
                format_id=fmt["format_id"],
                height=None,
                ext=fmt.get("ext", "m4a"),
                is_audio_only=True,
                filesize_mb=filesize_mb,
            ))

    video_formats.sort(key=lambda f: -(f.height or 0))
    audio_formats.sort(key=lambda f: -(f.filesize_mb or 0))

    if not video_formats and not audio_formats:
        raise ValueError(
            "No downloadable formats were found for this link. It may "
            "be private, region-locked, age-restricted, or not "
            "actually a playable video/audio page."
        )

    return VideoInfo(
        title=sanitize_filename(info.get("title", "video")),
        video_formats=video_formats,
        audio_formats=audio_formats,
        duration_seconds=info.get("duration"),
        thumbnail_url=info.get("thumbnail"),
    )


def download(
    url: str,
    format_id: str,
    output_dir: str | Path,
    filename: str,
    audio_only: bool = False,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> Path:
    """Download the given format_id from url into output_dir/filename.

    progress_callback, if given, is called repeatedly by yt-dlp with a
    dict containing at least 'status' ('downloading'/'finished'/'error')
    and, while downloading, '_percent_str' / 'downloaded_bytes' /
    'total_bytes' — the GUI uses this to drive a live progress bar.

    Raises RuntimeError with a clear, plain-English message if ffmpeg
    is required (audio_only=True) but not available (neither on the
    system PATH nor bundled via imageio-ffmpeg), instead of letting
    yt-dlp fail deep inside postprocessing with a raw, ANSI-coded error.
    """
    ffmpeg_path = None
    if audio_only:
        ffmpeg_path = _find_ffmpeg_path()
        if ffmpeg_path is None:
            raise RuntimeError(
                "Converting to MP3 requires ffmpeg, and no usable ffmpeg "
                "was found — neither on your system PATH nor bundled "
                "with this app. This shouldn't normally happen since "
                "ffmpeg is installed automatically; try reinstalling "
                "streamgrab. (Video downloads don't need ffmpeg.)"
            )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    outtmpl = str(output_dir / f"{filename}.%(ext)s")

    # For video downloads, merge in audio (many formats are video-only)
    # and force remux to mp4 so output is always a playable video+audio file.
    actual_format = format_id if audio_only else f"{format_id}+bestaudio/best"

    ydl_opts = {
        **_BASE_YDL_OPTS,
        "format": actual_format,
        "outtmpl": outtmpl,
    }
    if not audio_only:
        ydl_opts["merge_output_format"] = "mp4"
        system_or_bundled = _find_ffmpeg_path()
        if system_or_bundled:
            ydl_opts["ffmpeg_location"] = system_or_bundled

    if progress_callback is not None:
        ydl_opts["progress_hooks"] = [progress_callback]

    if audio_only:
        # Tell yt-dlp exactly which ffmpeg binary to use. This matters
        # specifically when falling back to the bundled imageio-ffmpeg
        # binary, since that binary lives inside the Python package's
        # install location rather than on PATH — yt-dlp has no way to
        # find it unless told explicitly via ffmpeg_location.
        ydl_opts["ffmpeg_location"] = ffmpeg_path
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        # Strip yt-dlp's ANSI color codes so the GUI shows a clean
        # message instead of raw escape sequences like "[0;31mERROR".
        clean_msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise RuntimeError(clean_msg) from e

    # Return the most likely resulting file path (extension may have
    # changed due to audio extraction/remuxing, so glob for it).
    matches = list(output_dir.glob(f"{filename}.*"))
    return matches[0] if matches else output_dir / filename
