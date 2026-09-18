"""Step 4.7: which model fills which role, and the rule that an empty one raises.

The property this sub-step exists to prove is the same one app/context.py's
current_tenant() already proves for tenancy: asking for something nothing
fills does not return a default, a None, or another role's model dressed up
as this one. It raises, with a reason a person can act on.
"""

from __future__ import annotations

import pytest

from app import models


@pytest.fixture(autouse=True)
def a_registry_with_one_filled_role(monkeypatch):
    """The real models.toml's shape, without depending on the file on disk."""
    monkeypatch.setattr(models, "_ROLES", {
        "chat": {"model": "Qwen/Qwen3-14B-AWQ", "provider": "vllm", "context_tokens": 16384},
        "embed": {},
        "vision": {},
        "stt": {},
        "tts": {},
    })


def test_asking_for_the_filled_chat_role_returns_its_config():
    config = models.model_for("chat")
    assert config["model"] == "Qwen/Qwen3-14B-AWQ"
    assert config["provider"] == "vllm"
    assert config["context_tokens"] == 16384


def test_asking_for_an_empty_role_raises():
    with pytest.raises(models.RoleUnavailable):
        models.model_for("embed")


def test_the_error_names_the_role_and_says_how_to_fill_it():
    with pytest.raises(models.RoleUnavailable) as caught:
        models.model_for("embed")
    message = str(caught.value)
    assert "embed" in message
    assert "unavailable" in message
    assert "models.toml" in message


def test_asking_for_a_role_that_does_not_exist_at_all_raises_the_same_way():
    # Not KeyError. A typo and an unfilled role are the same story to a
    # caller: nothing here can answer for that role right now.
    with pytest.raises(models.RoleUnavailable):
        models.model_for("summarize")


def test_role_filled_is_true_for_chat_and_false_for_the_others():
    assert models.role_filled("chat") is True
    assert models.role_filled("embed") is False
    assert models.role_filled("vision") is False
    assert models.role_filled("stt") is False
    assert models.role_filled("tts") is False


def test_role_filled_never_raises_even_for_an_unknown_role():
    assert models.role_filled("summarize") is False


def test_model_for_returns_a_copy_the_caller_cannot_mutate_the_registry_with():
    config = models.model_for("chat")
    config["model"] = "something-else"
    assert models.model_for("chat")["model"] == "Qwen/Qwen3-14B-AWQ"


# --------------------------------------------------------------------------
# the file on disk parses the way _load() assumes it does
# --------------------------------------------------------------------------

def test_the_real_models_toml_loads_and_declares_all_five_roles():
    roles = models._load()
    assert set(roles) == {"chat", "embed", "vision", "stt", "tts"}
    assert roles["chat"]["model"]
    # embed filled 18 September 2026 (Step 5.1's decision) -- see
    # docs/plans/step-05-embeddings.md. revision is required alongside model
    # for embed specifically, not just believed present: app/vectors.py's
    # freshness mechanism can only invalidate on a config change it can see,
    # and "model" alone would miss a same-name weights swap.
    assert roles["embed"]["model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert roles["embed"]["revision"]
    for empty in ("vision", "stt", "tts"):
        assert roles[empty] == {}
