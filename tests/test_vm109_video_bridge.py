import base64
import importlib.util
from pathlib import Path

import httpx
import pytest


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "deployment/comfy/nodes_vm109/sdcpp_client.py"
SPEC = importlib.util.spec_from_file_location("vm109_video_client", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


# Проверяем запрос и обрабатываем безопасный результат транспорта.
def request():
    return bridge.make_request("A red sailboat on a blue lake", "watermark", 42, 9, 192, 192, 8, 8)


# Операция completed: данного модуля.
def completed():
    return {"status": "completed", "result": {
        "frame_count": 9,
        "b64_json": base64.b64encode(b"RIFF\x08\x00\x00\x00AVI payload").decode(),
    }}


# Операция client: данного модуля.
def client(handler):
    return httpx.Client(base_url=bridge.BASE_URL, transport=httpx.MockTransport(handler))


# Проверяем сценарий: lost submission is not repeated.
def test_lost_submission_is_not_repeated(tmp_path):
    posts = []

    # Операция handler: данного модуля.
    def handler(req):
        posts.append(req.method)
        raise httpx.ReadTimeout("lost response", request=req)

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with client(handler) as transport:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(httpx.ReadTimeout):
            bridge.generate(request(), tmp_path, client=transport)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(RuntimeError, match="SUBMIT_UNKNOWN"):
            bridge.generate(request(), tmp_path, client=transport)
    assert posts == ["POST"]


# Проверяем сценарий: saved remote job resumes after process failure.
def test_saved_remote_job_resumes_after_process_failure(tmp_path):
    methods = []

    # Операция interrupted: данного модуля.
    def interrupted(req):
        methods.append(req.method)
        # Обрабатываем ветвь при условии: req.method == 'POST'.
        if req.method == "POST":
            return httpx.Response(202, json={"id": "job_saved"})
        raise RuntimeError("simulated process failure")

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with client(interrupted) as transport:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(RuntimeError, match="simulated"):
            bridge.generate(request(), tmp_path, client=transport)

    # Операция resume: данного модуля.
    def resume(req):
        methods.append(req.method)
        assert req.url.path == "/sdcpp/v1/jobs/job_saved"
        return httpx.Response(200, json=completed())

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with client(resume) as transport:
        artifact = bridge.generate(request(), tmp_path, client=transport)
        assert artifact.read_bytes().startswith(b"RIFF")
        assert bridge.generate(request(), tmp_path, client=transport) == artifact
    assert methods == ["POST", "GET", "GET"]


# Проверяем сценарий: remote expiration does not create another job.
def test_remote_expiration_does_not_create_another_job(tmp_path):
    posts = []

    # Операция handler: данного модуля.
    def handler(req):
        # Обрабатываем ветвь при условии: req.method == 'POST'.
        if req.method == "POST":
            posts.append(req.url.path)
            return httpx.Response(202, json={"id": "job_expired"})
        return httpx.Response(410)

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with client(handler) as transport:
        # Последовательно обрабатываем элементы: range(2).
        for _ in range(2):
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with pytest.raises(RuntimeError, match="JOB_MISSING"):
                bridge.generate(request(), tmp_path, client=transport)
    assert len(posts) == 1


# Проверяем сценарий: changed cached artifact is rejected.
def test_changed_cached_artifact_is_rejected(tmp_path):
    # Операция handler: данного модуля.
    def handler(req):
        return httpx.Response(202, json={"id": "job_ok"}) if req.method == "POST" else httpx.Response(200, json=completed())

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with client(handler) as transport:
        artifact = bridge.generate(request(), tmp_path, client=transport)
        artifact.write_bytes(b"changed")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(RuntimeError, match="ARTIFACT_CHANGED"):
            bridge.generate(request(), tmp_path, client=transport)


# Проверяем сценарий: wan frame grid rounds up without shortening scene.
def test_wan_frame_grid_rounds_up_without_shortening_scene():
    payload = bridge.make_request("sailboat", "", 1, 24, 192, 192, 8, 8)
    assert payload["video_frames"] == 25


# Проверяем сценарий: hardware limits reject invalid request.
@pytest.mark.parametrize("field,value", [("frames", 34), ("width", 193), ("seed", True), ("steps", 0), ("fps", 16)])
def test_hardware_limits_reject_invalid_request(field, value):
    values = dict(text="sailboat", negative="", seed=1, frames=9, width=192, height=192, steps=8, fps=8)
    values[field] = value
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(ValueError):
        bridge.make_request(**values)
