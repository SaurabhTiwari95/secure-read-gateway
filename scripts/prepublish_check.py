"""Pre-publish check: refuse to publish anything private.

Scans every text file in the repo for:
  - words on your private block-list (employer, product and people names you never publish)
  - IP addresses (except documentation ranges and localhost)
  - email addresses (except example domains and GitHub noreply)
  - Indian mobile numbers (except all-zero placeholders)
  - AWS account ids, ARNs with account ids, AWS access keys, private-key blocks

The block-list itself is never stored in the repo. It's read from:
  1. the BLOCKED_WORDS environment variable (comma- or newline-separated; used in CI as a secret)
  2. the file named by BLOCKED_WORDS_FILE, or ~/.prepublish/blocked-words.txt by default

Hits are printed as  path:line  rule  , and block-list hits never print the word itself,
so CI logs on a public repo stay clean. Exit code 1 if anything is found.

Usage:
    python scripts/prepublish_check.py [path]
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
}
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
    ".ps1",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sql",
    ".env",
    ".example",
    "",
}
MAX_BYTES = 2_000_000
# Put this on a line holding a deliberate fake sample (tests). It skips the pattern rules for
# that line only; block-list words are still checked.
IGNORE_MARKER = "prepublish-ignore"

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
DOC_NETWORKS = [
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8", "0.0.0.0/32")
]

# Base addresses of the standard private ranges, as written in CIDR config; they reveal nothing.
GENERIC_NETWORK_BASES = {"10.0.0.0", "172.16.0.0", "192.168.0.0"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("ipv4-address", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("email-address", re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")),
    ("phone-number", re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws-arn-with-account", re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{12}:")),
    ("aws-account-id", re.compile(r"(?<![\d-])\d{12}(?![\d-])")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.rule}"


def load_blocked_words(env: dict[str, str] | None = None) -> list[str]:
    """Return the private block-list from the environment and/or a local file."""
    env = dict(os.environ) if env is None else env
    words: list[str] = []
    raw = env.get("BLOCKED_WORDS", "")
    words += [w.strip() for w in re.split(r"[,\n]", raw) if w.strip()]
    file_path = Path(env.get("BLOCKED_WORDS_FILE", Path.home() / ".prepublish" / "blocked-words.txt"))
    if file_path.is_file():
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    # de-duplicate, keep order
    return list(dict.fromkeys(w.lower() for w in words))


def _allowed(rule: str, match: re.Match[str]) -> bool:
    text = match.group(0)
    if rule == "ipv4-address":
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return True  # e.g. a version string like 999.1.2.3 isn't an address
        return text in GENERIC_NETWORK_BASES or any(ip in net for net in DOC_NETWORKS)
    if rule == "email-address":
        domain = match.group(1).lower()
        return domain.endswith(ALLOWED_EMAIL_DOMAINS)
    if rule == "phone-number":
        return set(re.sub(r"\D", "", text.removeprefix("+91"))) == {"0"}
    return False


def scan_text(text: str, rel_path: str, blocked: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    word_patterns = [
        (i, re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.IGNORECASE))
        for i, w in enumerate(blocked, start=1)
    ]
    for lineno, line in enumerate(text.splitlines(), start=1):
        for i, pat in word_patterns:
            if pat.search(line):
                hits.append(Hit(rel_path, lineno, f"blocked-word #{i}"))
        if IGNORE_MARKER in line:
            continue  # pattern rules only; block-list words can never be ignored
        for rule, pat in RULES:
            for m in pat.finditer(line):
                if not _allowed(rule, m):
                    hits.append(Hit(rel_path, lineno, rule))
                    break
    return hits


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and (path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".")):
            if path.stat().st_size <= MAX_BYTES:
                yield path


def scan_repo(root: Path, blocked: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += scan_text(text, path.relative_to(root).as_posix(), blocked)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=".", help="folder to scan (default: current)")
    parser.add_argument(
        "--allow-missing-blocklist",
        action="store_true",
        help="run even if no block-list is configured (not recommended)",
    )
    args = parser.parse_args(argv)

    blocked = load_blocked_words()
    if not blocked and not args.allow_missing_blocklist:
        print(
            "No block-list found. Set BLOCKED_WORDS or create ~/.prepublish/blocked-words.txt "
            "(see README, 'Configuration').",
            file=sys.stderr,
        )
        return 2

    hits = scan_repo(Path(args.path).resolve(), blocked)
    for hit in hits:
        print(hit)
    print(f"prepublish-check: {len(hits)} hit(s), {len(blocked)} blocked word(s) checked.")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
