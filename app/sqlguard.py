"""Static check that a registered function's SQL can only read.

This is the first of three layers. The other two are stronger and always on:
  - every query runs inside a READ ONLY transaction, and
  - the app's database role has SELECT rights only (plus INSERT on the audit log).
The guard exists so a bad registration is refused at load time, loudly, instead of failing later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "merge", "upsert", "drop", "alter", "create", "truncate",
        "grant", "revoke", "copy", "call", "do", "execute", "prepare", "deallocate", "vacuum",
        "analyze", "cluster", "reindex", "refresh", "lock", "listen", "notify", "unlisten", "set",
        "reset", "begin", "commit", "rollback", "savepoint", "release", "comment", "security",
        "into", "import", "load", "discard", "checkpoint",
    }
)  # fmt: skip

FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
        "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get", "lo_put", "dblink",
        "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "set_config", "nextval",
        "setval", "pg_advisory_lock", "pg_advisory_xact_lock", "query_to_xml",
        "query_to_xml_and_xmlschema", "cursor_to_xml", "pg_reload_conf", "pg_rotate_logfile",
    }
)  # fmt: skip

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_DOLLAR_STRING = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|)\$.*?\$\1\$", re.DOTALL)
_STRING = re.compile(r"(?:[eE])?'(?:[^']|'')*'")
_LOCKING = re.compile(r"\bfor\s+(?:no\s+key\s+)?(?:update|share|key\s+share)\b")
_WORD = re.compile(r"[a-z_][a-z0-9_$]*")
_CALL = re.compile(r"([a-z_][a-z0-9_$]*)\s*\(")


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str = ""


def normalise(sql: str) -> str:
    """Lower-case SQL with comments and string literals removed and identifiers unquoted."""
    text = _BLOCK_COMMENT.sub(" ", sql)
    text = _DOLLAR_STRING.sub(" '' ", text)
    text = _STRING.sub(" '' ", text)
    text = _LINE_COMMENT.sub(" ", text)
    text = text.replace('"', "")  # "pg_sleep"(1) must not hide a call
    return " ".join(text.lower().split())


def check(sql: str) -> GuardResult:
    if not sql or not sql.strip():
        return GuardResult(False, "empty")
    text = normalise(sql)
    if "/*" in text or "*/" in text:
        return GuardResult(False, "unbalanced comment")
    if text.rstrip("; ").count(";"):
        return GuardResult(False, "more than one statement")
    text = text.rstrip("; ")
    if not (text.startswith("select ") or text.startswith("with ")):
        return GuardResult(False, "must start with SELECT or WITH")
    if _LOCKING.search(text):
        return GuardResult(False, "row locking (FOR UPDATE/SHARE) is not allowed")
    for word in _WORD.findall(text):
        if word in FORBIDDEN_KEYWORDS:
            return GuardResult(False, f"forbidden keyword: {word}")
    for name in _CALL.findall(text):
        if name in FORBIDDEN_FUNCTIONS or name.startswith("pg_catalog"):
            return GuardResult(False, f"forbidden function: {name}")
    return GuardResult(True)
