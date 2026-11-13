"""Validation / normalisation des URLs de posts X (Twitter).

Seules les URLs x.com / twitter.com sont acceptées. On n'envoie jamais à yt-dlp
l'URL fournie par l'utilisateur : on reconstruit une URL canonique à partir de
l'ID du tweet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import InvalidUrlError

ALLOWED_DOMAINS = {"x.com", "twitter.com"}
ALLOWED_SUBDOMAINS = {"", "www", "mobile", "m"}

# /<handle>/status/<id>, /i/status/<id>, /i/web/status/<id>, /<handle>/statuses/<id>
# éventuellement suivi de /video/<n> ou /photo/<n>
_PATH_RE = re.compile(
    r"^/(?:(?P<handle>[A-Za-z0-9_]{1,30})|i/web|i)/status(?:es)?/(?P<id>\d{1,25})"
    r"(?:/(?P<kind>video|photo)/(?P<num>\d{1,2}))?(?:/.*)?$"
)


@dataclass(frozen=True)
class TweetRef:
    tweet_id: str
    handle: str | None
    media_number: int | None  # /video/<n> dans l'URL (1-based), si présent

    @property
    def canonical_url(self) -> str:
        return f"https://x.com/i/status/{self.tweet_id}"

    @property
    def display_url(self) -> str:
        return f"https://x.com/{self.handle or 'i'}/status/{self.tweet_id}"


def parse_tweet_url(url: str) -> TweetRef:
    if not isinstance(url, str) or not url.strip():
        raise InvalidUrlError("URL manquante : fournissez le lien d'un post x.com ou twitter.com.")
    raw = url.strip()
    if len(raw) > 2048:
        raise InvalidUrlError("URL trop longue.")
    if "://" not in raw:
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError(f"URL invalide : {url!r}") from exc

    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidUrlError("Seules les URLs http(s) de x.com / twitter.com sont acceptées.")
    if parts.username or parts.password or port not in (None, 80, 443):
        raise InvalidUrlError("URL refusée (identifiants ou port non standard).")

    host = (parts.hostname or "").lower().rstrip(".")
    sub, domain = host.split(".", 1) if host.count(".") >= 2 else ("", host)
    if domain not in ALLOWED_DOMAINS or sub not in ALLOWED_SUBDOMAINS:
        raise InvalidUrlError(
            f"Domaine non autorisé : {host or '(vide)'}. Seuls les posts x.com / twitter.com sont acceptés."
        )

    match = _PATH_RE.match(parts.path or "/")
    if not match:
        raise InvalidUrlError(
            "Ce lien ne ressemble pas à un post X. Format attendu : https://x.com/<compte>/status/<id>."
        )

    handle = match.group("handle")
    media_number = int(match.group("num")) if match.group("kind") == "video" else None
    return TweetRef(tweet_id=match.group("id"), handle=handle, media_number=media_number)
