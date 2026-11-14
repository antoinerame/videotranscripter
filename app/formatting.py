"""Mise en forme du résultat en texte structuré, lisible par un LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import Image

from .downloader import format_duration
from .transcriber import TranscriptResult

DESCRIPTION_MAX_CHARS = 1500

PLATFORM_NAMES = {
    "Twitter": "X (Twitter)",
    "Youtube": "YouTube",
    "YoutubeTab": "YouTube",
    "TikTok": "TikTok",
    "Instagram": "Instagram",
    "LinkedIn": "LinkedIn",
    "Facebook": "Facebook",
    "Vimeo": "Vimeo",
    "Soundcloud": "SoundCloud",
    "ApplePodcasts": "Apple Podcasts",
    "Dailymotion": "Dailymotion",
    "Reddit": "Reddit",
    "TwitchVod": "Twitch",
    "ArchiveOrg": "Internet Archive",
    "Generic": "lien direct / page web",
}


def _ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _num(n: int | None) -> str | None:
    return None if n is None else f"{n:,}".replace(",", " ")


def _date(meta: dict[str, Any]) -> str | None:
    if meta.get("timestamp"):
        return f"{datetime.fromtimestamp(float(meta['timestamp']), tz=UTC):%Y-%m-%d %H:%M} UTC"
    d = meta.get("upload_date")
    if d and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None


def _author(meta: dict[str, Any]) -> str | None:
    name, ident = meta.get("author"), meta.get("author_id")
    if ident and meta.get("platform") in ("Twitter", "TikTok", "Instagram") and not ident.startswith("@"):
        ident = "@" + ident
    if ident and (ident == name or ident.isdigit() or ident.startswith("UC") and len(ident) == 24):
        ident = None  # identifiants techniques sans intérêt
    if name and ident:
        return f"{name} ({ident})"
    return name or ident


def _source_lines(meta: dict[str, Any], *, kind: str) -> list[str]:
    platform = meta.get("platform") or "?"
    platform_name = PLATFORM_NAMES.get(platform, platform)
    is_x = platform == "Twitter"
    author = _author(meta)
    title = None if is_x else (meta.get("title") or "").strip() or None

    fallback = f"post {platform_name} de {author}" if author else platform_name
    heading = f"# {kind} — {title or fallback}"
    if author and title:
        heading += f" ({author})"
    lines: list[str] = [heading, "", "## Source"]
    lines.append(f"- Plateforme : {platform_name}")
    lines.append(f"- URL : {meta.get('url')}")
    if title:
        lines.append(f"- Titre : {title}")
    if author:
        lines.append(f"- Auteur / chaîne : {author}")
    if date := _date(meta):
        lines.append(f"- Date : {date}")
    if meta.get("playlist_title"):
        lines.append(f"- Playlist / flux : {meta['playlist_title']}")

    stats = [
        (label, _num(meta.get(key)))
        for label, key in (
            ("likes", "like_count"),
            ("reposts", "repost_count"),
            ("commentaires", "comment_count"),
            ("vues", "view_count"),
        )
    ]
    stats_txt = " · ".join(f"{v} {label}" for label, v in stats if v is not None)
    if stats_txt:
        lines.append(f"- Engagement : {stats_txt}")

    text = (meta.get("description") or "").strip()
    if text:
        if len(text) > DESCRIPTION_MAX_CHARS:
            text = text[:DESCRIPTION_MAX_CHARS].rstrip() + " […]"
        lines.append("- Texte du post :" if is_x else "- Description :")
        lines.extend(f"  > {line}" if line.strip() else "  >" for line in text.splitlines())
    elif is_x:
        lines.append("- Texte du post : (aucun texte)")
    return lines


def render(meta: dict[str, Any], transcript: TranscriptResult, *, cached_at: int | None = None) -> str:
    lines = _source_lines(meta, kind="Transcript")
    lines.append("")
    lines.append("## Média")
    count = meta.get("item_count") or 1
    if count > 1:
        idx = meta.get("item_index", 0)
        last = min(count, 50) - 1
        lines.append(f"- Élément : {idx + 1}e sur {count} (video_index={idx} ; valeurs possibles 0 à {last})")
    lines.append(f"- Durée : {format_duration(meta.get('duration') or transcript.duration)}")
    if not transcript.note:
        lines.append(f"- Langue : {transcript.language or 'inconnue'}")
    if not transcript.note:
        lines.append(f"- Modèle de transcription : {transcript.model}")
    if cached_at:
        when = datetime.fromtimestamp(cached_at, tz=UTC)
        lines.append(f"- Source du transcript : cache (transcrit le {when:%Y-%m-%d %H:%M} UTC)")

    lines.append("")
    if transcript.note:
        lines += ["## Transcript", "", transcript.note]
        return "\n".join(lines).rstrip() + "\n"
    if transcript.timestamps and transcript.segments:
        lines.append("## Transcript (horodaté par segment)")
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
        lines.append("Note : aucune parole n'a été détectée (musique seule, silence ou média sans voix).")
    return "\n".join(lines).rstrip() + "\n"


def _cache_note(cached_at: int | None) -> str | None:
    if not cached_at:
        return None
    return f"cache (préparé le {datetime.fromtimestamp(cached_at, tz=UTC):%Y-%m-%d %H:%M} UTC)"


def render_post(meta: dict[str, Any], count: int, *, cached_at: int | None = None) -> str:
    """En-tête d'un post sans vidéo : métadonnées, puis les images suivent en blocs séparés."""
    lines = _source_lines(meta, kind="Post (images)")
    lines += ["", f"## Images ({count})", ""]
    lines.append(
        "Ce post ne contient ni vidéo ni audio : voici ses images, dans l'ordre du post "
        "(JPEG redimensionné, 1280 px max)."
    )
    if note := _cache_note(cached_at):
        lines.append(f"Source des images : {note}.")
    return "\n".join(lines).rstrip() + "\n"


