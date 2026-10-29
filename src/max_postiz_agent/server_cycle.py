"""Один ограниченный серверный цикл; единственный PG leader lock удерживает CLI.

Контроллер не отправляет сообщения. Ограниченное исправление отказа он поручает
прежнему циклу, который сохраняет журнал, точное согласование и неизвестные исходы.
"""
import json
import math

from sqlalchemy import select

from .article_attribution import illustrated_presentation
from .article_cycle import HourlyArticleCycle
from .automatic_policy import automatic_evidence
from .config import AgentError, fingerprint, inside
from .db import now
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from .server_state import ServerState, require_running, validate_settings

TERMINAL = {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}


class ServerCycle:
    def __init__(self, core, state=None, selector=None, *, cycle=None, clock=None):
        self.core = core
        self.state = state or ServerState(core)
        self.cycle = cycle or HourlyArticleCycle(core)
        self.selector = selector
        self.clock = clock or now

    def _phase(self, phase, **details):
        # Это подсказка панели, а не новый таймер или резервирование часа.
        next_run = None
        if phase in {"hour_used", "no_source", "selection_slot_expired", "rejected"}:
            slot = details.get("slot")
            if type(slot) is int and slot >= 0:
                next_run = (slot + 1) * 3600
        elif phase == "waiting_interval":
            next_run = details.get("earliest_due_at")
        elif phase == "waiting_dispatch":
            result = details.get("result")
            next_run = result.get("due_at") if isinstance(result, dict) else None
            if next_run is None:
                next_run = details.get("due_at")
        if type(next_run) not in {int, float} or not math.isfinite(next_run) or next_run < 0:
            next_run = None
        details["next_run_at"] = next_run
        self.state.phase(phase, **details)
        return {"phase": phase, **details}

    def _observed_phase(self, phase, record, result, **details):
        # Успешный HTTP/status сам по себе не доказывает известный исход.
        # Очищаем текущую ошибку только после здоровой очереди или полной сверки доставки.
        if (isinstance(result, dict) and not result.get("error")
                and (result.get("state") == result.get("dispatch_state") == "pending"
                     or result.get("state") == "published" and result.get("delivery_verified") is True
                     and result.get("postiz_state") == "PUBLISHED")):
            details["last_error"] = None
        return self._phase(phase, record_id=record["id"], result=result, **details)

    def _inventory(self):
        with self.core.db.session() as session:
            dispatches = list(session.scalars(select(Dispatch).where(Dispatch.state.in_(ACTIVE))))
            leases = session.scalar(select(BridgePost).where(
                BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                | BridgePost.mutation_until.is_(None)))
            owned = session.scalar(select(Dispatch).where(
                Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
            return [{"id": d.id, "variant_id": d.variant_id, "state": d.state}
                    for d in dispatches], bool(leases or owned)

    def _select(self, settings, ledger, policy):
        if self.selector is not None:
            return self.selector(settings, ledger=ledger, policy=policy, now=self.clock())
        if settings["source"] == "open_science":
            from .server_nasa import select_nasa_article
            from .server_science import select_science_article
            policies = self.cycle._policies()
            # Старый режим сохраняет вращение по часу. Резерв делает несколько
            # кандидатов в час и после любого известного результата меняет источник.
            offset = int(self.clock() // 3600) % len(policies)
            if getattr(self.cycle, "reserve_mode", False):
                source_ids = [current["source_id"] for current in policies]
                previous = next((record.get("source_id") for record in reversed(ledger["records"])
                    if record.get("reserve_contract") == "article-reserve-v1"
                    and record.get("source_id") in source_ids), None)
                if previous is not None:
                    offset = (source_ids.index(previous) + 1) % len(policies)
            policies = policies[offset:] + policies[:offset]
            failures = []
            for current in policies:
                if (illustrated_presentation(current.get("presentation_contract"))
                        and current["source_id"] not in {"noirlab_iotw", "esahubble_potw"}):
                    # В этом формате пока доказаны только собственные JPEG
                    # двух научных источников; NASA-текст не теряем из-за
                    # отсутствия отдельного разрешения на конкретное фото.
                    continue
                self.state.assert_running()
                try:
                    if current["source_id"] == policy["source_id"]:
                        article = select_nasa_article(self.core, settings["nasa"], ledger,
                            current, now=self.clock(), checkpoint=True)
                    else:
                        article = select_science_article(self.core, current["source_id"],
                            settings["nasa"], ledger, current, now=self.clock(), checkpoint=True)
                    if article is not None:
                        if fingerprint(self.cycle._policy_for_article(article)) != fingerprint(current):
                            raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
                        self.state.phase("selecting", source_errors=failures)
                        return article
                except AgentError as exc:
                    # Неудачный GET другого источника не одобряет его текст и не повторяется.
                    # Пауза/потеря leader и нарушение границ подбора останавливают весь цикл.
                    if exc.code not in {"NASA_SOURCE_UNAVAILABLE", "SCIENCE_SOURCE_UNAVAILABLE",
                            "NASA_SOURCE_CATALOG_INVALID", "SCIENCE_SOURCE_CATALOG_INVALID",
                            "SCIENCE_SOURCE_CONTENT_INVALID", "SCIENCE_SOURCE_ACCESS_BLOCKED",
                            "ARTICLE_SOURCE_TOO_LARGE",
                            "ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED"}:
                        raise
                    failures.append({"source_id": current["source_id"], "code": exc.code})
            self.state.phase("selecting", source_errors=failures,
                last_error={"code": "SCIENCE_SOURCES_UNAVAILABLE"} if failures else None)
            return None
        if settings["source"] != "nasa_svs":
            # Другой источник требует отдельной привязки разрешений и прав на текст.
            raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
        from .server_nasa import select_nasa_article
        return select_nasa_article(self.core, settings["nasa"], ledger, policy, now=self.clock(), checkpoint=True)

    def _ready(self, record, settings, policy):
        policy = self.cycle._policy_for_record(record)
        self.state.assert_running()
        snapshot = self.cycle._snapshot(record)
        hashed = fingerprint(snapshot)
        if record.get("prepared_hash") != hashed:
            raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
        with self.core.db.session() as session:
            variant = session.get(Variant, record.get("variant_id"))
            if (variant is None or automatic_evidence(session, variant) is None
                    or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
                    or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))):
                raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
            actual = list(session.scalars(select(DispatchAttempt).join(Dispatch).join(Variant).where(
                Variant.target_id == policy["target_id"], DispatchAttempt.result == "published")))
        earliest = self.cycle._cadence(policy, self.clock(), check_gap=False)
        interval = max(settings["interval_seconds"], policy["min_gap_seconds"])
        if actual:
            earliest = max(earliest, max(a.created_at for a in actual) + interval + 1)
        due = max(self.clock() + 120, earliest)
        if due > self.clock() + 3700:
            return self._phase("waiting_interval", record_id=record["id"], earliest_due_at=due,
                               last_error=None)
        # publish повторяет финальные locks/hash/evidence/interval/pause проверки.
        # Вызов ровно один, автоматического повтора неизвестного исхода нет.
        self.state.assert_running()
        self._phase("scheduling", record_id=record["id"], reviewed_hash=hashed, due_at=due,
                    last_error=None)
        result = self.cycle.publish(record["id"], hashed, due_at=due)
        return self._phase("waiting_dispatch", record_id=record["id"], result=result, due_at=due)

    def _resume_pending(self, record, dispatch_id, settings, policy):
        """Возобновить только прежнюю точную отправку после общей паузы."""
        policy = self.cycle._policy_for_record(record)
        with self.core.db.session() as session:
            target = session.get(Target, record.get("target_id"))
            if target is not None and not target.paused:
                return False
        if not record.get("postiz_id"):
            raise AgentError("EDITORIAL_PENDING_RESUME_NOT_SAFE", 409)
        # Это только GET. Ни создание approval, ни повтор HTTP-планирования не нужны.
        remote = self.core.postiz.get_post(record["postiz_id"])
        body, media, observed_due, remote_hash = self.core.bridge.normalize(remote)
        illustrated = illustrated_presentation(record.get("presentation_contract"))
        if illustrated:
            from .article_images import validate_article_image
            expected_media = validate_article_image(self.core, record)
            media_matches = self.core.bridge._media(media, target) == expected_media
        else:
            media_matches = not media
        with self.core.db.session.begin() as session:
            # Порядок согласован с глобальной паузой и отправкой: Control→Variant→Target→Dispatch.
            require_running(self.core, session, lock=True)
            variant = self.core._variant(session, record.get("variant_id"), True)
            target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
            dispatch = session.scalar(select(Dispatch).where(Dispatch.id == dispatch_id).with_for_update())
            approval = session.get(TargetApproval, dispatch.approval_id) if dispatch else None
            if illustrated:
                validate_article_image(self.core, record, variant)
            # Подписанная дата остаётся точной: Postiz возвращает её с точностью
            # до миллисекунд. Допуск относится только к наблюдаемому GET.
            due = dispatch.due_at if dispatch else None
            snapshot = self.core.snapshot(variant, target)
            bound = fingerprint({"content": snapshot, "due_at": due})
            link = session.get(BridgePost, record["postiz_id"])
            unsafe = (
                not dispatch or not approval or not target or not link
                or type(due) not in {int, float} or not math.isfinite(due)
                or type(observed_due) not in {int, float} or not math.isfinite(observed_due)
                or dispatch.state != "pending" or dispatch.owner is not None or dispatch.lease_until != 0
                or dispatch.attempts != 0 or dispatch.verified or dispatch.error
                or dispatch.external_id is not None or dispatch.permalink is not None
                or record.get("dispatch_id") != dispatch.id
                or target.id != policy["target_id"] or target.id != record.get("target_id")
                or target.platform != "max" or target.route != "max_browser" or not target.enabled
                or variant.metadata_json.get("approval_mode") != "automatic"
                or variant.metadata_json.get("origin") != "hourly_article_test"
                or variant.metadata_json.get("record_id") != record["id"]
                or variant.metadata_json.get("policy_hash") != fingerprint(policy)
                or record.get("policy_hash") != fingerprint(policy)
                or record.get("source_id") != policy["source_id"]
                or approval.actor != "hourly_article_policy" or approval.revoked
                or approval.variant_id != variant.id or dispatch.variant_id != variant.id
                or dispatch.revision != variant.revision or approval.revision != variant.revision
                or fingerprint(snapshot) != record.get("prepared_hash")
                or dispatch.snapshot != snapshot or dispatch.snapshot_hash != bound or approval.snapshot_hash != bound
                or automatic_evidence(session, variant) is None
                or remote.get("id") != record["postiz_id"] or remote.get("state") != "QUEUE"
                or body != snapshot["body"] or not media_matches or abs(observed_due - due) >= .001
                or due != approval.scheduled_at
                or due != record.get("due_at") or not self.clock() - 3600 <= due <= self.clock() + 3700
                or link.variant_id != variant.id or not link.active or link.error or link.remote_state != "QUEUE"
                or link.remote_hash != remote_hash or snapshot["metadata"].get("postiz_hash") != remote_hash
                or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE), Dispatch.id != dispatch.id))
                or session.scalar(select(Dispatch).where(Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                or session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id))
                or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id, Dispatch.id != dispatch.id))
                or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id,
                                                               TargetApproval.id != approval.id))
                or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                    | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))
            )
            if unsafe:
                raise AgentError("EDITORIAL_PENDING_RESUME_NOT_SAFE", 409)
            self.cycle._cadence(policy, due)
            times = list(session.scalars(select(DispatchAttempt.created_at).join(Dispatch).join(Variant).where(
                Variant.target_id == target.id, DispatchAttempt.result == "published")))
            if times and due < max(times) + max(settings["interval_seconds"], policy["min_gap_seconds"]) + 1:
                raise AgentError("ARTICLE_HOURLY_GAP", 409)
            self.core._quality(session, variant, target)
            if record.get("reserve_contract") == "article-reserve-v1":
                from .article_reserve import ArticleReserveStore
                # После доказанной сверки только прежнего QUEUE возвращаем его
                # fence текущему владельцу; дата/token/approval не меняются.
                ArticleReserveStore(self.core).rebind_pending(session, record["id"], dispatch_id)
            target.paused = False
            self.core.db.audit(session, "editorial_server", "editorial_resume_pending", dispatch.id,
                               snapshot_hash=dispatch.snapshot_hash, due_at=due, variant_id=variant.id)
        return True

    def _known_rejection(self, record):
        """Проверить завершённый известный отказ, не меняя запись и её доказательства."""
        if (record.get("state") != "needs_attention"
                or record.get("error") != "ARTICLE_QUALITY_REJECTED"
                or type(record.get("slot")) is not int or record["slot"] < 0):
            return False
        attempts = record.get("preparation_attempts", [])
        if not attempts or attempts[-1].get("finished_at") is None:
            return False
        finished = attempts[-1]["finished_at"]
        if type(finished) not in {int, float} or not math.isfinite(finished):
            return False
        path = inside(self.core.settings.root, record.get("quality_trace", ""))
        expected = self.cycle.directory / ("quality-" + record["id"] + ".local.jsonl")
        if path != expected or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return False
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (ValueError, UnicodeError):
            return False
        if (not events or any(not isinstance(e, dict) or e.get("record_id") != record["id"] for e in events)
                or events[-1].get("phase") != "rejected"
                or events[-1].get("error") != "ARTICLE_QUALITY_REJECTED"
                or any(type(e.get("at")) not in {int, float} or not math.isfinite(e["at"])
                       or e["at"] > finished for e in events)
                or any(e.get("phase") in {"review_failed", "translation_failed", "spelling_failed",
                                            "review_invalid", "translation_invalid"} for e in events)):
            return False
        known = any(e.get("phase") == "gate_rejected"
                    or e.get("phase") == "review_parsed" and e.get("review", {}).get("approved") is False
                    or e.get("phase") == "spelling_adjudicated" and bool(e.get("unresolved"))
                    for e in events)
        if not known:
            return False
        self.cycle._assert_pre_draft(record)
        return True

    def _close_known_rejection(self, record):
        slot = record.get("slot")
        # Пока исходный час открыт, оператор может явно исправить кандидат.
        # Закрытие не освобождает использованный источник или занятый час.
        if type(slot) is not int or slot >= int(self.clock() // 3600):
            return False
        # Claim может успеть попасть в Audit до записи ledger: старого trace недостаточно.
        status = self._automatic_repair_status(record)
        if status["eligible"] or status["reason"] != "EXPIRED" or not self._known_rejection(record):
            return False
        self.state.assert_running()
        self.cycle.cancel_preparation(record["id"],
            "Autonomous policy preserved a completed known rejection; no automatic correction or verdict retry")
        return True

    def _automatic_repair_status(self, record):
        status = self.cycle.automatic_repair_status(record)
        if (not isinstance(status, dict) or not {"eligible", "reason", "attempts"} <= set(status)
                or type(status["eligible"]) is not bool
                or not isinstance(status["reason"], str) or not status["reason"].strip()
                or type(status["attempts"]) is not int or not 0 <= status["attempts"] <= 2
                or status["eligible"] and (status["attempts"] >= 2 or status["reason"] in {
                    "CLAIM_LOST", "IN_PROGRESS", "FAILED", "EXHAUSTED", "NOT_REPAIRABLE", "NO_CHANGE", "EXPIRED"})):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_STATUS_INVALID", 409)
        return status

    def _repair_stopped(self, record, status, *, known_rejection=False):
        if known_rejection or status["reason"] in {"EXHAUSTED", "NOT_REPAIRABLE", "NO_CHANGE"}:
            return self._phase("rejected", record_id=record["id"], slot=record["slot"],
                repair_status=status["reason"], repair_attempts=status["attempts"],
                last_error={"code": "ARTICLE_QUALITY_REJECTED"})
        return self._phase("blocked", record_id=record["id"], repair_status=status["reason"],
                           last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})

    def _repair_current_rejection(self, record, settings, policy):
        policy = self.cycle._policy_for_record(record)
        if (type(record.get("slot")) is not int or record["slot"] != int(self.clock() // 3600)
                or not (record.get("error") == "ARTICLE_QUALITY_REJECTED"
                        or record.get("automatic_repair_attempts"))):
            return None
        status = self._automatic_repair_status(record)
        if not status["eligible"]:
            return self._repair_stopped(record, status)
        identity = {key: record.get(key) for key in ("id", "slot", "url", "original_hash", "input_hash",
                    "policy_hash", "target_id", "source_id", "source_published_at")}
        self.state.assert_running()
        if record["slot"] != int(self.clock() // 3600):
            return self._phase("rejected", record_id=record["id"], slot=record["slot"],
                               last_error={"code": "ARTICLE_QUALITY_REJECTED"})
        # Один вызов за проход. Лимит и незавершённый claim проверяет durable-протокол.
        self._phase("preparing", record_id=record["id"], slot=record["slot"],
                    operation="automatic_repair", repair_attempt=status["attempts"] + 1,
                    last_error={"code": "ARTICLE_QUALITY_REJECTED"})
        try:
            result = self.cycle.repair_rejected(record["id"])
        except AgentError as exc:
            if exc.code != "ARTICLE_QUALITY_REJECTED":
                raise
            fresh = self.cycle._record(self.cycle._load(), record["id"])
            if self._close_known_rejection(fresh):
                return self._phase("rejected", record_id=fresh["id"], slot=fresh["slot"],
                                   last_error={"code": "ARTICLE_QUALITY_REJECTED"})
            status = self._automatic_repair_status(fresh)
            return self._repair_stopped(fresh, status, known_rejection=status["eligible"])
        fresh = self.cycle._record(self.cycle._load(), record["id"])
        if (not isinstance(result, dict) or result.get("id") != record["id"]
                or any(fresh.get(key) != value for key, value in identity.items())):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_RESULT_INVALID", 409)
        if fresh.get("state") != "automatic_ready":
            status = self._automatic_repair_status(fresh)
            if status["eligible"]:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_RESULT_INVALID", 409)
            return self._repair_stopped(fresh, status)
        if (not fresh.get("variant_id")
                or not fresh.get("postiz_id")
                or any(fresh.get(key) is not None for key in ("dispatch_id", "due_at"))):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_RESULT_INVALID", 409)
        active, leased = self._inventory()
        if active or leased:
            return self._phase("blocked", record_id=fresh["id"],
                               last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        return self._ready(fresh, settings, policy)

    def run_once(self):
        control = self.state.read()
        if control["paused"]:
            return {"phase": "paused"}
        settings = validate_settings(control["settings"])
        self.state.assert_running()
        policy = self.cycle._policy()
        if policy.get("approval_mode") != "automatic":
            return self._phase("blocked", last_error={"code": "ARTICLE_AUTOMATIC_POLICY_REQUIRED"})
        ledger = self.cycle._load()
        active, leased = self._inventory()
        if leased or any(d["state"] in {"sending", "unknown"} for d in active):
            return self._phase("blocked", last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        if active:
            match = next((r for r in ledger["records"]
                          if any(d["variant_id"] == r.get("variant_id") for d in active)), None)
            if match is not None:
                exact = [d for d in active if d["variant_id"] == match.get("variant_id")]
                if len(active) == len(exact) == 1 and exact[0]["state"] == "pending":
                    self._resume_pending(match, exact[0]["id"], settings, policy)
                result = self.cycle.status(match["id"])
                return self._observed_phase("waiting_dispatch", match, result, due_at=match.get("due_at"))
            return self._phase("blocked", last_error={"code": "ARTICLE_OUTSTANDING_DELIVERY"})
        # Сначала сверяем прежний исход. status не вызывает новую отправку.
        unfinished = [r for r in ledger["records"] if r["state"] not in TERMINAL | {"automatic_ready"}]
        if unfinished:
            record = unfinished[0]
            if record.get("dispatch_id"):
                result = self.cycle.status(record["id"])
                return self._observed_phase("reconciling", record, result)
            if self._close_known_rejection(record):
                return self._phase("rejected", record_id=record["id"], slot=record["slot"],
                                   last_error={"code": "ARTICLE_QUALITY_REJECTED"})
            repaired = self._repair_current_rejection(record, settings, policy)
            if repaired is not None:
                return repaired
            return self._phase("blocked", record_id=record["id"],
                               last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        ready = [r for r in ledger["records"] if r["state"] == "automatic_ready"]
        slot = int(self.clock() // 3600)
        # Прежний готовый материал может использовать свой занятый текущий час.
        other_claims = [r for r in ledger["records"] if r.get("slot") == slot or r.get("automatic_slot") == slot]
        if ready:
            record = ready[0]
            if any(r["id"] != record["id"] for r in other_claims):
                return self._phase("hour_used", slot=slot)
            return self._ready(record, settings, policy)
        if other_claims or control.get("runtime", {}).get("cycle_claim_hour") == slot:
            return self._phase("hour_used", slot=slot)
        # Сохраняем час до сети/моделей: перезапуск не повторит подготовку,
        # а пропущенные часы никогда не догоняются.
        self._phase("selecting", cycle_claim_hour=slot, slot=slot, last_error=None)
        article = self._select(settings, ledger, policy)
        if article is None:
            return self._phase("no_source", slot=slot)
        if int(self.clock() // 3600) != slot:
            return self._phase("selection_slot_expired", slot=slot)
        self.state.assert_running()
        self._phase("preparing", slot=slot, source_url=article["url"])
        try:
            result = self.cycle.prepare(article)
        except AgentError as exc:
            fresh = self.cycle._load()
            failed = next((r for r in reversed(fresh["records"]) if r.get("slot") == slot), None)
            if exc.code == "ARTICLE_QUALITY_REJECTED" and failed:
                if self._close_known_rejection(failed):
                    return self._phase("rejected", record_id=failed["id"], slot=slot,
                                       last_error={"code": "ARTICLE_QUALITY_REJECTED"})
                repaired = self._repair_current_rejection(failed, settings, policy)
                if repaired is not None:
                    return repaired
            raise
        record = self.cycle._record(self.cycle._load(), result["id"])
        if record.get("state") != "automatic_ready":
            return self._phase("blocked", record_id=record["id"],
                               last_error={"code": "ARTICLE_AUTOMATIC_NOT_READY"})
        return self._ready(record, settings, policy)
