"""Métadonnées et audio d'un média (tout site pris en charge par yt-dlp), puis conversion ffmpeg."""

from __future__ import annotations

import hashlib
import json
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
    MediaUnavailableError,
    NoMediaError,
    NoVideoError,
    SiteBlockedError,
    UnsupportedUrlError,
)
from .urls import MediaRef, UrlPolicy, ensure_public_host, media_key

log = logging.getLogger(__name__)

MAX_ITEMS = 50  # nombre max d'éléments lus dans une playlist / un flux / un post multi-vidéos
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

COOKIES_HINT = (
    "Si le problème persiste, exportez les cookies d'un navigateur connecté à ce site (format Netscape "
    "cookies.txt) dans le fichier indiqué par COOKIES_PATH ; un même fichier peut contenir les cookies de "
    "plusieurs sites."
)

# Formats « playlist » qui feraient lire d'autres fichiers/URLs à ffmpeg : jamais attendus ici.
_FORBIDDEN_FFMPEG_FORMATS = {"hls", "applehttp", "concat", "m3u", "m3u8", "dash", "ffconcat"}


_FEED_FIELDS = ("title", "description", "timestamp", "upload_date", "release_timestamp", "duration", "id")


@dataclass
class MediaInfo:
    key: str  # clé de cache définitive (plateforme:id)
    platform: str  # extractor_key yt-dlp
    url: str
    title: str | None
    author: str | None
    author_id: str | None
    timestamp: float | None
    upload_date: str | None
    description: str | None
    like_count: int | None
    repost_count: int | None
    comment_count: int | None
    view_count: int | None
    duration: float | None
    item_index: int
    item_count: int
    playlist_title: str | None

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


AUDIO_FORMAT = "bestaudio/worst"
# images clés : vidéo dont les DEUX côtés font ≤ 1280 px (1280×720 en paysage, 720×1280 en vertical :
# les images sont de toute façon réduites à 1280 px) + audio ; repli sur l'audio seul (podcasts)
_FIT = "[height<=1280][width<=1280]"
VIDEO_FORMAT = f"bv*{_FIT}+ba/b{_FIT}/bv*+ba/b/ba"


def _ydl_opts(
    workdir: Path, cookies_path: Path | None, policy: UrlPolicy, fmt: str = AUDIO_FORMAT
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _QuietLogger(),
        "allowed_extractors": policy.allowed_extractors,
        # par défaut : piste audio seule si disponible, sinon le plus petit format vidéo+audio
        "format": fmt,
        "merge_output_format": "mkv",
        "outtmpl": str(workdir / "source.%(ext)s"),
        # watch?v=X&list=Y -> la vidéo X, pas la playlist
        "noplaylist": True,
        # playlists / flux RSS : on liste les éléments sans les résoudre tous
        "extract_flat": "in_playlist",
        "playlist_items": f"1:{MAX_ITEMS}",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "cachedir": False,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        # renvoie les métadonnées même sans média : distingue « pas de vidéo » de « introuvable »
        "ignore_no_formats_error": True,
    }
    if cookies_path:
        if not cookies_path.is_file():
            log.warning("COOKIES_PATH=%s introuvable, on continue sans cookies", cookies_path)
        else:
            # yt-dlp réécrit le fichier cookies en sortie : on travaille sur une copie
            # (le volume peut être monté en lecture seule).
            copy = workdir / "cookies.txt"
            if not copy.exists():
                shutil.copyfile(cookies_path, copy)
            opts["cookiefile"] = str(copy)
    return opts


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _has(low: str, *needles: str) -> bool:
    return any(n in low for n in needles)


