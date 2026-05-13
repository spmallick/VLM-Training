import asyncio

from app.config import Settings
from app.portal_automation import (
    FormControl,
    PortalAutomationRunner,
    control_catalog_entry,
    resolve_catalog_control,
    scroll_capture_positions,
)
from app.schemas import PortalState


def make_control(
    *,
    tag: str,
    input_type: str = "",
    name: str = "",
    element_id: str = "",
    label: str = "",
    placeholder: str = "",
    options: tuple[str, ...] = (),
    text: str = "",
) -> FormControl:
    return FormControl(
        tag=tag,
        input_type=input_type,
        name=name,
        element_id=element_id,
        label=label,
        placeholder=placeholder,
        options=options,
        text=text,
    )


class DummyPortalRunner(PortalAutomationRunner):
    def __init__(
        self,
        *,
        controls: list[FormControl] | None = None,
        buttons: list[FormControl] | None = None,
        qwen_responses: list[dict] | None = None,
    ):
        super().__init__(settings=Settings(hf_api_token="test-token"))
        self._controls = controls or []
        self._buttons = buttons or []
        self._qwen_responses = list(qwen_responses or [])
        self.prompt_history: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.selects: list[tuple[str, str]] = []
        self.uploads: list[str] = []
        self.checks: list[str] = []
        self.clicks: list[str] = []
        class DummyPage:
            url = "http://127.0.0.1:8000/consultant-demo/stingy"

            async def title(self):
                return "Stingy Portal"

        self.pages["session"] = DummyPage()

    async def inspect_visible_controls(self, page):
        return self._controls, self._buttons

    async def _capture_scroll_screenshots(self, page, max_shots: int = 3):
        return ["shot-top", "shot-middle", "shot-bottom"][:max_shots]

    async def _request_qwen_json(self, *, prompt: str, screenshot_data_urls: list[str], max_tokens: int):
        self.prompt_history.append(prompt)
        response = self._qwen_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def _fill_text(self, page, control, value: str):
        normalized = self._normalize_fill_value(control, value)
        self.fills.append((control.name or control.element_id, normalized))
        return normalized

    async def _select_explicit_option(self, page, control, option_label: str):
        self.selects.append((control.name or control.element_id, option_label))
        return option_label

    async def _set_file(self, page, control, receipt_path):
        self.uploads.append(control.name or control.element_id)

    async def _fill_checkbox(self, page, control):
        self.checks.append(control.name or control.element_id)

    async def _click_button(self, page, control):
        self.clicks.append(control.text or control.name or control.element_id)

    async def _submit_with_button(self, page, submit_button):
        self.clicks.append(f"submit:{submit_button.text or submit_button.name or submit_button.element_id}")
        return {"status": "accepted", "submission_id": "DEMO-123"}


def test_control_catalog_entry_and_resolution_round_trip():
    control = make_control(tag="input", input_type="text", name="merchant_name", label="Vendor name")

    entry = control_catalog_entry(control, 0)

    assert entry["index"] == 0
    assert entry["name"] == "merchant_name"
    assert resolve_catalog_control([control], 0) == control
    assert resolve_catalog_control([control], -1) is None
    assert resolve_catalog_control([control], "not-a-number") is None


def test_scroll_capture_positions_covers_top_middle_and_bottom():
    positions = scroll_capture_positions(scroll_height=3000, viewport_height=1000, max_shots=3)

    assert positions == [0, 1000, 2000]


def test_browser_launch_plan_respects_headless_setting():
    headed_runner = PortalAutomationRunner()
    headless_runner = PortalAutomationRunner(settings=Settings(_env_file=None, BROWSER_HEADLESS=True))

    assert headed_runner.browser_launch_plan()[0] == ({"channel": "chrome", "headless": False}, "Google Chrome")
    assert headless_runner.browser_launch_plan() == [({"headless": True}, "Headless Chromium")]


def test_submission_payload_can_be_recovered_from_thank_you_url():
    payload = PortalAutomationRunner._submission_payload_from_page_url(
        "http://127.0.0.1:8000/consultant-demo/stingy/thank-you"
        "?submission_id=STI-20260422-013000"
        "&claimed_amount=12.00"
        "&receipt_total=14.16"
        "&submitted_at=2026-04-22+01%3A30%3A00+UTC"
        "&vendor=Main+Street+Restaurant"
        "&expense_date=2017-04-07"
        "&expense_category=Food"
        "&explanation=Excluded+tip"
    )

    assert payload["status"] == "accepted"
    assert payload["submission_id"] == "STI-20260422-013000"
    assert payload["claimed_amount"] == "12.00"
    assert payload["vendor"] == "Main Street Restaurant"


