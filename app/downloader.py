"""Récupération des métadonnées et de l'audio d'un post X via yt-dlp, puis conversion ffmpeg."""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError
from yt_dlp.utils import ExtractorError

from .errors import (
    DownloadError,
    LoginRequiredError,
    NoVideoError,
    TweetUnavailableError,
    VideoTooLongError,
    XBlockedError,
)
from .xurl import TweetRef

log = logging.getLogger(__name__)

# yt-dlp ne peut utiliser QUE les extracteurs X/Twitter : si un post renvoie vers
# un site externe (YouTube…), il ne sera pas suivi.
ALLOWED_EXTRACTORS = [r"twitter", r"twitter:(amplify|broadcast|card)"]

COOKIES_HINT = (
    "Si le problème persiste, X bloque probablement les requêtes anonymes : exportez les cookies "
    "d'une session X connectée (format Netscape cookies.txt) et montez-les via COOKIES_PATH."
)


@dataclass
class VideoInfo:
    tweet_id: str
    url: str
    author_name: str | None
    author_handle: str | None
    timestamp: float | None
    text: str | None
    like_count: int | None
    repost_count: int | None
    comment_count: int | None
    view_count: int | None
    duration: float | None
    video_count: int
    video_index: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AudioChunk:
    path: Path
    offset: float
    duration: float


# --------------------------------------------------------------------------- yt-dlp


class _QuietLogger:
    """Redirige les logs yt-dlp vers logging (sans bruit sur stdout)."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp error: %s", msg)


def _ydl_opts(workdir: Path, cookies_path: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _QuietLogger(),
        "allowed_extractors": ALLOWED_EXTRACTORS,
        # piste audio seule si disponible, sinon le plus petit MP4 (qui contient l'audio)
        "format": "bestaudio/worst",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "cachedir": False,
        "max_filesize": 1024 * 1024 * 1024,
        # renvoie les métadonnées même sans vidéo : permet de distinguer
        # « post sans vidéo » de « post introuvable »
        "ignore_no_formats_error": True,
    }
    if cookies_path:
        if not cookies_path.is_file():
            log.warning("COOKIES_PATH=%s introuvable, on continue sans cookies", cookies_path)
        else:
            # yt-dlp réécrit le fichier cookies en sortie : on travaille sur une copie
            # (le volume peut être monté en lecture seule).
            copy = workdir / "cookies.txt"
            shutil.copyfile(cookies_path, copy)
            opts["cookiefile"] = str(copy)
    return opts


def _map_ytdlp_error(exc: Exception, has_cookies: bool) -> Exception:
    msg = str(exc)
    low = msg.lower()
    log.info("yt-dlp a échoué : %s", msg)
    if "no video could be found" in low or "is not a video" in low or "no video formats" in low:
        return NoVideoError("Ce post ne contient pas de vidéo (ou uniquement des images / du texte).")
    if "unsupported url" in low or "extractors are restricted" in low or "not supported because" in low:
        return NoVideoError(
            "Ce post ne contient pas de vidéo native X (il pointe peut-être vers un site externe, "
            "qui n'est pas pris en charge par ce serveur)."
        )
    if "protected tweet" in low or "not authorized to view" in low:
        return LoginRequiredError(
            "Ce post vient d'un compte protégé (privé) : il n'est pas accessible."
            + ("" if has_cookies else " Avec des cookies d'un compte qui suit ce compte, cela pourrait fonctionner.")
        )
    if "nsfw" in low or "requires authentication" in low or "login" in low or "log in" in low:
        return LoginRequiredError(
            "X exige d'être connecté pour voir ce post (contenu sensible ou restreint). "
            + ("Les cookies configurés semblent invalides ou expirés : ré-exportez-les." if has_cookies else COOKIES_HINT)
        )
    if (
        "tweet is unavailable" in low
        or "requested tweet is unavailable" in low
        or "twitter api says" in low
        or "suspended" in low
        or "deleted" in low
        or "http error 404" in low
        or "does not exist" in low
    ):
        return TweetUnavailableError(
            "Post introuvable : il a été supprimé, son compte est suspendu/privé, ou l'URL est erronée."
            f" (détail X : {msg.split(':')[-1].strip()[:200]})"
        )
    if (
        "429" in low
        or "rate-limit" in low
        or "rate limit" in low
        or "guest token" in low
        or "http error 403" in low
        or "forbidden" in low
        or "bad guest" in low
    ):
        return XBlockedError(
            "X bloque ou limite les requêtes de ce serveur (rate limit / jeton invité refusé). "
            + ("Réessayez plus tard ; si cela continue, ré-exportez les cookies." if has_cookies else COOKIES_HINT)
        )
    return DownloadError(f"Échec de la récupération de la vidéo via yt-dlp : {msg[:500]}. {COOKIES_HINT}")


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") in ("playlist", "multi_video"):
        return [e for e in (info.get("entries") or []) if e]
    return [info]


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _video_info(ref: TweetRef, root: dict[str, Any], entry: dict[str, Any], count: int, index: int) -> VideoInfo:
    def pick(key: str) -> Any:
        value = entry.get(key)
        return value if value is not None else root.get(key)

    handle = pick("uploader_id") or ref.handle
    return VideoInfo(
        tweet_id=ref.tweet_id,
        url=f"https://x.com/{handle or 'i'}/status/{ref.tweet_id}",
        author_name=pick("uploader"),
        author_handle=handle,
        timestamp=pick("timestamp"),
        text=pick("description"),
        like_count=_int(pick("like_count")),
        repost_count=_int(pick("repost_count")),
        comment_count=_int(pick("comment_count")),
        view_count=_int(pick("view_count")),
        duration=entry.get("duration"),
        video_count=count,
        video_index=index,
    )


def fetch_audio(
    ref: TweetRef,
    video_index: int,
    workdir: Path,
    cookies_path: Path | None,
    max_duration: float,
) -> tuple[VideoInfo, Path]:
    """Récupère les métadonnées, vérifie la durée, puis télécharge l'audio. Bloquant (à lancer dans un thread)."""
    opts = _ydl_opts(workdir, cookies_path)
    has_cookies = "cookiefile" in opts
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(ref.canonical_url, download=False)
            if not info:
                raise NoVideoError("Ce post ne contient pas de vidéo.")
            all_entries = _entries(info)
            entries = [e for e in all_entries if e.get("formats") or e.get("url")]
            if not entries:
                if not any(e.get("uploader") or e.get("uploader_id") for e in [info, *all_entries]):
                    raise TweetUnavailableError(
                        "Post introuvable : il a été supprimé, il est privé (compte protégé), l'URL est erronée, "
                        "ou X refuse l'accès anonyme à ce post. "
                        + ("Les cookies configurés sont peut-être expirés." if has_cookies else COOKIES_HINT)
                    )
                raise NoVideoError("Ce post ne contient pas de vidéo (ou uniquement des images / du texte).")
            if video_index >= len(entries):
                raise NoVideoError(
                    f"video_index={video_index} hors limites : ce post contient {len(entries)} vidéo(s) "
                    f"(indices valides : 0 à {len(entries) - 1})."
                )
            entry = entries[video_index]
            meta = _video_info(ref, info, entry, len(entries), video_index)

            if meta.duration and meta.duration > max_duration:
                raise VideoTooLongError(
                    f"Vidéo trop longue : {format_duration(meta.duration)} "
                    f"(maximum autorisé : {format_duration(max_duration)}, réglable via MAX_VIDEO_MINUTES)."
                )

            ydl.process_ie_result(entry, download=True)
    except (YtDlpDownloadError, ExtractorError) as exc:
        raise _map_ytdlp_error(exc, has_cookies) from exc

    sources = [p for p in workdir.glob("source.*") if p.suffix not in (".part", ".ytdl") and p.is_file()]
    if not sources:
        raise DownloadError("yt-dlp n'a produit aucun fichier audio/vidéo.")
    return meta, max(sources, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------- ffmpeg


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def probe_duration(path: Path) -> float | None:
    res = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


def has_audio_stream(path: Path) -> bool:
    res = _run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        60,
    )
    return res.returncode == 0 and bool(res.stdout.strip())