def image_blocks(info: dict[str, Any], blobs: list[bytes], *, frames: bool) -> list[str | Image]:
    """Un bloc texte (timestamp / numéro) juste avant chaque image."""
    items = info.get("items", [])
    blocks: list[str | Image] = []
    if frames:
        if not items:
            return ["## Images clés\n\nAucune image : ce média n'a pas de piste vidéo (audio seul)."]
        header = (
            f"## Images clés ({len(items)})\n\n"
            "Extraites par détection de changement de scène, complétées par un échantillonnage régulier si besoin. "
            "Chaque image est précédée de son timestamp [mm:ss], à relier au transcript ci-dessus."
        )
        if note := _cache_note(info.get("cached_at")):
            header += f"\nSource des images : {note}."
        blocks.append(header)
    for i, (item, jpeg) in enumerate(zip(items, blobs, strict=True), 1):
        if frames:
            blocks.append(f"[{_ts(item.get('timestamp') or 0.0)}] Image clé {i}/{len(items)} ({item.get('label')})")
        else:
            blocks.append(f"{item.get('label') or f'Image {i}/{len(items)}'} ({item.get('width')}×{item.get('height')})")
        blocks.append(Image(data=jpeg, format="jpeg"))
    return blocks


def render_post_layout(
    meta: dict[str, Any], layout: list[dict[str, Any]], index: int, *, photos_sent: bool, photo_note: str | None
) -> str | None:
    """Structure d'un post mixte (photos + vidéos) ou multi-vidéos, pour que Claude sache tout ce qu'il contient."""
    videos = meta.get("videos") or [{"index": 0, "duration": meta.get("duration")}]
    n_videos = sum(1 for e in layout if e["kind"] != "photo")
    n_photos = len(layout) - n_videos
    if not layout:
        if len(videos) <= 1:
            return None if not photo_note else f"## Contenu du post\n\n{photo_note}\n"
        layout = [{"kind": "video", "photo": None} for _ in videos]
        n_videos = len(videos)
    if n_photos == 0 and n_videos <= 1:
        return None  # post à une seule vidéo : rien à signaler

    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'s' if n > 1 else ''}"

    parts = [plural(n_videos, "vidéo")] if n_videos else []
    if n_photos:
        parts.append(plural(n_photos, "photo"))
    lines = [f"## Contenu du post ({plural(len(layout), 'élément')} : {', '.join(parts)})", ""]
    k = 0
    for pos, item in enumerate(layout, 1):
        if item["kind"] == "photo":
            if item.get("photo") and photos_sent:
                lines.append(f"{pos}. photo — Photo {item['photo']}/{n_photos}, ci-dessous")
            elif item.get("photo"):
                lines.append(f"{pos}. photo — envoyée avec video_index=0")
            else:
                lines.append(f"{pos}. photo — non récupérée")
            continue
        info = videos[k] if k < len(videos) else {"index": k, "duration": None}
        k += 1
        dur = f", {format_duration(info.get('duration'))}" if info.get("duration") else ""
        gif = " (GIF animé, sans son)" if item["kind"] == "gif" else ""
        if info["index"] == index:
            lines.append(f"{pos}. vidéo{gif} — video_index={info['index']}{dur} ← cette réponse")
        else:
            lines.append(
                f"{pos}. vidéo{gif} — video_index={info['index']}{dur} : non transcrite ici, "
                f"rappeler get_media_content avec video_index={info['index']}"
            )
    if photo_note:
        lines += ["", photo_note]
    return "\n".join(lines) + "\n"


def photo_blocks(info: dict[str, Any], blobs: list[bytes]) -> list[str | Image]:
    """Photos d'un post mixte (vidéo + photos), chacune précédée de sa position dans le post."""
    if not blobs:
        return []
    return [f"## Photos du post ({len(blobs)})", *image_blocks(info, blobs, frames=False)]
