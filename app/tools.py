from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from .schemas import BlurCheckResult, ToolDefinition


def expense_agent_tool_catalog() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_image_quality",
            kind="perception",
            execution="deterministic",
            purpose="Detect blur, darkness, or crop issues before extraction starts.",
        ),
        ToolDefinition(
            name="extract_receipt_data",
            kind="perception",
            execution="vlm",
            purpose="Read vendor, date, currency, totals, and visible line-item evidence from the receipt.",
        ),
        ToolDefinition(
            name="convert_currency",
            kind="reasoning",
            execution="deterministic",
            purpose="Convert receipt amounts into USD using a local demo FX table.",
        ),
        ToolDefinition(
            name="read_portal_policy",
            kind="reasoning",
            execution="deterministic",
            purpose="Read policy text from the live portal page using Playwright before claim math runs.",
        ),
        ToolDefinition(
            name="compute_reimbursable_amount",
            kind="reasoning",
            execution="deterministic",
            purpose="Apply policy rules to remove non-reimbursable amounts and recompute the claim.",
        ),
        ToolDefinition(
            name="open_company_portal",
            kind="action",
            execution="deterministic",
            purpose="Open the selected local reimbursement portal before form filling begins.",
        ),
        ToolDefinition(
            name="inspect_form_ui",
            kind="perception",
            execution="vlm",
            purpose="Interpret the current form or workflow from a screenshot without relying on fixed selectors.",
        ),
        ToolDefinition(
            name="browser_action",
            kind="action",
            execution="deterministic",
            purpose="Execute low-level browser actions such as click, type, select, upload, check, next, and submit.",
        ),
        ToolDefinition(
            name="validate_submission",
            kind="guardrail",
            execution="hybrid",
            purpose="Check required fields, derived claim math, and policy consistency before submit.",
        ),
        ToolDefinition(
            name="hold_for_review",
            kind="guardrail",
            execution="deterministic",
            purpose="Pause safely when the receipt or UI is too ambiguous for confident automation.",
        ),
    ]


class BlurDetector:
    """Small deterministic blur detector for the demo intake gate."""

    def assess(self, image_path: Path) -> BlurCheckResult:
        image = Image.open(image_path).convert("L")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((1200, 1200))

        edge_map = image.filter(
            ImageFilter.Kernel(
                size=(3, 3),
                kernel=[-1, -1, -1, -1, 8, -1, -1, -1, -1],
                scale=1,
            )
        )
        variance = float(ImageStat.Stat(edge_map).var[0])

        if variance >= 1200:
            verdict = "clear"
            confidence = 0.92
            summary = "The receipt edges look sharp enough for extraction."
        elif variance >= 350:
            verdict = "slightly_blurry"
            confidence = 0.74
            summary = "The receipt is readable, but the image looks a little soft."
        else:
            verdict = "blurry"
            confidence = 0.9
            summary = "The image is too blurry for a reliable extraction run."

        return BlurCheckResult(
            verdict=verdict,
            score=round(variance, 2),
            confidence=confidence,
            summary=summary,
        )


class CurrencyConverter:
    def __init__(self, rates_path: Path):
        self.rates_path = rates_path

    def supported_currencies(self) -> list[str]:
        payload = json.loads(self.rates_path.read_text())
        rates = payload.get("rates_to_usd", {})
        return sorted(rates)

    def convert(self, amount: str, from_currency: str, to_currency: str = "USD") -> str | None:
        if not amount or not from_currency or not to_currency:
            return None

        try:
            numeric_amount = float(amount)
        except ValueError:
            return None

        payload = json.loads(self.rates_path.read_text())
        rates = payload.get("rates_to_usd", {})
        source = from_currency.upper()
        target = to_currency.upper()
        source_rate = rates.get(source)
        target_rate = rates.get(target)
        if source_rate is None or target_rate is None or target_rate == 0:
            return None

        usd_amount = numeric_amount * float(source_rate)
        converted_amount = usd_amount / float(target_rate)
        return f"{converted_amount:.2f}"

    def convert_to_usd(self, amount: str, currency: str) -> str | None:
        return self.convert(amount, currency, "USD")
