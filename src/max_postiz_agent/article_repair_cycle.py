"""Однократные исправления известного pre-DRAFT отказа; согласования здесь нет."""
import copy
import hashlib
import json
import math
import os
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .article_repair import REPAIR_CONTRACT, REPAIR_MODEL, repair_replay_hash
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside, parse_json
from .db import Audit
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, Target
from .reddit import text_hash
from .server_state import require_running

MAX_AUTOMATIC_REPAIRS = 2
QUARANTINE_ERRORS = {"ARTICLE_REPAIR_RESPONSE_INVALID", "ARTICLE_REPAIR_PATCH_INVALID"}
_BINDING = ("id", "number", "contract", "model", "input_hash", "policy_hash", "slot",
            "parent_trace_hash", "parent_trace_bytes", "snapshot_hash", "snapshot_path", "prompts", "target_binding_hash",
            "source_config_hash")


def _now():
    # Единые часы с существующим протоколом и его проверками границы часа.
    from .article_cycle import now
    return now()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _claim_id(ident, number):
    return str(uuid5(NAMESPACE_URL, f"max-postiz:automatic-repair-v1:{ident}:{number}"))


def _write_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if path.exists():
        if path.read_bytes() != raw:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_FILE_CHANGED", 409)
        return _sha(raw)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return _sha(raw)


def capture_input(cycle, record, article):
    """Первичный вход сохраняется до Hy, включая пробелы и исходный тип даты."""
    relative = f"data/hourly-articles/input-{record['id']}.local.json"
    record.update(input_file=relative, input_file_hash=_write_json(inside(cycle.core.settings.root, relative), article))


