# SoberStack Receipt Run: Step-by-Step Output

This file is a transcript-style walkthrough of the Indian Sizzler receipt going through the local consultant app with the SoberStack Consulting portal selected.

It is meant to complement the main `README.md`. The main README explains how the app works. This file shows what one concrete run produces: the receipt extraction JSON, the policy review output, the computed reimbursement values, and the browser-agent event log.

## Run Context

- Company portal: `SoberStack Consulting`
- Example receipt: `app/data/example_receipts/indian_sizzler_receipt.png`
- Recorded session: `3a1d1d061ede`
- Final app status: `awaiting_confirmation`
- Final app step: `await_user`

The run stopped before submission because the policy reviewer returned `recommended_action: "ask_user"`. The agent still filled the SoberStack form with the computed claim values, but it did not submit because the policy decision required human confirmation.

## 1. Receipt Intake Starts

The user selects SoberStack Consulting and uploads the receipt. The app creates a session, stores the image, builds an extraction template from the selected portal, and asks Qwen to extract the fields needed for that portal.

Initial event output:

```text
Session created. Ready for receipt capture.

Consultant selected SoberStack Consulting as the reimbursement target.

Receipt image stored locally. Starting Qwen extraction.

Form-governed extraction: the selected portal requested vendor, transaction_date, total,
subtotal, tax, currency, category, payment_method, notes, tip_amount, fare_amount,
mandatory_fee_amount, alcohol_amount, alcohol_tax_amount, non_business_amount.
```

The template also adds semantic fields needed later in the agent loop:

```text
Template growth: added semantic fields tip_amount, fare_amount, mandatory_fee_amount,
alcohol_amount, alcohol_tax_amount, non_business_amount, claim_amount, adjustment_note,
employee_name, cost_center, receipt_upload, policy_acknowledgement, review_acknowledgement
for the selected company.
```

## 2. Qwen Receipt Extraction

Qwen reads the receipt image and returns this `ExtractionPayload`.

```json
{
  "fields": {
    "vendor": "Indian Sizzler",
    "transaction_date": "2026-05-10",
    "total": "50.20",
    "subtotal": "46.00",
    "tax": "4.20",
    "currency": "USD",
    "category": "Meals",
    "payment_method": "AmEx 3002 (Contactless)",
    "notes": ""
  },
  "reasoning_summary": "This is a clear, full receipt from Indian Sizzler showing vendor, date, total, and line items including alcohol; no tip was selected.",
  "raw_text": "",
  "source": "huggingface_qwen3_vl",
  "document_label": "receipt",
  "receipt_visibility": "full",
  "image_quality": "clear",
  "critical_elements_visible": true,
  "missing_critical_elements": [],
  "retake_required": false,
  "retake_reason": "",
  "confidence": 0.98,
  "semantic_amounts": {
    "tip_amount": "",
    "fare_amount": "",
    "mandatory_fee_amount": "",
    "alcohol_amount": "16.00",
    "alcohol_tax_amount": "",
    "non_business_amount": ""
  },
  "line_item_summary": [
    "House wine glass x 1: $16.00",
    "Grand Adult Buffet x 1: $30.00"
  ],
  "warnings": [],
  "follow_up_questions": []
}
```

Important extraction points:

- The model identifies this as a valid receipt.
- The vendor is `Indian Sizzler`.
- The date is normalized to `2026-05-10`.
- The receipt total is `50.20`.
- The subtotal is `46.00`.
- Sales tax is `4.20`.
- The model detects an alcohol line item: `House wine glass x 1: $16.00`.
- The model leaves `alcohol_tax_amount` blank because the receipt does not itemize alcohol tax separately.

The intake page displays this JSON directly in the `Extracted JSON` panel.

## 3. Working Memory Is Created

After extraction, the app compresses the receipt output into working memory. Working memory is what later steps use when filling the portal.

The event log records:

```text
Extraction completed using huggingface qwen3 vl.

Tool - agent state bootstrap
Loaded the extraction template, working memory, and reviewed receipt fields into the live agent loop.

Tool - merge_reviewed_fields
Working memory now carries 11 facts, 0 derived values, and the reviewed consultant edits
before the browser opens.
```

At this point, the app has receipt facts but has not yet computed the reimbursable claim.

## 4. Playwright Opens The SoberStack Portal

The browser automation phase starts.

```text
Step 2 - Live browser automation started
Target portal: SoberStack Consulting. The agent will validate policy, compute the claim,
inspect the live UI, discover the visible workflow step by step, map semantic fields to
visible controls, and decide whether submit is allowed.
```

The app explains the automation mode:

```text
Automation mode - browser control
Portal automation will send Qwen the end goal, the current screenshots, and the visible
controls, then execute the next actions Qwen chooses through Playwright. If Qwen cannot
return a usable plan, the run will stop instead of guessing.
```

Then Playwright opens the selected local portal:

```text
Tool - open_company_portal
Opened the live SoberStack Consulting portal in Google Chrome so the agent can inspect
website policy before computing the claim.
```

## 5. Playwright Reads The Live Portal Policy

SoberStack has a policy dialog. Playwright opens and reads it before claim math runs.

```text
Tool - read_portal_policy
Source: soberstack policy dialog. Discovered 574 characters of live policy text.
Policy preview: company policy soberstack consulting reimbursement policy close meals and
non-alcoholic beverages are reimbursable when tied to client work, travel, or approved
team events. alcohol is never reimbursable and must be excluded from the claim amount.
any tax attributable to alcohol must also be excluded from the reimbursable amount. if
the claimed amount is lower than the receipt total, the employee must explain the
excluded charges in the adjustment note field. receipts must include vendor, date, total,
and itemized line items; incomplete receipts require manual review.
```

The important policy rules for this receipt are:

- Alcohol is not reimbursable.
- Tax attributable to alcohol is not reimbursable.
- If the claim is lower than the receipt total, the adjustment explanation must be filled.

## 6. Qwen Policy Review

Qwen reviews the receipt evidence and returns a policy-risk JSON object.

```json
{
  "risk_level": "medium",
  "recommended_action": "ask_user",
  "confidence": 0.95,
  "receipt_visibility": "full",
  "policy_summary": "Receipt contains alcohol (House wine glass $16.00), which may be non-reimbursable depending on company policy. Tip was not selected, but the presence of alcohol requires policy validation before submission.",
  "missing_fields": [],
  "warnings": [],
  "issues": [
    {
      "label": "Alcohol Purchase",
      "evidence": "Line item 'House wine glass x 1: $16.00' indicates purchase of alcoholic beverage.",
      "severity": "medium"
    }
  ]
}
```

Event output:

```text
Tool - review_receipt_policy
Engine: Hugging Face VLM (Qwen/Qwen3-VL-235B-A22B-Instruct:novita).
Result: ask_user. Risk: medium. Confidence: 0.95.
Summary: Receipt contains alcohol (House wine glass $16.00), which may be
non-reimbursable depending on company policy. Tip was not selected, but the presence of
alcohol requires policy validation before submission.

Decision - policy issues
- Alcohol Purchase (medium): Line item 'House wine glass x 1: $16.00' indicates purchase
  of alcoholic beverage.
```

This policy-review result is why the run later pauses instead of submitting automatically.

## 7. Deterministic Claim Math

The app computes the reimbursable amount in Python. Qwen identifies the alcohol line item, but code owns the arithmetic.

Inputs:

```text
receipt_total = 50.20
subtotal = 46.00
tax = 4.20
alcohol_amount = 16.00
alcohol_tax_amount = blank
```

Because SoberStack excludes alcohol and alcohol tax, and the receipt does not itemize alcohol tax, the app prorates alcohol tax:

```text
alcohol_tax = 16.00 / 46.00 * 4.20 = 1.46
excluded_amount = 16.00 + 1.46 = 17.46
claim_amount = 50.20 - 17.46 = 32.74
```

Derived values stored in working memory:

```json
{
  "claim_amount_local": {
    "value": "32.74",
    "source": "derived",
    "confidence": 0.95,
    "notes": "Computed in USD after applying company policy."
  },
  "claim_amount": {
    "value": "32.74",
    "source": "derived",
    "confidence": 0.95,
    "notes": "Semantic alias for the amount that should be written into the portal claim field."
  },
  "claim_amount_usd": {
    "value": "32.74",
    "source": "derived",
    "confidence": 0.95,
    "notes": "Converted into USD using the local FX table."
  },
  "excluded_amount_local": {
    "value": "17.46",
    "source": "derived",
    "confidence": 0.95,
    "notes": "The amount removed from the original receipt total."
  },
  "adjustment_note": {
    "value": "Excluded 17.46 in policy-sensitive charges (alcohol, alcohol tax) before writing the claim.",
    "source": "agent",
    "confidence": 0.9,
    "notes": "Generated explanation for reduced claims."
  }
}
```

Event output:

```text
Tool - compute_reimbursable_amount
Computed a claim of 32.74 USD from the live website policy result and stored the derived
values back into working memory.
```

## 8. Qwen Inspects The Portal Form

The navigation model receives screenshots plus a catalog of visible controls and buttons. It chooses the next actions, but it does not execute them.

Event output:

