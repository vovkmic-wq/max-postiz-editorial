import json

from max_postiz_agent.cli import main


# Проверяем сценарий: cli init is idempotent and secret safe.
def test_cli_init_is_idempotent_and_secret_safe(project, capsys, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "fixture-owner")
    assert main(["--root", str(project), "init"]) == 0
    secret_file = project / ".env.local"
    before = secret_file.read_bytes()
    assert main(["--root", str(project), "init"]) == 0
    assert secret_file.read_bytes() == before
    output = capsys.readouterr().out
    # Последовательно обрабатываем элементы: before.decode().splitlines().
    for line in before.decode().splitlines():
        # Обрабатываем ветвь при условии: '=' in line.
        if "=" in line:
            assert line.split("=", 1)[1] not in output


# Проверяем сценарий: cli doctor and tick.
def test_cli_doctor_and_tick(project, capsys):
    assert main(["--root", str(project), "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"] == "sqlite" and report["production_accepted"] is False
    # Disable registered fixture source so CLI tick stays purely local.
    path = project / "config/sources.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["sources"][0]["enabled"] = False
    path.write_text(json.dumps(data))
    assert main(["--root", str(project), "tick"]) == 0
    assert json.loads(capsys.readouterr().out)["confirmed_publications"] == 0
