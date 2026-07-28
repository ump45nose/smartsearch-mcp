#!/usr/bin/env python3
"""Run SmartSearch doctor and print only non-secret readiness fields."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from smart_search import service


async def main() -> None:
    result = await service.doctor()
    connection_status: dict[str, Any] = {}
    for key, value in result.items():
        if key.endswith("_connection_test") and isinstance(value, dict):
            connection_status[key] = value.get("status", "unknown")

    main_status = {
        str(provider): (
            value.get("status", "unknown")
            if isinstance(value, dict)
            else "unknown"
        )
        for provider, value in (
            result.get("main_search_connection_tests") or {}
        ).items()
    }
    capability_status = {
        str(capability): {
            "ok": bool(value.get("ok")),
            "configured": [
                str(provider)
                for provider in (value.get("configured") or [])
            ],
        }
        for capability, value in (result.get("capability_status") or {}).items()
        if isinstance(value, dict)
    }
    print(
        json.dumps(
            {
                "ok": bool(result.get("ok")),
                "minimum_profile_ok": bool(result.get("minimum_profile_ok")),
                "minimum_profile_missing": result.get(
                    "minimum_profile_missing",
                    [],
                ),
                "primary_api_mode": result.get("primary_api_mode", ""),
                "main_search_connection_status": main_status,
                "connection_status": connection_status,
                "capability_status": capability_status,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
