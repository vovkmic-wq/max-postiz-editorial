"""Verify installed application bytes against an independently pinned wheel SHA256.

This module deliberately imports no application code before verification.  The
caller must itself be trusted and must obtain the expected digest independently
from the deployment claim; a checksum next to a wheel is not authentication.
"""
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import io
import re
import stat
import sys
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def verify_installed_wheel(wheel_path, expected_sha256):
    """Return a code proof or raise ValueError with a stable failure code."""
    path = Path(wheel_path)
    _require(path.is_absolute() and path.resolve() == path and path.is_file()
             and not path.is_symlink(), "WHEEL_PATH_INVALID")
    _require(re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is not None,
             "WHEEL_SHA256_INVALID")
    _require(path.stat().st_size <= 100 * 1024 * 1024, "WHEEL_TOO_LARGE")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _require(digest == expected_sha256, "WHEEL_SHA256_MISMATCH")
    _require(not any(name == "max_postiz_agent" or name.startswith("max_postiz_agent.")
                     for name in sys.modules), "APPLICATION_IMPORTED_BEFORE_WHEEL_PROOF")
    with ZipFile(io.BytesIO(raw)) as wheel:
        entries = wheel.infolist()
        names = [entry.filename for entry in entries]
        _require(len(names) == len(set(names)), "WHEEL_DUPLICATE_ENTRY")
        _require(all(not name.startswith("/") and "\\" not in name
                     and ".." not in PurePosixPath(name).parts for name in names),
                 "WHEEL_PATH_INVALID")
        package = {}
        for entry in entries:
            name = entry.filename
            if name.startswith("max_postiz_agent/") and not entry.is_dir():
                mode = entry.external_attr >> 16
                _require(not stat.S_ISLNK(mode) and entry.file_size <= 20 * 1024 * 1024,
                         "WHEEL_PACKAGE_ENTRY_INVALID")
                package[name.removeprefix("max_postiz_agent/")] = wheel.read(entry)
        _require("__init__.py" in package and "browser_worker.py" in package,
                 "WHEEL_PACKAGE_INCOMPLETE")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        _require(len(metadata) == 1, "WHEEL_METADATA_INVALID")
        metadata = BytesParser().parsebytes(wheel.read(metadata[0]))
        _require(metadata.get("Name", "").lower().replace("_", "-") == "max-postiz-agent"
                 and metadata.get("Version"), "WHEEL_METADATA_INVALID")
        version = metadata["Version"]
    distributions = list(importlib.metadata.distributions(name="max-postiz-agent"))
    _require(len(distributions) == 1 and distributions[0].version == version,
             "INSTALLED_DISTRIBUTION_MISMATCH")
    distribution = distributions[0]
    root = Path(distribution.locate_file("max_postiz_agent"))
    _require(root.is_absolute() and root.resolve() == root and root.is_dir() and not root.is_symlink(),
             "INSTALLED_PACKAGE_PATH_INVALID")
    spec = importlib.machinery.PathFinder.find_spec("max_postiz_agent", sys.path)
    selected = importlib.util.find_spec("max_postiz_agent")
    _require(spec is not None and spec.origin is not None and selected is not None
             and selected.origin == spec.origin
             and Path(spec.origin).resolve() == root / "__init__.py",
             "INSTALLED_PACKAGE_SHADOWED")
    actual = {}
    for item in root.rglob("*"):
        _require(not item.is_symlink(), "INSTALLED_PACKAGE_SYMLINK")
        if item.is_file() and "__pycache__" not in item.relative_to(root).parts:
            actual[item.relative_to(root).as_posix()] = item.read_bytes()
    _require(actual.keys() == package.keys(), "INSTALLED_PACKAGE_FILES_MISMATCH")
    _require(all(actual[name] == data for name, data in package.items()),
             "INSTALLED_PACKAGE_BYTES_MISMATCH")
    file_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in package.items()}
    combined = hashlib.sha256("\n".join(name + " " + file_hashes[name]
                                        for name in sorted(file_hashes)).encode()).hexdigest()
    # Python -B suppresses bytecode writes, not reads. Force an absent cache
    # prefix so verified source, rather than pre-existing .pyc files, is loaded.
    # A wheel is a regular file, so no bytecode cache can exist underneath it.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(path)
    return {"wheel": str(path), "sha256": digest, "version": version,
            "runtime_files_verified": len(package), "runtime_files_hash": combined,
            "scope": "installed_max_postiz_agent_package_only"}
