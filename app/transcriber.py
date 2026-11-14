"""Transcription via l'endpoint OpenRouter /api/v1/audio/transcriptions (compatible SDK OpenAI)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI

from .downloader import AudioChunk
from .errors import TranscriptionError

log = logging.getLogger(__name__)

PRICING_TTL = 24 * 3600
MAX_PARALLEL_CHUNKS = 4


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    duration: float | None = None
    billed_seconds: float = 0.0
    reported_cost: float | None = None  # usage.cost renvoyé par OpenRouter (USD), si fourni
    estimated_cost: float | None = None  # usage.seconds × prix/seconde du modèle
    timestamps: bool = False
    model: str = ""
    note: str | None = None  # ex. vidéo sans piste audio

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "segments": [[s.start, s.end, s.text] for s in self.segments],
            "language": self.language,
            "duration": self.duration,
            "billed_seconds": self.billed_seconds,
            "reported_cost": self.reported_cost,
            "estimated_cost": self.estimated_cost,
            "timestamps": self.timestamps,
            "model": self.model,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TranscriptResult:
        return cls(
            text=d.get("text", ""),
            segments=[Segment(float(s), float(e), t) for s, e, t in d.get("segments", [])],
            language=d.get("language"),
            duration=d.get("duration"),
            billed_seconds=d.get("billed_seconds") or 0.0,
            reported_cost=d.get("reported_cost"),
            estimated_cost=d.get("estimated_cost"),
            timestamps=bool(d.get("timestamps")),
            model=d.get("model", ""),
            note=d.get("note"),
        )


class OpenRouterTranscriber:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        provider_order: list[str] | None = None,
        provider_ignore: list[str] | None = None,
    ):
        self.model = model
        # Routage OpenRouter (champ multipart `provider`, objet JSON encodé en chaîne)
        self._provider: dict[str, Any] = {}
        if provider_order:
            self._provider["order"] = provider_order
            self._provider["allow_fallbacks"] = True
        if provider_ignore:
            self._provider["ignore"] = provider_ignore
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=openai.Timeout(180.0, connect=15.0),
            max_retries=2,
            default_headers={"X-Title": "X video transcript MCP"},
        )
        # None = inconnu, True/False = le modèle accepte (ou non) response_format=verbose_json
        self._verbose_supported: bool | None = None
        self._price_per_second: float | None = None
        self._price_fetched_at = 0.0

    # ------------------------------------------------------------------ prix

    async def price_per_second(self) -> float | None:
        """Prix estimé (USD / seconde d'audio) du fournisseur qui devrait servir la requête (API publique OpenRouter)."""
        if self._price_per_second is not None and time.time() - self._price_fetched_at < PRICING_TTL:
            return self._price_per_second
        try:
            async with httpx2.AsyncClient(timeout=10) as http:
                resp = await http.get(f"{self.base_url}/models/{self.model}/endpoints")
                resp.raise_for_status()
            ignored = [p.lower() for p in self._provider.get("ignore", [])]
            prices: dict[str, float] = {}
            for ep in resp.json().get("data", {}).get("endpoints", []):
                tag = (ep.get("tag") or "").lower()
                name = (ep.get("provider_name") or "").lower()
                if any(tag == i or tag.startswith(i + "/") or name == i for i in ignored):
                    continue
                price = float(ep["pricing"]["prompt"])
                prices[tag or name] = price
                prices.setdefault(name, price)
            # prix du premier fournisseur préféré disponible, sinon le moins cher (choix par défaut d'OpenRouter)
            chosen = None
            for preferred in (p.lower() for p in self._provider.get("order", [])):
                match = [v for k, v in prices.items() if k == preferred or k.startswith(preferred + "/")]
                if match:
                    chosen = match[0]
                    break
            if chosen is None and prices:
                chosen = min(prices.values())
            if chosen is not None:
                self._price_per_second = chosen
                self._price_fetched_at = time.time()
        except Exception as exc:  # le prix n'est qu'indicatif (logs)
            log.debug("Impossible de récupérer le prix du modèle : %s", exc)
        return self._price_per_second

    # ------------------------------------------------------------- appels API

    async def _call(self, chunk: AudioChunk, language: str | None, verbose: bool) -> dict[str, Any]:
        audio = chunk.path.read_bytes()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "file": (chunk.path.name, audio, "audio/mpeg"),
        }
        if language:
            kwargs["language"] = language
        if self._provider:
            kwargs["extra_body"] = {"provider": json.dumps(self._provider)}
        if verbose:
            kwargs["response_format"] = "verbose_json"
            kwargs["timestamp_granularities"] = ["segment"]
        else:
            kwargs["response_format"] = "json"
        resp = await self._client.audio.transcriptions.create(**kwargs)
        if isinstance(resp, str):
            return {"text": resp}
        return resp.model_dump(exclude_none=False)

    async def _transcribe_chunk(self, chunk: AudioChunk, language: str | None) -> dict[str, Any]:
        try:
            if self._verbose_supported is not False:
                try:
                    data = await self._call(chunk, language, verbose=True)
                    if data.get("segments"):
                        self._verbose_supported = True
                    return data
                except openai.BadRequestError as exc:
                    if self._verbose_supported:
                        raise
                    log.info(
                        "Le modèle %s refuse verbose_json (%s) ; repli sur response_format=json (sans timestamps)",
                        self.model,
                        _api_message(exc),
                    )
                    data = await self._call(chunk, language, verbose=False)
                    self._verbose_supported = False
                    return data
            return await self._call(chunk, language, verbose=False)
        except openai.APIStatusError as exc:
            raise _map_api_error(exc) from exc
        except openai.APITimeoutError as exc:
            raise TranscriptionError(
                "OpenRouter n'a pas répondu à temps (timeout). Réessayez dans quelques instants."
            ) from exc
        except openai.APIConnectionError as exc:
            raise TranscriptionError(f"Impossible de joindre OpenRouter : {exc}") from exc

    async def transcribe(self, chunks: list[AudioChunk], language: str | None) -> TranscriptResult:
        sem = asyncio.Semaphore(MAX_PARALLEL_CHUNKS)

        async def run(c: AudioChunk) -> dict[str, Any]:
            async with sem:
                return await self._transcribe_chunk(c, language)

        # Le premier morceau sert à découvrir si verbose_json est supporté ; les autres suivent en parallèle.
        first = await run(chunks[0])
        rest = await asyncio.gather(*(run(c) for c in chunks[1:])) if len(chunks) > 1 else []
        return await self._merge(chunks, [first, *rest], language)

    async def _merge(self, chunks: list[AudioChunk], results: list[dict[str, Any]], language: str | None) -> TranscriptResult:
        segments: list[Segment] = []
        texts: list[str] = []
        billed = 0.0
        reported_cost: float | None = None
        detected: str | None = None
        all_have_segments = True

        for chunk, data in zip(chunks, results, strict=True):
            text = _clean(data.get("text") or "")
            texts.append(text)
            detected = detected or data.get("language")

            raw_segments = data.get("segments") or []
            if raw_segments:
                for seg in raw_segments:
                    seg_text = _clean(seg.get("text") or "")
                    if not seg_text:
                        continue
                    segments.append(
                        Segment(
                            start=float(seg.get("start") or 0.0) + chunk.offset,
                            end=float(seg.get("end") or 0.0) + chunk.offset,
                            text=seg_text,
                        )
                    )
            else:
                all_have_segments = False
                if text:  # au moins l'horodatage du début du morceau
                    segments.append(Segment(chunk.offset, chunk.offset + chunk.duration, text))

            usage = data.get("usage") or {}
            seconds = usage.get("seconds") if isinstance(usage, dict) else None
            billed += float(seconds) if seconds is not None else chunk.duration
            cost = usage.get("cost") if isinstance(usage, dict) else None
            if cost is not None:
                reported_cost = (reported_cost or 0.0) + float(cost)

        price = await self.price_per_second()
        has_ts = all_have_segments and bool(segments)
        return TranscriptResult(
            text="\n".join(t for t in texts if t),
            segments=segments if (has_ts or len(chunks) > 1) else [],
            language=detected or language,
            duration=chunks[-1].offset + chunks[-1].duration,
            billed_seconds=billed,
            reported_cost=reported_cost,
            estimated_cost=billed * price if price is not None else None,
            timestamps=has_ts,
            model=self.model,
        )

    async def aclose(self) -> None:
        await self._client.close()


_SPACES = re.compile(r"[ \t]{2,}")


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _api_message(exc: openai.APIStatusError) -> str:
    body = exc.body
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(exc)[:300]


def _map_api_error(exc: openai.APIStatusError) -> TranscriptionError:
    status = exc.status_code
    detail = _api_message(exc)
    messages = {
        400: "Requête refusée par OpenRouter (paramètres ou audio invalides)",
        401: "Clé OpenRouter invalide ou absente (vérifiez OPENROUTER_API_KEY)",
        402: "Crédits OpenRouter insuffisants : rechargez le compte OpenRouter",
        403: "Accès refusé par OpenRouter pour ce modèle",
        404: "Modèle de transcription introuvable sur OpenRouter (vérifiez TRANSCRIPTION_MODEL)",
        413: "Fichier audio trop volumineux pour OpenRouter",
        429: "OpenRouter limite le débit (rate limit) : réessayez dans une minute",
    }
    base = messages.get(status) or (
        "OpenRouter ou le fournisseur du modèle est en échec / indisponible : réessayez plus tard"
        if status >= 500
        else "Erreur OpenRouter"
    )
    return TranscriptionError(f"Échec de la transcription OpenRouter (HTTP {status}) — {base}. Détail : {detail}")
