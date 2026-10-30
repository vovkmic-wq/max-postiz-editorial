"""Механическая оценка сохранённого стенда: без моделей, БД и реального HTTP."""

import base64
import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.reddit import text_hash


@pytest.fixture
def checker():
    path = Path(__file__).resolve().parents[1] / "deployment/translation/check_article_benchmark.py"
    spec = importlib.util.spec_from_file_location("article_benchmark_quality_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample(tmp_path, checker):
    data = tmp_path / "data"
    data.mkdir()
    articles = []
    for number in range(1, 21):
        ident = f"A{number:02}"
        body = (f"The source {ident} measured 2.5 km. The researchers studied faint stars and gas. "
                "See https://science.example.org/study for the scientific report.")
        article = {"id": ident, "source_id": "test_science", "title": "Stars " + ident,
                   "original": body, "url": "https://science.example.org/" + ident,
                   "published_at": 1792000000, "text_hash": text_hash(body),
                   "word_count": len(body.split()), "provenance": {"kind": "full synthetic fixture"}}
        article["source_hash"] = fingerprint({"title": article["title"], "body": body, "url": article["url"]})
        article["original_sha256"] = checker.sha(body.encode())
        articles.append(article)
    corpus = data / "corpus.local.json"
    corpus.write_text(json.dumps({"schema_version": "translation-comparison-corpus-v1", "articles": articles}),
                      encoding="utf-8")
    header = {"kind": "header", "contract": {"id": "translation-articles-benchmark-v1",
              "corpus_sha256": checker.sha(corpus.read_bytes()), "routes_sha256": "a" * 64,
              "adapter_sha256": "b" * 64, "harness_sha256": "c" * 64},
              "profiles": [{"id": route} for route in checker.ROUTES], "test_only": True}
    journal, output = data / "results.local.jsonl", data / "quality.local.json"
    value = {"root": tmp_path, "corpus": corpus, "journal": journal, "output": output,
             "articles": articles, "events": [header]}
    write_journal(checker, value)
    return value


def candidate(ident="A01", **changes):
    return {"title": "Звёзды " + ident,
            "body": (f"Источник {ident} измерил 2.5 км. Исследователи изучали слабые звёзды и газ. "
                     "Научный отчёт доступен по ссылке https://science.example.org/study ."), **changes}


def write_journal(checker, sample):
    previous = None
    for index, event in enumerate(sample["events"], 1):
        event.update(sequence=index, previous_sha256=previous, at="2026-10-23T12:00:00+00:00")
        event["event_sha256"] = fingerprint({k: v for k, v in event.items() if k != "event_sha256"})
        previous = event["event_sha256"]
    sample["journal"].write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sample["events"]),
                                 encoding="utf-8")


def add_task(checker, sample, *, route="hy_current", ident="A01", translated=None,
             failure=None, uncertain=False, terminal=True):
    source = next(a for a in sample["articles"] if a["id"] == ident)
    binding = {"task": route + ":" + ident, "route": route, "article": ident}
    sample["events"].append({"kind": "task_started", **binding, "source_hash": source["source_hash"]})
    if terminal:
        if failure:
            sample["events"].append({"kind": "task_failed", **binding, "error_code": failure,
                                     "known_model_failure": not uncertain, "uncertain": uncertain})
        else:
            translated = translated or candidate(ident)
            raw = json.dumps(translated, ensure_ascii=False)
            sample["events"].append({"kind": "task_completed", **binding, "adapter_output": translated,
                "adapter_output_json": raw, "adapter_output_sha256": checker.sha(raw.encode())})
    write_journal(checker, sample)


def fake_lt(calls, *, matches=None, status=200, interruption=False):
    def handle(request):
        calls.append((str(request.url), parse_qs(request.content.decode())))
        if interruption:
            raise KeyboardInterrupt
        return httpx.Response(status, json={"matches": matches or [], "software": {"version": "fake-current"},
                                           "private_extra": "full raw must survive"})
    return httpx.MockTransport(handle)


