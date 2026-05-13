from __future__ import annotations

import base64
from pathlib import Path

import httpx

from .config import Settings
from .schemas import ExpenseFields, ExtractionPayload, PolicyIssue, PolicyReview
from .vision import extract_json_object, supports_structured_outputs


class PolicyReviewService:
    """Qwen-based receipt risk reviewer.

    This service does not load company policy. Playwright discovers live portal
    policy before claim math runs; this review only checks whether the receipt
    evidence is safe enough for the agent to keep going.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    async def review_receipt(
        self,
        image_path: Path,
        extraction: ExtractionPayload | None,
        reviewed_fields: ExpenseFields,
    ) -> PolicyReview:
        if not self.settings.hf_api_token:
            raise RuntimeError("Qwen3-VL policy review requires a Hugging Face token.")
        try:
            return await self._review_with_hugging_face(image_path, extraction, reviewed_fields)
        except Exception as exc:
            raise RuntimeError(f"Qwen3-VL policy review failed: {exc}") from exc

    async def _review_with_hugging_face(
        self,
        image_path: Path,
        extraction: ExtractionPayload | None,
        reviewed_fields: ExpenseFields,
    ) -> PolicyReview:
        data_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(image_path.read_bytes()).decode("utf-8")
        )
        extraction_json = extraction.model_dump_json(indent=2) if extraction else "{}"
        reviewed_json = reviewed_fields.model_dump_json(indent=2)
        # Qwen sees the receipt plus the already-extracted fields so it can
        # judge visibility, missing evidence, and ambiguous items semantically.
        prompt = (
            "You are the policy reviewer inside a local receipt-filing agent.\n"
            "Assess whether this receipt should be automatically submitted, should ask the user for confirmation, or should be held for manual review.\n"
            "Be generic and semantic. Do not rely on a hand-written keyword list. You may infer policy-sensitive content such as alcohol, stale receipts, partial visibility, missing totals, or ambiguous evidence from the receipt image and its extracted data.\n"
            "Return JSON only with this schema:\n"
            "{\n"
            '  "risk_level": "low|medium|high",\n'
            '  "recommended_action": "submit|ask_user|hold",\n'
            '  "confidence": 0.0,\n'
            '  "receipt_visibility": "full|partial|unclear",\n'
            '  "policy_summary": "",\n'
            '  "missing_fields": [],\n'
            '  "warnings": [],\n'
            '  "issues": [\n'
            '    {"label": "", "evidence": "", "severity": "low|medium|high"}\n'
            "  ]\n"
            "}\n"
            "If the receipt is incomplete or only partially visible, say so explicitly.\n"
            "If policy-sensitive or non-reimbursable items may be present, explain the evidence.\n"
            "If the evidence is uncertain, prefer ask_user over hold unless critical information is missing.\n"
            f"Extraction:\n{extraction_json}\n"
            f"Reviewed fields:\n{reviewed_json}\n"
        )

        payload = {
            "model": self.settings.policy_model,
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
        # JSON mode keeps this path aligned with receipt extraction and page
        # planning where supported. Qwen3-VL Thinking providers can reject that
        # flag, so we fall back to prompt-constrained JSON and parse the object.
        if supports_structured_outputs(self.settings.policy_model):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.hf_api_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.hf_timeout_seconds) as client:
            response = await client.post(self.settings.hf_router_url, headers=headers, json=payload)
            response.raise_for_status()
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
            raise ValueError("Policy review response did not contain valid JSON.")

        # Keep the app boundary typed even though the model response is JSON.
        issues = [
            PolicyIssue(
                label=str(item.get("label", "")).strip(),
                evidence=str(item.get("evidence", "")).strip(),
                severity=str(item.get("severity", "medium")).strip() or "medium",
            )
            for item in parsed.get("issues", [])
            if isinstance(item, dict) and str(item.get("label", "")).strip()
        ]

        return PolicyReview(
            risk_level=parsed.get("risk_level", "medium"),
            recommended_action=parsed.get("recommended_action", "ask_user"),
            confidence=float(parsed.get("confidence", 0.65) or 0.65),
            receipt_visibility=parsed.get(
                "receipt_visibility",
                extraction.receipt_visibility if extraction else "unclear",
            ),
            policy_summary=parsed.get("policy_summary", "").strip()
            or "The policy reviewer assessed the receipt before deciding whether to submit.",
            missing_fields=[str(item).strip() for item in parsed.get("missing_fields", []) if str(item).strip()],
            warnings=[str(item).strip() for item in parsed.get("warnings", []) if str(item).strip()],
            issues=issues,
        )
