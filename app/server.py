"""Serveur MCP distant (streamable HTTP) exposant l'outil get_media_content."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field

from .auth import TokenAuthMiddleware
from .config import ConfigError, Settings, load_settings
from .errors import TranscriptError
from .service import TranscriptService

log = logging.getLogger("video_transcript")

INSTRUCTIONS = (
    "Ce serveur récupère le contenu de vidéos, d'audios et de posts en ligne : transcript (texte parlé), "
    "images clés des vidéos, images des posts photo, et métadonnées. Sources : YouTube, X/Twitter, Instagram, "
    "TikTok, LinkedIn, Facebook, Bluesky, Vimeo, SoundCloud, podcasts (Apple Podcasts, flux RSS, lien mp3 direct) "
    "et la plupart des sites pris en charge par yt-dlp / gallery-dl. Appelez get_media_content avec le lien. "
    "Les résultats sont mis en cache : rappeler l'outil sur le même lien est gratuit et instantané."
)

TOOL_DESCRIPTION = """Récupère le contenu d'une vidéo, d'un audio ou d'un post en ligne à partir de son URL.

- Vidéo / audio : transcript (texte parlé) horodaté par segment [mm:ss], plus, sur demande, des
  images clés de la vidéo (chacune précédée de son timestamp [mm:ss] pour la relier au transcript).
- Post sans vidéo (photos seules, carrousel Instagram…) : les images du post.
- Post mixte (photos + vidéos) : la vidéo demandée, toutes les photos du post (avec video_index=0)
  et la liste de ses éléments ; rappeler avec video_index=1, 2… pour les autres vidéos.
- Vidéo muette / GIF : pas de transcript, mais métadonnées, photos et images clés si demandées.
- Dans tous les cas : plateforme, titre, auteur/chaîne, date, description ou texte du post,
  engagement (likes, vues…), durée, langue détectée.

Sites : YouTube (vidéos, shorts), X/Twitter, Instagram, TikTok, LinkedIn, Facebook, Bluesky, Vimeo,
Dailymotion, Twitch (rediffusions), SoundCloud, Apple Podcasts, flux RSS de podcast, liens directs
vers un fichier audio/vidéo/image, et la plupart des sites gérés par yt-dlp / gallery-dl.

Mettre include_frames=true quand la vidéo contient probablement des graphiques, des slides ou du
texte à l'écran, ou quand l'utilisateur parle d'un élément visuel. Sinon le laisser à false : les
images coûtent du contexte et du temps de traitement.

À utiliser quand l'utilisateur partage un lien de vidéo, de post ou d'épisode de podcast et veut
savoir ce qui s'y dit ou s'y montre (résumé, citations, vérification…). Les directs en cours ne
sont pas pris en charge et la durée est plafonnée (30 min par défaut). Certains sites (Instagram,
LinkedIn, contenus restreints) peuvent exiger que le serveur ait des cookies."""


def build_server(service: TranscriptService) -> MCPServer:
    mcp = MCPServer("media-content", instructions=INSTRUCTIONS, version="3.0.0")

    @mcp.tool(
        name="get_media_content",
        title="Contenu d'une vidéo, d'un audio ou d'un post",
        description=TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Contenu d'une vidéo, d'un audio ou d'un post",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    async def get_media_content(
        url: Annotated[
            str,
            Field(
                description="Lien de la vidéo, du post ou de l'épisode : YouTube, x.com, Instagram, TikTok, "
                "LinkedIn, Bluesky, Apple Podcasts, flux RSS, lien direct .mp3/.mp4/.jpg…"
            ),
        ],
        ctx: Context,
        video_index: Annotated[
            int,
            Field(
                description="Index de l'élément quand l'URL en contient plusieurs : post à plusieurs vidéos, "
                "playlist, flux RSS de podcast (0 = le premier, ou l'épisode le plus récent d'un flux).",
                ge=0,
                le=49,
            ),
        ] = 0,
        language: Annotated[
            str | None,
            Field(
                description="Code langue ISO-639-1 du média (ex. 'fr', 'en') pour forcer la langue. "
                "Laisser vide pour la détection automatique."
            ),
        ] = None,
        include_frames: Annotated[
            bool,
            Field(
                description="Ajouter des images clés de la vidéo (changements de scène). Mettre true quand la "
                "vidéo contient probablement des graphiques, des slides ou du texte à l'écran, ou quand "
                "l'utilisateur parle d'un élément visuel."
            ),
        ] = False,
        max_frames: Annotated[
            int,
            Field(description="Nombre maximum d'images clés si include_frames=true (1 à 12).", ge=1, le=12),
        ] = 6,
    ) -> list[str | Image]:
        async def progress(fraction: float, message: str) -> None:
            try:
                await ctx.report_progress(fraction, 1.0, message)
            except Exception:  # la progression est facultative
                pass

        try:
            return await service.get_content(url, video_index, language, include_frames, max_frames, progress)
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
        "Démarrage : port %d | modèle %s (fournisseurs : ordre=%s ignorés=%s) | max %.0f min/média | "
        "%d transcriptions/h | sites %s | liens directs %s | cookies %s",
        settings.port,
        settings.transcription_model,
        ",".join(settings.provider_order) or "auto",
        ",".join(settings.provider_ignore) or "aucun",
        settings.max_video_minutes,
        settings.rate_limit_per_hour,
        ",".join(settings.allowed_sites) or "tous (yt-dlp)",
        "oui" if settings.allow_generic else "non",
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
