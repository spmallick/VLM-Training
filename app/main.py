from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent import ExpenseAutomationAgent
from .agent_memory import (
    build_extraction_template,
    build_working_memory,
    merge_reviewed_fields,
    requested_receipt_fields,
)
from .agent_runtime.company_targets import get_agent_company_target
from .config import get_settings
from .policy import PolicyReviewService
from .portal_site.company_portals import (
    get_portal_company,
    list_portal_companies,
    load_portal_policy_document,
)
from .schemas import ExpenseFields
from .store import SessionStore
from .tools import BlurDetector, expense_agent_tool_catalog
from .vision import ReceiptVisionService


settings = get_settings()
store = SessionStore(settings.database_path)
vision_service = ReceiptVisionService(settings)
policy_service = PolicyReviewService(settings)
agent = ExpenseAutomationAgent(settings, store, policy_service)
blur_detector = BlurDetector()
agent_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
portal_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "portal_site" / "templates"))
app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

runner_tasks: dict[str, asyncio.Task] = {}
static_dir = Path(__file__).resolve().parent / "static"
multipart_available = importlib.util.find_spec("multipart") is not None


def asset_version() -> str:
    paths = [static_dir / "styles.css", static_dir / "app.js"]
    latest = max(int(path.stat().st_mtime) for path in paths if path.exists())
    return str(latest)


def _first_present(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _submission_success_copy(company_slug: str) -> dict[str, str]:
    if company_slug == "china":
        return {
            "eyebrow": "提交成功",
            "title": "报销申请已成功提交",
            "message": "感谢提交。本次报销申请已经被系统接收，下面是本次提交的关键信息。",
            "details_title": "提交详情",
            "summary_title": "下一步",
            "summary_body": "请保留此页面中的申请编号。如需再次演示，可以返回门户重新提交另一张票据。",
            "primary_cta": "返回公司选择",
            "secondary_cta": "再次提交报销",
            "submission_id": "申请编号",
            "submitted_at": "提交时间",
            "claimed_amount": "申请金额",
            "receipt_total": "票据总额",
            "vendor": "商家名称",
            "expense_date": "消费日期",
            "expense_category": "报销类型",
            "explanation": "扣减说明",
        }
    return {
        "eyebrow": "Submission received",
        "title": "Thank you. Your reimbursement request was submitted successfully.",
        "message": "The sandbox portal accepted the submission. Here is a quick summary of what was filed.",
        "details_title": "Submission details",
        "summary_title": "What happens next",
        "summary_body": "Keep the submission ID handy for the demo walkthrough. You can return to the portal and file another receipt any time.",
        "primary_cta": "Back to company selection",
        "secondary_cta": "Submit another receipt",
        "submission_id": "Submission ID",
        "submitted_at": "Submitted at",
        "claimed_amount": "Claimed amount",
        "receipt_total": "Receipt total",
        "vendor": "Vendor",
        "expense_date": "Expense date",
        "expense_category": "Expense category",
        "explanation": "Adjustment note",
    }


@app.on_event("startup")
async def startup() -> None:
    store.init_db()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return agent_templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": settings.app_name,
            "hf_model": settings.hf_model,
            "has_hf_token": bool(settings.hf_api_token),
            "companies": list_portal_companies(),
            "tool_catalog": expense_agent_tool_catalog(),
            "asset_version": asset_version(),
        },
    )


@app.get("/consultant-demo", response_class=HTMLResponse)
async def consultant_demo_home(request: Request) -> HTMLResponse:
    return portal_templates.TemplateResponse(
        request,
        "consultant_demo_home.html",
        {
            "companies": list_portal_companies(),
            "asset_version": asset_version(),
        },
    )