def test_request_qwen_json_uses_navigation_model():
    runner = PortalAutomationRunner(
        settings=Settings(
            _env_file=None,
            hf_api_token="test-token",
            HF_MODEL="receipt-model",
            HF_NAVIGATION_MODEL="navigation-model",
        )
    )
    captured_payload = {}

    async def fake_post_hf_chat(payload):
        captured_payload.update(payload)
        return {"choices": [{"message": {"content": '{"status": "ok"}'}}]}

    runner._post_hf_chat = fake_post_hf_chat

    parsed = asyncio.run(
        runner._request_qwen_json(prompt="Plan the current page.", screenshot_data_urls=[], max_tokens=50)
    )

    assert parsed == {"status": "ok"}
    assert captured_payload["model"] == "navigation-model"


def test_request_qwen_json_retries_invalid_json_with_compact_prompt():
    runner = PortalAutomationRunner(settings=Settings(_env_file=None, hf_api_token="test-token"))
    captured_prompts = []
    responses = [
        {"choices": [{"finish_reason": "length", "message": {"content": '{"page_id": "review", "reasoning": "'}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": '{"status": "continue", "actions": []}'}}]},
    ]

    async def fake_post_hf_chat(payload):
        captured_prompts.append(payload["messages"][0]["content"][0]["text"])
        return responses.pop(0)

    runner._post_hf_chat = fake_post_hf_chat

    parsed = asyncio.run(
        runner._request_qwen_json(prompt="Plan the current page.", screenshot_data_urls=[], max_tokens=50)
    )

    assert parsed == {"status": "continue", "actions": []}
    assert len(captured_prompts) == 2
    assert "previous browser-planning answer was invalid JSON" in captured_prompts[1]


def test_plan_next_actions_repairs_missing_actions_and_collects_semantic_fields():
    controls = [make_control(tag="input", input_type="text", name="provider_stamp", label="Supplier name")]
    buttons = [make_control(tag="button", text="Review request")]
    runner = DummyPortalRunner(
        controls=controls,
        buttons=buttons,
        qwen_responses=[
            {
                "page_id": "expense_form",
                "page_summary": "Expense form",
                "reasoning": "Need to fill the visible vendor field.",
                "status": "continue",
                "actions": [],
            },
            {
                "page_id": "expense_form",
                "page_summary": "Expense form",
                "reasoning": "Repair added concrete actions.",
                "status": "continue",
                "actions": [
                    {
                        "action": "fill",
                        "target_type": "control",
                        "target_index": 0,
                        "value": "Main Street Restaurant",
                        "semantic_field": "vendor",
                    },
                    {
                        "action": "click",
                        "target_type": "button",
                        "target_index": 0,
                        "value": "",
                        "semantic_field": "",
                    },
                ],
            },
        ],
    )

    plan = asyncio.run(
        runner.plan_next_actions(
            session_id="session",
            portal_state=PortalState(vendor="Main Street Restaurant", total="12.00", category="Meals"),
            allow_submit=False,
        )
    )

    assert len(runner.prompt_history) == 2
    assert "End goal: successfully submit this reimbursement request" in runner.prompt_history[0]
    assert "did not include executable actions" in runner.prompt_history[1]
    assert plan["page_id"] == "expense_form"
    assert plan["semantic_fields"] == ("vendor",)
    assert plan["trace"]["inspection_mode"] == "qwen_goal_directed"
    assert plan["trace"]["screenshot_count"] == 3
    assert plan["trace"]["matched_fields"] == [
        "vendor: fill -> Supplier name (provider_stamp) = Main Street Restaurant",
        "click -> Review request",
    ]


def test_plan_next_actions_uses_strict_second_repair_when_first_repair_is_still_empty():
    controls = [make_control(tag="input", input_type="text", name="provider_stamp", label="Supplier name")]
    runner = DummyPortalRunner(
        controls=controls,
        qwen_responses=[
            {
                "page_id": "expense_form",
                "page_summary": "Expense form",
                "reasoning": "The vendor field should be filled next.",
                "status": "continue",
                "actions": [],
            },
            {
                "page_id": "expense_form",
                "page_summary": "Expense form",
                "reasoning": "Still thinking about the next action.",
                "status": "continue",
                "actions": [],
            },
            {
                "page_id": "expense_form",
                "page_summary": "Expense form",
                "reasoning": "The visible supplier name field should be filled now.",
                "status": "continue",
                "actions": [
                    {
                        "action": "fill",
                        "target_type": "control",
                        "target_index": 0,
                        "value": "Main Street Restaurant",
                        "semantic_field": "vendor",
                    }
                ],
            },
        ],
    )

    plan = asyncio.run(
        runner.plan_next_actions(
            session_id="session",
            portal_state=PortalState(vendor="Main Street Restaurant", total="12.00", category="Meals"),
            allow_submit=False,
        )
    )

    assert len(runner.prompt_history) == 3
    assert "Do not leave actions empty on a normal form page." in runner.prompt_history[2]
    assert plan["trace"]["matched_fields"] == [
        "vendor: fill -> Supplier name (provider_stamp) = Main Street Restaurant"
    ]


def test_plan_next_actions_repairs_invalid_fill_on_checkbox():
    controls = [make_control(tag="input", input_type="checkbox", element_id="review_ack", label="Review acknowledgement")]
    buttons = [make_control(tag="button", text="Submit workflow packet")]
    runner = DummyPortalRunner(
        controls=controls,
        buttons=buttons,
        qwen_responses=[
            {
                "page_id": "review_submit",
                "page_summary": "Review page",
                "reasoning": "A reduction note warning is visible.",
                "status": "continue",
                "actions": [
                    {
                        "action": "fill",
                        "target_type": "control",
                        "target_index": 0,
                        "value": "Some note",
                        "semantic_field": "adjustment_note",
                    }
                ],
            },
            {
                "page_id": "review_submit",
                "page_summary": "Review page",
                "reasoning": "The acknowledgement box should be checked next.",
                "status": "continue",
                "actions": [
                    {
                        "action": "check",
                        "target_type": "control",
                        "target_index": 0,
                        "value": "",
                        "semantic_field": "review_ack",
                    }
                ],
            },
        ],
    )

    plan = asyncio.run(
        runner.plan_next_actions(
            session_id="session",
            portal_state=PortalState(vendor="Main Street Restaurant", total="12.00", category="Meals"),
            allow_submit=True,
        )
    )

    assert len(runner.prompt_history) == 2
    assert "Action 'fill' cannot target Review acknowledgement." in runner.prompt_history[1]
    assert plan["trace"]["matched_fields"] == ["review_ack: check -> Review acknowledgement"]


def test_plan_next_actions_repairs_back_button_on_final_review_page():
    controls = [make_control(tag="input", input_type="checkbox", element_id="review_ack", label="Review acknowledgement")]
    buttons = [
        make_control(tag="button", text="Back to form"),
        make_control(tag="button", text="Submit workflow packet"),
    ]
    runner = DummyPortalRunner(
        controls=controls,
        buttons=buttons,
        qwen_responses=[
            {
                "page_id": "review_submit",
                "page_summary": "Final review page",
                "reasoning": "A warning mentions the reduction note, so I should go back.",
                "status": "continue",
                "actions": [
                    {
                        "action": "click",
                        "target_type": "button",
                        "target_index": 0,
                        "value": "",
                        "semantic_field": "",
                    }
                ],
            },
            {
                "page_id": "review_submit",
                "page_summary": "Final review page",
                "reasoning": "The page is ready to submit.",
                "status": "continue",
                "actions": [
                    {
                        "action": "submit",
                        "target_type": "button",
                        "target_index": 1,
                        "value": "",
                        "semantic_field": "submit_reimbursement",
                    }
                ],
            },
        ],
    )

    plan = asyncio.run(
        runner.plan_next_actions(
            session_id="session",
            portal_state=PortalState(vendor="Main Street Restaurant", total="12.00", category="Meals"),
            allow_submit=True,
        )
    )

    assert len(runner.prompt_history) == 2
    assert "Do not click a back button when the page is already in final review" in runner.prompt_history[1]
    assert plan["trace"]["matched_fields"] == ["submit_reimbursement: submit -> Submit workflow packet"]


def test_plan_next_actions_submits_when_qwen_marks_ready_but_submit_is_visible():
    controls = [
        make_control(tag="input", input_type="text", name="merchant_name", label="Vendor name", text="Main Street Restaurant"),
        make_control(tag="input", input_type="file", name="receipt_packet", label="Upload receipt"),
    ]
    buttons = [make_control(tag="button", text="Submit expense report")]
    runner = DummyPortalRunner(
        controls=controls,
        buttons=buttons,
        qwen_responses=[
            {
                "page_id": "expense_form",
                "page_summary": "Expense form with all fields filled.",
                "reasoning": "All required fields are filled and the receipt is uploaded.",
                "status": "done",
                "actions": [{"action": "done", "target_type": "button", "target_index": -1}],
            },
        ],
    )

    plan = asyncio.run(
        runner.plan_next_actions(
            session_id="session",
            portal_state=PortalState(vendor="Main Street Restaurant", total="12.00", category="Meals"),
            allow_submit=True,
        )
    )

    assert len(runner.prompt_history) == 1
    assert plan["status"] == "continue"
    assert plan["semantic_fields"] == ("submit_reimbursement",)
    assert plan["trace"]["matched_fields"] == ["submit_reimbursement: submit -> Submit expense report"]


def test_execute_action_plan_runs_the_current_action_vocabulary():
    controls = [
        make_control(
            tag="select",
            name="expense_lane",
            label="Expense category",
            options=("Choose one", "Food", "Travel"),
        ),
        make_control(tag="input", input_type="date", name="activity_day", label="Expense date"),
        make_control(tag="input", input_type="file", name="supporting_packet", label="Upload receipt"),
        make_control(tag="input", input_type="checkbox", element_id="stingy-review-ack", label="Review acknowledgement"),
    ]
    buttons = [make_control(tag="button", text="Review request")]
    runner = DummyPortalRunner(controls=controls, buttons=buttons)

    trace = asyncio.run(
        runner.execute_action_plan(
            session_id="session",
            plan={
                "page_id": "expense_form",
                "controls": controls,
                "buttons": buttons,
                "actions": [
                    {"action": "select", "target_type": "control", "target_index": 0, "value": "Food", "semantic_field": "expense_category"},
                    {"action": "fill", "target_type": "control", "target_index": 1, "value": "04/07/2017", "semantic_field": "expense_date"},
                    {"action": "upload", "target_type": "control", "target_index": 2, "value": "", "semantic_field": "receipt"},
                    {"action": "check", "target_type": "control", "target_index": 3, "value": "", "semantic_field": "review_ack"},
                    {"action": "click", "target_type": "button", "target_index": 0, "value": "", "semantic_field": ""},
                ],
            },
            receipt_path=Settings().uploads_dir / "unused.png",
        )
    )

    assert runner.selects == [("expense_lane", "Food")]
    assert runner.fills == [("activity_day", "2017-04-07")]
    assert runner.uploads == ["supporting_packet"]
    assert runner.checks == ["stingy-review-ack"]
    assert runner.clicks == ["Review request"]
    assert trace["selected_options"] == ["expense_category -> Food"]
    assert trace["filled_fields"] == ["expense_date"]
    assert trace["uploaded_receipt"] is True
    assert trace["checked_boxes"] == ["Review acknowledgement"]
    assert trace["clicked_buttons"] == ["Review request (Qwen next action)"]
    assert trace["executed_actions"][-1] == "click Review request"


def test_execute_action_plan_treats_submit_like_click_as_submission():
    buttons = [make_control(tag="button", text="Submit workflow packet")]
    runner = DummyPortalRunner(buttons=buttons)

    trace = asyncio.run(
        runner.execute_action_plan(
            session_id="session",
            plan={
                "page_id": "review_submit",
                "controls": [],
                "buttons": buttons,
                "actions": [
                    {"action": "click", "target_type": "button", "target_index": 0, "value": "", "semantic_field": ""},
                ],
            },
            receipt_path=Settings().uploads_dir / "unused.png",
        )
    )

    assert runner.clicks == ["submit:Submit workflow packet"]
    assert trace["submission"] == {"status": "accepted", "submission_id": "DEMO-123"}
    assert trace["clicked_buttons"] == ["Submit workflow packet (Qwen submit via click)"]
    assert trace["executed_actions"] == ["submit Submit workflow packet"]


def test_execute_action_plan_replans_after_upload_before_submit():
    controls = [make_control(tag="input", input_type="file", name="receipt_packet", label="Upload receipt")]
    buttons = [make_control(tag="button", text="Submit expense report")]
    runner = DummyPortalRunner(controls=controls, buttons=buttons)

    trace = asyncio.run(
        runner.execute_action_plan(
            session_id="session",
            plan={
                "page_id": "expense_form",
                "controls": controls,
                "buttons": buttons,
                "actions": [
                    {"action": "upload", "target_type": "control", "target_index": 0, "value": "", "semantic_field": "receipt"},
                    {"action": "submit", "target_type": "button", "target_index": 0, "value": "", "semantic_field": "submit_reimbursement"},
                ],
            },
            receipt_path=Settings().uploads_dir / "unused.png",
        )
    )

    assert runner.uploads == ["receipt_packet"]
    assert runner.clicks == []
    assert trace["uploaded_receipt"] is True
    assert trace["submission"] == {}
    assert trace["executed_actions"] == ["upload Upload receipt (receipt_packet)"]
