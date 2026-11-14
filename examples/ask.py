"""What an AI agent does: list its functions, then ask a question.

    python examples/ask.py

Reads GATEWAY_URL, AGENT_KEY_ID and AGENT_SECRET from the environment (or .env).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import GatewayClient, GatewayError  # noqa: E402


def load_dotenv(path: Path = Path(".env")) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_dotenv()
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    key_id, secret = os.environ.get("AGENT_KEY_ID"), os.environ.get("AGENT_SECRET")
    if not key_id or not secret:
        print("Set AGENT_KEY_ID and AGENT_SECRET (see .env.example)", file=sys.stderr)
        return 2
    with GatewayClient(url, key_id, secret) as gw:
        try:
            print("Functions I may call:")
            for f in gw.functions():
                print(f"  - {f['name']}: {f['description']}")
            result = gw.call(
                "list_overdue_invoices", {"as_of": dt.date.today().isoformat(), "min_amount": "5000"}, page_size=5
            )
        except GatewayError as err:
            print(f"Gateway refused: {err.status} {json.dumps(err.body)}", file=sys.stderr)
            return 1
    print("\nFive oldest overdue invoices of 5,000 or more:")
    for row in result["rows"]:
        print(f"  #{row['id']:>5}  {row['customer_name'][:30]:<30}  {row['total']:>12}  {row['days_overdue']} days")
    print(f"\nmore pages: {result['has_more']}   request id: {result['request_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