def execute(checker, sample, calls, *, resume=False, **lt_options):
    return checker.run(sample["root"], sample["corpus"], sample["journal"], sample["output"],
                       resume=resume, transport=fake_lt(calls, **lt_options))


def report(sample):
    return json.loads(sample["output"].read_text(encoding="utf-8"))


def evaluated(sample):
    return [e for e in report(sample)["events"] if e["kind"] == "task_evaluated"]


def test_good_candidate_uses_exact_production_gate_and_fixed_lt_without_semantic_approval(checker, sample):
    add_task(checker, sample)
    calls = []
    result = execute(checker, sample, calls)
    row = evaluated(sample)[0]
    assert row["hard_gates"] == {"passed": True, "error": None}
    assert row["mechanical_checks_passed"] is True and row["production_fit"]["fits"] is True
    assert row["references"]["contract"] == "article-links-v2"
    assert row["candidate_hash"] == fingerprint(candidate())
    assert calls == [("http://127.0.0.1:8010/v2/check",
                      {"language": ["ru-RU"], "text": [candidate()["title"] + "\n\n" + candidate()["body"]]})]
    assert result["partial"] is True and result["expected_tasks"] == 80 and result["terminal_tasks"] == 1
    assert row["semantic_evaluation_pending"] is True
    assert not {"approved", "ministral_approved", "semantic_approved"} & row.keys()
    assert all(result[k] == 0 for k in ("model_requests", "database_mutations", "publication_mutations"))


@pytest.mark.parametrize("mutation,expected,diagnostic", [
    (lambda text: text.replace("2.5", "3.5"), "TRANSLATION_NUMBER_CHANGED", "numbers"),
    (lambda text: text.replace("2.5", "2,5"), "TRANSLATION_NUMBER_CHANGED", "numbers"),
    (lambda text: text.replace("https://science.example.org/study", ""), "TRANSLATION_REFERENCE_CHANGED", "references"),
    (lambda text: text.replace("/study", "/different"), "TRANSLATION_REFERENCE_CHANGED", "references"),
])
def test_missing_urls_and_changed_numbers_still_fail(checker, sample, mutation, expected, diagnostic):
    translated = candidate()
    translated["body"] = mutation(translated["body"])
    add_task(checker, sample, translated=translated)
    execute(checker, sample, [])
    row = evaluated(sample)[0]
    assert row["hard_gates"] == {"passed": False, "error": expected}
    assert row["mechanical_checks_passed"] is False
    assert row[diagnostic]["missing"] and row["language_tool"]["status"] == "checked"


def test_raw_lt_grammar_match_is_preserved_and_blocks_without_becoming_style(checker, sample):
    add_task(checker, sample)
    match = {"message": "Грамматическая ошибка", "offset": 10, "length": 6,
             "context": {"text": "Исследователи изучали"}, "replacements": [{"value": "исправление"}],
             "rule": {"id": "SUBJECT_VERB_AGREEMENT", "issueType": "grammar", "category": {"id": "GRAMMAR"}},
             "type": {"typeName": "Other"}}
    execute(checker, sample, [], matches=[match])
    saved, row = report(sample), evaluated(sample)[0]
    raw = next(e["result"] for e in saved["events"] if e["kind"] == "lt_completed")
    decoded = json.loads(base64.b64decode(raw["raw_response_base64"]))
    assert decoded["matches"] == [match] and decoded["private_extra"] == "full raw must survive"
    assert raw["raw_sha256"] == checker.sha(base64.b64decode(raw["raw_response_base64"]))
    assert row["language_tool"]["matches_raw"] == row["language_tool"]["unresolved"] == [match]
    assert row["language_tool"]["adjudicated"] == [] and row["mechanical_checks_passed"] is False


