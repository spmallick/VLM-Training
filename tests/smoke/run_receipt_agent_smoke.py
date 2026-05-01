#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.agent import ExpenseAutomationAgent
from app.agent_memory import (
    apply_reimbursement_to_memory,
    build_extraction_template,
    build_working_memory,
    compute_reimbursement,
    requested_receipt_fields,
)
from app.agent_runtime.company_targets import get_agent_company_target
from app.config import Settings
from app.policy import PolicyReviewService
from app.store import SessionStore
from app.tools import BlurDetector, CurrencyConverter
from app.vision import ReceiptVisionService


@dataclass
class SmokeRunResult:
    company_slug: str
    session_id: str
    status: str
    current_step: str
    submission_id: str = ""
    portal_url: str = ""
    error_text: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the receipt agent headlessly against the local demo portals.")
    parser.add_argument(
        "--receipt",
        default="/Users/spmallick/Downloads/receipt.png",
        help="Absolute path to the receipt image to use.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL where the consultant demo portals are served.",
    )
    parser.add_argument(
        "--companies",
        nargs="*",
        default=["soberstack", "stingy", "china"],
        help="Company slugs to test.",
    )
    parser.add_argument(
        "--start-server",
        action="store_true",
        help="Start a local uvicorn server automatically if the base URL is not responding.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Maximum time to wait for each company run.",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=120,
        help="Inter-step demo delay in milliseconds for the agent loop.",
    )
    return parser.parse_args()


def ensure_server(base_url: str, *, start_server: bool) -> subprocess.Popen[str] | None:
    if server_ready(base_url):
        return None
    if not start_server:
        raise RuntimeError(f"The demo server at {base_url} is not responding. Start it first or pass --start-server.")

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if server_ready(base_url):
            return process
        time.sleep(0.5)
    process.terminate()
    raise RuntimeError(f"Started uvicorn, but {base_url} never became ready.")


def server_ready(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/consultant-demo", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


def create_run_dir(company_slug: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = ROOT_DIR / "output" / "cli_smoke_runs" / f"{timestamp}_{company_slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def stage_receipt(receipt_path: Path, company_slug: str) -> Path:
    run_dir = create_run_dir(company_slug)
    target = run_dir / f"receipt{receipt_path.suffix or '.png'}"
    shutil.copy2(receipt_path, target)
    return target


def log(message: str) -> None:
    print(message, flush=True)


async def run_company(
    *,
    settings: Settings,
    base_url: str,
    company_slug: str,
    receipt_source: Path,
    store: SessionStore,
    vision_service: ReceiptVisionService,
    blur_detector: BlurDetector,
    currency_converter: CurrencyConverter,
    policy_service: PolicyReviewService,
) -> SmokeRunResult:
    company = get_agent_company_target(company_slug)
    receipt_path = stage_receipt(receipt_source, company_slug)
    extraction_template, _discovered = build_extraction_template(company.slug)
    requested_fields = requested_receipt_fields(extraction_template)
    blur = blur_detector.assess(receipt_path)
    extraction = await vision_service.analyze_receipt(receipt_path, requested_fields=requested_fields)
    working_memory = build_working_memory(
        company_slug=company.slug,
        receipt_image_path=receipt_path,
        blur_check=blur,
        extraction=extraction,
        extraction_template=extraction_template,
    )
    reimbursement = compute_reimbursement(company.slug, working_memory, currency_converter)
    working_memory = apply_reimbursement_to_memory(
        working_memory,
        reimbursement,
        original_currency=extraction.fields.currency or settings.demo_currency,
    )

    session_id = store.create_session(company_slug=company.slug)
    store.append_event(session_id, f"CLI smoke test selected {company.name}.", kind="action")
    store.set_receipt_image(session_id, receipt_path)
    store.save_extraction_template(session_id, extraction_template)
    store.set_extraction(session_id, extraction)
    store.save_working_memory(session_id, working_memory)
    store.save_review(session_id, extraction.fields)
    store.append_event(
        session_id,
        (
            "CLI smoke test bootstrap\n"
            f"Receipt: {receipt_path.name}\n"
            f"Portal: {base_url.rstrip('/')}{company.portal_path}\n"
            f"Requested fields: {', '.join(requested_fields)}"
        ),
        kind="action",
    )

    agent = ExpenseAutomationAgent(settings, store, policy_service)
    try:
        await asyncio.wait_for(agent.run(session_id, base_url=base_url), timeout=args.timeout_seconds)
    except Exception:
        session = store.get_session(session_id)
        return SmokeRunResult(
            company_slug=company.slug,
            session_id=session_id,
            status=session.status,
            current_step=session.current_step,
            submission_id=session.portal_state.submission_id,
            portal_url=f"{base_url.rstrip('/')}{company.portal_path}",
            error_text=session.error_text,
        )
    finally:
        await agent.close_all_runners()

    session = store.get_session(session_id)
    return SmokeRunResult(
        company_slug=company.slug,
        session_id=session_id,
        status=session.status,
        current_step=session.current_step,
        submission_id=session.portal_state.submission_id,
        portal_url=f"{base_url.rstrip('/')}{company.portal_path}",
        error_text=session.error_text,
    )


def print_result(result: SmokeRunResult, store: SessionStore) -> None:
    status_label = "PASS" if result.status == "completed" and result.submission_id else "FAIL"
    log(
        f"[{status_label}] {result.company_slug} "
        f"session={result.session_id} status={result.status} step={result.current_step} "
        f"submission={result.submission_id or '-'}"
    )
    if status_label == "FAIL":
        session = store.get_session(result.session_id)
        recent = session.events[-6:]
        for event in recent:
            headline = event.message.splitlines()[0] if event.message else ""
            log(f"  - {event.kind}: {headline}")
        if result.error_text:
            log(f"  error: {result.error_text}")


if __name__ == "__main__":
    args = parse_args()
    receipt_path = Path(args.receipt).expanduser().resolve()
    if not receipt_path.exists():
        raise SystemExit(f"Receipt image not found: {receipt_path}")

    server_process = ensure_server(args.base_url, start_server=args.start_server)
    settings = Settings(browser_headless=True, demo_fill_delay_ms=args.delay_ms)
    store = SessionStore(settings.database_path)
    store.init_db()
    vision_service = ReceiptVisionService(settings)
    blur_detector = BlurDetector()
    currency_converter = CurrencyConverter(settings.currency_rates_path)
    policy_service = PolicyReviewService(settings)

    results: list[SmokeRunResult] = []
    try:
        for company_slug in args.companies:
            log(f"\n=== Running {company_slug} ===")
            result = asyncio.run(
                run_company(
                    settings=settings,
                    base_url=args.base_url,
                    company_slug=company_slug,
                    receipt_source=receipt_path,
                    store=store,
                    vision_service=vision_service,
                    blur_detector=blur_detector,
                    currency_converter=currency_converter,
                    policy_service=policy_service,
                )
            )
            results.append(result)
            print_result(result, store)
    finally:
        if server_process is not None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()

    summary_path = ROOT_DIR / "output" / "cli_smoke_runs" / "latest_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8")

    failed = [result for result in results if not (result.status == "completed" and result.submission_id)]
    if failed:
        raise SystemExit(1)
