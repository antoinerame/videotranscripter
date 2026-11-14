"""Images : posts sans vidéo (gallery-dl), images clés de vidéos (ffmpeg), optimisation JPEG (Pillow)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import subprocess
import warnings
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageOps

from .downloader import MediaInfo
from .errors import DownloadError, LoginRequiredError, NoMediaError
from .urls import MediaRef, ensure_public_host

log = logging.getLogger(__name__)

MAX_SIDE = 1280
JPEG_QUALITY = 80
# si le total dépasse le budget, on réencode plus petit (taille max, qualité)
_FALLBACK_STEPS = [(1024, 75), (768, 70), (512, 65)]
MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024
SCENE_THRESHOLD = 0.3

PILImage.MAX_IMAGE_PIXELS = 80_000_000  # au-delà : refus (protection « decompression bomb »)
warnings.simplefilter("error", PILImage.DecompressionBombWarning)


@dataclass
class ImageItem:
    """Une image prête à envoyer, avec son contexte."""

    jpeg: bytes
    width: int
    height: int
    label: str  # « Image 2/4 », « changement de scène »…
    timestamp: float | None = None  # position dans la vidéo (images clés)

    def header(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("jpeg")
        return d


# --------------------------------------------------------------------------- optimisation


def optimize_image(data: bytes, max_side: int = MAX_SIDE, quality: int = JPEG_QUALITY) -> tuple[bytes, int, int]:
    """JPEG qualité ~80, côté le plus long ≤ max_side, orientation EXIF appliquée, transparence sur fond blanc."""
    with PILImage.open(io.BytesIO(data)) as img:
        img.seek(0)  # GIF/WebP animés : première image
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = PILImage.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.getchannel("A"))
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        return out.getvalue(), img.width, img.height


def fit_budget(items: list[ImageItem], sources: list[bytes], budget_bytes: int) -> list[ImageItem]:
    """Réencode plus petit si l'ensemble dépasse le budget (taille de la réponse MCP)."""
    total = sum(len(i.jpeg) for i in items)
    for max_side, quality in _FALLBACK_STEPS:
        if total <= budget_bytes:
            break
        log.info("Images : %.1f Mo > budget %.1f Mo, réencodage en %dpx q%d",
                 total / 1e6, budget_bytes / 1e6, max_side, quality)  # fmt: skip
        for item, src in zip(items, sources, strict=True):
            item.jpeg, item.width, item.height = optimize_image(src, max_side, quality)
        total = sum(len(i.jpeg) for i in items)
    while items and total > budget_bytes:  # dernier recours : on retire les dernières images
        total -= len(items.pop().jpeg)
    return items


# --------------------------------------------------------------------------- posts photo (gallery-dl)

_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "jfif"}
_gallery_dl_configured = False


def configure_gallery_dl(cookies_path: Path | None) -> None:
    from gallery_dl import config

    global _gallery_dl_configured
    config.clear()
    if cookies_path and cookies_path.is_file():
        config.set(("extractor",), "cookies", str(cookies_path))
    config.set(("extractor",), "cookies-update", False)  # le volume cookies est en lecture seule
    config.set(("extractor",), "timeout", 30)
    config.set(("extractor",), "retries", 2)
    # X : la taille « large » (2048 px) suffit, on redimensionne de toute façon à 1280
    config.set(("extractor", "twitter"), "size", ["large", "orig"])
    config.set(("output",), "mode", "null")
    logging.getLogger("gallery-dl").setLevel(logging.WARNING)
    _gallery_dl_configured = True


