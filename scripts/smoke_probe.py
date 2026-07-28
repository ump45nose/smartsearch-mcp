#!/usr/bin/env python3
"""Exercise the remote wrapper without printing queries, bodies, or secrets."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from typing import Any

from fastmcp.server.auth import AccessToken


def load_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "smartsearch_remote_probe",
        "/app/server.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("remote server module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_token() -> AccessToken:
    return AccessToken(
        token="local-operational-probe",
        client_id="local-operational-probe",
        scopes=["openid", "profile", "offline_access"],
        expires_at=int(time.time()) + 3600,
        claims={"sub": "local-probe", "preferred_username": "yuwk"},
    )


def urls(items: list[Any]) -> list[str]:
    result: list[str] = []
    for item in items[:5]:
        if isinstance(item, dict) and item.get("url"):
            result.append(str(item["url"])[:2_048])
    return result


async def main() -> None:
    server = load_server()
    token = probe_token()

    search = await server.smart_search_tool(
        query="What is the purpose of the IANA example domains?",
        platform="web",
        extra_sources=2,
        validation="balanced",
        providers="auto",
        timeout_seconds=60,
        token=token,
    )
    fetch = await server.smart_fetch_tool(
        url="https://www.iana.org/help/example-domains",
        token=token,
    )
    research = await server.smart_research_tool(
        query=(
            "Using official sources, explain the intended use of IANA example "
            "domains and identify the governing RFC."
        ),
        budget="quick",
        fallback="auto",
        token=token,
    )

    print(
        json.dumps(
            {
                "search": {
                    "ok": bool(search.get("ok")),
                    "status": search.get("status", ""),
                    "error_type": search.get("error_type", ""),
                    "providers_used": search.get("providers_used", []),
                    "source_urls": urls(search.get("sources") or []),
                    "elapsed_ms": search.get("elapsed_ms", 0),
                },
                "fetch": {
                    "ok": bool(fetch.get("ok")),
                    "status": fetch.get("status", ""),
                    "error_type": fetch.get("error_type", ""),
                    "provider": fetch.get("provider", ""),
                    "url": str(fetch.get("url", ""))[:2_048],
                    "content_present": bool(fetch.get("content")),
                    "elapsed_ms": fetch.get("elapsed_ms", 0),
                },
                "research": {
                    "ok": bool(research.get("ok")),
                    "status": research.get("status", ""),
                    "error_type": research.get("error_type", ""),
                    "artifact_id": research.get("artifact_id", ""),
                    "providers_used": research.get("providers_used", []),
                    "citation_urls": urls(research.get("citations") or []),
                    "summary_present": bool(research.get("summary")),
                    "gap_status": (
                        research.get("gap_check") or {}
                    ).get("status", ""),
                    "elapsed_ms": research.get("elapsed_ms", 0),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
