# Google Drive API Asset Upload

This is the API-based path for updating the notebook asset folder. It does not
use browser UI automation. The only browser step is Google's OAuth consent flow
when a fresh local token is needed.

## Target Folder

The Qwen3-VL notebook asset folder is:

```text
https://drive.google.com/drive/folders/1geZSX49w_sY4gAhasOyYhxN_PXqbAt8q
```

Folder ID:

```text
1geZSX49w_sY4gAhasOyYhxN_PXqbAt8q
```

## One-Time Setup

1. In Google Cloud, enable the Google Drive API.
2. Create an OAuth client with application type `Desktop app`.
3. Download the client JSON.
4. Save it here:

```bash
/Users/spmallick/github/VLM-Training/secrets/google_drive_credentials.json
```

5. Install the optional Drive API dependencies:

```bash
cd /Users/spmallick/github/VLM-Training
./.venv/bin/python -m pip install -r requirements-google-drive.txt
```

The first successful run creates:

```bash
/Users/spmallick/github/VLM-Training/secrets/google_drive_token.json
```

Both files are ignored by `.gitignore`.

## Verify Access

```bash
cd /Users/spmallick/github/VLM-Training
./.venv/bin/python scripts/upload_drive_asset.py --check-only
```

## Upload Or Update The Score-Card Image

```bash
cd /Users/spmallick/github/VLM-Training
./.venv/bin/python scripts/upload_drive_asset.py
```

By default this uploads:

```bash
/Users/spmallick/github/VLM-Training/output/jupyter-notebook/real_world_tour_assets/visual_score_card.png
```

as:

```text
visual_score_card.png
```

If a file with that name already exists in the Drive folder, the script updates
that file instead of creating a duplicate.

## Upload A Different File

```bash
./.venv/bin/python scripts/upload_drive_asset.py \
  --file /path/to/asset.png \
  --name asset.png
```
