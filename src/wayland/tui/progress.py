"""Display only time estimates explicitly supplied by the agent with its plan."""

import re
import time

PROGRESS_CONTEXT = """The user requires a visible plan for EVERY request, even a single
step, a small edit, or a short answer. Before doing the work, publish a concise
plan using update_plan, then maintain its step statuses as you work. A simple
request needs just one step. Mark steps completed only when actually finished.
If update_plan is unavailable, publish an assistant message beginning with
'Plan:' followed by Markdown checklist lines '- [ ] Step', '- [>] Current step',
or '- [x] Completed step'. Send it as a separate message containing only the
checklist and optional ETA so the dashboard routes it to the Plan tab and hides
it from Chat. Republish the full checklist when statuses change.
Use the user's language for the step text. Never skip the plan for simple tasks.
Keep the plan aligned with the ongoing conversation, including messages that
arrive while you are working. At the next safe checkpoint, incorporate each
actionable addition before taking further work actions: publish the updated full
plan immediately, mark the current step in progress, and add a task for a distinct outcome or a
subtask for work belonging to an existing step. Keep unfinished work and its
statuses unless the user explicitly cancels or replaces it. For flat plan tools,
name subtasks 'Parent task: subtask' so their relationship remains visible.
Publish the revised full plan through the plan tool or fallback checklist.
Keep task details and priorities in the Plan tab only. Give a normal, useful
chat reply with relevant answers or findings, then end that reply with
"Plan uppdaterad" for Swedish or "Plan updated" for English when the plan was
updated. Do not replace the ordinary reply with only that acknowledgement.
A plan is a progress update, not completion. Continue authorized work after
publishing it. If you must stop for user input, explain the specific blocker
and ask a concrete question in a normal chat message; use the question tool
when available. Do not silently end the turn after publishing only a plan.
Do not repeat, summarize or enumerate plan steps in chat. Send any fallback
checklist in a separate message, which the dashboard hides from Chat.
Do not turn every discussion message into work: answer questions briefly and
add a task only when the user requests work or the discussion changes the work.
Infer priority from the user's intent and dependencies; ask only when a material
ambiguity prevents progress. Never claim a task was added without updating the
published plan. Complete a task only after its requested outcome is achieved.
When you can reasonably estimate the
remaining work, include a separate line in its explanation exactly like
'ETA: 5-10 min' (a range of whole minutes remaining at the time of that update).
Update the estimate when the plan or scope changes. If you cannot estimate
reliably, omit the ETA line; never invent precision. In a checklist message,
the optional ETA line goes directly after its steps.
Continue working normally; do not ask for confirmation just to publish a plan.
"""


PLAN_MESSAGE = re.compile(
    r"(?:\A|\n[ \t]*\n)[ \t]*(?:\*\*)?Plan:(?:\*\*)?\s*\n(?P<steps>(?:[ \t]*- \[[ xX>]\] [^\n]+\n?)+)(?P<rest>.*)",
    re.DOTALL,
)


def plan_match(text):
    for match in PLAN_MESSAGE.finditer(text):
        # A quoted code example is not a published plan.
        prefix = text[: match.start()]
        if len(re.findall(r"^\s*```", prefix, re.MULTILINE)) % 2 == 0:
            return match
    return None


def message_plan(text):
    """Read the explicit fallback checklist only, never ordinary prose or tool output."""
    match = plan_match(text)
    if not match:
        return None
    steps = []
    for mark, title in re.findall(r"^[ \t]*- \[([ xX>])\] ([^\n]+)", match["steps"], re.MULTILINE):
        steps.append(
            {
                "step": title.strip(),
                "status": "completed" if mark.lower() == "x" else "inProgress" if mark == ">" else "pending",
            }
        )
    return steps, parse_estimate(match["rest"])


def conversation_text(item):
    """Hide the agent's plan block while preserving its ordinary reply and user text."""
    text = item.get("text", "")
    if item.get("type") != "agentMessage":
        return text
    match = plan_match(text)
    if not match:
        # Avoid flashing the beginning of a checklist while it is streaming.
        if re.fullmatch(r"\s*(?:\*\*)?Plan:(?:\*\*)?\s*(?:-\s*(?:\[[ xX>]?\]?)?)?", text):
            return ""
        return text
    prefix = text[: match.start()].rstrip()
    remainder = match["rest"].lstrip()
    remainder = re.sub(
        r"\AETA:\s*\d+\s*[-–]\s*\d+\s+min[^\S\n]*(?:\n|$)", "", remainder, count=1, flags=re.IGNORECASE
    ).lstrip()
    if re.fullmatch(r"(?:-\s*(?:\[[ xX>]?\]?)?|ETA(?::[^\n]*)?)", remainder):
        return prefix
    return "\n\n".join(part for part in (prefix, remainder) if part)


def parse_estimate(explanation, now=None):
    match = re.search(
        r"^ETA:\s*(\d+)\s*[-–]\s*(\d+)\s+min\s*$", explanation or "", re.MULTILINE | re.IGNORECASE
    )
    if not match:
        return None
    low, high = map(int, match.groups())
    if not 0 <= low <= high or not 1 <= high <= 10080:
        return None
    return {"low": low, "high": high, "at": time.time() if now is None else now}


def estimate_label(thread, label, now=None):
    if label in ("approval", "question"):
        return "Time: waiting for your response"
    if label not in ("working", "quiet (active)"):
        return ""
    estimate = thread.get("estimate")
    if not estimate:
        return "Time remaining: not estimated"
    age = max(0, (time.time() if now is None else now) - estimate["at"])
    if age >= estimate["high"] * 60:
        return "Time estimate needs updating"
    # Keep the original range and its age visible, rather than inventing a countdown.
    when = "just now" if age < 60 else "%d min ago" % (age // 60)
    return "About %d–%d min left · estimated %s" % (estimate["low"], estimate["high"], when)


INTERVENTION_CONTEXT = """The user explicitly intervenes in the current work.
At the next safe checkpoint, read this message before taking more planned actions.
Keep the original objective and unfinished work unless the user cancels them.
If this adds context or corrects direction, briefly acknowledge it and update the
existing plan. If it clearly requests a new priority, insert the task in the
appropriate place. Ask whether to do it first only if the intended priority is
materially ambiguous. Do not discard earlier tasks or pretend a running tool was
stopped. If there is no active work, handle this as an ordinary request.
Keep the checklist in Plan and end the normal reply with 'Plan uppdaterad' when
updating it, or 'Plan updated' when speaking English.
"""


def plan_only_stop(thread, items):
    """Explain a completed plan-only response without inventing an approval request."""
    if thread.get("lastTurnStatus") != "completed" or thread.get("status", {}).get("type") != "idle":
        return None
    start = next(
        (i + 1 for i in range(len(items) - 1, -1, -1) if items[i].get("type") == "userMessage"), len(items)
    )
    tail = items[start:]
    plans = [
        item for item in tail if item.get("type") == "agentMessage" and message_plan(item.get("text", ""))
    ]
    if not plans or any(
        conversation_text(item).strip() for item in tail if item.get("type") == "agentMessage"
    ):
        return None
    if any(item.get("type") not in ("agentMessage", "reasoning", "plan") for item in tail):
        return None
    if not any(step["status"] != "completed" for step in message_plan(plans[-1]["text"])[0]):
        return None
    return {
        "id": "hub-plan-stop-" + str(plans[-1].get("id", "")),
        "type": "appNotice",
        "text": "The agent ended its response after publishing a plan. No tool activity was recorded, and no question or approval request was provided. To start the investigation, send a message asking it to continue.",
    }
