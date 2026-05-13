from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    async_playwright,
)

from .config import Settings, get_settings
from .schemas import PortalState
from .vision import extract_json_object, normalize_expense_category, supports_structured_outputs


SUBMIT_BUTTON_LABELS: tuple[str, ...] = (
    "submit expense report",
    "submit workflow packet",
    "提交报销申请",
    "submit",
)

def normalize_ui_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


@dataclass(frozen=True)
class FormControl:
    tag: str
    input_type: str
    name: str
    element_id: str
    label: str
    placeholder: str
    options: tuple[str, ...]
    text: str

    @property
    def haystack(self) -> str:
        parts = [self.label, self.name, self.element_id, self.placeholder, self.text, *self.options]
        return normalize_ui_text(" ".join(part for part in parts if part))


def describe_control(control: FormControl) -> str:
    primary = ""
    if control.label:
        primary = control.label.split("|")[0].strip()
    primary = primary or control.text or control.placeholder or control.name or control.element_id or control.tag
    if control.name and control.name not in primary:
        return f"{primary} ({control.name})"
    return primary


def control_catalog_entry(control: FormControl, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "tag": control.tag,
        "input_type": control.input_type,
        "name": control.name,
        "element_id": control.element_id,
        "label": control.label,
        "placeholder": control.placeholder,
        "text": control.text,
        "options": list(control.options),
    }


def resolve_catalog_control(catalog: list[FormControl], index_value: object) -> FormControl | None:
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(catalog):
        return catalog[index]
    return None


def scroll_capture_positions(scroll_height: int, viewport_height: int, max_shots: int = 3) -> list[int]:
    if max_shots <= 1 or viewport_height <= 0:
        return [0]

    bottom = max(scroll_height - viewport_height, 0)
    if bottom <= 32:
        return [0]

    positions = [0]
    for step in range(1, max_shots):
        positions.append(int(round((bottom * step) / max(max_shots - 1, 1))))

    ordered: list[int] = []
    seen: set[int] = set()
    for position in positions:
        if position in seen:
            continue
        ordered.append(position)
        seen.add(position)
    return ordered


def looks_like_submit_button(control: FormControl) -> bool:
    haystack = control.haystack
    return any(normalize_ui_text(label) in haystack for label in SUBMIT_BUTTON_LABELS)


def looks_like_back_button(control: FormControl) -> bool:
    haystack = control.haystack
    return "back" in haystack or "返回" in haystack


def supports_fill(control: FormControl) -> bool:
    if control.tag == "textarea":
        return True
    if control.tag != "input":
        return False
    return control.input_type not in {
        "checkbox",
        "file",
        "hidden",
        "radio",
        "submit",
        "button",
        "reset",
    }


