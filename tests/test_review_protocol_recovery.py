"""Only a saved positive advisory-count protocol failure can stage one draft."""
import copy
import hashlib
import json

import httpx
import pytest

from max_postiz_agent.article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT
from max_postiz_agent.article_quality import ArticleQuality, article_review_context
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft, Material, Story
from max_postiz_agent.hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant
from max_postiz_agent.human_review import CandidateOnlyTranslator

pytest_plugins = ["test_automatic_policy"]
REASON = "Repair the advisory-count-only protocol defect while retaining the exact original independent review."
CANDIDATE = {"title": "Космический ветер", "body":
    "Учёные изучили необычные процессы в атмосфере далёкой звезды и обнаружили звёздный ветер."}
REVIEW = {"approved": True, "topic": "science", "issues": [],
          "warnings": ["Первое стилистическое замечание.", "Второе стилистическое замечание.",
                       "Третье стилистическое замечание.", "Четвёртое стилистическое замечание."]}


class UnusedReviewer:
    model = "ministral"

    def __init__(self):
        self.client = self

    def generate_text(self, _):
        pytest.fail("Protocol recovery must never resample Ministral")

    def close(self):
        pass


@pytest.fixture(params=[LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT], ids=["legacy-v1", "exact-v2"])
def protocol_failure(automatic_cycle, monkeypatch, request):
    value, _ = automatic_cycle
    cycle, core, article, records, calls, stamp, policy = value
    reference_contract = request.param
    if reference_contract == LEGACY_REFERENCE_CONTRACT:
        # Исторический V1 закрепляется ДО создания record/attempt. Готовую
        # историю не переписываем под текущий V2 и не ослабляем saved replay.
        monkeypatch.setattr("max_postiz_agent.article_cycle.REFERENCE_CONTRACT", LEGACY_REFERENCE_CONTRACT)

    class InitialFailure:
        def __init__(self):
            self.translator, self.reviewer = CandidateOnlyTranslator(), UnusedReviewer()

        def prepare(self, *args):
            raise AgentError("OPENCODE_GENERATION_FAILED", 503)

        def close(self):
            pass

    monkeypatch.setattr(cycle, "_quality", lambda *args, **kwargs: InitialFailure())
    with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    original = article["title"].strip() + "\n\n" + article["original"].strip()
    text = CANDIDATE["title"] + "\n\n" + CANDIDATE["body"]
    prompt = (core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt").read_text(encoding="utf-8")
    context = article_review_context(prompt, original, text, "meaning-first-v1",
        hashlib.sha256(prompt.encode()).hexdigest(), reference_contract=reference_contract)
    raw_review = "```json\n" + json.dumps(REVIEW, ensure_ascii=False) + "\n```"

    class HistoricalFailure(InitialFailure):
        def check_candidate(self, *args):
            trace = cycle._trace_writer(record["id"])
            for event in [
                {"phase": "candidate_started", "mode": "candidate", "candidate": copy.deepcopy(CANDIDATE),
                 "source_untrusted": {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]},
                 **({"reference_contract": reference_contract} if reference_contract == REFERENCE_CONTRACT else {})},
                {"phase": "gate_passed", "characters": len(text + "\n\nИсточник: " + article["url"] +
                    "\nПеревод и редактура с помощью нейросетей.")},
                {"phase": "review_started", "context": copy.deepcopy(context)},
                {"phase": "review_returned", "raw": raw_review},
                {"phase": "review_invalid", "error": "ARTICLE_REVIEW_SCHEMA_INVALID", "review": copy.deepcopy(REVIEW)},
                {"phase": "rejected", "mode": "candidate", "error": "ARTICLE_QUALITY_REJECTED"},
            ]:
                trace({**event, "attempt": 1})
            raise AgentError("ARTICLE_QUALITY_REJECTED")

        def close(self):
            pass

    monkeypatch.setattr(cycle, "_quality", lambda *args, **kwargs: HistoricalFailure())
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article, retry_record=record["id"], candidate=copy.deepcopy(CANDIDATE))
    record = cycle._load()["records"][0]
    path = core.settings.root / record["quality_trace"]
    state = {"value": value, "record": copy.deepcopy(record), "article": copy.deepcopy(article),
             "candidate": copy.deepcopy(CANDIDATE), "trace": path, "bytes": path.read_bytes(),
             "reference_contract": reference_contract,
             "lt_calls": [], "matches": [], "status": 200, "callback": None}

    def transport(request):
        state["lt_calls"].append(str(request.url))
        if state["callback"]:
            state["callback"]()
        return httpx.Response(state["status"], json={"matches": state["matches"]})

    def quality(ident=None, review_only=False):
        assert review_only is True and ident != record["id"]
        return ArticleQuality(CandidateOnlyTranslator(), UnusedReviewer(),
            prompt_dir=core.settings.root / "prompts", trace=cycle._trace_writer(ident),
            review_rubric="meaning-first-v1", reference_contract=reference_contract,
            transport=httpx.MockTransport(transport))

    monkeypatch.setattr(cycle, "_quality", quality)
    monkeypatch.setattr(core, "approve_hourly_policy", lambda *args, **kwargs: pytest.fail("Recovery creates no approval"))
    return state


