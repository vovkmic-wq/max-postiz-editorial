"""Apply the bounded article profile to this project's private OpenCode v2 runtime.

The native v2 model ``body`` field overlays the outgoing compatible API request.
No server is contacted or restarted, and configuration values never enter output.
"""

import argparse
import copy
import json
import math
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from max_postiz_agent.config import AgentError, unique_keys

MODEL_IDS = ("qwen35-9b", "qwen25-coder-7b")


def _reject_constant(_):
    raise ValueError


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _paths(root):
    root = Path(root).resolve()
    runtime = root / "data" / "opencode-runtime" / "opencode.json"
    backups = root / "data" / "backups"
    if runtime.resolve() != runtime or backups.resolve() != backups:
        raise AgentError("OPENCODE_RUNTIME_PATH_INVALID")
    return runtime, backups


def _candidate(original):
    try:
        config = json.loads(original.decode("utf-8-sig"), object_pairs_hook=unique_keys,
                            parse_constant=_reject_constant, parse_float=_finite_float)
        models = config["providers"]["proxmox"]["models"]
        if not isinstance(models, dict):
            raise ValueError
        for ident in MODEL_IDS:
            if not isinstance(models.get(ident), dict):
                raise AgentError("OPENCODE_ARTICLE_MODELS_REQUIRED")
            body = models[ident].get("body", {})
            if not isinstance(body, dict):
                raise ValueError
            if ident == MODEL_IDS[0] and not isinstance(body.get("chat_template_kwargs", {}), dict):
                raise ValueError
    except AgentError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise AgentError("OPENCODE_RUNTIME_CONFIG_INVALID") from exc
    updated = copy.deepcopy(config)
    changed = []
    for ident in MODEL_IDS:
        model = updated["providers"]["proxmox"]["models"][ident]
        body = model.setdefault("body", {})
        if ident == MODEL_IDS[0]:
            body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
            body.update(max_tokens=2048, temperature=0.2)
        else:
            body.update(max_tokens=512, temperature=0)
        if json.dumps(model, sort_keys=True) != json.dumps(models[ident], sort_keys=True):
            changed.append("proxmox/" + ident)
    payload = (json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return payload, changed


def _write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def configure(root, apply=False):
    runtime, backups = _paths(root)
    try:
        original = runtime.read_bytes()
    except FileNotFoundError as exc:
        raise AgentError("OPENCODE_RUNTIME_CONFIG_REQUIRED") from exc
    payload, changed = _candidate(original)
    if apply and changed:
        # Validation completes before creating even the backup directory.
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d-%H%M%S")
        backup = backups / f"opencode-before-articles-{stamp}-{uuid4().hex}.local.json"
        _write_private(backup, original)
        temporary = runtime.with_name(f".opencode-articles-{uuid4().hex}.new")
        try:
            _write_private(temporary, payload)
            if runtime.read_bytes() != original:
                raise AgentError("OPENCODE_RUNTIME_CHANGED", 409)
            os.replace(temporary, runtime)
            if os.name != "nt":
                descriptor = os.open(runtime.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    return {"changed": bool(changed), "models": changed,
            "applied": bool(apply and changed), "restart_required": bool(apply and changed)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true", help="Save a private backup, then atomically apply")
    args = parser.parse_args(argv)
    try:
        report = configure(args.root, args.apply)
    except (AgentError, OSError) as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError) else "OPENCODE_PROFILE_WRITE_FAILED"}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
