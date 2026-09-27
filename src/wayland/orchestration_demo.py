"""Broker-free, explicitly labelled fixture execution of the production orchestrator."""

import asyncio
import threading

from .agents import SPECIALISTS
from .delegation import TASK_KEYS, AssignmentPlan
from .demo import fixture
from .orchestration import AgentOrchestrator, ProviderReply, RunJournal, SpecialistRunner, validated_reports


class FixtureSpecialistProvider:
    name = "fixture"
    model = "fixture-model"

    async def analyze(self, request):
        await asyncio.sleep(0)
        return ProviderReply(
            model=self.model,
            output={
                "coordinator_run_id": request.coordinator_run_id,
                "specialist_run_id": request.specialist_run_id,
                "request_id": request.request_id,
                "role": request.role,
                "summary": "SYNTHETIC FIXTURE: independent report for " + request.role,
                "findings": ["Only supplied fixture context was reviewed."],
                "missing_data": ["No real market/news feed was requested."],
            },
        )


async def run_demo(root):
    journal = RunJournal(root / "data/orchestration-demo/wayland.sqlite")
    provider = FixtureSpecialistProvider()
    runner = SpecialistRunner(provider, journal, timeout=2)
    market, _, _ = fixture()
    assignments = AssignmentPlan(
        objective="Verify the agent chain using synthetic fixture input, without a broker or real model.",
        language="English",
        **{TASK_KEYS[r.agent_id]: r.purpose for r in SPECIALISTS},
    )
    runs = await AgentOrchestrator(journal, runner).collect(
        assignments,
        {"market": market.model_dump(mode="json"), "news": []},
        threading.Event(),
        trigger="fixture",
    )
    run_id = runs[0].request.coordinator_run_id
    reports = validated_reports(runs, run_id)
    journal.finish(
        run_id,
        "COMPLETED",
        {
            "fixture_synthesis": True,
            "reports": reports,
            "summary": "Four independent fixture reports collected. No TradeProposal or order created.",
        },
    )
    return journal.read(run_id)
