from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import JWTIssuer
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from starlette.testclient import TestClient


SPEC = importlib.util.spec_from_file_location("smartsearch_remote_server", "/app/server.py")
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def token(username: str = "yuwk", subject: str = "subject-1") -> AccessToken:
    return AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["openid", "profile", "offline_access"],
        expires_at=int(time.time()) + 3600,
        claims={"sub": subject, "preferred_username": username},
    )


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_five_tools_and_no_doctor(self) -> None:
        mcp = server.build_mcp()
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "smart_search",
                "smart_fetch",
                "smart_map",
                "smart_route",
                "smart_research",
            },
        )
        for tool in tools.values():
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertTrue(tool.annotations.openWorldHint)

    async def test_schema_has_real_boundaries(self) -> None:
        tools = {
            tool.name: tool
            for tool in await server.build_mcp().list_tools()
        }
        search = tools["smart_search"].parameters
        self.assertEqual(search["properties"]["extra_sources"]["minimum"], 0)
        self.assertEqual(search["properties"]["extra_sources"]["maximum"], 10)
        self.assertEqual(search["properties"]["timeout_seconds"]["maximum"], 300)
        self.assertEqual(
            set(search["properties"]["validation"]["enum"]),
            {"fast", "balanced", "strict"},
        )
        self.assertFalse(search["additionalProperties"])

    async def test_pinned_upstream_has_all_five_backing_apis(self) -> None:
        self.assertTrue(hasattr(server.service, "route"))
        self.assertTrue(hasattr(server.service, "research"))
        search_parameters = inspect.signature(server.service.search).parameters
        self.assertIn("timeout_seconds", search_parameters)


class AuthorizationTests(unittest.TestCase):
    def test_authorized_username_direct_and_nested(self) -> None:
        direct = type("Ctx", (), {"token": token()})()
        self.assertTrue(server._only_authorized_user(direct))
        nested_token = token(username="wrong")
        nested_token.claims = {
            "sub": "subject-1",
            "upstream_claims": {"preferred_username": "yuwk"},
        }
        nested = type("Ctx", (), {"token": nested_token})()
        self.assertTrue(server._only_authorized_user(nested))
        denied = type("Ctx", (), {"token": token(username="other")})()
        self.assertFalse(server._only_authorized_user(denied))

    def test_redirect_allowlist(self) -> None:
        allowed = server.ALLOWED_CLIENT_REDIRECT_URIS
        self.assertTrue(
            validate_redirect_uri(
                "https://chatgpt.com/connector/oauth/callback-123",
                allowed,
            )
        )
        self.assertTrue(
            validate_redirect_uri(
                "http://127.0.0.1:54321/callback",
                allowed,
            )
        )
        self.assertTrue(
            validate_redirect_uri(
                (
                    "https://smartsearch-mcp-home.172906573.xyz:28443/"
                    "codex-oauth-callback/test-id"
                ),
                allowed,
            )
        )
        self.assertTrue(
            validate_redirect_uri(
                (
                    "https://smartsearch-mcp-home.172906573.xyz:28443/"
                    "hermes-oauth-callback/lingjun"
                ),
                allowed,
            )
        )
        self.assertTrue(
            validate_redirect_uri(
                "https://claude.ai/api/mcp/auth_callback",
                allowed,
            )
        )
        self.assertFalse(
            validate_redirect_uri(
                "https://chatgpt.com.evil.example/connector/oauth/callback",
                allowed,
            )
        )
        self.assertFalse(
            validate_redirect_uri(
                "http://localhost@evil.example/callback",
                allowed,
            )
        )


