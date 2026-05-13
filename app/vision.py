from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx

from .config import Settings
from .schemas import ExpenseFields, ExtractionPayload


# Qwen is asked to normalize currency already. This map only absorbs common
# symbols or lowercase names if the provider returns a near-miss value.
CURRENCY_ALIASES = {
    "$": "USD",
    "usd": "USD",
    "us$": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "cny": "CNY",
    "rmb": "CNY",
    "yuan": "CNY",
    "renminbi": "CNY",
    "jpy": "JPY",
    "yen": "JPY",
    "cad": "CAD",
    "c$": "CAD",
    "aud": "AUD",
    "a$": "AUD",
    "sgd": "SGD",
    "s$": "SGD",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "mxn": "MXN",
    "peso": "MXN",
    "pesos": "MXN",
}

# Qwen owns semantic classification. These aliases only map harmless wording
# variants into the four portal buckets used by the demo forms.
CANONICAL_CATEGORY_ALIASES = {
    "Meals": (
        "meals",
        "meal",
        "food",
        "food & beverage",
        "food and beverage",
        "food beverage",
        "dining",
        "餐饮",
        "餐费",
    ),
    "Travel": (
        "travel",
        "transport",
        "transportation",
        "transit",
        "taxi",
        "rideshare",
        "交通",
        "差旅",
        "出行",
    ),
    "Lodging": (
        "lodging",
        "accommodation",
        "hotel",
        "stay",
        "room",
        "住宿",
        "酒店",
        "宾馆",
    ),
    "Other": (
        "other",
        "general",
        "office supplies",
        "software",
        "supplies",
        "其他",
    ),
}


def normalize_amount(raw: str) -> str:
    """Keep only the numeric part of amount strings returned by Qwen."""
    cleaned = re.sub(r"[^0-9.,-]", "", raw).replace(",", "")
    if cleaned.count(".") > 1:
        first, *rest = cleaned.split(".")
        cleaned = first + "." + "".join(rest)
    return cleaned


def normalize_date(raw: str) -> str:
    """Trust Qwen's YYYY-MM-DD contract and remove only surrounding spaces."""
    return raw.strip()


def normalize_currency_code(raw: str, default: str = "USD") -> str:
    """Normalize Qwen's currency value into a three-letter currency code."""
    cleaned = raw.strip()
    if not cleaned:
        return default

    lowered = cleaned.lower()
    if lowered in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[lowered]
    if cleaned in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[cleaned]
    if len(cleaned) == 3 and cleaned.isalpha():
        return cleaned.upper()
    return default


def normalize_expense_category(raw: str) -> str:
    """Map Qwen's category label into the portal's canonical buckets."""
    candidate = raw.strip()
    normalized_candidate = re.sub(r"\s+", " ", candidate.lower()).strip()

    for canonical, aliases in CANONICAL_CATEGORY_ALIASES.items():
        if normalized_candidate == canonical.lower() or normalized_candidate in aliases:
            return canonical

    return "Other" if candidate else ""


def extract_json_object(text: str) -> dict | None:
    """Parse the JSON object returned by Qwen.

    The Hugging Face router is requested to return a JSON object. The small
    fallback below handles occasional full markdown fences or text before the
    first object without reimplementing JSON parsing.
    """
    if isinstance(text, dict):
        return text
    if not text:
        return None

    normalized = str(text).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", normalized, re.DOTALL | re.IGNORECASE)
    if fenced:
        normalized = fenced.group(1).strip()

    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        if start < 0:
            return None
        try:
            parsed, _end = json.JSONDecoder().raw_decode(normalized[start:])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def supports_structured_outputs(model_id: str) -> bool:
    """Return whether the HF provider accepts OpenAI-style JSON response mode."""
    # The Novita-hosted Qwen3-VL Thinking models currently reject
    # response_format={"type": "json_object"} even though they can still follow
    # an explicit "JSON only" prompt. Keep structured outputs for Instruct
    # models and omit the flag for Thinking models.
    return "thinking" not in (model_id or "").lower()