def test_known_style_remains_audited_but_grammar_is_not_discarded(checker, sample):
    add_task(checker, sample)
    style = {"message": "Стиль", "rule": {"id": "STYLE", "issueType": "style", "category": {"id": "STYLE"}},
             "type": {"typeName": "Style"}}
    execute(checker, sample, [], matches=[style])
    lt = evaluated(sample)[0]["language_tool"]
    assert lt["matches_raw"] == [style] and not lt["unresolved"]
    assert lt["adjudicated"][0]["match"] == style


def test_long_post_only_fails_production_fit_and_footer_is_counted(checker, sample):
    translated = candidate()
    translated["body"] += " Исследователи наблюдали слабые звёзды и межзвёздный газ." * 80
    add_task(checker, sample, translated=translated)
    execute(checker, sample, [])
    row = evaluated(sample)[0]
    assert row["hard_gates"]["passed"] and row["mechanical_checks_passed"] is True
    assert row["production_fit"]["fits"] is False
    full = translated["title"] + "\n\n" + translated["body"] + "\n\n" + checker.article_footer(sample["articles"][0]["url"])
    assert row["production_fit"]["characters"] == len(full)


def test_resume_and_shared_exact_candidate_never_repeat_lt(checker, sample):
    add_task(checker, sample)
    calls = []
    execute(checker, sample, calls)
    prefix = copy.deepcopy(report(sample)["events"])
    add_task(checker, sample, route="hy_glossary")
    execute(checker, sample, calls, resume=True)
    execute(checker, sample, calls, resume=True)
    assert len(calls) == 1 and len(evaluated(sample)) == 2
    assert report(sample)["events"][:len(prefix)] == prefix


def test_source_bound_spelling_is_recomputed_even_when_raw_ru_cache_is_shared(checker, sample, monkeypatch):
    add_task(checker, sample)
    add_task(checker, sample, route="hy_glossary", ident="A02", translated=candidate())
    original, sources = checker.rubric_spelling, []

    def observe(source, translated, matches, rubric, **options):
        sources.append(source)
        return original(source, translated, matches, rubric, **options)

    monkeypatch.setattr(checker, "rubric_spelling", observe)
    calls = []
    execute(checker, sample, calls)
    assert len(calls) == 1 and len(sources) == 2 and sources[0] != sources[1]


def test_interrupted_lt_claim_is_durable_and_unknown_is_not_retried(checker, sample):
    add_task(checker, sample)
    calls = []
    with pytest.raises(KeyboardInterrupt):
        execute(checker, sample, calls, interruption=True)
    assert [e["kind"] for e in report(sample)["events"]] == ["input_snapshot", "lt_started"]
    result = execute(checker, sample, calls, resume=True)
    assert len(calls) == 1 and result["partial"] is True
    row = evaluated(sample)[0]
    assert row["language_tool"]["status"] == "unknown"
    assert row["language_tool"]["error"] == "LANGUAGETOOL_OUTCOME_UNKNOWN"
    assert row["mechanical_checks_passed"] is None


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_lt_failure_stays_unverified_and_is_not_retried(checker, sample, status):
    add_task(checker, sample)
    calls = []
    execute(checker, sample, calls, status=status)
    result = execute(checker, sample, calls, resume=True)
    assert len(calls) == 1 and result["lt_unverified_tasks"] == ["hy_current:A01"]
    assert evaluated(sample)[0]["language_tool"]["status"] == "unavailable"


@pytest.mark.parametrize("target", ["corpus", "journal", "output"])
def test_all_content_and_outputs_must_be_inside_private_data(checker, sample, target):
    args = {key: sample[key] for key in ("root", "corpus", "journal", "output")}
    public = sample["root"] / "public.json"
    if target != "output":
        public.write_bytes(sample[target].read_bytes())
    args[target] = public
    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_PRIVATE_PATH_REQUIRED"):
        checker.run(args["root"], args["corpus"], args["journal"], args["output"], transport=fake_lt([]))
    assert not sample["output"].exists()


