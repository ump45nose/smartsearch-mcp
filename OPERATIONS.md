# Operations

This runbook describes the deployed homelab layout. Adapt host paths, domain
names, certificate IDs, and provider endpoints before using it elsewhere.

## Boundaries

- Public MCP resource:
  `https://smartsearch-mcp-home.172906573.xyz:28443/mcp`
- Upstream identity provider:
  `https://authelia-home.172906573.xyz`
- Container backend: `http://smartsearch-mcp:8000`
- Persistent state: `/vol2/1000/Docker/smartsearch-mcp/data`
- Stack: `/vol2/1000/Docker/stacks/smartsearch-mcp`

The remote stack is independent of Hermes' existing stdio adapter. The root
Hermes `.env` is passed only as a Compose interpolation source, while
`compose.yaml` explicitly allowlists every variable that reaches the container.
Never load a Profile `.env`.

## Prepare OAuth state

`scripts/prepare_secrets.py` creates root-owned secrets, an Authelia OIDC
candidate, and a timestamped rollback backup. It prints paths and key names,
not secret values.

The Authelia client uses authorization code, PKCE, refresh tokens, and
`openid profile offline_access`; the policy requires 2FA. Validate the
candidate before promotion:

```bash
docker compose -f compose.authelia-oidc.yaml config --quiet
docker compose -f compose.authelia-oidc.yaml run --rm authelia \
  authelia config validate --config /config/configuration.candidate.yml
```

Persist `X_AUTHELIA_CONFIG_FILTERS=template` in the real Authelia service,
atomically promote the validated candidate, and recreate only Authelia. Verify
the original portal, 2FA, discovery, JWKS, authorization, and registration
metadata before exposing the MCP backend.

## Deploy

```bash
docker compose --env-file /home/hermes/.hermes/.env build
docker compose --env-file /home/hermes/.hermes/.env up -d
docker compose --env-file /home/hermes/.hermes/.env ps
```

Check that the container is healthy, publishes no host port, joins only
`gateway_net`, and retains the Compose hardening settings.

From Nginx Proxy Manager, verify
`http://smartsearch-mcp:8000/healthz`. The create/delete scripts use NPM's own
models and schema validation, fail closed on drift, and affect only the exact
SmartSearch host. Copy a script temporarily into the NPM container, execute it
with Node, run `nginx -t`, reload, and remove the temporary copy.

The generated proxy configuration:

- forces HTTPS and enables HTTP/2
- disables proxy buffering and request buffering
- gives MCP/SSE requests a 900-second proxy timeout
- routes `/codex-oauth-callback/*` only to the temporary Codex listener at
  `192.168.31.201:5555`
- routes `/hermes-oauth-callback/*` only to a temporary bridge at
  `192.168.31.201:5556`, which forwards to Hermes' loopback-only listener
- routes all other paths to `smartsearch-mcp:8000`

## DNS, public port, and LAN split

The exact SmartSearch hostname uses DNS-only Cloudflare A and AAAA records.
DDNS-Go lists the exact hostname in both address families without
`?proxied=true`. Cloudflare's reverse proxy does not accept arbitrary port
28443, so enabling orange-cloud proxying breaks this public endpoint.

The gateway publishes NPM container port 443 on host port 28443. External
clients therefore use:

```text
https://smartsearch-mcp-home.172906573.xyz:28443/mcp
```

LAN clients use NPM's local 443 mapping with:

```text
192.168.31.201 smartsearch-mcp-home.172906573.xyz
```

The OAuth proxy accepts the corresponding no-port `/mcp` resource as the
single LAN alias and normalizes it to the public `:28443/mcp` token audience.
Do not broaden the resource alias set.

After editing `/etc/hosts`, restart ShellCrash and verify the line exists in
both the source hosts file and its effective `/tmp/ShellCrash/config.yaml`.
Authelia's NPM host rewrites its browser redirects and advertised OIDC
endpoints to public port 28443 while preserving the original issuer. Apply the
guarded configuration with `scripts/configure-authelia-public-port.mjs`; pass
`--remove` only to roll back that exact managed block.

Test LAN and public access independently; a LAN result is not proof of public
DNS, IPv4/IPv6, TCP 28443, or TLS.

## OAuth and protocol acceptance

Verify all of the following before calling OAuth complete:

- RFC 9728 protected-resource metadata
- OAuth authorization-server metadata
- unauthenticated MCP request returns 401 with `WWW-Authenticate`
- S256 PKCE, refresh, consent, and 2FA
- wrong audience and unauthorized user rejection
- invalid redirect URI rejection
- callback allowlist for ChatGPT, loopback clients, the scoped Codex ingress,
  and reserved Claude callbacks

The successful Streamable HTTP initialize response uses SSE. List exactly five
tools and run a real search, fetch, and quick research. Research should return
an opaque `artifact_id`, never a host path.

Internal operational probes:

```bash
docker exec smartsearch-mcp python /app/scripts/doctor_probe.py
docker exec smartsearch-mcp python /app/scripts/smoke_probe.py
```

Mount or copy those probe scripts temporarily if they are not present in the
runtime image. Probe output excludes queries, response bodies, and credentials.

## Codex client

```toml
[mcp_servers.smartsearch-remote]
url = "https://smartsearch-mcp-home.172906573.xyz/mcp"
startup_timeout_sec = 30.0
tool_timeout_sec = 900.0
default_tools_approval_mode = "auto"
```

For a headless host whose browser runs elsewhere:

```bash
codex mcp login \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote
```

Codex appends a per-login callback suffix. The NPM route forwards only that
scoped path while the temporary listener is running.

## Hermes client

Hermes stores MCP OAuth state per Profile. Preserve any existing stdio
SmartSearch entry and add a second remote entry:

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

Hermes binds its callback server to `127.0.0.1`. During login, run a temporary
host bridge on `192.168.31.201:5556` forwarding to `127.0.0.1:5556`, then run
`hermes --profile lingjun mcp login smartsearch-remote`. Stop the bridge after
the token is stored. Do not persist it as a general-purpose listener.

```bash
python3 scripts/hermes_oauth_loopback_bridge.py
```

## ChatGPT and Claude

Add the public `/mcp` resource as a custom connector in ChatGPT Developer Mode,
complete OAuth, confirm exactly five read-only tools, then invoke search and
research. Server health is not evidence of ChatGPT-side visibility or product
eligibility.

Claude-compatible redirect URIs are reserved by the server, but Claude.ai and
Claude Code remain a separate client acceptance phase.

## Data lifecycle and logs

Research evidence uses one UUID directory per operation and is automatically
removed after 30 days. Audit logs contain only tool name, anonymized subject,
duration, status, provider count, and artifact ID. They must not contain
queries, page bodies, OAuth tokens, or provider keys.

## Rollback

1. Execute `scripts/delete-smartsearch-proxy.mjs` against NPM to remove only the
   exact managed proxy host.
2. Stop the Compose project without `-v`; keep the data directory.
3. Restore the most recent verified pre-activation Authelia configuration,
   validate it offline, and recreate only Authelia.
4. Remove the exact DDNS-Go hostname and the single `/etc/hosts` line, then
   restart and verify DDNS-Go/ShellCrash as applicable.
5. Log out/remove the Codex MCP registration.
6. For full retirement only, remove the `SMARTSEARCH_REMOTE_*` root-env entries
   and OIDC secret files.
7. Update the authoritative infrastructure fact to `rolled_back`.

Hermes' original stdio SmartSearch MCP is not modified and does not need
rollback.