@app.get("/consultant-demo/{company_slug}", response_class=HTMLResponse)
async def consultant_company_portal(request: Request, company_slug: str) -> HTMLResponse:
    try:
        company = get_portal_company(company_slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown company portal") from exc

    policy_title, policy_bullets = load_portal_policy_document(company.policy_path)
    return portal_templates.TemplateResponse(
        request,
        company.template_name,
        {
            "company": company,
            "policy_title": policy_title,
            "policy_bullets": policy_bullets,
            "asset_version": asset_version(),
        },
    )


@app.get("/consultant-demo/{company_slug}/thank-you", response_class=HTMLResponse)
async def consultant_submission_success(
    request: Request,
    company_slug: str,
    submission_id: str = "",
    claimed_amount: str = "",
    receipt_total: str = "",
    explanation: str = "",
    submitted_at: str = "",
    vendor: str = "",
    expense_date: str = "",
    expense_category: str = "",
) -> HTMLResponse:
    try:
        company = get_portal_company(company_slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown company portal") from exc

    copy = _submission_success_copy(company_slug)
    return portal_templates.TemplateResponse(
        request,
        "company_submission_success.html",
        {
            "company": company,
            "copy": copy,
            "submission": {
                "submission_id": submission_id,
                "claimed_amount": claimed_amount,
                "receipt_total": receipt_total,
                "explanation": explanation,
                "submitted_at": submitted_at,
                "vendor": vendor,
                "expense_date": expense_date,
                "expense_category": expense_category,
            },
            "asset_version": asset_version(),
        },
    )


@app.post("/api/consultant-demo/{company_slug}/submit")
async def submit_consultant_demo(request: Request, company_slug: str, payload: dict) -> JSONResponse:
    try:
        company = get_portal_company(company_slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown company portal") from exc

    claim_amount = _first_present(
        payload,
        "_derived_claim_amount",
        "amount_to_route",
        "amount_requested",
        "claimed_amount",
        "requested_reimbursement",
        "queue_request_total",
        "shenqing_jine",
    )
    explanation = _first_present(
        payload,
        "_derived_explanation",
        "variance_narrative",
        "exclusion_brief",
        "adjustment_note",
        "variance_note",
        "exception_context",
        "koujian_shuoming",
    )
    receipt_total = _first_present(
        payload,
        "receipt_gross",
        "gross_charge_total",
        "fapiao_zonge",
    )
    vendor = _first_present(
        payload,
        "merchant_name",
        "provider_stamp",
        "shangjia_mingcheng",
    )
    expense_date = _first_present(
        payload,
        "service_day",
        "activity_day",
        "xiaofei_riqi",
    )
    expense_category = _first_present(
        payload,
        "claim_bucket",
        "expense_lane",
        "baoxiao_leixing",
    )
    submission_id = f"{company.slug[:3].upper()}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    thank_you_url = str(
        request.url_for("consultant_submission_success", company_slug=company_slug).include_query_params(
            submission_id=submission_id,
            claimed_amount=claim_amount,
            receipt_total=receipt_total,
            explanation=explanation,
            submitted_at=submitted_at,
            vendor=vendor,
            expense_date=expense_date,
            expense_category=expense_category,
        )
    )
    return JSONResponse(
        {
            "status": "accepted",
            "company": company.name,
            "submission_id": submission_id,
            "claimed_amount": claim_amount,
            "receipt_total": receipt_total,
            "explanation": explanation,
            "submitted_at": submitted_at,
            "redirect_url": thank_you_url,
        }
    )


@app.get("/portal/{session_id}", response_class=HTMLResponse)
async def portal_view(request: Request, session_id: str) -> HTMLResponse:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown session") from exc

    return agent_templates.TemplateResponse(
        request,
        "portal.html",
        {
            "session_id": session_id,
            "session": session,
            "asset_version": asset_version(),
        },
    )


if multipart_available:

    @app.post("/api/sessions/capture")
    async def capture_receipt(image: UploadFile = File(...), company_slug: str = Form("")) -> JSONResponse:
        company = None
        if company_slug:
            try:
                company = get_agent_company_target(company_slug)
            except KeyError as exc:
                raise HTTPException(status_code=400, detail="Unknown company portal") from exc

        session_id = store.create_session(company_slug=company_slug)
        extension = Path(image.filename or "receipt.jpg").suffix or ".jpg"
        file_path = settings.uploads_dir / f"{session_id}{extension}"
        file_path.write_bytes(await image.read())

        store.set_receipt_image(session_id, file_path)
        if company:
            store.append_event(
                session_id,
                f"Consultant selected {company.name} as the reimbursement target.",
                kind="action",
            )
        blur_check = blur_detector.assess(file_path)
        store.append_event(
            session_id,
            f"Tool: blur detector marked the image as {blur_check.verdict} (score {blur_check.score:.2f}).",
            kind="warning" if blur_check.verdict == "blurry" else "action",
        )
        store.append_event(session_id, "Receipt image stored locally. Starting extraction.")

        extraction_template, discovered = build_extraction_template(company_slug or "soberstack")
        store.save_extraction_template(session_id, extraction_template)
        receipt_fields = requested_receipt_fields(extraction_template)
        store.append_event(
            session_id,
            f"Form-governed extraction: the selected portal requested {', '.join(receipt_fields)}.",
            kind="action",
        )

        try:
            extraction = await vision_service.analyze_receipt(file_path, requested_fields=receipt_fields)
        except Exception as exc:
            store.update_status(
                session_id,
                status="error",
                current_step="extraction_failed",
                error_text=str(exc),
            )
            store.append_event(session_id, f"Receipt extraction failed: {exc}", kind="error")
            raise HTTPException(status_code=500, detail=f"Receipt extraction failed: {exc}") from exc

        store.set_extraction(session_id, extraction)
        working_memory = build_working_memory(
            company_slug=company_slug or "soberstack",
            receipt_image_path=file_path,
            blur_check=blur_check,
            extraction=extraction,
            extraction_template=extraction_template,
        )
        store.save_working_memory(session_id, working_memory)
        if discovered:
            store.append_event(
                session_id,
                f"Template growth: added semantic fields {', '.join(discovered)} for the selected company.",
                kind="action",
            )
        if extraction.retake_required:
            store.append_event(
                session_id,
                extraction.retake_reason
                or "The capture did not pass the receipt intake check. Please take the picture again.",
                kind="warning",
            )
        else:
            store.append_event(
                session_id,
                f"Extraction completed using {extraction.source.replace('_', ' ')}.",
                kind="success",
            )
        return JSONResponse(store.get_session(session_id).model_dump(mode="json"))

else:

    @app.post("/api/sessions/capture")
    async def capture_receipt_unavailable() -> JSONResponse:
        raise HTTPException(
            status_code=503,
            detail='Receipt upload is unavailable because the "python-multipart" package is missing.',
        )


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown session") from exc
    return JSONResponse(session.model_dump(mode="json"))


@app.get("/api/sessions/{session_id}/receipt")
async def get_receipt_image(session_id: str) -> FileResponse:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown session") from exc
    if not session.receipt_image_path:
        raise HTTPException(status_code=404, detail="No receipt image stored.")
    return FileResponse(session.receipt_image_path)


@app.post("/api/sessions/{session_id}/review")
async def save_review(session_id: str, payload: dict) -> JSONResponse:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown session") from exc
    if session.extraction and session.extraction.retake_required:
        raise HTTPException(
            status_code=400,
            detail=session.extraction.retake_reason
            or "The capture did not pass the receipt intake check. Please retake the picture first.",
        )

    fields = ExpenseFields.model_validate(payload)
    store.save_review(session_id, fields)
    updated_memory = merge_reviewed_fields(session.working_memory, fields)
    store.save_working_memory(session_id, updated_memory)
    store.append_event(session_id, "Review fields saved. The agent is ready to run.")
    return JSONResponse(store.get_session(session_id).model_dump(mode="json"))


@app.post("/api/sessions/{session_id}/run")
async def run_agent(session_id: str, request: Request) -> JSONResponse:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown session") from exc

    existing = runner_tasks.get(session_id)
    if existing and not existing.done():
        return JSONResponse({"status": "already_running"})

    if not session.company_slug:
        raise HTTPException(status_code=400, detail="Select a company before running the agent.")

    if session.extraction and session.extraction.retake_required:
        raise HTTPException(
            status_code=400,
            detail=session.extraction.retake_reason
            or "The capture did not pass the receipt intake check. Please retake the picture first.",
        )

    if not session.reviewed_fields.vendor and not session.reviewed_fields.total:
        raise HTTPException(status_code=400, detail="Review the extracted fields before running the agent.")

    runner_tasks[session_id] = asyncio.create_task(
        agent.run(session_id, base_url=str(request.base_url).rstrip("/"))
    )
    return JSONResponse({"status": "started"})