def recover(state, **kwargs):
    cycle = state["value"][0]
    return cycle.recover_review_protocol(state["record"]["id"], state["article"], state["candidate"],
        kwargs.get("sha", hashlib.sha256(state["trace"].read_bytes()).hexdigest()), REASON)


def rewrite(state, transform):
    events = [json.loads(line) for line in state["trace"].read_text(encoding="utf-8").splitlines()]
    transform(events)
    state["trace"].write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")


def test_explicit_protocol_recovery_preserves_old_refusal_and_all_warnings_without_new_min(protocol_failure):
    state = protocol_failure
    cycle, core, _, records, calls, _, _ = state["value"]
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        cycle.prepare(state["article"], retry_record=state["record"]["id"], candidate=state["candidate"])
    assert not state["lt_calls"]
    result = recover(state)
    assert result["state"] == "automatic_ready" and len(records) == len(calls) == 1
    assert records[result["postiz_id"]]["state"] == "DRAFT" and len(state["lt_calls"]) == 1
    assert state["trace"].read_bytes() == state["bytes"]
    assert result["preparation_attempts"] == state["record"]["preparation_attempts"]
    assert result["quality_trace"] != state["record"]["quality_trace"]
    assert result["review_protocol_recoveries"][0]["original_error"] == "ARTICLE_QUALITY_REJECTED"
    with core.db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        proof = variant.metadata_json["machine_review_evidence"]
        assert proof["review"] == REVIEW and len(proof["review"]["warnings"]) == 4
        assert not session.query(TargetApproval).count() and not session.query(Dispatch).count()
        assert session.get(Target, "max").paused
        assert session.query(Audit).filter_by(action="hourly_article_review_protocol_started").count() == 1
        assert session.query(Audit).filter_by(action="hourly_article_review_protocol_completed").count() == 1


@pytest.mark.parametrize("change", ["sha", "candidate", "source", "prompt", "policy", "slot", "marker",
                                   "reference_contract", "lt_phase", "prior_verdict"])
def test_unsafe_or_nonexact_protocol_recovery_fails_before_lt_and_claim(protocol_failure, change):
    state = protocol_failure
    cycle, core, _, records, calls, stamp, policy = state["value"]
    sha = None
    if change == "sha":
        sha = "0" * 64
    elif change == "candidate":
        state["candidate"]["body"] += " Изменённый текст."
    elif change == "source":
        state["article"]["original"] += " A new fact."
    elif change == "prompt":
        path = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
        path.write_text(path.read_text(encoding="utf-8") + "\nNew instruction.", encoding="utf-8")
    elif change == "policy":
        (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps({**policy, "owner_instruction": "Changed"}))
    elif change == "slot":
        stamp[0] += 3600
    elif change == "marker":
        ledger = cycle._load()
        ledger["records"][0]["preparation_attempts"][-1].pop("candidate_normalization")
        cycle._save(ledger)
    elif change == "reference_contract":
        def wrong_contract(events):
            first = next(event for event in events if event.get("phase") == "candidate_started")
            first["reference_contract"] = (LEGACY_REFERENCE_CONTRACT
                if state["reference_contract"] == REFERENCE_CONTRACT else REFERENCE_CONTRACT)
        rewrite(state, wrong_contract)
    elif change == "lt_phase":
        rewrite(state, lambda events: events.insert(-1, {**events[-1], "phase": "spelling_started"}))
    else:
        def add_prior(events):
            previous = copy.deepcopy(events[2])
            previous["at"] -= 0.1
            events.insert(0, previous)
        rewrite(state, add_prior)
    with pytest.raises(AgentError):
        recover(state, **({"sha": sha} if sha else {}))
    assert not state["lt_calls"] and not records and not calls
    with core.db.session() as session:
        assert not session.query(Audit).filter_by(action="hourly_article_review_protocol_started").count()


