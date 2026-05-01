from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ExpenseFields(BaseModel):
    vendor: str = ""
    transaction_date: str = ""
    total: str = ""
    subtotal: str = ""
    tax: str = ""
    currency: str = "USD"
    category: str = ""
    payment_method: str = ""
    notes: str = ""


class BlurCheckResult(BaseModel):
    verdict: Literal["clear", "slightly_blurry", "blurry"] = "clear"
    score: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""


class ToolDefinition(BaseModel):
    name: str
    kind: Literal["perception", "reasoning", "action", "guardrail"]
    execution: Literal["deterministic", "vlm", "hybrid"]
    purpose: str


class MemoryFact(BaseModel):
    value: str = ""
    source: Literal["receipt", "policy", "ui", "derived", "user", "agent"] = "receipt"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""


class PageRequirement(BaseModel):
    page_id: str
    title: str = ""
    required_fields: list[str] = Field(default_factory=list)
    discovered_fields: list[str] = Field(default_factory=list)
    status: Literal["pending", "observed", "filled", "submitted"] = "pending"


class ExtractionTemplate(BaseModel):
    core_fields: list[str] = Field(default_factory=list)
    company_specific_fields: dict[str, list[str]] = Field(default_factory=dict)
    discovered_fields: list[str] = Field(default_factory=list)
    active_fields: list[str] = Field(default_factory=list)


class WorkingMemory(BaseModel):
    company_slug: str = ""
    receipt_image_path: str = ""
    blur_check: BlurCheckResult | None = None
    facts: dict[str, MemoryFact] = Field(default_factory=dict)
    derived_values: dict[str, MemoryFact] = Field(default_factory=dict)
    page_requirements: list[PageRequirement] = Field(default_factory=list)
    visited_pages: list[str] = Field(default_factory=list)
    action_log: list[str] = Field(default_factory=list)


class ExtractionPayload(BaseModel):
    fields: ExpenseFields
    reasoning_summary: str = ""
    raw_text: str = ""
    source: Literal["huggingface_qwen3_vl"] = "huggingface_qwen3_vl"
    document_label: Literal["receipt", "not_receipt", "unclear"] = "unclear"
    receipt_visibility: Literal["full", "partial", "unclear"] = "unclear"
    image_quality: Literal["clear", "unclear", "poor"] = "unclear"
    critical_elements_visible: bool = False
    missing_critical_elements: list[str] = Field(default_factory=list)
    retake_required: bool = False
    retake_reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_amounts: dict[str, str] = Field(default_factory=dict)
    line_item_summary: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)


class PortalState(BaseModel):
    company_slug: str = ""
    current_page: str = ""
    open_portal_url: str = ""
    vendor: str = ""
    transaction_date: str = ""
    total: str = ""
    subtotal: str = ""
    tax: str = ""
    currency: str = "USD"
    category: str = ""
    payment_method: str = ""
    claim_amount: str = ""
    adjustment_note: str = ""
    notes: str = ""
    submitted: bool = False
    submission_id: str = ""
    submission_timestamp: str = ""
    last_agent_action: str = ""


class SessionEvent(BaseModel):
    created_at: datetime
    kind: Literal["info", "success", "warning", "error", "action"] = "info"
    message: str


class PolicyIssue(BaseModel):
    label: str
    evidence: str = ""
    severity: Literal["low", "medium", "high"] = "medium"


class PolicyReview(BaseModel):
    risk_level: Literal["low", "medium", "high"] = "low"
    recommended_action: Literal["submit", "ask_user", "hold"] = "submit"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    receipt_visibility: Literal["full", "partial", "unclear"] = "unclear"
    policy_summary: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    issues: list[PolicyIssue] = Field(default_factory=list)


class SessionSnapshot(BaseModel):
    session_id: str
    company_slug: str = ""
    status: str
    current_step: str = ""
    created_at: datetime
    updated_at: datetime
    receipt_image_path: str = ""
    extraction: ExtractionPayload | None = None
    policy_review: PolicyReview | None = None
    extraction_template: ExtractionTemplate = Field(default_factory=ExtractionTemplate)
    working_memory: WorkingMemory = Field(default_factory=WorkingMemory)
    reviewed_fields: ExpenseFields = Field(default_factory=ExpenseFields)
    portal_state: PortalState = Field(default_factory=PortalState)
    events: list[SessionEvent] = Field(default_factory=list)
    error_text: str = ""
