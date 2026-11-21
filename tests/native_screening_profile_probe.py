"""Exercise the HTTP payload and receipts in both isolated urinary workers."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

def exercise(worker, tmp, specification):
    sys.path.insert(0, str(worker))
    from automation import gateway_preflight, monthly_adjudication, monthly_parallel, monthly_profile
    from automation.preprint_recovery import preprint_screening
    from scripts import dual_screening, run_claude_gemini_parallel_review as driver
    from scripts import cost_accounting, validate_monthly_artifacts

    assert monthly_profile.specification() == specification
    stages = specification["stages"]
    assert set(gateway_preflight.required_models()) == {s["model"] for s in stages.values()}
    captured = []

    def output(**kwargs):
        captured.append(kwargs)
        effort = kwargs.get("reasoning_effort") or kwargs["reasoning"]["effort"]
        return SimpleNamespace(id="test-response", model=kwargs["model"], usage=None,
                               output_text='{"results":[]}', reasoning=SimpleNamespace(effort=effort),
                               reasoning_effort=effort, choices=[SimpleNamespace(
                                   message=SimpleNamespace(content='{"results":[]}'), finish_reason="stop")])

    def check(role, *, responses):
        sent = captured[-1]
        assert sent["model"] == stages[role]["model"]
        actual = sent["reasoning"]["effort"] if responses else sent["reasoning_effort"]
        assert actual == stages[role]["reasoning_effort"]
        assert sent["extra_body"]["drop_params"] is False

    responses = SimpleNamespace(create=output)
    client = SimpleNamespace(responses=responses, chat=SimpleNamespace(completions=responses))
    track = dual_screening.TrackRuntime("chatgpt", client, dual_screening.CHATGPT_FIRST_MODEL,
        dual_screening.CHATGPT_SECOND_MODEL, dual_screening.CHATGPT_FULL_TEXT_MODEL, "responses")
    for stage, role in (("first", "r1"), ("second", "sol"), ("full_text", "luna")):
        model = stages[role]["model"]
        assert validate_monthly_artifacts.EXPECTED_MODELS["chatgpt"][stage] == model
        usage = dual_screening.empty_usage(model)
        assert usage["reasoning_effort_requested"] == stages[role]["reasoning_effort"]
        assert dual_screening.call_json(track, stage, "system", "prompt", usage) == {"results": []}
        check(role, responses=True)
        assert cost_accounting.estimated_cost_usd({"input_tokens": 1000000}, model) > 0

    def wrong_effort(**kwargs):
        answer = output(**kwargs)
        answer.reasoning = None
        answer.reasoning_effort = "low"
        return answer

    rejected_usage = dual_screening.empty_usage(stages["r1"]["model"])
    with patch.object(client.responses, "create", side_effect=wrong_effort) as calls:
        try:
            dual_screening.call_json(track, "first", "system", "prompt", rejected_usage)
        except monthly_profile.ResponsePolicyMismatch:
            pass
        else:
            raise AssertionError("The urinary worker accepted a lower reported effort")
        assert calls.call_count == 1 and rejected_usage["calls"] == 1
        assert rejected_usage["reasoning_effort_reported_counts"] == {"low": 1}

    def parse_raw(**kwargs):
        response = output(**kwargs)
        return SimpleNamespace(headers={}, parse=lambda: response)

    with patch.object(driver, "_write_cost_files"):
        for stage, role, model in (("claude_r2", "opus", driver.CLAUDE_R2_MODEL),
                                   ("gemini_r3", "gemini", driver.GEMINI_R3_MODEL)):
            assert monthly_parallel.MODELS["r2" if role == "opus" else "r3"] == stages[role]["model"]
            for transport in ("chat", "raw"):
                client.chat.completions = SimpleNamespace(create=output)
                if transport == "raw":
                    client.chat.completions.with_raw_response = SimpleNamespace(create=parse_raw)
                folder = tmp / stage / transport
                options = dict(output_dir=folder, stage=stage, model=model, system="system", prompt="prompt",
                               context_ids=["fixture"], exposure_weights={"food": 1},
                               validator=lambda p: p["results"])
                assert driver._call_gateway_json(client, **options) == []
                check(role, responses=False)
                receipt = json.loads(next((folder / "model_response_cache" / stage).glob("*.json")).read_text())
                assert receipt["reasoning_effort_requested"] == receipt["reasoning_effort_reported"] == stages[role]["reasoning_effort"]
                total_calls = len(captured)
                assert driver._call_gateway_json(client, **options) == []
                assert len(captured) == total_calls
    with patch.object(monthly_adjudication, "_client", return_value=client):
        monthly_adjudication.cornell_transport({"input": {}, "reasoning_effort": stages["adjudicator"]["reasoning_effort"]})
        check("adjudicator", responses=True)
    for role, stage in (("gemini", "gemini"), ("luna", "luna"), ("astra", "adjudicator")):
        assert preprint_screening.MODELS[role] == (stages[stage]["model"], stages[stage]["reasoning_effort"])


if __name__ == "__main__":
    exercise(Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3]))
