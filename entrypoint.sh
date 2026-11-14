#!/bin/sh
# Démarrage du conteneur : mise à jour de yt-dlp, droits sur /data, puis lancement en utilisateur non-root.
set -e

# Optionnel : aligner l'UID/GID de l'utilisateur "app" sur ceux de l'hôte (droits sur ./data et ./cookies)
if [ "$(id -u)" = "0" ]; then
    if [ -n "${PGID:-}" ] && [ "$PGID" != "$(id -g app)" ]; then groupmod -o -g "$PGID" app; fi
    if [ -n "${PUID:-}" ] && [ "$PUID" != "$(id -u app)" ]; then usermod -o -u "$PUID" app; fi
fi

# Les sites (X, YouTube, Instagram…) changent souvent : on garde yt-dlp à jour à chaque démarrage (désactivable).
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
    echo "Mise à jour de yt-dlp et gallery-dl..."
    if ! timeout 180 pip install --no-cache-dir --upgrade --quiet --disable-pip-version-check "yt-dlp[default,deno]" gallery-dl; then
        echo "Avertissement : mise à jour impossible, les versions installées sont conservées."
    fi
fi
echo "yt-dlp $(yt-dlp --version) | gallery-dl $(gallery-dl --version) | deno $(deno --version 2>/dev/null | head -n1 | cut -d" " -f2) | $(ffmpeg -version | head -n1)"

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/tmp"

if [ "$(id -u)" = "0" ]; then
    chown -R app:app "$DATA_DIR"
    exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi
exec "$@"
