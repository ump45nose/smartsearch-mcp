#!/usr/bin/env python3
"""OAuth 2.1 Streamable HTTP MCP facade for SmartSearch 0.1.14."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import SplitResult, urlsplit, urlunsplit

import anyio
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.dependencies import CurrentAccessToken
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


SERVICE_NAME = "smart-search-remote"
SERVICE_VERSION = "0.1.14-beta.8+fastmcp.3.4.4"
PUBLIC_BASE_URL = os.getenv(
    "SMARTSEARCH_REMOTE_PUBLIC_BASE_URL",
    "https://smartsearch-mcp-home.172906573.xyz:28443",
).rstrip("/")
MCP_RESOURCE_URL = f"{PUBLIC_BASE_URL}/mcp"
AUTHELIA_DISCOVERY_URL = os.getenv(
    "SMARTSEARCH_REMOTE_OIDC_DISCOVERY_URL",
    "https://authelia-home.172906573.xyz/.well-known/openid-configuration",
)
AUTHORIZED_USERNAME = os.getenv(
    "SMARTSEARCH_REMOTE_AUTHORIZED_USERNAME",
    "yuwk",
)
DATA_DIR = Path(os.getenv("SMARTSEARCH_REMOTE_DATA_DIR", "/data"))
OAUTH_DIR = DATA_DIR / "oauth"
EVIDENCE_DIR = DATA_DIR / "evidence"
RETENTION_DAYS = int(os.getenv("SMARTSEARCH_REMOTE_RETENTION_DAYS", "30"))
MAX_OUTPUT_BYTES = 96 * 1024
# Leave one minute below the 900-second ingress timeout so the application can
# return a structured timeout instead of having NPM sever the connection first.
MAX_RESEARCH_SECONDS = 14 * 60
# The operation timeout is shorter than the FastMCP tool timeout so evidence
# scrubbing and a structured timeout response get a bounded completion window.
RESEARCH_OPERATION_SECONDS = 13 * 60

ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://chatgpt.com/connector/oauth/*",
    f"{PUBLIC_BASE_URL}/codex-oauth-callback/*",
    f"{PUBLIC_BASE_URL}/hermes-oauth-callback/*",
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]

PROVIDER_ENV_KEYS = (
    "ANYSEARCH_API_KEY",
    "CONTEXT7_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "JINA_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "TAVILY_API_KEY",
    "XAI_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_MCP_API_KEY",
)
REMOTE_SECRET_ENV_KEYS = (
    "SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET",
    "SMARTSEARCH_REMOTE_JWT_SIGNING_KEY",
    "SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY",
)

_SECRET_VALUES = tuple(
    value
    for name in (*PROVIDER_ENV_KEYS, *REMOTE_SECRET_ENV_KEYS)
    for value in (os.getenv(name, ""),)
    if len(value) >= 8
)
_GENERIC_SECRET = re.compile(
    r"(?i)(?:\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|xai|ghp)-[A-Za-z0-9_.-]{8,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_.-]{8,}\b"
)
_SECRET_FIELD = re.compile(
    r"(?i)^(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|password|secret)$"
)
_EXPLICIT_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_UUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PROVIDERS = {
    "auto",
    "anysearch",
    "chat-completions",
    "context7",
    "exa",
    "firecrawl",
    "grok",
    "grok-web-tools",
    "jina",
    "openai",
    "openai-compatible",
    "primary",
    "tavily",
    "xai",
    "xai-responses",
    "zhipu",
    "zhipu-mcp",
    "zhipu-mcp-reader",
}

READ_ONLY_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
RESEARCH_READ_ONLY_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

Query = Annotated[str, Field(min_length=1, max_length=4_000)]
URL = Annotated[str, Field(min_length=8, max_length=2_048)]
Platform = Annotated[str, Field(max_length=200)]
Instructions = Annotated[str, Field(max_length=2_000)]
Providers = Annotated[str, Field(min_length=1, max_length=200)]
Validation = Literal["fast", "balanced", "strict"]
ResearchBudget = Literal["quick", "standard", "deep"]
Fallback = Literal["auto", "off"]
RouteMode = Literal["", "hybrid", "rules", "off"]

T = TypeVar("T")

logging.basicConfig(level=logging.CRITICAL, format="%(message)s")
audit_log = logging.getLogger("smartsearch_remote.audit")
audit_log.setLevel(logging.INFO)
audit_log.propagate = False
if not audit_log.handlers:
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(message)s"))
    audit_log.addHandler(_audit_handler)

# SmartSearch constructs its process-local config object at import time. The
# Compose file explicitly disables its file/debug logging before this import.
from smart_search import service  # noqa: E402


def _silence_non_audit_loggers() -> None:
    prefixes = ("fastmcp", "mcp", "httpx", "httpcore", "smart_search", "uvicorn")
    for name, candidate in logging.Logger.manager.loggerDict.items():
        if not name.startswith(prefixes) or not isinstance(candidate, logging.Logger):
            continue
        candidate.handlers.clear()
        candidate.propagate = False
        candidate.disabled = True
    for name in prefixes:
        candidate = logging.getLogger(name)
        candidate.handlers.clear()
        candidate.addHandler(logging.NullHandler())
        candidate.propagate = False
        candidate.setLevel(logging.CRITICAL)


_silence_non_audit_loggers()


def _ensure_data_dirs() -> None:
    os.umask(0o077)
    for directory in (DATA_DIR, OAUTH_DIR, EVIDENCE_DIR, DATA_DIR / "config"):
        _ensure_private_directory(directory)


def _ensure_private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise RuntimeError("private data directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("private data path is not a directory")
    directory.chmod(0o700)


def _require_secret(name: str, minimum_length: int = 32) -> str:
    value = os.getenv(name, "")
    if len(value) < minimum_length:
        raise RuntimeError(f"{name} is missing or too short")
    return value


def _claims(token: AccessToken | None) -> dict[str, Any]:
    if token is None or not isinstance(token.claims, dict):
        return {}
    claims = dict(token.claims)
    nested = claims.get("upstream_claims")
    if isinstance(nested, dict):
        for key, value in nested.items():
            claims.setdefault(key, value)
    return claims


def _only_authorized_user(ctx: Any) -> bool:
    username = _claims(getattr(ctx, "token", None)).get("preferred_username")
    return isinstance(username, str) and hmac.compare_digest(
        username,
        AUTHORIZED_USERNAME,
    )


def _subject_id(token: AccessToken | None) -> str:
    claims = _claims(token)
    subject = str(claims.get("sub") or claims.get("preferred_username") or "unknown")
    key = os.getenv("SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY", "unconfigured")
    return hmac.new(key.encode(), subject.encode(), hashlib.sha256).hexdigest()[:16]


class SubjectRateLimiter:
    """Small in-process sliding-window limiter keyed by authenticated subject."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, subject: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            entries = self._requests[subject]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self.max_requests:
                return False
            entries.append(now)
            return True

    def reset(self) -> None:
        self._requests.clear()


