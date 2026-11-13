# x-transcript-mcp

Serveur MCP distant, auto-hébergé, qui permet à Claude (claude.ai, en connecteur personnalisé)
de récupérer le **transcript des vidéos de posts X/Twitter**.

Pipeline : URL x.com → cache SQLite → yt-dlp (métadonnées + audio) → ffmpeg (mp3 mono 16 kHz, 32 kb/s)
→ découpage si besoin → OpenRouter `/api/v1/audio/transcriptions` → cache.

- Transport MCP **streamable HTTP**, SDK officiel `mcp` v2 (où `FastMCP` s'appelle désormais `MCPServer`).
- Écoute en HTTP simple sur `PORT` : le HTTPS est géré par votre reverse proxy.
- Un outil : `get_x_transcript(url, video_index=0, language=None)`.

## Installation

```bash
git clone <ce dépôt> x-transcript-mcp && cd x-transcript-mcp
cp .env.example .env
# Générez un token et collez-le dans MCP_AUTH_TOKEN :
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Renseignez OPENROUTER_API_KEY, puis :
mkdir -p data cookies
docker compose up -d --build
docker compose logs -f
```

Vérification : `curl http://127.0.0.1:8000/health` → `{"status": "ok"}`.

Par défaut le port est publié sur `127.0.0.1` uniquement. Si votre reverse proxy tourne
sur une autre machine, mettez `BIND_ADDRESS=0.0.0.0` dans `.env`.

## Ajouter le connecteur dans claude.ai

Dans claude.ai : **Paramètres → Connecteurs → Ajouter un connecteur personnalisé**, puis :

- Nom : `Transcripts X`, par exemple
- URL du serveur MCP distant : **`https://<votre-domaine>/mcp/<MCP_AUTH_TOKEN>`**
- Laissez vides les champs OAuth (Client ID / Secret).

Le token se trouve dans le chemin parce que claude.ai ne permet pas d'ajouter un header.
Pour d'autres clients MCP, le serveur accepte aussi `POST /mcp` avec `Authorization: Bearer <token>`.

Ensuite, dans une conversation : *« Résume la vidéo de ce post : https://x.com/… »*.

## Reverse proxy

- Transmettez tout le chemin `/mcp/…` au port local.
- Les réponses sont envoyées en **SSE**. Désactivez le buffering et autorisez des réponses
  longues : une transcription de 30 min prend environ 30 à 90 s.
  Le serveur envoie un ping toutes les 15 s pour garder la connexion ouverte.
  - nginx : `proxy_buffering off; proxy_read_timeout 300s; proxy_http_version 1.1;`
  - Caddy : `reverse_proxy 127.0.0.1:8000 { flush_interval -1 }`
- ⚠️ **Le token apparaît dans l'URL** : les logs d'accès de votre reverse proxy le contiennent.
  Masquez ce chemin dans les logs, ou désactivez-les pour ce vhost. Le serveur lui-même
  n'écrit jamais le token dans ses logs : il affiche `/mcp/***`.
- Optionnel : `ALLOWED_HOSTS=mcp.example.com` active la protection DNS-rebinding du SDK MCP.
  Seuls les headers `Host` listés sont alors acceptés.

## Configuration (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `OPENROUTER_API_KEY` | — (obligatoire) | Clé API OpenRouter |
| `MCP_AUTH_TOKEN` | — (obligatoire, ≥ 32 car.) | Token d'accès au serveur |
| `PORT` | `8000` | Port HTTP d'écoute |
| `TRANSCRIPTION_MODEL` | `openai/whisper-large-v3-turbo` | Modèle OpenRouter de speech-to-text |
| `MAX_VIDEO_MINUTES` | `30` | Durée max d'une vidéo. Au-delà, refus avant tout téléchargement |
| `RATE_LIMIT_PER_HOUR` | `30` | Transcriptions par heure glissante (les hits de cache ne comptent pas) |
| `COOKIES_PATH` | vide | ex. `/cookies/cookies.txt` (voir plus bas) |

Variables avancées (voir `.env.example`) : `BIND_ADDRESS`, `ALLOWED_HOSTS`, `MAX_UPLOAD_MB` (24),
`CHUNK_MAX_SECONDS` (600), `AUDIO_BITRATE` (32k), `LOG_LEVEL`, `PUID`/`PGID`, `YTDLP_AUTO_UPDATE` (1).

## Cookies (si X bloque ou exige une connexion)

1. Avec un navigateur connecté à X, exportez les cookies de `x.com` au format **Netscape**
   (extension « Get cookies.txt LOCALLY » ou équivalent).
2. Enregistrez-les dans `./cookies/cookies.txt`, puis mettez `COOKIES_PATH=/cookies/cookies.txt` dans `.env`.
3. `docker compose up -d`.

Le fichier est monté en lecture seule et copié dans un dossier temporaire à chaque requête,
car yt-dlp le réécrit. Il doit être lisible par l'UID du conteneur : 1000 par défaut,
modifiable avec `PUID`/`PGID`. Utilisez de préférence un compte X secondaire.

## Tester

Le script appelle l'outil MCP sur l'URL passée en argument :

```bash
# Dans le conteneur, à travers le vrai serveur HTTP (teste aussi le transport et l'auth) :
docker compose exec -u app x-transcript python scripts/test_transcript.py \
    "https://x.com/compte/status/123" --server http://localhost:8000

# En local, sans Docker (Python 3.12 + ffmpeg installés), dans le même processus :
pip install -r requirements.txt
python scripts/test_transcript.py "https://x.com/compte/status/123" --language fr
```

Options : `--video-index N`, `--language xx`, `--bearer` (token en header plutôt que dans le chemin).
Tests unitaires : `pip install pytest && pytest tests`.

## Comportement

- **URLs acceptées** : `x.com`, `twitter.com` (et `www.`, `mobile.`), au format `/<compte>/status/<id>`,
  `/i/status/<id>` ou `…/video/<n>`. Avec `…/video/2`, la 2e vidéo est choisie si `video_index` vaut 0.
  yt-dlp ne reçoit jamais l'URL fournie : il reçoit `https://x.com/i/status/<id>` reconstruite,
  et seuls ses extracteurs Twitter sont autorisés. Un post qui renvoie vers YouTube, par exemple,
  n'est donc pas suivi.
- **Cache** : clé `(tweet_id, video_index, langue demandée)`. Une entrée en détection automatique est
  réutilisée si la langue détectée correspond à celle demandée. La base se trouve dans `./data/cache.sqlite3`.
- **Taille / découpage** : l'API accepte 25 Mo par fichier, et OpenRouter coupe les requêtes vers les
  fournisseurs au bout d'environ 60 s. L'audio est donc découpé en morceaux de 10 min maximum
  (environ 2,4 Mo à 32 kb/s). Les morceaux sont transcrits en parallèle, puis les segments sont
  recollés avec décalage des timestamps.
- **Timestamps** : le serveur demande `response_format=verbose_json` avec
  `timestamp_granularities=["segment"]`. La doc OpenRouter précise que le support dépend du
  fournisseur qui sert le modèle. Si `verbose_json` est refusé (HTTP 400), le serveur repasse
  automatiquement en `json` (texte brut, horodaté par morceau) et retient ce choix jusqu'au redémarrage.
- **Coût** : chaque transcription est loggée avec la durée facturée (`usage.seconds`), le coût estimé
  (`usage.seconds` × prix/seconde lu sur l'API publique des modèles OpenRouter) et le coût réel
  (`usage.cost`) quand OpenRouter le renvoie. Whisper Large V3 Turbo coûte environ 0,0002 $/min.
- **Rate limit** : en mémoire, remis à zéro au redémarrage. Une requête qui échoue avant l'appel
  OpenRouter (pas de vidéo, vidéo trop longue, post introuvable…) ne consomme pas de quota.
- **Erreurs** renvoyées à Claude en clair : URL non-X, post sans vidéo, post introuvable / privé / supprimé,
  connexion requise (contenu sensible), X qui bloque (avec suggestion d'ajouter des cookies),
  vidéo trop longue, vidéo sans piste audio, rate limit, échec OpenRouter (clé invalide, crédits épuisés,
  fournisseur indisponible…).

Exemple de log :

```
x_transcript: tweet=1724884212803834154 video=0 lang=auto -> OK | durée vidéo 5:41 | 1 morceau(x) |
  audio facturé 342s | coût ~$0.00114 estimé / $0.00114 facturé | timestamps=True |
  dl 12.7s + transcription 4.4s = 17.1s | quota restant 29/h
```

## Structure

```
app/
  server.py       MCPServer + outil get_x_transcript + app ASGI (uvicorn)
  auth.py         auth par token (chemin ou Bearer), comparaison en temps constant, logs sans token
  service.py      pipeline, cache, rate limit, concurrence
  downloader.py   yt-dlp (métadonnées, audio) + ffmpeg (conversion, découpage)
  transcriber.py  appels OpenRouter (SDK OpenAI), repli json, fusion des morceaux, coût
  formatting.py   texte structuré renvoyé à Claude
  xurl.py / cache.py / ratelimit.py / config.py / errors.py
scripts/test_transcript.py
```