class PortalAutomationRunner:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.browser_app_name: str | None = None
        self.contexts: dict[str, BrowserContext] = {}
        self.pages: dict[str, Page] = {}

    def uses_vlm_page_inspection(self) -> bool:
        return bool(self.settings.hf_api_token)

    def require_vlm_page_inspection(self) -> None:
        if not self.uses_vlm_page_inspection():
            raise RuntimeError(
                "Qwen page inspection is required for portal automation, but no Hugging Face token is configured."
            )

    def browser_launch_plan(self) -> list[tuple[dict[str, Any], str]]:
        if self.settings.browser_headless:
            return [({"headless": True}, "Headless Chromium")]
        return [
            ({"channel": "chrome", "headless": False}, "Google Chrome"),
            ({"headless": False}, "Google Chrome for Testing"),
        ]

    async def open_portal(self, session_id: str, portal_url: str) -> Page:
        if session_id in self.pages:
            page = self.pages[session_id]
            await page.goto(portal_url, wait_until="domcontentloaded")
            await page.bring_to_front()
            await self._activate_browser_app()
            return page

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if self.browser is None:
            last_error: PlaywrightError | None = None
            for launch_options, app_name in self.browser_launch_plan():
                try:
                    self.browser = await self.playwright.chromium.launch(**launch_options)
                    self.browser_app_name = app_name
                    break
                except PlaywrightError as exc:
                    last_error = exc
            if self.browser is None:
                raise RuntimeError(str(last_error) if last_error else "Could not launch a browser for portal automation.")

        context = await self.browser.new_context()
        page = await context.new_page()
        await page.goto(portal_url, wait_until="domcontentloaded")
        await page.bring_to_front()
        await self._activate_browser_app()
        self.contexts[session_id] = context
        self.pages[session_id] = page
        return page

    async def discover_live_policy(self, session_id: str, company_slug: str) -> dict[str, Any]:
        page = self.pages[session_id]
        if company_slug == "soberstack":
            return await self._discover_soberstack_policy(page)
        if company_slug == "stingy":
            return await self._discover_visible_policy(
                page,
                ".workflow-panel[data-step-panel='1']",
                source="visible policy gate",
            )
        if company_slug == "china":
            return await self._discover_visible_policy(
                page,
                ".china-policy-card",
                source="visible policy side rail",
            )
        return await self._discover_generic_policy(page)

    async def _discover_soberstack_policy(self, page: Page) -> dict[str, Any]:
        button = page.locator("#open-soberstack-policy").first
        if await button.count() == 0:
            return {"found": False, "source": "soberstack policy dialog", "text": ""}
        await button.click()
        dialog = page.locator("#soberstack-policy-dialog").first
        try:
            await dialog.wait_for(state="visible", timeout=3_000)
            text = normalize_ui_text(await dialog.inner_text())
        finally:
            close = page.locator("#close-soberstack-policy").first
            if await close.count() > 0:
                await close.click()
        return {
            "found": bool(text),
            "source": "soberstack policy dialog",
            "text": text,
        }

    async def _discover_visible_policy(self, page: Page, selector: str, *, source: str) -> dict[str, Any]:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return {"found": False, "source": source, "text": ""}
        text = normalize_ui_text(await locator.inner_text())
        return {
            "found": bool(text),
            "source": source,
            "text": text,
        }

    async def _discover_generic_policy(self, page: Page) -> dict[str, Any]:
        text = await page.evaluate(
            """
            () => {
              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const candidates = [...document.querySelectorAll("dialog, aside, section, [class*='policy']")];
              for (const candidate of candidates) {
                const text = clean(candidate.innerText || candidate.textContent || "");
                if (/policy|报销政策/i.test(text)) return text;
              }
              return "";
            }
            """
        )
        return {
            "found": bool(str(text or "").strip()),
            "source": "generic visible policy scan",
            "text": normalize_ui_text(str(text or "")),
        }

    async def inspect_visible_controls(self, page: Page) -> tuple[list[FormControl], list[FormControl]]:
        raw_controls = await page.evaluate(
            """
            () => {
              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                if (!style) return false;
                if (style.display === "none" || style.visibility === "hidden") return false;
                return element.getClientRects().length > 0;
              };
              const labelText = (element) => {
                const values = [];
                if (element.id) {
                  document
                    .querySelectorAll(`label[for="${CSS.escape(element.id)}"]`)
                    .forEach((label) => values.push(clean(label.innerText)));
                }
                const wrapper = element.closest("label");
                if (wrapper) values.push(clean(wrapper.innerText));
                values.push(clean(element.getAttribute("aria-label")));
                return [...new Set(values.filter(Boolean))].join(" | ");
              };

              return [...document.querySelectorAll("input, select, textarea, button")]
                .filter((element) => isVisible(element))
                .map((element) => ({
                  tag: element.tagName.toLowerCase(),
                  input_type: (element.getAttribute("type") || "").toLowerCase(),
                  name: element.getAttribute("name") || "",
                  element_id: element.id || "",
                  label: labelText(element),
                  placeholder: element.getAttribute("placeholder") || "",
                  options: element.tagName.toLowerCase() === "select"
                    ? [...element.options].map((option) => clean(option.textContent))
                    : [],
                  text: clean(element.innerText || element.textContent || element.value || ""),
                }));
            }
            """
        )
        controls = [
            FormControl(
                tag=item["tag"],
                input_type=item["input_type"],
                name=item["name"],
                element_id=item["element_id"],
                label=item["label"],
                placeholder=item["placeholder"],
                options=tuple(item["options"]),
                text=item["text"],
            )
            for item in raw_controls
        ]
        actionable_controls = [
            control
            for control in controls
            if control.tag != "button" and control.input_type not in {"hidden", "submit", "button", "reset"}
        ]
        buttons = [control for control in controls if control.tag == "button" or control.input_type == "submit"]
        return actionable_controls, buttons

    def build_goal_values(self, portal_state: PortalState) -> dict[str, str]:
        values = {
            "vendor": portal_state.vendor,
            "expense_date": portal_state.transaction_date,
            "receipt_total": portal_state.total,
            "subtotal": portal_state.subtotal,
            "tax": portal_state.tax,
            "currency": portal_state.currency,
            "expense_category": normalize_expense_category(portal_state.category) or portal_state.category or "Other",
            "claim_amount": portal_state.claim_amount or portal_state.total,
            "business_purpose": portal_state.notes or "Client-facing business expense.",
            "adjustment_note": portal_state.adjustment_note,
            "employee_name": "Casey Consultant",
            "cost_center": "Client Services",
        }
        return {key: value for key, value in values.items() if str(value or "").strip()}

    async def plan_next_actions(
        self,
        *,
        session_id: str,
        portal_state: PortalState,
        allow_submit: bool,
    ) -> dict[str, Any]:
        self.require_vlm_page_inspection()
        page = self.pages[session_id]
        controls, buttons = await self.inspect_visible_controls(page)
        screenshot_data_urls = await self._capture_scroll_screenshots(page)
        current_url = page.url
        page_title = await page.title()
        goal_values = self.build_goal_values(portal_state)
        control_catalog = [control_catalog_entry(control, index) for index, control in enumerate(controls)]
        button_catalog = [control_catalog_entry(button, index) for index, button in enumerate(buttons)]

        prompt = (
            "You are the browser control brain for a reimbursement agent.\n"
            "Look at the screenshots of the current page, the visible control catalogs, and the end goal.\n"
            "Decide the next ordered actions for the CURRENT page only. Do not assume a fixed workflow.\n"
            "Use the screenshots first, and use the catalogs only to point at exact targets.\n"
            "End goal: successfully submit this reimbursement request using the visible portal UI.\n"
            f"Submission allowed right now: {'yes' if allow_submit else 'no'}.\n"
            "Return JSON only with this schema:\n"
            "{\n"
            '  "page_id": "",\n'
            '  "page_summary": "",\n'
            '  "reasoning": "",\n'
            '  "status": "continue|done",\n'
            '  "actions": [\n'
            '    {"action": "fill|select|upload|check|click|submit|done", "target_type": "control|button", "target_index": -1, "value": "", "semantic_field": "", "confidence": 0.0, "reasoning": ""}\n'
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- For fill/select/upload/check, target_type must be control.\n"
            "- For click/submit, target_type must be button.\n"
            "- When several visible fields can be filled safely on the current page, include all of them in one response instead of returning only one field.\n"
            "- Put navigation clicks last because the page may change after them.\n"
            "- If a button will finalize the reimbursement request, use action=submit, not click.\n"
            "- Only use values from the goal values below. Do not invent values.\n"
            "- If upload is needed, use action=upload and the runner will attach the receipt file.\n"
            "- If submission is not allowed, never emit action=submit.\n"
            "- On a review page, if a submit button is visible, submission is allowed, and no editable text/select/file fields remain, prefer submit instead of going back.\n"
            "- If the page already shows success or thank-you, return status=done with no actions.\n"
            "- Keep page_summary under 80 characters and reasoning under 120 characters.\n"
            "- Keep each action reasoning under 80 characters, or use an empty string.\n"
            "- Return one complete compact JSON object. Do not include markdown, prose, or extra keys.\n"
            f"Current URL: {current_url}\n"
            f"Page title: {page_title}\n"
            f"Screenshot count: {len(screenshot_data_urls)}\n"
            f"Goal values: {json.dumps(goal_values, ensure_ascii=False)}\n"
            "A receipt image file is available for upload if needed.\n"
            f"Control catalog: {json.dumps(control_catalog, ensure_ascii=False)}\n"
            f"Button catalog: {json.dumps(button_catalog, ensure_ascii=False)}\n"
        )

        parsed = await self._request_qwen_json(
            prompt=prompt,
            screenshot_data_urls=screenshot_data_urls,
            max_tokens=2200,
        )

        page_id = str(parsed.get("page_id", "")).strip() or "current_portal_step"
        status = str(parsed.get("status", "continue")).strip().lower()
        if status not in {"continue", "done"}:
            status = "continue"

        actions = [
            item
            for item in parsed.get("actions", [])
            if isinstance(item, dict) and str(item.get("action", "")).strip()
        ]
        validation_errors = self._validate_actions(
            actions=actions,
            controls=controls,
            buttons=buttons,
            allow_submit=allow_submit,
        )

        if (not actions and status != "done") or validation_errors:
            repaired = await self._repair_missing_actions(
                screenshot_data_urls=screenshot_data_urls,
                current_url=current_url,
                page_title=page_title,
                goal_values=goal_values,
                control_catalog=control_catalog,
                button_catalog=button_catalog,
                prior_reasoning=str(parsed.get("reasoning", "")).strip(),
                repair_reason="; ".join(validation_errors),
            )
            if repaired is not None:
                parsed["page_summary"] = repaired.get("page_summary", parsed.get("page_summary", ""))
                parsed["reasoning"] = repaired.get("reasoning", parsed.get("reasoning", ""))
                page_id = repaired.get("page_id", page_id) or page_id
                status = repaired.get("status", status) or status
                actions = repaired.get("actions", actions)

        if allow_submit and self._plan_is_ready_without_submit(status, actions):
            submit_index = self._first_submit_button_index(buttons)
            if submit_index is not None:
                status = "continue"
                actions = [
                    {
                        "action": "submit",
                        "target_type": "button",
                        "target_index": submit_index,
                        "value": "",
                        "semantic_field": "submit_reimbursement",
                        "confidence": 1.0,
                        "reasoning": "Qwen reported the page is ready, and a visible submit button is still available.",
                    }
                ]

        trace = {
            "controls_seen": len(controls),
            "buttons_seen": len(buttons),
            "control_preview": [describe_control(control) for control in controls[:5]],
            "button_preview": [describe_control(button) for button in buttons[:4]],
            "inspection_mode": "qwen_goal_directed",
            "screenshot_count": len(screenshot_data_urls),
            "vlm_summary": str(parsed.get("page_summary", "")).strip(),
            "vlm_reasoning": str(parsed.get("reasoning", "")).strip(),
            "vlm_error": "",
            "vlm_field_matches": [],
            "vlm_button_matches": [],
            "matched_fields": self._readable_goal_actions(actions, controls, buttons),
        }
        semantic_fields: list[str] = []
        for item in actions:
            semantic_field = str(item.get("semantic_field", "")).strip()
            if semantic_field and semantic_field not in semantic_fields:
                semantic_fields.append(semantic_field)

        return {
            "page_id": page_id,
            "status": status,
            "actions": actions,
            "semantic_fields": tuple(semantic_fields),
            "controls": controls,
            "buttons": buttons,
            "trace": trace,
        }

    def _validate_actions(
        self,
        *,
        actions: list[dict[str, Any]],
        controls: list[FormControl],
        buttons: list[FormControl],
        allow_submit: bool,
    ) -> list[str]:
        errors: list[str] = []
        has_submit_button = any(looks_like_submit_button(button) for button in buttons)
        has_editable_controls = any(
            supports_fill(control) or control.tag == "select" or control.input_type == "file" for control in controls
        )
        for action_index, action in enumerate(actions):
            action_kind = str(action.get("action", "")).strip().lower()
            target_type = str(action.get("target_type", "")).strip().lower()
            if action_kind in {"", "done"}:
                continue

            if target_type == "control":
                target = resolve_catalog_control(controls, action.get("target_index"))
            elif target_type == "button":
                target = resolve_catalog_control(buttons, action.get("target_index"))
            else:
                errors.append(f"Unknown target_type '{target_type or 'empty'}' for action '{action_kind}'.")
                continue

            if target is None:
                errors.append(f"Invalid {target_type} index for action '{action_kind}'.")
                continue

            label = describe_control(target)
            if action_kind == "fill" and not supports_fill(target):
                errors.append(f"Action 'fill' cannot target {label}.")
            elif action_kind == "select" and target.tag != "select":
                errors.append(f"Action 'select' must target a select control, not {label}.")
            elif action_kind == "upload" and target.input_type != "file":
                errors.append(f"Action 'upload' must target a file input, not {label}.")
            elif action_kind == "check" and target.input_type != "checkbox":
                errors.append(f"Action 'check' must target a checkbox, not {label}.")
            elif action_kind in {"click", "submit"} and target_type != "button":
                errors.append(f"Action '{action_kind}' must target a button, not {label}.")
            elif action_kind == "submit" and not allow_submit:
                errors.append("Submission is not allowed on this step.")
            elif (
                action_kind == "click"
                and allow_submit
                and has_submit_button
                and not has_editable_controls
                and looks_like_back_button(target)
            ):
                errors.append(
                    "Do not click a back button when the page is already in final review with a visible submit button."
                )
        return errors

    @staticmethod
    def _plan_is_ready_without_submit(status: str, actions: list[dict[str, Any]]) -> bool:
        if not actions:
            return status == "done"
        return all(str(action.get("action", "")).strip().lower() in {"", "done"} for action in actions)

    @staticmethod
    def _first_submit_button_index(buttons: list[FormControl]) -> int | None:
        for index, button in enumerate(buttons):
            if looks_like_submit_button(button):
                return index
        return None

    async def execute_action_plan(
        self,
        *,
        session_id: str,
        plan: dict[str, Any],
        receipt_path: Path,
    ) -> dict[str, Any]:
        page = self.pages[session_id]
        controls: list[FormControl] = list(plan.get("controls", []))
        buttons: list[FormControl] = list(plan.get("buttons", []))
        actions: list[dict[str, Any]] = list(plan.get("actions", []))

        trace: dict[str, Any] = {
            "page_id": plan.get("page_id", "current_portal_step"),
            "filled_fields": [],
            "selected_options": [],
            "checked_boxes": [],
            "clicked_buttons": [],
            "uploaded_receipt": False,
            "executed_actions": [],
            "submission": {},
        }

        for action_index, action in enumerate(actions):
            action_kind = str(action.get("action", "")).strip().lower()
            if action_kind in {"", "done"}:
                continue

            target_type = str(action.get("target_type", "")).strip().lower()
            if target_type == "control":
                target = resolve_catalog_control(controls, action.get("target_index"))
            elif target_type == "button":
                target = resolve_catalog_control(buttons, action.get("target_index"))
            else:
                raise RuntimeError(f"Qwen returned an unknown target type: {target_type or 'empty'}.")

            if target is None:
                raise RuntimeError(f"Qwen pointed at an invalid {target_type or 'target'} index.")

            value = str(action.get("value", "")).strip()
            semantic_field = str(action.get("semantic_field", "")).strip()
            label = describe_control(target)

            if action_kind == "fill":
                if not supports_fill(target):
                    raise RuntimeError(f"Qwen selected a non-fillable control for fill: {label}.")
                if not value:
                    raise RuntimeError(f"Qwen chose fill for {label}, but did not provide a value.")
                written_value = await self._fill_text(page, target, value)
                trace["filled_fields"].append(semantic_field or label)
                trace["executed_actions"].append(f"fill {label} = {written_value}")
            elif action_kind == "select":
                if not value:
                    raise RuntimeError(f"Qwen chose select for {label}, but did not provide an option label.")
                selected = await self._select_explicit_option(page, target, value)
                trace["selected_options"].append(f"{semantic_field or label} -> {selected}")
                trace["executed_actions"].append(f"select {label} = {selected}")
            elif action_kind == "upload":
                if target.input_type != "file":
                    raise RuntimeError(f"Qwen selected a non-file control for upload: {label}.")
                await self._set_file(page, target, receipt_path)
                trace["uploaded_receipt"] = True
                trace["executed_actions"].append(f"upload {label}")
                later_actions = actions[action_index + 1 :]
                should_replan_before_submit = False
                for later_action in later_actions:
                    later_kind = str(later_action.get("action", "")).strip().lower()
                    if later_kind == "submit":
                        should_replan_before_submit = True
                        break
                    if later_kind != "click":
                        continue
                    if str(later_action.get("target_type", "")).strip().lower() != "button":
                        continue
                    later_button = resolve_catalog_control(buttons, later_action.get("target_index"))
                    if later_button is not None and looks_like_submit_button(later_button):
                        should_replan_before_submit = True
                        break
                if should_replan_before_submit:
                    await asyncio.sleep(0.35)
                    break
            elif action_kind == "check":
                if target.input_type != "checkbox":
                    raise RuntimeError(f"Qwen selected a non-checkbox control for check: {label}.")
                await self._fill_checkbox(page, target)
                trace["checked_boxes"].append(label)
                trace["executed_actions"].append(f"check {label}")
            elif action_kind == "click":
                if looks_like_submit_button(target):
                    submission = await self._submit_with_button(page, target)
                    trace["clicked_buttons"].append(f"{label} (Qwen submit via click)")
                    trace["executed_actions"].append(f"submit {label}")
                    trace["submission"] = submission
                    break
                await self._click_button(page, target)
                trace["clicked_buttons"].append(f"{label} (Qwen next action)")
                trace["executed_actions"].append(f"click {label}")
                await asyncio.sleep(0.35)
                break
            elif action_kind == "submit":
                submission = await self._submit_with_button(page, target)
                trace["clicked_buttons"].append(f"{label} (Qwen submit)")
                trace["executed_actions"].append(f"submit {label}")
                trace["submission"] = submission
                break
            else:
                raise RuntimeError(f"Qwen returned an unsupported action kind: {action_kind}.")

            await asyncio.sleep(0.08)

        return trace

    def _readable_goal_actions(
        self,
        actions: list[dict[str, Any]],
        controls: list[FormControl],
        buttons: list[FormControl],
    ) -> list[str]:
        readable: list[str] = []
        for action in actions:
            target_type = str(action.get("target_type", "")).strip().lower()
            if target_type == "control":
                target = resolve_catalog_control(controls, action.get("target_index"))
            else:
                target = resolve_catalog_control(buttons, action.get("target_index"))
            if target is None:
                continue

            action_kind = str(action.get("action", "")).strip().lower()
            semantic_field = str(action.get("semantic_field", "")).strip()
            value = str(action.get("value", "")).strip()
            detail = f"{action_kind} -> {describe_control(target)}"
            if value and action_kind in {"fill", "select"}:
                detail += f" = {value}"
            readable.append(f"{semantic_field}: {detail}" if semantic_field else detail)
        return readable

    async def _fill_text(self, page: Page, control: FormControl, value: str) -> str:
        normalized = self._normalize_fill_value(control, value)
        locator = self._locator_for(page, control)
        await locator.scroll_into_view_if_needed()
        await locator.fill(normalized)
        return normalized

    async def _select_explicit_option(self, page: Page, control: FormControl, option_label: str) -> str:
        locator = self._locator_for(page, control)
        await locator.scroll_into_view_if_needed()
        try:
            await locator.select_option(label=option_label)
            return option_label
        except PlaywrightError:
            normalized_target = normalize_ui_text(option_label)
            for option in control.options:
                if normalize_ui_text(option) == normalized_target:
                    await locator.select_option(label=option)
                    return option
            raise RuntimeError(f"Could not find option '{option_label}' for {describe_control(control)}.")

    async def _submit_with_button(self, page: Page, submit_button: FormControl) -> dict[str, Any]:
        async with page.expect_response(
            lambda response: "/api/consultant-demo/" in response.url and response.request.method == "POST",
            timeout=10_000,
        ) as response_info:
            await self._click_button(page, submit_button)

        response = await response_info.value
        try:
            await page.wait_for_url("**/thank-you**", timeout=10_000)
            await page.wait_for_load_state("networkidle")
            payload = self._submission_payload_from_page_url(page.url)
            if payload:
                return payload
        except PlaywrightError:
            pass

        try:
            payload = await response.json()
            await page.wait_for_load_state("networkidle")
            return payload
        except PlaywrightError:
            await page.wait_for_load_state("networkidle")
            payload = self._submission_payload_from_page_url(page.url)
            if payload:
                return payload
            raise

    @staticmethod
    def _normalize_fill_value(control: FormControl, value: str) -> str:
        normalized = str(value or "").strip()
        if control.input_type != "date":
            return normalized

        for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(normalized, pattern).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return normalized

    async def _post_hf_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.settings.hf_api_token}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.settings.hf_timeout_seconds) as client:
                    response = await client.post(self.settings.hf_router_url, headers=headers, json=payload)
                    response.raise_for_status()
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Qwen request failed before the router returned a response.")

    async def _request_qwen_json(
        self,
        *,
        prompt: str,
        screenshot_data_urls: list[str],
        max_tokens: int,
    ) -> dict[str, Any]:
        active_prompt = prompt
        last_response_text = ""
        last_finish_reason = ""
        for attempt in range(2):
            payload: dict[str, Any] = {
                "model": self.settings.navigation_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": active_prompt}]
                        + [
                            {"type": "image_url", "image_url": {"url": screenshot_data_url}}
                            for screenshot_data_url in screenshot_data_urls
                        ],
                    }
                ],
                "max_tokens": max(max_tokens, 2200),
                "temperature": 0.0,
            }
            if supports_structured_outputs(self.settings.navigation_model):
                payload["response_format"] = {"type": "json_object"}

            body = await self._post_hf_chat(payload)
            choice = body["choices"][0]
            last_finish_reason = str(choice.get("finish_reason", "") or "")
            message_content = choice["message"]["content"]
            if isinstance(message_content, list):
                response_text = "\n".join(item.get("text", "") for item in message_content if isinstance(item, dict))
            else:
                response_text = str(message_content)
            last_response_text = response_text

            parsed = extract_json_object(response_text)
            if parsed is not None:
                return parsed

            active_prompt = (
                "Your previous browser-planning answer was invalid JSON or was too long.\n"
                "Return a COMPLETE compact JSON object only. No markdown. No prose outside JSON.\n"
                "Keep page_summary under 80 characters. Keep reasoning under 120 characters.\n"
                "Keep action reasoning under 80 characters or use an empty string.\n"
                "Use the exact schema and action vocabulary from the original request.\n\n"
                f"Original request:\n{prompt}"
            )

        raise ValueError(
            "Qwen response did not contain valid JSON"
            f" (finish_reason={last_finish_reason or 'unknown'}). Preview: {last_response_text[:500]!r}"
        )

    async def _repair_missing_actions(
        self,
        *,
        screenshot_data_urls: list[str],
        current_url: str,
        page_title: str,
        goal_values: dict[str, str],
        control_catalog: list[dict[str, Any]],
        button_catalog: list[dict[str, Any]],
        prior_reasoning: str,
        repair_reason: str,
    ) -> dict[str, Any] | None:
        for force_single_action in (False, True):
            prompt = (
                "Your previous browser-planning answer described the page but did not include executable actions.\n"
                "Return JSON only with the same schema as before and include at least one executable action unless the page is truly done.\n"
                "End goal: successfully submit this reimbursement request using the visible portal UI.\n"
                f"Current URL: {current_url}\n"
                f"Page title: {page_title}\n"
                f"Goal values: {json.dumps(goal_values, ensure_ascii=False)}\n"
                f"Prior reasoning: {prior_reasoning}\n"
                f"Control catalog: {json.dumps(control_catalog, ensure_ascii=False)}\n"
                f"Button catalog: {json.dumps(button_catalog, ensure_ascii=False)}\n"
                "Schema:\n"
                "{\n"
                '  "page_id": "",\n'
                '  "page_summary": "",\n'
                '  "reasoning": "",\n'
                '  "status": "continue|done",\n'
                '  "actions": [\n'
                '    {"action": "fill|select|upload|check|click|submit|done", "target_type": "control|button", "target_index": -1, "value": "", "semantic_field": "", "confidence": 0.0, "reasoning": ""}\n'
                "  ]\n"
                "}\n"
            )
            if repair_reason:
                prompt += f"Correct this problem in the new plan: {repair_reason}\n"
            if force_single_action:
                prompt += (
                    "Do not leave actions empty on a normal form page.\n"
                    "Return exactly one executable action now unless the page is truly done.\n"
                    "If a visible field can be filled safely from Goal values, prefer one fill/select/upload/check action.\n"
                    "If the only safe next step is a button press, return one click or submit action.\n"
                    "Never target a checkbox or file input with fill.\n"
                    "Never invent a missing form field when only buttons or acknowledgements are visible.\n"
                    "If a submit button is visible and no editable text/select/file controls remain, do not go back.\n"
                )

            try:
                parsed = await self._request_qwen_json(
                    prompt=prompt,
                    screenshot_data_urls=screenshot_data_urls,
                    max_tokens=900,
                )
            except ValueError:
                continue

            raw_actions = parsed.get("actions", [])
            actions = [
                item for item in raw_actions if isinstance(item, dict) and str(item.get("action", "")).strip()
            ]
            status = str(parsed.get("status", "continue")).strip().lower()
            if status not in {"continue", "done"}:
                status = "continue"
            if actions or status == "done":
                return {
                    "page_id": str(parsed.get("page_id", "")).strip() or "current_portal_step",
                    "page_summary": str(parsed.get("page_summary", "")).strip(),
                    "reasoning": str(parsed.get("reasoning", "")).strip(),
                    "status": status,
                    "actions": actions,
                }
        return None

    async def _fill_checkbox(self, page: Page, control: FormControl) -> None:
        locator = self._locator_for(page, control)
        await locator.scroll_into_view_if_needed()
        await locator.check()

    async def _set_file(self, page: Page, control: FormControl, receipt_path: Path) -> None:
        locator = self._locator_for(page, control)
        await locator.scroll_into_view_if_needed()
        await locator.set_input_files(str(receipt_path))

    async def _click_button(self, page: Page, control: FormControl) -> None:
        locator = self._locator_for(page, control)
        await locator.scroll_into_view_if_needed()
        await locator.click()

    @staticmethod
    def _encode_screenshot_data_url(screenshot_bytes: bytes) -> str:
        encoded = base64.b64encode(screenshot_bytes).decode("utf-8")
        return f"data:image/png;base64,{encoded}"

    async def _capture_scroll_screenshots(self, page: Page, max_shots: int = 3) -> list[str]:
        metrics = await page.evaluate(
            """
            () => {
              const root = document.scrollingElement || document.documentElement;
              const scrollHeight = Math.max(
                root ? root.scrollHeight : 0,
                document.documentElement ? document.documentElement.scrollHeight : 0,
                document.body ? document.body.scrollHeight : 0
              );
              const viewportHeight = window.innerHeight || (root ? root.clientHeight : 0) || 800;
              return { scrollHeight, viewportHeight };
            }
            """
        )
        positions = scroll_capture_positions(
            int(metrics.get("scrollHeight", 0) or 0),
            int(metrics.get("viewportHeight", 0) or 0),
            max_shots=max_shots,
        )

        screenshots: list[str] = []
        for position in positions:
            await page.evaluate(
                """
                (top) => {
                  const root = document.scrollingElement || document.documentElement;
                  if (root) root.scrollTo(0, top);
                  window.scrollTo(0, top);
                }
                """,
                position,
            )
            await asyncio.sleep(0.18)
            screenshot_bytes = await page.screenshot(type="png", full_page=False)
            screenshots.append(self._encode_screenshot_data_url(screenshot_bytes))

        await page.evaluate(
            """
            () => {
              const root = document.scrollingElement || document.documentElement;
              if (root) root.scrollTo(0, 0);
              window.scrollTo(0, 0);
            }
            """
        )
        return screenshots

    def _locator_for(self, page: Page, control: FormControl) -> Locator:
        if control.element_id:
            return page.locator(f'[id="{control.element_id}"]').first
        if control.name:
            return page.locator(f'{control.tag}[name="{control.name}"]').first
        if control.tag == "button" and control.text:
            return page.get_by_role("button", name=re.compile(re.escape(control.text), re.IGNORECASE))
        raise RuntimeError(f"Could not build a locator for control {control}")

    @staticmethod
    def _submission_payload_from_page_url(page_url: str) -> dict[str, Any]:
        parsed = urlparse(page_url)
        if "/thank-you" not in parsed.path:
            return {}

        params = parse_qs(parsed.query)

        def first(name: str) -> str:
            values = params.get(name, [])
            return values[0] if values else ""

        return {
            "status": "accepted",
            "submission_id": first("submission_id"),
            "claimed_amount": first("claimed_amount"),
            "receipt_total": first("receipt_total"),
            "explanation": first("explanation"),
            "submitted_at": first("submitted_at"),
            "vendor": first("vendor"),
            "expense_date": first("expense_date"),
            "expense_category": first("expense_category"),
        }

    async def close_session(self, session_id: str) -> None:
        page = self.pages.pop(session_id, None)
        context = self.contexts.pop(session_id, None)
        try:
            if page is not None:
                await page.close()
        finally:
            if context is not None:
                await context.close()

    async def close_all(self) -> None:
        session_ids = list(self.contexts)
        for session_id in session_ids:
            await self.close_session(session_id)
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
            self.browser_app_name = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def _activate_browser_app(self) -> None:
        if self.settings.browser_headless or sys.platform != "darwin" or not self.browser_app_name:
            return

        script = f'tell application "{self.browser_app_name}" to activate'
        await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )


async def verify_browser_runtime() -> None:
    runner = PortalAutomationRunner()
    try:
        await runner.open_portal("__probe__", "about:blank")
    except PlaywrightError as exc:  # pragma: no cover - depends on local browser runtime
        raise RuntimeError(str(exc)) from exc
    finally:
        await runner.close_all()