class OAuthProtocolTests(unittest.TestCase):
    def test_metadata_challenge_pkce_refresh_dcr_and_audience(self) -> None:
        class DiscoveryResponse:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "issuer": "https://idp.example",
                    "authorization_endpoint": "https://idp.example/authorize",
                    "token_endpoint": "https://idp.example/token",
                    "jwks_uri": "https://idp.example/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "scopes_supported": ["openid", "profile", "offline_access"],
                    "grant_types_supported": [
                        "authorization_code",
                        "refresh_token",
                    ],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic"
                    ],
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {
                "SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET": "a" * 48,
                "SMARTSEARCH_REMOTE_JWT_SIGNING_KEY": "b" * 48,
                "SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY": "c" * 48,
            }
            with (
                patch.dict(os.environ, env),
                patch.object(server, "DATA_DIR", root),
                patch.object(server, "OAUTH_DIR", root / "oauth"),
                patch.object(server, "EVIDENCE_DIR", root / "evidence"),
                patch(
                    "fastmcp.server.auth.oidc_proxy.httpx.get",
                    return_value=DiscoveryResponse(),
                ),
            ):
                auth, oauth_store = server.build_oidc_provider()
                mcp = server.build_mcp(auth=auth, oauth_store=oauth_store)
                app = mcp.http_app(
                    path="/mcp",
                    transport="streamable-http",
                    stateless_http=True,
                    json_response=False,
                    allowed_hosts=["smartsearch-mcp-home.172906573.xyz"],
                    allowed_origins=["https://chatgpt.com"],
                )
                app.routes.insert(
                    0,
                    server.Route("/healthz", server.healthz, methods=["GET"]),
                )

                with TestClient(
                    app,
                    base_url=(
                        "https://smartsearch-mcp-home.172906573.xyz:28443"
                    ),
                ) as client:
                    initialize = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    }
                    challenge = client.post(
                        "/mcp",
                        json=initialize,
                        headers={"accept": "application/json, text/event-stream"},
                    )
                    self.assertEqual(challenge.status_code, 401)
                    self.assertIn(
                        "/.well-known/oauth-protected-resource/mcp",
                        challenge.headers["www-authenticate"],
                    )
                    health = client.get("/healthz")
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()["status"], "ok")

                    resource = client.get(
                        "/.well-known/oauth-protected-resource/mcp"
                    )
                    self.assertEqual(resource.status_code, 200)
                    self.assertEqual(
                        resource.json()["resource"],
                        (
                            "https://smartsearch-mcp-home.172906573.xyz:"
                            "28443/mcp"
                        ),
                    )

                    metadata = client.get(
                        "/.well-known/oauth-authorization-server"
                    )
                    self.assertEqual(metadata.status_code, 200)
                    body = metadata.json()
                    self.assertEqual(body["code_challenge_methods_supported"], ["S256"])
                    self.assertIn("refresh_token", body["grant_types_supported"])
                    self.assertTrue(body["registration_endpoint"].endswith("/register"))

                    rejected = client.post(
                        "/register",
                        json={
                            "client_name": "test",
                            "redirect_uris": ["http://evil.example/callback"],
                            "grant_types": [
                                "authorization_code",
                                "refresh_token",
                            ],
                            "response_types": ["code"],
                            "token_endpoint_auth_method": "none",
                        },
                    )
                    self.assertEqual(rejected.status_code, 400)
                    self.assertEqual(
                        rejected.json()["error"],
                        "invalid_redirect_uri",
                    )

                    accepted = client.post(
                        "/register",
                        json={
                            "client_name": "test",
                            "redirect_uris": [
                                "http://127.0.0.1:54321/callback"
                            ],
                            "grant_types": [
                                "authorization_code",
                                "refresh_token",
                            ],
                            "response_types": ["code"],
                            "token_endpoint_auth_method": "none",
                        },
                    )
                    self.assertEqual(accepted.status_code, 201)
                    missing_pkce = client.get(
                        "/authorize",
                        params={
                            "client_id": accepted.json()["client_id"],
                            "redirect_uri": "http://127.0.0.1:54321/callback",
                            "response_type": "code",
                            "scope": "openid profile offline_access",
                            "state": "test-state",
                        },
                        follow_redirects=False,
                    )
                    self.assertEqual(missing_pkce.status_code, 302)
                    pkce_error = parse_qs(
                        urlsplit(missing_pkce.headers["location"]).query
                    )
                    self.assertEqual(pkce_error["error"], ["invalid_request"])

                    wrong_issuer = JWTIssuer(
                        issuer=auth.jwt_issuer.issuer,
                        audience="https://wrong.example/mcp",
                        signing_key=auth.jwt_issuer._signing_key,
                    )
                    wrong_token = wrong_issuer.issue_access_token(
                        client_id=accepted.json()["client_id"],
                        scopes=["openid", "profile", "offline_access"],
                        jti="wrong-audience-test",
                        upstream_claims={"preferred_username": "yuwk"},
                    )
                    with self.assertRaises(Exception):
                        auth.jwt_issuer.verify_token(wrong_token)
                    wrong_audience = client.post(
                        "/mcp",
                        json=initialize,
                        headers={
                            "accept": "application/json, text/event-stream",
                            "authorization": f"Bearer {wrong_token}",
                        },
                    )
                    self.assertEqual(wrong_audience.status_code, 401)

                stored_bytes = b"".join(
                    item.read_bytes()
                    for item in (root / "oauth").rglob("*")
                    if item.is_file() and item.stat().st_size < 2 * 1024 * 1024
                )
                self.assertTrue(stored_bytes)
                self.assertNotIn(b"127.0.0.1:54321", stored_bytes)


