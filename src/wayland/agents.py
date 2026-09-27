"""Application-local analytical roles and their one-way reporting hierarchy."""

from dataclasses import dataclass

COORDINATOR_ID = "wayland:strategist"


@dataclass(frozen=True)
class AgentRole:
    agent_id: str
    name: str
    purpose: str


REGISTRY = (
    AgentRole(
        "wayland:market-analyst",
        "Market Analyst",
        "Assess supplied price, volume, currency and quote freshness. Explain market conditions and missing observations; do not invent live data.",
    ),
    AgentRole(
        "wayland:technical-analyst",
        "Technical Analyst",
        "Review supplied technical evidence and the deterministic rebound/reversal setup. Explain confirmation and invalidation; do not invent candles or indicators.",
    ),
    AgentRole(
        "wayland:news-analyst",
        "News Analyst",
        "Review supplied news, source timestamps and potential catalysts. Separate sourced facts from interpretation; explicitly report when no news feed or evidence is available.",
    ),
    AgentRole(
        "wayland:options-analyst",
        "Options Analyst",
        "Review supplied qualified option contracts, liquidity, spreads, expiry, multiplier and bounded risk. Identify missing data; deterministic risk code independently calculates executable limits.",
    ),
    AgentRole(
        COORDINATOR_ID,
        "Coordinator",
        "Act as Strategist and sole analytical coordinator. Request independent specialist reports, resolve disagreements, and explain the combined evidence and uncertainty. Never place orders or override deterministic risk.",
    ),
)
SPECIALISTS = tuple(role for role in REGISTRY if role.agent_id != COORDINATOR_ID)

ROLE_HELP = (
    "\n\n## Default roles\n"
    + "\n\n".join(
        "### "
        + role.name
        + (" (Strategist)" if role.agent_id == COORDINATOR_ID else "")
        + "\n"
        + role.purpose
        for role in (REGISTRY[-1], *SPECIALISTS)
    )
    + """

## How the roles communicate
You → Coordinator → specialist → Coordinator → you.
Specialists do not message each other or read each other's conversations.
A qualifying market event automatically requests up to four independent reports,
then a typed proposal (maximum five model requests). Ordinary quotes do not call models.
Chat coordination first plans distinct assignments, then requests four reports and a synthesis
(maximum six model requests). Specialists receive scoped evidence and new request IDs.
The risk engine gates execution; no prompt or per-order approval is required.
You can also request a coordinated review through chat, which remains analytical.
Full IBKR execution reconciliation is still required before real paper orders.
Missing, archived or busy specialists are reported as unavailable.
You may also ask a specialist directly for a single-role answer.
The Plan tab shows collection progress; each specialist's chat shows its assignment
and report. Additional Coordinator prompts are queued; /interrupt stops the run.

## Decision boundaries
These are analytical conversations, not trade orders. The trading event pipeline
separately validates typed proposals with RiskEngine and ExecutionEngine.
Neither Coordinator nor a specialist can approve orders, change risk limits or
bypass broker reconciliation. No model has broker tools.
"""
)
