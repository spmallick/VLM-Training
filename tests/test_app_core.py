import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent_memory import build_extraction_template, build_working_memory, compute_reimbursement
from app.agent import ExpenseAutomationAgent
from app.config import Settings
from app.policy import PolicyReviewService
from app.schemas import BlurCheckResult, ExpenseFields, ExtractionPayload, MemoryFact, PageRequirement, WorkingMemory
from app.store import SessionStore
from app.tools import CurrencyConverter
from app.vision import ReceiptVisionService, normalize_amount, normalize_date, normalize_expense_category
from consultant_agent.highgui_app import ConsultantDesktopApp


def test_normalization_helpers():
    assert normalize_amount("$13.53") == "13.53"
    assert normalize_date("2026-03-13") == "2026-03-13"
    assert normalize_expense_category("Food & Beverage") == "Meals"
    assert normalize_expense_category("Transport") == "Travel"
    assert normalize_expense_category("") == ""


def test_store_and_agent_normalization(tmp_path: Path):
    currency_rates_path = tmp_path / "currency_rates.json"
    currency_rates_path.write_text('{"rates_to_usd":{"USD":1.0,"EUR":1.08}}', encoding="utf-8")

    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        currency_rates_path=currency_rates_path,
        hf_api_token=None,
    )
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(settings.database_path)
    store.init_db()

    session_id = store.create_session()
    receipt_path = settings.uploads_dir / "receipt.jpg"
    receipt_path.write_bytes(b"fake")
    store.set_receipt_image(session_id, receipt_path)
    store.set_extraction(
        session_id,
        ExtractionPayload(
            fields=ExpenseFields(
                vendor="Blue Bottle Coffee",
                transaction_date="2026-03-13",
                total="13.53",
                subtotal="12.50",
                tax="",
                currency="usd",
                category="",
                payment_method="",
                notes="",
            ),
            reasoning_summary="test",
            receipt_visibility="full",
        ),
    )

    agent = ExpenseAutomationAgent(settings, store, PolicyReviewService(settings))
    normalized = agent._normalize_fields(store.get_session(session_id).reviewed_fields)
    conversion_note = agent._build_currency_note(
        ExpenseFields(total="10.00", currency="EUR")
    )

    assert normalized.currency == "USD"
    assert normalized.category == "Other"
    assert normalized.tax == "1.03"
    assert normalized.payment_method == "Corporate Card"
    assert "receipt agent" in normalized.notes.lower()
    assert conversion_note == (
        "Tool: currency converter estimated about 10.80 USD from 10.00 EUR using the local JSON rate table."
    )


def test_agent_uses_a_distinct_portal_runner_per_session(tmp_path: Path):
    currency_rates_path = tmp_path / "currency_rates.json"
    currency_rates_path.write_text('{"rates_to_usd":{"USD":1.0}}', encoding="utf-8")

    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        currency_rates_path=currency_rates_path,
        hf_api_token=None,
    )
    store = SessionStore(settings.database_path)
    store.init_db()

    agent = ExpenseAutomationAgent(settings, store, PolicyReviewService(settings))

    first = agent._portal_runner_for_session("session-a")
    second = agent._portal_runner_for_session("session-b")

    assert first is agent._portal_runner_for_session("session-a")
    assert second is agent._portal_runner_for_session("session-b")
    assert first is not second


def test_policy_review_requires_qwen_token(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        hf_api_token=None,
    )
    settings.hf_api_token = None
    policy_service = PolicyReviewService(settings)
    extraction = ExtractionPayload(
        fields=ExpenseFields(
            vendor="Sample Bistro",
            transaction_date="2026-03-13",
            total="88.00",
            currency="EUR",
        ),
        reasoning_summary="test",
        receipt_visibility="partial",
    )

    with pytest.raises(RuntimeError, match="Qwen3-VL policy review requires"):
        asyncio.run(policy_service.review_receipt(tmp_path / "receipt.jpg", extraction, extraction.fields))


def test_compute_reimbursement_uses_live_policy_and_full_reimbursement_fallback(tmp_path: Path):
    currency_rates_path = tmp_path / "currency_rates.json"
    currency_rates_path.write_text('{"rates_to_usd":{"USD":1.0}}', encoding="utf-8")
    settings = Settings(currency_rates_path=currency_rates_path)
    extraction_template, _ = build_extraction_template("soberstack")
    memory = build_working_memory(
        company_slug="soberstack",
        receipt_image_path=tmp_path / "receipt.jpg",
        blur_check=BlurCheckResult(verdict="clear", score=1000.0, confidence=0.9),
        extraction=ExtractionPayload(
            fields=ExpenseFields(
                vendor="Dinner House",
                transaction_date="2026-03-13",
                total="100.00",
                currency="USD",
                category="Meals",
            ),
            semantic_amounts={"alcohol_amount": "25.00", "tip_amount": "10.00"},
            reasoning_summary="test",
        ),
        extraction_template=extraction_template,
    )

    with_policy = compute_reimbursement(
        "soberstack",
        memory,
        CurrencyConverter(settings.currency_rates_path),
        live_policy_text="Alcohol is never reimbursable.",
    )
    no_policy = compute_reimbursement(
        "soberstack",
        memory,
        CurrencyConverter(settings.currency_rates_path),
        live_policy_text="",
    )

    assert with_policy.claim_amount_local == "75.00"
    assert no_policy.claim_amount_local == "100.00"
    assert "No live portal policy" in no_policy.explanation


