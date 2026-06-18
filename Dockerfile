# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
LABEL org.opencontainers.image.title="mcp-server-polarion" \
      org.opencontainers.image.description="MCP server for Polarion ALM — read and write documents and work items" \
      org.opencontainers.image.source="https://github.com/devemberx/mcp-server-polarion" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -r -u 1000 mcp
USER mcp
ENTRYPOINT ["mcp-server-polarion"]
