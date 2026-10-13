"""Freeze already downloaded Postiz stack images by immutable digest."""
import argparse
import json
import subprocess
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    deployment = root / "deployment/postiz"
    result = subprocess.run([args.docker, "compose", "--env-file", str(deployment / ".env"),
                             "-f", str(deployment / "compose.yaml"), "config", "--format", "json"],
                            capture_output=True, check=True, text=True, encoding="utf-8")
    # Compose's environment contains secrets: never print or persist its raw output.
    services = json.loads(result.stdout)["services"]
    lock = {}
    for name, service in services.items():
        inspect = subprocess.run([args.docker, "image", "inspect", service["image"],
                                  "--format", "{{json .RepoDigests}}"],
                                 capture_output=True, check=True, text=True, encoding="utf-8")
        digests = json.loads(inspect.stdout)
        if not digests:
            raise RuntimeError("Image has no repository digest")
        lock[name] = {"image": digests[0]}
    (deployment / "images.lock.yaml").write_text(
        yaml.safe_dump({"services": lock}, sort_keys=False), encoding="utf-8")
    print(json.dumps({"state": "locked", "services": len(lock)}))


if __name__ == "__main__":
    main()