class ReceiptVisionService:
    """Qwen3-VL receipt extraction service."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _format_hf_error(response: httpx.Response) -> str:
        """Turn Hugging Face router errors into readable app errors."""
        try:
            body = response.json()
        except ValueError:
            body = response.text
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                message = str(error.get("message", "")).strip()
                code = str(error.get("code", "")).strip()
                if message and code:
                    return f"Hugging Face router returned {response.status_code} ({code}): {message}"
                if message:
                    return f"Hugging Face router returned {response.status_code}: {message}"
            return f"Hugging Face router returned {response.status_code}: {body}"
        return f"Hugging Face router returned {response.status_code}: {str(body)[:500]}"

    async def analyze_receipt(self, image_path: Path, requested_fields: list[str] | None = None) -> ExtractionPayload:
        """Extract receipt facts with Qwen3-VL and return normalized app data."""
        if not self.settings.hf_api_token:
            raise RuntimeError(
                "Qwen3-VL receipt extraction requires a Hugging Face token."
            )
        try:
            return await self._extract_with_hugging_face(image_path, requested_fields=requested_fields)
        except Exception as exc:
            raise RuntimeError(f"Qwen3-VL receipt extraction failed: {exc}") from exc

    async def _extract_with_hugging_face(
        self,
        image_path: Path,
        *,
        requested_fields: list[str] | None = None,
    ) -> ExtractionPayload:
        data_url = self._encode_data_url(image_path)
        requested_fields = requested_fields or []
        requested_fields_text = ", ".join(requested_fields) if requested_fields else "vendor, transaction_date, total"
        # The prompt is the extraction contract: Qwen decides whether the image
        # is a receipt, extracts visible fields, and returns policy-relevant
        # semantic amounts that later policy math can use.
        prompt = (
            "You are checking whether an image shows a valid receipt and extracting structured expense data for an internal expense filing demo.\n"
            f"The selected company form currently needs these receipt-backed fields: {requested_fields_text}.\n"
            "Prioritize those fields first. Leave unrelated fields blank unless they are obviously visible and useful.\n"
            "Return JSON only with the following schema:\n"
            "{\n"
            '  "vendor": "",\n'
            '  "transaction_date": "",\n'
            '  "total": "",\n'
            '  "subtotal": "",\n'
            '  "tax": "",\n'
            '  "currency": "",\n'
            '  "category": "",\n'
            '  "payment_method": "",\n'
            '  "notes": "",\n'
            '  "tip_amount": "",\n'
            '  "fare_amount": "",\n'
            '  "mandatory_fee_amount": "",\n'
            '  "alcohol_amount": "",\n'
            '  "alcohol_tax_amount": "",\n'
            '  "non_business_amount": "",\n'
            '  "line_item_summary": [],\n'
            '  "document_label": "receipt|not_receipt|unclear",\n'
            '  "receipt_visibility": "full|partial|unclear",\n'
            '  "image_quality": "clear|unclear|poor",\n'
            '  "critical_elements_visible": true,\n'
            '  "missing_critical_elements": [],\n'
            '  "retake_required": false,\n'
            '  "retake_reason": "",\n'
            '  "reasoning_summary": "",\n'
            '  "confidence": 0.0,\n'
            '  "follow_up_questions": [],\n'
            '  "warnings": []\n'
            "}\n"
            "Rules:\n"
            "- First decide whether the image is actually a receipt.\n"
            "- If the image is not a receipt, set document_label to not_receipt and retake_required to true.\n"
            "- Use empty strings if a field is not visible.\n"
            "- If the receipt is cut off or only partly visible, set receipt_visibility to partial.\n"
            "- If you are unsure whether the receipt is complete, set receipt_visibility to unclear.\n"
            "- Set image_quality to poor if text is blurred, too dark, too small, or unreadable.\n"
            "- critical_elements_visible should be true only when vendor, date, and total are all visible enough to trust.\n"
            "- missing_critical_elements should list any of vendor, transaction_date, total that are not clearly readable.\n"
            "- If the image needs to be taken again because it is not a receipt, is blurry, or cuts off critical elements, set retake_required to true and explain why in retake_reason.\n"
            "- Normalize transaction_date to YYYY-MM-DD when possible.\n"
            "- Keep total/subtotal/tax as strings formatted like 12.34.\n"
            "- When visible, also extract policy-relevant semantic amounts such as tip, fare, mandatory fees, alcohol, alcohol tax, or other clearly non-business charges.\n"
            "- For restaurant receipts, inspect any tip/gratuity section, signed merchant copy, checked box, written custom tip, circled option, or handwritten mark near a suggested tip.\n"
            "- A slash, X, check mark, scribble, or handwritten mark inside a checkbox or directly on a suggested-tip row counts as a selected tip option. A signature by itself does not select a tip.\n"
            "- Restaurant card receipts often show an earlier pre-tip authorization total, then a separate tip section with rows like 'Tip: 7.53, Total: 57.73'. If one of those rows is selected, the selected row is the final paid total.\n"
            "- If a tip option or custom tip is visibly selected, set tip_amount to the selected tip and set total to the final total associated with that selected tip, not the earlier pre-tip card authorization amount.\n"
            "- Do not call this a form error just because the selected final total differs from the earlier card authorization total; that difference is expected when a tip is selected.\n"
            "- If a receipt shows suggested tip choices but none is visibly selected, leave tip_amount blank and use the printed receipt total.\n"
            "- Do not treat unselected suggested tip choices as paid amounts.\n"
            "- If alcohol appears as a line item, set alcohol_amount to the visible alcohol line total. Only set alcohol_tax_amount when the receipt separately itemizes alcohol tax; otherwise leave it blank.\n"
            "- The requested field list comes from the target form, so prioritize what the form actually needs.\n"
            "- If a semantic amount is not visible, return an empty string for that field.\n"
            "- line_item_summary should be short phrases that describe visible line items or charges.\n"
            "- Set category to one of Meals, Travel, Lodging, or Other.\n"
            "- Use semantic understanding across languages and line items when assigning category.\n"
            "- Coffee, cafe, restaurant, beverage, or Food & Beverage receipts should map to Meals.\n"
            "- reasoning_summary must be a short, plain-English sentence.\n"
            "- warnings should mention uncertainty or ambiguous fields.\n"
            "- Output valid JSON with no markdown fences."
        )

        # JSON mode keeps the parser simple where the provider supports it.
        # Thinking models may need a little more budget and reject the
        # response_format flag, so that compatibility check happens below.
        payload = {
            "model": self.settings.receipt_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 900,
            "temperature": 0.1,
        }
        if supports_structured_outputs(self.settings.receipt_model):
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.settings.hf_api_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.settings.hf_timeout_seconds) as client:
            response = await client.post(self.settings.hf_router_url, headers=headers, json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(self._format_hf_error(response)) from exc
            body = response.json()

        message_content = body["choices"][0]["message"]["content"]
        if isinstance(message_content, list):
            response_text = "\n".join(
                item.get("text", "") for item in message_content if isinstance(item, dict)
            )
        else:
            response_text = str(message_content)

        parsed = extract_json_object(response_text)
        if parsed is None:
            raise ValueError("Model response did not contain valid JSON.")

        # Keep post-processing intentionally light. Qwen does the reading and
        # semantic extraction; code only adapts values to the app schema.
        fields = ExpenseFields(
            vendor=parsed.get("vendor", "").strip(),
            transaction_date=normalize_date(parsed.get("transaction_date", "")),
            total=normalize_amount(parsed.get("total", "")),
            subtotal=normalize_amount(parsed.get("subtotal", "")),
            tax=normalize_amount(parsed.get("tax", "")),
            currency=normalize_currency_code(parsed.get("currency", ""), self.settings.demo_currency),
            category=normalize_expense_category(
                parsed.get("category", "")
            ),
            payment_method=parsed.get("payment_method", "").strip(),
            notes=parsed.get("notes", "").strip(),
        )
        if not fields.category:
            fields.category = "Other"
        assessment = self._normalize_capture_assessment(parsed, fields)

        # Store both user-facing fields and policy-sensitive semantic amounts in
        # one payload so the agent can compute the claim before form filling.
        return ExtractionPayload(
            fields=fields,
            reasoning_summary=parsed.get("reasoning_summary", "").strip()
            or "Qwen3-VL extracted the key receipt fields from the image.",
            raw_text="",
            source="huggingface_qwen3_vl",
            document_label=assessment["document_label"],
            receipt_visibility=assessment["receipt_visibility"],
            image_quality=assessment["image_quality"],
            critical_elements_visible=assessment["critical_elements_visible"],
            missing_critical_elements=assessment["missing_critical_elements"],
            retake_required=assessment["retake_required"],
            retake_reason=assessment["retake_reason"],
            confidence=float(parsed.get("confidence", 0.75) or 0.75),
            semantic_amounts={
                "tip_amount": normalize_amount(parsed.get("tip_amount", "")),
                "fare_amount": normalize_amount(parsed.get("fare_amount", "")),
                "mandatory_fee_amount": normalize_amount(parsed.get("mandatory_fee_amount", "")),
                "alcohol_amount": normalize_amount(parsed.get("alcohol_amount", "")),
                "alcohol_tax_amount": normalize_amount(parsed.get("alcohol_tax_amount", "")),
                "non_business_amount": normalize_amount(parsed.get("non_business_amount", "")),
            },
            line_item_summary=[
                str(item).strip() for item in parsed.get("line_item_summary", []) if str(item).strip()
            ],
            warnings=[str(item).strip() for item in parsed.get("warnings", []) if str(item).strip()],
            follow_up_questions=[
                str(item).strip() for item in parsed.get("follow_up_questions", []) if str(item).strip()
            ],
        )

    def _encode_data_url(self, image_path: Path) -> str:
        """Embed the receipt image in the chat-completions image_url format."""
        mime = "image/jpeg"
        raw = image_path.read_bytes()
        encoded = base64.b64encode(raw).decode("utf-8")
        return f"data:{mime};base64,{encoded}"

    def _normalize_capture_assessment(self, parsed: dict, fields: ExpenseFields) -> dict[str, object]:
        """Validate Qwen's capture-gate fields against the extracted facts."""
        document_label = str(parsed.get("document_label", "unclear")).strip().lower()
        if document_label not in {"receipt", "not_receipt", "unclear"}:
            document_label = "unclear"

        receipt_visibility = str(parsed.get("receipt_visibility", "unclear")).strip().lower()
        if receipt_visibility not in {"full", "partial", "unclear"}:
            receipt_visibility = "unclear"

        image_quality = str(parsed.get("image_quality", "unclear")).strip().lower()
        if image_quality not in {"clear", "unclear", "poor"}:
            image_quality = "unclear"

        raw_missing = parsed.get("missing_critical_elements", [])
        if isinstance(raw_missing, str):
            raw_missing = [raw_missing]
        if not isinstance(raw_missing, list):
            raw_missing = []
        missing_critical_elements = [str(item).strip() for item in raw_missing if str(item).strip()]
        actual_missing = [
            field_name
            for field_name, value in (
                ("vendor", fields.vendor),
                ("transaction_date", fields.transaction_date),
                ("total", fields.total),
            )
            if not value
        ]
        # If Qwen says a critical field is visible but the extracted value is
        # blank, prefer the concrete missing value and ask for recapture.
        for field_name in actual_missing:
            if field_name not in missing_critical_elements:
                missing_critical_elements.append(field_name)

        critical_elements_visible = self._coerce_bool(parsed.get("critical_elements_visible", False))
        if not missing_critical_elements and receipt_visibility in {"full", "unclear"}:
            critical_elements_visible = True
        if missing_critical_elements:
            critical_elements_visible = False

        parsed_reason = str(parsed.get("retake_reason", "")).strip()
        retake_signal = self._coerce_bool(parsed.get("retake_required", False))
        retake_reason = self._compose_retake_reason(
            document_label=document_label,
            image_quality=image_quality,
            receipt_visibility=receipt_visibility,
            missing_critical_elements=missing_critical_elements,
            parsed_reason=parsed_reason,
        )
        retake_required = self._should_require_retake(
            document_label=document_label,
            image_quality=image_quality,
            receipt_visibility=receipt_visibility,
            critical_elements_visible=critical_elements_visible,
            missing_critical_elements=missing_critical_elements,
            retake_signal=retake_signal,
        )
        if not retake_required:
            retake_reason = ""

        return {
            "document_label": document_label,
            "receipt_visibility": receipt_visibility,
            "image_quality": image_quality,
            "critical_elements_visible": critical_elements_visible,
            "missing_critical_elements": missing_critical_elements,
            "retake_required": retake_required,
            "retake_reason": retake_reason,
        }

    def _should_require_retake(
        self,
        *,
        document_label: str,
        image_quality: str,
        receipt_visibility: str,
        critical_elements_visible: bool,
        missing_critical_elements: list[str],
        retake_signal: bool = False,
    ) -> bool:
        """Decide whether extraction evidence is trustworthy enough to continue."""
        if document_label != "receipt":
            return True
        if receipt_visibility == "partial":
            return True
        if missing_critical_elements or not critical_elements_visible:
            return True
        if image_quality == "poor" and retake_signal and receipt_visibility != "full":
            return True
        return False

    def _compose_retake_reason(
        self,
        *,
        document_label: str,
        image_quality: str,
        receipt_visibility: str,
        missing_critical_elements: list[str],
        parsed_reason: str,
    ) -> str:
        """Create the short user-facing reason shown when recapture is needed."""
        reasons: list[str] = []
        if document_label == "not_receipt":
            reasons.append("The image does not appear to show a receipt.")
        elif document_label == "unclear":
            reasons.append("The app is not confident that the image is actually a receipt.")

        if image_quality == "poor":
            reasons.append("The image is too blurry or low-quality to read reliably.")
        elif image_quality == "unclear":
            reasons.append("Some text is only partially readable.")

        if receipt_visibility != "full":
            reasons.append("Not all critical receipt elements are visible in the frame.")

        if missing_critical_elements:
            reasons.append(
                f"Critical elements are missing or unreadable: {', '.join(missing_critical_elements)}."
            )

        if parsed_reason:
            reasons.insert(0, parsed_reason)

        if not reasons:
            return ""

        unique_reasons = list(dict.fromkeys(reasons))
        return " ".join(unique_reasons)

    def _coerce_bool(self, value: object) -> bool:
        """Accept provider booleans that arrive as strings."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)
