import asyncio
import threading

import pytest

from wayland.agents import COORDINATOR_ID, REGISTRY, SPECIALISTS
from wayland.config import app_root
from wayland.coordination import coordinate
from wayland.delegation import TASK_KEYS, AssignmentPlan
from wayland.operator import OperatorRuntime
from wayland.orchestration import RunJournal
from wayland.orchestration_demo import FixtureSpecialistProvider
from wayland.tui.ui import HELP


class SpyProvider(FixtureSpecialistProvider):
    def __init__(self):
        self.requests = []

    async def analyze(self, request):
        self.requests.append(request)
        return await super().analyze(request)


@pytest.fixture(autouse=True)
def scoped_planner(monkeypatch):
    async def plan(*args, **kwargs):
        return AssignmentPlan(
            objective="Fresh ORCL evidence review",
            language="Swedish",
            **{TASK_KEYS[r.agent_id]: r.purpose for r in SPECIALISTS},
        )

    monkeypatch.setattr("wayland.coordination.plan_assignments", plan)
    monkeypatch.setattr(OperatorRuntime, "specialist_provider", SpyProvider(), raising=False)


def test_default_roles_documented_and_renames_preserved(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    assert all(role.name in HELP and role.purpose in HELP for role in REGISTRY)
    runtime.dispatch("rename", threadId=COORDINATOR_ID, name="My Coordinator")
    restarted = OperatorRuntime(runtime.root)
    assert restarted.dispatch("snapshot")["threads"][COORDINATOR_ID]["name"] == "My Coordinator"


def test_hub_and_spoke_passes_four_structured_reports_to_coordinator(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    calls = []

    async def reply(tid, text, context, config, cancel):
        calls.append(tid)
        assert tid == COORDINATOR_ID
        assert len(context["analyst_reports"]) == 4
        assert all(report["result"]["summary"] for report in context["analyst_reports"].values())
        assert all("history" not in report for report in context["analyst_reports"].values())
        return "Combined answer"

    runtime.model_reply = reply
    result = asyncio.run(
        coordinate(
            runtime,
            "Start a new full ORCL analysis and call all agents",
            {"market": {"symbol": "ORCL"}},
            runtime.settings(),
            threading.Event(),
        )
    )
    assert result == "Combined answer" and calls == [COORDINATOR_ID]
    requests = runtime.specialist_provider.requests
    assert len(requests) == 4 and len({r.task for r in requests}) == 4
    assert all("Start a new full ORCL analysis and call all agents" not in r.task for r in requests)
    assert not runtime.delegated
    record = RunJournal(runtime.root / "state/wayland.sqlite").read()
    assert record["status"] == "COMPLETED"
    assert record["synthesis"] == "Combined answer"
    with runtime.store() as store:
        for role in SPECIALISTS:
            thread = store.get("ui:threads")[role.agent_id]
            assert thread["status"]["type"] == "idle"
            assert "Report to Coordinator" in thread["items"][-1]["text"]
            assert thread["items"][-2]["senderRole"] == "coordinator"


def test_archived_and_failed_specialists_explicitly_missing_but_synthesis_runs(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    runtime.dispatch("archive", threadId=SPECIALISTS[0].agent_id)

    class Failing(SpyProvider):
        async def analyze(self, request):
            if request.role == SPECIALISTS[1].agent_id:
                raise TimeoutError("must not expose raw credentials")
            return await super().analyze(request)

    runtime.specialist_provider = Failing()

    async def reply(tid, text, context, config, cancel):
        assert tid == COORDINATOR_ID
        reports = context["analyst_reports"]
        assert reports[SPECIALISTS[0].name]["status"] == "UNAVAILABLE"
        assert reports[SPECIALISTS[0].name]["result"] is None
        assert reports[SPECIALISTS[1].name]["status"] == "TIMED_OUT"
        assert reports[SPECIALISTS[1].name]["result"] is None
        assert reports[SPECIALISTS[2].name]["status"] == "COMPLETED"
        return "Two reports available, two missing."

    runtime.model_reply = reply
    assert asyncio.run(coordinate(runtime, "Review", {}, runtime.settings(), threading.Event()))
    assert not runtime.delegated
    with runtime.store() as store:
        assert "must not expose raw credentials" not in str(store.recent(100))


def test_interrupt_cancels_run_and_never_synthesizes(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    cancel = threading.Event()

    class Interrupting(SpyProvider):
        async def analyze(self, request):
            cancel.set()
            await asyncio.sleep(60)

    runtime.specialist_provider = Interrupting()

    async def forbidden(*args):
        raise AssertionError("cancelled run reached synthesis")

    runtime.model_reply = forbidden
    assert asyncio.run(coordinate(runtime, "Review", {}, runtime.settings(), cancel)) == ""
    assert not runtime.delegated
    assert RunJournal(runtime.root / "state/wayland.sqlite").read()["status"] == "CANCELLED"


def test_synthesis_never_receives_prior_chat_history(tmp_path, monkeypatch):
    from types import SimpleNamespace

    runtime = OperatorRuntime(app_root(tmp_path))
    with runtime.store() as store:
        threads = store.get("ui:threads")
        threads[COORDINATOR_ID]["items"].append(runtime.message("agentMessage", "OLD CACHED REPORT"))
        store.set("ui:threads", threads)
    seen = []

    async def transport(root, config, instructions, history, schema):
        assert "OLD CACHED REPORT" not in str(history) + instructions
        assert len(history) == 1
        assert "specialist_run_id" in instructions
        seen.append(instructions)
        return SimpleNamespace(
            output={"answer": "Fresh synthesis"}, model="fixture-model", id="fixture-response"
        )

    monkeypatch.setattr("wayland.providers.cli_transport.analyze", transport)
    result = asyncio.run(
        coordinate(runtime, "New complete analysis", {}, runtime.settings(), threading.Event())
    )
    assert result == "Fresh synthesis" and len(seen) == 1
    record = RunJournal(runtime.root / "state/wayland.sqlite").read()
    assert record["synthesis_metadata"]["model"] == "fixture-model"


def test_planning_failure_records_no_calls_and_pending_plan(tmp_path, monkeypatch):
    runtime = OperatorRuntime(app_root(tmp_path))

    async def failed(*args, **kwargs):
        raise ValueError("private invalid response")

    monkeypatch.setattr("wayland.coordination.plan_assignments", failed)
    context = {}
    result = asyncio.run(coordinate(runtime, "Fresh review", context, runtime.settings(), threading.Event()))
    assert "No specialists were called" in result
    assert context["coordination_failed"]
    assert not runtime.specialist_provider.requests
    with runtime.store() as store:
        assert store.get("ui:threads")[COORDINATOR_ID]["plan"][0]["status"] == "pending"
    assert RunJournal(runtime.root / "state/wayland.sqlite").read()["status"] == "FAILED"


def test_chat_orchestration_never_calls_broker_or_execution_tools(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from wayland.broker.ibkr import IbkrBroker
    from wayland.execution import ExecutionEngine

    blocked = AsyncMock(side_effect=AssertionError("Chat has no execution capability"))
    for method in ("connect", "submit", "cancel"):
        monkeypatch.setattr(IbkrBroker, method, blocked)
    monkeypatch.setattr(ExecutionEngine, "execute", blocked)
    runtime = OperatorRuntime(app_root(tmp_path))

    async def reply(*args):
        return "Analytical synthesis only."

    runtime.model_reply = reply
    asyncio.run(coordinate(runtime, "New full analysis", {}, runtime.settings(), threading.Event()))
    blocked.assert_not_called()
    with runtime.store() as store:
        assert store.intents() == []
