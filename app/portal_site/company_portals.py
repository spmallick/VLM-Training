from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PORTAL_POLICY_DIR = Path(__file__).resolve().parent / "policies"


@dataclass(frozen=True)
class PortalCompanyConfig:
    slug: str
    name: str
    tagline: str
    description: str
    template_name: str
    policy_path: Path
    cta_label: str
    field_teasers: tuple[str, ...]


PORTAL_COMPANIES: dict[str, PortalCompanyConfig] = {
    "soberstack": PortalCompanyConfig(
        slug="soberstack",
        name="SoberStack Consulting",
        tagline="Simple expense form with a policy popup",
        description=(
            "A straightforward expense report that accepts food, lodging, travel, or other receipts, "
            "with policy guidance hidden behind a small pop-up link."
        ),
        template_name="company_soberstack.html",
        policy_path=PORTAL_POLICY_DIR / "soberstack_consulting.md",
        cta_label="Open the SoberStack portal",
        field_teasers=("Receipt type", "Claim amount", "Policy popup", "Upload receipt"),
    ),
    "stingy": PortalCompanyConfig(
        slug="stingy",
        name="Stingy Corp.",
        tagline="A multi-step reimbursement workflow",
        description=(
            "A step-by-step UI where the user reads the policy first, completes the expense form "
            "second, and reviews everything before submission."
        ),
        template_name="company_stingy.html",
        policy_path=PORTAL_POLICY_DIR / "stingy_corp.md",
        cta_label="Open the Stingy Corp. portal",
        field_teasers=("Step 1 policy", "Step 2 form", "Step 3 review", "Agree and submit"),
    ),
    "china": PortalCompanyConfig(
        slug="china",
        name="China Manufacturing Corp.",
        tagline="A reimbursement form written in Chinese",
        description=(
            "A localized portal with Chinese labels and instructions so the same agent has to adapt "
            "to a different language as well as a different layout."
        ),
        template_name="company_china.html",
        policy_path=PORTAL_POLICY_DIR / "china_manufacturing_corp.md",
        cta_label="Open the China Manufacturing portal",
        field_teasers=("报销类型", "发票总额", "申请金额", "上传凭证"),
    ),
}


def list_portal_companies() -> list[PortalCompanyConfig]:
    return list(PORTAL_COMPANIES.values())


def get_portal_company(slug: str) -> PortalCompanyConfig:
    company = PORTAL_COMPANIES.get(slug)
    if company is None:
        raise KeyError(slug)
    return company


def load_portal_policy_document(policy_path: Path) -> tuple[str, list[str]]:
    title = "Reimbursement Policy"
    bullets: list[str] = []
    for line in policy_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            title = stripped.removeprefix("# ").strip()
        elif stripped.startswith("- "):
            bullets.append(stripped.removeprefix("- ").strip())
    return title, bullets