class TransportTests(unittest.TestCase):
    def test_successful_initialize_uses_streamable_http_sse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(server, "DATA_DIR", root),
                patch.object(server, "OAUTH_DIR", root / "oauth"),
                patch.object(server, "EVIDENCE_DIR", root / "evidence"),
            ):
                mcp = server.build_mcp()
                app = mcp.http_app(
                    path="/mcp",
                    transport="streamable-http",
                    stateless_http=True,
                    json_response=False,
                    allowed_hosts=["testserver"],
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {},
                                "clientInfo": {
                                    "name": "transport-test",
                                    "version": "1",
                                },
                            },
                        },
                        headers={
                            "accept": "application/json, text/event-stream",
                        },
                    )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith(
                    "text/event-stream"
                )
            )
            self.assertIn('"protocolVersion"', response.text)


class SSRFTests(unittest.TestCase):
    def test_rejects_non_public_and_unsafe_urls(self) -> None:
        values = [
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
            "http://user:pass@example.com/",
            "https://example.com:22/",
            "http://2130706433/",
            "http://0177.0.0.1/",
            "http://0x7f000001/",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                server._validate_public_url_sync(value)

    def test_rejects_hostname_if_any_answer_is_private(self) -> None:
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=records):
            with self.assertRaises(ValueError):
                server._validate_public_url_sync("https://example.test/")

    def test_accepts_public_default_port(self) -> None:
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=records):
            self.assertEqual(
                server._validate_public_url_sync("HTTPS://Example.Test:443/a?q=1"),
                "https://example.test:443/a?q=1",
            )


