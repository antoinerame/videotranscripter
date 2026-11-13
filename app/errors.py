"""Erreurs « attendues », dont le message est renvoyé tel quel à Claude."""

from __future__ import annotations


class TranscriptError(Exception):
    """Erreur anticipée : le message est lisible et destiné au LLM."""


class InvalidUrlError(TranscriptError):
    pass


class NoVideoError(TranscriptError):
    pass


class TweetUnavailableError(TranscriptError):
    pass


class LoginRequiredError(TranscriptError):
    pass


class XBlockedError(TranscriptError):
    pass


class VideoTooLongError(TranscriptError):
    pass


class RateLimitedError(TranscriptError):
    pass


class TranscriptionError(TranscriptError):
    pass


class DownloadError(TranscriptError):
    pass
