# Serveur MCP BIMData — image de production.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_DATA_DIR=/data \
    FASTMCP_HOME=/data/fastmcp

# git : les briques first-party (bim-core, bimdata-read…) sont résolues depuis
# leurs tags GitHub via [tool.uv.sources].
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install "uv>=0.5,<1"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

RUN useradd --system --uid 10001 --create-home app \
 && mkdir -p /data && chown -R app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["bimdata-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000", "--path", "/mcp"]
