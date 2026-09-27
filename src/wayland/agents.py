"""Application-local analytical roles, not discoverable CLI agent sessions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRole:
    agent_id: str
    purpose: str


REGISTRY = tuple(
    AgentRole("wayland:" + name, purpose)
    for name, purpose in (
        ("market-analyst", "Price, volume and market context"),
        ("technical-analyst", "Deterministic setup evidence"),
        ("news-analyst", "Supplied catalysts and source timestamps"),
        ("options-analyst", "Qualified contracts, liquidity and bounded risk"),
        ("strategist", "WAIT or a typed proposal from supplied evidence"),
    )
)