def _map_ytdlp_error(exc: Exception, has_cookies: bool, site: str) -> Exception:
    msg = str(exc).removeprefix("ERROR: ")
    low = msg.lower()
    log.info("yt-dlp a échoué (%s) : %s", site, msg)
    cookies_bad = "Les cookies configurés sont peut-être expirés : ré-exportez-les." if has_cookies else COOKIES_HINT

    if "unsupported url" in low:
        return UnsupportedUrlError(f"Aucune vidéo, aucun audio ni aucune image exploitable à cette adresse : {msg[:200]}")
    if _has(low, "drm"):
        return NoMediaError(f"Ce contenu ({site}) est protégé par DRM : impossible de récupérer l'audio.")
    if _has(low, "not a bot"):
        return SiteBlockedError(f"{site} bloque ce serveur (vérification anti-bot). {cookies_bad}")
    if _has(low, "private video", "video is private", "protected tweet", "not authorized to view", "is private"):
        return LoginRequiredError(
            f"Ce contenu {site} est privé : il n'est pas accessible."
            + ("" if has_cookies else " Avec les cookies d'un compte autorisé à le voir, cela pourrait fonctionner.")
        )
    if _has(low, "confirm your age", "age-restricted", "age restricted", "nsfw", "inappropriate"):
        return LoginRequiredError(
            f"{site} exige d'être connecté pour ce contenu (restriction d'âge / contenu sensible). {cookies_bad}"
        )
    if _has(low, "geo restrict", "geo-restrict", "not available in your country", "not available from your location"):
        return MediaUnavailableError(f"Ce contenu {site} n'est pas disponible depuis le pays du serveur (géo-blocage).")
    if _has(
        low, "no video could be found", "no video formats", "is not a video", "no media found",
        "there is no video", "no formats found",
    ):  # fmt: skip
        return NoVideoError(f"Aucune vidéo ni piste audio trouvée sur cette page ({site}).")
    if _has(low, "live event will begin", "premieres in", "not started yet"):
        return NoMediaError("Ce direct / cette première n'a pas encore commencé : rien à transcrire.")
    if _has(
        low, "unavailable", "removed", "deleted", "does not exist", "not found", "http error 404",
        "suspended", "no longer available", "twitter api says", "terminated",
    ):  # fmt: skip
        return MediaUnavailableError(
            f"Contenu introuvable sur {site} : supprimé, privé, ou URL erronée. (détail : {msg[:200]})"
        )
    if _has(low, "login", "log in", "logged-in", "logged in", "sign in", "authentication", "cookies"):
        return LoginRequiredError(f"{site} exige d'être connecté pour accéder à ce contenu. {cookies_bad}")
    if _has(low, "429", "rate-limit", "rate limit", "too many requests", "guest token", "http error 403", "forbidden"):
        return SiteBlockedError(f"{site} bloque ou limite les requêtes de ce serveur. {cookies_bad}")
    return DownloadError(f"Échec de la récupération du média via yt-dlp ({site}) : {msg[:400]}. {COOKIES_HINT}")


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _has_media(entry: dict[str, Any]) -> bool:
    if entry.get("formats"):
        return True
    return bool(entry.get("url")) and entry.get("_type") not in ("url", "url_transparent")


def _exists(entry: dict[str, Any]) -> bool:
    """Heuristique : une page qui existe a au moins un auteur ou un titre non vide."""
    return bool(entry.get("uploader") or entry.get("uploader_id") or entry.get("channel") or entry.get("title"))


def _media_info(ref: MediaRef, root: dict[str, Any], entry: dict[str, Any], count: int, index: int) -> MediaInfo:
    in_playlist = root is not entry

    def pick(key: str) -> Any:
        value = entry.get(key)
        return root.get(key) if value is None and in_playlist else value

    # sur X, la « playlist » d'un post multi-vidéos n'apporte rien de plus que le post
    playlist_title = root.get("title") if in_playlist and root.get("extractor_key") != "Twitter" else None
    return MediaInfo(
        key=media_key(entry),
        platform=entry.get("extractor_key") or ref.extractor,
        url=entry.get("webpage_url") or root.get("webpage_url") or ref.url,
        title=entry.get("title"),
        author=pick("uploader") or pick("channel") or pick("creator"),
        author_id=pick("uploader_id") or pick("channel_id"),
        timestamp=entry.get("timestamp") or entry.get("release_timestamp"),
        upload_date=entry.get("upload_date") or entry.get("release_date"),
        description=pick("description"),
        like_count=_int(pick("like_count")),
        repost_count=_int(pick("repost_count")),
        comment_count=_int(pick("comment_count")),
        view_count=_int(pick("view_count")),
        duration=entry.get("duration"),
        item_index=index,
        item_count=count,
        playlist_title=playlist_title,
    )