def to_compact_mp3(src: Path, dst: Path, bitrate: str) -> None:
    """Convertit en MP3 mono 16 kHz bas débit (suffisant pour la reconnaissance vocale)."""
    if not has_audio_stream(src):
        raise NoVideoError("La vidéo de ce post n'a pas de piste audio (GIF ou vidéo muette) : rien à transcrire.")
    res = _run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate,
            str(dst),
        ]
    )  # fmt: skip
    if res.returncode != 0 or not dst.exists():
        raise DownloadError(f"Échec de la conversion audio ffmpeg : {res.stderr.strip()[-400:]}")


def split_audio(src: Path, duration: float, max_bytes: int, max_seconds: float, bitrate: str) -> list[AudioChunk]:
    """Découpe le fichier si trop gros (limite API) ou trop long (timeout provider ~60 s)."""
    size = src.stat().st_size
    n = max(1, math.ceil(size / max_bytes), math.ceil(duration / max_seconds))
    if n == 1:
        return [AudioChunk(src, 0.0, duration)]

    chunk_len = duration / n
    chunks: list[AudioChunk] = []
    for i in range(n):
        start = i * chunk_len
        length = duration - start if i == n - 1 else chunk_len
        dst = src.with_name(f"chunk_{i:03d}.mp3")
        res = _run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
                "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", bitrate,
                str(dst),
            ]
        )  # fmt: skip
        if res.returncode != 0 or not dst.exists():
            raise DownloadError(f"Échec du découpage audio ffmpeg : {res.stderr.strip()[-400:]}")
        if dst.stat().st_size > 25 * 1024 * 1024:
            raise DownloadError("Un morceau audio dépasse 25 Mo malgré le découpage (AUDIO_BITRATE trop élevé ?).")
        chunks.append(AudioChunk(dst, start, length))
    return chunks


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "inconnue"
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
