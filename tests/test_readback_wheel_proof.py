"""Exercise the release proof in a fresh interpreter with installed wheel fixtures."""

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
from zipfile import ZipFile

import pytest


PROOF = Path(__file__).resolve().parents[1] / "scripts" / "readback_wheel_proof.py"

RUN_PROOF = r"""
import json, runpy, sys
sys.path.insert(0, sys.argv[2])
if sys.argv[5]:
    sys.path.insert(0, sys.argv[5])
if sys.argv[6] == 'import-first':
    import max_postiz_agent
proof = runpy.run_path(sys.argv[1])['verify_installed_wheel']
try:
    result = proof(sys.argv[3], sys.argv[4])
except ValueError as error:
    print(json.dumps({'error': str(error)}))
else:
    if sys.argv[6] == 'import-after':
        from max_postiz_agent.browser_worker import VALUE
        result['imported_value'] = VALUE
    print(json.dumps({'result': result}))
"""


def make_release(tmp_path, version="0.15.6"):
    """Create both the independent wheel artifact and an installed package."""
    site = tmp_path / "site"
    site.mkdir()
    wheel = tmp_path / f"max_postiz_agent-{version}-py3-none-any.whl"
    files = {
        "max_postiz_agent/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "max_postiz_agent/browser_worker.py": b"VALUE = 1\n",
        "max_postiz_agent/submodule.py": b"ORIGINAL = True\n",
        f"max_postiz_agent-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: max-postiz-agent\nVersion: {version}\n\n"
        ).encode(),
    }
    with ZipFile(wheel, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
            destination = site / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
    return site, wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def run_proof(site, wheel, digest, *, shadow=None, import_first=False, import_after=False):
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            "-c",
            RUN_PROOF,
            str(PROOF),
            str(site),
            str(wheel),
            digest,
            str(shadow) if shadow else "",
            "import-first" if import_first else "import-after" if import_after else "",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(process.stdout)


@pytest.mark.parametrize("version", ["0.15.6", "42.9.102"])
def test_matching_artifact_and_installation_succeed_for_any_version(tmp_path, version):
    site, wheel, digest = make_release(tmp_path, version)
    proof = run_proof(site, wheel, digest)["result"]
    assert proof["version"] == version
    assert proof["sha256"] == digest
    assert proof["runtime_files_verified"] == 3


def test_changed_wheel_is_rejected_even_if_installed_files_still_match(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("extra.txt", "changed")
    assert run_proof(site, wheel, digest) == {"error": "WHEEL_SHA256_MISMATCH"}


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ("../outside.py", "WHEEL_PATH_INVALID"),
        ("max_postiz_agent/browser_worker.py", "WHEEL_DUPLICATE_ENTRY"),
    ],
)
def test_even_pinned_wheel_with_ambiguous_archive_paths_is_rejected(tmp_path, entry, error):
    site, wheel, _ = make_release(tmp_path)
    with ZipFile(wheel, "a") as archive:
        with pytest.warns(UserWarning) if entry.endswith("browser_worker.py") else nullcontext():
            archive.writestr(entry, b"ambiguous")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert run_proof(site, wheel, digest) == {"error": error}


def test_changed_installed_file_is_rejected(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    (site / "max_postiz_agent/submodule.py").write_bytes(b"ORIGINAL = False\n")
    assert run_proof(site, wheel, digest) == {"error": "INSTALLED_PACKAGE_BYTES_MISMATCH"}


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_installed_file_set_must_exactly_match(tmp_path, change):
    site, wheel, digest = make_release(tmp_path)
    package = site / "max_postiz_agent"
    if change == "missing":
        (package / "submodule.py").unlink()
    else:
        (package / "unexpected.py").write_bytes(b"unexpected = True\n")
    assert run_proof(site, wheel, digest) == {"error": "INSTALLED_PACKAGE_FILES_MISMATCH"}


def test_wheel_version_must_match_installed_distribution(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    metadata = next(site.glob("*.dist-info/METADATA"))
    metadata.write_text("Metadata-Version: 2.1\nName: max-postiz-agent\nVersion: 99.0\n\n")
    assert run_proof(site, wheel, digest) == {"error": "INSTALLED_DISTRIBUTION_MISMATCH"}


def test_noncanonical_wheel_path_is_rejected(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    noncanonical = wheel.parent / "site" / ".." / wheel.name
    assert run_proof(site, noncanonical, digest) == {"error": "WHEEL_PATH_INVALID"}


def test_other_package_cannot_shadow_verified_installation(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    shadow = tmp_path / "shadow"
    (shadow / "max_postiz_agent").mkdir(parents=True)
    (shadow / "max_postiz_agent/__init__.py").write_text("SHADOWED = True\n")
    assert run_proof(site, wheel, digest, shadow=shadow) == {"error": "INSTALLED_PACKAGE_SHADOWED"}


def test_package_must_not_have_been_imported_before_proof(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    assert run_proof(site, wheel, digest, import_first=True) == {
        "error": "APPLICATION_IMPORTED_BEFORE_WHEEL_PROOF"
    }


def test_proof_prevents_loading_forged_valid_timestamp_bytecode(tmp_path):
    site, wheel, digest = make_release(tmp_path)
    source = site / "max_postiz_agent/browser_worker.py"
    original = source.read_bytes()
    original_stat = source.stat()
    source.write_bytes(b"VALUE = 9\n")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    py_compile.compile(str(source), doraise=True)
    source.write_bytes(original)
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    baseline = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "from max_postiz_agent.browser_worker import VALUE; print(VALUE)", str(site)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert baseline.stdout.strip() == "9", "fixture must demonstrate the forged cache is valid"
    assert run_proof(site, wheel, digest, import_after=True)["result"]["imported_value"] == 1
