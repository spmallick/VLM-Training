# Receipt-to-Expense Agent Demo

This repository contains a local demo app that turns a receipt image into a submitted reimbursement claim. It is intentionally built as a small end-to-end agent system rather than a static form demo: a vision-language model reads the receipt, deterministic code computes policy-sensitive amounts, Playwright opens and operates a live sandbox portal, and another model call interprets the current browser page from screenshots.

The app is useful for teaching how vision-language models fit into a larger workflow. Qwen is not asked to do everything. It reads images and plans browser actions where visual interpretation is valuable. Python code handles state, math, policy application, persistence, validation, and browser execution.

## What You See

Open the app and choose a company portal. Then upload or capture a receipt photo. The first page now includes a running commentary panel underneath the upload area. It shows the intake events and, after extraction finishes, the exact JSON payload produced by Qwen for the receipt.

After the receipt is accepted, you can launch the agent. A controlled browser opens the selected reimbursement portal and the app shows the agent loop as it reads policy text, computes the claim amount, inspects the visible form, fills fields, uploads the receipt, and either submits or pauses for human confirmation.

## Quick Start

Clone the repository, enter the project directory, and activate a Python virtual environment:

```bash
git clone https://github.com/spmallick/VLM-Training.git
cd VLM-Training
python -m venv .venv
source .venv/bin/activate
```

Install dependencies if needed:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Create a local `.env`:

```bash
cp .env.example .env
```

Edit `.env` and set:

```bash
HUGGINGFACEHUB_API_TOKEN=hf_your_token_here
```

Start the app:

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Model Configuration

The app supports separate model roles. This matters because the expensive model is needed for nuanced receipt understanding, but it is wasteful to use it for every browser-navigation step.

Example `.env`:

```bash
HUGGINGFACEHUB_API_TOKEN=hf_your_token_here
HF_MODEL=Qwen/Qwen3-VL-8B-Instruct:novita
HF_RECEIPT_MODEL=Qwen/Qwen3-VL-235B-A22B-Instruct:novita
HF_POLICY_MODEL=Qwen/Qwen3-VL-235B-A22B-Instruct:novita
HF_NAVIGATION_MODEL=Qwen/Qwen3-VL-8B-Instruct:novita
HF_TIMEOUT_SECONDS=180
REQUIRE_QWEN3_VL=true
BROWSER_HEADLESS=false
```

The role split is:

- `HF_RECEIPT_MODEL`: reads the receipt image and extracts structured JSON.
- `HF_POLICY_MODEL`: reviews the receipt evidence for risk and ambiguity.
- `HF_NAVIGATION_MODEL`: looks at portal screenshots and chooses the next browser actions.
- `HF_MODEL`: fallback used when a role-specific model is not set.

For the demo receipt with a signed restaurant tip section, the 8B model missed the selected tip. The 235B Instruct model handled the extraction correctly with the prompt rules in `app/vision.py`. Browser navigation uses 8B because it is called repeatedly and only needs to map visible controls to known goal values.

Do not commit a real `.env` token. `.env` is ignored by Git; `.env.example` is the safe template.

## Demo Flow

1. Choose a company portal on the first page.
2. Upload a receipt image or use the camera capture.
3. Watch the intake commentary as the app stores the image, builds the extraction template, and calls Qwen.
4. Inspect the extracted JSON in the commentary panel.
5. Launch the agent.
6. The agent opens the selected portal with Playwright.
7. It reads any visible policy text from the portal.
8. It computes the reimbursable claim amount.
9. Qwen reviews screenshots of the live portal and returns a compact action plan.
10. Playwright executes the planned browser actions.
11. The loop repeats until the form is submitted, paused for confirmation, or held for review.

## How The Agent Is Divided

The app deliberately separates model work from deterministic work.

Qwen handles perception and semantic interpretation:

