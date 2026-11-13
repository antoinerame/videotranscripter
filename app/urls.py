"""Validation des URLs avant de les confier à yt-dlp.

- http(s) uniquement, pas d'identifiants, ports 80/443 seulement ;
- l'hôte doit résoudre vers des adresses IP publiques (pas de réseau local, loopback,
  métadonnées cloud…) : protège le serveur maison contre le SSRF, par exemple via une
  injection de prompt qui ferait appeler l'outil sur http://192.168.1.1/ ;
- un extracteur yt-dlp autorisé doit reconnaître l'URL (ALLOWED_SITES, ALLOW_GENERIC) ;
- l'ID du média est calculé hors ligne pour servir de clé de cache.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yt_dlp

from .errors import InvalidUrlError

log = logging.getLogger(__name__)

_X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
_X_VIDEO_SUFFIX = re.compile(r"^(?P<base>/[^/]+(?:/web)?/status(?:es)?/\d+)/video/(?P<num>\d{1,2})/?$")


@dataclass(frozen=True)
class MediaRef:
    url: str  # URL nettoyée, passée à yt-dlp
    extractor: str  # ie_key yt-dlp ("Youtube", "Twitter", "Generic"…)
    media_id: str | None  # ID calculé hors ligne (None pour l'extracteur générique)
    index_hint: int | None = None  # .../status/<id>/video/<n> sur X (0-based)

    @property
    def is_generic(self) -> bool:
        return self.extractor == "Generic"

    @property
    def request_key(self) -> str | None:
        """Clé de cache utilisable sans requête réseau (None si l'URL ne désigne pas un média stable)."""
        return f"{self.extractor}:{self.media_id}" if self.media_id and not self.is_generic else None


def media_key(info: dict[str, Any]) -> str:
    """Clé de cache définitive d'un média extrait (plateforme + ID)."""
    extractor = info.get("extractor_key") or info.get("ie_key") or "unknown"
    media_id = str(info.get("id") or "")
    if extractor == "Generic" or not media_id:
        # l'ID générique est souvent le nom de fichier ("episode.mp3") : on y ajoute l'URL
        src = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
        media_id = f"{media_id}-{hashlib.sha256(src.encode()).hexdigest()[:16]}"
    return f"{extractor}:{media_id}"


def _ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def ensure_public_host(url: str) -> None:
    """Refuse les URLs dont l'hôte résout vers une adresse non publique. Bloquant (DNS)."""
    host = urlsplit(url).hostname
    if not host:
        raise InvalidUrlError("URL sans nom d'hôte.")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except socket.gaierror as exc:
        raise InvalidUrlError(f"Nom de domaine introuvable : {host}") from exc
    bad = sorted(a for a in addrs if not _ip_is_public(a))
    if bad or not addrs:
        log.warning("URL refusée (adresse non publique) : %s -> %s", host, ", ".join(bad) or "aucune")
        raise InvalidUrlError(
            f"URL refusée : {host} pointe vers une adresse réseau privée ou locale. "
            "Seuls les contenus publics d'Internet sont acceptés."
        )


class UrlPolicy:
    def __init__(self, allowed_sites: list[str] | None = None, allow_generic: bool = True):
        if allowed_sites:
            # "youtube" autorise aussi "youtube:tab", "youtube:shorts"… (noms d'extracteurs yt-dlp)
            self.allowed_extractors = [f"{re.escape(s.lower())}(:.*)?" for s in allowed_sites]
            if allow_generic:
                self.allowed_extractors.append("generic")
        else:
            self.allowed_extractors = ["default"] if allow_generic else ["default", "-generic"]
        self.allow_generic = allow_generic
        ydl = yt_dlp.YoutubeDL({"quiet": True, "allowed_extractors": self.allowed_extractors})
        # mêmes extracteurs, dans le même ordre que yt-dlp (le générique en dernier)
        self._extractors = list(ydl._ies.values())
        if not self._extractors:
            raise ValueError("ALLOWED_SITES ne correspond à aucun extracteur yt-dlp.")

    def check(self, url: str) -> MediaRef:
        if not isinstance(url, str) or not url.strip():
            raise InvalidUrlError("URL manquante.")
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
            raise InvalidUrlError("Seules les URLs http(s) sont acceptées.")
        if parts.username or parts.password:
            raise InvalidUrlError("URL refusée : elle contient des identifiants.")
        if port not in (None, 80, 443):
            raise InvalidUrlError("URL refusée : port non standard (seuls 80 et 443 sont acceptés).")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or "." not in host:
            raise InvalidUrlError(f"Nom d'hôte invalide : {host or '(vide)'}")
        try:
            if not _ip_is_public(host):
                raise InvalidUrlError("URL refusée : adresse IP privée ou locale.")
        except ValueError:
            pass  # c'est un nom de domaine, vérifié par ensure_public_host()

        path, index_hint = parts.path, None
        if host in _X_HOSTS and (m := _X_VIDEO_SUFFIX.match(path)):
            path, index_hint = m.group("base"), int(m.group("num")) - 1
        clean = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))

        for ie in self._extractors:
            if ie.suitable(clean):
                key = ie.ie_key()
                media_id = None if key == "Generic" else ie.get_temp_id(clean)
                return MediaRef(clean, key, media_id, index_hint)
        if self.allow_generic:  # pragma: no cover - le générique accepte toute URL
            return MediaRef(clean, "Generic", None, index_hint)
        raise InvalidUrlError(
            f"Site non pris en charge : {host}. "
            + (
                "Seuls les sites listés dans ALLOWED_SITES sont autorisés."
                if self.allowed_extractors[0] != "default"
                else "yt-dlp ne connaît pas ce site, et les liens directs/pages génériques sont désactivés "
                "(ALLOW_GENERIC=false)."
            )
        )
