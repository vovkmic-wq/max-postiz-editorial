"""V8 clarifies real LT repairs; syntax, refusal and independent QA stay strict."""
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.article_links import REFERENCE_CONTRACT
from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.article_repair import (
    ArticleRepair, REPAIR_CONTRACT, repair_context, repair_prompt_name, repair_reference_contract,
)
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.human_review import CandidateOnlyTranslator
from test_article_repair import ARTICLE, CANDIDATE, Editor


PROPOSED = {**CANDIDATE, "body": CANDIDATE["body"].replace("Учёные изучила", "Учёные исследовали")}
FEEDBACK = {"review": {"approved": True, "topic": "science", "issues": [],
    "warnings": ["An advisory uses a different preferred style.", "The number 2 is missing, although it is present."]},
    "spelling_matches": [{"message": "Неверное согласование", "offset": CANDIDATE["body"].index("изучила"),
        "length": len("изучила"), "rule": {"id": "SUBJECT_VERB_AGREEMENT", "issueType": "grammar",
            "category": {"id": "GRAMMAR"}}, "type": {"typeName": "Other"}}]}


def repair(output=PROPOSED, *, contract="article-repair-v8", blocked=()):
    events, editor = [], Editor(copy.deepcopy(output))
    result = ArticleRepair(editor, trace=events.append, contract=contract).propose(
        ARTICLE, CANDIDATE, FEEDBACK, rejected_replay_hashes=blocked)
    return result, editor, events


def test_v8_changes_only_the_instruction_and_preserves_full_mixed_feedback_and_reference_context():
    assert REPAIR_CONTRACT == "article-repair-v8"
    assert repair_prompt_name(REPAIR_CONTRACT) == "ARTICLE_REPAIR_V8.txt"
    assert repair_reference_contract(REPAIR_CONTRACT) == REFERENCE_CONTRACT
    assert repair_context(ARTICLE, CANDIDATE, FEEDBACK, contract="article-repair-v8") == repair_context(
        ARTICLE, CANDIDATE, FEEDBACK, contract="article-repair-v7")
    result, editor, events = repair()
    assert len(editor.calls) == 1
    context = json.loads(editor.calls[0].split("\n\nUNTRUSTED_JSON_DATA:\n", 1)[1])
    assert context["feedback_untrusted"] == FEEDBACK
    assert context["source_untrusted"]["body"] == ARTICLE["original"]
    assert events[0]["binding"]["feedback_hash"] == fingerprint(FEEDBACK)
    assert events[0]["binding"]["reference_contract"] == REFERENCE_CONTRACT
    assert result["candidate"] == PROPOSED
    assert result["evidence"]["contract"] == "article-repair-v8"
    assert result["raw"] == events[1]["raw"] == json.dumps(PROPOSED, ensure_ascii=False)
    assert [event["phase"] for event in events] == ["repair_started", "repair_returned", "repair_candidate"]
    assert not {"approved", "quality_passed", "machine_review_status", "postiz_id", "dispatch_id"} & set(result)


def test_v8_prompt_distinguishes_real_grammar_from_advisories_without_forbidding_honest_rare_term_refusal():
    prompt = (Path(__file__).resolve().parents[1] / "prompts/ARTICLE_REPAIR_V8.txt").read_text(encoding="utf-8")
    assert "предупреждение Ministral не отменяет реальную независимую ошибку" in prompt
    assert "нарушенное согласование и неграмматичная фраза" in prompt
    assert "допустима грамматичная русская перефразировка" in prompt
    assert "сохрани все факты, имена, числа, единицы, отрицания" in prompt
    assert "Корректный редкий термин нельзя" in prompt and "допустим честный not_repairable" in prompt
    assert "свежие независимые Ministral и LanguageTool" in prompt
    assert "«Хоралек» → «Хорáлек» недопустимо" in prompt


def test_v8_accepts_a_russian_paraphrase_of_a_malformed_word_with_full_lt_and_false_warning_retained():
    previous = {**PROPOSED, "body": PROPOSED["body"].replace("исследовали звёздный ветер", "занялись ветроизучением")}
    feedback = copy.deepcopy(FEEDBACK)
    feedback["spelling_matches"] = [{"message": "Неизвестное слово", "offset": previous["body"].index("ветроизучением"),
        "length": len("ветроизучением"), "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling",
            "category": {"id": "TYPOS"}}, "type": {"typeName": "UnknownWord"}}]
    editor, events = Editor(PROPOSED), []
    result = ArticleRepair(editor, contract="article-repair-v8", trace=events.append).propose(
        ARTICLE, previous, feedback)
    assert result["candidate"] == PROPOSED and len(editor.calls) == 1
    assert events[0]["context"]["feedback_untrusted"] == feedback
    assert events[0]["binding"]["feedback_hash"] == fingerprint(feedback)
    assert "2 измерений" in result["candidate"]["body"] and "не прекратился" in result["candidate"]["body"]
    assert "approved" not in result and events[-1]["phase"] == "repair_candidate"