@pytest.mark.parametrize("field", ["source_hash", "adapter_output_sha256", "candidate_hash"])
def test_hash_mismatch_blocks_before_language_tool(checker, sample, field):
    add_task(checker, sample)
    event = sample["events"][1 if field == "source_hash" else 2]
    event[field] = "f" * 64
    write_journal(checker, sample)
    calls = []
    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_(SOURCE|CANDIDATE)_HASH_MISMATCH"):
        execute(checker, sample, calls)
    assert not calls and not sample["output"].exists()


def test_broken_hashchain_is_not_a_valid_saved_result(checker, sample):
    add_task(checker, sample)
    sample["events"][1]["source_hash"] = "f" * 64
    sample["journal"].write_text("".join(json.dumps(e) + "\n" for e in sample["events"]), encoding="utf-8")
    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_JOURNAL_INVALID"):
        execute(checker, sample, [])


def test_resume_rejects_rewritten_history_even_with_a_recomputed_valid_upstream_chain(checker, sample):
    add_task(checker, sample)
    calls = []
    execute(checker, sample, calls)
    sample["events"][2]["extra"] = "rewritten"
    write_journal(checker, sample)
    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED"):
        execute(checker, sample, calls, resume=True)
    assert len(calls) == 1


def test_changed_input_during_lt_stops_before_next_http(checker, sample):
    add_task(checker, sample)
    add_task(checker, sample, ident="A02")
    calls, normal = [], fake_lt([])

    def change_file(request):
        calls.append(str(request.url))
        response = normal.handle_request(request)
        sample["corpus"].write_bytes(sample["corpus"].read_bytes() + b" ")
        return response

    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_INPUT_HISTORY_CHANGED"):
        checker.run(sample["root"], sample["corpus"], sample["journal"], sample["output"],
                    transport=httpx.MockTransport(change_file))
    assert len(calls) == 1


def test_all_80_known_model_failures_are_measured_terminal_coverage_without_lt(checker, sample):
    for route in checker.ROUTES:
        for source in sample["articles"]:
            add_task(checker, sample, route=route, ident=source["id"], failure="MODEL_OUTPUT_TRUNCATED")
    calls = []
    result = execute(checker, sample, calls)
    assert not calls and result["coverage_complete"] and result["partial"] is False
    assert result["translation_failed"] == result["terminal_tasks"] == result["evaluated_tasks"] == 80
    assert result["translation_completed"] == 0 and result["semantic_evaluation_pending"] is True
    assert all(e["known_model_failure"] and e["candidate_hash"] is None for e in evaluated(sample))


def test_open_and_uncertain_upstream_tasks_are_partial_without_model_or_lt_calls(checker, sample):
    add_task(checker, sample, terminal=False)
    add_task(checker, sample, ident="A02", failure="MODEL_OUTCOME_UNKNOWN", uncertain=True)
    calls = []
    result = execute(checker, sample, calls)
    assert not calls and result["partial"] and not result["coverage_complete"]
    assert result["terminal_tasks"] == result["evaluated_tasks"] == 1
    assert result["uncertain_tasks"] == ["hy_current:A02"] and len(result["missing_terminal_tasks"]) == 79


def test_all_80_completed_tasks_check_every_exact_candidate_and_preserve_coverage(checker, sample):
    for route in checker.ROUTES:
        for source in sample["articles"]:
            add_task(checker, sample, route=route, ident=source["id"])
    calls = []
    result = execute(checker, sample, calls)
    assert len(calls) == 20 and result["translation_completed"] == result["evaluated_tasks"] == 80
    assert result["coverage_complete"] is True and result["partial"] is False
    assert all(e["mechanical_checks_passed"] and e["semantic_evaluation_pending"] for e in evaluated(sample))


def test_80_infrastructure_failures_do_not_become_complete_measured_model_failures(checker, sample):
    for route in checker.ROUTES:
        for source in sample["articles"]:
            add_task(checker, sample, route=route, ident=source["id"], failure="HTTPStatusError")
    for event in sample["events"]:
        if event["kind"] == "task_failed":
            event["known_model_failure"] = False
    write_journal(checker, sample)
    result = execute(checker, sample, [])
    assert result["coverage_complete"] and result["partial"] and len(result["upstream_unverified_tasks"]) == 80