def _kw_first(kw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        node: Any = kw
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node not in (None, "", [], {}):
            return node
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_PLATFORMS = {"twitter": "Twitter", "instagram": "Instagram", "reddit": "Reddit", "bluesky": "Bluesky",
              "imgur": "Imgur", "tumblr": "Tumblr", "pinterest": "Pinterest", "mastodon": "Mastodon",
              "facebook": "Facebook", "tiktok": "TikTok", "directlink": "Generic"}  # fmt: skip


def meta_from_gallery_dl(ref: MediaRef, kw: dict[str, Any], count: int) -> MediaInfo:
    """Métadonnées d'un post à partir du kwdict gallery-dl (les clés varient selon le site)."""
    category = str(kw.get("category") or "")
    date = kw.get("date")
    timestamp = date.replace(tzinfo=date.tzinfo or UTC).timestamp() if isinstance(date, datetime) else None
    post_id = _kw_first(kw, "tweet_id", "post_id", "shortcode", "post_shortcode", "id")
    key = (
        ref.request_key
        or f"{category or 'Images'}:{post_id or hashlib.sha256(ref.url.encode()).hexdigest()[:16]}"
    )
    return MediaInfo(
        key=key,
        platform=_PLATFORMS.get(category, category.title() or ref.extractor),
        url=ref.url,
        title=_kw_first(kw, "title") if category not in ("twitter", "bluesky") else None,
        author=_kw_first(kw, "fullname", "author.nick", "author.displayName", "author.name", "username", "user.name"),
        author_id=_kw_first(kw, "username", "author.name", "author.handle", "user.screen_name"),
        timestamp=timestamp,
        upload_date=None,
        description=_kw_first(kw, "content", "text", "description", "caption"),
        like_count=_to_int(_kw_first(kw, "favorite_count", "likeCount", "likes", "like_count", "score")),
        repost_count=_to_int(_kw_first(kw, "retweet_count", "repostCount", "reblog_count")),
        comment_count=_to_int(_kw_first(kw, "reply_count", "replyCount", "comment_count", "num_comments")),
        view_count=_to_int(_kw_first(kw, "view_count", "views")),
        duration=None,
        item_index=0,
        item_count=count,
        playlist_title=None,
    )


def fetch_post_images(ref: MediaRef, max_images: int) -> tuple[list[ImageItem], list[bytes], dict[str, Any] | None]:
    """Images d'un post via gallery-dl. Renvoie (images optimisées, originaux, métadonnées du 1er fichier). Bloquant."""
    from gallery_dl import exception, job

    if not _gallery_dl_configured:
        configure_gallery_dl(None)
    try:
        data_job = job.DataJob(ref.url, file=None, resolve=1)
    except exception.NoExtractorError:
        return [], [], None
    data_job.run()
    exc = data_job.exception
    if exc is not None and not data_job.data_urls:
        msg = str(exc)
        log.info("gallery-dl a échoué : %s: %s", type(exc).__name__, msg)
        if isinstance(exc, exception.AuthRequired | exception.AuthorizationError) or "login" in msg.lower():
            raise LoginRequiredError(
                "Ce site exige d'être connecté pour voir les images de ce post : ajoutez ses cookies (COOKIES_PATH)."
            ) from exc
        return [], [], None

    session = data_job.extractor.session
    items: list[ImageItem] = []
    sources: list[bytes] = []
    first_kw = None
    candidates = [
        (url, kw)
        for url, kw in zip(data_job.data_urls, data_job.data_meta, strict=False)
        if not url.startswith("ytdl:") and str(kw.get("extension", "")).lower() in _IMAGE_EXTENSIONS
    ]
    for url, kw in candidates[:max_images]:
        try:
            ensure_public_host(url)
            raw = _download(session, url)
            jpeg, w, h = optimize_image(raw)
        except Exception as e:  # une image illisible ne doit pas faire échouer tout le post
            log.info("Image ignorée (%s) : %s", url[:120], e)
            continue
        first_kw = first_kw or kw
        sources.append(raw)
        items.append(ImageItem(jpeg, w, h, label=""))
    for i, item in enumerate(items, 1):
        item.label = f"Image {i}/{len(items)}"
    if len(candidates) > max_images:
        log.info("Post avec %d images : seules les %d premières sont gardées (MAX_POST_IMAGES)",
                 len(candidates), max_images)  # fmt: skip
    return items, sources, first_kw


def _download(session: Any, url: str) -> bytes:
    with session.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_DOWNLOAD_BYTES:
                raise DownloadError("image trop volumineuse")
        return bytes(buf)


# --------------------------------------------------------------------------- images clés (ffmpeg)

_SHOWINFO_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def has_video_stream(path: Path) -> bool:
    res = _run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v",
         "-show_entries", "stream=codec_name:stream_disposition=attached_pic", "-of", "csv=p=0", str(path)],
        60,
    )  # fmt: skip
    # une pochette d'album (attached_pic) n'est pas une vidéo
    return any(line and not line.endswith(",1") for line in res.stdout.splitlines())