def resolve_media(
    ref: MediaRef, index: int, workdir: Path, cookies_path: Path | None, policy: UrlPolicy
) -> tuple[MediaInfo, dict[str, Any] | None]:
    """Récupère les métadonnées du média visé (sans le télécharger). Bloquant.

    Renvoie (métadonnées, entrée yt-dlp), ou (métadonnées, None) si la page existe mais sans vidéo ni audio.
    """
    opts = _ydl_opts(workdir, cookies_path, policy)
    has_cookies = "cookiefile" in opts
    site = ref.extractor
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(ref.url, download=False)
            if not info:
                raise NoMediaError("Aucun média trouvé à cette adresse.")
            site = info.get("extractor_key") or site

            if info.get("_type") in ("playlist", "multi_video"):
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise NoVideoError(f"Cette page ({site}) ne contient aucune vidéo ni piste audio.")
            else:
                entries = [info]

            if index >= len(entries):
                more = " au moins" if len(entries) >= MAX_ITEMS else ""
                raise NoMediaError(
                    f"video_index={index} hors limites : cette URL contient{more} {len(entries)} élément(s) "
                    f"(indices valides : 0 à {len(entries) - 1})."
                )
            entry = entries[index]
            meta_entry = entry
            key = None

            # élément de playlist / flux non encore résolu : on le résout, avec les mêmes garde-fous
            if entry.get("_type") in ("url", "url_transparent"):
                flat = entry
                target = entry.get("url") or ""
                policy.check(target)
                ensure_public_host(target)
                resolved = ydl.extract_info(target, download=False, ie_key=entry.get("ie_key"))
                if not resolved or resolved.get("_type") in ("playlist", "multi_video"):
                    raise NoMediaError("L'élément choisi est lui-même une playlist : donnez le lien direct de la vidéo.")
                entry = meta_entry = resolved
                if resolved.get("extractor_key") == "Generic":
                    # épisode de flux RSS : le flux connaît le vrai titre / la date, pas le fichier mp3
                    meta_entry = {**resolved, **{k: flat[k] for k in _FEED_FIELDS if flat.get(k)}}
                    # l'URL du mp3 contient souvent des jetons qui changent : clé = flux + GUID de l'épisode
                    feed = info.get("webpage_url") or ref.url
                    episode = str(flat.get("id") or target)
                    key = f"Feed:{_h(feed)}:{_h(episode)}"

            if not _has_media(entry):
                if not _exists(entry) and not _exists(info):
                    raise MediaUnavailableError(
                        f"Contenu introuvable sur {site} : supprimé, privé, URL erronée, ou le site refuse l'accès "
                        "anonyme. " + ("Les cookies configurés sont peut-être expirés." if has_cookies else COOKIES_HINT)
                    )
                # la page existe mais sans vidéo : peut-être un post photo (géré par l'appelant)
                return _media_info(ref, info, meta_entry, len(entries), index), None

            if entry.get("is_live") or entry.get("live_status") == "is_live":
                raise NoMediaError("C'est un direct en cours : seules les vidéos terminées peuvent être transcrites.")
            if entry.get("live_status") == "is_upcoming":
                raise NoMediaError("Ce direct / cette première n'a pas encore commencé : rien à transcrire.")

            count = len(entries)
            if count >= MAX_ITEMS and (info.get("playlist_count") or 0) > count:
                count = int(info["playlist_count"])
            media = _media_info(ref, info, meta_entry, count, index)
            if key:
                media.key = key
            return media, entry
    except (YtDlpDownloadError, ExtractorError) as exc:
        raise _map_ytdlp_error(exc, has_cookies, site) from exc


def download_media(
    ref: MediaRef,
    entry: dict[str, Any],
    workdir: Path,
    cookies_path: Path | None,
    policy: UrlPolicy,
    with_video: bool = False,
) -> Path:
    """Télécharge l'audio (ou la vidéo ≤ 720p si with_video) du média déjà résolu. Bloquant."""
    opts = _ydl_opts(workdir, cookies_path, policy, VIDEO_FORMAT if with_video else AUDIO_FORMAT)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_ie_result(entry, download=True)
    except (YtDlpDownloadError, ExtractorError) as exc:
        raise _map_ytdlp_error(exc, "cookiefile" in opts, entry.get("extractor_key") or ref.extractor) from exc

    sources = [p for p in workdir.glob("source.*") if p.is_file() and p.suffix not in (".part", ".ytdl")]
    if not sources:
        raise DownloadError(
            "yt-dlp n'a produit aucun fichier audio/vidéo (fichier trop volumineux ou téléchargement interrompu)."
        )
    return max(sources, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------- ffmpeg


def _run(cmd: list[str], timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def probe_duration(path: Path) -> float | None:
    res = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


def _probe_source(path: Path) -> tuple[str, bool]:
    """(format détecté, présence d'une piste audio). ffprobe limité aux fichiers locaux."""
    res = _run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file",
            "-show_entries", "format=format_name:stream=codec_type", "-of", "json", str(path),
        ],
        60,
    )  # fmt: skip
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    fmt = (data.get("format") or {}).get("format_name") or ""
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])
    return fmt, has_audio


def to_compact_mp3(src: Path, dst: Path, bitrate: str) -> None:
    """Convertit en MP3 mono 16 kHz bas débit (suffisant pour la reconnaissance vocale)."""
    fmt, has_audio = _probe_source(src)
    if set(fmt.split(",")) & _FORBIDDEN_FFMPEG_FORMATS:
        raise DownloadError(f"Format de fichier inattendu ({fmt}) : conversion refusée.")
    if not has_audio:
        raise NoMediaError("Ce média n'a pas de piste audio (GIF ou vidéo muette) : rien à transcrire.")
    res = _run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file", "-i", str(src),
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
