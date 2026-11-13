"""Erreurs « attendues », dont le message est renvoyé tel quel à Claude."""

from __future__ import annotations


class TranscriptError(Exception):
    """Erreur anticipée : le message est lisible et destiné au LLM."""


class InvalidUrlError(TranscriptError):
    """URL refusée (format, site non pris en charge, adresse réseau privée…)."""


class NoMediaError(TranscriptError):
    """La page ne contient pas de vidéo/audio exploitable."""


class MediaUnavailableError(TranscriptError):
    """Contenu supprimé, privé, introuvable ou géo-bloqué."""


class LoginRequiredError(TranscriptError):
    pass


class SiteBlockedError(TranscriptError):
    """Le site bloque/limite les requêtes du serveur (anti-bot, rate limit)."""


class VideoTooLongError(TranscriptError):
    pass


class RateLimitedError(TranscriptError):
    pass


class TranscriptionError(TranscriptError):
    pass


class DownloadError(TranscriptError):
    pass
