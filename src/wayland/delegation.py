"""Scoped coordinator planning and role-specific evidence envelopes."""

import asyncio
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agents import SPECIALISTS

TASK_KEYS = {
    "wayland:market-analyst": "market_task",
    "wayland:technical-analyst": "technical_task",
    "wayland:news-analyst": "news_task",
    "wayland:options-analyst": "options_task",
}
COMMON = (
    "snapshot_id",
    "symbol",
    "timestamp",
    "price",
    "usd_sek",
    "fx_timestamp",
    "connected",
    "live",
    "volume",
    "bid",
    "ask",
    "last",
    "spread",
    "market_session",
    "source",
    "data_type",
    "session_source",
    "timestamp_kind",
)
SETUP = ("event", "phase", "low", "high", "date", "snapshot_id", "reason", "last_analysis")


class AssignmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=400)
    language: str = Field(min_length=1, max_length=40)
    market_task: str = Field(min_length=10, max_length=1800)
    technical_task: str = Field(min_length=10, max_length=1800)
    news_task: str = Field(min_length=10, max_length=1800)
    options_task: str = Field(min_length=10, max_length=1800)

    @model_validator(mode="after")
    def distinct_tasks(self):
        values = [getattr(self, key).strip().casefold() for key in TASK_KEYS.values()]
        if len(set(values)) != len(values):
            raise ValueError("Specialists require distinct tasks")
        return self


def evidence_for(role_id, context):
    market = context.get("quote") or context.get("market") or {}
    evidence = {
        "market": {k: market[k] for k in COMMON if k in market},
        "operating_state": context.get("operating_state"),
        "state_reasons": context.get("state_reasons"),
    }
    quality_key = {
        "wayland:market-analyst": "quote",
        "wayland:technical-analyst": "history",
        "wayland:news-analyst": "news",
        "wayland:options-analyst": "options",
    }[role_id]
    evidence["data_quality"] = (context.get("data_quality") or {}).get(quality_key)
    evidence["entry_policy"] = context.get("entry_policy")
    if role_id == "wayland:technical-analyst":
        setup = context.get("setup") or {}
        evidence["setup"] = {k: setup[k] for k in SETUP if k in setup}
        evidence["price_history"] = context.get("price_history")
        evidence["indicators"] = context.get("indicators")
    elif role_id == "wayland:news-analyst":
        evidence["news"] = context.get("news") or []
    elif role_id == "wayland:options-analyst":
        evidence["market"]["options"] = (context.get("market") or {}).get("options") or []
        evidence["candidates"] = context.get("candidates") or []
    return evidence


async def plan_assignments(runtime, text, context, config, cancel, run_id, autonomous=False):
    if autonomous:
        # The event pipeline already defines a fixed analytical objective; no extra model request.
        return AssignmentPlan(
            objective="Review this fresh market setup and report only supported evidence.",
            language="English",
            **{TASK_KEYS[r.agent_id]: r.purpose for r in SPECIALISTS},
        )
    from .providers.cli_transport import analyze

    instructions = (
        "You are the Wayland Coordinator preparing assignments, not executing analysis. "
        "Rewrite the user's request into four distinct, self-contained specialist tasks in the user's language. "
        "Preserve the subject and constraints, but do NOT paste the original prompt into any task. "
        "Each task must concern only that specialist's expertise. Market: prices/volume/FX/freshness. "
        "Technical: supplied price history and rebound/reversal evidence. News: supplied sourced news/catalysts. "
        "Options: supplied contracts/liquidity/expiry/spread/bounded risk. "
        "Requests to start agents, collect reports, verify orchestration or summarize everybody belong ONLY to "
        "the Coordinator/runtime. Never delegate those responsibilities to a specialist. "
        "For an orchestration test, ask each specialist for its own fresh evidence/missing-data report. "
        "No tools, fabricated data, trades, risk changes or TradeProposal. Runtime will dispatch your tasks. "
        "The objective is a short summary, not the raw prompt. Missing evidence is a valid analytical outcome."
    )
    supplied = {
        "user_request": text,
        "available_input_fields": {
            role.name: [k for k, v in evidence_for(role.agent_id, context).items() if v]
            for role in SPECIALISTS
        },
    }
    request_id = str(uuid.uuid4())
    with runtime.store() as store:
        store.event(
            "coordination.plan.input",
            {
                "coordinator_run_id": run_id,
                "request_id": request_id,
                "model": config.openai_model,
                "instructions": instructions,
                "input": supplied,
                "schema": AssignmentPlan.model_json_schema(),
            },
            run_id,
        )
    task = asyncio.create_task(
        analyze(runtime.root, config, instructions, supplied, AssignmentPlan.model_json_schema())
    )
    try:
        while not task.done():
            if cancel.is_set():
                task.cancel()
                raise asyncio.CancelledError
            await asyncio.sleep(0.1)
        response = await task
        plan = AssignmentPlan.model_validate(response.output)
        if len(text.strip()) >= 40 and any(text.strip() in getattr(plan, k) for k in TASK_KEYS.values()):
            raise ValueError("Coordinator copied the entire request")
        with runtime.store() as store:
            store.event(
                "coordination.plan.response",
                {
                    "coordinator_run_id": run_id,
                    "request_id": request_id,
                    "provider_response_id": response.id if response.id != "codex-exec" else None,
                    "model": response.model,
                    "plan": plan.model_dump(),
                },
                run_id,
            )
        return plan
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def assignment_for(role, plan, run_id, request_id):
    return (
        f"Coordinator → {role.name}\n\n"
        f"{getattr(plan, TASK_KEYS[role.agent_id])}\n\n"
        f"Answer in {plan.language}. This is a new, independent specialist request. "
        "Use only the evidence supplied with this request. State your own conclusion, supporting evidence "
        "and missing inputs. Do not start agents, summarize other specialists, verify their calls or create "
        "a TradeProposal. The runtime handles dispatch and request tracking. "
        f"Report only to Coordinator.\nRun: {run_id}\nRequest: {request_id}"
    )
