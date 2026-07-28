# syntax=docker/dockerfile:1.7
FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1 AS base

ARG SMARTSEARCH_VERSION=0.1.14-beta.8
ARG SMARTSEARCH_COMMIT=667c465d0f6ea16a423f03c434f94e21505d3595
ARG FASTMCP_VERSION=3.4.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /app

COPY --chmod=0444 requirements.lock /app/requirements.lock
RUN python -m pip install --require-hashes --no-deps -r /app/requirements.lock

COPY --chmod=0555 src/server.py /app/server.py
RUN chmod 0444 /app/requirements.lock \
    && chmod 0555 /app/server.py

LABEL org.opencontainers.image.title="SmartSearch Remote MCP" \
      org.opencontainers.image.version="${SMARTSEARCH_VERSION}" \
      org.opencontainers.image.revision="${SMARTSEARCH_COMMIT}" \
      io.fastmcp.version="${FASTMCP_VERSION}"

FROM base AS test
COPY tests /app/tests
RUN python -m unittest discover -s /app/tests -v

FROM base AS runtime
USER 1000:1001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]
ENTRYPOINT ["python", "/app/server.py"]