_GLOBAL_SEMAPHORE = asyncio.Semaphore(2)
_RESEARCH_SEMAPHORE = asyncio.Semaphore(1)
_TOOL_LIMITER = SubjectRateLimiter(max_requests=30, window_seconds=60)
_RESEARCH_LIMITER = SubjectRateLimiter(max_requests=4, window_seconds=15 * 60)


def _redact_text(value: str, limit: int = 64 * 1024) -> str:
    for secret in _SECRET_VALUES:
        value = value.replace(secret, "[REDACTED]")
    value = _GENERIC_SECRET.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "\n[TRUNCATED]"
    return value


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        return "[TRUNCATED: nesting limit]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        items = list(value.items())
        result: dict[str, Any] = {}
        for key, item in items[:150]:
            key_text = str(key)
            result[key_text] = (
                "[REDACTED]"
                if _SECRET_FIELD.fullmatch(key_text)
                else _sanitize(item, depth + 1)
            )
        if len(items) > 150:
            result["_truncated_fields"] = len(items) - 150
        return result
    if isinstance(value, (list, tuple)):
        result = [_sanitize(item, depth + 1) for item in value[:150]]
        if len(value) > 150:
            result.append({"_truncated_items": len(value) - 150})
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _bound_output(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize(value)
    had_string_truncation = "[TRUNCATED]" in json.dumps(
        sanitized,
        ensure_ascii=False,
        default=str,
    )
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    if len(encoded) <= MAX_OUTPUT_BYTES:
        if had_string_truncation:
            sanitized["_truncated"] = True
        return sanitized

    compact_sources: list[dict[str, str]] = []
    for item in (sanitized.get("sources") or [])[:10]:
        if not isinstance(item, dict):
            continue
        compact_sources.append(
            {
                key: _redact_text(str(item[key]), limit)
                for key, limit in (
                    ("title", 512),
                    ("url", 2_048),
                    ("provider", 128),
                    ("published_date", 128),
                )
                if item.get(key)
            }
        )

    compact_citations: list[dict[str, str]] = []
    for item in (sanitized.get("citations") or [])[:10]:
        if not isinstance(item, dict):
            continue
        compact_citations.append(
            {
                key: _redact_text(str(item[key]), limit)
                for key, limit in (
                    ("title", 512),
                    ("url", 2_048),
                    ("provider", 128),
                )
                if item.get(key)
            }
        )

    compact: dict[str, Any] = {
        "ok": bool(sanitized.get("ok")),
        "status": sanitized.get("status", ""),
        "error_type": sanitized.get("error_type", ""),
        "error": _redact_text(str(sanitized.get("error", "")), 2_000),
        "content": _redact_text(str(sanitized.get("content", "")), 48 * 1024),
        "summary": _redact_text(str(sanitized.get("summary", "")), 32 * 1024),
        "sources": compact_sources,
        "citations": compact_citations,
        "providers_used": [
            _redact_text(str(item), 128)
            for item in (sanitized.get("providers_used") or [])[:20]
        ],
        "fallback_used": bool(sanitized.get("fallback_used", False)),
        "elapsed_ms": sanitized.get("elapsed_ms", 0),
        **(
            {"artifact_id": sanitized["artifact_id"]}
            if sanitized.get("artifact_id")
            else {}
        ),
        **(
            {"gap_check": sanitized["gap_check"]}
            if sanitized.get("gap_check")
            else {}
        ),
        "_truncated": True,
        "_original_bytes": len(encoded),
    }
    while (
        len(
            json.dumps(
                compact,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode()
        )
        > MAX_OUTPUT_BYTES
    ):
        if len(compact["content"]) > 2_048:
            compact["content"] = _redact_text(
                compact["content"][: len(compact["content"]) // 2],
                len(compact["content"]) // 2,
            )
        elif len(compact["summary"]) > 2_048:
            compact["summary"] = _redact_text(
                compact["summary"][: len(compact["summary"]) // 2],
                len(compact["summary"]) // 2,
            )
        elif compact["sources"]:
            compact["sources"].pop()
        elif compact["citations"]:
            compact["citations"].pop()
        elif compact.get("gap_check"):
            compact["gap_check"] = {"status": "truncated", "gaps": []}
        else:
            compact["content"] = "[TRUNCATED]"
            compact["summary"] = "[TRUNCATED]"
            break
    return compact


def _normalize_providers(value: str) -> str:
    providers = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not providers:
        raise ValueError("providers must not be empty")
    invalid = sorted(set(providers) - _PROVIDERS)
    if invalid:
        raise ValueError(f"unsupported providers: {', '.join(invalid)}")
    return ",".join(dict.fromkeys(providers))


def _validate_public_url_sync(value: str) -> str:
    try:
        parsed: SplitResult = urlsplit(value)
    except ValueError as exc:
        raise ValueError("invalid URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    if not parsed.hostname:
        raise ValueError("URL hostname is required")
    if "%" in parsed.hostname:
        raise ValueError("scoped or encoded hostnames are not allowed")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise ValueError("only the scheme default port is allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("local hostnames are not allowed")

    # Reject non-canonical all-numeric IPv4 spellings before the resolver can
    # reinterpret them as octal, hexadecimal, or a single 32-bit integer.
    if re.fullmatch(r"[0-9.]+", hostname):
        try:
            canonical = str(ipaddress.ip_address(hostname))
        except ValueError as exc:
            raise ValueError("non-canonical numeric IP address is not allowed") from exc
        if canonical != hostname:
            raise ValueError("non-canonical numeric IP address is not allowed")
    if hostname.startswith("0x"):
        raise ValueError("hexadecimal IP address is not allowed")

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        direct_ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                hostname,
                expected_port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("hostname did not resolve") from exc
        for record in records:
            addresses.add(ipaddress.ip_address(record[4][0]))
    else:
        addresses.add(direct_ip)

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("URL resolves to a non-public address")

    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


async def _validate_public_url(value: str) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_validate_public_url_sync, value),
            timeout=5,
        )
    except TimeoutError as exc:
        raise ValueError("URL DNS validation timed out") from exc


async def _validate_research_urls(query: str) -> None:
    for candidate in _EXPLICIT_URL.findall(query):
        await _validate_public_url(candidate.rstrip(".,);]}"))


def _provider_count(result: dict[str, Any]) -> int:
    names = {
        str(name)
        for name in (result.get("providers_used") or [])
        if name
    }
    if names:
        return len(names)
    for attempt in result.get("provider_attempts") or []:
        if isinstance(attempt, dict) and attempt.get("provider"):
            names.add(str(attempt["provider"]))
    return len(names)


async def _run_with_global_slot(
    operation: Callable[[], Awaitable[dict[str, Any]]],
    timeout_seconds: float,
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with _GLOBAL_SEMAPHORE:
            return await operation()

    return await asyncio.wait_for(run(), timeout=timeout_seconds)


def _audit_tool_call(
    *,
    name: str,
    subject: str,
    start: float,
    status: str,
    provider_count: int = 0,
    artifact_id: str = "",
) -> None:
    audit_log.info(
        json.dumps(
            {
                "event": "tool_call",
                "tool": name,
                "subject": subject,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                "status": status,
                "provider_count": provider_count,
                "artifact_id": artifact_id or None,
            },
            separators=(",", ":"),
        )
    )


async def _preflight_rate_limit(
    *,
    name: str,
    token: AccessToken,
    research: bool = False,
) -> dict[str, Any] | None:
    subject = _subject_id(token)
    start = time.monotonic()
    limiter = _RESEARCH_LIMITER if research else _TOOL_LIMITER
    if await limiter.check(subject):
        return None
    _audit_tool_call(
        name=name,
        subject=subject,
        start=start,
        status="rate_limited",
    )
    return {
        "ok": False,
        "status": "rate_limited",
        "error_type": "rate_limit",
        "error": "request rate limit exceeded",
    }


async def _execute_tool(
    *,
    name: str,
    token: AccessToken,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    timeout_seconds: float,
    artifact_id: str = "",
    research: bool = False,
    rate_limit_checked: bool = False,
) -> dict[str, Any]:
    subject = _subject_id(token)
    start = time.monotonic()
    limiter = _RESEARCH_LIMITER if research else _TOOL_LIMITER
    if not rate_limit_checked and not await limiter.check(subject):
        _audit_tool_call(
            name=name,
            subject=subject,
            start=start,
            status="rate_limited",
            artifact_id=artifact_id,
        )
        return {
            "ok": False,
            "status": "rate_limited",
            "error_type": "rate_limit",
            "error": "request rate limit exceeded",
            **({"artifact_id": artifact_id} if artifact_id else {}),
        }

    status = "error"
    provider_count = 0
    try:
        if research:
            # A queued research must not consume one of the two general slots.
            async def run_research() -> dict[str, Any]:
                async with _RESEARCH_SEMAPHORE:
                    async with _GLOBAL_SEMAPHORE:
                        return await operation()

            result = await asyncio.wait_for(
                run_research(),
                timeout=timeout_seconds,
            )
        else:
            result = await _run_with_global_slot(operation, timeout_seconds)
        status = "ok" if result.get("ok") else "failed"
        provider_count = _provider_count(result)
        return result
    except (asyncio.TimeoutError, TimeoutError):
        status = "timeout"
        return {
            "ok": False,
            "status": "timeout",
            "error_type": "timeout",
            "error": "tool execution exceeded its time limit",
            **({"artifact_id": artifact_id} if artifact_id else {}),
        }
    except Exception:
        status = "error"
        return {
            "ok": False,
            "status": "failed",
            "error_type": "runtime_error",
            "error": "tool execution failed",
            **({"artifact_id": artifact_id} if artifact_id else {}),
        }
    finally:
        _audit_tool_call(
            name=name,
            subject=subject,
            start=start,
            status=status,
            provider_count=provider_count,
            artifact_id=artifact_id,
        )


def _new_artifact_path() -> tuple[str, Path]:
    artifact_id = str(uuid.uuid4())
    path = EVIDENCE_DIR / artifact_id
    return artifact_id, path


def cleanup_old_artifacts(now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    deleted = 0
    if not EVIDENCE_DIR.exists():
        return deleted
    for entry in EVIDENCE_DIR.iterdir():
        if (
            not entry.is_dir()
            or entry.is_symlink()
            or not _UUID_NAME.fullmatch(entry.name)
        ):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                deleted += 1
        except FileNotFoundError:
            continue
    return deleted


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(6 * 60 * 60)
        deleted = await asyncio.to_thread(cleanup_old_artifacts)
        if deleted:
            audit_log.info(
                json.dumps(
                    {"event": "evidence_cleanup", "deleted": deleted},
                    separators=(",", ":"),
                )
            )


def _scrub_artifact_files(path: Path) -> None:
    path.chmod(0o700)
    for entry in path.rglob("*"):
        if entry.is_symlink():
            entry.unlink(missing_ok=True)
            continue
        if entry.is_dir():
            entry.chmod(0o700)
            continue
        if not entry.is_file():
            continue
        marker = ""
        try:
            if entry.stat().st_size > 10 * 1024 * 1024:
                marker = "[REDACTED: oversized evidence omitted]\n"
        except OSError:
            continue
        try:
            original = "" if marker else entry.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            original = ""
            marker = "[REDACTED: non-text evidence omitted]\n"
        scrubbed = marker or _redact_text(original, limit=max(len(original), 1))
        if scrubbed != original:
            temporary = entry.with_suffix(entry.suffix + ".scrub")
            temporary.write_text(scrubbed, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(entry)
        entry.chmod(0o600)


def _compact_research(
    result: dict[str, Any],
    artifact_id: str,
) -> dict[str, Any]:
    raw_gaps = (result.get("gap_check") or {}).get("gaps") or []
    gaps: list[Any] = []
    for item in raw_gaps[:20]:
        if isinstance(item, dict):
            gaps.append(
                {
                    key: _redact_text(str(item[key]), limit)
                    for key, limit in (
                        ("subquestion_id", 128),
                        ("reason", 1_000),
                        ("url", 2_048),
                        ("status", 128),
                    )
                    if item.get(key)
                }
            )
        else:
            gaps.append(_redact_text(str(item), 1_000))
    citations = []
    for item in (result.get("citations") or [])[:20]:
        if not isinstance(item, dict):
            continue
        citations.append(
            {
                key: _redact_text(
                    str(item[key]),
                    {"title": 512, "url": 2_048, "provider": 128}[key],
                )
                for key in ("title", "url", "provider")
                if item.get(key)
            }
        )

    if result.get("ok") and not result.get("degraded"):
        status = "completed"
    elif result.get("ok"):
        status = "degraded"
    else:
        status = "failed"

    compact = {
        "ok": bool(result.get("ok")),
        "status": status,
        "artifact_id": artifact_id,
        "summary": _redact_text(
            str(result.get("final_answer") or result.get("content") or ""),
            32 * 1024,
        ),
        "citations": citations,
        "gap_check": {
            "status": (result.get("gap_check") or {}).get("status", "failed"),
            "gaps": gaps,
            "stop_reason": (result.get("gap_check") or {}).get("stop_reason", ""),
        },
        "providers_used": _sanitize(result.get("providers_used") or []),
        "provider_count": _provider_count(result),
        "fallback_used": bool(result.get("fallback_used")),
        "degraded": bool(result.get("degraded")),
        "error_type": result.get("error_type", ""),
        "error": _redact_text(str(result.get("error", "")), 2_000),
        "elapsed_ms": result.get("elapsed_ms", 0),
    }
    return _bound_output(compact)


async def smart_search_tool(
    query: Query,
    platform: Platform = "",
    extra_sources: Annotated[int, Field(ge=0, le=10)] = 0,
    validation: Validation = "balanced",
    providers: Providers = "auto",
    timeout_seconds: Annotated[float, Field(ge=10, le=300)] = 90,
    token: AccessToken = CurrentAccessToken(),
) -> dict[str, Any]:
    """Search current information with provider and source provenance."""
    limited = await _preflight_rate_limit(name="smart_search", token=token)
    if limited is not None:
        return _bound_output(limited)
    normalized_providers = _normalize_providers(providers)
    result = await _execute_tool(
        name="smart_search",
        token=token,
        timeout_seconds=timeout_seconds + 30,
        rate_limit_checked=True,
        operation=lambda: service.search(
            query=query,
            platform=platform,
            extra_sources=extra_sources,
            validation=validation,
            providers=normalized_providers,
            timeout_seconds=timeout_seconds,
        ),
    )
    return _bound_output(result)


async def smart_fetch_tool(
    url: URL,
    token: AccessToken = CurrentAccessToken(),
) -> dict[str, Any]:
    """Fetch a public URL through configured extraction-provider fallback."""
    limited = await _preflight_rate_limit(name="smart_fetch", token=token)
    if limited is not None:
        return _bound_output(limited)

    async def run_fetch() -> dict[str, Any]:
        normalized_url = await _validate_public_url(url)
        return await service.fetch(normalized_url)

    result = await _execute_tool(
        name="smart_fetch",
        token=token,
        timeout_seconds=180,
        rate_limit_checked=True,
        operation=run_fetch,
    )
    return _bound_output(result)


async def smart_map_tool(
    url: URL,
    instructions: Instructions = "",
    max_depth: Annotated[int, Field(ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(ge=1, le=100)] = 20,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    token: AccessToken = CurrentAccessToken(),
) -> dict[str, Any]:
    """Map a public site's structure before fetching individual pages."""
    limited = await _preflight_rate_limit(name="smart_map", token=token)
    if limited is not None:
        return _bound_output(limited)

    async def run_map() -> dict[str, Any]:
        normalized_url = await _validate_public_url(url)
        return await service.map_site(
            url=normalized_url,
            instructions=instructions,
            max_depth=max_depth,
            max_breadth=max_breadth,
            limit=limit,
            timeout=150,
        )

    result = await _execute_tool(
        name="smart_map",
        token=token,
        timeout_seconds=180,
        rate_limit_checked=True,
        operation=run_map,
    )
    return _bound_output(result)


async def smart_route_tool(
    query: Query,
    validation: Validation = "balanced",
    mode: RouteMode = "",
    token: AccessToken = CurrentAccessToken(),
) -> dict[str, Any]:
    """Explain required search capabilities without executing remote search."""
    limited = await _preflight_rate_limit(name="smart_route", token=token)
    if limited is not None:
        return _bound_output(limited)
    result = await _execute_tool(
        name="smart_route",
        token=token,
        timeout_seconds=30,
        rate_limit_checked=True,
        operation=lambda: service.route(
            query=query,
            validation=validation,
            mode=mode,
            allow_remote=False,
        ),
    )
    return _bound_output(result)


async def smart_research_tool(
    query: Query,
    budget: ResearchBudget = "standard",
    fallback: Fallback = "auto",
    token: AccessToken = CurrentAccessToken(),
) -> dict[str, Any]:
    """Run source discovery, fetching, gap checks, and evidence synthesis."""
    limited = await _preflight_rate_limit(
        name="smart_research",
        token=token,
        research=True,
    )
    if limited is not None:
        return _bound_output(limited)

    artifact_id, artifact_dir = _new_artifact_path()
    artifact_created = False

    async def run_research() -> dict[str, Any]:
        nonlocal artifact_created
        await _validate_research_urls(query)
        _ensure_private_directory(EVIDENCE_DIR)
        await asyncio.to_thread(cleanup_old_artifacts)
        artifact_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        artifact_created = True
        return await service.research(
            query=query,
            budget=budget,
            evidence_dir=str(artifact_dir),
            fallback=fallback,
        )

    try:
        result = await _execute_tool(
            name="smart_research",
            token=token,
            timeout_seconds=RESEARCH_OPERATION_SECONDS,
            artifact_id=artifact_id,
            research=True,
            rate_limit_checked=True,
            operation=run_research,
        )
    finally:
        if artifact_created and artifact_dir.exists():
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(30):
                    await anyio.to_thread.run_sync(
                        _scrub_artifact_files,
                        artifact_dir,
                        abandon_on_cancel=True,
                    )
    if result.get("status") in {"timeout", "rate_limited"}:
        return _bound_output(result)
    return _compact_research(result, artifact_id)


def build_oidc_provider() -> tuple[OIDCProxy, DiskStore]:
    _ensure_data_dirs()
    client_id = os.getenv("SMARTSEARCH_REMOTE_OIDC_CLIENT_ID", "smartsearch-mcp")
    client_secret = _require_secret("SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET")
    signing_key = _require_secret("SMARTSEARCH_REMOTE_JWT_SIGNING_KEY")
    storage_key = _require_secret("SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY")

    raw_store = DiskStore(
        directory=OAUTH_DIR,
        auto_create=False,
        max_size=64 * 1024 * 1024,
    )
    encrypted_store = FernetEncryptionWrapper(
        raw_store,
        source_material=storage_key,
        salt="smartsearch-remote-oauth-v1",
        raise_on_decryption_error=True,
    )
    auth = OIDCProxy(
        config_url=AUTHELIA_DISCOVERY_URL,
        client_id=client_id,
        client_secret=client_secret,
        base_url=PUBLIC_BASE_URL,
        resource_base_url=PUBLIC_BASE_URL,
        issuer_url=PUBLIC_BASE_URL,
        redirect_path="/auth/callback",
        required_scopes=["openid", "profile", "offline_access"],
        verify_id_token=True,
        allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECT_URIS,
        client_storage=encrypted_store,
        jwt_signing_key=signing_key,
        token_endpoint_auth_method="client_secret_basic",
        require_authorization_consent=True,
        enable_cimd=True,
        forward_resource=False,
        token_expiry_threshold_seconds=30,
    )
    return auth, raw_store


def build_mcp(auth: OIDCProxy | None = None, oauth_store: Any = None) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_: FastMCP):
        _ensure_data_dirs()
        await asyncio.to_thread(cleanup_old_artifacts)
        cleanup_task = asyncio.create_task(_cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            if oauth_store is not None:
                await oauth_store.close()

    middleware: list[Any] = [ResponseLimitingMiddleware(max_size=256_000)]
    if auth is not None:
        middleware.insert(0, AuthMiddleware(auth=_only_authorized_user))

    mcp = FastMCP(
        SERVICE_NAME,
        version=SERVICE_VERSION,
        instructions=(
            "Read-only current-information research. Fetch primary pages before "
            "relying on material claims. smart_research keeps full evidence in a "
            "private 30-day audit store and returns only a compact artifact ID."
        ),
        auth=auth,
        middleware=middleware,
        lifespan=lifespan,
        mask_error_details=True,
        strict_input_validation=True,
    )
    mcp.tool(
        name="smart_search",
        annotations=READ_ONLY_OPEN_WORLD,
    )(smart_search_tool)
    mcp.tool(
        name="smart_fetch",
        annotations=READ_ONLY_OPEN_WORLD,
    )(smart_fetch_tool)
    mcp.tool(
        name="smart_map",
        annotations=READ_ONLY_OPEN_WORLD,
    )(smart_map_tool)
    mcp.tool(
        name="smart_route",
        annotations=READ_ONLY_OPEN_WORLD,
    )(smart_route_tool)
    mcp.tool(
        name="smart_research",
        annotations=RESEARCH_READ_ONLY_OPEN_WORLD,
        timeout=MAX_RESEARCH_SECONDS,
    )(smart_research_tool)
    return mcp


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
        }
    )


def build_app() -> Any:
    auth, oauth_store = build_oidc_provider()
    mcp = build_mcp(auth=auth, oauth_store=oauth_store)
    app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
        json_response=False,
        allowed_hosts=[
            "smartsearch-mcp-home.172906573.xyz",
            "smartsearch-mcp",
            "localhost",
            "127.0.0.1",
        ],
        allowed_origins=[
            "https://chatgpt.com",
            "https://claude.ai",
            "https://claude.com",
        ],
    )
    app.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    return app


def main() -> None:
    app = build_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        access_log=False,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="critical",
        log_config=None,
    )


if __name__ == "__main__":
    main()
