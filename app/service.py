"""Pipeline : URL -> cache -> yt-dlp (métadonnées) -> cache -> téléchargement -> ffmpeg -> OpenRouter -> cache."""

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

from .cache import TranscriptCache, legacy_meta
from .config import Settings
from .downloader import download_audio, format_duration, probe_duration, resolve_media, split_audio, to_compact_mp3
from .errors import InvalidUrlError, RateLimitedError, TranscriptError, VideoTooLongError
from .formatting import render
from .ratelimit import SlidingWindowRateLimiter
from .transcriber import OpenRouterTranscriber, TranscriptResult
from .urls import MediaRef, UrlPolicy, ensure_public_host

log = logging.getLogger("video_transcript")

ProgressFn = Callable[[float, str], Awaitable[None]]
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
MAX_INDEX = 49


async def _no_progress(_: float, __: str) -> None:
    return None


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
        self._jobs = asyncio.Semaphore(settings.max_concurrent_jobs)
        # un verrou par requête : deux appels simultanés sur le même média ne le transcrivent qu'une fois
        self._locks: weakref.WeakValueDictionary[tuple[str, int, str], asyncio.Lock] = weakref.WeakValueDictionary()
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

    def _from_cache(self, cached: dict) -> str:
        return render(
            legacy_meta(cached["meta"]), TranscriptResult.from_dict(cached["transcript"]), cached_at=cached["cached_at"]
        )

    async def get_transcript(
        self,
        url: str,
        video_index: int = 0,
        language: str | None = None,
        progress: ProgressFn = _no_progress,
    ) -> str:
        t0 = time.monotonic()
        ref = await asyncio.to_thread(self.policy.check, url)
        await asyncio.to_thread(ensure_public_host, ref.url)
        lang = self._normalize_language(language)
        index = video_index or 0
        if index < 0 or index > MAX_INDEX:
            raise InvalidUrlError(f"video_index doit être compris entre 0 et {MAX_INDEX} (0 = premier élément).")
        if index == 0 and ref.index_hint:
            index = ref.index_hint  # x.com/.../status/<id>/video/2

        lock_key = (ref.request_key or ref.url, index, lang or "auto")
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = self._locks[lock_key] = asyncio.Lock()
        async with lock:
            # 1) cache sans réseau : l'URL désigne directement un média connu
            if index == 0 and ref.request_key and (cached := self.cache.get(ref.request_key, lang)):
                log.info("%s lang=%s -> CACHE HIT (%.2fs)", ref.request_key, lang or "auto", time.monotonic() - t0)
                return self._from_cache(cached)
            async with self._jobs:
                return await self._run_job(ref, index, lang, progress, t0)

    async def _run_job(self, ref: MediaRef, index: int, lang: str | None, progress: ProgressFn, t0: float) -> str:
        s = self.settings
        workdir = Path(tempfile.mkdtemp(prefix="job-", dir=s.tmp_dir))
        label = f"{ref.request_key or ref.url}#{index}"
        acquired = spent = False
        try:
            # 2) métadonnées (une requête vers le site), puis cache sur l'identifiant définitif du média
            await progress(0.05, "Récupération des métadonnées…")
            meta, entry = await asyncio.to_thread(resolve_media, ref, index, workdir, s.cookies_path, self.policy)
            label = meta.key
            single = meta.item_count == 1 and index == 0
            if cached := self.cache.get(meta.key, lang):
                if single and ref.request_key and ref.request_key != meta.key:
                    self.cache.put([ref.request_key], lang, cached["transcript"].get("model", ""),
                                   {"meta": cached["meta"], "transcript": cached["transcript"]})  # fmt: skip
                log.info("%s lang=%s -> CACHE HIT après métadonnées (%.2fs)", meta.key, lang or "auto",
                         time.monotonic() - t0)  # fmt: skip
                return self._from_cache(cached)

            if meta.duration and meta.duration > s.max_video_seconds:
                raise VideoTooLongError(
                    f"Média trop long : {format_duration(meta.duration)} "
                    f"(maximum autorisé : {format_duration(s.max_video_seconds)}, réglable via MAX_VIDEO_MINUTES)."
                )

            # 3) quota : seules les vraies transcriptions comptent
            allowed, retry_in = self.limiter.try_acquire()
            if not allowed:
                raise RateLimitedError(
                    f"Limite atteinte : {s.rate_limit_per_hour} transcriptions par heure "
                    f"(RATE_LIMIT_PER_HOUR). Réessayez dans {int(retry_in // 60) + 1} min. "
                    "Les transcripts déjà en cache restent disponibles."
                )
            acquired = True

            await progress(0.15, f"Téléchargement de l'audio ({meta.platform})…")
            source = await asyncio.to_thread(download_audio, ref, entry, workdir, s.cookies_path, self.policy)
            t_dl = time.monotonic()

            await progress(0.35, "Conversion audio (mp3 mono 16 kHz)…")
            audio = workdir / "audio.mp3"
            await asyncio.to_thread(to_compact_mp3, source, audio, s.audio_bitrate)
            source.unlink(missing_ok=True)
            duration = await asyncio.to_thread(probe_duration, audio) or meta.duration or 0.0
            if meta.duration is None:
                meta.duration = duration
            if duration > s.max_video_seconds:  # durée inconnue avant téléchargement (liens directs…)
                raise VideoTooLongError(
                    f"Média trop long : {format_duration(duration)} "
                    f"(maximum autorisé : {format_duration(s.max_video_seconds)}, réglable via MAX_VIDEO_MINUTES)."
                )

            chunks = await asyncio.to_thread(
                split_audio, audio, duration, s.max_upload_bytes, s.chunk_max_seconds, s.audio_bitrate
            )
            await progress(0.5, f"Transcription via OpenRouter ({len(chunks)} morceau(x))…")
            spent = True
            result = await self.transcriber.transcribe(chunks, lang)
            t_tr = time.monotonic()

            payload = {"meta": meta.to_dict(), "transcript": result.to_dict()}
            keys = [meta.key]
            if single and ref.request_key:
                keys.append(ref.request_key)
            self.cache.put(keys, lang, result.model, payload)
            await progress(1.0, "Terminé")

            cost_bits = []
            if result.estimated_cost is not None:
                cost_bits.append(f"~${result.estimated_cost:.5f} estimé")
            if result.reported_cost is not None:
                cost_bits.append(f"${result.reported_cost:.5f} facturé")
            log.info(
                "%s lang=%s -> OK | durée %s | %d morceau(x) | audio facturé %.0fs | coût %s | timestamps=%s | "
                "métadonnées+dl %.1fs + transcription %.1fs = %.1fs | quota restant %d/h",
                meta.key, lang or "auto", format_duration(duration), len(chunks), result.billed_seconds,
                " / ".join(cost_bits) or "inconnu", result.timestamps,
                t_dl - t0, t_tr - t_dl, time.monotonic() - t0, self.limiter.remaining,
            )  # fmt: skip
            return render(payload["meta"], result)
        except TranscriptError as exc:
            if acquired and not spent:
                self.limiter.refund()  # rien n'a été facturé : on rend le quota
            log.info("%s -> ÉCHEC %s : %s (%.1fs)", label, type(exc).__name__, exc, time.monotonic() - t0)
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def aclose(self) -> None:
        await self.transcriber.aclose()
        self.cache.close()
