# uv base image is multi-arch (linux/amd64 + linux/arm64) — tag verified on
# ghcr.io/astral-sh/uv. Note: uv.lock exists locally but is untracked, so we
# cannot use --frozen; dependencies resolve from pyproject.toml at build time.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY proton_mcp/ ./proton_mcp/

ENV UV_LINK_MODE=copy
RUN uv sync --no-dev

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8010

# HTTP mode additionally requires MCP_BEARER at runtime (fail closed).
CMD ["uv", "run", "python", "-m", "proton_mcp.server"]
