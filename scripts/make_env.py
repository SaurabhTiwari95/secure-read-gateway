"""Create .env from .env.example with fresh random secrets.

    python scripts/make_env.py            # refuses to overwrite an existing .env
    python scripts/make_env.py --force

The agent secret is written to both CLIENT_KEYS (server side) and AGENT_SECRET (for the examples),
so they always match. .env is git-ignored.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_env(example: str, key_id: str = "demo-agent") -> str:
    agent_secret = secrets.token_urlsafe(32)
    values = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "APP_DB_PASSWORD": secrets.token_urlsafe(24),
        "CLIENT_KEYS": json.dumps({key_id: agent_secret}),
        "AGENT_KEY_ID": key_id,
        "AGENT_SECRET": agent_secret,
    }
    lines = []
    for line in example.splitlines():
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            line = f"{key}={values[key]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = root / ".env"
    if target.exists() and "--force" not in argv:
        print(".env already exists; run with --force to replace it", file=sys.stderr)
        return 1
    target.write_text(build_env((root / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote .env with fresh random secrets (it is git-ignored; never commit it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
