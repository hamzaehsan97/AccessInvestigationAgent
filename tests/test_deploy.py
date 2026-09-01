"""Deployment configuration smoke tests."""
from importlib import import_module
from pathlib import Path

from fastapi.testclient import TestClient

from agent.core.run_store import FileRunStore


def test_docker_entrypoint_imports_fastapi_app() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "deploy" / "Dockerfile").read_text()
    assert "agent.interfaces.server:app" in dockerfile
    module = import_module("agent.interfaces.server")
    assert module.app.title == "Access Investigation Agent"


def test_offline_api_validates_playbooks_and_persists_runs(
    tmp_path, monkeypatch
) -> None:
    module = import_module("agent.interfaces.server")
    store = FileRunStore(tmp_path)
    monkeypatch.setattr(module, "run_store", store)
    client = TestClient(module.app)

    created = client.post(
        "/investigations",
        json={
            "offline": True,
            "playbook": "access_explain",
            "parameters": {"person": "Ariel Chen", "resource": "Snowflake"},
        },
    )
    assert created.status_code == 200
    investigation_id = created.json()["id"]
    assert store.load(investigation_id) is not None

    invalid_playbook = client.post(
        "/investigations",
        json={"offline": True, "playbook": "not-a-playbook"},
    )
    assert invalid_playbook.status_code == 422

    invalid_limit = client.post(
        "/investigations",
        json={
            "offline": True,
            "playbook": "offboarding_leakage",
            "parameters": {"limit": 0},
        },
    )
    assert invalid_limit.status_code == 400

    free_form_offline = client.post(
        "/investigations",
        json={"offline": True, "question": "Who has access?"},
    )
    assert free_form_offline.status_code == 400
