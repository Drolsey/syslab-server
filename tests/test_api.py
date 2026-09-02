"""Tests for the HTTP layer, with the model replaced.

The agent is already covered in test_agent.py. What is under test here is the
web app around it: uploads, path handling, error codes, and the shape of the
JSON the page depends on.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agent, config, main
from app.llm import LlmError


TEST_TOKEN = "a-test-token-long-enough-to-count"


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_TOKEN", TEST_TOKEN)
    main._failures.clear()
    yield tmp_path


@pytest.fixture
def client():
    """A signed-in client, since that is the ordinary case."""
    signed_in = TestClient(main.app)
    signed_in.headers.update({"X-Syslab-Token": TEST_TOKEN})
    return signed_in


@pytest.fixture
def stranger():
    """Nobody. Used to prove the door is actually shut."""
    return TestClient(main.app)


def upload(client, name, content=b"%PDF-1.4 hello"):
    return client.post("/api/upload", files={"file": (name, io.BytesIO(content), "application/pdf")})


# --- the page and health --------------------------------------------------

def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>syslab</title>" in response.text


def test_health_reports_the_model(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["model"]


# --- uploads --------------------------------------------------------------

def test_a_pdf_upload_lands_in_the_data_folder(client, temp_data_dir):
    response = upload(client, "invoice.pdf")
    assert response.status_code == 200
    assert response.json()["name"] == "invoice.pdf"
    assert (temp_data_dir / "invoice.pdf").read_bytes() == b"%PDF-1.4 hello"


@pytest.mark.parametrize("name", ["notes.txt", "script.exe", "archive.zip", "noextension"])
def test_unhandled_file_types_are_refused(client, name):
    assert upload(client, name).status_code == 400


def test_a_path_in_the_filename_is_stripped_not_followed(client, temp_data_dir):
    response = upload(client, "../../evil.pdf")
    assert response.status_code == 200
    assert response.json()["name"] == "evil.pdf"
    assert (temp_data_dir / "evil.pdf").is_file()
    assert not (temp_data_dir.parent / "evil.pdf").exists()


def test_a_windows_path_in_the_filename_is_stripped(client, temp_data_dir):
    response = upload(client, r"C:\Users\me\Desktop\report.pdf")
    assert response.status_code == 200
    assert response.json()["name"] == "report.pdf"


def test_uploading_the_same_name_twice_does_not_overwrite(client, temp_data_dir):
    upload(client, "report.pdf", b"%PDF-1.4 first")
    second = upload(client, "report.pdf", b"%PDF-1.4 second")
    assert second.json()["renamed"] is True
    assert second.json()["name"] == "report (2).pdf"
    assert (temp_data_dir / "report.pdf").read_bytes() == b"%PDF-1.4 first"


def test_an_empty_file_is_refused(client):
    assert upload(client, "empty.pdf", b"").status_code == 400


def test_a_file_over_the_limit_is_refused(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    response = upload(client, "big.pdf", b"x" * 50)
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


# --- listing and download -------------------------------------------------

def test_files_are_listed_and_downloadable(client):
    upload(client, "invoice.pdf", b"%PDF-1.4 payload")
    listing = client.get("/api/files").json()
    assert [f["name"] for f in listing["files"]] == ["invoice.pdf"]

    download = client.get("/api/files/invoice.pdf")
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 payload"


def test_downloading_a_missing_file_is_a_404(client):
    assert client.get("/api/files/nope.pdf").status_code == 404


@pytest.mark.parametrize("name", ["..%2Fescape.pdf", "..%5C..%5Cwindows%5Cwin.ini"])
def test_download_cannot_escape_the_data_folder(client, name):
    assert client.get(f"/api/files/{name}").status_code in {400, 404}


# --- chat -----------------------------------------------------------------

def test_chat_returns_the_answer_the_trace_and_the_history(client, monkeypatch):
    monkeypatch.setattr(
        agent,
        "ask",
        lambda message, history=None: {
            "answer": "It says 42.",
            "steps": [
                {"tool": "read_pdf", "arguments": {"filename": "a.pdf"},
                 "ok": True, "error": None, "result": {"text": "big payload"}}
            ],
            "messages": [
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": message},
                {"role": "assistant", "content": "It says 42."},
            ],
        },
    )
    body = client.post("/api/chat", json={"message": "what does a.pdf say?"}).json()

    assert body["answer"] == "It says 42."
    assert body["steps"][0]["tool"] == "read_pdf"
    # the raw tool result is not shipped to the browser, only what it displays
    assert "result" not in body["steps"][0]
    # the system prompt is not echoed back into the page's history
    assert all(m["role"] != "system" for m in body["history"])


def test_chat_survives_the_model_being_down(client, monkeypatch):
    def down(*_args, **_kwargs):
        raise LlmError("Cannot reach Ollama at http://127.0.0.1:11434.")

    monkeypatch.setattr(agent, "ask", down)
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 503
    assert "Ollama" in response.json()["detail"]


def test_an_empty_message_is_rejected_before_it_reaches_the_model(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_history_is_passed_through_to_the_agent(client, monkeypatch):
    seen = {}

    def capture(message, history=None):
        seen["history"] = history
        return {"answer": "ok", "steps": [], "messages": []}

    monkeypatch.setattr(agent, "ask", capture)
    prior = [{"role": "user", "content": "earlier"}]
    client.post("/api/chat", json={"message": "next", "history": prior})
    assert seen["history"] == prior


def test_health_reports_when_the_app_started(client):
    """Phase 05 needs this to tell a boot-start from someone starting it by hand."""
    from datetime import datetime

    body = client.get("/api/health").json()
    assert datetime.fromisoformat(body["started_at"])
    assert body["uptime_seconds"] >= 0


def test_health_reports_a_code_fingerprint(client):
    """So the check can spot a service still running yesterday's code."""
    from app import config

    assert client.get("/api/health").json()["code_fingerprint"] == config.code_fingerprint()


