from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .schemas import (
    BlurCheckResult,
    ExpenseFields,
    ExtractionPayload,
    ExtractionTemplate,
    MemoryFact,
    PageRequirement,
    WorkingMemory,
)
from .tools import CurrencyConverter


CORE_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "expense_date",
    "receipt_total",
    "subtotal_amount",
    "tax_amount",
    "currency_code",
    "expense_category",
    "payment_method",
    "business_purpose",
)

GENERIC_POLICY_FIELDS: tuple[str, ...] = (
    "tip_amount",
    "fare_amount",
    "mandatory_fee_amount",
    "alcohol_amount",
    "alcohol_tax_amount",
    "non_business_amount",
    "claim_amount",
    "adjustment_note",
)

PORTAL_ONLY_FIELDS: tuple[str, ...] = (
    "employee_name",
    "cost_center",
    "receipt_upload",
    "policy_acknowledgement",
    "review_acknowledgement",
)

FIELD_MAPPING: dict[str, str] = {
    "vendor": "vendor_name",
    "transaction_date": "expense_date",
    "total": "receipt_total",
    "subtotal": "subtotal_amount",
    "tax": "tax_amount",
    "currency": "currency_code",
    "category": "expense_category",
    "payment_method": "payment_method",
    "notes": "business_purpose",
}

SEMANTIC_TO_EXTRACTION_FIELD: dict[str, str] = {
    "vendor_name": "vendor",
    "expense_date": "transaction_date",
    "receipt_total": "total",
    "subtotal_amount": "subtotal",
    "tax_amount": "tax",
    "currency_code": "currency",
    "expense_category": "category",
    "payment_method": "payment_method",
    "business_purpose": "notes",
    "tip_amount": "tip_amount",
    "fare_amount": "fare_amount",
    "mandatory_fee_amount": "mandatory_fee_amount",
    "alcohol_amount": "alcohol_amount",
    "alcohol_tax_amount": "alcohol_tax_amount",
    "non_business_amount": "non_business_amount",
}

LINE_ITEM_HINTS: dict[str, tuple[str, ...]] = {
    "alcohol": ("beer", "wine", "cocktail", "alcohol", "baijiu", "whiskey"),
    "tip": ("tip", "gratuity"),
}


@dataclass(frozen=True)
class ReimbursementResult:
    claim_amount_local: str
    claim_amount_usd: str
    excluded_amount_local: str
    explanation: str


