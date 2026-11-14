from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_make_env_fills_every_placeholder_and_keeps_secrets_in_sync(tmp_path):
    make_env = _load("make_env")
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    assert make_env.main([], root=tmp_path) == 0
    env = dict(
        line.split("=", 1)
        for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert "replace-with" not in "".join(env.values())
    assert json.loads(env["CLIENT_KEYS"]) == {env["AGENT_KEY_ID"]: env["AGENT_SECRET"]}
    assert len(env["AGENT_SECRET"]) >= 32
    assert make_env.main([], root=tmp_path) == 1  # won't overwrite
    assert make_env.main(["--force"], root=tmp_path) == 0
