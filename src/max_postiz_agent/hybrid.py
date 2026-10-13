# Сценарии видео, бюджет генерации, сборка и проверка материалов.
"""Hybrid channel: original storyboard -> scenes -> montage -> human review."""

import hashlib
import math
import threading

from sqlalchemy import func, select, update

from .comfy import ComfyUI
from .config import AgentError, fingerprint
from .db import (
    Budget,
    Channel,
    Draft,
    DraftRevision,
    Story,
    VideoAsset,
    VideoProject,
    VideoScene,
    Material,
    MaterialMedia,
    now,
    serialize,
)
from .video_tools import quality_check, render_montage, visually_similar


# Проверяем сценарий видео и ограничения генерации.
def validate_plan(plan, max_duration=60):
    # Обрабатываем ветвь при условии: not isinstance(plan, dict) or set(plan) != {'title', 'caption', 'rights_basis', 'scenes'}.
    if not isinstance(plan, dict) or set(plan) != {"title", "caption", "rights_basis", "scenes"}:
        raise AgentError("VIDEO_PLAN_SCHEMA_INVALID")
    # Последовательно обрабатываем элементы: ('title', 'caption', 'rights_basis').
    for key in ("title", "caption", "rights_basis"):
        # Обрабатываем ветвь при условии: not isinstance(plan[key], str) or not 1 <= len(plan[key]) <= 3500.
        if not isinstance(plan[key], str) or not 1 <= len(plan[key]) <= 3500:
            raise AgentError("VIDEO_PLAN_TEXT_INVALID")
    # Обрабатываем ветвь при условии: len(plan['rights_basis']) < 10.
    if len(plan["rights_basis"]) < 10:
        raise AgentError("VIDEO_RIGHTS_REQUIRED")
    scenes = plan["scenes"]
    # Обрабатываем ветвь при условии: not isinstance(scenes, list) or not 1 <= len(scenes) <= 12.
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 12:
        raise AgentError("SCENE_LIST_INVALID")
    total = 0
    # Последовательно обрабатываем элементы: scenes.
    for scene in scenes:
        # Обрабатываем ветвь при условии: not isinstance(scene, dict) or set(scene) != {'prompt', 'negative_prompt', 'duration_seconds'}.
        if not isinstance(scene, dict) or set(scene) != {
            "prompt",
            "negative_prompt",
            "duration_seconds",
        }:
            raise AgentError("SCENE_SCHEMA_INVALID")
        # Обрабатываем ветвь при условии: not isinstance(scene['prompt'], str) or not 1 <= len(scene['prompt']) <= 4000 or (not isinstance(scene['neg….
        if (
            not isinstance(scene["prompt"], str)
            or not 1 <= len(scene["prompt"]) <= 4000
            or not isinstance(scene["negative_prompt"], str)
            or len(scene["negative_prompt"]) > 4000
        ):
            raise AgentError("SCENE_PROMPT_INVALID")
        duration = scene["duration_seconds"]
        # Обрабатываем ветвь при условии: isinstance(duration, bool) or not isinstance(duration, (int, float)) or (not math.isfinite(duration)) or (n….
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 0.25 <= duration <= max_duration
        ):
            raise AgentError("SCENE_DURATION_INVALID")
        total += duration
    # Обрабатываем ветвь при условии: total > max_duration.
    if total > max_duration:
        raise AgentError("VIDEO_DURATION_EXCEEDED")
    return plan


