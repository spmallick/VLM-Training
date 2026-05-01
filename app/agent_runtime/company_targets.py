from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCompanyTarget:
    slug: str
    name: str
    portal_path: str


AGENT_COMPANY_TARGETS: dict[str, AgentCompanyTarget] = {
    "soberstack": AgentCompanyTarget(
        slug="soberstack",
        name="SoberStack Consulting",
        portal_path="/consultant-demo/soberstack",
    ),
    "stingy": AgentCompanyTarget(
        slug="stingy",
        name="Stingy Corp.",
        portal_path="/consultant-demo/stingy",
    ),
    "china": AgentCompanyTarget(
        slug="china",
        name="China Manufacturing Corp.",
        portal_path="/consultant-demo/china",
    ),
}


def list_agent_company_targets() -> list[AgentCompanyTarget]:
    return list(AGENT_COMPANY_TARGETS.values())


def get_agent_company_target(slug: str) -> AgentCompanyTarget:
    target = AGENT_COMPANY_TARGETS.get(slug)
    if target is None:
        raise KeyError(slug)
    return target
