"""The model and effort contract for scheduled nutrition evidence screening."""
import os
import re

PROFILE = "2026-11-xhigh-v1"
ENV = "MONTHLY_SCREENING_PROFILE"
MODELS = {
    "r1": "gpt-6-luna",
    "sol": "gpt-6.1-sol",
    "opus": "claude-opus-5-5",
    "luna": "gpt-6-luna",
    "gemini": "gemini-3.8-flash",
    "adjudicator": "gpt-6-astra",
}
EFFORT = {role: ("high" if role == "gemini" else "xhigh") for role in MODELS}


def enabled():
    profile = os.environ.get(ENV, "")
    if profile and profile != PROFILE:
        raise ValueError(f"Unknown monthly screening profile {profile!r}")
    return profile == PROFILE


def specification():
    return {"id": PROFILE, "stages": {
        role: {"model": model, "reasoning_effort": EFFORT[role]}
        for role, model in MODELS.items()
    }}


def requested_effort(model):
    if enabled():
        for role, configured in MODELS.items():
            if model == configured:
                return EFFORT[role]
    return None


def request_options(model, *, responses=False):
    effort = requested_effort(model)
    if effort is None:
        return {}
    options = {"extra_body": {"drop_params": False}}
    if responses:
        options["reasoning"] = {"effort": effort}
    else:
        options["reasoning_effort"] = effort
    return options


class ResponsePolicyMismatch(ValueError):
    """The provider explicitly reports a different model or reasoning setting."""


def reported_effort(response):
    reasoning = getattr(response, "reasoning", None)
    nested = reasoning.get("effort") if isinstance(reasoning, dict) else getattr(reasoning, "effort", None)
    return nested or getattr(response, "reasoning_effort", None)


def verify_reported(model, reported_model, effort):
    expected = requested_effort(model)
    if expected is None:
        return
    if reported_model:
        returned = reported_model.rsplit("/", 1)[-1]
        if returned != model and not re.fullmatch(re.escape(model) + r"-20\d\d-\d\d-\d\d", returned):
            raise ResponsePolicyMismatch(f"Gateway returned model {reported_model} for requested {model}")
    if effort is not None and effort != expected:
        raise ResponsePolicyMismatch(f"Gateway returned reasoning effort {effort} for requested {expected}")
