#!/usr/bin/env python3
"""Appelle l'outil get_media_content sur une URL (YouTube, X, podcast…).

Deux modes :

  1. En local, dans ce processus (pas besoin de lancer le serveur) :
         python scripts/test_transcript.py https://www.youtube.com/watch?v=xxxxxxxxxxx

  2. Contre un serveur MCP qui tourne (teste aussi le transport HTTP et l'auth) :
         python scripts/test_transcript.py https://www.youtube.com/watch?v=xxxxxxxxxxx --server http://localhost:8000
     Le token est lu dans MCP_AUTH_TOKEN (.env) et envoyé dans le chemin (/mcp/<token>),
     ou en header Authorization si --bearer est passé.

     Dans le conteneur :
         docker compose exec x-transcript python scripts/test_transcript.py <url> --server http://localhost:8000

Options : --video-index N, --language fr, --frames [--max-frames 6], --save-images DOSSIER
Les images reçues (images clés, photos d'un post) sont enregistrées dans --save-images
(par défaut ./data/test-images/) pour pouvoir les regarder.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # pour que le .env du projet soit trouvé


async def run(args: argparse.Namespace) -> int:
    from mcp.client import Client

    from app.config import ConfigError, load_settings
    from app.server import setup_logging

    arguments: dict = {"url": args.url, "video_index": args.video_index}
    if args.language:
        arguments["language"] = args.language
    if args.frames:
        arguments["include_frames"] = True
        arguments["max_frames"] = args.max_frames

    if args.server:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        token = args.token or os.environ.get("MCP_AUTH_TOKEN")
        if not token:
            from dotenv import load_dotenv

            load_dotenv()
            token = os.environ.get("MCP_AUTH_TOKEN")
        if not token:
            print("MCP_AUTH_TOKEN introuvable (.env ou --token).", file=sys.stderr)
            return 2
        base = args.server.rstrip("/").removesuffix("/mcp")
        timeout = httpx2.Timeout(900.0, connect=10.0)
        if args.bearer:
            url = f"{base}/mcp"
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        else:
            url = f"{base}/mcp/{token}"
            http = httpx2.AsyncClient(timeout=timeout)
        print(f"→ Serveur : {base}/mcp/{'***' if not args.bearer else ''}", file=sys.stderr)
        # le client MCP limite par défaut un événement SSE à 1 Mio : trop peu pour une réponse avec images
        target = streamable_http_client(url, http_client=http, max_sse_event_size=None)
    else:
        try:
            settings = load_settings(require_token=False)
        except ConfigError as exc:
            print(f"Configuration invalide : {exc}", file=sys.stderr)
            return 2
        if not os.environ.get("DATA_DIR") and not Path("/data").is_dir():
            os.environ["DATA_DIR"] = str(ROOT / "data")
            settings = load_settings(require_token=False)
        setup_logging(settings.log_level)
        from app.server import build_server
        from app.service import TranscriptService

        service = TranscriptService(settings)
        target = build_server(service)
        print(f"→ Mode local (cache : {settings.cache_db_path})", file=sys.stderr)

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        print(f"  … {progress * 100:.0f}% {message or ''}", file=sys.stderr)

    t0 = time.monotonic()
    async with Client(target, read_timeout_seconds=900) as client:
        result = await client.call_tool("get_media_content", arguments, progress_callback=on_progress)
    elapsed = time.monotonic() - t0

    out_dir = Path(args.save_images or ROOT / "data" / "test-images")
    n_images, total_bytes = 0, 0
    for block in result.content:
        if getattr(block, "type", None) == "image":
            import base64

            data = base64.b64decode(block.data)
            n_images += 1
            total_bytes += len(data)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"image_{n_images:02d}.jpg"
            path.write_bytes(data)
            print(f"<image {block.mime_type}, {len(data) / 1024:.0f} Ko -> {path}>")
        else:
            print(getattr(block, "text", block))
    if n_images:
        print(f"\n{n_images} image(s), {total_bytes / 1024:.0f} Ko au total, enregistrées dans {out_dir}", file=sys.stderr)
    status = "ERREUR" if result.is_error else "OK"
    print(f"\n[{status}] en {elapsed:.1f}s", file=sys.stderr)
    return 1 if result.is_error else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Teste l'outil MCP get_media_content sur une URL (YouTube, X, podcast…).")
    parser.add_argument("url", help="URL de la vidéo, du post ou de l'épisode")
    parser.add_argument("--video-index", type=int, default=0, help="index de l'élément (post multi-vidéos, playlist, flux RSS ; défaut 0)")
    parser.add_argument("--language", help="code langue ISO-639-1 (défaut : détection auto)")
    parser.add_argument("--frames", action="store_true", help="demander les images clés (include_frames=true)")
    parser.add_argument("--max-frames", type=int, default=6, help="nombre max d'images clés (1-12, défaut 6)")
    parser.add_argument("--save-images", help="dossier où enregistrer les images reçues (défaut : data/test-images)")
    parser.add_argument("--server", help="URL de base d'un serveur en cours d'exécution, ex. http://localhost:8000")
    parser.add_argument("--token", help="token MCP (défaut : MCP_AUTH_TOKEN)")
    parser.add_argument("--bearer", action="store_true", help="envoyer le token en header Authorization")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
