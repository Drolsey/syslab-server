"""Step 4.7: which model fills which role. docs/plans/step-04-retrieval-plane.md, section 7.

Declared here, so adding a role later is filling a row rather than designing a
mechanism. `models.toml` has five roles; only `chat` is filled today.

AN EMPTY ROLE MEANS UNAVAILABLE, NEVER A SILENT FALLBACK -- the same rule
app.context.current_tenant() enforces for tenancy, and for the same reason: a
default is how "embed is not deployed yet" quietly becomes "embed silently
used chat instead," and nobody would see it happen.

THIS DOES NOT CHANGE HOW THE APP TALKS TO A MODEL TODAY. app/llm.py keeps
reading LLM_BASE_URL / LLM_MODEL from app/config.py exactly as it always has.
models.toml's `chat` entry documents that same deployment; nothing reads it
into app/llm.py, because nothing today needs two chat configurations to agree.
The seam exists so a producer can ask "is embed available" the day one is
built, not to re-plumb the one role that already works.
"""

from __future__ import annotations

import tomllib

from app.config import PROJECT_ROOT

MODELS_TOML = PROJECT_ROOT / "models.toml"


class RoleUnavailable(RuntimeError):
    """Asked for a role nothing fills. Not a bug to catch -- a wait to respect."""


def _load() -> dict[str, dict]:
    if not MODELS_TOML.is_file():
        return {}
    with MODELS_TOML.open("rb") as f:
        return tomllib.load(f).get("roles", {})


# Loaded once at import, the way config.py's own constants are -- a role
# filled by editing models.toml takes effect on the next process start, not
# mid-request, which is what "declared" means here.
_ROLES: dict[str, dict] = _load()


def role_filled(role: str) -> bool:
    """For diagnostics only. Never branch behaviour on this.

    The same warning app.context.tenant_if_set() carries: a bool that means
    "is something there" invites being treated as the answer itself. Call
    model_for() and let it raise; that is the one place the refusal and its
    reason live.
    """
    return bool(_ROLES.get(role))


def model_for(role: str) -> dict:
    """Which model fills this role. Raises if none does.

    There is no default model for an unfilled role: a default is how "embed
    is not deployed yet" quietly becomes "embed silently used chat instead."
    """
    config = _ROLES.get(role)
    if not config:
        raise RoleUnavailable(
            f"No model fills the {role!r} role. It is declared in "
            f"models.toml and empty, which means unavailable -- not a silent "
            f"fallback to another role's model. Fill in [roles.{role}] to "
            f"enable it."
        )
    return dict(config)