```text
Tool - inspect_form_ui
Page: current_portal_step. Visible controls: 9. Action buttons: 3.
Inspection mode: Qwen goal-directed next-action planning.
Semantic targets for this step: expense_category, vendor, expense_date, employee_name,
receipt_total, claim_amount, business_purpose, adjustment_note, receipt.
Vision input: Qwen reviewed 3 scrolled screenshots of the page.
Matches: expense_category: select -> Expense type Choose one Food Lodging Travel Other
(claim_bucket) = Food, vendor: fill -> Vendor name (merchant_name) = Indian Sizzler,
expense_date: fill -> Expense date (service_day) = 2026-05-10, employee_name: fill ->
Employee name (claimer_identity) = Casey Consultant, receipt_total: fill -> Receipt total
(receipt_gross) = 50.20, claim_amount: fill -> Amount requested
(requested_reimbursement) = 32.74.
```

Qwen's page-level reasoning:

```text
VLM response - portal inspection
Page: current_portal_step.
Screenshots reviewed: 3.
Summary: Expense report intake form with fields to fill and submit button visible.
Reasoning: Form fields are empty and need to be filled with goal values before submission.
```

## 9. Playwright Executes The Browser Actions

Playwright executes the action plan against the live portal.

```text
Tool - browser_action
Page: current_portal_step.
Filled semantic fields: vendor, expense_date, employee_name, receipt_total,
claim_amount, business_purpose, adjustment_note.
Dropdown selections: expense_category -> Food.
Uploaded the receipt file to the portal.
Executed Qwen actions: select Expense type Choose one Food Lodging Travel Other
(claim_bucket) = Food, fill Vendor name (merchant_name) = Indian Sizzler, fill Expense
date (service_day) = 2026-05-10, fill Employee name (claimer_identity) = Casey
Consultant, fill Receipt total (receipt_gross) = 50.20, fill Amount requested
(requested_reimbursement) = 32.74, fill Business reason (client_story) = Captured locally
and reviewed by the receipt agent., fill Adjustment explanation (variance_note) =
Excluded 17.46 in policy-sensitive charges (alcohol, alcohol tax) before writing the
claim..
```

This is the point where the SoberStack form has the corrected reimbursable claim amount, not the full receipt total.

## 10. Qwen Checks The Filled Form

The agent asks Qwen to inspect the page again.

```text
Tool - inspect_form_ui
Page: current_portal_step. Visible controls: 9. Action buttons: 3.
Inspection mode: Qwen goal-directed next-action planning.
Semantic targets for this step: .
Vision input: Qwen reviewed 3 scrolled screenshots of the page.
Visible control preview: Expense type Choose one Food Lodging Travel Other
(claim_bucket), Vendor name (merchant_name), Expense date (service_day), Employee name
(claimer_identity).
```

Qwen sees the form as ready:

```text
VLM response - portal inspection
Page: current_portal_step.
Screenshots reviewed: 3.
Summary: Expense report form with all fields filled, receipt uploaded, and submission
button visible.
Reasoning: All required fields are filled, receipt uploaded, and submission button is
visible. Ready to submit.
```

## 11. The Agent Pauses For Human Confirmation

Even though the visible form is ready, the earlier policy review recommended `ask_user`. The agent therefore does not submit automatically.

Final event output:

```text
Tool - browser_action
Page: current_portal_step.
Act: observed the current_portal_step step, but some required values are still missing
from memory.

Decision - human confirmation required
The agent filled the known fields but stopped before submit because policy confidence was
not high enough.
```

Final session state:

```text
status = awaiting_confirmation
current_step = await_user
```

## Summary Of The SoberStack Run

| Stage | Owner | Output |
| --- | --- | --- |
| Receipt upload | FastAPI + SQLite | Session created and receipt image stored |
| Receipt extraction | Qwen receipt model | Vendor/date/totals/items extracted as JSON |
| Working memory | Python | Receipt fields converted into semantic facts |
| Portal open | Playwright | SoberStack portal opened in browser |
| Policy read | Playwright | Live policy text read from the policy dialog |
| Policy review | Qwen policy model | Alcohol issue found, `ask_user` recommended |
| Claim math | Python | Claim reduced from `50.20` to `32.74` |
| Page inspection | Qwen navigation model | Visible controls mapped to semantic fields |
| Browser actions | Playwright | Form filled and receipt uploaded |
| Final decision | Python guardrail | Paused for human confirmation before submit |

The key lesson is that the VLM does not run the whole workflow. Qwen reads the receipt and interprets screenshots. Python owns state and money math. Playwright owns browser execution. The agent pauses when the policy-review result says the situation needs a human decision.
