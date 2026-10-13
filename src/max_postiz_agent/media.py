# Проверка файлов изображений и видео перед использованием.
"""Owned short-video assets. Generation backends are explicit, never simulated as AI output."""

import hashlib
import re
import shutil
import subprocess


from .config import AgentError, inside
from .db import now


# Сверяем формат, размер и хеш локального изображения.
def inspect_image(root, relative_path, max_bytes=10485760):
    from PIL import Image, UnidentifiedImageError

    path = inside(root, relative_path)
    # Обрабатываем ветвь при условии: path.suffix.lower() not in {'.jpg', '.jpeg', '.png'} or not path.is_file().
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"} or not path.is_file():
        raise AgentError("IMAGE_FORMAT_INVALID")
    size = path.stat().st_size
    # Обрабатываем ветвь при условии: not 0 < size <= max_bytes.
    if not 0 < size <= max_bytes:
        raise AgentError("IMAGE_SIZE_INVALID")
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with Image.open(path) as image:
            # Обрабатываем ветвь при условии: image.format not in {'JPEG', 'PNG'} or image.width * image.height > 40000000.
            if image.format not in {"JPEG", "PNG"} or image.width * image.height > 40_000_000:
                raise AgentError("IMAGE_FORMAT_INVALID")
            image.verify()
    # Обрабатываем (OSError, UnidentifiedImageError) и выбираем допустимый исход.
    except (OSError, UnidentifiedImageError) as exc:
        raise AgentError("IMAGE_FORMAT_INVALID") from exc
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": size}


# Проверяем контейнер, дорожки и длительность локального ролика.
def inspect_video(root, relative_path, duration_limit=60, max_bytes=104857600):
    path = inside(root, relative_path)
    # Обрабатываем ветвь при условии: path.suffix.lower() != '.mp4' or not path.is_file().
    if path.suffix.lower() != ".mp4" or not path.is_file():
        raise AgentError("VIDEO_MP4_REQUIRED")
    # Обрабатываем ветвь при условии: path.stat().st_size > max_bytes.
    if path.stat().st_size > max_bytes:
        raise AgentError("VIDEO_TOO_LARGE")
    import imageio_ffmpeg

    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Fixed executable and argument array; no shell interpolation.
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    # Обрабатываем (OSError, subprocess.TimeoutExpired) и выбираем допустимый исход.
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError("VIDEO_INSPECTION_FAILED") from exc
    output = result.stderr.decode("utf-8", errors="replace")
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    # Обрабатываем ветвь при условии: not duration_match or 'Video:' not in output.
    if not duration_match or "Video:" not in output:
        raise AgentError("VIDEO_STREAM_INVALID")
    hours, minutes, seconds = map(float, duration_match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    # Обрабатываем ветвь при условии: not 0 < duration <= duration_limit.
    if not 0 < duration <= duration_limit:
        raise AgentError("VIDEO_DURATION_EXCEEDED")
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "duration_seconds": duration,
        "bytes": path.stat().st_size,
        "format": "mp4",
        "has_audio": "Audio:" in output,
    }


# Прикрепляем проверенный ролик к ревизии черновика.
def attach_video(service, draft_id, relative_path, rights_basis, actor="owner"):
    from sqlalchemy import select, update
    from .config import fingerprint
    from .db import Approval, Draft, DraftRevision, Intent

    # Обрабатываем ветвь при условии: not isinstance(rights_basis, str) or len(rights_basis.strip()) < 10.
    if not isinstance(rights_basis, str) or len(rights_basis.strip()) < 10:
        raise AgentError("VIDEO_RIGHTS_REQUIRED")
    options = service.settings.app.get("video", {})
    info = inspect_video(
        service.settings.root,
        relative_path,
        options.get("max_duration_seconds", 60),
        options.get("max_bytes", 104857600),
    )
    media_dir = inside(service.settings.root, service.settings.app["media_dir"])
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / (info["sha256"] + ".mp4")
    # Обрабатываем ветвь при условии: not target.exists().
    if not target.exists():
        shutil.copyfile(inside(service.settings.root, relative_path), target)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as session:
        draft = session.get(Draft, draft_id)
        # Обрабатываем ветвь при условии: not draft or draft.state == 'published' or session.scalar(select(Intent).where(Intent.draft_id == draft_id)).
        if (
            not draft
            or draft.state == "published"
            or session.scalar(select(Intent).where(Intent.draft_id == draft_id))
        ):
            raise AgentError("DRAFT_STATE_CONFLICT", 409)
        payload = dict(draft.current)
        payload["video"] = {
            **info,
            "path": target.relative_to(service.settings.root).as_posix(),
            "rights_basis": rights_basis,
            "added_at": now(),
        }
        draft.revision += 1
        draft.current, draft.state, draft.scheduled_at = payload, "review_pending", None
        session.execute(update(Approval).where(Approval.draft_id == draft_id).values(revoked=True))
        session.add(
            DraftRevision(
                draft_id=draft_id,
                revision=draft.revision,
                payload=payload,
                config_id=draft.config_id,
            )
        )
        service.db.audit(session, actor, "attach_video", draft_id, sha256=info["sha256"])
        return {**info, "revision": draft.revision, "content_hash": fingerprint(payload)}
