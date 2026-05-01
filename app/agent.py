from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path

from .agent_memory import (
    apply_reimbursement_to_memory,
    build_extraction_template,
    build_working_memory,
    compute_reimbursement,
    ensure_page_requirement,
    mark_page_filled,
    mark_page_observed,
    mark_run_complete,
    merge_reviewed_fields,
)
from .agent_runtime.company_targets import get_agent_company_target
from .config import Settings
from .policy import PolicyReviewService
from .portal_automation import PortalAutomationRunner
from .schemas import ExpenseFields, ExtractionPayload, MemoryFact, PolicyReview, PortalState, WorkingMemory
from .store import SessionStore
from .tools import CurrencyConverter
from .vision import normalize_amount, normalize_expense_category


class ExpenseAutomationAgent:
    def __init__(self, settings: Settings, store: SessionStore, policy_service: PolicyReviewService):
        self.settings = settings
        self.store = store
        self.policy_service = policy_service
        self.currency_converter = CurrencyConverter(settings.currency_rates_path)
        self.portal_runners: dict[str, PortalAutomationRunner] = {}

    def _portal_runner_for_session(self, session_id: str) -> PortalAutomationRunner:
        runner = self.portal_runners.get(session_id)
        if runner is None:
            runner = PortalAutomationRunner(settings=self.settings)
            self.portal_runners[session_id] = runner
        return runner

    async def close_session_runner(self, session_id: str) -> None:
        runner = self.portal_runners.pop(session_id, None)
        if runner is not None:
            await runner.close_session(session_id)

    async def close_all_runners(self) -> None:
        runners = list(self.portal_runners.values())
        self.portal_runners.clear()
        for runner in runners:
            await runner.close_all()

    async def run(self, session_id: str, *, base_url: str | None = None) -> None:
        delay_seconds = self.settings.demo_fill_delay_ms / 1000
        portal_runner = self._portal_runner_for_session(session_id)
        try:
            session = self.store.get_session(session_id)
            if not session.company_slug:
                raise ValueError("No company was selected for this session.")
            if session.extraction is None or not session.receipt_image_path:
                raise ValueError("The agent needs a captured receipt before it can run.")

            company = get_agent_company_target(session.company_slug)
            receipt_path = Path(session.receipt_image_path)
            reviewed_fields = self._normalize_fields(session.reviewed_fields, extraction=session.extraction)

            self.store.append_event(
                session_id,
                (
                    "Step 2 · Live browser automation started\n"
                    f"Target portal: {company.name}. The agent will validate policy, compute the claim, inspect the live UI, "
                    "discover the visible workflow step by step, map semantic fields to visible controls, and decide whether submit is allowed."
                ),
                kind="action",
            )
            self.store.append_event(
                session_id,
                (
                    "Automation mode · browser control\n"
                    "Portal automation will send Qwen the end goal, the current screenshots, and the visible controls, then execute the next actions Qwen chooses through Playwright. "
                    "If Qwen cannot return a usable plan, the run will stop instead of guessing."
                ),
                kind="info",
            )

            extraction_template = session.extraction_template
            if not extraction_template.active_fields:
                extraction_template, discovered = build_extraction_template(company.slug)
                self.store.save_extraction_template(session_id, extraction_template)
                self.store.append_event(
                    session_id,
                    f"Observe: built the first extraction template for {company.name} with {len(discovered)} portal-relevant fields.",
                    kind="action",
                )

            memory = session.working_memory
            if not memory.facts:
                memory = build_working_memory(
                    company_slug=company.slug,
                    receipt_image_path=receipt_path,
                    extraction=session.extraction,
                    extraction_template=extraction_template,
                )
                self.store.save_working_memory(session_id, memory)

            self.store.update_status(session_id, status="agent_running", current_step="observe_template")
            self.store.append_event(
                session_id,
                (
                    "Tool · agent state bootstrap\n"
                    "Loaded the extraction template, working memory, and reviewed receipt fields into the live agent loop."
                ),
            )
            await asyncio.sleep(delay_seconds)

            memory = merge_reviewed_fields(memory, reviewed_fields)
            self.store.save_review(session_id, reviewed_fields)
            self.store.save_working_memory(session_id, memory)
            self.store.update_status(session_id, status="agent_running", current_step="merge_memory")
            self.store.append_event(
                session_id,
                (
                    "Tool · merge_reviewed_fields\n"
                    f"Working memory now carries {len(memory.facts)} facts, {len(memory.derived_values)} derived values, "
                    "and the reviewed consultant edits before the browser opens."
                ),
                kind="action",
            )
            await asyncio.sleep(delay_seconds)

            portal_url = self._resolve_portal_url(company.portal_path, base_url)
            await portal_runner.open_portal(session_id, portal_url)
            self.store.append_event(
                session_id,
                (
                    "Tool · open_company_portal\n"
                    f"Opened the live {company.name} portal in {portal_runner.browser_app_name or 'a controlled browser'} "
                    "so the agent can inspect website policy before computing the claim."
                ),
                kind="action",
            )
            await asyncio.sleep(delay_seconds)

            live_policy = await portal_runner.discover_live_policy(session_id, company.slug)
            live_policy_text = str(live_policy.get("text", "") or "")
            live_policy_found = bool(live_policy.get("found") and live_policy_text)
            if live_policy_found:
                preview = live_policy_text[:700]
                self.store.append_event(
                    session_id,
                    (
                        "Tool · read_portal_policy\n"
                        f"Source: {live_policy.get('source', 'live portal')}. Discovered {len(live_policy_text)} characters of live policy text.\n"
                        f"Policy preview: {preview}"
                    ),
                    kind="action",
                )
                policy_review = await self.policy_service.review_receipt(
                    receipt_path,
                    session.extraction,
                    reviewed_fields,
                )
            else:
                self.store.append_event(
                    session_id,
                    (
                        "Tool · read_portal_policy\n"
                        "No live policy text was discovered on the portal. Demo fallback: claim the full receipt amount and continue with required-field validation."
                    ),
                    kind="warning",
                )
                policy_review = PolicyReview(
                    risk_level="low",
                    recommended_action="submit",
                    confidence=0.75,
                    receipt_visibility=session.extraction.receipt_visibility,
                    policy_summary=(
                        "No live portal policy was discovered, so the demo fallback treats the receipt as fully reimbursable."
                    ),
                    warnings=["No live policy discovered; demo fallback claims the full receipt amount."],
                )
            self.store.set_policy_review(session_id, policy_review)
            self.store.update_status(session_id, status="agent_running", current_step="policy_review")
            self.store.append_event(
                session_id,
                (
                    "Tool · review_receipt_policy\n"
                    f"Engine: {self._policy_engine_label(policy_review)}. Result: {policy_review.recommended_action}. "
                    f"Risk: {policy_review.risk_level}. Confidence: {policy_review.confidence:.2f}.\n"
                    f"Summary: {policy_review.policy_summary}"
                ),
                kind="warning" if policy_review.recommended_action != "submit" else "success",
            )
            if self.settings.hf_api_token and not self._is_demo_policy_fallback(policy_review):
                self.store.append_event(
                    session_id,
                    (
                        "VLM response · policy reasoning\n"
                        f"{policy_review.policy_summary}\n"
                        f"Recommended action: {policy_review.recommended_action}. Risk: {policy_review.risk_level}."
                    ),
                    kind="action",
                )
            if policy_review.issues:
                self.store.append_event(
                    session_id,
                    "Decision · policy issues\n" + self._policy_issue_summary(policy_review),
                    kind="warning" if policy_review.recommended_action != "submit" else "action",
                )
            if policy_review.warnings:
                self.store.append_event(
                    session_id,
                    "Decision · policy cautions\n" + "\n".join(f"- {warning}" for warning in policy_review.warnings[:4]),
                    kind="warning",
                )
            await asyncio.sleep(delay_seconds)

            original_currency = memory.facts.get("currency_code")
            reimbursement = compute_reimbursement(
                company.slug,
                memory,
                self.currency_converter,
                live_policy_text=live_policy_text if live_policy_found else "",
            )
            memory = apply_reimbursement_to_memory(
                memory,
                reimbursement,
                original_currency=original_currency.value if original_currency else "USD",
            )
            self.store.save_working_memory(session_id, memory)
            self.store.update_status(session_id, status="agent_running", current_step="compute_claim")
            self.store.append_event(
                session_id,
                (
                    "Tool · compute_reimbursable_amount\n"
                    f"Computed a claim of {reimbursement.claim_amount_local} {original_currency.value if original_currency else 'USD'} "
                    "from the live website policy result and stored the derived values back into working memory."
                ),
                kind="action",
            )
            if original_currency and original_currency.value and original_currency.value.upper() != "USD":
                self.store.append_event(
                    session_id,
                    (
                        "Tool · convert_currency\n"
                        f"Mapped the claim to about {reimbursement.claim_amount_usd} USD using the local FX table."
                    ),
                    kind="action",
            )
            await asyncio.sleep(delay_seconds)

            portal = self._build_portal_state(company.slug, company.portal_path, memory)
            portal.last_agent_action = "Act: opened the live company portal and read policy context before filling."
            self.store.update_portal_state(session_id, portal)

            visited_signatures: set[tuple[str, tuple[str, ...]]] = set()
            submission: dict[str, str] | None = None
            for step_index in range(20):
                plan = await portal_runner.plan_next_actions(
                    session_id=session_id,
                    portal_state=portal,
                    allow_submit=policy_review.recommended_action == "submit",
                )
                page_id = str(plan.get("page_id", "current_portal_step"))
                semantic_fields = tuple(str(field) for field in plan.get("semantic_fields", ()))
                plan_trace = plan["trace"]
                planned_actions = tuple(str(item) for item in plan_trace.get("matched_fields", []))
                signature = (page_id, planned_actions)
                if signature in visited_signatures and plan.get("status") != "done":
                    raise RuntimeError(
                        f"Qwen repeated the same plan on {page_id}, so the workflow did not advance safely."
                    )
                visited_signatures.add(signature)

                memory = ensure_page_requirement(
                    memory,
                    page_id,
                    list(semantic_fields),
                    title=page_id.replace("_", " ").title(),
                )
                memory = mark_page_observed(memory, page_id)
                self.store.save_working_memory(session_id, memory)
                self.store.update_status(session_id, status="agent_running", current_step=f"plan_{page_id}")
                portal.current_page = page_id
                self.store.update_portal_state(session_id, portal)
                self.store.append_event(
                    session_id,
                    self._page_observation_message(page_id, semantic_fields, plan_trace),
                    kind="action",
                )
                if plan_trace.get("vlm_summary") or plan_trace.get("vlm_reasoning"):
                    self.store.append_event(
                        session_id,
                        self._page_vlm_message(page_id, plan_trace),
                        kind="action",
                    )
                await asyncio.sleep(delay_seconds / 2)

                self.store.update_status(session_id, status="agent_running", current_step=f"act_{page_id}")
                trace = await portal_runner.execute_action_plan(
                    session_id=session_id,
                    plan=plan,
                    receipt_path=receipt_path,
                )
                memory = mark_page_filled(memory, page_id)
                self.store.save_working_memory(session_id, memory)
                portal.last_agent_action = self._page_fill_message(page_id, semantic_fields, memory, trace)
                self.store.update_portal_state(session_id, portal)
                self.store.append_event(
                    session_id,
                    self._page_action_message(page_id, trace, portal.last_agent_action),
                    kind="action",
                )
                await asyncio.sleep(delay_seconds)

                if trace.get("submission"):
                    submission = trace["submission"]
                    break
                if plan.get("status") == "done":
                    break
                if not trace.get("executed_actions"):
                    raise RuntimeError(
                        f"Qwen did not provide any executable next action on {page_id}."
                    )
            else:
                raise RuntimeError("The portal workflow exceeded the maximum number of Qwen-guided steps.")

            if submission is not None:
                confirmation = submission.get("submission_id") or self._generate_confirmation(session_id)
                memory = mark_run_complete(memory)
                self.store.save_working_memory(session_id, memory)
                portal.submitted = True
                portal.submission_id = confirmation
                portal.submission_timestamp = submission.get("submitted_at") or datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
                portal.last_agent_action = "Act: submitted the live reimbursement form."
                self.store.update_portal_state(session_id, portal)
                self.store.update_status(session_id, status="completed", current_step="remember")
                self.store.append_event(
                    session_id,
                    (
                        "Decision · submit\n"
                        f"Stored the completed memory snapshot and the live submission {confirmation}."
                    ),
                    kind="success",
                )
                return

            portal.last_agent_action = (
                "Act: filled the live portal but stopped before submission because the policy reviewer asked for human review."
            )
            self.store.update_portal_state(session_id, portal)
            if policy_review.recommended_action == "ask_user":
                self.store.update_status(session_id, status="awaiting_confirmation", current_step="await_user")
                self.store.append_event(
                    session_id,
                    (
                        "Decision · human confirmation required\n"
                        "The agent filled the known fields but stopped before submit because policy confidence was not high enough."
                    ),
                    kind="warning",
                )
                return

            self.store.update_status(session_id, status="needs_review", current_step="hold_for_review")
            self.store.append_event(
                session_id,
                (
                    "Decision · hold for review\n"
                    "The agent held the run for manual review because the evidence looked incomplete or risky."
                ),
                kind="warning",
            )
        except Exception as exc:
            self.store.update_status(session_id, status="error", current_step="agent_failed", error_text=str(exc))
            self.store.append_event(session_id, f"Agent run failed: {exc}", kind="error")
            raise

    def _normalize_fields(
        self,
        fields: ExpenseFields,
        *,
        extraction: ExtractionPayload | None = None,
    ) -> ExpenseFields:
        normalized = fields.model_copy()
        normalized.total = normalize_amount(normalized.total)
        normalized.subtotal = normalize_amount(normalized.subtotal)
        normalized.tax = normalize_amount(normalized.tax)
        if not normalized.tax and normalized.total and normalized.subtotal:
            normalized.tax = f"{max(float(normalized.total) - float(normalized.subtotal), 0.0):.2f}"
        normalized.currency = (normalized.currency or self.settings.demo_currency).upper()
        normalized.category = self._resolve_content_category(normalized, extraction)
        normalized.payment_method = normalized.payment_method or "Corporate Card"
        if not normalized.notes:
            normalized.notes = "Captured locally and reviewed by the receipt agent."
        return normalized

    def _resolve_content_category(
        self,
        fields: ExpenseFields,
        extraction: ExtractionPayload | None,
    ) -> str:
        line_items = " ".join(extraction.line_item_summary or []).lower() if extraction else ""
        semantic_amounts = extraction.semantic_amounts if extraction else {}
        notes = (fields.notes or "").lower()
        content = f"{line_items}\n{notes}"
        normalized_existing = normalize_expense_category(fields.category)
        if normalized_existing and normalized_existing != "Other":
            return normalized_existing

        if semantic_amounts.get("alcohol_amount"):
            return "Meals"
        if semantic_amounts.get("fare_amount") or semantic_amounts.get("mandatory_fee_amount"):
            return "Travel"
        return "Other"

    def _build_portal_state(self, company_slug: str, portal_path: str, memory: WorkingMemory) -> PortalState:
        facts = memory.facts
        derived = memory.derived_values
        return PortalState(
            company_slug=company_slug,
            current_page=memory.page_requirements[0].page_id if memory.page_requirements else "",
            open_portal_url=portal_path,
            vendor=facts.get("vendor_name", self._empty_fact()).value,
            transaction_date=facts.get("expense_date", self._empty_fact()).value,
            total=facts.get("receipt_total", self._empty_fact()).value,
            subtotal=facts.get("subtotal_amount", self._empty_fact()).value,
            tax=facts.get("tax_amount", self._empty_fact()).value,
            currency=facts.get("currency_code", self._empty_fact(default="USD")).value or "USD",
            category=facts.get("expense_category", self._empty_fact()).value,
            payment_method=facts.get("payment_method", self._empty_fact(default="Corporate Card")).value,
            claim_amount=derived.get("claim_amount_local", self._empty_fact()).value,
            adjustment_note=derived.get("adjustment_note", self._empty_fact()).value,
            notes=facts.get("business_purpose", self._empty_fact()).value,
            last_agent_action="Agent state initialized from working memory.",
        )

    @staticmethod
    def _resolve_portal_url(portal_path: str, base_url: str | None) -> str:
        if base_url:
            return f"{base_url.rstrip('/')}{portal_path}"
        return portal_path

    def _page_fill_message(
        self,
        page_id: str,
        semantic_fields: tuple[str, ...],
        memory: WorkingMemory,
        trace: dict[str, object] | None = None,
    ) -> str:
        known = []
        for field in semantic_fields:
            if field in memory.derived_values and memory.derived_values[field].value:
                known.append(field)
            elif field in memory.facts and memory.facts[field].value:
                known.append(field)
        uploads = bool(trace and trace.get("uploaded_receipt"))
        if known:
            suffix = " and uploaded the receipt packet." if uploads else "."
            return f"Act: matched memory fields {', '.join(known)} to the {page_id} step{suffix}"
        return f"Act: observed the {page_id} step, but some required values are still missing from memory."

    def _policy_engine_label(self, policy_review: PolicyReview) -> str:
        if self._is_demo_policy_fallback(policy_review):
            return "Demo full-reimbursement fallback"
        if self.settings.hf_api_token:
            return f"Hugging Face VLM ({self.settings.hf_model})"
        return "Qwen3-VL unavailable"

    @staticmethod
    def _is_demo_policy_fallback(policy_review: PolicyReview) -> bool:
        return any("no live policy discovered" in warning.lower() for warning in policy_review.warnings)

    @staticmethod
    def _policy_issue_summary(policy_review: PolicyReview) -> str:
        lines = []
        for issue in policy_review.issues[:4]:
            evidence = f": {issue.evidence}" if issue.evidence else ""
            lines.append(f"- {issue.label} ({issue.severity}){evidence}")
        if policy_review.missing_fields:
            lines.append(f"- Missing fields: {', '.join(policy_review.missing_fields)}")
        return "\n".join(lines) or "No policy issues were raised."

    @staticmethod
    def _page_observation_message(
        page_id: str,
        semantic_fields: tuple[str, ...],
        trace: dict[str, object],
    ) -> str:
        controls_seen = trace.get("controls_seen", 0)
        buttons_seen = trace.get("buttons_seen", 0)
        matched_fields = trace.get("matched_fields", [])
        preview = trace.get("control_preview", [])
        lines = [
            "Tool · inspect_form_ui",
            f"Page: {page_id}. Visible controls: {controls_seen}. Action buttons: {buttons_seen}.",
            f"Inspection mode: {ExpenseAutomationAgent._inspection_mode_label(str(trace.get('inspection_mode', 'dom_semantic_matching')))}.",
            f"Semantic targets for this step: {', '.join(semantic_fields)}.",
        ]
        screenshot_count = int(trace.get("screenshot_count", 0) or 0)
        if screenshot_count > 1:
            lines.append(f"Vision input: Qwen reviewed {screenshot_count} scrolled screenshots of the page.")
        elif screenshot_count == 1:
            lines.append("Vision input: Qwen reviewed a single page screenshot.")
        if matched_fields:
            lines.append(f"Matches: {', '.join(str(item) for item in matched_fields[:6])}.")
        elif preview:
            lines.append(f"Visible control preview: {', '.join(str(item) for item in preview[:4])}.")
        if trace.get("vlm_error"):
            lines.append(f"Vision planner error: {trace['vlm_error']}.")
        return "\n".join(lines)

    @staticmethod
    def _page_action_message(
        page_id: str,
        trace: dict[str, object],
        fallback_message: str,
    ) -> str:
        filled_fields = [str(item) for item in trace.get("filled_fields", [])]
        selected_options = [str(item) for item in trace.get("selected_options", [])]
        checked_boxes = [str(item) for item in trace.get("checked_boxes", [])]
        clicked_buttons = [str(item) for item in trace.get("clicked_buttons", [])]
        executed_actions = [str(item) for item in trace.get("executed_actions", [])]
        uploaded_receipt = bool(trace.get("uploaded_receipt"))

        lines = ["Tool · browser_action", f"Page: {page_id}."]
        if filled_fields:
            lines.append(f"Filled semantic fields: {', '.join(filled_fields)}.")
        if selected_options:
            lines.append(f"Dropdown selections: {', '.join(selected_options)}.")
        if checked_boxes:
            lines.append(f"Checked boxes: {', '.join(checked_boxes)}.")
        if uploaded_receipt:
            lines.append("Uploaded the receipt file to the portal.")
        if clicked_buttons:
            lines.append(f"Clicked buttons: {', '.join(clicked_buttons)}.")
        if executed_actions:
            lines.append(f"Executed Qwen actions: {', '.join(executed_actions[:8])}.")
        if len(lines) == 2:
            lines.append(fallback_message)
        return "\n".join(lines)

    @staticmethod
    def _page_vlm_message(page_id: str, trace: dict[str, object]) -> str:
        lines = [
            "VLM response · portal inspection",
            f"Page: {page_id}.",
        ]
        screenshot_count = int(trace.get("screenshot_count", 0) or 0)
        if screenshot_count:
            lines.append(f"Screenshots reviewed: {screenshot_count}.")
        if trace.get("vlm_summary"):
            lines.append(f"Summary: {trace['vlm_summary']}")
        if trace.get("vlm_reasoning"):
            lines.append(f"Reasoning: {trace['vlm_reasoning']}")
        field_matches = [str(item) for item in trace.get("vlm_field_matches", [])]
        button_matches = [str(item) for item in trace.get("vlm_button_matches", [])]
        if field_matches:
            lines.append(f"Field picks: {', '.join(field_matches[:6])}.")
        if button_matches:
            lines.append(f"Button picks: {', '.join(button_matches[:4])}.")
        return "\n".join(lines)

    @staticmethod
    def _inspection_mode_label(mode: str) -> str:
        return {
            "qwen_screenshot_guided": "Qwen screenshot guidance with Playwright execution",
            "qwen_goal_directed": "Qwen goal-directed next-action planning",
        }.get(mode, mode.replace("_", " "))

    def _build_currency_note(self, fields: ExpenseFields) -> str:
        currency = (fields.currency or "USD").upper()
        if currency == "USD" or not fields.total:
            return ""

        converted = self.currency_converter.convert_to_usd(fields.total, currency)
        if not converted:
            return ""

        return (
            f"Tool: currency converter estimated about {converted} USD from {fields.total} {currency} "
            "using the local JSON rate table."
        )

    def _generate_confirmation(self, session_id: str) -> str:
        suffix = random.randint(1000, 9999)
        return f"EXP-{datetime.now().strftime('%Y%m%d')}-{session_id[:4].upper()}-{suffix}"

    @staticmethod
    def _empty_fact(*, default: str = "") -> MemoryFact:
        return MemoryFact(value=default, source="agent", confidence=0.0)
