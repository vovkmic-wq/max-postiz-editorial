"""Durable handoff to the fixed VM 109 native video API; never retry a lost POST."""

import base64
import hashlib
import json
import time
from pathlib import Path

import httpx


BASE_URL = "http://10.20.30.95:8190"


# Выполняем операцию make_request в рамках данного модуля.
def make_request(text, negative, seed, frames, width, height, steps, fps):
    # Обрабатываем ветвь при условии: not isinstance(text, str) or not 1 <= len(text) <= 4000.
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        raise ValueError("VIDEO_PROMPT_INVALID")
    # Обрабатываем ветвь при условии: not isinstance(negative, str) or len(negative) > 4000.
    if not isinstance(negative, str) or len(negative) > 4000:
        raise ValueError("VIDEO_NEGATIVE_INVALID")
    bounds = {"seed": (seed, 0, 2**63 - 1), "frames": (frames, 1, 33),
              "width": (width, 128, 256), "height": (height, 128, 256),
              "steps": (steps, 1, 30), "fps": (fps, 8, 8)}
    # Обрабатываем ветвь при условии: any((isinstance(v, bool) or not isinstance(v, int) or (not lo <= v <= hi) for v, lo, hi in bounds.values())).
    if any(isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi
           for v, lo, hi in bounds.values()):
        raise ValueError("VIDEO_SIZE_OR_DURATION_INVALID")
    # Обрабатываем ветвь при условии: width % 16 or height % 16.
    if width % 16 or height % 16:
        raise ValueError("VIDEO_DIMENSIONS_INVALID")
    # Wan uses 4*n+1 frames. Round upward so the agent can trim to its scene duration.
    frames = max(9, ((frames - 1 + 3) // 4) * 4 + 1)
    return {
        "prompt": text, "negative_prompt": negative, "seed": seed,
        "width": width, "height": height, "video_frames": frames, "fps": fps,
        "output_format": "avi", "preview": "none",
        "sample_params": {"sample_method": "euler", "sample_steps": steps,
                          "flow_shift": 3.0, "guidance": {"txt_cfg": 5.0}},
        "vae_tiling_params": {"enabled": True, "temporal_tiling": True,
                              "tile_size_w": 128, "tile_size_h": 128},
    }


# Выполняем операцию _save в рамках данного модуля.
def _save(path, state):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


# Выполняем операцию generate в рамках данного модуля.
def generate(request, directory, *, client=None, timeout=3600, interval=2):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(
        {"server": BASE_URL, "request": request, "bridge_version": 1}, sort_keys=True
    ).encode()).hexdigest()
    ledger, artifact = directory / (key + ".json"), directory / (key + ".avi")
    initial = {"state": "submitting", "request_sha256": key}
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with ledger.open("x", encoding="utf-8") as handle:
            json.dump(initial, handle)
        fresh = True
    # Обрабатываем FileExistsError и выбираем допустимый исход.
    except FileExistsError:
        fresh = False
    state = json.loads(ledger.read_text(encoding="utf-8"))
    # Обрабатываем ветвь при условии: state.get('state') == 'completed' and artifact.is_file().
    if state.get("state") == "completed" and artifact.is_file():
        # Обрабатываем ветвь при условии: hashlib.sha256(artifact.read_bytes()).hexdigest() != state.get('sha256').
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != state.get("sha256"):
            raise RuntimeError("VIDEO_CACHED_ARTIFACT_CHANGED")
        return artifact
    # Обрабатываем ветвь при условии: not fresh and (not state.get('remote_id')).
    if not fresh and not state.get("remote_id"):
        raise RuntimeError("VIDEO_REMOTE_SUBMIT_UNKNOWN_DO_NOT_RESUBMIT")
    owns_client = client is None
    client = client or httpx.Client(base_url=BASE_URL, trust_env=False, timeout=30)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Обрабатываем ветвь при условии: fresh.
        if fresh:
            response = client.post("/sdcpp/v1/vid_gen", json=request)
            response.raise_for_status()
            remote_id = response.json()["id"]
            # Обрабатываем ветвь при условии: not isinstance(remote_id, str) or not remote_id.startswith('job_').
            if not isinstance(remote_id, str) or not remote_id.startswith("job_"):
                raise RuntimeError("VIDEO_REMOTE_RESPONSE_INVALID")
            state.update(state="submitted", remote_id=remote_id)
            _save(ledger, state)
        deadline = time.monotonic() + timeout
        # Продолжаем цикл, пока выполняется условие: time.monotonic() < deadline.
        while time.monotonic() < deadline:
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                response = client.get("/sdcpp/v1/jobs/" + state["remote_id"])
            # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
            except httpx.HTTPError:
                time.sleep(interval)
                continue
            # Обрабатываем ветвь при условии: response.status_code in {404, 410}.
            if response.status_code in {404, 410}:
                raise RuntimeError("VIDEO_REMOTE_JOB_MISSING_DO_NOT_RESUBMIT")
            response.raise_for_status()
            job = response.json()
            # Обрабатываем ветвь при условии: job['status'] in {'failed', 'cancelled'}.
            if job["status"] in {"failed", "cancelled"}:
                state.update(state="failed", error=job["status"])
                _save(ledger, state)
                raise RuntimeError("VIDEO_REMOTE_GENERATION_FAILED")
            # Обрабатываем ветвь при условии: job['status'] == 'completed'.
            if job["status"] == "completed":
                result = job["result"]
                # Обрабатываем ветвь при условии: result.get('frame_count') != request['video_frames'].
                if result.get("frame_count") != request["video_frames"]:
                    raise RuntimeError("VIDEO_REMOTE_FRAME_COUNT_INVALID")
                encoded = result.get("b64_json", "")
                # Обрабатываем ветвь при условии: len(encoded) > 70000000.
                if len(encoded) > 70_000_000:
                    raise RuntimeError("VIDEO_REMOTE_RESULT_TOO_LARGE")
                data = base64.b64decode(encoded, validate=True)
                # Обрабатываем ветвь при условии: not data.startswith(b'RIFF') or data[8:12] != b'AVI '.
                if not data.startswith(b"RIFF") or data[8:12] != b"AVI ":
                    raise RuntimeError("VIDEO_REMOTE_FORMAT_INVALID")
                partial = artifact.with_suffix(".partial")
                partial.write_bytes(data)
                partial.replace(artifact)
                state.update(state="completed", sha256=hashlib.sha256(data).hexdigest())
                _save(ledger, state)
                return artifact
            time.sleep(interval)
        raise RuntimeError("VIDEO_REMOTE_TIMEOUT_CONTINUE_USING_SAVED_JOB")
    finally:
        # Обрабатываем ветвь при условии: owns_client.
        if owns_client:
            client.close()
