"""Install a hash-pinned local CLIP model outside the tracked source tree."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import httpx

REVISION = "b318363e9c2fe791fd691d44862bb4cd4d7accc5"
BASE = f"https://huggingface.co/onnx-community/clip-vit-base-patch32-ONNX/resolve/{REVISION}/"
FILES = {
    "onnx/model_quantized.onnx": "06e11583385faecfa7273f7cc72b603be915ab9340269482cde69a5d0f5e3350",
    "tokenizer.json": "b68d571997a1f81bf521fb73806740ddb91e4ed6666cb6e996c066bb289cf55b",
    "preprocessor_config.json": "5df7e578c37e907a431daf47fd592fc49fa50d23ed4c41285a0a34a58a9d2e06",
}


# Операция install: данного модуля.
def install(root):
    destination = root / "data/models/clip-vit-base-patch32-onnx"
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"repository": "onnx-community/clip-vit-base-patch32-ONNX",
                "revision": REVISION, "files": {}}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with httpx.Client(timeout=90, trust_env=False, follow_redirects=True) as client:
        # Последовательно обрабатываем элементы: FILES.items().
        for name, expected in FILES.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
            # Обрабатываем ветвь при условии: digest != expected.
            if digest != expected:
                temporary = target.with_suffix(target.suffix + ".part")
                hasher = hashlib.sha256()
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with client.stream("GET", BASE + name) as response:
                        response.raise_for_status()
                        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                        with temporary.open("wb") as handle:
                            # Последовательно обрабатываем элементы: response.iter_bytes(1024 * 1024).
                            for chunk in response.iter_bytes(1024 * 1024):
                                handle.write(chunk)
                                hasher.update(chunk)
                    # Обрабатываем ветвь при условии: hasher.hexdigest() != expected.
                    if hasher.hexdigest() != expected:
                        raise RuntimeError("IMAGE_MODEL_HASH_MISMATCH")
                    os.replace(temporary, target)
                finally:
                    # Завершаем работу и освобождаем ресурсы независимо от результата.
                    temporary.unlink(missing_ok=True)
            manifest["files"][name] = {"sha256": expected, "bytes": target.stat().st_size}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"state": "ready", "model": str(destination), "sha256": FILES["onnx/model_quantized.onnx"]}))


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    install(parser.parse_args().root.resolve())