@pytest.mark.parametrize("failure", [None, "meaning", "grammar"])
def test_v8_paraphrase_is_only_a_candidate_and_still_requires_fresh_independent_qa(failure):
    proposed, _, _ = repair()
    reviews, spelling = [], []

    class Reviewer:
        model = "independent-ministral"

        def generate_text(self, prompt):
            reviews.append(json.loads(prompt))
            return json.dumps({"approved": failure != "meaning", "topic": "science", "warnings": [],
                "issues": ["A source relationship is changed."] if failure == "meaning" else []}), {}

    def check(request):
        spelling.append(request)
        matches = [{"message": "Реальная ошибка согласования", "offset": 0, "length": 6,
            "rule": {"id": "SUBJECT_VERB_AGREEMENT", "issueType": "grammar", "category": {"id": "GRAMMAR"}},
            "type": {"typeName": "Other"}}] if failure == "grammar" else []
        return httpx.Response(200, json={"matches": matches})

    quality = ArticleQuality(CandidateOnlyTranslator(), Reviewer(), review_rubric="meaning-first-v1",
        reference_contract=REFERENCE_CONTRACT, transport=httpx.MockTransport(check))
    try:
        if failure is None:
            result = quality.check_candidate(**proposed["candidate"], original_title=ARTICLE["title"],
                original=ARTICLE["original"], url=ARTICLE["url"])
            assert result["machine_review_status"] == "passed"
        else:
            with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
                quality.check_candidate(**proposed["candidate"], original_title=ARTICLE["title"],
                    original=ARTICLE["original"], url=ARTICLE["url"])
    finally:
        quality.close()
    assert len(reviews) == len(spelling) == 1
    assert reviews[0]["source_untrusted"] == ARTICLE["title"] + "\n\n" + ARTICLE["original"]
    assert reviews[0]["translation_untrusted"] == PROPOSED["title"] + "\n\n" + PROPOSED["body"]


def test_v8_honest_not_repairable_is_preserved_once_without_becoming_a_pass():
    output = {"repairable": False,
        "reason": "Корректный редкий термин нельзя безопасно заменить без изменения исходного смысла."}
    result, editor, events = repair(output)
    assert result["repairable"] is False and result["reason"] == output["reason"]
    assert result["evidence"]["outcome"] == "not_repairable"
    assert len(editor.calls) == 1
    assert events[-1]["phase"] == "repair_not_repairable" and "candidate" not in result
    assert result["raw"] == events[1]["raw"]


@pytest.mark.parametrize("damage,error", [
    ("number", "TRANSLATION_NUMBER_CHANGED"), ("unchanged", "ARTICLE_REPAIR_UNCHANGED"),
    ("reference", "TRANSLATION_REFERENCE_CHANGED"),
    ("english", "TRANSLATION_NOT_RUSSIAN"), ("mixed_latin", "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"),
])
def test_v8_advisory_clarification_never_weakens_existing_hard_or_replay_guards(damage, error):
    output = copy.deepcopy(PROPOSED)
    if damage == "number":
        output["body"] = output["body"].replace("2", "3")
    elif damage == "unchanged":
        output = copy.deepcopy(CANDIDATE)
    elif damage == "english":
        output = {"title": "A detailed stellar wind observation from a distant star", "body": ARTICLE["original"]}
    elif damage == "reference":
        output["body"] += " https://example.org/result/2/"
    else:
        output["body"] = output["body"].replace("Учёные", "Учёнaе")
    events, editor = [], Editor(output)
    with pytest.raises(AgentError, match=error):
        ArticleRepair(editor, trace=events.append, contract="article-repair-v8").propose(ARTICLE, CANDIDATE, FEEDBACK)
    assert len(editor.calls) == 1 and events[-1]["phase"] == "repair_failed"
    assert events[1]["raw"] == json.dumps(output, ensure_ascii=False)


@pytest.mark.parametrize("contract,prompt,hashed", [
    ("article-repair-v3", "ARTICLE_REPAIR.txt", "9a8a3543e63173d6a51d81153561cebea0ba726b0e9e22f347f8a0c8c8fc5040"),
    ("article-repair-v4", "ARTICLE_REPAIR.txt", "9a8a3543e63173d6a51d81153561cebea0ba726b0e9e22f347f8a0c8c8fc5040"),
    ("article-repair-v5", "ARTICLE_REPAIR_V5.txt", "80102f3d47a88e3e6589284ea45b755b64dbb7586a3885ee7d4c48413fd52ad0"),
    ("article-repair-v6", "ARTICLE_REPAIR_V6.txt", "6aac286e48fe749391b49255373a1ff5067fd914086e70b48a935bebbe5b7209"),
    ("article-repair-v7", "ARTICLE_REPAIR_V7.txt", "525204a8788396bbbc8932138c72cbd7653c2b833e97490fdad3824d86233122"),
])
def test_every_historical_contract_keeps_its_exact_prompt_and_raw_replay(contract, prompt, hashed):
    first, editor, events = repair(contract=contract)
    assert repair_prompt_name(contract) == prompt
    assert first["evidence"]["prompt_hash"] == hashed
    assert hashlib.sha256(editor.calls[0].split("\n\nUNTRUSTED_JSON_DATA:\n", 1)[0].encode()).hexdigest() == hashed
    replay_events, replay_editor = [], Editor(events[1]["raw"])
    replay = ArticleRepair(replay_editor, trace=replay_events.append, contract=contract).propose(
        ARTICLE, CANDIDATE, FEEDBACK)
    assert replay == first and replay_events == events and replay_editor.calls == editor.calls
