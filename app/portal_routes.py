from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .portal_site.company_portals import (
    get_portal_company,
    list_portal_companies,
    load_portal_policy_document,
)


def _first_present(payload: dict, *keys: str) -> str:
    """Read the first non-empty value across the three intentionally different portals."""
    for key in keys:
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _submission_success_copy(company_slug: str) -> dict[str, str]:
    """Return localized copy for the sandbox portal thank-you page."""
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


def create_portal_router(
    portal_templates: Jinja2Templates,
    asset_version: Callable[[], str],
) -> APIRouter:
    """Routes for the sandbox websites the agent operates on."""
    router = APIRouter()

    @router.get("/consultant-demo", response_class=HTMLResponse)
    async def consultant_demo_home(request: Request) -> HTMLResponse:
        """Render the sandbox company picker that the agent treats as an external site."""
        return portal_templates.TemplateResponse(
            request,
            "consultant_demo_home.html",
            {
                "companies": list_portal_companies(),
                "asset_version": asset_version(),
            },
        )

    @router.get("/consultant-demo/{company_slug}", response_class=HTMLResponse)
    async def consultant_company_portal(request: Request, company_slug: str) -> HTMLResponse:
        """Render one sandbox company portal with its live policy text."""
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

    @router.get("/consultant-demo/{company_slug}/thank-you", response_class=HTMLResponse)
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

    @router.post("/api/consultant-demo/{company_slug}/submit")
    async def submit_consultant_demo(request: Request, company_slug: str, payload: dict) -> JSONResponse:
        """Accept a sandbox portal submission and return a thank-you redirect URL."""
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

    return router
