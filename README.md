# SmartSearch Remote OAuth MCP

A hardened Streamable HTTP MCP wrapper around
[SmartSearch](https://github.com/konbakuyomu/smartsearch), protected by OAuth
2.1 through FastMCP `OIDCProxy` and Authelia.

The deployment keeps an existing Hermes stdio MCP untouched. It exposes a
separate remote endpoint for ChatGPT, Codex, and compatible Claude clients.

Public MCP resource:

```text
https://smartsearch-mcp-home.172906573.xyz:28443/mcp
```

The explicit port is required because the public ingress maps TCP `28443` to
Nginx Proxy Manager's HTTPS port.

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

## Local Hermes and Codex

Clients running on the NAS use the LAN-split HTTPS endpoint on port 443:

```text
https://smartsearch-mcp-home.172906573.xyz/mcp
```

The OAuth proxy accepts this NAS-local resource as an explicit alias and
normalizes it to the public `:28443/mcp` audience. Other resource indicators
are rejected.

Codex local setup:

```bash
codex mcp add smartsearch-remote \
  --url https://smartsearch-mcp-home.172906573.xyz/mcp

codex mcp login \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote
```

Hermes local setup preserves the existing stdio `smart-search` entry and adds
the remote server to the selected Profile:

```yaml
mcp_servers:
  smartsearch-remote:
    url: "https://smartsearch-mcp-home.172906573.xyz/mcp"
    auth: oauth
    connect_timeout: 315
    timeout: 900
    enabled: true
    oauth:
      redirect_port: 5556
      redirect_uri: "https://smartsearch-mcp-home.172906573.xyz:28443/hermes-oauth-callback/lingjun"
```

## External Codex

External clients use the explicit public port. Run both commands on the
external Codex machine. Do not reuse the NAS callback override: the default
OAuth callback is a loopback listener on that external machine, so the browser
can return the authorization code directly to the Codex process that started
the login.

```bash
codex mcp add smartsearch-remote \
  --url https://smartsearch-mcp-home.172906573.xyz:28443/mcp

codex mcp login smartsearch-remote
```
