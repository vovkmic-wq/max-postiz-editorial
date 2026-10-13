"""Explicit reuse of the user's MAX LLM and destinations; never print secret values."""
import argparse
import json
import sqlite3
from pathlib import Path

from max_postiz_agent.config import read_topic


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def bindings(root):
    db = root / "data/state/agent.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT id, chat_id FROM channels WHERE chat_id IS NOT NULL"))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-source", type=Path, required=True)
    parser.add_argument("--pikabu-source", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.max_source.resolve()
    original = read(source / "config/app.json")
    app = read(root / "config/app.json")
    app["opencode"] = original["opencode"]
    app["opencode"]["local_access_path"] = "data/opencode-runtime/access.json"
    app["opencode"]["runtime_dir"] = "data/opencode-runtime"
    app["opencode"]["max_concurrent_tasks"] = 1
    access = source / original["opencode"].get("local_access_path", "data/opencode-runtime/access.json")
    target_access = root / "data/opencode-runtime/access.json"
    if not target_access.exists() and access.exists():
        write(target_access, read(access))
    runtime = source / original["opencode"].get("runtime_dir", "data/opencode-runtime") / "opencode.json"
    if runtime.exists():
        config = read(runtime)
        approved = {key: value for key, value in config.items() if key in {
            "$schema", "provider", "providers", "model", "models", "agent", "permission", "permissions"}}
        write(root / "data/opencode-runtime/opencode.json", approved)
    app["max"] = original["max"]
    app["video"]["backend"] = "disabled"
    app["require_postgresql"] = True
    app["api"]["port"] = 8090
    write(root / "config/app.json", app)
    topic = read_topic(source)
    for profile in topic["channels"]:
        profile["publication"]["mode"] = "review"
        profile["budget"]["daily_llm_limit"] = 0
    targets = []
    max_bindings = bindings(source)
    for profile in topic["channels"]:
        chat = max_bindings.get(profile["id"])
        if chat:
            targets.append({"id": "max_" + profile["id"], "profile_id": profile["id"], "platform": "max",
                            "route": "max_browser" if app["max"]["adapter_mode"] == "browser" else "max_api",
                            "destination": chat, "enabled": True, "options": {"max_chars": 3500}})
    if args.pikabu_source:
        pikabu_root = args.pikabu_source.resolve()
        pikabu_topic = read_topic(pikabu_root)
        pikabu_bindings = bindings(pikabu_root)
        for profile in pikabu_topic["channels"]:
            if not profile["enabled"]:
                continue
            profile = json.loads(json.dumps(profile))
            old_id = profile["id"]
            profile["id"] = "pikabu_" + old_id
            profile["publication"]["mode"] = "review"
            topic["channels"].append(profile)
            destination = pikabu_bindings.get(old_id, "")
            targets.append({"id": "pikabu_" + old_id, "profile_id": profile["id"], "platform": "pikabu",
                            "route": "manual", "destination": destination,
                            "enabled": bool(destination), "options": {"max_chars": 15000}})
    for platform in ("telegram", "vk"):
        targets.append({"id": platform, "profile_id": "test", "platform": platform, "route": "disabled",
                        "destination": "", "enabled": False, "options": {"max_chars": 3500}})
    (root / "TOPIC.md").write_text("# Тематика редакции\n\n<!-- TOPIC_CONFIG_BEGIN -->\n```json\n" +
        json.dumps(topic, ensure_ascii=False, indent=2) + "\n```\n<!-- TOPIC_CONFIG_END -->\n", encoding="utf-8")
    publishing = read(root / "config/publishing.example.json")
    publishing["targets"] = targets
    write(root / "config/publishing.json", publishing)
    # The existing profile is used in place under its original exclusive lock.
    # No browser cookies are exported or copied. A portable installation logs in anew.
    if app["max"]["adapter_mode"] == "browser":
        env = root / ".env.local"
        lines = env.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if not line.startswith("MAX_EXISTING_BROWSER_PROFILE=")]
        lines.append("MAX_EXISTING_BROWSER_PROFILE=" + str(source / app["max"]["browser"]["profile_dir"]))
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Register existing sources as disabled until the new collector is explicitly enabled.
    source_config = read(source / original["sources_path"])
    for item in source_config["sources"]:
        item["enabled"] = False
    write(root / "config/sources.json", source_config)
    write(root / "data/configuration-reuse.json", {"llm_model": app["opencode"].get("model_id"),
          "source": str(source), "targets": [{"id": t["id"], "route": t["route"], "destination": t["destination"]}
                                              for t in targets], "delivery_paused": True})
    print(json.dumps({"model": app["opencode"].get("model_id"), "targets": [t["id"] for t in targets],
                      "delivery_paused": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