def detect_scenes(video: Path, duration: float, threshold: float = SCENE_THRESHOLD) -> list[tuple[float, float]]:
    """(timestamp, score) des changements de scène, calculés sur une version réduite (rapide)."""
    res = _run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-protocol_whitelist", "file", "-i", str(video),
            "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"scale=320:-2,select='gt(scene,{threshold})',metadata=print:key=lavfi.scene_score,showinfo",
            "-f", "null", "-",
        ],
        timeout=max(180.0, duration * 0.5),
    )  # fmt: skip
    scenes: list[tuple[float, float]] = []
    pending_score: float | None = None
    for line in res.stderr.splitlines():
        if m := _SCORE_RE.search(line):
            pending_score = float(m.group(1))
        elif "Parsed_showinfo" in line and (m := _SHOWINFO_RE.search(line)):
            scenes.append((float(m.group(1)), pending_score or threshold))
            pending_score = None
    return scenes


def choose_timestamps(
    duration: float, scenes: list[tuple[float, float]], max_frames: int
) -> list[tuple[float, str]]:
    """Choisit au plus max_frames instants, répartis sur toute la vidéo.

    La vidéo est découpée en tranches égales : la 1re est représentée par le début de la vidéo
    (souvent un titre / une slide de couverture), chacune des autres par son changement de scène le
    plus marqué, et à défaut (détection trop pauvre) par un échantillon régulier au milieu de la tranche.
    """
    if duration <= 0:
        return [(0.0, "début")]
    target = max(1, min(max_frames, int(duration // 2)))  # pas plus d'une image toutes les 2 s
    width = duration / target
    best: dict[int, tuple[float, float]] = {}
    for t, score in scenes:
        if 0 <= t < duration:
            b = min(target - 1, int(t // width))
            if b not in best or score > best[b][1]:
                best[b] = (t, score)
    chosen: list[tuple[float, str]] = [(min(0.5, duration / 10), "début")]
    for b in range(1, target):
        if b in best:
            chosen.append((best[b][0], "changement de scène"))
        else:
            chosen.append(((b + 0.5) * width, "échantillon régulier"))
    return chosen


def _is_uniform(png: bytes) -> bool:
    """Écran noir / blanc / fondu : image quasi unie, sans information."""
    from PIL import ImageStat

    with PILImage.open(io.BytesIO(png)) as img:
        small = img.convert("L").resize((64, 36))
        return ImageStat.Stat(small).stddev[0] < 6


def extract_frames(video: Path, duration: float, max_frames: int, workdir: Path) -> tuple[list[ImageItem], list[bytes]]:
    """Images clés d'une vidéo. Bloquant."""
    if not has_video_stream(video):
        return [], []
    scenes = detect_scenes(video, duration)
    times = choose_timestamps(duration, scenes, max_frames)
    log.info("Images clés : %d changement(s) de scène détecté(s), %d image(s) retenue(s)", len(scenes), len(times))
    items: list[ImageItem] = []
    sources: list[bytes] = []
    for i, (t0, label) in enumerate(times):
        raw, used = None, t0
        # si l'image est unie (fondu, écran noir), on décale un peu
        for t in (t0, t0 + 2.0, t0 + 5.0):
            if t >= duration and t != t0:
                break
            png = workdir / f"frame_{i:02d}.png"
            res = _run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file",
                 "-ss", f"{t:.3f}", "-i", str(video), "-map", "0:v:0", "-frames:v", "1", str(png)],
                120,
            )  # fmt: skip
            if res.returncode != 0 or not png.exists():
                log.info("Image clé à %.1fs non extraite : %s", t, res.stderr.strip()[-200:])
                continue
            raw, used = png.read_bytes(), t
            if not _is_uniform(raw):
                break
        if raw is None:
            continue
        jpeg, w, h = optimize_image(raw)
        sources.append(raw)
        items.append(ImageItem(jpeg, w, h, label=label, timestamp=used))
    if not items:
        raise NoMediaError("Impossible d'extraire des images de cette vidéo.")
    return items, sources
