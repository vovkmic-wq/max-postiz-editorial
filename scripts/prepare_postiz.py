"""Prepare canonical upstream Compose at an immutable commit; never starts containers."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx
import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="main")
    parser.add_argument("--destination", type=Path, default=Path("deployment/postiz"))
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    repo = "gitroomhq/postiz-docker-compose"
    with httpx.Client(timeout=30, follow_redirects=True, trust_env=False) as client:
        response = client.get("https://api.github.com/repos/" + repo + "/commits/" + args.ref)
        response.raise_for_status()
        commit = response.json()["sha"]
        tree = client.get("https://api.github.com/repos/" + repo + "/git/trees/" + commit, params={"recursive": "1"})
        tree.raise_for_status()
        files = [f["path"] for f in tree.json()["tree"] if f["type"] == "blob" and
                 (f["path"] in {"docker-compose.yml", "docker-compose.yaml"} or f["path"].startswith("dynamicconfig/"))]
        hashes = {}
        compose = None
        for name in files:
            raw = client.get("https://raw.githubusercontent.com/" + repo + "/" + commit + "/" + name)
            raw.raise_for_status()
            if len(raw.content) > 2 * 1024 * 1024:
                raise RuntimeError("Upstream file too large")
            hashes[name] = hashlib.sha256(raw.content).hexdigest()
            if name.startswith("docker-compose."):
                compose = yaml.safe_load(raw.text)
            else:
                path = (destination / name).resolve()
                if not path.is_relative_to(destination):
                    raise RuntimeError("Invalid upstream path")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw.content)
    if not compose:
        raise RuntimeError("Canonical Compose not found")
    services = compose["services"]
    services.pop("spotlight", None)
    for network in compose.get("networks", {}).values():
        if isinstance(network, dict) and not network.get("external"):
            network.pop("name", None)
    for name, service in services.items():
        service.pop("container_name", None)
        if "ports" in service:
            # Upstream can already bind a host IP; replace it instead of prefixing twice.
            ports = []
            for port in service["ports"]:
                if isinstance(port, dict):
                    ports.append({**port, "host_ip": "127.0.0.1"})
                else:
                    parts = str(port).rsplit(":", 2)
                    if len(parts) == 1:
                        raise RuntimeError("An explicit published port is required")
                    ports.append("127.0.0.1:" + ":".join(parts[-2:]))
            service["ports"] = ports
        if name == "postiz":
            service["image"] = "${POSTIZ_IMAGE:?Choose an explicit release tag or digest}"
            service["environment"]["JWT_SECRET"] = "${POSTIZ_JWT_SECRET:?Required}"  # noqa: S105 -- required env reference
            service["environment"]["DATABASE_URL"] = "postgresql://postiz-user:${POSTIZ_DB_PASSWORD:?Required}@postiz-postgres:5432/postiz-db-local"
            service["environment"]["DISABLE_REGISTRATION"] = "${POSTIZ_DISABLE_REGISTRATION:-true}"
            # This deployment publishes only to loopback HTTP.
            service["environment"]["NOT_SECURED"] = "true"
            service["environment"]["OPENAI_API_KEY"] = ""
        elif name == "postiz-postgres":
            service["environment"]["POSTGRES_PASSWORD"] = "${POSTIZ_DB_PASSWORD:?Required}"  # noqa: S105 -- env reference
        elif name == "temporal-postgresql":
            service["environment"]["POSTGRES_PASSWORD"] = "${TEMPORAL_DB_PASSWORD:?Required}"  # noqa: S105 -- env reference
        elif name == "temporal":
            service["environment"] = ["POSTGRES_PWD=${TEMPORAL_DB_PASSWORD:?Required}" if item.startswith("POSTGRES_PWD=") else item for item in service["environment"]]
            service.setdefault("healthcheck", {})["test"] = ["CMD-SHELL", 'temporal operator cluster health --address "$$(hostname -i):7233" --command-timeout 4s']
        elif name == "temporal-ui":
            service["ports"] = ["127.0.0.1:8088:8080"]
    (destination / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    (destination / "upstream-manifest.json").write_text(json.dumps({"repository": repo, "commit": commit,
        "sha256": hashes, "mutations": ["loopback_ports", "required_secrets", "explicit_postiz_image", "no_spotlight"],
        "runtime_verified": False}, indent=2), encoding="utf-8")
    print(json.dumps({"state": "prepared", "commit": commit, "runtime_verified": False}))


if __name__ == "__main__":
    main()
