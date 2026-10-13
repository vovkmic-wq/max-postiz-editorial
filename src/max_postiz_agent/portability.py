"""Архив исходников для чистой установки на Windows или Linux без локальных секретов."""

import hashlib
import json
import zipfile

from .config import AgentError, inside


# Упаковываем исходники по явному списку без локальных секретов.
def package_project(root, destination):
    # Выбор по разрешённым путям исключает окружение, БД, Chrome и незаявленные файлы.
    paths = []
    for directory in ("src", "schemas", "prompts", "scripts", "tests", "docs", "deployment", "migrations"):
        if not inside(root, directory).exists():
            continue
        paths.extend(path for path in inside(root, directory).rglob("*")
                     if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                     and not any(part.endswith(".egg-info") for part in path.parts)
                     and "research" not in path.parts
                     and not (path.name.startswith(".env") and path.name != ".env.example")
                     and path.suffix not in {".enc", ".key", ".pem", ".p12"})
    paths.extend(root.glob("requirements*.lock"))
    paths.extend(root.glob("config/**/*.example.json"))
    paths.extend(root.glob("config/extraction/*.json"))
    # Последовательно обрабатываем элементы: ('README.md', 'TOPIC.md', 'pyproject.toml', 'package.json', 'package-lock.json', '.gitignore').
    for name in ("README.md", "AGENTS.md", "CHANGELOG.md", "TOPIC.md", "pyproject.toml", "package.json", "package-lock.json", ".gitignore", "alembic.ini", ".env.example", "MANIFEST.in"):
        # Обрабатываем ветвь при условии: inside(root, name).exists().
        if inside(root, name).exists():
            paths.append(inside(root, name))
    # Обрабатываем ветвь при условии: destination.resolve().is_relative_to(root / 'src').
    if destination.resolve().is_relative_to(root / "src"):
        raise AgentError("PACKAGE_DESTINATION_INVALID")
    hashes = {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        # Последовательно обрабатываем элементы: sorted(set(paths)).
        for path in sorted(set(paths)):
            name = path.relative_to(root).as_posix()
            value = path.read_bytes()
            archive.writestr(name, value)
            hashes[name] = hashlib.sha256(value).hexdigest()
        archive.writestr("package-manifest.json", json.dumps({"version": 1, "hashes": hashes}, indent=2))
    return {"state": "packaged", "files": len(hashes), "path": str(destination.resolve()),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