class OutputTests(unittest.TestCase):
    def test_redacts_secret_fields_and_bearer_tokens(self) -> None:
        value = server._sanitize(
            {
                "access_token": "secret-value",
                "text": "Authorization: Bearer abcdefghijklmnop",
            }
        )
        self.assertEqual(value["access_token"], "[REDACTED]")
        self.assertNotIn("abcdefghijklmnop", value["text"])

    def test_bounds_large_output(self) -> None:
        value = server._bound_output({"ok": True, "content": "x" * 200_000})
        encoded = json.dumps(value).encode()
        self.assertLess(len(encoded), server.MAX_OUTPUT_BYTES)
        self.assertTrue(value["_truncated"])

    def test_bounds_large_nested_sources_and_gaps(self) -> None:
        value = server._bound_output(
            {
                "ok": True,
                "summary": "s" * 200_000,
                "sources": [
                    {"title": "t" * 100_000, "url": "https://example.com/" + "u" * 100_000}
                    for _ in range(100)
                ],
                "gap_check": {
                    "status": "open",
                    "gaps": [{"reason": "g" * 100_000} for _ in range(100)],
                },
            }
        )
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.assertLessEqual(len(encoded), server.MAX_OUTPUT_BYTES)
        self.assertTrue(value["_truncated"])

    def test_research_compaction_never_returns_evidence_path(self) -> None:
        value = server._compact_research(
            {
                "ok": True,
                "final_answer": "summary",
                "evidence_dir": "/data/evidence/secret",
                "evidence_items": [{"content": "full page"}],
                "citations": [{"url": "https://example.com", "provider": "tavily"}],
                "gap_check": {"status": "closed", "gaps": []},
            },
            "123e4567-e89b-42d3-a456-426614174000",
        )
        serialized = json.dumps(value)
        self.assertNotIn("/data/", serialized)
        self.assertNotIn("full page", serialized)
        self.assertEqual(value["artifact_id"], "123e4567-e89b-42d3-a456-426614174000")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        server._TOOL_LIMITER.reset()
        server._RESEARCH_LIMITER.reset()

    async def test_global_concurrency_is_two(self) -> None:
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def operation() -> dict:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return {"ok": True}

        await asyncio.gather(
            *[
                server._execute_tool(
                    name="smart_search",
                    token=token(subject=f"s-{index}"),
                    operation=operation,
                    timeout_seconds=2,
                )
                for index in range(5)
            ]
        )
        self.assertEqual(peak, 2)

    async def test_research_concurrency_is_one(self) -> None:
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def operation() -> dict:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            return {"ok": True}

        await asyncio.gather(
            *[
                server._execute_tool(
                    name="smart_research",
                    token=token(subject=f"r-{index}"),
                    operation=operation,
                    timeout_seconds=2,
                    artifact_id=f"a-{index}",
                    research=True,
                )
                for index in range(3)
            ]
        )
        self.assertEqual(peak, 1)

    async def test_rate_limit_is_per_subject(self) -> None:
        limiter = server.SubjectRateLimiter(max_requests=1, window_seconds=60)
        self.assertTrue(await limiter.check("one"))
        self.assertFalse(await limiter.check("one"))
        self.assertTrue(await limiter.check("two"))

    async def test_search_wrapper_calls_real_pinned_signature(self) -> None:
        mocked = AsyncMock(return_value={"ok": True, "providers_used": ["test"]})
        with patch.object(server.service, "search", mocked):
            result = await server.smart_search_tool(
                query="current test",
                platform="web",
                extra_sources=2,
                validation="balanced",
                providers="auto",
                timeout_seconds=45,
                token=token(),
            )
        self.assertTrue(result["ok"])
        mocked.assert_awaited_once_with(
            query="current test",
            platform="web",
            extra_sources=2,
            validation="balanced",
            providers="auto",
            timeout_seconds=45,
        )

    async def test_rate_limited_research_creates_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary)
            limiter = server.SubjectRateLimiter(max_requests=0, window_seconds=60)
            with (
                patch.object(server, "EVIDENCE_DIR", evidence_root),
                patch.object(server, "_RESEARCH_LIMITER", limiter),
                patch.object(server, "_ensure_private_directory") as ensure_dir,
            ):
                result = await server.smart_research_tool(
                    query="rate limit test",
                    budget="quick",
                    fallback="auto",
                    token=token(),
                )
            self.assertEqual(result["status"], "rate_limited")
            self.assertEqual(list(evidence_root.iterdir()), [])
            ensure_dir.assert_not_called()

    async def test_research_creates_missing_evidence_root_privately(self) -> None:
        mocked = AsyncMock(
            return_value={
                "ok": True,
                "final_answer": "done",
                "citations": [],
                "gap_check": {"status": "closed", "gaps": []},
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary) / "missing"
            with (
                patch.object(server, "EVIDENCE_DIR", evidence_root),
                patch.object(server.service, "research", mocked),
            ):
                result = await server.smart_research_tool(
                    query="directory test",
                    budget="quick",
                    fallback="auto",
                    token=token(subject="directory-subject"),
                )
            self.assertTrue(result["ok"])
            self.assertEqual(evidence_root.stat().st_mode & 0o777, 0o700)
            artifact_root = evidence_root / result["artifact_id"]
            self.assertTrue(artifact_root.is_dir())
            self.assertEqual(artifact_root.stat().st_mode & 0o777, 0o700)

    async def test_research_directory_failure_is_structured(self) -> None:
        with (
            patch.object(
                server,
                "_ensure_private_directory",
                side_effect=PermissionError("denied"),
            ),
            patch.object(server.service, "research", AsyncMock()) as research,
        ):
            result = await server.smart_research_tool(
                query="directory failure test",
                budget="quick",
                fallback="auto",
                token=token(subject="directory-failure-subject"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "runtime_error")
        research.assert_not_awaited()

    async def test_rate_limit_runs_before_url_dns_validation(self) -> None:
        limiter = server.SubjectRateLimiter(max_requests=0, window_seconds=60)
        validator = AsyncMock(return_value="https://example.com/")
        with (
            patch.object(server, "_TOOL_LIMITER", limiter),
            patch.object(server, "_validate_public_url", validator),
        ):
            result = await server.smart_fetch_tool(
                url="https://example.com/",
                token=token(subject="limited-fetch"),
            )
        self.assertEqual(result["status"], "rate_limited")
        validator.assert_not_awaited()

    async def test_research_timeout_leaves_ingress_response_margin(self) -> None:
        self.assertLess(
            server.RESEARCH_OPERATION_SECONDS,
            server.MAX_RESEARCH_SECONDS,
        )
        self.assertLessEqual(server.MAX_RESEARCH_SECONDS, 14 * 60)

    async def test_research_operation_timeout_still_scrubs_artifact(self) -> None:
        async def slow_research(**_: object) -> dict:
            await asyncio.sleep(1)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary)
            scrubbed: list[Path] = []
            with (
                patch.object(server, "EVIDENCE_DIR", evidence_root),
                patch.object(server, "RESEARCH_OPERATION_SECONDS", 0.01),
                patch.object(server.service, "research", slow_research),
                patch.object(
                    server,
                    "_scrub_artifact_files",
                    lambda path: scrubbed.append(path),
                ),
            ):
                result = await server.smart_research_tool(
                    query="timeout test",
                    budget="quick",
                    fallback="auto",
                    token=token(subject="timeout-subject"),
                )
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(len(scrubbed), 1)
            self.assertTrue(scrubbed[0].is_dir())

    async def test_non_audit_info_logs_are_suppressed(self) -> None:
        self.assertGreaterEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(server.audit_log.level, logging.INFO)
        self.assertFalse(server.audit_log.propagate)


class RetentionTests(unittest.TestCase):
    def test_data_directories_are_created_with_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            with (
                patch.object(server, "DATA_DIR", data_root),
                patch.object(server, "OAUTH_DIR", data_root / "oauth"),
                patch.object(server, "EVIDENCE_DIR", data_root / "evidence"),
            ):
                server._ensure_data_dirs()

            for directory in (
                data_root,
                data_root / "oauth",
                data_root / "evidence",
                data_root / "config",
            ):
                self.assertTrue(directory.is_dir())
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_only_old_uuid_directories_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_uuid = root / "123e4567-e89b-42d3-a456-426614174000"
            fresh_uuid = root / "223e4567-e89b-42d3-a456-426614174000"
            unrelated = root / "keep-me"
            old_uuid.mkdir()
            fresh_uuid.mkdir()
            unrelated.mkdir()
            old = time.time() - 31 * 86400
            os.utime(old_uuid, (old, old))
            os.utime(unrelated, (old, old))

            with patch.object(server, "EVIDENCE_DIR", root):
                deleted = server.cleanup_old_artifacts(now=time.time())

            self.assertEqual(deleted, 1)
            self.assertFalse(old_uuid.exists())
            self.assertTrue(fresh_uuid.exists())
            self.assertTrue(unrelated.exists())

    def test_artifact_scrub_redacts_text_and_omits_unsafe_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_file = root / "evidence.md"
            text_file.write_text(
                "Authorization: Bearer abcdefghijklmnop",
                encoding="utf-8",
            )
            binary_file = root / "evidence.bin"
            binary_file.write_bytes(b"\xff\xfe\x00\x01")
            external = root / "external.txt"
            external.write_text("outside", encoding="utf-8")
            symlink = root / "evidence-link"
            symlink.symlink_to(external)

            server._scrub_artifact_files(root)

            self.assertNotIn("abcdefghijklmnop", text_file.read_text())
            self.assertIn(
                "non-text evidence omitted",
                binary_file.read_text(encoding="utf-8"),
            )
            self.assertFalse(symlink.exists())
            self.assertEqual(text_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(binary_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(external.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
