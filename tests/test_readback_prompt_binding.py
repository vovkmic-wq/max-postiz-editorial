"""Readback uses saved protocol evidence when the current prompt changes."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from max_postiz_agent.article_quality import REVIEW_BOUNDS_V2_CONTRACT
from max_postiz_agent.automatic_policy import review_protocol_trace_binding
from max_postiz_agent.config import AgentError


@pytest.fixture
def readback(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("readback_max_test", scripts / "readback_max.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "read_private", lambda path, maximum: path.read_bytes())
    return module


def write_trace(path, instruction="saved instruction", *, expected_instruction="saved instruction"):
    contract = REVIEW_BOUNDS_V2_CONTRACT
    prompt_hash = hashlib.sha256(expected_instruction.encode()).hexdigest()
    evidence = {"review_protocol_contract": contract, "review_prompt_hash": prompt_hash,
                "review_raw": "review", "review": {"ok": True}}
    base = {"record_id": "r1", "review_protocol_contract": contract}
    events = [
        {**base, "phase": "review_started", "context": {
            "review_protocol_contract": contract, "instruction": instruction,
            "review_prompt_hash": prompt_hash, "source_untrusted": "original",
            "translation_untrusted": "translation"}},
        {**base, "phase": "review_returned", "raw": "review"},
        {**base, "phase": "review_parsed", "review": {"ok": True}},
        {**base, "phase": "accepted", "result": {"review_protocol_contract": contract,
            "original": "original", "translation": "translation", "machine_review_evidence": evidence}},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    return {**evidence, "trace_hash": hashlib.sha256(path.read_bytes()).hexdigest()}


def check(module, prompt, trace, evidence):
    return module.verify_prompt_binding(prompt, trace, evidence, record_id="r1", bounds_v2=REVIEW_BOUNDS_V2_CONTRACT,
                                        validate_trace=review_protocol_trace_binding)


def test_changed_prompt_uses_complete_bound_trace(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    prompt.write_text("new prompt", encoding="utf-8")
    evidence = write_trace(trace)
    assert check(readback, prompt, trace, evidence) == "verified_v2_trace"


def test_altered_instruction_cannot_reuse_old_hash(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    prompt.write_text("new prompt", encoding="utf-8")
    evidence = write_trace(trace, instruction="different instruction")
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID"):
        check(readback, prompt, trace, evidence)


def test_missing_or_changed_trace_fails(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    evidence = write_trace(trace)
    trace.write_text("{}", encoding="utf-8")
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID"):
        check(readback, prompt, trace, evidence)


def test_old_trace_does_not_bypass_changed_prompt(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    prompt.write_text("new prompt", encoding="utf-8")
    evidence = write_trace(trace)
    del evidence["review_protocol_contract"]
    with pytest.raises(ValueError, match="REVIEW_PROMPT_CHANGED"):
        check(readback, prompt, trace, evidence)


def test_old_trace_uses_original_prompt(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    prompt.write_text("saved instruction", encoding="utf-8")
    evidence = write_trace(trace)
    del evidence["review_protocol_contract"]
    assert check(readback, prompt, trace, evidence) == "current_prompt"


def test_trace_for_another_record_is_rejected(tmp_path, readback):
    prompt, trace = tmp_path / "prompt", tmp_path / "trace"
    evidence = write_trace(trace)
    with pytest.raises(ValueError, match="REVIEW_TRACE_RECORD_MISMATCH"):
        readback.verify_prompt_binding(prompt, trace, evidence, record_id="other",
            bounds_v2=REVIEW_BOUNDS_V2_CONTRACT, validate_trace=review_protocol_trace_binding)