@pytest.mark.parametrize("field", ["raw_sha256", "matches_raw", "response_json", "text_sha256",
                                   "candidate_hash", "hard_gates", "production_fit"])
def test_resume_validates_saved_raw_cache_and_evaluation_even_after_report_rehash(checker, sample, field):
    add_task(checker, sample)
    calls = []
    execute(checker, sample, calls)
    saved = report(sample)
    if field in {"raw_sha256", "matches_raw", "response_json"}:
        target = next(e["result"] for e in saved["events"] if e["kind"] == "lt_completed")
    elif field == "text_sha256":
        target = next(e for e in saved["events"] if e["kind"] == "lt_started")
    else:
        target = next(e for e in saved["events"] if e["kind"] == "task_evaluated")
    target[field] = {"tampered": True} if field in {"response_json", "hard_gates", "production_fit"} else "f" * 64
    previous = None
    for event in saved["events"]:
        event["previous_sha256"] = previous
        event["event_sha256"] = fingerprint({k: v for k, v in event.items() if k != "event_sha256"})
        previous = event["event_sha256"]
    checker.save_report(sample["output"], saved)
    with pytest.raises(AgentError, match="BENCHMARK_QUALITY_REPORT_INVALID"):
        execute(checker, sample, calls, resume=True)
    assert len(calls) == 1


def test_real_benchmark_runner_journal_is_consumed_without_another_translation(checker, sample):
    path = Path(__file__).resolve().parents[1] / "deployment/translation/benchmark_articles.py"
    spec = importlib.util.spec_from_file_location("quality_actual_benchmark_under_test", path)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    routes = []
    for number, ident in enumerate(bench.ROUTES):
        routes.append({"id": ident, "base_url": f"http://127.0.0.1:{9100 + number}",
            "model": bench.HY_MODEL if ident.startswith("hy_") else ident,
            "profile": {"backend_identity": "same-hy" if ident.startswith("hy_") else ident,
                "sampling": {"temperature": 0}, "gateway_numeric_restore": False,
                "prompt_sha256": str(number + 1) * 64, "cache_policy": "retained",
                **({"glossary_sha256": "a" * 64} if ident == "hy_glossary" else {})}})
    routes_path = sample["root"] / "data/routes.local.json"
    routes_path.write_text(json.dumps({"schema_version": "translation-comparison-routes-v1", "routes": routes}),
                           encoding="utf-8")
    plan = bench.load_plan(sample["root"], sample["corpus"], routes_path)
    actual = sample["root"] / "data/actual-results.local.jsonl"
    model_calls = []

    def gateway(request):
        value = json.loads(request.content)
        model_calls.append(value)
        text = candidate()["title"] if value["text"] == sample["articles"][0]["title"] else candidate()["body"]
        return httpx.Response(200, json={"model": value["model"], "text": text, "complete": True})

    bench.run(plan, actual, route_id="hy_current", max_new_articles=1,
              transport_factory=lambda: httpx.MockTransport(gateway))
    count, lt_calls = len(model_calls), []
    checker.run(sample["root"], sample["corpus"], actual, sample["output"], transport=fake_lt(lt_calls))
    assert len(model_calls) == count and len(lt_calls) == 1
    assert evaluated(sample)[0]["mechanical_checks_passed"] is True


def test_dry_run_only_validates_and_prints_aggregate_without_http_or_output(checker, sample, capsys):
    add_task(checker, sample)
    result = checker.main(["--root", str(sample["root"]), "--corpus", str(sample["corpus"]),
        "--results", str(sample["journal"]), "--output", str(sample["output"])])
    printed = capsys.readouterr().out
    assert result["dry_run"] is True and not sample["output"].exists()
    assert "original" not in printed and "body" not in printed and "2.5" not in printed
