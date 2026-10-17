import copy
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from max_postiz_agent.config import AgentError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_article_models.py"
SPEC = importlib.util.spec_from_file_location("configure_article_models", SCRIPT)
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


@pytest.fixture
def runtime(tmp_path):
    model = {"name": "Local model", "limit": {"context": 16384, "output": 4096},
        "headers": {"X-Model": "preserved"}, "cost": {"input": 0, "output": 0},
        "settings": {"compaction": {"enabled": False}},
        "body": {"custom_field": "retained", "chat_template_kwargs": {"custom": True}}}
    config = {"model": "proxmox/qwen35-9b", "permissions": [{"effect": "deny", "action": "*"}],
        "providers": {"proxmox": {"package": "@opencode-ai/ai/providers/openai-compatible",
            "settings": {"baseURL": "http://10.20.30.95:8083/v1", "apiKey": "fixture-private-key"},
            "headers": {"Authorization": "Bearer fixture-private-header"},
            "models": {"qwen35-9b": copy.deepcopy(model), "qwen25-coder-7b": copy.deepcopy(model),
                       "unrelated": {"body": {"max_tokens": 123}}}},
            "other": {"settings": {"apiKey": "another-fixture"}}}}
    path = tmp_path / "data/opencode-runtime/opencode.json"
    path.parent.mkdir(parents=True)
    original = ("\ufeff" + json.dumps(config, indent=4) + "\n").encode("utf-8")
    path.write_bytes(original)
    return tmp_path, path, config, original


def test_dry_run_preserves_every_byte_and_creates_no_files(runtime):
    root, path, _, original = runtime
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    result = profile.configure(root)
    assert result == {"changed": True, "models": ["proxmox/qwen35-9b", "proxmox/qwen25-coder-7b"],
                      "applied": False, "restart_required": False}
    assert path.read_bytes() == original
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


def test_apply_merges_only_profile_fields_backs_up_bytes_and_is_idempotent(runtime):
    root, path, config, original = runtime
    assert profile.configure(root, apply=True)["restart_required"] is True
    updated = json.loads(path.read_text(encoding="utf-8"))
    expected = copy.deepcopy(config)
    qwen, reviewer = (expected["providers"]["proxmox"]["models"][name] for name in profile.MODEL_IDS)
    qwen["body"]["chat_template_kwargs"]["enable_thinking"] = False
    qwen["body"].update(max_tokens=2048, temperature=0.2)
    reviewer["body"].update(max_tokens=512, temperature=0)
    assert updated == expected
    backups = list((root / "data/backups").glob("*.local.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    applied_bytes = path.read_bytes()
    assert profile.configure(root, apply=True) == {
        "changed": False, "models": [], "applied": False, "restart_required": False}
    assert path.read_bytes() == applied_bytes
    assert list((root / "data/backups").glob("*.local.json")) == backups


@pytest.mark.parametrize("change", [
    lambda config: config["providers"]["proxmox"]["models"].pop("qwen35-9b"),
    lambda config: config["providers"]["proxmox"]["models"].pop("qwen25-coder-7b"),
    lambda config: config["providers"]["proxmox"].update(models=[]),
    lambda config: config["providers"]["proxmox"]["models"]["qwen35-9b"].update(body=[]),
    lambda config: config["providers"]["proxmox"]["models"]["qwen35-9b"]["body"].update(chat_template_kwargs=False),
])
def test_malformed_required_configuration_has_no_writes(runtime, change):
    root, path, config, _ = runtime
    change(config)
    original = json.dumps(config).encode()
    path.write_bytes(original)
    with pytest.raises(AgentError):
        profile.configure(root, apply=True)
    assert path.read_bytes() == original
    assert not (root / "data/backups").exists()


@pytest.mark.parametrize("content", [b"{not-json}", b"[]", b'{"providers":{},"providers":{}}',
    b'{"providers":NaN}', b'{"providers":1e999}', b"\xff"])
def test_invalid_json_has_no_writes(tmp_path, content):
    path = tmp_path / "data/opencode-runtime/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(AgentError):
        profile.configure(tmp_path, apply=True)
    assert path.read_bytes() == content and not (tmp_path / "data/backups").exists()


def test_missing_runtime_does_not_create_it(tmp_path):
    with pytest.raises(AgentError, match="OPENCODE_RUNTIME_CONFIG_REQUIRED"):
        profile.configure(tmp_path, apply=True)
    assert list(tmp_path.iterdir()) == []


def test_cli_outputs_only_safe_metadata(runtime, capsys):
    root, _, _, _ = runtime
    assert profile.main(["--root", str(root), "--apply"]) == 0
    output = capsys.readouterr().out
    assert set(json.loads(output)) == {"changed", "models", "applied", "restart_required"}
    assert "fixture-private" not in output and "10.20.30.95" not in output
    assert "Authorization" not in output and "baseURL" not in output


def test_failed_atomic_replace_preserves_runtime_and_private_backup(runtime, monkeypatch):
    root, path, _, original = runtime

    def fail_replace(*_):
        raise OSError("fixture write failure")

    monkeypatch.setattr(profile.os, "replace", fail_replace)
    with pytest.raises(OSError):
        profile.configure(root, apply=True)
    assert path.read_bytes() == original
    assert next((root / "data/backups").glob("*.local.json")).read_bytes() == original
    assert list(path.parent.glob("*.new")) == []
