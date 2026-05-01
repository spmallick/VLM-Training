"""Cross-platform consultant-side desktop intake app.

This app is intentionally simple:
- OpenCV HighGUI window for the main UI
- keyboard-driven company selection and camera capture
- native file dialog fallback where available
- pure Python intake logic that is separate from the company websites

Controls:
- `1`, `2`, `3`: select a company
- `c`: start/stop camera
- `space`: capture a photo from the live camera
- `u`: load a receipt image from disk
- `a`: run blur check + form-governed extraction
- `g`: run intake and open the selected company portal in the default browser
- `r`: clear the current receipt and results
- `q` or `esc`: quit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Thread

import cv2
import numpy as np


ROOT_DIR = Path(__file__).resolve().parents[1]
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
from app.agent_runtime.company_targets import get_agent_company_target, list_agent_company_targets
from app.config import get_settings
from app.policy import PolicyReviewService
from app.store import SessionStore
from app.tools import BlurDetector, CurrencyConverter, expense_agent_tool_catalog
from app.vision import ReceiptVisionService


WINDOW_NAME = "Consultant Agent"
CANVAS_SIZE = (1360, 900)


@dataclass
class IntakeResult:
    run_dir: Path
    image_path: Path
    company_name: str
    portal_url: str
    requested_fields: list[str]
    blur: dict
    extraction: dict
    template: dict
    working_memory: dict
    claim_amount_local: str
    warnings: list[str]


def choose_image_file() -> Path | None:
    if sys.platform == "darwin":
        script = 'POSIX path of (choose file with prompt "Select a receipt image")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        path = result.stdout.strip()
        return Path(path) if path else None

    if sys.platform.startswith("win"):
        script = textwrap.dedent(
            r"""
            Add-Type -AssemblyName System.Windows.Forms
            $dialog = New-Object System.Windows.Forms.OpenFileDialog
            $dialog.Filter = "Images|*.jpg;*.jpeg;*.png;*.bmp;*.webp"
            if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
              Write-Output $dialog.FileName
            }
            """
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        path = result.stdout.strip()
        return Path(path) if path else None

    for command in ("zenity", "kdialog"):
        if shutil.which(command):
            if command == "zenity":
                result = subprocess.run(
                    [
                        "zenity",
                        "--file-selection",
                        "--title=Select a receipt image",
                        "--file-filter=Images | *.jpg *.jpeg *.png *.bmp *.webp",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                result = subprocess.run(
                    [
                        "kdialog",
                        "--getopenfilename",
                        str(Path.home()),
                        "*.jpg *.jpeg *.png *.bmp *.webp",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            path = result.stdout.strip()
            return Path(path) if path else None

    return None


def draw_text(
    canvas: np.ndarray,
    text: str,
    origin: tuple[int, int],
    *,
    scale: float = 0.8,
    color: tuple[int, int, int] = (26, 43, 60),
    thickness: int = 2,
) -> None:
    cv2.putText(
        canvas,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def fit_image(image: np.ndarray, width: int, height: int) -> np.ndarray:
    image_h, image_w = image.shape[:2]
    if image_h == 0 or image_w == 0:
        return np.zeros((height, width, 3), dtype=np.uint8)
    scale = min(width / image_w, height / image_h)
    resized = cv2.resize(image, (max(1, int(image_w * scale)), max(1, int(image_h * scale))))
    canvas = np.full((height, width, 3), 244, dtype=np.uint8)
    y = (height - resized.shape[0]) // 2
    x = (width - resized.shape[1]) // 2
    canvas[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
    return canvas


def blend_color(color_a: tuple[int, int, int], color_b: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    return tuple(int(color_a[i] * (1.0 - alpha) + color_b[i] * alpha) for i in range(3))


def fill_vertical_gradient(
    canvas: np.ndarray,
    top_color: tuple[int, int, int],
    bottom_color: tuple[int, int, int],
) -> None:
    height = canvas.shape[0]
    for row in range(height):
        alpha = row / max(height - 1, 1)
        canvas[row, :] = blend_color(top_color, bottom_color, alpha)


def draw_rounded_rect(
    canvas: np.ndarray,
    rect: tuple[int, int, int, int],
    color: tuple[int, int, int],
    *,
    radius: int = 24,
    thickness: int = -1,
) -> None:
    x, y, w, h = rect
    radius = max(2, min(radius, w // 2, h // 2))

    if thickness < 0:
        cv2.rectangle(canvas, (x + radius, y), (x + w - radius, y + h), color, -1)
        cv2.rectangle(canvas, (x, y + radius), (x + w, y + h - radius), color, -1)
        cv2.circle(canvas, (x + radius, y + radius), radius, color, -1)
        cv2.circle(canvas, (x + w - radius, y + radius), radius, color, -1)
        cv2.circle(canvas, (x + radius, y + h - radius), radius, color, -1)
        cv2.circle(canvas, (x + w - radius, y + h - radius), radius, color, -1)
        return

    cv2.line(canvas, (x + radius, y), (x + w - radius, y), color, thickness, cv2.LINE_AA)
    cv2.line(canvas, (x + radius, y + h), (x + w - radius, y + h), color, thickness, cv2.LINE_AA)
    cv2.line(canvas, (x, y + radius), (x, y + h - radius), color, thickness, cv2.LINE_AA)
    cv2.line(canvas, (x + w, y + radius), (x + w, y + h - radius), color, thickness, cv2.LINE_AA)
    cv2.ellipse(canvas, (x + radius, y + radius), (radius, radius), 180, 0, 90, color, thickness, cv2.LINE_AA)
    cv2.ellipse(canvas, (x + w - radius, y + radius), (radius, radius), 270, 0, 90, color, thickness, cv2.LINE_AA)
    cv2.ellipse(canvas, (x + radius, y + h - radius), (radius, radius), 90, 0, 90, color, thickness, cv2.LINE_AA)
    cv2.ellipse(canvas, (x + w - radius, y + h - radius), (radius, radius), 0, 0, 90, color, thickness, cv2.LINE_AA)


def draw_card(
    canvas: np.ndarray,
    rect: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int],
    border: tuple[int, int, int] = (208, 214, 224),
    shadow: tuple[int, int, int] = (212, 219, 228),
    radius: int = 24,
) -> None:
    x, y, w, h = rect
    draw_rounded_rect(canvas, (x + 8, y + 10, w, h), shadow, radius=radius, thickness=-1)
    draw_rounded_rect(canvas, rect, fill, radius=radius, thickness=-1)
    draw_rounded_rect(canvas, rect, border, radius=radius, thickness=2)


def draw_wrapped_text(
    canvas: np.ndarray,
    text: str,
    origin: tuple[int, int],
    *,
    width: int,
    line_height: int = 28,
    scale: float = 0.58,
    color: tuple[int, int, int] = (72, 88, 102),
    thickness: int = 1,
) -> int:
    lines = textwrap.wrap(text, width=width) or [text]
    x, y = origin
    for line in lines:
        draw_text(canvas, line, (x, y), scale=scale, color=color, thickness=thickness)
        y += line_height
    return y


class ConsultantDesktopApp:
    def __init__(self, *, base_url: str, camera_index: int = 0):
        self.settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.camera_index = camera_index
        self.output_root = ROOT_DIR / "output" / "consultant_agent_runs"
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.vision_service = ReceiptVisionService(self.settings)
        self.blur_detector = BlurDetector()
        self.currency_converter = CurrencyConverter(self.settings.currency_rates_path)
        self.tool_catalog = expense_agent_tool_catalog()
        self.session_store = SessionStore(self.settings.database_path)
        self.session_store.init_db()
        self.policy_service = PolicyReviewService(self.settings)
        self.automation_agent = ExpenseAutomationAgent(self.settings, self.session_store, self.policy_service)
        self.company_portals = list_agent_company_targets()
        self.company_index = 0

        self.camera: cv2.VideoCapture | None = None
        self.live_frame: np.ndarray | None = None
        self.current_image: np.ndarray | None = None
        self.current_image_path: Path | None = None
        self.last_result: IntakeResult | None = None
        self.last_session_id: str | None = None
        self.status = "Choose a company, then capture or load a receipt image."

    @property
    def selected_company(self):
        return self.company_portals[self.company_index]

    def select_company(self, index: int) -> None:
        if not 0 <= index < len(self.company_portals):
            return
        self.company_index = index
        if self.current_image_path is not None:
            self.status = (
                f"Selected {self.selected_company.name}. Current receipt is still loaded, so you can submit it here too."
            )
            return
        self.status = f"Selected {self.selected_company.name}."

    def toggle_camera(self) -> None:
        if self.camera is not None:
            self.camera.release()
            self.camera = None
            self.live_frame = None
            self.status = "Camera stopped."
            return

        camera = cv2.VideoCapture(self.camera_index)
        if not camera.isOpened():
            self.status = "Could not open the camera."
            return
        self.camera = camera
        self.status = "Camera started. Press SPACE to capture a receipt photo."

    def capture_photo(self) -> None:
        if self.live_frame is None:
            self.status = "Start the camera first."
            return
        self.current_image = self.live_frame.copy()
        self.current_image_path = self._save_current_image("camera_capture.jpg")
        self.status = f"Captured a photo for {self.selected_company.name}."

    def load_photo(self) -> None:
        path = choose_image_file()
        if not path:
            self.status = "No image was selected."
            return
        image = cv2.imread(str(path))
        if image is None:
            self.status = f"Could not read {path}."
            return
        self.current_image = image
        self.current_image_path = self._save_current_image(path.name, source_path=path)
        self.status = f"Loaded {path.name}."

    def clear(self) -> None:
        self.current_image = None
        self.current_image_path = None
        self.last_result = None
        self.last_session_id = None
        self.status = "Cleared the current receipt."

    def analyze(self, *, open_portal: bool = False) -> None:
        if self.current_image_path is None:
            self.status = "Capture or load a receipt image first."
            return

        self.last_session_id = None
        company = self.selected_company
        receipt_path = self._stage_current_receipt_for_run()
        template, discovered = build_extraction_template(company.slug)
        requested_fields = requested_receipt_fields(template)
        blur = self.blur_detector.assess(receipt_path)
        extraction = asyncio.run(
            self.vision_service.analyze_receipt(receipt_path, requested_fields=requested_fields)
        )
        memory = build_working_memory(
            company_slug=company.slug,
            receipt_image_path=receipt_path,
            blur_check=blur,
            extraction=extraction,
            extraction_template=template,
        )
        reimbursement = compute_reimbursement(company.slug, memory, self.currency_converter)
        memory = apply_reimbursement_to_memory(
            memory,
            reimbursement,
            original_currency=extraction.fields.currency or self.settings.demo_currency,
        )

        run_dir = receipt_path.parent
        intake_result = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "company": {
                "slug": company.slug,
                "name": company.name,
                "portal_url": f"{self.base_url}{company.portal_path}",
            },
            "requested_fields": requested_fields,
            "discovered_fields": discovered,
            "blur_check": blur.model_dump(mode="json"),
            "extraction_template": template.model_dump(mode="json"),
            "extraction": extraction.model_dump(mode="json"),
            "working_memory": memory.model_dump(mode="json"),
            "tool_catalog": [tool.model_dump(mode="json") for tool in self.tool_catalog],
        }
        (run_dir / "intake_result.json").write_text(json.dumps(intake_result, indent=2), encoding="utf-8")
        (run_dir / "working_memory.json").write_text(
            json.dumps(memory.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        (run_dir / "tool_catalog.json").write_text(
            json.dumps([tool.model_dump(mode="json") for tool in self.tool_catalog], indent=2),
            encoding="utf-8",
        )
        self.last_result = IntakeResult(
            run_dir=run_dir,
            image_path=receipt_path,
            company_name=company.name,
            portal_url=f"{self.base_url}{company.portal_path}",
            requested_fields=requested_fields,
            blur=blur.model_dump(mode="json"),
            extraction=extraction.model_dump(mode="json"),
            template=template.model_dump(mode="json"),
            working_memory=memory.model_dump(mode="json"),
            claim_amount_local=reimbursement.claim_amount_local,
            warnings=list(extraction.warnings),
        )

        status_bits = [
            f"Blur: {blur.verdict}",
            f"Vendor: {extraction.fields.vendor or 'unknown'}",
            f"Total: {extraction.fields.total or 'unknown'} {extraction.fields.currency or ''}".strip(),
            f"Category: {extraction.fields.category or 'unknown'}",
            f"Claim: {reimbursement.claim_amount_local} {extraction.fields.currency or self.settings.demo_currency}".strip(),
        ]
        self.status = " | ".join(status_bits)

        should_hold = extraction.document_label == "not_receipt" or extraction.retake_required
        should_hold = should_hold or (
            blur.verdict == "blurry"
            and extraction.image_quality == "poor"
            and not extraction.critical_elements_visible
        )
        if should_hold:
            self.status += " | Retake recommended before opening a company portal."
        else:
            if blur.verdict == "blurry":
                self.status += " | Blur detector is cautious, but extraction still found the critical fields."
            if open_portal:
                session_id = self._launch_live_agent(
                    company_slug=company.slug,
                    company_name=company.name,
                    receipt_path=receipt_path,
                    extraction_template=template,
                    extraction=extraction,
                    working_memory=memory,
                )
                self.status += (
                    f" | Live portal automation started ({session_id}). You can switch companies and reuse this receipt."
                )

    def _launch_live_agent(
        self,
        *,
        company_slug: str,
        company_name: str,
        receipt_path: Path,
        extraction_template,
        extraction,
        working_memory,
    ) -> str:
        session_id = self.session_store.create_session(company_slug=company_slug)
        self.session_store.append_event(
            session_id,
            f"Consultant selected {company_name} as the reimbursement target.",
            kind="action",
        )
        self.session_store.set_receipt_image(session_id, receipt_path)
        self.session_store.save_extraction_template(session_id, extraction_template)
        self.session_store.set_extraction(session_id, extraction)
        self.session_store.save_working_memory(session_id, working_memory)
        self.session_store.save_review(session_id, extraction.fields)
        self._append_intake_trace_events(
            session_id=session_id,
            company_name=company_name,
            receipt_path=receipt_path,
            extraction_template=extraction_template,
            extraction=extraction,
            working_memory=working_memory,
        )
        self.session_store.append_event(
            session_id,
            (
                "Decision · intake gate passed\n"
                "Consultant desktop intake completed. Launching live browser automation."
            ),
            kind="action",
        )

        worker = Thread(target=self._run_agent_session, args=(session_id,), daemon=True)
        worker.start()
        self.last_session_id = session_id
        return session_id

    def _run_agent_session(self, session_id: str) -> None:
        asyncio.run(self.automation_agent.run(session_id, base_url=self.base_url))

    def _append_intake_trace_events(
        self,
        *,
        session_id: str,
        company_name: str,
        receipt_path: Path,
        extraction_template,
        extraction,
        working_memory,
    ) -> None:
        requested_fields = requested_receipt_fields(extraction_template)
        blur = working_memory.blur_check
        extracted = extraction.fields

        self.session_store.append_event(
            session_id,
            (
                "Step 1 · Receipt intake\n"
                f"Staged {receipt_path.name} for {company_name} in a fresh run folder so this same receipt can be reused for other companies later."
            ),
            kind="action",
        )

        if blur is not None:
            self.session_store.append_event(
                session_id,
                (
                    "Tool · check_image_quality\n"
                    f"Deterministic blur detector verdict: {blur.verdict}. Score: {blur.score:.2f}. "
                    f"Confidence: {blur.confidence:.2f}. {blur.summary}"
                ),
                kind="warning" if blur.verdict == "blurry" else "action",
            )

        self.session_store.append_event(
            session_id,
            (
                "Tool · build_extraction_template\n"
                f"The selected portal requested these receipt-backed fields: {', '.join(requested_fields)}."
            ),
            kind="action",
        )

        discovered = extraction_template.discovered_fields or []
        if discovered:
            self.session_store.append_event(
                session_id,
                (
                    "Tool · template enrichment\n"
                    f"Added company-specific semantic fields for this portal: {', '.join(discovered)}."
                ),
                kind="action",
            )

        self.session_store.append_event(
            session_id,
            (
                "Tool · extract_receipt_data\n"
                f"Engine: {self._extraction_engine_label(extraction)}. "
                f"Document: {extraction.document_label}. Visibility: {extraction.receipt_visibility}. "
                f"Image quality: {extraction.image_quality}. Confidence: {extraction.confidence:.2f}.\n"
                f"Reasoning: {extraction.reasoning_summary or 'The extraction engine summarized the visible receipt evidence.'}"
            ),
            kind="success" if not extraction.retake_required else "warning",
        )
        self.session_store.append_event(
            session_id,
            (
                "VLM response · receipt understanding\n"
                f"{extraction.reasoning_summary or 'The VLM confirmed the image looked like a valid receipt and extracted the visible fields.'}\n"
                f"Visible evidence: {', '.join(extraction.line_item_summary[:3]) or 'No line items summarized.'}"
            ),
            kind="action",
        )

        line_items = ", ".join(extraction.line_item_summary[:3]) if extraction.line_item_summary else "No clear line items extracted"
        self.session_store.append_event(
            session_id,
            (
                "Finding · receipt facts\n"
                f"Vendor: {extracted.vendor or 'Unknown'}. Date: {extracted.transaction_date or 'Unknown'}. "
                f"Total: {extracted.total or 'Unknown'} {extracted.currency or self.settings.demo_currency}. "
                f"Category: {extracted.category or 'Unknown'}.\n"
                f"Evidence preview: {line_items}."
            ),
            kind="action",
        )

        self.session_store.append_event(
            session_id,
            (
                "Tool · build_working_memory\n"
                f"Packed {len(working_memory.facts)} facts, {len(working_memory.derived_values)} derived values, and "
                f"{len(working_memory.page_requirements)} portal workflow steps into agent memory. "
                f"Live workflow steps will be discovered after the browser opens; currently known steps: {len(working_memory.page_requirements)}."
            ),
            kind="action",
        )

        if extraction.warnings:
            self.session_store.append_event(
                session_id,
                "Decision · extraction cautions\n" + "\n".join(f"- {warning}" for warning in extraction.warnings[:4]),
                kind="warning",
            )

        if extraction.retake_required:
            self.session_store.append_event(
                session_id,
                (
                    "Decision · hold before browser launch\n"
                    f"The receipt needs recapture before safe automation can continue. Reason: {extraction.retake_reason}"
                ),
                kind="warning",
            )

    def _extraction_engine_label(self, extraction) -> str:
        return f"Hugging Face VLM ({self.settings.hf_model})"

    def _create_run_dir(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_dir = self.output_root / timestamp
        suffix = 1
        while run_dir.exists():
            run_dir = self.output_root / f"{timestamp}_{suffix}"
            suffix += 1
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _stage_current_receipt_for_run(self) -> Path:
        if self.current_image_path is None:
            raise RuntimeError("No receipt image is available for this run.")

        extension = self.current_image_path.suffix or ".jpg"
        run_dir = self._create_run_dir()
        target = run_dir / f"receipt{extension}"
        shutil.copy2(self.current_image_path, target)
        return target

    def _save_current_image(self, filename: str, source_path: Path | None = None) -> Path:
        run_dir = self._create_run_dir()
        extension = Path(filename).suffix or ".jpg"
        target = run_dir / f"receipt{extension}"
        if source_path is not None:
            shutil.copy2(source_path, target)
        else:
            if self.current_image is None:
                raise RuntimeError("No current image to save.")
            cv2.imwrite(str(target), self.current_image)
        return target

    def update_camera_frame(self) -> None:
        if self.camera is None:
            return
        ok, frame = self.camera.read()
        if ok:
            self.live_frame = frame

    def render(self) -> np.ndarray:
        canvas = np.zeros((CANVAS_SIZE[1], CANVAS_SIZE[0], 3), dtype=np.uint8)
        fill_vertical_gradient(canvas, (242, 238, 231), (229, 235, 241))
        cv2.circle(canvas, (CANVAS_SIZE[0] - 120, 120), 170, (237, 243, 246), -1, cv2.LINE_AA)
        cv2.circle(canvas, (180, CANVAS_SIZE[1] - 90), 210, (236, 230, 219), -1, cv2.LINE_AA)

        margin = 36
        gap = 16
        left_w = 286
        preview_w = 610
        right_w = 360
        top_y = 154

        header_rect = (margin, 28, CANVAS_SIZE[0] - margin * 2, 104)
        companies_rect = (margin, top_y, left_w, 246)
        flow_rect = (margin, top_y + 246 + gap, left_w, 146)
        session_rect = (margin, top_y + 246 + gap + 146 + gap, left_w, 286)
        preview_rect = (margin + left_w + gap, top_y, preview_w, CANVAS_SIZE[1] - top_y - margin)
        controls_rect = (preview_rect[0] + preview_w + gap, top_y, right_w, 294)
        status_rect = (controls_rect[0], controls_rect[1] + controls_rect[3] + gap, right_w, 170)
        latest_rect = (controls_rect[0], status_rect[1] + status_rect[3] + gap, right_w, 214)

        cream_card = (250, 248, 243)
        light_card = (247, 249, 251)
        border = (213, 218, 224)
        title_color = (52, 38, 26)
        body_color = (79, 93, 104)
        accent_teal = (71, 122, 118)
        accent_gold = (160, 128, 39)
        accent_amber = (188, 126, 66)
        accent_green = (79, 136, 95)

        preview_source = self.live_frame if self.camera is not None and self.live_frame is not None else self.current_image
        preview_label = "Live camera" if self.camera is not None and self.live_frame is not None else "Loaded receipt"
        if preview_source is None:
            preview_label = "Awaiting capture"

        status_lower = self.status.lower()
        status_fill = (246, 249, 251)
        status_border = (208, 216, 224)
        status_accent = accent_teal
        if any(token in status_lower for token in ("retake", "warning", "blurry", "cautious")):
            status_fill = (249, 243, 235)
            status_border = (223, 197, 160)
            status_accent = accent_amber
        elif any(token in status_lower for token in ("started", "opened", "captured", "loaded", "selected")):
            status_fill = (237, 246, 239)
            status_border = (188, 213, 194)
            status_accent = accent_green

        draw_card(canvas, header_rect, fill=cream_card, border=border, shadow=(222, 226, 232), radius=28)
        draw_text(canvas, "Consultant-side receipt intake", (58, 61), scale=0.52, color=(116, 126, 132), thickness=1)
        draw_text(canvas, "Consultant Agent", (58, 103), scale=1.18, color=title_color, thickness=3)
        draw_wrapped_text(
            canvas,
            "Capture a receipt, review the extraction, and launch a live portal run without leaving this window.",
            (420, 66),
            width=50,
            line_height=24,
            scale=0.56,
            color=body_color,
        )

        badge_rect = (header_rect[0] + header_rect[2] - 270, 48, 220, 44)
        draw_rounded_rect(canvas, badge_rect, (237, 244, 239), radius=20, thickness=-1)
        draw_rounded_rect(canvas, badge_rect, (186, 210, 196), radius=20, thickness=2)
        draw_text(canvas, self.selected_company.name[:24], (badge_rect[0] + 18, badge_rect[1] + 29), scale=0.53, color=accent_green, thickness=2)

        draw_card(canvas, companies_rect, fill=light_card, border=border)
        draw_text(canvas, "Companies", (companies_rect[0] + 20, companies_rect[1] + 34), scale=0.72, color=title_color, thickness=2)
        draw_text(canvas, "Pick a reimbursement target", (companies_rect[0] + 20, companies_rect[1] + 62), scale=0.48, color=(118, 127, 135), thickness=1)

        tile_y = companies_rect[1] + 84
        for index, company in enumerate(self.company_portals):
            tile_rect = (companies_rect[0] + 18, tile_y + index * 54, companies_rect[2] - 36, 42)
            selected = index == self.company_index
            tile_fill = (235, 244, 242) if selected else (250, 250, 248)
            tile_border = (168, 197, 193) if selected else (224, 227, 232)
            draw_rounded_rect(canvas, tile_rect, tile_fill, radius=18, thickness=-1)
            draw_rounded_rect(canvas, tile_rect, tile_border, radius=18, thickness=2)

            marker_center = (tile_rect[0] + 22, tile_rect[1] + tile_rect[3] // 2)
            cv2.circle(canvas, marker_center, 12, accent_teal if selected else (206, 211, 217), -1, cv2.LINE_AA)
            draw_text(
                canvas,
                str(index + 1),
                (marker_center[0] - 6, marker_center[1] + 5),
                scale=0.5,
                color=(247, 249, 251),
                thickness=2,
            )
            draw_text(
                canvas,
                textwrap.shorten(company.name, width=24, placeholder="..."),
                (tile_rect[0] + 46, tile_rect[1] + 27),
                scale=0.56,
                color=title_color if selected else body_color,
                thickness=2 if selected else 1,
            )

        draw_card(canvas, flow_rect, fill=cream_card, border=border)
        draw_text(canvas, "Workflow", (flow_rect[0] + 20, flow_rect[1] + 34), scale=0.7, color=title_color, thickness=2)
        flow_lines = [
            ("1", "Capture or load a receipt"),
            ("2", "Analyze and review the extraction"),
            ("3", "Launch the live browser agent"),
        ]
        line_y = flow_rect[1] + 68
        for step, label in flow_lines:
            pill = (flow_rect[0] + 18, line_y - 18, 28, 28)
            draw_rounded_rect(canvas, pill, (238, 245, 239), radius=12, thickness=-1)
            draw_rounded_rect(canvas, pill, (185, 209, 192), radius=12, thickness=2)
            draw_text(canvas, step, (pill[0] + 9, pill[1] + 20), scale=0.48, color=accent_green, thickness=2)
            draw_wrapped_text(
                canvas,
                label,
                (flow_rect[0] + 58, line_y - 2),
                width=24,
                line_height=16,
                scale=0.44,
                color=body_color,
            )
            line_y += 34

        draw_card(canvas, session_rect, fill=light_card, border=border)
        draw_text(canvas, "Session", (session_rect[0] + 20, session_rect[1] + 34), scale=0.7, color=title_color, thickness=2)
        session_items = [
            ("Company", self.selected_company.name),
            ("Camera", "Live" if self.camera is not None else "Idle"),
            ("Receipt", "Ready" if self.current_image_path is not None else "Missing"),
            ("Launch", "Press G when the receipt looks good"),
        ]
        item_y = session_rect[1] + 68
        for label, value in session_items:
            draw_text(canvas, label.upper(), (session_rect[0] + 20, item_y), scale=0.4, color=(136, 144, 150), thickness=1)
            draw_wrapped_text(
                canvas,
                value,
                (session_rect[0] + 20, item_y + 26),
                width=22,
                line_height=20,
                scale=0.5,
                color=body_color if label != "Company" else title_color,
            )
            item_y += 52

        draw_card(canvas, preview_rect, fill=(248, 248, 246), border=border, shadow=(218, 224, 232), radius=28)
        draw_text(canvas, "Receipt Preview", (preview_rect[0] + 22, preview_rect[1] + 36), scale=0.78, color=title_color, thickness=2)
        preview_badge = (preview_rect[0] + preview_rect[2] - 180, preview_rect[1] + 16, 150, 34)
        badge_fill = (235, 244, 242) if preview_source is not None else (243, 239, 232)
        badge_border = (169, 196, 192) if preview_source is not None else (214, 204, 186)
        badge_color = accent_teal if preview_source is not None else accent_gold
        draw_rounded_rect(canvas, preview_badge, badge_fill, radius=16, thickness=-1)
        draw_rounded_rect(canvas, preview_badge, badge_border, radius=16, thickness=2)
        draw_text(canvas, preview_label, (preview_badge[0] + 18, preview_badge[1] + 22), scale=0.48, color=badge_color, thickness=2)

        image_frame = (preview_rect[0] + 20, preview_rect[1] + 62, preview_rect[2] - 40, preview_rect[3] - 86)
        draw_rounded_rect(canvas, image_frame, (241, 242, 240), radius=24, thickness=-1)
        draw_rounded_rect(canvas, image_frame, (219, 222, 226), radius=24, thickness=2)

        if preview_source is not None:
            fitted = fit_image(preview_source, image_frame[2] - 28, image_frame[3] - 28)
            fitted = cv2.copyMakeBorder(fitted, 0, 0, 0, 0, cv2.BORDER_CONSTANT, value=(245, 245, 243))
            y0 = image_frame[1] + 14
            x0 = image_frame[0] + 14
            canvas[y0 : y0 + fitted.shape[0], x0 : x0 + fitted.shape[1]] = fitted
        else:
            icon_rect = (image_frame[0] + image_frame[2] // 2 - 60, image_frame[1] + 150, 120, 84)
            draw_rounded_rect(canvas, icon_rect, (236, 242, 245), radius=22, thickness=-1)
            draw_rounded_rect(canvas, icon_rect, (201, 212, 220), radius=22, thickness=2)
            cv2.circle(canvas, (icon_rect[0] + 38, icon_rect[1] + 42), 18, (184, 197, 208), 3, cv2.LINE_AA)
            cv2.rectangle(canvas, (icon_rect[0] + 58, icon_rect[1] + 28), (icon_rect[0] + 86, icon_rect[1] + 56), (184, 197, 208), 3, cv2.LINE_AA)
            draw_text(canvas, "No receipt loaded yet", (image_frame[0] + 142, image_frame[1] + 190), scale=0.72, color=title_color, thickness=2)
            draw_wrapped_text(
                canvas,
                "Press C to start the camera, SPACE to capture, or U to load an image from disk.",
                (image_frame[0] + 116, image_frame[1] + 232),
                width=36,
                line_height=28,
                scale=0.58,
                color=(112, 123, 132),
            )

        draw_card(canvas, controls_rect, fill=light_card, border=border)
        draw_text(canvas, "Controls", (controls_rect[0] + 20, controls_rect[1] + 36), scale=0.76, color=title_color, thickness=2)
        control_rows = [
            ("1/2/3", "Select company"),
            ("C", "Start or stop camera"),
            ("SPACE", "Capture the current frame"),
            ("U", "Load a receipt from disk"),
            ("A", "Analyze without launching"),
            ("G", "Analyze and launch agent"),
            ("R", "Clear current receipt"),
            ("Q", "Quit"),
        ]
        row_y = controls_rect[1] + 76
        for key_label, label in control_rows:
            pill_w = 74 if key_label == "SPACE" else 56
            pill_rect = (controls_rect[0] + 18, row_y - 18, pill_w, 30)
            draw_rounded_rect(canvas, pill_rect, (242, 244, 247), radius=14, thickness=-1)
            draw_rounded_rect(canvas, pill_rect, (207, 213, 221), radius=14, thickness=2)
            draw_text(canvas, key_label, (pill_rect[0] + 12, pill_rect[1] + 21), scale=0.43, color=title_color, thickness=2)
            draw_text(canvas, label, (controls_rect[0] + 104, row_y + 3), scale=0.56, color=body_color, thickness=1)
            row_y += 32

        draw_card(canvas, status_rect, fill=status_fill, border=status_border)
        draw_text(canvas, "Status", (status_rect[0] + 20, status_rect[1] + 36), scale=0.76, color=title_color, thickness=2)
        status_pill = (status_rect[0] + status_rect[2] - 104, status_rect[1] + 16, 74, 32)
        draw_rounded_rect(canvas, status_pill, (245, 247, 249), radius=14, thickness=-1)
        draw_rounded_rect(canvas, status_pill, status_border, radius=14, thickness=2)
        draw_text(canvas, "LIVE", (status_pill[0] + 18, status_pill[1] + 21), scale=0.42, color=status_accent, thickness=2)
        draw_wrapped_text(
            canvas,
            self.status,
            (status_rect[0] + 20, status_rect[1] + 78),
            width=38,
            line_height=26,
            scale=0.58,
            color=title_color if status_accent != accent_amber else (92, 66, 38),
        )

        draw_card(canvas, latest_rect, fill=cream_card, border=border)
        draw_text(canvas, "Latest intake", (latest_rect[0] + 20, latest_rect[1] + 36), scale=0.74, color=title_color, thickness=2)
        if self.last_result is None:
            draw_wrapped_text(
                canvas,
                "Analyze a receipt to see the latest extraction, blur verdict, and saved run folder here.",
                (latest_rect[0] + 20, latest_rect[1] + 78),
                width=36,
                line_height=26,
                scale=0.56,
                color=body_color,
            )
        else:
            details = [
                ("Target", self.last_result.company_name),
                ("Blur", self.last_result.blur.get("verdict", "unknown")),
                ("Vendor", self.last_result.extraction.get("fields", {}).get("vendor", "unknown")),
                ("Total", self.last_result.extraction.get("fields", {}).get("total", "unknown")),
                ("Claim", self.last_result.claim_amount_local),
                ("Saved", self.last_result.run_dir.name),
            ]
            detail_y = latest_rect[1] + 74
            for label, value in details:
                draw_text(canvas, label.upper(), (latest_rect[0] + 20, detail_y), scale=0.38, color=(139, 145, 151), thickness=1)
                draw_wrapped_text(
                    canvas,
                    value,
                    (latest_rect[0] + 108, detail_y + 2),
                    width=20,
                    line_height=20,
                    scale=0.5,
                    color=body_color if label != "Vendor" else title_color,
                )
                detail_y += 30

            if self.last_result.warnings:
                warning_rect = (latest_rect[0] + 18, latest_rect[1] + latest_rect[3] - 60, latest_rect[2] - 36, 40)
                draw_rounded_rect(canvas, warning_rect, (249, 241, 233), radius=16, thickness=-1)
                draw_rounded_rect(canvas, warning_rect, (225, 194, 160), radius=16, thickness=2)
                draw_wrapped_text(
                    canvas,
                    self.last_result.warnings[0],
                    (warning_rect[0] + 14, warning_rect[1] + 24),
                    width=40,
                    line_height=18,
                    scale=0.42,
                    color=(108, 74, 44),
                )

        return canvas

    def run(self) -> None:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, CANVAS_SIZE[0], CANVAS_SIZE[1])
        try:
            while True:
                self.update_camera_frame()
                cv2.imshow(WINDOW_NAME, self.render())
                key = cv2.waitKey(30) & 0xFF

                if key in (27, ord("q")):
                    break
                if key in (ord("1"), ord("2"), ord("3")):
                    index = int(chr(key)) - 1
                    self.select_company(index)
                elif key == ord("c"):
                    self.toggle_camera()
                elif key == ord("u"):
                    self.load_photo()
                elif key == ord("r"):
                    self.clear()
                elif key == ord("a"):
                    self.analyze(open_portal=False)
                elif key == ord("g"):
                    self.analyze(open_portal=True)
                elif key == 32:
                    self.capture_photo()
        finally:
            if self.camera is not None:
                self.camera.release()
            cv2.destroyAllWindows()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Consultant-side desktop intake app built with Python and OpenCV.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011", help="Base URL where the company portals are served.")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index for OpenCV VideoCapture.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = ConsultantDesktopApp(base_url=args.base_url, camera_index=args.camera_index)
    app.run()


if __name__ == "__main__":
    main()
