#!/usr/bin/env python3
"""Prepare remote-MCP secrets and an offline Authelia candidate.

The script intentionally prints paths and key names only. Secret values stay
in root-owned files and the Hermes root environment file.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_ENV = Path("/home/hermes/.hermes/.env")
AUTHELIA_DIR = Path("/vol2/1000/Docker/authelia/config")
STACK_DIR = Path("/vol2/1000/Docker/stacks/smartsearch-mcp")
STATE_DIR = Path("/vol2/1000/Docker/smartsearch-mcp")
BACKUP_ROOT = STATE_DIR / "backups"
SECRET_DIR = AUTHELIA_DIR / "secrets" / "oidc"
JWKS_DIR = SECRET_DIR / "jwks"
CANDIDATE = AUTHELIA_DIR / "configuration.candidate.yml"
FRAGMENT = STACK_DIR / "authelia-oidc.fragment.yml"

REMOTE_KEYS = (
    "SMARTSEARCH_REMOTE_OIDC_CLIENT_ID",
    "SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET",
    "SMARTSEARCH_REMOTE_JWT_SIGNING_KEY",
    "SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY",
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def secure_write(path: Path, value: str, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def generated_client_secret_and_digest() -> tuple[str, str]:
    command = [
        "docker",
        "exec",
        "authelia",
        "authelia",
        "crypto",
        "hash",
        "generate",
        "pbkdf2",
        "--variant",
        "sha512",
        "--random",
        "--random.length",
        "72",
    ]
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    password = ""
    digest = ""
    for line in result.stdout.splitlines():
        if line.startswith("Random Password: "):
            password = line.removeprefix("Random Password: ").strip()
        elif line.startswith("Digest: "):
            digest = line.removeprefix("Digest: ").strip()
    if len(password) < 32 or not digest.startswith("$pbkdf2-sha512$"):
        fail("Authelia did not produce the expected secret and PBKDF2 digest")
    return password, digest


def main() -> None:
    if os.geteuid() != 0:
        fail("run with sudo so ownership and mode can be preserved")
    if not ROOT_ENV.is_file() or not FRAGMENT.is_file():
        fail("required root env or OIDC fragment is missing")
    if not AUTHELIA_DIR.is_dir():
        fail("Authelia config directory is missing")

    os.umask(0o077)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = BACKUP_ROOT / timestamp
    backup_dir.mkdir(parents=True, mode=0o700)

    env_stat = ROOT_ENV.stat()
    shutil.copy2(ROOT_ENV, backup_dir / "hermes-root.env")
    shutil.copy2(
        AUTHELIA_DIR / "configuration.yml",
        backup_dir / "authelia-configuration.yml",
    )
    shutil.copy2(
        Path("/vol2/1000/Docker/stacks/gateway-net/compose.yaml"),
        backup_dir / "gateway-net-compose.yaml",
    )
    for source, destination in (
        (
            AUTHELIA_DIR / "db.sqlite3",
            backup_dir / "authelia-db.sqlite3",
        ),
        (
            Path("/vol2/1000/Docker/nginx/data/database.sqlite"),
            backup_dir / "npm-database.sqlite",
        ),
    ):
        subprocess.run(
            ["sqlite3", str(source), f".backup {destination}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.chmod(destination, 0o600)

    root_text = ROOT_ENV.read_text(encoding="utf-8")
    env = parse_env(root_text)
    existing_remote = [key for key in REMOTE_KEYS if key in env]
    hash_path = SECRET_DIR / "smartsearch-client-secret.hash"
    if existing_remote and set(existing_remote) != set(REMOTE_KEYS):
        fail("root env has a partial SMARTSEARCH_REMOTE_* configuration")
    if existing_remote and not hash_path.is_file():
        fail("existing client secret has no matching Authelia hash file")

    SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    JWKS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SECRET_DIR, 0o700)
    os.chmod(JWKS_DIR, 0o700)

    if not existing_remote:
        client_secret, client_digest = generated_client_secret_and_digest()
        additions = {
            "SMARTSEARCH_REMOTE_OIDC_CLIENT_ID": "smartsearch-mcp",
            "SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET": client_secret,
            "SMARTSEARCH_REMOTE_JWT_SIGNING_KEY": secrets.token_urlsafe(64),
            "SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY": secrets.token_urlsafe(64),
        }
        suffix = "\n# SmartSearch remote OAuth 2.1 MCP\n"
        suffix += "\n".join(f"{key}={value}" for key, value in additions.items())
        suffix += "\n"
        secure_write(ROOT_ENV, root_text.rstrip("\n") + suffix, env_stat.st_mode & 0o777)
        os.chown(ROOT_ENV, env_stat.st_uid, env_stat.st_gid)
        secure_write(hash_path, client_digest + "\n")

    hmac_path = SECRET_DIR / "hmac.secret"
    if not hmac_path.exists():
        secure_write(
            hmac_path,
            "".join(
                secrets.choice(
                    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                )
                for _ in range(72)
            )
            + "\n",
        )

    private_key = JWKS_DIR / "private.pem"
    if not private_key.exists():
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:4096",
                "-out",
                str(private_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.chmod(private_key, 0o600)

    active_text = (AUTHELIA_DIR / "configuration.yml").read_text(encoding="utf-8")
    if re.search(r"(?m)^identity_providers\s*:", active_text):
        fail("active Authelia config already has identity_providers; merge manually")
    candidate_text = active_text.rstrip() + "\n" + FRAGMENT.read_text(encoding="utf-8")
    secure_write(CANDIDATE, candidate_text, 0o600)

    print(f"backup_dir={backup_dir}")
    print(f"candidate={CANDIDATE}")
    print("root_env_keys=SMARTSEARCH_REMOTE_OIDC_CLIENT_ID,SMARTSEARCH_REMOTE_OIDC_CLIENT_SECRET,SMARTSEARCH_REMOTE_JWT_SIGNING_KEY,SMARTSEARCH_REMOTE_STORAGE_ENCRYPTION_KEY")
    print("secret_files=hmac.secret,jwks/private.pem,smartsearch-client-secret.hash")


if __name__ == "__main__":
    main()
