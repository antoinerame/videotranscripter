"""Cache SQLite des transcripts. Clé : (média, langue demandée).

Les images (posts photo, images clés de vidéos) sont stockées en JPEG optimisé, dans les tables
media_images / media_image_blobs, avec les mêmes clés.

Le média est identifié par « plateforme:id » (ex. « Youtube:dQw4w9WgXcQ », « Twitter:1724883339285544960 »).
On enregistre aussi un alias « requête » (ex. « Twitter:<id du post> ») quand l'URL désigne un média unique :
un rappel sur la même URL est alors servi sans aucune requête réseau.
"""

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
            CREATE TABLE IF NOT EXISTS media_transcripts (
                media_key   TEXT    NOT NULL,
                language    TEXT    NOT NULL,
                created_at  INTEGER NOT NULL,
                model       TEXT    NOT NULL,
                payload     TEXT    NOT NULL,
                PRIMARY KEY (media_key, language)
            )
            """
        )
        # images : posts photo (variant « post ») et images clés de vidéos (variant « frames-<n> »)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_images (
                media_key   TEXT    NOT NULL,
                variant     TEXT    NOT NULL,
                created_at  INTEGER NOT NULL,
                info        TEXT    NOT NULL,  -- JSON : {"meta": ..., "items": [{label, timestamp, width, height}]}
                PRIMARY KEY (media_key, variant)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_image_blobs (
                media_key   TEXT    NOT NULL,
                variant     TEXT    NOT NULL,
                idx         INTEGER NOT NULL,
                jpeg        BLOB    NOT NULL,
                PRIMARY KEY (media_key, variant, idx)
            )
            """
        )
        self._migrate_v1()
        self._conn.commit()

    def _migrate_v1(self) -> None:
        """Reprend le cache de la version « X uniquement » (table transcripts)."""
        has_v1 = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transcripts'"
        ).fetchone()
        if not has_v1:
            return
        rows = self._conn.execute(
            "SELECT tweet_id, language, created_at, model, payload FROM transcripts WHERE video_index = 0"
        ).fetchall()
        for tweet_id, language, created_at, model, payload in rows:
            data = json.loads(payload)
            data["meta"] = legacy_meta(data.get("meta", {}))
            self._conn.execute(
                "INSERT OR IGNORE INTO media_transcripts VALUES (?, ?, ?, ?, ?)",
                (f"Twitter:{tweet_id}", language, created_at, model, json.dumps(data)),
            )
        self._conn.execute("DROP TABLE transcripts")

    def get(self, key: str, language: str | None) -> dict[str, Any] | None:
        """Renvoie le résultat en cache.

        Si une langue est imposée, une entrée « auto » dont la langue détectée correspond est aussi acceptée.
        """
        wanted = language or AUTO
        with self._lock:
            rows = self._conn.execute(
                "SELECT language, created_at, payload FROM media_transcripts WHERE media_key = ? AND language IN (?, ?)",
                (key, wanted, AUTO),
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

    def put(self, keys: list[str], language: str | None, model: str, payload: dict[str, Any]) -> None:
        now = int(time.time())
        blob = json.dumps(payload)
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO media_transcripts (media_key, language, created_at, model, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                [(k, language or AUTO, now, model, blob) for k in dict.fromkeys(keys)],
            )
            self._conn.commit()

    def get_images(self, key: str, variant: str) -> tuple[dict[str, Any], list[bytes]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, info FROM media_images WHERE media_key = ? AND variant = ?", (key, variant)
            ).fetchone()
            if not row:
                return None
            blobs = [
                b
                for (b,) in self._conn.execute(
                    "SELECT jpeg FROM media_image_blobs WHERE media_key = ? AND variant = ? ORDER BY idx",
                    (key, variant),
                )
            ]
        info = json.loads(row[1])
        info["cached_at"] = row[0]
        if len(blobs) != len(info.get("items", [])):
            return None  # entrée incomplète : on la refait
        return info, blobs

    def put_images(self, keys: list[str], variant: str, info: dict[str, Any], blobs: list[bytes]) -> None:
        now = int(time.time())
        blob_info = json.dumps(info)
        with self._lock:
            for key in dict.fromkeys(keys):
                self._conn.execute("DELETE FROM media_image_blobs WHERE media_key = ? AND variant = ?", (key, variant))
                self._conn.execute(
                    "INSERT OR REPLACE INTO media_images (media_key, variant, created_at, info) VALUES (?, ?, ?, ?)",
                    (key, variant, now, blob_info),
                )
                self._conn.executemany(
                    "INSERT INTO media_image_blobs (media_key, variant, idx, jpeg) VALUES (?, ?, ?, ?)",
                    [(key, variant, i, b) for i, b in enumerate(blobs)],
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


def legacy_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Convertit les métadonnées de la v1 (posts X) au format générique."""
    if "tweet_id" not in meta:
        return meta
    return {
        "key": f"Twitter:{meta.get('tweet_id')}",
        "platform": "Twitter",
        "url": meta.get("url"),
        "title": None,
        "author": meta.get("author_name"),
        "author_id": meta.get("author_handle"),
        "timestamp": meta.get("timestamp"),
        "upload_date": None,
        "description": meta.get("text"),
        "like_count": meta.get("like_count"),
        "repost_count": meta.get("repost_count"),
        "comment_count": meta.get("comment_count"),
        "view_count": meta.get("view_count"),
        "duration": meta.get("duration"),
        "item_index": meta.get("video_index", 0),
        "item_count": meta.get("video_count", 1),
        "playlist_title": None,
    }


# Whisper (verbose_json) renvoie parfois le nom complet de la langue.
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