def test_normalized_capture_assessment_allows_soft_but_complete_receipts(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        hf_api_token=None,
    )
    vision = ReceiptVisionService(settings)

    assessment = vision._normalize_capture_assessment(
        {
            "document_label": "receipt",
            "receipt_visibility": "unclear",
            "image_quality": "unclear",
            "critical_elements_visible": True,
            "missing_critical_elements": [],
            "retake_required": True,
            "retake_reason": "The image is a little soft.",
        },
        ExpenseFields(
            vendor="Blue Bottle Coffee",
            transaction_date="2026-03-13",
            total="13.53",
            currency="USD",
        ),
    )

    assert assessment["retake_required"] is False
    assert assessment["critical_elements_visible"] is True
    assert assessment["retake_reason"] == ""


def test_store_marks_session_for_recapture_when_capture_fails_gate(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        hf_api_token=None,
    )
    store = SessionStore(settings.database_path)
    store.init_db()

    session_id = store.create_session()
    store.set_extraction(
        session_id,
        ExtractionPayload(
            fields=ExpenseFields(),
            reasoning_summary="test",
            document_label="not_receipt",
            receipt_visibility="unclear",
            image_quality="poor",
            critical_elements_visible=False,
            missing_critical_elements=["vendor", "transaction_date", "total"],
            retake_required=True,
            retake_reason="The image does not appear to show a receipt.",
        ),
    )

    session = store.get_session(session_id)

    assert session.status == "needs_recapture"
    assert session.current_step == "retake_requested"
    assert session.extraction is not None
    assert session.extraction.retake_required is True


def test_session_store_echoes_events_to_stdout(tmp_path: Path, capsys):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        hf_api_token=None,
    )
    store = SessionStore(settings.database_path)
    store.init_db()

    session_id = store.create_session()
    capsys.readouterr()

    store.append_event(session_id, "Tool · inspect_form_ui\nPage: expense_form.", kind="action")

    captured = capsys.readouterr()
    assert session_id in captured.out
    assert "[ACTION]" in captured.out
    assert "]\n  Tool · inspect_form_ui" in captured.out
    assert "\n  Page: expense_form." in captured.out


def test_receipt_can_be_reused_for_multiple_company_runs(tmp_path: Path):
    app = object.__new__(ConsultantDesktopApp)
    app.output_root = tmp_path / "runs"
    app.output_root.mkdir(parents=True, exist_ok=True)
    app.company_portals = [
        SimpleNamespace(name="SoberStack Consulting"),
        SimpleNamespace(name="Stingy Corp."),
    ]
    app.company_index = 0

    source = tmp_path / "loaded" / "receipt.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("demo receipt", encoding="utf-8")
    app.current_image_path = source

    first_run_receipt = ConsultantDesktopApp._stage_current_receipt_for_run(app)
    app.select_company(1)
    second_run_receipt = ConsultantDesktopApp._stage_current_receipt_for_run(app)

    assert app.current_image_path == source
    assert app.company_index == 1
    assert "still loaded" in app.status.lower()
    assert first_run_receipt != second_run_receipt
    assert first_run_receipt.parent != second_run_receipt.parent
    assert first_run_receipt.read_text(encoding="utf-8") == "demo receipt"
    assert second_run_receipt.read_text(encoding="utf-8") == "demo receipt"


def test_intake_trace_events_include_model_and_blur_details(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        data_dir=tmp_path,
        uploads_dir=tmp_path / "uploads",
        hf_api_token="demo-token",
        hf_model="Qwen/Qwen3-VL-8B-Instruct",
    )
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(settings.database_path)
    store.init_db()
    session_id = store.create_session(company_slug="soberstack")

    app = object.__new__(ConsultantDesktopApp)
    app.settings = settings
    app.session_store = store

    extraction_template, _ = build_extraction_template("soberstack")
    extraction = ExtractionPayload(
        fields=ExpenseFields(
            vendor="Blue Bottle Coffee",
            transaction_date="2026-03-13",
            total="13.53",
            currency="USD",
            category="Meals",
        ),
        reasoning_summary="Qwen3-VL found a clear restaurant receipt.",
        source="huggingface_qwen3_vl",
        document_label="receipt",
        receipt_visibility="full",
        image_quality="clear",
        confidence=0.91,
    )
    working_memory = WorkingMemory(
        blur_check=BlurCheckResult(
            verdict="clear",
            score=1820.4,
            confidence=0.92,
            summary="The receipt edges look sharp enough for extraction.",
        ),
        facts={"vendor_name": MemoryFact(value="Blue Bottle Coffee", source="receipt", confidence=0.93)},
        derived_values={"claim_amount_local": MemoryFact(value="13.53", source="derived", confidence=0.88)},
        page_requirements=[PageRequirement(page_id="expense_form", required_fields=["vendor_name", "claim_amount"])],
    )

    ConsultantDesktopApp._append_intake_trace_events(
        app,
        session_id=session_id,
        company_name="SoberStack Consulting",
        receipt_path=tmp_path / "receipt.jpg",
        extraction_template=extraction_template,
        extraction=extraction,
        working_memory=working_memory,
    )

    session = store.get_session(session_id)
    combined = "\n".join(event.message for event in session.events)

    assert "Deterministic blur detector" in combined
    assert "Qwen/Qwen3-VL-8B-Instruct" in combined
    assert "Packed 1 facts, 1 derived values, and 1 portal workflow steps" in combined