class AutomaticArticleRepair:
    def __init__(self, cycle):
        self.cycle, self.core = cycle, cycle.core

    def _history(self, record):
        history = record.get("automatic_repair_attempts", [])
        if not isinstance(history, list) or len(history) > MAX_AUTOMATIC_REPAIRS:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_STATE_INVALID", 409)
        with self.core.db.session() as session:
            for number in range(1, MAX_AUTOMATIC_REPAIRS + 1):
                audit = session.get(Audit, _claim_id(record["id"], number))
                claim = history[number - 1] if number <= len(history) else None
                if number <= len(history) and (not isinstance(claim, dict) or not claim):
                    return history, "CLAIM_LOST"
                # Audit PK переживает потерю/откат ledger и не выдаёт второй запрос.
                if bool(audit) != bool(claim):
                    return history, "CLAIM_LOST"
                if claim and (not isinstance(claim, dict) or claim.get("number") != number
                        or claim.get("id") != _claim_id(record["id"], number)
                        or audit.actor != "automatic_article_repair"
                        or audit.entity != record["id"] or audit.action != "automatic_article_repair_claim"
                        or not isinstance(audit.details, dict)
                        or any(claim.get(key) != audit.details.get(key) for key in _BINDING)):
                    return history, "CLAIM_LOST"
        for claim in history:
            state = claim.get("state")
            if state in {"correcting", "checking"}:
                return history, "IN_PROGRESS"
            if state not in {"passed", "rejected", "not_repairable", "no_change", "expired"}:
                return history, "FAILED"
            if (type(claim.get("finished_at")) not in {int, float}
                    or not math.isfinite(claim["finished_at"])):
                return history, "FAILED"
        return history, None

    def status(self, record):
        history, blocked = self._history(record)
        result = {"eligible": False, "reason": blocked or "NOT_ELIGIBLE", "attempts": len(history)}
        if blocked:
            return result
        if history and history[-1].get("outcome_origin") == "protocol_guard":
            try:
                self._quarantine_proof(record, history[-1], saved=True)
            except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError):
                return {**result, "reason": "TRACE_INVALID"}
        if record.get("state") != "needs_attention" or record.get("error") != "ARTICLE_QUALITY_REJECTED":
            return result
        try:
            self._payload(record)
            self.cycle._assert_pre_draft(record)
        except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return {**result, "reason": "TRACE_INVALID"}
        if record.get("slot") < int(_now() // 3600):
            return {**result, "reason": "EXPIRED"}
        if record.get("slot") != int(_now() // 3600):
            return result
        if history and history[-1]["state"] in {"not_repairable", "no_change", "expired", "passed"}:
            return {**result, "reason": {"not_repairable": "NOT_REPAIRABLE", "no_change": "NO_CHANGE"}.get(history[-1]["state"], "EXHAUSTED")}
        if len(history) >= MAX_AUTOMATIC_REPAIRS:
            return {**result, "reason": "EXHAUSTED"}
        if self.cycle._policy().get("approval_mode") != "automatic":
            return result
        return {**result, "eligible": True, "reason": "READY"}

    def _prompts(self):
        root = self.core.settings.root / "prompts"
        return {name: _sha((root / name).read_bytes()) for name in (
            "ARTICLE_TRANSLATE.txt", "ARTICLE_REVIEW_MEANING_FIRST_V1.txt", "ARTICLE_REPAIR.txt")}

    def _quiet(self, record, policy):
        require_running(self.core)
        self.cycle._assert_pre_draft(record)
        if (record["slot"] != int(_now() // 3600)
                or fingerprint(self.cycle._policy()) != record["policy_hash"]
                or record["policy_hash"] != fingerprint(policy)):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        source, target = self.cycle._source_target(policy)
        if record.get("target_id") != target.id or record.get("source_id") != source["id"]:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        with self.core.db.session() as session:
            self._quiet_session(session)
        return source, target

    def _quiet_session(self, session):
        if (any(not target.paused for target in session.scalars(select(Target)))
                or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE)
                    | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                    | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_NOT_QUIET", 409)

    def _payload(self, record):
        """Читаем полный завершённый отказ, не принимая замечания за инструкции."""
        from .adapters import extract_object
        from .article_quality import (_numeric_feedback, normalize_article_links, normalize_rubric_review,
                                      rubric_spelling, rubric_translation_gate, TOPICS)
        from .editorial import numbers

        def require(value):
            if not value:
                raise ValueError

        def timestamp(value):
            require(type(value) in {int, float} and math.isfinite(value) and value >= 0)
            return value

        def fields(value):
            require(isinstance(value, dict) and set(value) == {"title", "body"}
                    and all(isinstance(v, str) and v.strip() for v in value.values()))
            return {key: normalize_article_links(value[key].strip()) for key in ("title", "body")}

        try:
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            require(record.get("quality_trace") == expected and type(record.get("slot")) is int)
            raw = inside(self.core.settings.root, expected).read_bytes()
            require(0 < len(raw) <= 2 * 1024 * 1024)
            lines = raw.decode("utf-8").splitlines()
            require(0 < len(lines) <= 10000 and all(line.strip() for line in lines))
            events, previous = [], -1
            for line in lines:
                event = parse_json(line)
                require(isinstance(event, dict) and event.get("record_id") == record["id"]
                        and isinstance(event.get("phase"), str)
                        and type(event.get("attempt")) is int and 1 <= event["attempt"] <= 3)
                stamp = timestamp(event.get("at"))
                require(stamp >= previous)
                previous = stamp
                events.append(event)
            attempts = record.get("preparation_attempts")
            require(isinstance(attempts, list) and attempts and all(isinstance(a, dict) for a in attempts))
            attempt = attempts[-1]
            require(type(attempt.get("number")) is int and attempt["number"] == len(attempts)
                    and attempt.get("state") in {"needs_attention", "rejected", "failed"}
                    and attempt.get("error") == "ARTICLE_QUALITY_REJECTED")
            started, finished = timestamp(attempt.get("started_at")), timestamp(attempt.get("finished_at"))
            require(timestamp(record.get("claimed_at")) <= started <= finished)
            first = next(e for e in events if e["phase"] == "translation_started")
            first_source = first["context"]["source_untrusted"]
            require(isinstance(first_source, dict) and set(first_source) == {"url", "title", "body"})
            article = {"url": first_source["url"], "title": first_source["title"],
                       "original": first_source["body"], "published_at": record["source_published_at"]}
            if record.get("input_file"):
                expected_input = f"data/hourly-articles/input-{record['id']}.local.json"
                require(record["input_file"] == expected_input)
                saved = inside(self.core.settings.root, expected_input).read_bytes()
                require(0 < len(saved) <= 40000 and _sha(saved) == record.get("input_file_hash"))
                article = parse_json(saved.decode("utf-8"))
            require(isinstance(article, dict) and set(article) == {"url", "title", "original", "published_at"}
                    and all(isinstance(article[k], str) and article[k].strip() for k in ("url", "title", "original")))
            timestamp(article["published_at"])
            require(fingerprint(article) == record["input_hash"] and article["url"] == record["url"]
                    and text_hash(article["original"]) == record["original_hash"]
                    and article["published_at"] == record["source_published_at"])
            source = {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
            require(first_source == source)
            for old in attempts:
                if not old.get("candidate_hash") and old.get("error") == "ARTICLE_QUALITY_REJECTED":
                    self.cycle._load_canonical_initial_candidates(record, old, article)
            # Два последовательных вызова могут иметь одинаковые timestamp.
            # Последний вызов отделяется фазой attempt=1, а не только временем.
            call_starts = [i for i, e in enumerate(events) if e["phase"] in {
                "translation_started", "candidate_started"} and e["attempt"] == 1]
            require(call_starts)
            segment = events[call_starts[-1]:]
            require(segment and all(started <= e["at"] <= finished for e in segment))
            terminal = segment[-1]
            initial = segment[0]["phase"] == "translation_started"
            require(initial == (not bool(attempt.get("candidate_hash"))))
            require(terminal["phase"] == "rejected" and terminal.get("error") == "ARTICLE_QUALITY_REJECTED"
                    and terminal.get("mode") == (None if initial else "candidate"))
            starts = [i for i, e in enumerate(segment[:-1]) if e["phase"] in {"translation_started", "candidate_started"}]
            require(starts and starts[0] == 0 and len(starts) == terminal["attempt"]
                    and (initial or len(starts) == 1))
            translation_prompt = (self.core.settings.root / "prompts/ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8")
            review_prompt = (self.core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt").read_text(encoding="utf-8")
            review_hash = _sha(review_prompt.encode("utf-8"))
            original = source["title"] + "\n\n" + source["body"]
            prior_feedback = {}
            candidate = None
            for number, left in enumerate(starts, 1):
                right = starts[number] if number < len(starts) else len(segment) - 1
                block = segment[left:right]
                require(block and all(e["attempt"] == number for e in block))
                phases = [e["phase"] for e in block]
                if initial:
                    require(phases[:3] == ["translation_started", "translation_returned", "translation_parsed"])
                    context = block[0].get("context")
                    require(isinstance(context, dict) and context.get("source_untrusted") == source
                            and context.get("instruction") == translation_prompt
                            and context.get("numeric_tokens_must_match_exactly") == sorted(numbers(original))
                            and isinstance(block[1].get("raw"), str))
                    candidate = fields(extract_object(block[1]["raw"]))
                    require(block[2].get("translation") == candidate)
                    tail = block[3:]
                    require(tail and tail[-1]["phase"] == "retry")
                    retry = tail.pop()
                else:
                    require(phases[0] == "candidate_started" and block[0].get("mode") == "candidate"
                            and block[0].get("source_untrusted") == source)
                    candidate = fields(block[0].get("candidate"))
                    require(block[0]["candidate"] == candidate)
                    supplied = block[0].get("candidate_raw", candidate)
                    require(fields(supplied) == candidate
                            and self.cycle._candidate_hash(candidate) == attempt.get("candidate_hash"))
                    if attempt.get("candidate_raw_hash") is not None:
                        require(fingerprint(supplied) == attempt["candidate_raw_hash"])
                    tail, retry = block[1:], None
                translated = candidate["title"] + "\n\n" + candidate["body"]
                full = translated + "\n\nИсточник: " + article["url"] + "\nПеревод и редактура с помощью нейросетей."
                gate_error = None
                try:
                    rubric_translation_gate(original, translated, "meaning-first-v1")
                    if len(full) > 3500:
                        raise AgentError("ARTICLE_POST_TOO_LONG")
                except AgentError as exc:
                    gate_error = exc.code
                require(tail and tail[0].get("characters") == len(full))
                if gate_error:
                    require([e["phase"] for e in tail] == ["gate_rejected"] and tail[0].get("error") == gate_error)
                    feedback = _numeric_feedback(original, translated) if gate_error == "TRANSLATION_NUMBER_CHANGED" else gate_error
                else:
                    require(tail[0]["phase"] == "gate_passed")
                    replay = repair_replay_hash(candidate)
                    if [e["phase"] for e in tail] == ["gate_passed", "candidate_repeated"]:
                        require(replay in prior_feedback and tail[1].get("feedback") == prior_feedback[replay])
                        feedback = prior_feedback[replay]
                    else:
                        phases = [e["phase"] for e in tail]
                        expected_phases = ["gate_passed", "review_started", "review_returned", "review_parsed",
                                           "spelling_started", "spelling_returned", "spelling_adjudicated"]
                        if "review_normalized" in phases:
                            expected_phases.insert(3, "review_normalized")
                        require(phases == expected_phases)
                        context = tail[1].get("context")
                        require(isinstance(context, dict) and context == {
                            "instruction": review_prompt, "source_untrusted": original, "translation_untrusted": translated,
                            "review_rubric": "meaning-first-v1", "review_prompt_hash": review_hash})
                        require(isinstance(tail[2].get("raw"), str))
                        raw_review = extract_object(tail[2]["raw"])
                        reviewed = normalize_rubric_review(raw_review, "meaning-first-v1")
                        normalized = [e for e in tail if e["phase"] == "review_normalized"]
                        require(bool(normalized) == (reviewed != raw_review))
                        if normalized:
                            require(normalized[0].get("raw_review") == raw_review and normalized[0].get("review") == reviewed)
                        parsed = next(e for e in tail if e["phase"] == "review_parsed")
                        require(parsed.get("review") == reviewed)
                        matches = next(e for e in tail if e["phase"] == "spelling_returned").get("matches")
                        require(isinstance(matches, list) and all(isinstance(m, dict) for m in matches))
                        adjudicated, unresolved = rubric_spelling(original, translated, matches, "meaning-first-v1", allow_spacetime=True)
                        spelling = tail[-1]
                        require(spelling.get("adjudicated") == adjudicated and spelling.get("unresolved") == unresolved
                                and not (reviewed["approved"] is True and not reviewed["issues"]
                                         and reviewed["topic"] in TOPICS and not unresolved))
                        feedback = {"review_approved": reviewed["approved"], "topic": reviewed["topic"],
                                    "review_issues": reviewed["issues"], "spelling_matches": unresolved[:20]}
                if retry is not None:
                    require(retry.get("feedback") == feedback)
                prior_feedback[repair_replay_hash(candidate)] = feedback
            require(terminal.get("feedback") == feedback)
            rejected = set()
            for event in events:
                if event["phase"] in {"translation_parsed", "candidate_started"}:
                    value = event.get("translation") if event["phase"] == "translation_parsed" else event.get("candidate")
                    require(fields(value) == value)
                    rejected.add(repair_replay_hash(value))
            feedback = {"events": [copy.deepcopy(e) for e in segment if e["phase"] in {
                "gate_rejected", "review_returned", "review_parsed", "spelling_returned", "spelling_adjudicated", "retry", "rejected"}]}
            return {"article": article, "candidate": candidate, "feedback": feedback,
                    "parent_trace_hash": _sha(raw), "rejected_replay_hashes": sorted(rejected)}
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, StopIteration, AgentError, OverflowError) as exc:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409) from exc

    def _trace(self, record, claim):
        path = inside(self.core.settings.root, claim["repair_trace"])

        def write(event):
            raw = json.dumps({**event, "record_id": record["id"], "repair_claim": claim["id"], "at": _now()}, ensure_ascii=False) + "\n"
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return write

    def _quarantine_proof(self, record, claim, *, saved=False):
        """Завершённый неверный JSON можно отложить; неизвестный запрос — нельзя."""
        code = claim.get("error")
        if code not in QUARANTINE_ERRORS:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        self.cycle._assert_pre_draft(record)
        snapshot = parse_json(inside(self.core.settings.root, claim["snapshot_path"]).read_text(encoding="utf-8"))
        self._files(record, claim, snapshot["article"])
        source, target = self.cycle._source_target(self.cycle._policy())
        if (self._prompts() != claim["prompts"] or fingerprint(self.cycle._policy()) != claim["policy_hash"]
                or fingerprint(source) != claim["source_config_hash"] or target.binding_hash != claim["target_binding_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        with self.core.db.session() as session:
            self._quiet_session(session)
        raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
        if not 0 < len(raw) <= 512 * 1024:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        events = [parse_json(line) for line in raw.decode("utf-8").splitlines()]
        expected = {"contract": claim["contract"], "model": claim["model"],
            "prompt_hash": claim["prompts"]["ARTICLE_REPAIR.txt"], "input_hash": claim["input_hash"],
            "old_candidate_hash": fingerprint(snapshot["candidate"]),
            "old_replay_hash": repair_replay_hash(snapshot["candidate"]),
            "feedback_hash": fingerprint(snapshot["feedback"])}
        article = snapshot["article"]
        if ([e.get("phase") for e in events] != ["repair_started", "repair_returned", "repair_failed"]
                or any(e.get("record_id") != record["id"] or e.get("repair_claim") != claim["id"]
                       or e.get("attempt") != 1 or e.get("binding") != expected for e in events)
                or any(type(e.get("at")) not in {int, float} or not math.isfinite(e["at"])
                       or e["at"] < claim["started_at"] for e in events)
                or [e["at"] for e in events] != sorted(e["at"] for e in events)
                or events[0].get("context", {}).get("source_untrusted") != {
                    "title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
                or events[0].get("context", {}).get("candidate_untrusted") != snapshot["candidate"]
                or events[0].get("context", {}).get("feedback_untrusted") != snapshot["feedback"]
                or events[-1].get("error") != code or not isinstance(events[1].get("raw"), str)
                or not events[1]["raw"].strip() or len(events[1]["raw"].encode("utf-8")) > 128 * 1024):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        if saved:
            with self.core.db.session() as session:
                audit = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":quarantine")))
                if (not audit or audit.action != "automatic_article_repair_quarantined"
                        or audit.actor != "automatic_article_repair" or audit.entity != record["id"]
                        or audit.details != {"claim_id": claim["id"], "error": code, "trace_hash": _sha(raw),
                                             "snapshot_hash": claim["snapshot_hash"]}):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        return _sha(raw)

    def authorize(self, record, claim_id, candidate, article):
        """Проверка перехода в штатный prepare после освобождения cycle.lock."""
        history, blocked = self._history(record)
        if blocked and not (blocked == "IN_PROGRESS" and claim_id):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + blocked, 409)
        active = history and history[-1].get("state") in {"correcting", "checking", "unknown", "failed"}
        if claim_id is None:
            if active:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_IN_PROGRESS", 409)
            return None
        if (not history or history[-1].get("id") != claim_id or history[-1].get("state") != "checking"):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        claim = history[-1]
        with self.core.db.session() as session:
            audit = session.get(Audit, claim_id)
            if not audit or any(claim.get(key) != audit.details.get(key) for key in _BINDING):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        if (fingerprint(article) != claim["input_hash"] or fingerprint(candidate) != claim.get("candidate_hash")
                or self._prompts() != claim["prompts"]
                or _sha(inside(self.core.settings.root, record["quality_trace"]).read_bytes()) != claim["parent_trace_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        self._files(record, claim, article, candidate)
        self._quiet(record, self.cycle._policy())
        return claim

    def _files(self, record, claim, article, candidate=None):
        snapshot_raw = inside(self.core.settings.root, claim["snapshot_path"]).read_bytes()
        snapshot = parse_json(snapshot_raw.decode("utf-8"))
        if _sha(snapshot_raw) != claim["snapshot_hash"] or snapshot["article"] != article:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if record.get("input_file"):
            original = inside(self.core.settings.root, record["input_file"]).read_bytes()
            if _sha(original) != record["input_file_hash"] or parse_json(original.decode("utf-8")) != article:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if claim.get("proposal_path"):
            raw = inside(self.core.settings.root, claim["proposal_path"]).read_bytes()
            proposal = parse_json(raw.decode("utf-8"))
            if (_sha(raw) != claim["proposal_hash"] or proposal["evidence"] != claim["evidence"]
                    or candidate is not None and proposal["candidate"] != candidate
                    or fingerprint(proposal["candidate"]) != claim["candidate_hash"]):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if claim.get("repair_trace_hash"):
            raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
            if _sha(raw) != claim["repair_trace_hash"]:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        trace = inside(self.core.settings.root, record["quality_trace"]).read_bytes()
        # Новая проверка только дописывает события; прежний отказ остаётся побайтно.
        prefix = trace[:claim["parent_trace_bytes"]]
        if _sha(prefix) != claim["parent_trace_hash"]:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)

    def assert_complete(self, record, session):
        """Незавершённая правка не получает policy approval даже при известном DRAFT."""
        history, blocked = self._history(record)
        if blocked or history and history[-1]["state"] != "passed":
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + (blocked or "IN_PROGRESS"), 409)
        if not history:
            return
        claim = history[-1]
        rows = list(session.scalars(select(Audit).where(Audit.entity == record["id"],
            Audit.actor == "automatic_article_repair", Audit.action == "automatic_article_repair_passed")))
        if not any(isinstance(row.details, dict) and row.details.get("claim_id") == claim["id"]
                   and row.details.get("prepared_hash") == claim.get("prepared_hash") for row in rows):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        snapshot = parse_json(inside(self.core.settings.root, claim["snapshot_path"]).read_text(encoding="utf-8"))
        self._files(record, claim, snapshot["article"])

    def after_quality(self, record, claim, article):
        source, target = self._quiet(record, self.cycle._policy())
        if (self._prompts() != claim["prompts"] or fingerprint(article) != claim["input_hash"]
                or target.binding_hash != claim["target_binding_hash"] or fingerprint(source) != claim["source_config_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        self._files(record, claim, article)

    def _matching(self, ledger, ident, claim_id):
        record = self.cycle._record(ledger, ident)
        history = record.get("automatic_repair_attempts", [])
        if not history or history[-1].get("id") != claim_id:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        with self.core.db.session() as session:
            audit = session.get(Audit, claim_id)
            if not audit or any(history[-1].get(key) != audit.details.get(key) for key in _BINDING):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        return record, history[-1]

    def run(self, ident):
        cycle, editor, claim = self.cycle, None, None
        with profile_lock(cycle.directory / "cycle.lock"):
            policy, ledger = cycle._policy(), cycle._load()
            record = cycle._record(ledger, ident)
            status = self.status(record)
            if not status["eligible"]:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + status["reason"], 409)
            source, target = self._quiet(record, policy)
            payload = self._payload(record)
            cycle._article(payload["article"], policy, source, target)
            cycle._protocol_source_novel(record, ledger, payload["article"])
            number = status["attempts"] + 1
            claim_id = _claim_id(ident, number)
            path = f"data/hourly-articles/repair-input-{claim_id}.local.json"
            claim = {"id": claim_id, "number": number, "state": "correcting", "started_at": _now(),
                "contract": REPAIR_CONTRACT, "model": REPAIR_MODEL, "slot": record["slot"],
                "input_hash": record["input_hash"], "policy_hash": record["policy_hash"],
                "parent_trace_hash": payload["parent_trace_hash"], "snapshot_path": path,
                "parent_trace_bytes": len(inside(self.core.settings.root, record["quality_trace"]).read_bytes()),
                "snapshot_hash": _write_json(inside(self.core.settings.root, path), payload),
                "prompts": self._prompts(), "target_binding_hash": target.binding_hash,
                "source_config_hash": fingerprint(source),
                "repair_trace": f"data/hourly-articles/repair-{claim_id}.local.jsonl",
                "owner_authorization": "2026-10-22 bounded automatic translation corrections"}
            try:
                with self.core.db.session.begin() as session:
                    require_running(self.core, session, lock=True)
                    self._quiet_session(session)
                    session.add(Audit(id=claim_id, actor="automatic_article_repair", action="automatic_article_repair_claim",
                                      entity=ident, details=copy.deepcopy(claim)))
            except IntegrityError as exc:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409) from exc
            # PK уже зафиксирован: даже падение следующего save не разрешает повтор LLM.
            record.setdefault("automatic_repair_attempts", []).append(claim)
            record["state"] = "preparing"
            cycle._save(ledger)
            expected = fingerprint(record)
            try:
                editor = cycle._repair(self._trace(record, claim))
                proposal = editor.propose(payload["article"], payload["candidate"], payload["feedback"],
                                          rejected_replay_hashes=payload["rejected_replay_hashes"])
                fresh = cycle._load()
                if fingerprint(cycle._record(fresh, ident)) != expected:
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                # _load обновляет CAS revision. Сохраняем свежий ledger целиком,
                # чтобы не стереть изменения других записей старым снимком.
                ledger = fresh
                record, claim = self._matching(ledger, ident, claim_id)
                if (_sha(inside(self.core.settings.root, record["quality_trace"]).read_bytes()) != claim["parent_trace_hash"]
                        or _sha(inside(self.core.settings.root, path).read_bytes()) != claim["snapshot_hash"]):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                self.after_quality(record, claim, payload["article"])
                repair_raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
                repair_events = [parse_json(line) for line in repair_raw.decode("utf-8").splitlines()]
                terminal = "repair_not_repairable" if proposal.get("repairable") is False else "repair_candidate"
                expected_binding = {"contract": REPAIR_CONTRACT, "model": REPAIR_MODEL,
                    "prompt_hash": claim["prompts"]["ARTICLE_REPAIR.txt"], "input_hash": claim["input_hash"],
                    "old_candidate_hash": fingerprint(payload["candidate"]),
                    "old_replay_hash": repair_replay_hash(payload["candidate"]),
                    "feedback_hash": fingerprint(payload["feedback"])}
                times = [e.get("at") for e in repair_events]
                if (not 0 < len(repair_raw) <= 512 * 1024
                        or [e.get("phase") for e in repair_events] != ["repair_started", "repair_returned", terminal]
                        or any(e.get("record_id") != ident or e.get("repair_claim") != claim_id
                               or e.get("attempt") != 1 for e in repair_events)
                        or repair_events[-1].get("result") != proposal
                        or repair_events[1].get("raw") != proposal.get("raw")
                        or _sha(proposal["raw"].encode("utf-8")) != proposal["evidence"].get("raw_sha256")
                        or proposal["evidence"].get("input_hash") != claim["input_hash"]
                        or proposal["evidence"].get("model") != REPAIR_MODEL
                        or proposal["evidence"].get("prompt_hash") != claim["prompts"]["ARTICLE_REPAIR.txt"]
                        or any(type(t) not in {int, float} or not math.isfinite(t) or t < claim["started_at"] for t in times)
                        or times != sorted(times)
                        or repair_events[0].get("binding") != expected_binding
                        or repair_events[1].get("binding") != expected_binding
                        or any(proposal["evidence"].get(k) != v for k, v in expected_binding.items())
                        or repair_events[0].get("context", {}).get("candidate_untrusted") != payload["candidate"]
                        or repair_events[0].get("context", {}).get("feedback_untrusted") != payload["feedback"]):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
                proposal_path = f"data/hourly-articles/repair-proposal-{claim_id}.local.json"
                claim.update(proposal_path=proposal_path,
                    proposal_hash=_write_json(inside(self.core.settings.root, proposal_path), proposal),
                    repair_trace_hash=_sha(repair_raw), evidence=proposal["evidence"])
                if proposal.get("repairable") is False:
                    claim.update(state="not_repairable", reason=proposal["reason"], evidence=proposal["evidence"], finished_at=_now())
                    record["state"] = "needs_attention"
                    cycle._save(ledger)
                    raise AgentError("ARTICLE_QUALITY_REJECTED", 409)
                candidate = proposal["candidate"]
                cycle._validate_correction(candidate)
                cycle._guard_candidate_replay(ledger, record, candidate, "automatic_repair", payload["article"])
                claim.update(state="checking", candidate_hash=fingerprint(candidate),
                             proposal_path=proposal_path, proposal_hash=_write_json(inside(self.core.settings.root, proposal_path), proposal),
                             evidence=proposal["evidence"])
                record["state"] = "needs_attention"
                cycle._save(ledger)
            except Exception as exc:
                if claim["state"] not in {"not_repairable", "no_change"}:
                    code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_REPAIR_INTERRUPTED"
                    safe_no_change = code in {"ARTICLE_REPAIR_UNCHANGED", "ARTICLE_REPAIR_PREVIOUSLY_REJECTED", "ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"}
                    fresh = cycle._load()
                    current, current_claim = self._matching(fresh, ident, claim_id)
                    if fingerprint(current) == expected:
                        current_claim.update(error=code)
                        quarantined = False
                        if code in QUARANTINE_ERRORS:
                            try:
                                hashed = self._quarantine_proof(current, current_claim)
                                with self.core.db.session.begin() as session:
                                    session.add(Audit(id=str(uuid5(NAMESPACE_URL, claim_id + ":quarantine")),
                                        actor="automatic_article_repair", action="automatic_article_repair_quarantined",
                                        entity=ident, details={"claim_id": claim_id, "error": code, "trace_hash": hashed,
                                                               "snapshot_hash": current_claim["snapshot_hash"]}))
                                current_claim.update(outcome_origin="protocol_guard", repair_trace_hash=hashed,
                                    reason="Completed invalid editor response; original rejection retained")
                                quarantined = True
                            except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError):
                                pass
                        current_claim.update(state="not_repairable" if quarantined else "no_change" if safe_no_change else "failed",
                                             finished_at=_now())
                        current.update(state="needs_attention", error="ARTICLE_QUALITY_REJECTED" if safe_no_change or quarantined else code)
                        cycle._save(fresh)
                    # Чужой изменённый ledger не перезаписываем старым снимком.
                    # Незавершённый Audit claim продолжает блокировать повтор.
                raise
            finally:
                if editor is not None and not cycle.repair_factory:
                    editor.editor.client.close()
        # Новый lock и повторная проверка claim обязательны: flock не reentrant.
        try:
            result = cycle.prepare(payload["article"], retry_record=ident, candidate=candidate, automatic_repair_claim=claim_id)
        except Exception as exc:
            with profile_lock(cycle.directory / "cycle.lock"):
                ledger = cycle._load()
                record, claim = self._matching(ledger, ident, claim_id)
                code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_REPAIR_INTERRUPTED"
                from .server_cycle import ServerCycle
                known = code == "ARTICLE_QUALITY_REJECTED" and ServerCycle(self.core, cycle=cycle)._known_rejection(record)
                claim.update(state="rejected" if known else "failed", error=code, finished_at=_now())
                cycle._save(ledger)
            raise
        with profile_lock(cycle.directory / "cycle.lock"):
            ledger = cycle._load()
            record, claim = self._matching(ledger, ident, claim_id)
            claim.update(state="passed", finished_at=_now(), prepared_hash=record["prepared_hash"])
            cycle._save(ledger)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "automatic_article_repair", "automatic_article_repair_passed", ident,
                                   claim_id=claim_id, prepared_hash=record["prepared_hash"])
        return result
