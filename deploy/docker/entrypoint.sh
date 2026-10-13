#!/bin/sh
# spark-entrypoint: starts the project service or a manual CLI command.
#   spark-entrypoint run|login|check ...   one-shot command under Xvfb, then exit
#   spark-entrypoint status|schedule|password|gui|--help   no browser needed
#   spark-entrypoint mcp                   MCP server on stdin/stdout (`run -T`); its
#                                          background jobs start their own Xvfb
# Scheduling belongs to `spark gui`; stopping that service stops automatic work.
set -u
umask 077

data_dir="${SPARK_DATA_DIR:-/data}"
if ! mkdir -p "$data_dir" 2>/dev/null || [ ! -w "$data_dir" ]; then
    echo "数据目录 $data_dir 不可写（容器内用户 uid $(id -u)）。" >&2
    echo "请在宿主机执行：sudo chown -R $(id -u):$(id -g) ./data" >&2
    exit 73
fi

if [ "$#" -eq 0 ]; then
    set -- gui --host 0.0.0.0 --no-open
fi

case "$1" in
    cron)
        echo "cron entry retired: start the project service with docker compose up -d spark." >&2
        exit 64
        ;;
    status|schedule|doctor|password|gui|mcp|-h|--help|--version) exec spark "$@" ;;
esac

exec xvfb-run --auto-servernum --server-args="-screen 0 1366x900x24 -nolisten tcp" spark "$@"
