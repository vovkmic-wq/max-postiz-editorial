# Отдельные расписания каналов, блокировка повторов и обработка ошибок.
"""Project-owned scheduling, durable stop-on-error, and one run per Moscow slot."""

import json
import os
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone

from .config import AgentError, inside
from .channel_profiles import channel_path, window

DIRECTORY = "data/image-schedule"
MOSCOW = timezone(timedelta(hours=3))
QUIET_STATES = {"confirmed", "no_new_image", "already_attempted", "already_published_in_slot", "paused"}


# Получаем текущее время в часовом поясе служебного журнала.
def clock(at=None):
    return (at or datetime.now(MOSCOW)).astimezone(MOSCOW)


# Определяем начало текущего четырёхчасового интервала.
def slot_start(at):
    return at.replace(hour=at.hour // 4 * 4, minute=0, second=0, microsecond=0)


# Читаем сохранённое состояние с проверкой формата.
def read(root, name, default, channel_id="test"):
    path = inside(root, f"{DIRECTORY}/" + channel_path(channel_id, f"{name}.json"))
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    # Обрабатываем (ValueError, OSError) и выбираем допустимый исход.
    except (ValueError, OSError) as exc:
        raise AgentError("IMAGE_SCHEDULE_STATE_INVALID") from exc


# Сохраняем полный снимок состояния через промежуточный файл.
def write(root, name, value, channel_id="test"):
    path = inside(root, f"{DIRECTORY}/" + channel_path(channel_id, f"{name}.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# Исключаем одновременное выполнение конфликтующих операций.
@contextmanager
def lock(root, name):
    from .browser_runtime import profile_lock

    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with profile_lock(inside(root, f"{DIRECTORY}/{name}.lock")):
            yield
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        # Обрабатываем ветвь при условии: exc.code == 'BROWSER_PROFILE_BUSY'.
        if exc.code == "BROWSER_PROFILE_BUSY":
            raise AgentError("IMAGE_SCHEDULE_BUSY", 409) from exc
        raise


# Возвращаем состояние без выполнения публикации.
def status(root, *, at=None, channel_id="test"):
    current, boundary, following, slot, cfg = window(root, channel_id, at)
    control = read(root, "control", {"enabled": False}, channel_id)
    last = read(root, "last", {}, channel_id)
    engine = read(root, "engine", {"mode": "embedded", "state": "offline"})
    # Обрабатываем ветвь при условии: engine.get('heartbeat_at') and (current - datetime.fromisoformat(engine['heartbeat_at'])).total_seconds() > 45.
    if engine.get("heartbeat_at") and (current - datetime.fromisoformat(engine["heartbeat_at"])).total_seconds() > 45:
        engine = {**engine, "state": "offline"}
    from .calendar_schedule import hours_and_days
    times, weekdays = hours_and_days(cfg)
    phase = last.get("phase", "idle")
    # Обрабатываем ветвь при условии: not control.get('enabled').
    if not control.get("enabled"):
        state = "paused"
    # Обрабатываем ветвь при условии: control.get('profile_hash') and control['profile_hash'] != cfg['profile_hash'].
    elif control.get("profile_hash") and control["profile_hash"] != cfg["profile_hash"]:
        state = "configuration_changed"
    # Обрабатываем ветвь при условии: phase in {'running', 'requires_attention'}.
    elif phase in {"running", "requires_attention"}:
        state = phase
    else:
        # Альтернативная ветвь после проверки основного условия.
        state = "enabled"
    not_before = datetime.fromisoformat(control.get("not_before", current.isoformat()))
    # Обрабатываем ветвь при условии: last.get('slot') == slot.
    if last.get("slot") == slot:
        boundary = following
    from .calendar_schedule import plan
    if plan(cfg)["kind"] == "weekly" and current.timestamp() - boundary.timestamp() > 300:
        boundary = following
    boundary = max(boundary, not_before)
    return {"state": state, "enabled": bool(control.get("enabled")),
            "channel_id": channel_id, "timezone": cfg["timezone"],
            "hours": sorted({int(value[:2]) for value in times}), "times": times, "weekdays": weekdays,
            "next_run_at": boundary.isoformat() if state == "enabled" else None,
            "last_run": last, "engine": engine}


# Изменяем состояние управления после проверки незавершённых операций.
def configure(root, enabled, *, at=None, channel_id="test"):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with lock(root, "control"), (lock(root, "run") if enabled else nullcontext()):
        # Обрабатываем ветвь при условии: enabled.
        if enabled:
            check_corrections(root, channel_id)
            # Resuming never repeats the current interval or clears unresolved deliveries.
            from sqlalchemy import select
            from .cli import load_env
            from .config import Settings
            from .db import Database, Intent

            load_env(root)
            db = Database(Settings.load(root))
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                db.initialize()
                # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
                with db.session() as session:
                    # Обрабатываем ветвь при условии: session.scalar(select(Intent).where(Intent.channel_id == channel_id, Intent.state.in_(['pending', 'sending'….
                    if session.scalar(select(Intent).where(
                        Intent.channel_id == channel_id,
                        Intent.state.in_(["pending", "sending", "unknown", "retry_wait"]),
                    )):
                        raise AgentError("IMAGE_SCHEDULE_UNRESOLVED_PUBLICATION", 409)
            finally:
                # Завершаем работу и освобождаем ресурсы независимо от результата.
                db.engine.dispose()
            last = read(root, "last", {}, channel_id)
            # Holding the run lock proves a saved 'running' record is from a stopped process.
            # Обрабатываем ветвь при условии: last.get('phase') in {'running', 'requires_attention'}.
            if last.get("phase") in {"running", "requires_attention"}:
                write(root, "last", {**last, "phase": "acknowledged"}, channel_id)
        current, _, following, _, cfg = window(root, channel_id, at)
        write(root, "control", {"enabled": enabled, "changed_at": current.isoformat(),
              "profile_hash": cfg["profile_hash"], "not_before": following.isoformat()}, channel_id)
    return status(root, at=at, channel_id=channel_id)


# Выполняем один допустимый шаг обработки с учётом сохранённого состояния.
def tick(root, *, at=None, runner=None, channel_id="test"):
    current, start, following, slot, cfg = window(root, channel_id, at)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with lock(root, "run"):
        control = read(root, "control", {"enabled": False}, channel_id)
        last = read(root, "last", {}, channel_id)
        # Обрабатываем ветвь при условии: not control.get('enabled').
        if not control.get("enabled"):
            return {"state": "paused"}
        # Обрабатываем ветвь при условии: control.get('profile_hash') and control['profile_hash'] != cfg['profile_hash'].
        if control.get("profile_hash") and control["profile_hash"] != cfg["profile_hash"]:
            return {"state": "configuration_changed", "channel_id": channel_id}
        # Обрабатываем ветвь при условии: last.get('phase') == 'running'.
        if last.get("phase") == "running":
            last = {**last, "phase": "requires_attention",
                    "result": {"state": "error", "code": "IMAGE_SCHEDULE_INTERRUPTED"}}
            write(root, "last", last, channel_id)
        # Обрабатываем ветвь при условии: last.get('phase') == 'requires_attention'.
        if last.get("phase") == "requires_attention":
            return {"state": "requires_attention", "last_run": last}
        # Обрабатываем ветвь при условии: current < datetime.fromisoformat(control['not_before']).
        if current < datetime.fromisoformat(control["not_before"]):
            return {"state": "not_due", "next_run_at": control["not_before"]}
        # Обрабатываем ветвь при условии: last.get('slot') == slot.
        if last.get("slot") == slot:
            return {"state": "already_checked", "slot": slot}
        # Недельные часы не догоняем задним числом после долгого выключения компьютера.
        from .calendar_schedule import plan
        if plan(cfg)["kind"] == "weekly" and current.timestamp() - start.timestamp() > 300:
            return {"state": "not_due", "next_run_at": following.isoformat()}
        record = {"phase": "running", "slot": slot, "started_at": current.isoformat(),
                  "pid": os.getpid()}
        write(root, "last", record, channel_id)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            check_corrections(root, channel_id)
            result = runner(root, current) if runner else run_images(root, current, channel_id=channel_id)
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as exc:
            result = {"state": "error", "code": exc.code}
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            # Exception messages from HTTP clients may contain API keys.
            result = {"state": "error", "code": "IMAGE_SCHEDULE_RUN_FAILED"}
        record.update(phase="idle" if result["state"] in QUIET_STATES else "requires_attention",
                      finished_at=clock().isoformat(), result=result)
        write(root, "last", record, channel_id)
        history = inside(root, f"{DIRECTORY}/" + channel_path(channel_id, f"history/{slot}.json"))
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**result, "schedule_state": record["phase"]}


# Открываем сервис для одного канала и возвращаем его на паузу после попытки.
def run_images(root, at, *, channel_id="test"):
    from .cli import build_service
    from .image_pipeline import auto_run, publish_rank

    service = build_service(root)
    # Повторно проверяем включённое расписание непосредственно перед отправкой.
    def publish_if_enabled(owner, rank, sha256, **kwargs):
        # Обрабатываем ветвь при условии: not read(root, 'control', {'enabled': False}, channel_id).get('enabled').
        if not read(root, "control", {"enabled": False}, channel_id).get("enabled"):
            return {"state": "paused"}
        return publish_rank(owner, rank, sha256, **kwargs)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        return auto_run(service, at=at, publisher=publish_if_enabled, **({"channel_id": channel_id} if channel_id != "test" else {}))
    finally:
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            service.pause(channel_id, True, actor="project_schedule")
        finally:
            # Завершаем работу и освобождаем ресурсы независимо от результата.
            service.close()


# Не допускаем отправку при незавершённом исправлении подписи.
def check_corrections(root, channel_id="test"):
    # Последовательно обрабатываем элементы: inside(root, 'data/browser-corrections').glob('*.json').
    for path in inside(root, "data/browser-corrections").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        # Обрабатываем ветвь при условии: value.get('intent_id') and value.get('state') in {'editing', 'unknown'}.
        if value.get("intent_id") and value.get("state") in {"editing", "unknown"}:
            # Обрабатываем ветвь при условии: value.get('channel_id') and value['channel_id'] != channel_id.
            if value.get("channel_id") and value["channel_id"] != channel_id:
                continue
            raise AgentError("IMAGE_SCHEDULE_UNRESOLVED_CORRECTION", 409)


# Последовательно обслуживаем каналы, изолируя их ошибки.
def tick_all(root, *, at=None):
    from .channel_profiles import profiles
    from .image_pipeline import source_config

    # Один браузер и одна очередь отправки: каналы обслуживаются последовательно.
    catalog = source_config(root)
    ids = set(profiles(root)["channels"]) | {catalog["target_channel"]}
    results = {}
    # Последовательно обрабатываем элементы: sorted(ids).
    for channel_id in sorted(ids):
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            options = {"at": at} if at else {}
            # Обрабатываем ветвь при условии: channel_id != 'test'.
            if channel_id != "test":
                options["channel_id"] = channel_id
            results[channel_id] = tick(root, **options)
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as exc:
            # Обрабатываем ветвь при условии: exc.code == 'IMAGE_SCHEDULE_BUSY'.
            if exc.code == "IMAGE_SCHEDULE_BUSY":
                continue
            results[channel_id] = {"state": "error", "code": exc.code}
            write(root, "last", {"phase": "requires_attention", "result": results[channel_id]}, channel_id)
    return results


# Поддерживаем встроенный планировщик и не допускаем второго экземпляра.
def run_loop(root, stop):
    """Scheduler lives inside the MAX server; no OS task or Codex wake-up is used."""
    engine = {"mode": "embedded", "state": "online", "pid": os.getpid()}
    # Обрабатываем безопасные коды ошибок фонового запуска.
    def checked_tick():
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            tick_all(root)
        # Обрабатываем AgentError и выбираем допустимый исход.
        except AgentError as exc:
            # Обрабатываем ветвь при условии: exc.code != 'IMAGE_SCHEDULE_BUSY'.
            if exc.code != "IMAGE_SCHEDULE_BUSY":
                write(root, "last", {"phase": "requires_attention",
                      "finished_at": clock().isoformat(),
                      "result": {"state": "error", "code": exc.code}})
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            write(root, "last", {"phase": "requires_attention",
                  "finished_at": clock().isoformat(),
                  "result": {"state": "error", "code": "IMAGE_SCHEDULE_RUN_FAILED"}})
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with lock(root, "engine"):
        job = None
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            # Продолжаем цикл, пока выполняется условие: not stop.is_set().
            while not stop.is_set():
                write(root, "engine", {**engine, "heartbeat_at": clock().isoformat()})
                # Обрабатываем ветвь при условии: not job or not job.is_alive().
                if not job or not job.is_alive():
                    job = threading.Thread(target=checked_tick, daemon=True, name="max-image-run")
                    job.start()
                stop.wait(15)
        finally:
            # Обрабатываем ветвь при условии: job.
            if job:
                job.join(timeout=5)
            write(root, "engine", {**engine, "state": "offline", "heartbeat_at": clock().isoformat()})


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    """Single diagnostic tick, using the same built-in scheduler as the server."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        result = tick(root)
        return 1 if result.get("schedule_state") == "requires_attention" or result["state"] in {
            "requires_attention", "error"
        } else 0
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        # Обрабатываем ветвь при условии: exc.code == 'IMAGE_SCHEDULE_BUSY'.
        if exc.code == "IMAGE_SCHEDULE_BUSY":
            return 0
        write(root, "last", {"phase": "requires_attention", "finished_at": clock().isoformat(),
                             "result": {"state": "error", "code": exc.code}})
        return 1


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    raise SystemExit(main())
