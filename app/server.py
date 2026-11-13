"""Serveur MCP distant (streamable HTTP) exposant l'outil get_x_transcript."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field

from .auth import TokenAuthMiddleware
from .config import ConfigError, Settings, load_settings
from .errors import TranscriptError
from .service import TranscriptService

log = logging.getLogger("x_transcript")

INSTRUCTIONS = (
    "Ce serveur récupère le transcript (texte parlé) des vidéos publiées dans des posts X/Twitter. "
    "Appelez get_x_transcript avec le lien du post. Les résultats sont mis en cache : rappeler l'outil "
    "sur le même post est gratuit et instantané."
)

TOOL_DESCRIPTION = """Récupère le transcript de la vidéo d'un post X (x.com / twitter.com).

Renvoie un texte structuré : auteur (nom + @handle), date, texte du post, likes/reposts,
durée de la vidéo, langue détectée, puis le transcript (horodaté par segment [mm:ss]
quand le modèle le permet).

À utiliser quand l'utilisateur partage un lien de post X contenant une vidéo et veut
savoir ce qui s'y dit (résumé, citation, vérification…). Seules les URLs x.com /
twitter.com sont acceptées. Durée de vidéo limitée (30 min par défaut)."""


def build_server(service: TranscriptService) -> MCPServer:
    mcp = MCPServer("x-video-transcript", instructions=INSTRUCTIONS, version="1.0.0")

    @mcp.tool(
        name="get_x_transcript",
        title="Transcript d'une vidéo X/Twitter",
        description=TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Transcript d'une vidéo X/Twitter",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def get_x_transcript(
        url: Annotated[
            str, Field(description="Lien d'un post X, ex. https://x.com/compte/status/1234567890123456789")
        ],
        ctx: Context,
        video_index: Annotated[
            int,
            Field(description="Index de la vidéo si le post en contient plusieurs (0 = la première).", ge=0, le=50),
        ] = 0,
        language: Annotated[
            str | None,
            Field(
                description="Code langue ISO-639-1 de la vidéo (ex. 'fr', 'en') pour forcer la langue. "
                "Laisser vide pour la détection automatique."
            ),
        ] = None,
    ) -> str:
        async def progress(fraction: float, message: str) -> None:
            try:
                await ctx.report_progress(fraction, 1.0, message)
            except Exception:  # la progression est facultative
                pass

        try:
            return await service.get_transcript(url, video_index, language, progress)
        except TranscriptError as exc:
            raise ToolError(str(exc)) from exc

    return mcp


def create_app(settings: Settings):
    service = TranscriptService(settings)
    mcp = build_server(service)

    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*settings.allowed_hosts, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[f"https://{h}" for h in settings.allowed_hosts] + ["https://claude.ai"],
        )
    else:
        # Derrière un reverse proxy, le header Host est le nom public : la protection
        # DNS-rebinding par défaut (localhost uniquement) bloquerait tout. L'auth par token
        # reste obligatoire sur chaque requête.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # Mode stateless : chaque requête est indépendante (robuste aux redémarrages du conteneur).
    # Réponses en SSE : des pings réguliers gardent la connexion ouverte pendant les transcriptions longues.
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=False,
        transport_security=security,
        host=settings.host,
    )

    original_lifespan = inner.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app) as state:
            try:
                yield state
            finally:
                await service.aclose()

    inner.router.lifespan_context = lifespan
    return TokenAuthMiddleware(inner, settings.mcp_auth_token, mcp_path="/mcp"), mcp, service


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    for noisy in ("httpx2", "httpcore", "openai", "mcp", "sse_starlette", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    import uvicorn

    try:
        settings = load_settings(require_token=True)
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        sys.exit(2)

    setup_logging(settings.log_level)
    app, _, _ = create_app(settings)
    log.info(
        "Démarrage : port %d | modèle %s (fournisseurs : ordre=%s ignorés=%s) | max %.0f min/vidéo | "
        "%d transcriptions/h | cookies %s",
        settings.port,
        settings.transcription_model,
        ",".join(settings.provider_order) or "auto",
        ",".join(settings.provider_ignore) or "aucun",
        settings.max_video_minutes,
        settings.rate_limit_per_hour,
        settings.cookies_path if settings.cookies_path and settings.cookies_path.is_file() else "non",
    )
    log.info("Endpoint MCP : http://<hôte>:%d/mcp/<MCP_AUTH_TOKEN>  (ou /mcp + Authorization: Bearer)", settings.port)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        access_log=False,  # le log d'accès uvicorn contiendrait le token présent dans l'URL
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
