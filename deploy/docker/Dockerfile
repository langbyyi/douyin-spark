# Project service with its own scheduler; one-shot CLI and MCP remain available.
FROM python:3.12-slim-bookworm

# chrome = Google Chrome stable (amd64 only); use "chromium" on arm64 and set
# browser.channel = "chromium" in config.toml.
ARG PLAYWRIGHT_BROWSER=chrome
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    SPARK_ENV=cloud \
    SPARK_DATA_DIR=/data \
    SPARK_CONFIG=/config/config.toml \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb xauth x11vnc novnc websockify tini ca-certificates tzdata \
      fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Install the browser (and its OS deps) before copying application sources so
# routine code changes do not re-run `playwright install --with-deps`.
COPY pyproject.toml README.md ./
RUN if [ "${TARGETARCH}" = "arm64" ] && [ "${PLAYWRIGHT_BROWSER}" != "chromium" ]; then \
      echo "ARM64 requires --build-arg PLAYWRIGHT_BROWSER=chromium" >&2; exit 1; \
    fi \
 && pip install "playwright>=1.48,<2" \
 && playwright install --with-deps "${PLAYWRIGHT_BROWSER}" \
 && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY docs ./docs
ARG SPARK_BUILD_COMMIT=unknown
RUN python -c 'import json,sys; from pathlib import Path; Path("src/dyspark/_build.json").write_text(json.dumps({"commit": sys.argv[1]}))' "$SPARK_BUILD_COMMIT"
RUN pip install ".[gui,mcp]"

COPY deploy/docker/entrypoint.sh /usr/local/bin/spark-entrypoint
# Douyin probes this external scheme; Linux otherwise shows an xdg-open modal
# that consumes chat input while remaining invisible to page screenshots.
COPY deploy/docker/browser-policy.json /etc/chromium/policies/managed/dyspark.json
COPY deploy/docker/browser-policy.json /etc/opt/chrome/policies/managed/dyspark.json
COPY deploy/docker/browser-policy.json /etc/opt/chrome_for_testing/policies/managed/dyspark.json
RUN chmod 0755 /usr/local/bin/spark-entrypoint \
 && useradd --create-home --uid 1000 spark \
 && mkdir -p /data /config \
 && chown spark:spark /data

USER spark
VOLUME ["/data"]
# 8789: one-time login page / QR link, 6080: temporary noVNC — both only listen
# while a login runs (`spark login`, or MCP login_start)
EXPOSE 8765 8789 6080
ENTRYPOINT ["tini", "-g", "--", "spark-entrypoint"]
CMD ["gui", "--host", "0.0.0.0", "--no-open"]
