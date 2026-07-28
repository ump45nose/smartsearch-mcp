# SmartSearch Remote OAuth MCP

A hardened Streamable HTTP MCP wrapper around
[SmartSearch](https://github.com/konbakuyomu/smartsearch), protected by OAuth
2.1 through FastMCP `OIDCProxy` and Authelia.

The deployment keeps an existing Hermes stdio MCP untouched. It exposes a
separate remote endpoint for ChatGPT, Codex, and compatible Claude clients.

## Public tools

- `smart_search`
- `smart_fetch`
- `smart_map`
- `smart_route`
- `smart_research`

`smart_doctor` is deliberately kept as an internal operational probe.

The wrapper adds:

- OAuth authorization-code flow, PKCE, refresh tokens, DCR, consent, and
  audience-bound access tokens
- a single-user `preferred_username` authorization policy
- SSRF checks for fetch/map entry URLs
- per-subject rate limits, global concurrency 2, research concurrency 1
- bounded parameters, redaction, output truncation, and 15-minute request limits
- UUID research artifacts with 30-day evidence retention
- privacy-minimized audit logs

## Pinned versions

- Python `3.11.13`
- FastMCP `3.4.4`
- SmartSearch `v0.1.14-beta.8`,
  commit `667c465d0f6ea16a423f03c434f94e21505d3595`

The stable SmartSearch `v0.1.14` commit does not provide the route/research
service APIs required for all five tools. The beta pin is intentional and
reproducible.

## Build and test

All Python dependencies, including transitive dependencies, are hash-locked in
`requirements.lock`.

```bash
docker build --target test -t local/smartsearch-mcp:test .
docker compose --env-file /home/hermes/.hermes/.env config --quiet
docker compose --env-file /home/hermes/.hermes/.env build
```

The Compose service has no published host port and joins only the external
`gateway_net`. It runs as `1000:1001` with a read-only root filesystem, all
capabilities dropped, and `no-new-privileges`.

This repository intentionally contains no OAuth, provider, DNS, or TLS
credentials. The deployment reads only the variables explicitly listed in
`compose.yaml`; do not source a Hermes Profile `.env`.

See [OPERATIONS.md](OPERATIONS.md) for activation, ingress, client setup,
verification, and rollback.
