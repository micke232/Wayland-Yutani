"""UI/event adapter around the deterministic fixed-role AgentOrchestrator."""

import asyncio
import time
import uuid

from .agents import COORDINATOR_ID, SPECIALISTS
from .delegation import plan_assignments
from .orchestration import (
    AgentOrchestrator,
    RunJournal,
    RunStatus,
    SpecialistProvider,
    SpecialistRunner,
    render_run,
    validated_reports,
)


def report_text(run):
    header = f"**Report to Coordinator**\nRun: {run.request.specialist_run_id}\nRequest: {run.request.request_id}\n"
    if run.result is None:
        return header + "\n" + run.status + " · " + (run.error_type or "No report available")
    report = run.result
    return (
        header
        + "\n"
        + report.summary
        + ("\n\nFindings\n" + "\n".join("• " + item for item in report.findings) if report.findings else "")
        + (
            "\n\nMissing data\n" + "\n".join("• " + item for item in report.missing_data)
            if report.missing_data
            else ""
        )
    )


async def coordinate(runtime, text, context, config, cancel, synthesize=None):
    run_id = str(uuid.uuid4())
    journal = RunJournal(runtime.root / "state/wayland.sqlite")
    journal.start(
        run_id,
        "market_event" if synthesize else "chat",
        text,
        model=config.openai_model or "CLI default (not reported)",
    )
    with runtime.lock, runtime.store() as store:
        threads = store.get("ui:threads")
        queued_steps = [
            s for s in threads[COORDINATOR_ID]["plan"] if s["step"] == "Incorporate the additional prompt"
        ]
        threads[COORDINATOR_ID]["plan"] = [
            {"step": "Prepare distinct specialist assignments", "status": "inProgress"},
            *[{"step": "Collect " + role.name + " report", "status": "pending"} for role in SPECIALISTS],
            {"step": "Synthesize evidence and disagreements", "status": "pending"},
            *queued_steps,
        ]
        store.set("ui:threads", threads)
    try:
        assignments = await plan_assignments(
            runtime, text, context, config, cancel, run_id, autonomous=synthesize is not None
        )
    except asyncio.CancelledError:
        journal.finish(run_id, "CANCELLED")
        return ""
    except Exception as error:  # noqa: BLE001 -- no raw provider errors in UI
        context["coordination_failed"] = True
        journal.finish(run_id, "FAILED", {"stage": "planning", "error_type": type(error).__name__})
        with runtime.lock, runtime.store() as store:
            threads = store.get("ui:threads")
            threads[COORDINATOR_ID]["plan"][0]["status"] = "pending"
            store.set("ui:threads", threads)
        return "Coordinator could not prepare assignments. No specialists were called. Run: " + run_id
    with runtime.lock, runtime.store() as store:
        threads = store.get("ui:threads")
        threads[COORDINATOR_ID]["plan"][0]["status"] = "completed"
        store.set("ui:threads", threads)
        store.event("orchestration.plan.validated", assignments.model_dump(mode="json"), run_id)

    indexes = {role.agent_id: i for i, role in enumerate(SPECIALISTS, 1)}

    def started(request):
        with runtime.lock, runtime.store() as store:
            threads = store.get("ui:threads")
            child = threads.get(request.role)
            if (
                not child
                or child.get("bucket") != "threads"
                or request.role in runtime.active
                or request.role in runtime.delegated
            ):
                threads[COORDINATOR_ID]["plan"][indexes[request.role]]["status"] = "completed"
                store.set("ui:threads", threads)
                return False
            runtime.delegated[request.role] = cancel
            child["status"], child["startedAt"] = {"type": "active"}, time.time()
            child["plan"] = [
                {
                    "step": "Complete own specialist assignment and report to Coordinator",
                    "status": "inProgress",
                }
            ]
            role = next(r for r in SPECIALISTS if r.agent_id == request.role)
            child["items"].append(
                runtime.message(
                    "agentMessage",
                    request.task,
                    senderRole="coordinator",
                    recipientName=role.name,
                    coordinationId=run_id,
                    requestId=request.request_id,
                )
            )
            threads[COORDINATOR_ID]["plan"][indexes[request.role]]["status"] = "inProgress"
            store.set("ui:threads", threads)
        return True

    def finished(run):
        with runtime.lock, runtime.store() as store:
            threads = store.get("ui:threads")
            child = threads[run.request.role]
            child["items"].append(
                runtime.message(
                    "agentMessage",
                    report_text(run),
                    coordinationId=run_id,
                    requestId=run.request.request_id,
                )
            )
            complete = run.status == RunStatus.COMPLETED
            child["status"] = {"type": "idle" if complete else "waiting"}
            child["plan"][0]["status"] = "completed" if complete else "pending"
            child["durationMs"] = int((time.time() - child.pop("startedAt", time.time())) * 1000)
            threads[COORDINATOR_ID]["plan"][indexes[run.request.role]]["status"] = "completed"
            store.set("ui:threads", threads)
            runtime.delegated.pop(run.request.role, None)

    provider = getattr(runtime, "specialist_provider", None) or SpecialistProvider(runtime.root, config)
    runner = SpecialistRunner(provider, journal, config.analysis_timeout_seconds)
    orchestrator = AgentOrchestrator(journal, runner)
    runs = await orchestrator.collect(
        assignments,
        context,
        cancel,
        run_id=run_id,
        trigger="market_event" if synthesize else "chat",
        on_start=started,
        on_finish=finished,
    )
    if cancel.is_set():
        return ""
    reports = validated_reports(runs, run_id)
    with runtime.lock, runtime.store() as store:
        threads = store.get("ui:threads")
        threads[COORDINATOR_ID]["plan"][len(SPECIALISTS) + 1]["status"] = "inProgress"
        threads[COORDINATOR_ID]["items"].append(
            runtime.message(
                "appNotice",
                render_run(journal.read(run_id)),
                coordinationId=run_id,
            )
        )
        store.set("ui:threads", threads)
    final_context = dict(
        context, analyst_reports=reports, coordination_id=run_id, request_id=str(uuid.uuid4())
    )
    try:
        result = (
            await synthesize(reports)
            if synthesize
            else await runtime.model_reply(COORDINATOR_ID, text, final_context, config, cancel)
        )
        status = "CANCELLED" if cancel.is_set() else "COMPLETED" if result else "FAILED"
        journal.finish(
            run_id,
            status,
            result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
            {"request_id": final_context["request_id"], **final_context.get("request_trace", {})}
            if not synthesize
            else {"source": "market-event typed proposal"},
        )
        return result
    except asyncio.CancelledError:
        journal.finish(run_id, "CANCELLED")
        raise
    except Exception:
        journal.finish(run_id, "FAILED")
        raise