def test_the_fingerprint_changes_when_a_file_is_touched():
    """Editing any app file must change the stamp, or the staleness check is blind."""
    import os

    from app import config

    before = config.code_fingerprint()
    target = Path(config.__file__).resolve().parent / "tools.py"
    original = target.stat().st_mtime
    # Newer than every other file in the package, not merely newer than itself.
    # Bumping by a fixed amount silently stopped working once other files were
    # edited more recently than this one.
    newest = max(f.stat().st_mtime for f in Path(config.__file__).resolve().parent.glob("*.py"))
    future = newest + 100_000
    try:
        os.utime(target, (future, future))
        assert config.code_fingerprint() != before
    finally:
        os.utime(target, (original, original))
    assert config.code_fingerprint() == before


# --- Phase 06: the door ---------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/health"),
        ("get", "/api/files"),
        ("get", "/api/files/anything.pdf"),
        ("post", "/api/upload"),
        ("post", "/api/chat"),
    ],
)
def test_every_api_route_is_shut_to_a_stranger(stranger, method, path):
    response = getattr(stranger, method)(path)
    assert response.status_code == 401, f"{method.upper()} {path} was reachable"


def test_the_page_itself_is_served_so_the_sign_in_form_can_load(stranger):
    assert stranger.get("/").status_code == 200


def test_a_wrong_token_is_refused(stranger):
    assert stranger.post("/api/login", json={"token": "not-it"}).status_code == 401


def test_the_right_token_sets_a_cookie_that_then_works(stranger):
    response = stranger.post("/api/login", json={"token": TEST_TOKEN})
    assert response.status_code == 200
    cookie = response.cookies.get(main.TOKEN_COOKIE)
    assert cookie == TEST_TOKEN
    # the cookie the browser stores must not be readable by page javascript
    assert "httponly" in response.headers["set-cookie"].lower()
    # and it carries the session from here on, which is what download links need
    assert stranger.get("/api/health").status_code == 200


def test_signing_out_closes_the_door_again(stranger):
    stranger.post("/api/login", json={"token": TEST_TOKEN})
    assert stranger.get("/api/health").status_code == 200
    stranger.post("/api/logout")
    assert stranger.get("/api/health").status_code == 401


def test_a_bearer_header_works_for_scripts(stranger):
    response = stranger.get("/api/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert response.status_code == 200


def test_repeated_wrong_tokens_are_throttled(stranger):
    for _ in range(main.MAX_FAILURES):
        stranger.post("/api/login", json={"token": "wrong"})
    blocked = stranger.post("/api/login", json={"token": "wrong"})
    assert blocked.status_code == 429
    # and the throttle does not care that the next guess happens to be right
    assert stranger.post("/api/login", json={"token": TEST_TOKEN}).status_code == 429


@pytest.mark.parametrize("weak", ["", "change-me", "changeme", "password", "short"])
def test_a_weak_or_missing_token_makes_the_app_refuse_to_serve(stranger, monkeypatch, weak):
    monkeypatch.setattr(config, "APP_TOKEN", weak)
    response = stranger.get("/api/health")
    assert response.status_code == 503
    assert "new_token" in response.json()["detail"]


def test_the_app_refuses_to_listen_beyond_this_machine_without_a_token(monkeypatch):
    """The guard that matters: never open to the network with a weak token."""
    monkeypatch.setattr(config, "APP_TOKEN", "change-me")
    monkeypatch.setattr(main, "APP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exit_info:
        main.main()
    assert exit_info.value.code == 2


def test_loopback_with_no_token_is_still_allowed_to_start(monkeypatch):
    """Otherwise Phases 04 and 05 could never have run."""
    monkeypatch.setattr(config, "APP_TOKEN", "change-me")
    monkeypatch.setattr(main, "APP_HOST", "127.0.0.1")
    started = {}
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.update(k))
    main.main()
    assert started["host"] == "127.0.0.1"
