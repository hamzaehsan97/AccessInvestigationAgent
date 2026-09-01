"""Minimal FastAPI wrapper around the Agent.

Run: ``uvicorn agent.interfaces.server:app --host 0.0.0.0 --port 8080``

The same ``Agent`` object powers both the CLI and this HTTP surface. In
production this file would gain OIDC middleware, RBAC, streaming, and
Prometheus metrics; the internal contract stays identical.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from agent.core.config import get_settings
from agent.core.db import get_db
from agent.playbooks import (
    access_explain_llm,
    offboarding_leakage_llm,
    offline_access_explain_report,
    offline_offboarding_report,
)
from agent.core.run_store import FileRunStore
from agent.llm.runtime import Agent
from agent.core.schemas import Investigation

settings = get_settings()
db = get_db(settings.agent_db_path)
run_store = FileRunStore(settings.run_store_dir)

app = FastAPI(
    title="Access Investigation Agent",
    version="0.1.0",
    description="LLM-driven investigations over a read-only IAM snapshot.",
)


class InvestigateRequest(BaseModel):
    question: Optional[str] = None
    playbook: Optional[Literal["offboarding_leakage", "access_explain"]] = None
    parameters: dict = Field(default_factory=dict)
    offline: bool = False


def _bounded_limit(parameters: dict, default: int = 5) -> int:
    value = parameters.get("limit", default)
    if isinstance(value, bool):
        raise HTTPException(400, "parameters.limit must be an integer from 1 to 100")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "parameters.limit must be an integer from 1 to 100")
    if not 1 <= limit <= 100:
        raise HTTPException(400, "parameters.limit must be an integer from 1 to 100")
    return limit


def _save_and_return(inv: Investigation) -> Investigation:
    run_store.save(inv)
    return inv


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "snapshot_time": db.snapshot_time_iso,
        "dataset_version": db.dataset_version,
    }


@app.get("/scanners")
def list_scanners() -> list[str]:
    from agent.tools.scanners import available_scanners

    return available_scanners()


@app.post("/investigations", response_model=Investigation)
def create_investigation(req: InvestigateRequest) -> Investigation:
    if req.offline:
        if req.playbook == "access_explain":
            person = req.parameters.get("person")
            resource = req.parameters.get("resource")
            if not person:
                raise HTTPException(400, "parameters.person is required")
            return _save_and_return(
                offline_access_explain_report(
                    person_query=person, resource_hint=resource, db=db
                )
            )
        if req.playbook == "offboarding_leakage" or (
            req.playbook is None and req.question is None
        ):
            return _save_and_return(
                offline_offboarding_report(
                    limit=_bounded_limit(req.parameters), db=db
                )
            )
        raise HTTPException(
            400,
            "offline mode requires a supported playbook; free-form questions need an LLM",
        )

    if not settings.openrouter_api_key:
        raise HTTPException(
            503,
            "OPENROUTER_API_KEY is not configured; either set it or POST with offline=true.",
        )
    agent = Agent(db=db, settings=settings, run_store=run_store)
    if req.playbook == "offboarding_leakage":
        return offboarding_leakage_llm(
            agent, limit=_bounded_limit(req.parameters)
        )
    if req.playbook == "access_explain":
        person = req.parameters.get("person")
        resource = req.parameters.get("resource")
        if not person:
            raise HTTPException(400, "parameters.person is required")
        return access_explain_llm(agent, person, resource)
    if not req.question:
        raise HTTPException(400, "question or playbook is required")
    return agent.investigate(question=req.question, parameters=req.parameters)


@app.get("/investigations")
def list_investigations() -> list[dict]:
    out = []
    for rid in run_store.list_ids():
        inv = run_store.load(rid)
        if not inv:
            continue
        out.append(
            {
                "id": inv.id,
                "status": inv.status,
                "verified": inv.verified,
                "question": inv.question,
                "created_at": inv.created_at,
                "snapshot_time": inv.snapshot_time,
            }
        )
    return out


@app.get("/investigations/{inv_id}", response_model=Investigation)
def get_investigation(inv_id: str) -> Investigation:
    inv = run_store.load(inv_id)
    if not inv:
        raise HTTPException(404, "not found")
    return inv


@app.get("/investigations/{inv_id}/report", response_class=PlainTextResponse)
def get_report(inv_id: str) -> str:
    inv = run_store.load(inv_id)
    if not inv:
        raise HTTPException(404, "not found")
    return inv.report_md
