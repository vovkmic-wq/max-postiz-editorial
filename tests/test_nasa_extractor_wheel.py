"""The installed runtime must extract source text without a repository scripts tree."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory):
    directory = tmp_path_factory.mktemp("nasa-wheel")
    source = directory / "build-source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(
        ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"),
    )
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--no-index", "--wheel-dir", str(directory / "wheels"), str(source)],
        capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list((directory / "wheels").glob("max_postiz_agent-*.whl"))
    assert len(wheels) == 1
    site = directory / "installed" / "lib" / "python3.12" / "site-packages"
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel.extractall(site)
    empty = directory / "empty-working-directory"
    empty.mkdir()
    return site, empty, source


def test_installed_wheel_parser_is_independent_and_never_reads_repository(installed_wheel):
    site, empty, build_source = installed_wheel
    program = r'''
import json
from pathlib import Path
import sys
site, repository, build_source = map(Path, sys.argv[1:])
forbidden = [repository / "src", repository / "scripts", build_source]
sys.path[:] = [str(site), *(p for p in sys.path
    if not any(Path(p).resolve().is_relative_to(root.resolve()) for root in forbidden))]
def audit(event, arguments):
    if event == "open" and isinstance(arguments[0], (str, bytes)):
        path = Path(arguments[0].decode() if isinstance(arguments[0], bytes) else arguments[0])
        if (path.name == "fetch_nasa_article.py"
                or any(path.resolve().is_relative_to(root.resolve()) for root in forbidden)):
            raise AssertionError("repository extractor access forbidden")
    if event in {"socket.connect", "subprocess.Popen"}:
        raise AssertionError("extractor must not use network or child processes")
sys.addaudithook(audit)
import max_postiz_agent.server_nasa as nasa
assert Path(nasa.__file__).resolve().is_relative_to(site.resolve())
result = {"errors": [], "installed_module": True, "parsers": []}
for science in (False, True, False):
    try:
        parser = nasa._parser(science_mailto=science)
        assert Path(parser.__file__).resolve().is_relative_to(site.resolve())
        result["parsers"].append(parser)
    except Exception as exc:
        result["errors"].append(type(exc).__name__)
if not result["errors"]:
    from max_postiz_agent.config import AgentError
    old, science, another_old = result["parsers"]
    assert old is not science and old is not another_old
    base = "https://noirlab.edu/public/images/iotw2601a/"
    paragraph = '<p>Full words and <a href="https://doi.org/example">the paper</a>.</p>'
    expected = "Full words and [the paper](https://doi.org/example)."
    assert all(p.narrative_text(paragraph, base) == expected
               for p in (old, science, another_old))
    assert science.narrative_text('<a href="mailto:comms@noirlab.edu">Contact</a>', base) == "[Contact](mailto:comms@noirlab.edu)"
    for parser in (old, another_old):
        try:
            parser._narrative_href("mailto:comms@noirlab.edu", base)
        except AgentError as exc:
            assert exc.code == "ARTICLE_SOURCE_LINK_INVALID"
        else:
            raise AssertionError("science mailto permission leaked to legacy parser")
    try:
        science._narrative_href("javascript:alert(1)", base)
    except AgentError as exc:
        assert exc.code == "ARTICLE_SOURCE_LINK_INVALID"
    else:
        raise AssertionError("unsafe href accepted")
result["parsers"] = len(result["parsers"])
print(json.dumps(result))
'''
    checked = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(site), str(ROOT), str(build_source)],
        cwd=empty, capture_output=True, text=True, timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    result = json.loads(checked.stdout)
    assert result == {"errors": [], "installed_module": True, "parsers": 3}
