"""LLM investigator loop logic, against a scripted fake model (no real model or sandbox needed)."""

from __future__ import annotations

import json

import httpx
import pytest

from agent import llm_agent
from agent.evidence import EvidenceLedger


def fake_model(script: list):
    """Each item: a list of (tool_name, args) calls, or a str (plain reply without tool calls)."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        step = script[min(len(seen) - 1, len(script) - 1)]
        if "response_format" in seen[-1]:   # schema-constrained final answer: content, never tool calls
            content = step if isinstance(step, str) else json.dumps(step[0][1])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if isinstance(step, str):
            msg = {"role": "assistant", "content": step}
        else:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{len(seen)}{i}", "type": "function",
                 "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
                for i, (n, a) in enumerate(step)]}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake"), seen


def ledger(calls: list | None = None):
    def queue_stats():
        if calls is not None:
            calls.append("queue_stats")
        return {"pending": 900, "oldest_pending_age_s": 40}
    return EvidenceLedger({"queue_stats": queue_stats, "worker_heartbeats": lambda: [{"worker_id": "worker-1", "age_s": 30}]})


DIAG = {"incident_type": "worker_hung", "evidence": ["ev-1", "ev-2"], "confidence": "high", "unknowns": []}


def test_investigate_then_diagnose_with_valid_citations():
    client, seen = fake_model([[("queue_stats", {})], [("worker_heartbeats", {})], [("submit_diagnosis", DIAG)]])
    inv = llm_agent.investigate([{"alert": "WorkerHeartbeatStale"}], ledger(), client)
    assert (inv.diagnosis, inv.citations_valid, inv.stop_reason, inv.model_calls) == ("worker_hung", True, "submitted", 3)
    assert "restart_service" not in {t["function"]["name"] for t in seen[0]["tools"]}   # no action tools offered


def test_citing_evidence_never_observed_is_flagged():
    client, _ = fake_model([[("queue_stats", {})], [("submit_diagnosis", {**DIAG, "evidence": ["ev-1", "ev-7"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.diagnosis == "worker_hung" and inv.citations_valid is False


def test_invalid_arguments_are_rejected_and_the_loop_continues():
    client, _ = fake_model([[("search_logs", {"instance": "../../etc"})], [("queue_stats", {})],
                            [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.invalid_calls == 1 and inv.citations_valid and inv.stop_reason == "submitted"


def test_action_tools_are_refused_in_read_only_mode(monkeypatch):
    from agent.tools import act
    called = []
    monkeypatch.setitem(act.ACTIONS, "restart_service", lambda **kw: called.append(kw))
    client, _ = fake_model([[("restart_service", {"service": "worker"})], [("queue_stats", {})],
                            [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.invalid_calls == 1 and called == [] and inv.actions == []


def test_prose_reply_triggers_forced_finalisation(monkeypatch):
    monkeypatch.setattr(llm_agent, "FINAL_MODE", "tool")   # legacy path
    client, seen = fake_model([[("queue_stats", {})], "It looks like the worker is hung (ev-1).",
                               [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.forced_submit and inv.diagnosis == "worker_hung" and inv.citations_valid
    assert seen[2]["tool_choice"] == "required"
    assert [t["function"]["name"] for t in seen[2]["tools"]] == ["submit_diagnosis"]


def test_model_that_never_submits_stops_without_a_diagnosis(monkeypatch):
    monkeypatch.setattr(llm_agent, "FINAL_MODE", "tool")   # legacy path
    client, _ = fake_model(["I think it's the worker.", "Still the worker."])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.stop_reason == "no_tool_call" and inv.model_calls == 2 and inv.diagnosis == "none"


def test_step_budget_ends_with_a_forced_submission_attempt_and_no_extra_tool_runs(monkeypatch):
    monkeypatch.setattr(llm_agent, "MAX_STEPS", 3)
    monkeypatch.setattr(llm_agent, "FINAL_MODE", "tool")   # legacy path
    client, seen = fake_model([[("queue_stats", {})]])   # keeps asking for queue_stats, even when finalising
    calls = []
    inv = llm_agent.investigate([], ledger(calls), client)
    assert inv.stop_reason == "step_budget" and inv.forced_submit and len(calls) == 3   # 4th request: refused
    assert seen[-1]["tool_choice"] == "required" and inv.invalid_calls == 1


def test_model_server_error_ends_the_case_without_crashing_the_run():
    """Real failure (held-out run 1): output budget hit mid tool call -> llama.cpp can't parse it -> 5xx."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": {"type": "upstream_error", "message": "upstream returned 500"}})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")
    inv = llm_agent.investigate([{"alert": "Api5xxRateHigh"}], ledger(), client)
    assert (inv.diagnosis, inv.stop_reason, inv.model_calls) == ("none", "model_error", 0)
    assert "upstream_error" in inv.transcript[-1]


