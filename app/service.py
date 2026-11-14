"""Pipeline.

URL -> contrôles -> cache -> yt-dlp (métadonnées) ->
  * vidéo/audio : cache -> téléchargement -> [images clés ffmpeg] -> [ffmpeg -> OpenRouter] -> cache
  * pas de vidéo : gallery-dl (images du post) -> optimisation JPEG -> cache
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image

from .cache import TranscriptCache, legacy_meta
from .config import Settings
from .downloader import (
    MediaInfo,
    download_media,
    format_duration,
    probe_duration,
    resolve_media,
    split_audio,
    to_compact_mp3,
)
from .errors import (
    InvalidUrlError,
    MediaUnavailableError,
    NoMediaError,
    NoVideoError,
    RateLimitedError,
    TranscriptError,
    UnsupportedUrlError,
    VideoTooLongError,
)
from .formatting import image_blocks, render, render_post
from .images import configure_gallery_dl, extract_frames, fetch_post_images, fit_budget, meta_from_gallery_dl
from .ratelimit import SlidingWindowRateLimiter
from .transcriber import OpenRouterTranscriber, TranscriptResult
from .urls import MediaRef, UrlPolicy, ensure_public_host

log = logging.getLogger("video_transcript")

ProgressFn = Callable[[float, str], Awaitable[None]]
Content = list[str | Image]
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
MAX_INDEX = 49
MAX_FRAMES = 12
POST = "post"


async def _no_progress(_: float, __: str) -> None:
    return None


def _frames_variant(max_frames: int) -> str:
    return f"frames-{max_frames}"


class TranscriptService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy = UrlPolicy(settings.allowed_sites, settings.allow_generic)
        self.cache = TranscriptCache(settings.cache_db_path)
        self.limiter = SlidingWindowRateLimiter(settings.rate_limit_per_hour, 3600)
        self.transcriber = OpenRouterTranscriber(
            settings.openrouter_api_key,
            settings.transcription_model,
            settings.openrouter_base_url,
            provider_order=settings.provider_order,
            provider_ignore=settings.provider_ignore,
        )
        configure_gallery_dl(settings.cookies_path)
        self._jobs = asyncio.Semaphore(settings.max_concurrent_jobs)
        # un verrou par requête : deux appels simultanés sur le même média ne le traitent qu'une fois
        self._locks: weakref.WeakValueDictionary[tuple, asyncio.Lock] = weakref.WeakValueDictionary()
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in settings.tmp_dir.glob("job-*"):  # restes d'un arrêt brutal
            shutil.rmtree(leftover, ignore_errors=True)

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        if language is None:
            return None
        lang = language.strip().lower()
        if lang in ("", "auto", "none", "null"):
            return None
        lang = lang.split("-")[0].split("_")[0]  # "fr-FR" -> "fr"
        if not _LANG_RE.match(lang):
            raise InvalidUrlError(f"Code langue invalide : {language!r}. Utilisez un code ISO-639-1, ex. 'fr', 'en'.")
        return lang

    # ------------------------------------------------------------------ rendu

    @staticmethod
    def _render_video(
        transcript: dict[str, Any], frames: tuple[dict[str, Any], list[bytes]] | None
    ) -> Content:
        text = render(
            legacy_meta(transcript["meta"]),
            TranscriptResult.from_dict(transcript["transcript"]),
            cached_at=transcript.get("cached_at"),
        )
        content: Content = [text]
        if frames is not None:
            content += image_blocks(frames[0], frames[1], frames=True)
        return content

    @staticmethod
    def _render_post(info: dict[str, Any], blobs: list[bytes]) -> Content:
        text = render_post(legacy_meta(info["meta"]), len(blobs), cached_at=info.get("cached_at"))
        return [text, *image_blocks(info, blobs, frames=False)]

    # ------------------------------------------------------------------ point d'entrée

    async def get_content(
        self,
        url: str,
        video_index: int = 0,
        language: str | None = None,
        include_frames: bool = False,
        max_frames: int = 6,
        progress: ProgressFn = _no_progress,
    ) -> Content:
        t0 = time.monotonic()
        ref = await asyncio.to_thread(self.policy.check, url)
        await asyncio.to_thread(ensure_public_host, ref.url)
        lang = self._normalize_language(language)
        index = video_index or 0
        if index < 0 or index > MAX_INDEX:
            raise InvalidUrlError(f"video_index doit être compris entre 0 et {MAX_INDEX} (0 = premier élément).")
        if index == 0 and ref.index_hint:
            index = ref.index_hint  # x.com/.../status/<id>/video/2
        max_frames = max(1, min(MAX_FRAMES, max_frames or 6))
        variant = _frames_variant(max_frames)

        lock_key = (ref.request_key or ref.url, index, lang or "auto", include_frames and variant)
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = self._locks[lock_key] = asyncio.Lock()
        async with lock:
            # 1) cache sans réseau : l'URL désigne directement un média connu
            if index == 0 and (rk := ref.request_key):
                if post := self.cache.get_images(rk, POST):
                    log.info("%s -> CACHE HIT (post images, %.2fs)", rk, time.monotonic() - t0)
                    return self._render_post(*post)
                transcript = self.cache.get(rk, lang)
                frames = self.cache.get_images(rk, variant) if include_frames else None
                if transcript and (frames or not include_frames):
                    log.info("%s lang=%s frames=%s -> CACHE HIT (%.2fs)", rk, lang or "auto",
                             include_frames, time.monotonic() - t0)  # fmt: skip
                    return self._render_video(transcript, frames)
            async with self._jobs:
                return await self._run_job(ref, index, lang, include_frames, max_frames, progress, t0)

    async def _run_job(
        self,
        ref: MediaRef,
        index: int,
        lang: str | None,
        include_frames: bool,
        max_frames: int,
        progress: ProgressFn,
        t0: float,
    ) -> Content:
        s = self.settings
        workdir = Path(tempfile.mkdtemp(prefix="job-", dir=s.tmp_dir))
        label = f"{ref.request_key or ref.url}#{index}"
        try:
            # 2) métadonnées (une requête vers le site)
            await progress(0.05, "Récupération des métadonnées…")
            meta: MediaInfo | None = None
            entry: dict[str, Any] | None = None
            no_video: TranscriptError | None = None
            try:
                meta, entry = await asyncio.to_thread(resolve_media, ref, index, workdir, s.cookies_path, self.policy)
            except (NoVideoError, UnsupportedUrlError, MediaUnavailableError) as exc:
                no_video = exc  # peut-être un post photo / une image : on tente gallery-dl
            if meta:
                label = meta.key
            if entry is None:
                return await self._image_post(ref, meta, no_video, progress, t0)
            assert meta is not None
            return await self._video(ref, meta, entry, workdir, index, lang, include_frames, max_frames, progress, t0)
        except TranscriptError as exc:
            log.info("%s -> ÉCHEC %s : %s (%.1fs)", label, type(exc).__name__, exc, time.monotonic() - t0)
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ posts photo

    async def _image_post(
        self, ref: MediaRef, meta: MediaInfo | None, no_video: TranscriptError | None, progress: ProgressFn, t0: float
    ) -> Content:
        keys = [k for k in (meta.key if meta else None, ref.request_key) if k]
        for key in keys:
            if cached := self.cache.get_images(key, POST):
                log.info("%s -> CACHE HIT (post images après métadonnées)", key)
                return self._render_post(*cached)

        await progress(0.3, "Pas de vidéo : récupération des images du post…")
        items, sources, kw = await asyncio.to_thread(fetch_post_images, ref, self.settings.max_post_images)
        if not items:
            raise no_video or NoMediaError(
                "Cette page ne contient ni vidéo, ni audio, ni image exploitable (ou uniquement du texte)."
            )
        items = await asyncio.to_thread(fit_budget, items, sources, self.settings.max_images_bytes)
        if meta is None:
            meta = meta_from_gallery_dl(ref, kw or {}, len(items))
        meta.item_count, meta.item_index = 1, 0
        keys = keys or [meta.key]
        if meta.key not in keys:
            keys.insert(0, meta.key)

        info = {"meta": meta.to_dict(), "items": [i.header() for i in items]}
        blobs = [i.jpeg for i in items]
        self.cache.put_images(keys, POST, info, blobs)
        log.info("%s -> OK post images | %d image(s), %.0f Ko | %.1fs", meta.key, len(items),
                 sum(map(len, blobs)) / 1024, time.monotonic() - t0)  # fmt: skip
        return self._render_post(info, blobs)

    # ------------------------------------------------------------------ vidéo / audio

    async def _video(
        self,
        ref: MediaRef,
        meta: MediaInfo,
        entry: dict[str, Any],
        workdir: Path,
        index: int,
        lang: str | None,
        include_frames: bool,
        max_frames: int,
        progress: ProgressFn,
        t0: float,
    ) -> Content:
        s = self.settings
        variant = _frames_variant(max_frames)
        single = meta.item_count == 1 and index == 0
        keys = [meta.key] + ([ref.request_key] if single and ref.request_key else [])

        transcript = self.cache.get(meta.key, lang)
        frames = self.cache.get_images(meta.key, variant) if include_frames else None
        need_transcript = transcript is None
        need_frames = include_frames and frames is None
        if not need_transcript and not need_frames:
            self._alias(keys, lang, transcript, frames, variant)
            log.info("%s lang=%s -> CACHE HIT après métadonnées (%.2fs)", meta.key, lang or "auto",
                     time.monotonic() - t0)  # fmt: skip
            return self._render_video(transcript, frames)

        if meta.duration and meta.duration > s.max_video_seconds:
            raise VideoTooLongError(
                f"Média trop long : {format_duration(meta.duration)} "
                f"(maximum autorisé : {format_duration(s.max_video_seconds)}, réglable via MAX_VIDEO_MINUTES)."
            )

        # quota : seules les vraies transcriptions (payantes) comptent
        acquired = spent = False
        if need_transcript:
            allowed, retry_in = self.limiter.try_acquire()
            if not allowed:
                raise RateLimitedError(
                    f"Limite atteinte : {s.rate_limit_per_hour} transcriptions par heure "
                    f"(RATE_LIMIT_PER_HOUR). Réessayez dans {int(retry_in // 60) + 1} min. "
                    "Les transcripts déjà en cache restent disponibles."
                )
            acquired = True
        try:
            what = "de la vidéo (≤ 1280 px)" if need_frames else "de l'audio"
            await progress(0.15, f"Téléchargement {what} ({meta.platform})…")
            source = await asyncio.to_thread(
                download_media, ref, entry, workdir, s.cookies_path, self.policy, need_frames
            )
            t_dl = time.monotonic()
            duration = await asyncio.to_thread(probe_duration, source) or meta.duration or 0.0
            if meta.duration is None:
                meta.duration = duration
            if duration > s.max_video_seconds:  # durée inconnue avant téléchargement (liens directs…)
                raise VideoTooLongError(
                    f"Média trop long : {format_duration(duration)} "
                    f"(maximum autorisé : {format_duration(s.max_video_seconds)}, réglable via MAX_VIDEO_MINUTES)."
                )

            if need_frames:
                await progress(0.25, "Extraction des images clés (changements de scène)…")
                items, _ = await asyncio.to_thread(self._frames, source, duration, max_frames, workdir)
                frames_info = {"meta": meta.to_dict(), "items": [i.header() for i in items]}
                blobs = [i.jpeg for i in items]
                self.cache.put_images(keys, variant, frames_info, blobs)
                frames = (frames_info, blobs)
                log.info("%s -> %d image(s) clé(s), %.0f Ko", meta.key, len(items), sum(map(len, blobs)) / 1024)

            if need_transcript:
                await progress(0.4, "Conversion audio (mp3 mono 16 kHz)…")
                audio = workdir / "audio.mp3"
                await asyncio.to_thread(to_compact_mp3, source, audio, s.audio_bitrate)
                source.unlink(missing_ok=True)
                chunks = await asyncio.to_thread(
                    split_audio, audio, duration, s.max_upload_bytes, s.chunk_max_seconds, s.audio_bitrate
                )
                await progress(0.5, f"Transcription via OpenRouter ({len(chunks)} morceau(x))…")
                spent = True
                t_tr0 = time.monotonic()
                result = await self.transcriber.transcribe(chunks, lang)
                payload = {"meta": meta.to_dict(), "transcript": result.to_dict()}
                self.cache.put(keys, lang, result.model, payload)
                transcript = payload
                self._log_transcription(meta, lang, duration, len(chunks), result, t0, t_dl, t_tr0)
            else:
                self._alias(keys, lang, transcript, None, variant)
        except TranscriptError:
            if acquired and not spent:
                self.limiter.refund()  # rien n'a été facturé : on rend le quota
            raise

        await progress(1.0, "Terminé")
        return self._render_video(transcript, frames if include_frames else None)

    def _frames(self, source: Path, duration: float, max_frames: int, workdir: Path):
        try:
            items, sources = extract_frames(source, duration, max_frames, workdir)
        except NoMediaError as exc:  # le transcript reste utile même sans images
            log.info("Images clés impossibles : %s", exc)
            return [], []
        return fit_budget(items, sources, self.settings.max_images_bytes), sources

    def _alias(self, keys: list[str], lang, transcript, frames, variant: str) -> None:
        """Recopie les entrées trouvées sous la clé définitive vers la clé « requête » (accès sans réseau)."""
        extra = keys[1:]
        if not extra:
            return
        if transcript:
            body = {"meta": transcript["meta"], "transcript": transcript["transcript"]}
            self.cache.put(extra, lang, transcript["transcript"].get("model", ""), body)
        if frames:
            info = {k: v for k, v in frames[0].items() if k != "cached_at"}
            self.cache.put_images(extra, variant, info, frames[1])

    def _log_transcription(self, meta, lang, duration, n_chunks, result, t0, t_dl, t_tr0) -> None:
        cost_bits = []
        if result.estimated_cost is not None:
            cost_bits.append(f"~${result.estimated_cost:.5f} estimé")
        if result.reported_cost is not None:
            cost_bits.append(f"${result.reported_cost:.5f} facturé")
        log.info(
            "%s lang=%s -> OK | durée %s | %d morceau(x) | audio facturé %.0fs | coût %s | timestamps=%s | "
            "métadonnées+dl %.1fs + transcription %.1fs = %.1fs | quota restant %d/h",
            meta.key, lang or "auto", format_duration(duration), n_chunks, result.billed_seconds,
            " / ".join(cost_bits) or "inconnu", result.timestamps,
            t_dl - t0, time.monotonic() - t_tr0, time.monotonic() - t0, self.limiter.remaining,
        )  # fmt: skip

    async def aclose(self) -> None:
        await self.transcriber.aclose()
        self.cache.close()

