#!/bin/sh
set -eu
umask 077

case "${1:-}" in
    -h|--help)
        printf '%s\n' 'Usage: bash deploy/docker/start.sh' \
            'Linux Docker quick start. Panel: http://127.0.0.1:8765'
        exit 0
        ;;
    "") ;;
    *) printf '%s\n' 'Unsupported argument. Use --help.' >&2; exit 64 ;;
esac

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
command -v docker >/dev/null 2>&1 || { printf '%s\n' 'Docker is required.' >&2; exit 69; }
docker compose version >/dev/null

for path in config data .env; do
    if [ -L "$path" ]; then
        printf 'Symbolic links are not supported for %s.\n' "$path" >&2
        exit 64
    fi
done
mkdir -p config data
if [ ! -e .env ]; then
    (set -C; cat .env.example > .env)
fi
chmod 600 .env
docker compose config --quiet
running=$(docker compose ps --status running --services)
case "
$running
" in
    *"
spark
"*) printf '%s\n' 'Stop the HTTPS service before starting local: docker compose stop spark' >&2; exit 75 ;;
esac

docker compose build local
docker compose run --rm --no-deps --user 0:0 --entrypoint /bin/sh local -eu -c '
    umask 077
    if [ -L /config/config.toml ]; then
        echo "Symbolic links are not supported for config.toml." >&2
        exit 64
    fi
    if [ ! -e /config/config.toml ]; then
        (set -C; cat /defaults/config.toml > /config/config.toml)
    fi
    chown 1000:1000 /data /config /config/config.toml
    chmod 700 /data /config
    chmod 600 /config/config.toml
'
docker compose up -d --wait --wait-timeout 60 local
printf '%s\n' 'Panel: http://127.0.0.1:8765' \
    'Initial password: docker compose logs local' \
    'Stop: docker compose stop local'