def _dedupe(items: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _make_fact(value: str, *, source: str, confidence: float, notes: str = "") -> MemoryFact:
    return MemoryFact(value=str(value or "").strip(), source=source, confidence=confidence, notes=notes)


def _money(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_money(value: float) -> str:
    return f"{max(value, 0.0):.2f}"


def _contains_hint(lines: list[str], hint_group: str) -> bool:
    tokens = LINE_ITEM_HINTS.get(hint_group, ())
    lowered = "\n".join(lines).lower()
    return any(token in lowered for token in tokens)


def build_extraction_template(company_slug: str) -> tuple[ExtractionTemplate, list[str]]:
    template = ExtractionTemplate(
        core_fields=list(CORE_FIELDS),
        company_specific_fields={},
        discovered_fields=[],
        active_fields=list(CORE_FIELDS),
    )
    company_fields = _dedupe(list(GENERIC_POLICY_FIELDS) + list(PORTAL_ONLY_FIELDS))
    discovered = [field for field in company_fields if field not in template.core_fields]
    template.company_specific_fields[company_slug or "dynamic_portal"] = company_fields
    template.discovered_fields = discovered
    template.active_fields = _dedupe(template.core_fields + company_fields)
    return template, discovered


def requested_receipt_fields(extraction_template: ExtractionTemplate, page_id: str | None = None) -> list[str]:
    semantic_fields = list(extraction_template.active_fields)
    if page_id:
        semantic_fields = [field for field in semantic_fields if field in extraction_template.active_fields]
    extraction_fields = [
        SEMANTIC_TO_EXTRACTION_FIELD[field]
        for field in semantic_fields
        if field in SEMANTIC_TO_EXTRACTION_FIELD
    ]
    return _dedupe(extraction_fields)


def build_working_memory(
    *,
    company_slug: str,
    receipt_image_path: Path,
    blur_check: BlurCheckResult,
    extraction: ExtractionPayload,
    extraction_template: ExtractionTemplate,
) -> WorkingMemory:
    facts: dict[str, MemoryFact] = {}
    allowed_semantic_fields = set(extraction_template.active_fields)

    for source_name, semantic_name in FIELD_MAPPING.items():
        if semantic_name not in allowed_semantic_fields:
            continue
        value = getattr(extraction.fields, source_name)
        if value:
            facts[semantic_name] = _make_fact(
                value,
                source="receipt",
                confidence=extraction.confidence,
                notes="Seeded from the initial receipt extraction.",
            )

    for semantic_name, value in extraction.semantic_amounts.items():
        if semantic_name not in allowed_semantic_fields:
            continue
        if value:
            facts[semantic_name] = _make_fact(
                value,
                source="receipt",
                confidence=max(0.55, extraction.confidence - 0.1),
                notes="Policy-relevant amount inferred from the receipt.",
            )

    if extraction.line_item_summary:
        facts["line_item_summary"] = _make_fact(
            " | ".join(extraction.line_item_summary),
            source="receipt",
            confidence=max(0.45, extraction.confidence - 0.2),
            notes="Compressed line-item observations from the receipt.",
        )

    return WorkingMemory(
        company_slug=company_slug,
        receipt_image_path=str(receipt_image_path),
        blur_check=blur_check,
        facts=facts,
        derived_values={},
        page_requirements=[],
        visited_pages=[],
        action_log=[
            "Initialized working memory from the consultant intake page.",
            "Stored the first extraction template for the selected portal.",
        ],
    )


def ensure_page_requirement(
    memory: WorkingMemory,
    page_id: str,
    required_fields: list[str] | tuple[str, ...],
    *,
    title: str = "",
) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    desired_fields = _dedupe(list(required_fields))
    discovered_fields = [field for field in desired_fields if field not in CORE_FIELDS]
    for requirement in updated.page_requirements:
        if requirement.page_id != page_id:
            continue
        requirement.required_fields = _dedupe(requirement.required_fields + desired_fields)
        requirement.discovered_fields = _dedupe(requirement.discovered_fields + discovered_fields)
        if title:
            requirement.title = title
        return updated

    updated.page_requirements.append(
        PageRequirement(
            page_id=page_id,
            title=title or page_id.replace("_", " ").title(),
            required_fields=desired_fields,
            discovered_fields=discovered_fields,
            status="pending",
        )
    )
    return updated


def merge_reviewed_fields(memory: WorkingMemory, reviewed_fields: ExpenseFields) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    for source_name, semantic_name in FIELD_MAPPING.items():
        value = getattr(reviewed_fields, source_name)
        if not value:
            continue
        updated.facts[semantic_name] = _make_fact(
            value,
            source="user",
            confidence=1.0,
            notes="Confirmed or edited by the consultant during review.",
        )
    updated.action_log.append("Merged the reviewed form values back into working memory.")
    return updated


def mark_page_observed(memory: WorkingMemory, page_id: str) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    if page_id not in updated.visited_pages:
        updated.visited_pages.append(page_id)
    for requirement in updated.page_requirements:
        if requirement.page_id == page_id:
            requirement.status = "observed"
    updated.action_log.append(f"Observed the {page_id} page and compared it with the extraction template.")
    return updated


def mark_page_filled(memory: WorkingMemory, page_id: str) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    for requirement in updated.page_requirements:
        if requirement.page_id == page_id:
            requirement.status = "filled"
    updated.action_log.append(f"Filled the semantic requirements for {page_id}.")
    return updated


def mark_run_complete(memory: WorkingMemory) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    for requirement in updated.page_requirements:
        requirement.status = "submitted"
    updated.action_log.append("Marked all known page requirements as submitted.")
    return updated


def compute_reimbursement(
    company_slug: str,
    memory: WorkingMemory,
    converter: CurrencyConverter,
    *,
    live_policy_text: str | None = None,
) -> ReimbursementResult:
    facts = memory.facts
    total = _money(facts.get("receipt_total", MemoryFact()).value)
    currency = facts.get("currency_code", MemoryFact(value="USD")).value or "USD"
    explanation = "The requested amount matches the receipt total."
    excluded = 0.0

    policy_text = live_policy_text.lower() if live_policy_text is not None else ""
    use_legacy_policy = live_policy_text is None
    excludes_tip = use_legacy_policy or "tip" in policy_text or "gratu" in policy_text
    excludes_alcohol = use_legacy_policy or "alcohol" in policy_text or "酒" in policy_text
    excludes_non_business = (
        use_legacy_policy
        or "non-business" in policy_text
        or "non business" in policy_text
        or "personal" in policy_text
        or "个人" in policy_text
        or "无关" in policy_text
    )

    exclusions = {
        "tip": _money(facts.get("tip_amount", MemoryFact()).value) if excludes_tip else 0.0,
        "alcohol": _money(facts.get("alcohol_amount", MemoryFact()).value) if excludes_alcohol else 0.0,
        "alcohol_tax": _money(facts.get("alcohol_tax_amount", MemoryFact()).value) if excludes_alcohol else 0.0,
        "non_business": _money(facts.get("non_business_amount", MemoryFact()).value) if excludes_non_business else 0.0,
    }
    excluded = sum(exclusions.values())
    if excluded > 0:
        labels = [label.replace("_", " ") for label, value in exclusions.items() if value > 0]
        explanation = (
            f"Excluded {excluded:.2f} in policy-sensitive charges ({', '.join(labels)}) before writing the claim."
        )
    elif excludes_alcohol and _contains_hint(memory_line_items(memory), "alcohol"):
        explanation = "Possible alcohol items were detected but the amount is unclear, so the claim should be reviewed."
    elif live_policy_text == "":
        explanation = "No live portal policy was discovered, so the demo fallback claims the full receipt amount."

    claim_local = _format_money(total - excluded if total else 0.0)
    claim_usd = converter.convert(claim_local, currency, "USD") or claim_local
    return ReimbursementResult(
        claim_amount_local=claim_local,
        claim_amount_usd=claim_usd,
        excluded_amount_local=_format_money(excluded),
        explanation=explanation,
    )


def apply_reimbursement_to_memory(
    memory: WorkingMemory,
    reimbursement: ReimbursementResult,
    *,
    original_currency: str,
) -> WorkingMemory:
    updated = memory.model_copy(deep=True)
    updated.derived_values["claim_amount_local"] = _make_fact(
        reimbursement.claim_amount_local,
        source="derived",
        confidence=0.95,
        notes=f"Computed in {original_currency or 'USD'} after applying company policy.",
    )
    updated.derived_values["claim_amount"] = _make_fact(
        reimbursement.claim_amount_local,
        source="derived",
        confidence=0.95,
        notes="Semantic alias for the amount that should be written into the portal claim field.",
    )
    updated.derived_values["claim_amount_usd"] = _make_fact(
        reimbursement.claim_amount_usd,
        source="derived",
        confidence=0.95,
        notes="Converted into USD using the local FX table.",
    )
    updated.derived_values["excluded_amount_local"] = _make_fact(
        reimbursement.excluded_amount_local,
        source="derived",
        confidence=0.95,
        notes="The amount removed from the original receipt total.",
    )
    updated.derived_values["adjustment_note"] = _make_fact(
        reimbursement.explanation,
        source="agent",
        confidence=0.9,
        notes="Generated explanation for reduced claims.",
    )
    updated.action_log.append("Computed the reimbursable amount and wrote the derived values back into memory.")
    return updated


def memory_line_items(memory: WorkingMemory) -> list[str]:
    value = memory.facts.get("line_item_summary", MemoryFact()).value
    return [item.strip() for item in value.split("|") if item.strip()]
