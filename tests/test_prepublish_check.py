import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prepublish_check as pc  # noqa: E402


def rules(text, blocked=()):
    return [h.rule for h in pc.scan_text(text, "f.md", list(blocked))]


@pytest.mark.parametrize(
    "text",
    [
        "server at 192.0.2.10 (documentation range)",
        "localhost 127.0.0.1",
        "mail me at someone@example.com",
        "commits from 123+me@users.noreply.github.com",
        "phone +91-00000-00000",
        "version 1.2.3 and port 8080",
        "a plain sentence about encoding",
    ],
)
def test_allowed_text_has_no_hits(text):
    assert rules(text, ["acme-secret"]) == []


@pytest.mark.parametrize(
    "text, rule",
    [
        ("db at 10.20.30.40", "ipv4-address"),  # prepublish-ignore
        ("write to real.person@gmail.com", "email-address"),  # prepublish-ignore
        ("call +91 98765 43210", "phone-number"),  # prepublish-ignore
        ("call 9876543210", "phone-number"),  # prepublish-ignore
        ("key AKIA" + "ABCDEFGHIJKLMNOP", "aws-access-key"),  # prepublish-ignore
        ("arn:aws:iam::123456789012:role/x", "aws-arn-with-account"),  # prepublish-ignore
        ("account 123456789012", "aws-account-id"),  # prepublish-ignore
        ("-----BEGIN RSA PRIVATE KEY-----", "private-key"),  # prepublish-ignore
    ],
)
def test_private_patterns_are_caught(text, rule):
    assert rule in rules(text)


def test_blocked_word_is_caught_whole_word_only_and_not_printed():
    hits = pc.scan_text("We worked at Contoso.\ncontosoish is fine", "f.md", ["contoso"])
    assert [(h.line, h.rule) for h in hits] == [(1, "blocked-word #1")]
    assert "contoso" not in str(hits[0]).lower().replace("f.md", "")


def test_ignore_marker_skips_patterns_but_never_blocked_words():
    marker = pc.IGNORE_MARKER
    assert rules(f"10.20.30.40 {marker}") == []  # prepublish-ignore
    assert rules(f"Contoso {marker}", ["contoso"]) == ["blocked-word #1"]


def test_invalid_ip_like_string_is_ignored():
    assert rules("build 999.1.2.3") == []


def test_load_blocked_words_from_env_and_file(tmp_path):
    f = tmp_path / "words.txt"
    f.write_text("# comment\nFabrikam\n\ncontoso\n", encoding="utf-8")
    words = pc.load_blocked_words({"BLOCKED_WORDS": "Contoso, Northwind", "BLOCKED_WORDS_FILE": str(f)})
    assert words == ["contoso", "northwind", "fabrikam"]


def test_scan_repo_skips_git_and_reports_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("contoso", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("ok\nContoso here\n", encoding="utf-8")
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00bad")
    hits = pc.scan_repo(tmp_path, ["contoso"])
    assert [str(h) for h in hits] == ["notes/a.md:2  blocked-word #1"]


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BLOCKED_WORDS_FILE", str(tmp_path / "missing.txt"))
    monkeypatch.delenv("BLOCKED_WORDS", raising=False)
    (tmp_path / "a.md").write_text("clean\n", encoding="utf-8")
    assert pc.main([str(tmp_path)]) == 2
    assert pc.main([str(tmp_path), "--allow-missing-blocklist"]) == 0

    monkeypatch.setenv("BLOCKED_WORDS", "contoso")
    assert pc.main([str(tmp_path)]) == 0
    (tmp_path / "b.md").write_text("contoso\n", encoding="utf-8")
    assert pc.main([str(tmp_path)]) == 1
    assert "1 hit(s)" in capsys.readouterr().out
