"""Configuration lue depuis les variables d'environnement (et un éventuel fichier .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

MIN_TOKEN_LENGTH = 32


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu : {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre (reçu : {raw!r})") from exc


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    mcp_auth_token: str
    host: str = "0.0.0.0"
    port: int = 8000
    transcription_model: str = "openai/whisper-large-v3-turbo"
    max_video_minutes: float = 30.0
    rate_limit_per_hour: int = 30
    cookies_path: Path | None = None
    data_dir: Path = Path("/data")
    allowed_hosts: list[str] = field(default_factory=list)
    max_upload_mb: float = 24.0
    chunk_max_seconds: int = 300
    audio_bitrate: str = "32k"
    max_concurrent_jobs: int = 2
    log_level: str = "INFO"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    provider_order: list[str] = field(default_factory=list)
    provider_ignore: list[str] = field(default_factory=list)
    allowed_sites: list[str] = field(default_factory=list)  # vide = tous les sites connus de yt-dlp
    allow_generic: bool = True  # liens directs (mp3, mp4), flux RSS, pages avec vidéo intégrée
    max_post_images: int = 20  # images max renvoyées pour un post photo / carrousel
    max_images_mb: float = 5.0  # budget total des images d'une réponse (au-delà : réencodage plus petit)

    @property
    def max_images_bytes(self) -> int:
        return int(self.max_images_mb * 1024 * 1024)

    @property
    def cache_db_path(self) -> Path:
        return self.data_dir / "cache.sqlite3"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def max_video_seconds(self) -> float:
        return self.max_video_minutes * 60

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "oui", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "non", "off"):
        return False
    raise ConfigError(f"{name} doit valoir true ou false (reçu : {raw!r})")


def _env_list(name: str, default: str) -> list[str]:
    raw = _env(name, default) or ""
    if raw.lower() in ("none", "-", "aucun"):
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_settings(*, require_token: bool = True) -> Settings:
    """Charge la config. `require_token=False` sert au script de test en mode direct."""
    load_dotenv(override=False)

    api_key = _env("OPENROUTER_API_KEY")
    if not api_key:
        raise ConfigError("OPENROUTER_API_KEY est obligatoire.")

    token = _env("MCP_AUTH_TOKEN", "") or ""
    if require_token:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"MCP_AUTH_TOKEN est obligatoire et doit faire au moins {MIN_TOKEN_LENGTH} caractères. "
                'Générez-en un avec : python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if "/" in token or any(c.isspace() for c in token):
            raise ConfigError("MCP_AUTH_TOKEN ne doit contenir ni '/' ni espace (il est utilisé dans l'URL).")

    cookies_raw = _env("COOKIES_PATH")
    cookies_path = Path(cookies_raw) if cookies_raw else None

    allowed_hosts = [h.strip() for h in (_env("ALLOWED_HOSTS", "") or "").split(",") if h.strip()]

    settings = Settings(
        openrouter_api_key=api_key,
        mcp_auth_token=token,
        host=_env("HOST", "0.0.0.0") or "0.0.0.0",
        port=_env_int("PORT", 8000),
        transcription_model=_env("TRANSCRIPTION_MODEL", "openai/whisper-large-v3-turbo")
        or "openai/whisper-large-v3-turbo",
        max_video_minutes=_env_float("MAX_VIDEO_MINUTES", 30.0),
        rate_limit_per_hour=_env_int("RATE_LIMIT_PER_HOUR", 30),
        cookies_path=cookies_path,
        data_dir=Path(_env("DATA_DIR", "/data") or "/data"),
        allowed_hosts=allowed_hosts,
        max_upload_mb=min(_env_float("MAX_UPLOAD_MB", 24.0), 25.0),
        chunk_max_seconds=max(60, _env_int("CHUNK_MAX_SECONDS", 300)),
        audio_bitrate=_env("AUDIO_BITRATE", "32k") or "32k",
        max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 2)),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        openrouter_base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        or "https://openrouter.ai/api/v1",
        # whisper-large-v3-turbo via DeepInfra perd du texte aux frontières de ses fenêtres de 30 s
        # (constaté en test) : on privilégie PrimeIntellect, avec repli automatique sur les autres.
        provider_order=_env_list("TRANSCRIPTION_PROVIDER_ORDER", "primeintellect"),
        provider_ignore=_env_list("TRANSCRIPTION_PROVIDER_IGNORE", ""),
        allowed_sites=_env_list("ALLOWED_SITES", ""),
        allow_generic=_env_bool("ALLOW_GENERIC", True),
        max_post_images=max(1, _env_int("MAX_POST_IMAGES", 20)),
        max_images_mb=max(0.5, _env_float("MAX_IMAGES_TOTAL_MB", 5.0)),
    )
    if settings.max_video_minutes <= 0:
        raise ConfigError("MAX_VIDEO_MINUTES doit être > 0.")
    if settings.rate_limit_per_hour <= 0:
        raise ConfigError("RATE_LIMIT_PER_HOUR doit être > 0.")
    return settings