- receipt image understanding
- receipt completeness and quality checks
- extracting vendor, date, totals, currency, line items, and policy-sensitive semantic amounts
- detecting values such as tip, alcohol, fare, mandatory fee, or non-business charges
- reviewing receipt evidence for ambiguity
- interpreting screenshots of the current portal page
- selecting the next browser actions from a visible control catalog

Python handles state and business logic:

- FastAPI routes
- SQLite session storage
- receipt upload persistence
- extraction templates and working memory
- field normalization
- deterministic currency conversion
- deterministic reimbursement math
- prorating alcohol tax when the receipt identifies alcohol but does not itemize alcohol tax
- status transitions and event logging
- serving the agent UI and sandbox portals

Playwright handles browser execution:

- launching a controlled browser
- opening the selected company portal
- reading visible controls and buttons from the DOM
- capturing screenshots for Qwen
- filling text inputs
- selecting dropdown values
- checking boxes
- uploading the receipt file
- clicking navigation and submit buttons
- reading the final thank-you URL or response payload

The important boundary is that Qwen may decide what the next action should be, but Playwright performs the action. The app validates Qwen's action plan against the visible controls before executing it.

## Receipt Extraction

Receipt extraction is implemented in `app/vision.py`.

The extraction prompt asks Qwen to return JSON with:

- `vendor`
- `transaction_date`
- `total`
- `subtotal`
- `tax`
- `currency`
- `category`
- `payment_method`
- `notes`
- semantic amounts such as `tip_amount`, `alcohol_amount`, and `alcohol_tax_amount`
- line-item summary
- capture quality and retake fields

The prompt includes restaurant-specific instructions because tip receipts are subtle. A restaurant card receipt can show a pre-tip authorization total and then a separate tip section. If a suggested tip row is visibly selected, the app wants the final total associated with that selected row, not the earlier pre-tip card authorization amount.

The code does not hard-code a particular restaurant or amount. It gives Qwen general rules for restaurant receipts:

- inspect the tip/gratuity section
- treat a mark inside a checkbox or directly on a suggested-tip row as selected
- do not treat a signature by itself as selecting a tip
- use the selected row's final total when a tip is selected
- do not treat unselected tip suggestions as paid amounts
- extract visible alcohol line totals
- leave `alcohol_tax_amount` blank unless the receipt itemizes it separately

The first-page commentary renders the resulting `ExtractionPayload` JSON directly so the demo audience can see exactly what the model returned.

## Policy And Claim Math

The sandbox portals contain company-specific policy text under `app/portal_site/policies/`.

Before computing the claim, the agent opens the live portal and reads the policy text through Playwright. The reimbursement computation in `app/agent_memory.py` uses the policy text and the extracted receipt facts to compute:

- claim amount in local currency
- claim amount in USD
- excluded amount
- adjustment explanation

Some policy math is deterministic by design. For example, if policy says alcohol and alcohol tax are not reimbursable, and Qwen extracts `alcohol_amount` but the receipt does not itemize alcohol tax, the app prorates alcohol tax from the receipt subtotal and tax. This avoids asking the model to do arithmetic that should be owned by code.

For example, if alcohol is `$16.00`, subtotal is `$46.00`, and tax is `$4.20`, the app estimates:

```text
alcohol tax = 16.00 / 46.00 * 4.20 = 1.46
```

Then it excludes `$17.46` for companies that do not reimburse alcohol or alcohol tax.

## Browser Planning

Browser planning is implemented in `app/portal_automation.py`.

Each planning step does this:

1. Inspect visible controls and buttons with Playwright.
2. Capture one or more screenshots of the current page.
3. Build a goal-values object from working memory.
4. Send screenshots, controls, buttons, and goal values to Qwen.
5. Ask Qwen for a compact JSON action plan.
6. Validate that each action points to an actual visible control or button.
7. Execute the actions with Playwright.

The planner prompt uses a fixed action vocabulary:

```text
fill
select
upload
check
click
submit
done
```

