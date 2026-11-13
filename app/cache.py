"""Cache SQLite des transcripts. Clé : (tweet_id, video_index, langue demandée)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

AUTO = "auto"


class TranscriptCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                tweet_id     TEXT    NOT NULL,
                video_index  INTEGER NOT NULL,
                language     TEXT    NOT NULL,
                created_at   INTEGER NOT NULL,
                model        TEXT    NOT NULL,
                payload      TEXT    NOT NULL,
                PRIMARY KEY (tweet_id, video_index, language)
            )
            """
        )
        self._conn.commit()

    def get(self, tweet_id: str, video_index: int, language: str | None) -> dict[str, Any] | None:
        """Renvoie le résultat en cache.

        Si une langue est imposée, une entrée « auto » dont la langue détectée
        correspond est aussi acceptée.
        """
        wanted = language or AUTO
        with self._lock:
            rows = self._conn.execute(
                "SELECT language, created_at, payload FROM transcripts "
                "WHERE tweet_id = ? AND video_index = ? AND language IN (?, ?)",
                (tweet_id, video_index, wanted, AUTO),
            ).fetchall()
        by_lang = {lang: (created, payload) for lang, created, payload in rows}
        if wanted in by_lang:
            return self._decode(*by_lang[wanted])
        if language and AUTO in by_lang:
            data = self._decode(*by_lang[AUTO])
            detected = (data.get("transcript", {}).get("language") or "").lower()
            if detected in (language.lower(), _LANG_NAMES.get(language.lower(), "")):
                return data
        return None

    def put(self, tweet_id: str, video_index: int, language: str | None, model: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO transcripts (tweet_id, video_index, language, created_at, model, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tweet_id, video_index, language or AUTO, int(time.time()), model, json.dumps(payload)),
            )
            self._conn.commit()

    @staticmethod
    def _decode(created_at: int, payload: str) -> dict[str, Any]:
        data = json.loads(payload)
        data["cached_at"] = created_at
        return data

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Whisper (verbose_json) renvoie souvent le nom complet de la langue.
_LANG_NAMES = {
    "fr": "french",
    "en": "english",
    "es": "spanish",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
    "ar": "arabic",
    "ja": "japanese",
    "zh": "chinese",
    "ru": "russian",
    "uk": "ukrainian",
    "ko": "korean",
    "tr": "turkish",
    "pl": "polish",
    "hi": "hindi",
}