# Структура Hybrid объединяет связанные данные и операции модуля.
class Hybrid:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, service, backend=None):
        self.service = service
        self.db, self.settings = service.db, service.settings
        self.backend = backend or ComfyUI(self.settings)
        self.guard = threading.Lock()

    # Операция create: сценарии видео, бюджет генерации, сборка и проверка материалов.
    def create(self, channel_id, plan, actor="owner"):
        channel = self.service.settings.channel(channel_id)
        validate_plan(plan, self.options.get("max_duration_seconds", 60))
        # Обрабатываем ветвь при условии: any((t.casefold() in (plan['caption'] + plan['title']).casefold() for t in channel['exclude_topics'])).
        if any(
            t.casefold() in (plan["caption"] + plan["title"]).casefold()
            for t in channel["exclude_topics"]
        ):
            raise AgentError("EXCLUDED_TOPIC")
        from .collectors import detect_language

        # Обрабатываем ветвь при условии: detect_language(plan['caption']) != 'ru'.
        if detect_language(plan["caption"]) != "ru":
            raise AgentError("OUTPUT_NOT_RUSSIAN")
        # Обрабатываем ветвь при условии: len(plan['caption']) > channel['publication']['max_chars'].
        if len(plan["caption"]) > channel["publication"]["max_chars"]:
            raise AgentError("VIDEO_CAPTION_INVALID")
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            project = VideoProject(
                channel_id=channel_id, plan=plan, config_id=self.service.settings.hash
            )
            session.add(project)
            session.flush()
            # Последовательно обрабатываем элементы: range(len(plan['scenes'])).
            for index in range(len(plan["scenes"])):
                session.add(VideoScene(project_id=project.id, index=index))
            self.db.audit(session, actor, "video_plan_created", project.id)
            return serialize(project)

    # Операция options: сценарии видео, бюджет генерации, сборка и проверка материалов.
    @property
    def options(self):
        return self.service.settings.app.get("video", {})

    # Создаём сценарий видео по идее и профилю канала.
    def generate_plan(self, channel_id, idea, actor="owner"):
        # Обрабатываем ветвь при условии: not isinstance(idea, str) or not 1 <= len(idea) <= 2000.
        if not isinstance(idea, str) or not 1 <= len(idea) <= 2000:
            raise AgentError("VIDEO_IDEA_INVALID")
        plan = self.service.model_task(
            "video_plan",
            channel_id,
            {
                "channel": self.service.settings.channel(channel_id),
                "idea": idea,
                "max_duration_seconds": self.options.get("max_duration_seconds", 60),
            },
        )
        return self.create(channel_id, plan, actor)

    # Операция curate: сценарии видео, бюджет генерации, сборка и проверка материалов.
    def curate(self, channel_id, media_id, caption, actor="owner"):
        from .editorial import validate_draft
        from .network import SafeHTTP
        from .config import inside

        channel = self.service.settings.channel(channel_id)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            media = session.get(MaterialMedia, media_id)
            # Обрабатываем ветвь при условии: not media.
            if not media:
                raise AgentError("SOURCE_MEDIA_NOT_FOUND", 404)
            material = session.get(Material, media.material_id)
            source = self.service.settings.source(material.source_id)
        # Обрабатываем ветвь при условии: not source['enabled'] or channel_id not in source['channel_ids'] or source.get('purpose', 'ideas') not in {….
        if (
            not source["enabled"]
            or channel_id not in source["channel_ids"]
            or source.get("purpose", "ideas") not in {"videos", "mixed"}
        ):
            raise AgentError("SOURCE_NOT_VIDEO_POOL")
        rights = source["rights"]
        # Обрабатываем ветвь при условии: not all((rights.get(key) for key in ('verified', 'allow_storage', 'allow_media', 'allow_full_repost'))) or ….
        if (
            not all(
                rights.get(key)
                for key in ("verified", "allow_storage", "allow_media", "allow_full_repost")
            )
            or not channel["quality"]["allow_full_reposts"]
        ):
            raise AgentError("VIDEO_REPOST_NOT_ALLOWED")
        # Обрабатываем ветвь при условии: not isinstance(caption, str) or not caption.strip().
        if not isinstance(caption, str) or not caption.strip():
            raise AgentError("VIDEO_CAPTION_INVALID")
        body = caption + "\n\nИсточник: " + material.url
        payload = {
            "title": material.title,
            "body": body,
            "language": "ru",
            "source_ids": [material.id],
            "decision": "review",
            "reason_codes": ["CURATED_VIDEO"],
            "claims": [],
            "confidence": 0,
            "payload": {"origin": "curated", "media_id": media_id},
        }
        checks = validate_draft(payload, [serialize(material)], channel)
        # Обрабатываем ветвь при условии: set(checks) - {'EDITOR_REVIEW_REQUIRED'}.
        if set(checks) - {"EDITOR_REVIEW_REQUIRED"}:
            raise AgentError("QUALITY_GATE_BLOCKED")
        http = SafeHTTP(max_bytes=self.options.get("max_bytes", 104857600))
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            _, content, _, _ = http.get(media.url)
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            http.close()
        digest = hashlib.sha256(content).hexdigest()
        relative = self.settings.app["media_dir"] + "/" + digest + ".mp4"
        path = inside(self.settings.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        qa = quality_check(self.settings.root, relative, self.options)
        payload["checks"] = sorted(set(checks + qa["flags"]))
        payload["video"] = {
            **qa,
            "path": relative,
            "rights_basis": rights["evidence_ref"],
            "added_at": now(),
        }
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            # File SHA identity prevents the same video from being curated twice under new URLs.
            story_hash = fingerprint("video:" + qa["sha256"])
            story = session.scalar(select(Story).where(Story.text_hash == story_hash))
            # Обрабатываем ветвь при условии: not story.
            if not story:
                story = Story(text_hash=story_hash, title=material.title, text=material.text)
                session.add(story)
                session.flush()
            existing = session.scalar(
                select(Draft).where(Draft.channel_id == channel_id, Draft.story_id == story.id)
            )
            # Обрабатываем ветвь при условии: existing.
            if existing:
                return {"draft_id": existing.id, "duplicate": True}
            # Keep the material linked to the canonical video story for future revision checks.
            session.get(Material, material.id).story_id = story.id
            draft = Draft(
                channel_id=channel_id,
                story_id=story.id,
                current=payload,
                config_id=self.service.settings.hash,
            )
            session.add(draft)
            session.flush()
            session.add(
                DraftRevision(
                    draft_id=draft.id, revision=1, payload=payload, config_id=draft.config_id
                )
            )
            # Обрабатываем ветвь при условии: not session.scalar(select(VideoAsset).where(VideoAsset.sha256 == qa['sha256'])).
            if not session.scalar(select(VideoAsset).where(VideoAsset.sha256 == qa["sha256"])):
                session.add(
                    VideoAsset(
                        sha256=qa["sha256"],
                        path=relative,
                        frames=qa["frames"],
                        quality=qa,
                        provenance={"source_id": material.source_id, "material_id": material.id},
                    )
                )
            self.db.audit(session, actor, "video_curated", draft.id)
            return {"draft_id": draft.id, "duplicate": False}

    # Согласуем генерацию текущей версии сценария.
    def approve_generation(self, project_id, revision, plan_hash, actor="owner"):
        self.service.refresh()
        self.backend.configuration()  # Fail before changing state if model/workflow are absent.
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            project = session.scalar(
                select(VideoProject).where(VideoProject.id == project_id).with_for_update()
            )
            # Обрабатываем ветвь при условии: not project or project.revision != revision or fingerprint(project.plan) != plan_hash.
            if (
                not project
                or project.revision != revision
                or fingerprint(project.plan) != plan_hash
            ):
                raise AgentError("VIDEO_PLAN_REVISION_CONFLICT", 409)
            # Обрабатываем ветвь при условии: project.config_id != self.service.settings.hash or self.service.config_error.
            if project.config_id != self.service.settings.hash or self.service.config_error:
                raise AgentError("VIDEO_PLAN_STALE", 409)
            # Обрабатываем ветвь при условии: project.state != 'planning'.
            if project.state != "planning":
                raise AgentError("VIDEO_PROJECT_STATE_CONFLICT", 409)
            project.approved_hash, project.state = (
                fingerprint({"plan": project.plan, "backend": self.backend.binding_hash}),
                "queued",
            )
            self.db.audit(session, actor, "video_generation_approved", project_id)

    # Проверяем готовую сцену и основание права на её использование.
    def import_scene(self, project_id, index, relative, rights_basis, actor="owner"):
        # Обрабатываем ветвь при условии: not isinstance(rights_basis, str) or len(rights_basis) < 10.
        if not isinstance(rights_basis, str) or len(rights_basis) < 10:
            raise AgentError("VIDEO_RIGHTS_REQUIRED")
        qa = quality_check(self.settings.root, relative, self.options)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            project = session.get(VideoProject, project_id)
            scene = session.scalar(
                select(VideoScene).where(
                    VideoScene.project_id == project_id, VideoScene.index == index
                )
            )
            # Обрабатываем ветвь при условии: not project or not scene or project.state not in {'planning', 'queued', 'generating', 'generation_failed'} ….
            if (
                not project
                or not scene
                or project.state not in {"planning", "queued", "generating", "generation_failed"}
                or scene.state not in {"pending", "failed", "timed_out"}
            ):
                raise AgentError("VIDEO_SCENE_STATE_CONFLICT", 409)
            # Обрабатываем ветвь при условии: qa['duration_seconds'] + 0.1 < project.plan['scenes'][index]['duration_seconds'].
            if qa["duration_seconds"] + 0.1 < project.plan["scenes"][index]["duration_seconds"]:
                raise AgentError("SCENE_TOO_SHORT")
            # Store a content-addressed copy rather than depend on a mutable incoming path.
            import shutil
            from .config import inside

            target = inside(
                self.settings.root, self.settings.app["media_dir"] + "/" + qa["sha256"] + ".mp4"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            # Обрабатываем ветвь при условии: not target.exists().
            if not target.exists():
                shutil.copyfile(inside(self.settings.root, relative), target)
            scene.artifact = {
                "path": target.relative_to(self.settings.root).as_posix(),
                "quality": qa,
                "origin": "imported",
                "rights_basis": rights_basis,
            }
            scene.state = "completed"
            # Обрабатываем ветвь при условии: project.state == 'generation_failed'.
            if project.state == "generation_failed":
                project.state, project.error = "generating", None
            self.db.audit(session, actor, "video_scene_imported", scene.id, sha256=qa["sha256"])
        return qa

    # Сохраняем состояние задачи перед запросом генерации.
    def _submit(self, project, scene):
        options = self.options
        free = options.get("local_zero_cost") is True
        quote = 0 if free else options.get("max_scene_cost_rub")
        # Обрабатываем ветвь при условии: quote is None or isinstance(quote, bool) or (not isinstance(quote, (int, float))) or (not math.isfinite(quo….
        if (
            quote is None
            or isinstance(quote, bool)
            or not isinstance(quote, (int, float))
            or not math.isfinite(quote)
            or quote < 0
        ):
            raise AgentError("VIDEO_PRICE_UNKNOWN")
        self.backend.configuration()
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            session.execute(
                update(Channel)
                .where(Channel.id == project.channel_id)
                .values(paused=Channel.paused)
            )
            row = session.get(VideoScene, scene.id)
            # Обрабатываем ветвь при условии: row.state != 'pending'.
            if row.state != "pending":
                return
            # Both a per-project cap and a channel-wide daily cap include unresolved reservations.
            reserved = session.scalar(
                select(func.coalesce(func.sum(Budget.amount), 0)).where(
                    Budget.action_key.like("video:" + project.id + ":%")
                )
            )
            daily = session.scalar(
                select(func.coalesce(func.sum(Budget.amount), 0)).where(
                    Budget.action_key.like("video:%"),
                    Budget.channel_id == project.channel_id,
                    Budget.created_at >= now() - 86400,
                )
            )
            # Обрабатываем ветвь при условии: reserved + quote > options.get('max_project_cost_rub', 0) or daily + quote > options.get('daily_generation_….
            if reserved + quote > options.get(
                "max_project_cost_rub", 0
            ) or daily + quote > options.get("daily_generation_limit_rub", 0):
                raise AgentError("VIDEO_BUDGET_EXCEEDED")
            session.add(
                Budget(
                    channel_id=project.channel_id,
                    action_key=f"video:{project.id}:{scene.id}",
                    amount=quote,
                )
            )
            row.state, row.started_at, row.backend_hash = (
                "submitting",
                now(),
                self.backend.binding_hash,
            )
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            remote_id = self.backend.submit(
                project.plan["scenes"][scene.index],
                scene.request_id,
                int(hashlib.sha256(scene.request_id.encode()).hexdigest()[:8], 16),
            )
            state, error = "running", None
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as exc:
            remote_id, error = None, exc.code
            state = "failed" if error == "VIDEO_WORKFLOW_REJECTED" else "submit_unknown"
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            row = session.get(VideoScene, scene.id)
            row.state, row.remote_id, row.error = state, remote_id, error

    # Обновляем состояние сцен существующего видеопроекта.
    def poll_project(self, project_id):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            project = session.get(VideoProject, project_id)
            # Обрабатываем ветвь при условии: not project or project.state not in {'queued', 'generating'}.
            if not project or project.state not in {"queued", "generating"}:
                return
            scenes = session.scalars(
                select(VideoScene)
                .where(VideoScene.project_id == project_id)
                .order_by(VideoScene.index)
            ).all()
        # Обрабатываем ветвь при условии: project.config_id != self.service.settings.hash or self.service.config_error.
        if project.config_id != self.service.settings.hash or self.service.config_error:
            raise AgentError("VIDEO_PLAN_STALE", 409)
        # Обрабатываем ветвь при условии: project.approved_hash != fingerprint({'plan': project.plan, 'backend': self.backend.binding_hash}).
        if project.approved_hash != fingerprint(
            {"plan": project.plan, "backend": self.backend.binding_hash}
        ):
            raise AgentError("VIDEO_PLAN_NOT_APPROVED")
        # Последовательно обрабатываем элементы: scenes.
        for scene in scenes:
            # Обрабатываем ветвь при условии: scene.state == 'running' and scene.started_at and (now() - scene.started_at > self.options.get('task_timeou….
            if (
                scene.state == "running"
                and scene.started_at
                and now() - scene.started_at > self.options.get("task_timeout_seconds", 1800)
            ):
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with self.db.session.begin() as session:
                    row = session.get(VideoScene, scene.id)
                    row.state, row.error = "timed_out", "VIDEO_GENERATION_TIMEOUT"
                continue
            # Обрабатываем ветвь при условии: scene.state == 'pending'.
            if scene.state == "pending":
                self._submit(project, scene)
                break
            # Обрабатываем ветвь при условии: scene.state in {'submitting', 'submit_unknown', 'running'}.
            if scene.state in {"submitting", "submit_unknown", "running"}:
                # Обрабатываем ветвь при условии: scene.backend_hash != self.backend.binding_hash.
                if scene.backend_hash != self.backend.binding_hash:
                    raise AgentError("VIDEO_BACKEND_CHANGED")
                # Обрабатываем ветвь при условии: not scene.remote_id.
                if not scene.remote_id:
                    recovered = self.backend.reconcile(scene.request_id)
                    # Обрабатываем ветвь при условии: not recovered.
                    if not recovered:
                        continue  # Never interpret missing history as permission to submit again.
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session.begin() as session:
                        row = session.get(VideoScene, scene.id)
                        row.remote_id, row.state = recovered, "running"
                    scene.remote_id = recovered
                result = self.backend.poll(scene.remote_id)
                # Обрабатываем ветвь при условии: result['state'] == 'completed'.
                if result["state"] == "completed":
                    relative = f"{self.settings.app['media_dir']}/projects/{project_id}/scene-{scene.index}.mp4"
                    self.backend.download(result["asset"], relative)
                    qa = quality_check(self.settings.root, relative, self.options)
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session.begin() as session:
                        row = session.get(VideoScene, scene.id)
                        row.state = "completed"
                        row.artifact = {
                            "path": relative,
                            "quality": qa,
                            "origin": "comfyui",
                            "remote_id": scene.remote_id,
                            "workflow_hash": self.options.get("workflow_sha256"),
                        }
                # Обрабатываем ветвь при условии: result['state'] == 'failed'.
                elif result["state"] == "failed":
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session.begin() as session:
                        row = session.get(VideoScene, scene.id)
                        row.state, row.error = "failed", "VIDEO_GENERATION_FAILED"
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            row = session.get(VideoProject, project_id)
            # Обрабатываем ветвь при условии: row.state != 'cancelled'.
            if row.state != "cancelled":
                failures = session.scalars(
                    select(VideoScene).where(
                        VideoScene.project_id == project_id,
                        VideoScene.state.in_(["failed", "timed_out"]),
                    )
                ).all()
                row.state = "generation_failed" if failures else "generating"
                row.error = failures[0].error if failures else None

    # Собираем готовые проверенные сцены в итоговый ролик.
    def assemble(self, project_id, revision, actor="owner"):
        from .collectors import detect_language

        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session() as session:
            project = session.get(VideoProject, project_id)
            # Обрабатываем ветвь при условии: not project or project.revision != revision or project.state not in {'planning', 'queued', 'generating', 'r….
            if (
                not project
                or project.revision != revision
                or project.state
                not in {"planning", "queued", "generating", "render_failed", "generation_failed"}
            ):
                raise AgentError("VIDEO_PROJECT_STATE_CONFLICT", 409)
            scenes = session.scalars(
                select(VideoScene)
                .where(VideoScene.project_id == project_id)
                .order_by(VideoScene.index)
            ).all()
            # Обрабатываем ветвь при условии: not scenes or any((s.state != 'completed' for s in scenes)).
            if not scenes or any(s.state != "completed" for s in scenes):
                raise AgentError("VIDEO_SCENES_NOT_READY")
        # Обрабатываем ветвь при условии: self.service.config_error or project.config_id != self.service.settings.hash.
        if self.service.config_error or project.config_id != self.service.settings.hash:
            raise AgentError("VIDEO_PLAN_STALE", 409)
        relative = f"{self.settings.app['media_dir']}/projects/{project_id}/final.mp4"
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            claimed = session.execute(
                update(VideoProject)
                .where(
                    VideoProject.id == project_id,
                    VideoProject.state.in_(
                        ["planning", "queued", "generating", "render_failed", "generation_failed"]
                    ),
                )
                .values(state="rendering")
            )
            # Обрабатываем ветвь при условии: claimed.rowcount != 1.
            if claimed.rowcount != 1:
                raise AgentError("VIDEO_PROJECT_STATE_CONFLICT", 409)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            qa = render_montage(
                self.settings.root,
                [s.artifact["path"] for s in scenes],
                [s["duration_seconds"] for s in project.plan["scenes"]],
                relative,
                self.options,
            )
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as exc:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session.begin() as session:
                row = session.get(VideoProject, project_id)
                row.state, row.error = "render_failed", exc.code
            raise
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            session.execute(
                update(Channel)
                .where(Channel.id == project.channel_id)
                .values(paused=Channel.paused)
            )
            asset = session.scalar(select(VideoAsset).where(VideoAsset.sha256 == qa["sha256"]))
            duplicates = [
                a.id
                for a in session.scalars(select(VideoAsset))
                if visually_similar(a.frames, qa["frames"])
            ]
            # Обрабатываем ветвь при условии: not asset.
            if not asset:
                asset = VideoAsset(
                    sha256=qa["sha256"],
                    path=relative,
                    frames=qa["frames"],
                    quality=qa,
                    provenance={"project_id": project_id, "scenes": [s.artifact for s in scenes]},
                )
                session.add(asset)
            story_hash = fingerprint("video:" + qa["sha256"])
            story = session.scalar(select(Story).where(Story.text_hash == story_hash))
            # Обрабатываем ветвь при условии: not story.
            if not story:
                story = Story(
                    text_hash=story_hash, title=project.plan["title"], text=project.plan["caption"]
                )
                session.add(story)
                session.flush()
            draft = session.scalar(
                select(Draft).where(
                    Draft.channel_id == project.channel_id, Draft.story_id == story.id
                )
            )
            # Обрабатываем ветвь при условии: not draft.
            if not draft:
                checks = ["EDITOR_REVIEW_REQUIRED", *qa["flags"]]
                # Обрабатываем ветвь при условии: duplicates.
                if duplicates:
                    checks.append("SIMILAR_VIDEO_REVIEW")
                body = project.plan["caption"]
                # Обрабатываем ветвь при условии: detect_language(body) != 'ru' or len(body) > self.service.settings.channel(project.channel_id)['publication….
                if (
                    detect_language(body) != "ru"
                    or len(body)
                    > self.service.settings.channel(project.channel_id)["publication"]["max_chars"]
                ):
                    raise AgentError("VIDEO_CAPTION_INVALID")
                payload = {
                    "title": project.plan["title"],
                    "body": body,
                    "language": "ru",
                    "source_ids": [],
                    "decision": "review",
                    "reason_codes": ["ORIGINAL_VIDEO"],
                    "claims": [],
                    "confidence": 0,
                    "payload": {"origin": "original", "project_id": project_id},
                    "checks": checks,
                    "video": {
                        **qa,
                        "path": relative,
                        "rights_basis": project.plan["rights_basis"],
                        "added_at": now(),
                    },
                }
                draft = Draft(
                    channel_id=project.channel_id,
                    story_id=story.id,
                    current=payload,
                    config_id=project.config_id,
                    state="review_pending",
                )
                session.add(draft)
                session.flush()
                session.add(
                    DraftRevision(
                        draft_id=draft.id, revision=1, payload=payload, config_id=draft.config_id
                    )
                )
            row = session.get(VideoProject, project_id)
            row.state, row.draft_id, row.error = "review_pending", draft.id, None
            self.db.audit(
                session,
                actor,
                "video_assembled",
                project_id,
                draft_id=draft.id,
                sha256=qa["sha256"],
            )
            return {"draft_id": draft.id, "quality": qa, "similar_assets": duplicates}

    # Останавливаем дальнейшую обработку проекта.
    def cancel(self, project_id, actor="owner"):
        # Do not use ComfyUI's global interrupt: it could terminate another operator's job.
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with self.db.session.begin() as session:
            project = session.get(VideoProject, project_id)
            # Обрабатываем ветвь при условии: not project or project.state in {'review_pending', 'cancelled'}.
            if not project or project.state in {"review_pending", "cancelled"}:
                raise AgentError("VIDEO_PROJECT_STATE_CONFLICT", 409)
            project.state = "cancelled"
            self.db.audit(session, actor, "video_cancelled_locally", project_id)

    # Выполняем один допустимый шаг обработки с учётом сохранённого состояния.
    def tick(self):
        # Обрабатываем ветвь при условии: not self.guard.acquire(blocking=False).
        if not self.guard.acquire(blocking=False):
            return
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with self.db.session() as session:
                ids = session.scalars(
                    select(VideoProject.id).where(VideoProject.state.in_(["queued", "generating"]))
                ).all()
            # Последовательно обрабатываем элементы: ids[:10].
            for project_id in ids[:10]:
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    self.poll_project(project_id)
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session() as session:
                        project = session.get(VideoProject, project_id)
                        scenes = session.scalars(
                            select(VideoScene).where(VideoScene.project_id == project_id)
                        ).all()
                    # Обрабатываем ветвь при условии: project.state == 'generating' and scenes and all((s.state == 'completed' for s in scenes)).
                    if (
                        project.state == "generating"
                        and scenes
                        and all(s.state == "completed" for s in scenes)
                    ):
                        self.assemble(project_id, project.revision, "video_worker")
                # Обрабатываем AgentError и выбираем допустимый исход.
                except AgentError as exc:
                    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                    with self.db.session.begin() as session:
                        project = session.get(VideoProject, project_id)
                        # Обрабатываем ветвь при условии: project.error != exc.code.
                        if project.error != exc.code:
                            project.error = exc.code
                            self.db.audit(session, "video_worker", exc.code, project_id)
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            self.guard.release()

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        self.backend.close()
