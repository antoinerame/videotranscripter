# video-transcript-mcp

Serveur MCP distant, auto-hébergé, qui permet à Claude (claude.ai, en connecteur personnalisé)
de récupérer le **transcript de n'importe quelle vidéo ou audio en ligne** :
YouTube, X/Twitter, Instagram, TikTok, LinkedIn, Facebook, Vimeo, Dailymotion, Twitch, SoundCloud,
Apple Podcasts, flux RSS de podcast, liens directs `.mp3` / `.mp4`… bref, tout ce que gère [yt-dlp](https://github.com/yt-dlp/yt-dlp).

Pipeline : URL → contrôles de sécurité → cache SQLite → yt-dlp (métadonnées + audio) → ffmpeg
(mp3 mono 16 kHz, 32 kb/s) → découpage → OpenRouter `/api/v1/audio/transcriptions` → cache.

- Transport MCP **streamable HTTP**, SDK officiel `mcp` v2 (où `FastMCP` s'appelle désormais `MCPServer`).
- Écoute en HTTP simple sur `PORT` : le HTTPS est géré par votre reverse proxy.
- Un outil : `get_video_transcript(url, video_index=0, language=None)`.

## Installation

```bash
git clone <ce dépôt> video-transcript-mcp && cd video-transcript-mcp
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

**Mise à jour depuis la version « X uniquement »** : `git pull && docker compose up -d --build`.
Le cache existant est repris automatiquement. Dans claude.ai, l'outil `get_x_transcript` est remplacé
par `get_video_transcript` : ouvrez une nouvelle conversation, ou déconnectez puis reconnectez le
connecteur, pour que Claude voie le nouvel outil.

## Ajouter le connecteur dans claude.ai

Dans claude.ai : **Paramètres → Connecteurs → Ajouter un connecteur personnalisé**, puis :

- Nom : `Transcripts vidéo`, par exemple
- URL du serveur MCP distant : **`https://<votre-domaine>/mcp/<MCP_AUTH_TOKEN>`**
- Laissez vides les champs OAuth (Client ID / Secret).

Le token se trouve dans le chemin parce que claude.ai ne permet pas d'ajouter un header.
Pour d'autres clients MCP, le serveur accepte aussi `POST /mcp` avec `Authorization: Bearer <token>`.

Ensuite, dans une conversation : *« Résume cette vidéo : https://youtu.be/… »*,
*« Que dit-il dans ce post ? https://x.com/… »*, *« Transcris le dernier épisode de ce podcast : <flux RSS> »*.

## Reverse proxy

- Transmettez tout le chemin `/mcp/…` au port local.
- Les réponses sont envoyées en **SSE**. Désactivez le buffering et autorisez des réponses longues :
  - 30 min d'audio prennent environ 30 à 90 s ;
  - un podcast de 2 h prend quelques minutes.

  Le serveur envoie un ping toutes les 15 s pour garder la connexion ouverte.
  - nginx : `proxy_buffering off; proxy_read_timeout 600s; proxy_http_version 1.1;`
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
| `MAX_VIDEO_MINUTES` | `30` | Durée max d'un média. Au-delà, refus avant téléchargement. **Montez-la pour les podcasts** (ex. `120`) |
| `RATE_LIMIT_PER_HOUR` | `30` | Transcriptions par heure glissante (les hits de cache ne comptent pas) |
| `COOKIES_PATH` | vide | ex. `/cookies/cookies.txt` (voir plus bas) |
| `ALLOWED_SITES` | vide (= tous) | Restreint aux sites listés : noms d'extracteurs yt-dlp, ex. `youtube,twitter,instagram,tiktok,linkedin,apple:podcasts` |
| `ALLOW_GENERIC` | `true` | Liens directs (mp3/mp4), flux RSS, pages web avec vidéo intégrée. `false` = uniquement les sites que yt-dlp connaît explicitement |
| `TRANSCRIPTION_PROVIDER_ORDER` | `primeintellect` | Fournisseurs OpenRouter préférés, séparés par des virgules (repli automatique sur les autres). `none` = choix d'OpenRouter |
| `TRANSCRIPTION_PROVIDER_IGNORE` | vide | Fournisseurs à exclure, ex. `deepinfra` |

Variables avancées (voir `.env.example`) : `BIND_ADDRESS`, `ALLOWED_HOSTS`, `MAX_UPLOAD_MB` (24),
`CHUNK_MAX_SECONDS` (300), `AUDIO_BITRATE` (32k), `LOG_LEVEL`, `PUID`/`PGID`, `YTDLP_AUTO_UPDATE` (1).

## Cookies (contenus qui exigent une connexion, ou site qui bloque)

Instagram, LinkedIn, Facebook, certains posts X, les vidéos YouTube avec restriction d'âge, etc.
demandent souvent d'être connecté. YouTube peut aussi afficher « confirm you're not a bot ».
Pour ces cas :

1. Avec un navigateur connecté aux sites voulus, exportez les cookies au format **Netscape**
   (extension « Get cookies.txt LOCALLY » ou équivalent). Un seul fichier peut contenir
   les cookies de plusieurs sites : concaténez les exports.
2. Enregistrez-le dans `./cookies/cookies.txt`, puis mettez `COOKIES_PATH=/cookies/cookies.txt` dans `.env`.
3. `docker compose up -d`.

yt-dlp n'envoie à chaque site que ses propres cookies. Le fichier est monté en lecture seule et
copié dans un dossier temporaire à chaque requête, car yt-dlp le réécrit. Il doit être lisible par
l'UID du conteneur : 1000 par défaut, modifiable avec `PUID`/`PGID`.
Utilisez de préférence des comptes secondaires. Pour YouTube, le
[wiki yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) recommande
d'exporter depuis une fenêtre de navigation privée, pour que les cookies ne soient pas renouvelés.

## Sécurité

- **Auth** : token obligatoire, dans le chemin ou en header `Bearer`, comparé en temps constant.
- **URLs** : seules les URLs `http(s)` sur les ports 80/443, sans identifiants, sont acceptées.
  L'hôte doit résoudre vers des **adresses IP publiques** : réseau local, localhost, métadonnées cloud
  et IPv6 locales sont refusés. Cela protège ton réseau maison si une page malveillante poussait Claude
  (injection de prompt) à appeler l'outil sur `http://192.168.1.1/…`. Les éléments de playlist ou de flux
  sont contrôlés de la même façon. Limite connue : les redirections et requêtes internes faites par yt-dlp
  lui-même ne sont pas filtrées. `ALLOW_GENERIC=false` réduit encore la surface, au prix des liens directs
  et des flux RSS.
- **Fichiers** : ffmpeg ne lit que des fichiers locaux (`-protocol_whitelist file`). Les fichiers de type
  playlist (HLS, concat…) sont refusés avant conversion.
- **Coûts** : durée maximale par média, rate limit par heure, et cache pour éviter les retranscriptions.

## Tester

Le script appelle l'outil MCP sur l'URL passée en argument :

```bash
# Dans le conteneur, à travers le vrai serveur HTTP (teste aussi le transport et l'auth) :
docker compose exec -u app x-transcript python scripts/test_transcript.py \
    "https://www.youtube.com/watch?v=xxxxxxxxxxx" --server http://localhost:8000

# En local, sans Docker (Python 3.12 + ffmpeg installés), dans le même processus :
pip install -r requirements.txt
python scripts/test_transcript.py "https://x.com/compte/status/123" --language fr
```

Options : `--video-index N`, `--language xx`, `--bearer` (token en header plutôt que dans le chemin).
Tests unitaires : `pip install pytest && pytest tests`.

## Comportement

- **Plusieurs médias derrière une URL** : `video_index` choisit l'élément.
  - Post X à plusieurs vidéos, carrousel Instagram : 0 = le premier.
  - Playlist YouTube : 0 = la première vidéo.
  - Flux RSS : 0 = l'épisode le plus récent.
  - Seuls les 50 premiers éléments sont accessibles.
  - Sur X, `…/status/<id>/video/2` sélectionne directement la 2e vidéo.
  - Un lien `watch?v=…&list=…` donne la vidéo, pas la playlist.
- **Directs** : les directs en cours et les premières pas encore commencées sont refusés.
- **Cache** : clé `plateforme:id` du média et langue demandée.
  - L'ID est calculé hors ligne quand c'est possible. `youtu.be/X`, `youtube.com/watch?v=X&t=30` et
    `/shorts/X` donnent donc le même cache, servi sans aucune requête réseau.
  - Pour un flux RSS, la clé est le flux plus l'identifiant de l'épisode.
  - Une entrée en détection automatique est réutilisée si la langue détectée correspond à celle demandée.
  - La base se trouve dans `./data/cache.sqlite3`.
- **Taille et découpage** : l'API accepte 25 Mo par fichier, et OpenRouter coupe les requêtes vers les
  fournisseurs au bout d'environ 60 s. En test, un morceau de 8 min a pris environ 50 s. L'audio est donc
  découpé en morceaux de 5 min, transcrits 4 par 4 en parallèle, puis les segments sont recollés avec
  décalage des timestamps.
- **Timestamps** : le serveur demande `response_format=verbose_json` avec des timestamps par segment.
  Vérifié avec `whisper-large-v3-turbo`. Si un fournisseur refuse `verbose_json` (HTTP 400), le serveur
  repasse automatiquement en texte brut, horodaté par morceau.
- **Choix du fournisseur** : `whisper-large-v3-turbo` est servi par DeepInfra, PrimeIntellect et Groq.
  OpenRouter choisit le moins cher (DeepInfra) si on ne précise rien. En test, DeepInfra **perdait des
  phrases à chaque frontière de 30 s**, de façon reproductible. Le serveur demande donc PrimeIntellect
  en priorité, avec repli automatique sur les autres. Le prix est d'environ 0,04 $/h d'audio au lieu de
  0,012 $/h.
- **Coût** : chaque transcription est loggée avec la durée facturée (`usage.seconds`), le coût estimé
  (secondes × prix du fournisseur préféré) et le coût réel (`usage.cost`).
- **Rate limit** : en mémoire, remis à zéro au redémarrage. Seules les vraies transcriptions consomment
  du quota. Les hits de cache et les échecs avant l'appel OpenRouter (pas de vidéo, trop long, site qui
  bloque…) n'en consomment pas.
- **Erreurs** renvoyées à Claude en clair :
  - URL refusée ou site non pris en charge ;
  - pas de vidéo ni d'audio sur la page ;
  - contenu introuvable, privé, géo-bloqué ou protégé par DRM (Spotify, Netflix…) ;
  - connexion requise ou site qui bloque le serveur, avec la suggestion d'ajouter des cookies ;
  - direct en cours, média trop long, rate limit ;
  - échec OpenRouter (clé invalide, crédits épuisés, fournisseur indisponible…).

Exemple de log :

```
video_transcript: ArchiveOrg:MLKDream lang=auto -> OK | durée 16:28 | 2 morceau(x) | audio facturé 987s |
  coût ~$0.01097 estimé / $0.01097 facturé | timestamps=True | métadonnées+dl 3.4s + transcription 51.9s
```

## Structure

```
app/
  server.py       MCPServer + outil get_video_transcript + app ASGI (uvicorn)
  auth.py         auth par token (chemin ou Bearer), comparaison en temps constant, logs sans token
  urls.py         validation des URLs : IP publiques, extracteurs autorisés, clé de cache hors ligne
  service.py      pipeline, cache à deux niveaux, rate limit, concurrence
  downloader.py   yt-dlp (métadonnées, playlists/flux, audio) + ffmpeg (conversion, découpage)
  transcriber.py  appels OpenRouter (SDK OpenAI), routage fournisseur, repli json, fusion, coût
  formatting.py   texte structuré renvoyé à Claude
  cache.py / ratelimit.py / config.py / errors.py
scripts/test_transcript.py
```
