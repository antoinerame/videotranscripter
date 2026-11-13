"""Pipeline : URL -> cache -> yt-dlp -> ffmpeg -> (découpage) -> OpenRouter -> cache."""

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

from .cache import TranscriptCache
from .config import Settings
from .downloader import fetch_audio, format_duration, probe_duration, split_audio, to_compact_mp3
from .errors import InvalidUrlError, RateLimitedError, TranscriptError, VideoTooLongError
from .formatting import render
from .ratelimit import SlidingWindowRateLimiter
from .transcriber import OpenRouterTranscriber, TranscriptResult
from .xurl import TweetRef, parse_tweet_url

log = logging.getLogger("x_transcript")

ProgressFn = Callable[[float, str], Awaitable[None]]
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


async def _no_progress(_: float, __: str) -> None:
    return None


class TranscriptService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = TranscriptCache(settings.cache_db_path)
        self.limiter = SlidingWindowRateLimiter(settings.rate_limit_per_hour, 3600)
        self.transcriber = OpenRouterTranscriber(
            settings.openrouter_api_key, settings.transcription_model, settings.openrouter_base_url
        )
        self._jobs = asyncio.Semaphore(settings.max_concurrent_jobs)
        # un verrou par clé de cache : deux appels simultanés sur la même vidéo ne la transcrivent qu'une fois
        self._key_locks: weakref.WeakValueDictionary[tuple[str, int, str], asyncio.Lock] = weakref.WeakValueDictionary()
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        # nettoyage d'éventuels restes d'un arrêt brutal
        for leftover in settings.tmp_dir.glob("job-*"):
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

    async def get_transcript(
        self,
        url: str,
        video_index: int = 0,
        language: str | None = None,
        progress: ProgressFn = _no_progress,
    ) -> str:
        t0 = time.monotonic()
        ref = parse_tweet_url(url)
        lang = self._normalize_language(language)
        if video_index is None:
            video_index = 0
        if video_index < 0 or video_index > 50:
            raise InvalidUrlError("video_index doit être compris entre 0 et 50 (0 = première vidéo du post).")
        if video_index == 0 and ref.media_number:
            video_index = ref.media_number - 1  # URL du type .../status/<id>/video/2

        key = (ref.tweet_id, video_index, lang or "auto")
        lock = self._key_locks.get(key)
        if lock is None:
            lock = self._key_locks[key] = asyncio.Lock()
        async with lock:
            cached = self.cache.get(ref.tweet_id, video_index, lang)
            if cached:
                log.info(
                    "tweet=%s video=%d lang=%s -> CACHE HIT (%.2fs)",
                    ref.tweet_id, video_index, lang or "auto", time.monotonic() - t0,
                )  # fmt: skip
                return render(
                    cached["meta"], TranscriptResult.from_dict(cached["transcript"]), cached_at=cached["cached_at"]
                )

            allowed, retry_in = self.limiter.try_acquire()
            if not allowed:
                raise RateLimitedError(
                    f"Limite atteinte : {self.settings.rate_limit_per_hour} transcriptions par heure "
                    f"(RATE_LIMIT_PER_HOUR). Réessayez dans {int(retry_in // 60) + 1} min. "
                    "Les transcripts déjà en cache restent disponibles."
                )
            async with self._jobs:
                return await self._run_job(ref, video_index, lang, progress, t0)

    async def _run_job(self, ref: TweetRef, video_index: int, lang: str | None, progress: ProgressFn, t0: float) -> str:
        s = self.settings
        workdir = Path(tempfile.mkdtemp(prefix="job-", dir=s.tmp_dir))
        spent = False
        try:
            await progress(0.05, "Récupération du post et de la vidéo sur X…")
            meta, source = await asyncio.to_thread(
                fetch_audio, ref, video_index, workdir, s.cookies_path, s.max_video_seconds
            )
            t_dl = time.monotonic()

            await progress(0.35, "Conversion audio (mp3 mono 16 kHz)…")
            audio = workdir / "audio.mp3"
            await asyncio.to_thread(to_compact_mp3, source, audio, s.audio_bitrate)
            source.unlink(missing_ok=True)
            duration = await asyncio.to_thread(probe_duration, audio) or meta.duration or 0.0
            if meta.duration is None:
                meta.duration = duration
            if duration > s.max_video_seconds:  # durée inconnue avant téléchargement
                raise VideoTooLongError(
                    f"Vidéo trop longue : {format_duration(duration)} "
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
            self.cache.put(ref.tweet_id, video_index, lang, result.model, payload)
            await progress(1.0, "Terminé")

            cost_bits = []
            if result.estimated_cost is not None:
                cost_bits.append(f"~${result.estimated_cost:.5f} estimé")
            if result.reported_cost is not None:
                cost_bits.append(f"${result.reported_cost:.5f} facturé")
            log.info(
                "tweet=%s video=%d lang=%s -> OK | durée vidéo %s | %d morceau(x) | audio facturé %.0fs | coût %s | "
                "timestamps=%s | dl %.1fs + transcription %.1fs = %.1fs | quota restant %d/h",
                ref.tweet_id, video_index, lang or "auto", format_duration(duration), len(chunks),
                result.billed_seconds, " / ".join(cost_bits) or "inconnu", result.timestamps,
                t_dl - t0, t_tr - t_dl, time.monotonic() - t0, self.limiter.remaining,
            )  # fmt: skip
            return render(payload["meta"], result)
        except TranscriptError as exc:
            if not spent:
                self.limiter.refund()  # rien n'a été facturé : on ne consomme pas le quota
            log.info(
                "tweet=%s video=%d -> ÉCHEC %s : %s (%.1fs)",
                ref.tweet_id, video_index, type(exc).__name__, exc, time.monotonic() - t0,
            )  # fmt: skip
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def aclose(self) -> None:
        await self.transcriber.aclose()
        self.cache.close()
