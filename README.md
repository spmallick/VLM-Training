# Receipt-to-Expense Agent Demo

This repo now includes a local demo app that captures a receipt from your laptop camera, extracts expense fields with `Qwen3-VL` via the Hugging Face API, and then runs a local agent loop that fills a sandbox expense portal.

## What the app does

- captures a receipt image from the laptop camera or file upload
- stores a persistent session in local SQLite
- extracts `vendor`, `date`, `total`, `tax`, `currency`, `category`, and `notes`
- rejects images that do not appear to be receipts or are too incomplete to trust
- flags whether the receipt looks `full`, `partial`, or `unclear`
- lets you review the fields before running the agent
- uses a local JSON-backed currency-converter tool to estimate USD totals when needed
- runs a semantic policy review that can `submit`, `ask_user`, or `hold`
- animates a local sandbox expense portal as the agent fills and, when allowed, submits it
- keeps a live event log that mirrors `Observe -> Reason -> Decide -> Act -> Remember`

## Run it

1. Activate the existing virtual environment:

   ```bash
   source .venv/bin/activate
   ```

2. Add a Hugging Face token to `.env`:

   ```bash
   cp .env.example .env
   ```

   Then set `HUGGINGFACEHUB_API_TOKEN=...`. The receipt extraction path requires Qwen3-VL.

3. Start the server:

   ```bash
   uvicorn app.main:app --reload
   ```

4. Open `http://127.0.0.1:8000`.

## Demo flow

1. Click `Capture from camera`.
2. Let the intake gate decide whether the image is actually a receipt and whether it is readable enough to continue.
3. If the app asks for a retake, capture the receipt again with the full page in frame.
4. Review the extracted expense fields.
5. Click `Run agent`.
6. Watch the agent normalize the data, call the currency tool when needed, and run a policy check.
7. Watch the sandbox expense portal populate field by field.
8. See whether the agent auto-submits, pauses for confirmation, or holds the receipt for review.
9. Optionally open the standalone portal view in a second window.

## Files

- `app/main.py`: FastAPI entrypoint and routes
- `app/vision.py`: Hugging Face Qwen3-VL receipt extraction
- `app/policy.py`: semantic policy review and fallback risk checks
- `app/tools.py`: local JSON-backed tools such as the currency converter
- `app/agent.py`: local agent loop and portal automation state
- `app/store.py`: SQLite-backed session and event persistence
- `app/templates/`: HTML templates
- `app/static/`: JS and CSS