def test_prompt_version_and_tool_results_are_recorded():
    client, seen = fake_model([[("queue_stats", {})], [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.prompt_version == llm_agent.PROMPT_VERSION
    assert seen[0]["messages"][0]["content"] == llm_agent.PROMPTS[llm_agent.PROMPT_VERSION]
    assert inv.evidence_log[0]["id"] == "ev-1" and '"pending": 900' in inv.evidence_log[0]["result"]


def test_overlong_unknowns_are_rejected():
    """Bound that keeps a submission inside the output budget (two v1 cases overflowed it)."""
    client, _ = fake_model([[("queue_stats", {})], [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"], "unknowns": ["a", "b", "c", "d"]})],
                            [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.invalid_calls == 1 and inv.stop_reason == "submitted"


# --- schema-constrained final answer (default) ---------------------------------------------------

def test_schema_final_answer_recovers_a_prose_reply():
    """Real failure: the model wrote a correct diagnosis as a code block in prose; with
    tool_choice=required (not enforced by the server) it was lost."""
    client, seen = fake_model([[("queue_stats", {})], "It looks like the worker is hung (ev-1).",
                               [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert (inv.diagnosis, inv.citations_valid, inv.forced_submit, inv.final_mode) == ("worker_hung", True, True, "schema")
    final = seen[-1]
    assert "tools" not in final and final["max_tokens"] == llm_agent.FINAL_MAX_TOKENS
    assert final["response_format"]["json_schema"]["schema"] == llm_agent.DIAGNOSIS_TOOL["submit_diagnosis"][1]


def test_step_budget_ends_in_a_schema_answer(monkeypatch):
    monkeypatch.setattr(llm_agent, "MAX_STEPS", 3)
    client, seen = fake_model([[("queue_stats", {})], [("queue_stats", {})], [("queue_stats", {})],
                               [("submit_diagnosis", {**DIAG, "evidence": ["ev-1"]})]])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.stop_reason == "step_budget" and inv.diagnosis == "worker_hung" and "response_format" in seen[-1]


def test_server_error_mid_investigation_still_gets_a_structured_answer():
    """Real failure: prose before a tool call used up the 400-token budget, the server couldn't
    parse the cut-off call (5xx), and the evidence already gathered was thrown away."""
    n = {"i": 0}

    def handler(request):
        n["i"] += 1
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                  "content": json.dumps({**DIAG, "evidence": ["ev-1"]})}}], "usage": {}})
        if n["i"] == 1:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "queue_stats", "arguments": "{}"}}]}
            return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {}})
        return httpx.Response(502, json={"error": {"type": "upstream_error"}})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.diagnosis == "worker_hung" and inv.citations_valid and inv.forced_submit
    assert any(t.startswith("[model_error]") for t in inv.transcript)


def test_final_answer_that_is_not_schema_valid_is_rejected():
    client, _ = fake_model(["prose only", "still prose, not JSON"])
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.stop_reason == "invalid_final" and inv.diagnosis == "none" and inv.invalid_calls == 1


def test_reply_cut_off_at_max_tokens_is_not_added_to_the_history():
    """Real failure: a reply hit max_tokens mid tool call; appended to the history, it made the
    server reject every later request (500 before generating), so the diagnosis was lost."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            assert not any("<tool_call>" in (m.get("content") or "") for m in body["messages"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                  "content": json.dumps({**DIAG, "evidence": ["ev-1"]})}}], "usage": {}})
        if len(seen) == 1:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "queue_stats", "arguments": "{}"}}]}
            return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "tool_calls"}], "usage": {}})
        cut = {"role": "assistant", "content": 'Long analysis...\n<tool_call>\n{"name": "submit_diagnosis", "arguments": {"unknowns": ["Investigate if there are'}
        return httpx.Response(200, json={"choices": [{"message": cut, "finish_reason": "length"}], "usage": {}})
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")
    inv = llm_agent.investigate([], ledger(), client)
    assert inv.diagnosis == "worker_hung" and inv.citations_valid and len(seen) == 3
    assert any(t.startswith("[truncated at max_tokens") for t in inv.transcript)