The model does not get to run arbitrary browser code. It only chooses actions from that vocabulary, and the runner maps them to Playwright calls.

Small models can sometimes start valid JSON and then become too verbose, causing truncated output. The navigation planner now asks for compact JSON, increases the response budget, and retries once with a stricter prompt if the first response is not parseable.

## Sandbox Portals

The app includes three local target portals:

- `soberstack`: a simple policy-popup and one-page form.
- `stingy`: a multi-step reimbursement workflow with review and acknowledgement.
- `china`: a Chinese-language reimbursement form.

These are served by `app/portal_routes.py` and templates in `app/portal_site/templates/`.

The target portals are intentionally separate from the agent UI. The agent is not calling internal Python functions to submit a claim. It opens the portal as a browser user would and fills the visible website. This makes the demo closer to real browser automation.

## Running Tests

Run the focused app tests:

```bash
python -m pytest tests/test_app_core.py tests/test_portal_automation.py -q
```

Check the JavaScript syntax:

```bash
node --check app/static/app.js
```

Run a local smoke test against a receipt image:

```bash
python tests/smoke/run_receipt_agent_smoke.py \
  --receipt app/data/example_receipts/indian_sizzler_receipt.png \
  --companies soberstack \
  --start-server \
  --timeout-seconds 240 \
  --delay-ms 0
```

The smoke test starts the server if requested, runs the same extraction and browser loop as the UI, and writes a summary under `output/cli_smoke_runs/`.

## File Structure

```text
app/
  main.py                         FastAPI app setup, shared services, routers
  config.py                       Settings, environment variables, model-role split
  agent_routes.py                 Agent control UI routes and session APIs
  portal_routes.py                Sandbox portal routes
  vision.py                       Qwen receipt extraction
  policy.py                       Qwen receipt risk review
  agent.py                        Agent orchestration loop
  agent_memory.py                 Extraction templates, working memory, claim math
  portal_automation.py            Playwright browser runner and Qwen page planner
  store.py                        SQLite session/event persistence
  tools.py                        Tool catalog and currency converter
  schemas.py                      Pydantic data models
  templates/
    index.html                    Upload/intake UI
    portal.html                   Standalone session mirror
  static/
    app.js                        Browser-side upload, polling, commentary UI
    styles.css                    App and portal styling
  portal_site/
    company_portals.py            Portal registry and policy loading
    policies/                     Company policy markdown
    templates/                    Sandbox company portal HTML
  data/
    currency_rates.json           Local demo FX table
    example_receipts/             Committed receipt images for demos and smoke tests

tests/
  test_app_core.py                Core normalization, memory, reimbursement tests
  test_portal_automation.py       Browser planner and action validation tests
  test_main_routes.py             Route-level tests
  smoke/run_receipt_agent_smoke.py End-to-end smoke runner

requirements.txt                  Python dependencies
.env.example                      Safe local configuration template
```

## Data Flow

The main data object passed through the system is the session snapshot.

The upload route creates a session in SQLite. After Qwen extraction, the session contains:

- `receipt_image_path`
- `extraction`
- `extraction_template`
- `working_memory`
- `reviewed_fields`
- `events`

When the agent runs, it adds:

- `policy_review`
- derived reimbursement values in working memory
- portal state
- browser action events
- submission details when successful

The UI polls `/api/sessions/{session_id}` and renders the latest state. The first page uses that polling to keep the commentary and extracted JSON current.

## Design Notes

This app uses Qwen where model perception is useful, but it avoids turning the whole program into a single prompt. That is the main engineering lesson:

- use the VLM to read images and interpret screens
- use code to keep state and compute money
- use Playwright to execute browser actions
- validate model outputs before doing anything externally visible
- keep a visible event log so the user can audit what happened

The result is slower than a hard-coded form filler but much more instructive: the same agent can adapt to different portal layouts, languages, and policy surfaces while still keeping math and execution under deterministic control.