@pytest.mark.parametrize("value", [False, "true", "issues", "other", 3, 9])
def test_protocol_recovery_cannot_invent_or_convert_a_verdict(protocol_failure, value):
    state = protocol_failure
    def change(events):
        review = copy.deepcopy(REVIEW)
        if value in {False, "true"}:
            review["approved"] = value
        elif value == "issues":
            review["issues"] = ["A substantive issue remains."]
        elif value == "other":
            review["topic"] = "other"
        else:
            review["warnings"] = ["Advisory"] * value
        events[3]["raw"] = json.dumps(review)
        events[4]["review"] = review
    rewrite(state, change)
    with pytest.raises(AgentError):
        recover(state)
    assert not state["lt_calls"] and not state["value"][3]


@pytest.mark.parametrize("failure", ["unavailable", "grammar", "hour", "source_rights", "postiz_unknown"])
def test_failure_after_durable_claim_never_retries_the_same_protocol_recovery(protocol_failure, monkeypatch, failure):
    state = protocol_failure
    cycle, core, _, records, calls, stamp, _ = state["value"]
    if failure == "unavailable":
        state["status"] = 503
    elif failure == "grammar":
        state["matches"] = [{"message": "An unresolved grammar error", "rule": {
            "id": "GRAMMAR_ERROR", "issueType": "grammar", "category": {"id": "GRAMMAR"}}}]
    elif failure == "hour":
        state["callback"] = lambda: stamp.__setitem__(0, stamp[0] + 3600)
    elif failure == "source_rights":
        state["callback"] = lambda: core.settings.source("example_rss")["rights"].update(verified=False)
    else:
        def unknown(*args, **kwargs):
            calls.append("uncertain")
            raise AgentError("POSTIZ_UNKNOWN", 503)
        monkeypatch.setattr(core.postiz, "create", unknown)
    with pytest.raises(AgentError):
        recover(state)
    assert len(state["lt_calls"]) == 1 and state["trace"].read_bytes() == state["bytes"]
    with core.db.session() as session:
        assert session.query(Audit).filter_by(action="hourly_article_review_protocol_started").count() == 1
        assert not session.query(TargetApproval).count() and not session.query(Dispatch).count()
        if failure == "postiz_unknown":
            assert session.query(RemoteDraft).one().state == "unknown"
        else:
            assert not session.query(Variant).count()
    with pytest.raises(AgentError):
        recover(state)
    assert len(state["lt_calls"]) == 1 and (calls == ["uncertain"] if failure == "postiz_unknown" else not calls)
    assert not records


def test_database_claim_survives_removed_local_claim_history(protocol_failure):
    state = protocol_failure
    cycle = state["value"][0]
    state["status"] = 503
    with pytest.raises(AgentError):
        recover(state)
    ledger = cycle._load()
    ledger["records"][0].pop("review_protocol_recoveries")
    cycle._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_ALREADY_ATTEMPTED"):
        recover(state)
    assert len(state["lt_calls"]) == 1


