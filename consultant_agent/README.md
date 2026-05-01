# Consultant Desktop App

This folder contains the consultant-side intake app.

The architecture is intentionally split:

- The consultant app is a pure Python desktop tool built with OpenCV HighGUI.
- The company portals stay on the local web server as plain external websites.
- The agent-side extraction template, working memory, and policy math live on the consultant side, not inside the portals.

## Run The Company Portals

```bash
source ~/.venv/codex/bin/activate
cd /Users/spmallick/github/VLM-Training
python -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

## Run The Consultant App

```bash
source ~/.venv/codex/bin/activate
cd /Users/spmallick/github/VLM-Training
python consultant_agent/highgui_app.py --base-url http://127.0.0.1:8011
```

## Controls

- `1`, `2`, `3`: select the target company portal
- `c`: start or stop the camera
- `space`: capture a receipt photo from the live camera
- `u`: load a receipt image from disk
- `a`: run blur detection, extraction, and working-memory generation
- `g`: run intake and open the selected company portal if the receipt is good enough
- `r`: clear the current receipt
- `q` or `esc`: quit

## Output

Each intake run is saved under `output/consultant_agent_runs/<timestamp>/` with:

- `receipt.<ext>`
- `intake_result.json`
- `working_memory.json`
- `tool_catalog.json`

This makes it easy to inspect what the consultant-side agent extracted before browser automation takes over.
