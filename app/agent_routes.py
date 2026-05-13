from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .agent import ExpenseAutomationAgent
from .agent_memory import (
    build_extraction_template,
    build_working_memory,
    merge_reviewed_fields,
    requested_receipt_fields,
)
from .agent_runtime.company_targets import get_agent_company_target
from .config import Settings
from .portal_site.company_portals import list_portal_companies
from .schemas import ExpenseFields
from .store import SessionStore
from .tools import expense_agent_tool_catalog
from .vision import ReceiptVisionService


def create_agent_router(
    *,
    settings: Settings,
    store: SessionStore,
    vision_service: ReceiptVisionService,
    agent: ExpenseAutomationAgent,
    agent_templates: Jinja2Templates,
    asset_version: Callable[[], str],
) -> APIRouter:
    """Routes for the agent control UI and session API.

    In FastAPI, a router is a group of related web endpoints. These endpoints
    are the "agent side" of the demo: the browser UI calls them to upload a
    receipt, review extracted fields, start the agent, and poll progress.
    """
    router = APIRouter()

    # In-progress agent runs are background tasks keyed by session. The UI polls
    # the session store for progress instead of waiting on this request.
    runner_tasks: dict[str, asyncio.Task] = {}

    # File uploads require python-multipart. The fallback route below turns a
    # missing dependency into a clear web error instead of a confusing crash.
    multipart_available = importlib.util.find_spec("multipart") is not None

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Render the main page where the user starts an agent run."""
        return agent_templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_name": settings.app_name,
                "hf_model": settings.hf_model,
                "receipt_model": settings.receipt_model,
                "policy_model": settings.policy_model,
                "navigation_model": settings.navigation_model,
                "has_hf_token": bool(settings.hf_api_token),
                "companies": list_portal_companies(),
                "tool_catalog": expense_agent_tool_catalog(),
                "asset_version": asset_version(),
            },
        )

    @router.get("/portal/{session_id}", response_class=HTMLResponse)
    async def portal_view(request: Request, session_id: str) -> HTMLResponse:
        """Render the progress page for one reimbursement session."""
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

        @router.post("/api/sessions/capture")
        async def capture_receipt(image: UploadFile = File(...), company_slug: str = Form("")) -> JSONResponse:
            """Create a session and run the receipt-intake part of the agent."""
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

            # From this point on, the session store is the UI's source of truth.
            # The frontend receives the session JSON and renders each event.
            store.set_receipt_image(session_id, file_path)
            if company:
                store.append_event(
                    session_id,
                    f"Consultant selected {company.name} as the reimbursement target.",
                    kind="action",
                )
            store.append_event(session_id, "Receipt image stored locally. Starting Qwen extraction.")

            # The selected portal controls which semantic fields we ask Qwen to extract.
            # This keeps receipt extraction tied to the current task instead of a fixed form.
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
            # Working memory is the compact handoff from receipt understanding to browser automation.
            working_memory = build_working_memory(
                company_slug=company_slug or "soberstack",
                receipt_image_path=file_path,
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

        @router.post("/api/sessions/capture")
        async def capture_receipt_unavailable() -> JSONResponse:
            """Return a clear API error when file upload support is not installed."""
            raise HTTPException(
                status_code=503,
                detail='Receipt upload is unavailable because the "python-multipart" package is missing.',
            )

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        """Return the latest session state for frontend polling."""
        try:
            session = store.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc
        return JSONResponse(session.model_dump(mode="json"))

    @router.get("/api/sessions/{session_id}/receipt")
    async def get_receipt_image(session_id: str) -> FileResponse:
        """Serve the uploaded receipt image back to the browser UI."""
        try:
            session = store.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc
        if not session.receipt_image_path:
            raise HTTPException(status_code=404, detail="No receipt image stored.")
        return FileResponse(session.receipt_image_path)

    @router.post("/api/sessions/{session_id}/review")
    async def save_review(session_id: str, payload: dict) -> JSONResponse:
        """Save the user's corrections before browser automation starts."""
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

    @router.post("/api/sessions/{session_id}/run")
    async def run_agent(session_id: str, request: Request) -> JSONResponse:
        """Start the browser-driving agent loop without blocking the web request."""
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

    return router