@pytest.mark.parametrize("existing", ["record_link", "story", "lease", "active"])
def test_existing_external_or_ambiguous_state_blocks_protocol_recovery(protocol_failure, existing):
    state = protocol_failure
    cycle, core, _, _, _, stamp, _ = state["value"]
    if existing == "record_link":
        ledger = cycle._load()
        ledger["records"][0]["postiz_id"] = "already-staged"
        cycle._save(ledger)
    else:
        with core.db.session.begin() as session:
            story = Story(title="Existing", text="Existing source", text_hash=fingerprint({
                "hourly_article": state["record"]["id"]}) if existing == "story" else "other-story-hash")
            session.add(story)
            session.flush()
            if existing != "story":
                draft = Draft(channel_id="main", story_id=story.id, current={}, config_id=core.settings.hash)
                session.add(draft)
                session.flush()
                target = session.get(Target, "max")
                variant = Variant(draft_id=draft.id, target_id="max", body="Other", source_revision=1,
                    config_hash=core.settings.hash, binding_hash=target.binding_hash)
                session.add(variant)
                session.flush()
                if existing == "lease":
                    session.add(BridgePost(post_id="other-post", integration_id="integration-max", group_id="other-group",
                        target_id="max", variant_id=variant.id, remote_hash="0" * 64, remote_state="DRAFT", due_at=stamp[0],
                        mutation_owner=None, mutation_until=stamp[0] - 1))
                else:
                    approval = TargetApproval(variant_id=variant.id, revision=1, snapshot_hash="0" * 64,
                        scheduled_at=stamp[0], actor="editor")
                    session.add(approval)
                    session.flush()
                    session.add(Dispatch(variant_id=variant.id, approval_id=approval.id, revision=1,
                        snapshot_hash="0" * 64, snapshot={}, due_at=stamp[0], state="unknown"))
    with pytest.raises(AgentError):
        recover(state)
    assert not state["lt_calls"]


@pytest.mark.parametrize("kind", ["url", "hash"])
@pytest.mark.parametrize("timing", ["before", "during_lt"])
def test_archive_source_dedup_is_rechecked_before_claim_and_before_staging(protocol_failure, kind, timing):
    state = protocol_failure
    core = state["value"][1]

    def archive():
        with core.db.session.begin() as session:
            story = Story(title="Other archive", text="Other archive text", text_hash="other-archive-story")
            session.add(story)
            session.flush()
            session.add(Material(source_id="different_source", external_id="archived-elsewhere", story_id=story.id,
                title="Archived source", text=state["article"]["original"],
                url=state["article"]["url"] if kind == "url" else "https://science.example.org/another-url/",
                text_hash=state["record"]["original_hash"] if kind == "hash" else "other-archive-content-hash",
                published_at=state["article"]["published_at"], language="en"))

    if timing == "before":
        archive()
    else:
        state["callback"] = archive
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        recover(state)
    assert len(state["lt_calls"]) == (0 if timing == "before" else 1)
    assert not state["value"][3] and not state["value"][4]
    with core.db.session() as session:
        assert session.query(Material).count() == 1 and not session.query(Variant).count()
        assert session.query(Audit).filter_by(action="hourly_article_review_protocol_started").count() == (0 if timing == "before" else 1)


@pytest.mark.parametrize("kind", ["url", "hash"])
def test_other_cancelled_record_source_is_still_a_duplicate(protocol_failure, kind):
    state = protocol_failure
    cycle = state["value"][0]
    ledger = cycle._load()
    ledger["records"].append({"id": "another-claim", "slot": state["record"]["slot"] - 1, "state": "cancelled",
        **({"url": state["article"]["url"]} if kind == "url" else {"original_hash": state["record"]["original_hash"]})})
    cycle._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        recover(state)
    assert not state["lt_calls"]


@pytest.mark.parametrize("kind", ["unknown", "staging", "backlog"])
@pytest.mark.parametrize("timing", ["before", "during_lt"])
def test_full_other_record_state_is_rechecked_before_claim_and_before_store(protocol_failure, kind, timing):
    state = protocol_failure
    cycle = state["value"][0]

    def add_waiting():
        ledger = cycle._load()
        count = 24 if kind == "backlog" else 1
        for index in range(count):
            ledger["records"].append({"id": "other-" + str(index), "slot": state["record"]["slot"] - index - 1,
                "state": "automatic_ready" if kind == "backlog" else "staging_automatic_draft" if kind == "staging" else "unknown",
                "automatic_enrolled": kind == "backlog"})
        cycle._save(ledger)

    if timing == "before":
        add_waiting()
    else:
        state["callback"] = add_waiting
    with pytest.raises(AgentError, match="ARTICLE_(AUTOMATIC_BACKLOG|RECONCILIATION_REQUIRED)"):
        recover(state)
    assert len(state["lt_calls"]) == (0 if timing == "before" else 1)
    assert not state["value"][3] and not state["value"][4]
    # A newly observed foreign record must not be overwritten by a stale ledger.
    assert len(cycle._load()["records"]) == (25 if kind == "backlog" else 2)
