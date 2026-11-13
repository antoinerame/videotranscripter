"""Mise en forme du résultat en texte structuré, lisible par un LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .downloader import format_duration
from .transcriber import TranscriptResult


def _ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _num(n: int | None) -> str | None:
    return None if n is None else f"{n:,}".replace(",", " ")


def render(meta: dict[str, Any], transcript: TranscriptResult, *, cached_at: int | None = None) -> str:
    lines: list[str] = []
    author = meta.get("author_name") or "?"
    handle = meta.get("author_handle")
    lines.append(f"# Transcript de la vidéo d'un post X de {author}" + (f" (@{handle})" if handle else ""))
    lines.append("")
    lines.append("## Post")
    lines.append(f"- URL : {meta.get('url')}")
    lines.append(f"- Auteur : {author}" + (f" (@{handle})" if handle else ""))
    if meta.get("timestamp"):
        date = datetime.fromtimestamp(float(meta["timestamp"]), tz=UTC)
        lines.append(f"- Date : {date:%Y-%m-%d %H:%M} UTC")

    stats = [
        (label, _num(meta.get(key)))
        for label, key in (
            ("likes", "like_count"),
            ("reposts", "repost_count"),
            ("réponses", "comment_count"),
            ("vues", "view_count"),
        )
    ]
    stats_txt = " · ".join(f"{v} {label}" for label, v in stats if v is not None)
    if stats_txt:
        lines.append(f"- Engagement : {stats_txt}")

    text = (meta.get("text") or "").strip()
    lines.append("- Texte du post :")
    if text:
        lines.extend(f"  > {line}" if line else "  >" for line in text.splitlines())
    else:
        lines.append("  (aucun texte)")

    lines.append("")
    lines.append("## Vidéo")
    count = meta.get("video_count") or 1
    if count > 1:
        idx = meta.get("video_index", 0)
        lines.append(f"- Vidéo : {idx + 1}e sur {count} dans le post (video_index={idx} ; valeurs possibles 0 à {count - 1})")
    lines.append(f"- Durée : {format_duration(meta.get('duration') or transcript.duration)}")
    lines.append(f"- Langue : {transcript.language or 'inconnue'}")
    lines.append(f"- Modèle de transcription : {transcript.model}")
    if cached_at:
        when = datetime.fromtimestamp(cached_at, tz=UTC)
        lines.append(f"- Source : cache (transcrit le {when:%Y-%m-%d %H:%M} UTC)")

    lines.append("")
    if transcript.timestamps and transcript.segments:
        lines.append("## Transcript (horodaté par segment, [mm:ss])")
        lines.append("")
        lines.extend(f"[{_ts(s.start)}] {s.text}" for s in transcript.segments)
    elif transcript.segments:
        lines.append("## Transcript (texte brut ; horodatage approximatif par morceau audio)")
        lines.append("")
        for s in transcript.segments:
            lines.append(f"[{_ts(s.start)}]")
            lines.append(s.text)
            lines.append("")
    else:
        lines.append("## Transcript (texte brut, le modèle ne fournit pas de timestamps)")
        lines.append("")
        lines.append(transcript.text or "(aucune parole détectée)")

    if not (transcript.text or "").strip():
        lines.append("")
        lines.append("Note : aucune parole n'a été détectée (musique seule, silence ou vidéo sans voix).")
    return "\n".join(lines).rstrip() + "\n"
