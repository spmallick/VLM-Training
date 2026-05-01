#!/usr/bin/env python3
"""Upload or update a local asset in the class Google Drive asset folder.

This uses the official Google Drive API with user OAuth. It intentionally avoids
browser UI automation; the browser is only opened by Google's OAuth flow when a
fresh token is needed.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS_FOLDER_ID = "1geZSX49w_sY4gAhasOyYhxN_PXqbAt8q"
DEFAULT_FILE = ROOT_DIR / "output" / "jupyter-notebook" / "real_world_tour_assets" / "visual_score_card.png"
DEFAULT_CREDENTIALS = ROOT_DIR / "secrets" / "google_drive_credentials.json"
DEFAULT_TOKEN = ROOT_DIR / "secrets" / "google_drive_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a local file to a Google Drive folder by folder ID. "
            "Defaults target the Qwen3-VL notebook asset folder."
        )
    )
    parser.add_argument(
        "--file",
        default=os.getenv("GOOGLE_DRIVE_UPLOAD_FILE", str(DEFAULT_FILE)),
        help="Local file to upload.",
    )
    parser.add_argument(
        "--folder-id",
        default=os.getenv("GOOGLE_DRIVE_ASSETS_FOLDER_ID", DEFAULT_ASSETS_FOLDER_ID),
        help="Google Drive folder ID to upload into.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Optional filename to use in Drive. Defaults to the local filename.",
    )
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_DRIVE_CREDENTIALS_JSON", str(DEFAULT_CREDENTIALS)),
        help="OAuth desktop-app credentials JSON downloaded from Google Cloud.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GOOGLE_DRIVE_TOKEN_JSON", str(DEFAULT_TOKEN)),
        help="Local OAuth token cache. This file is created after first authorization.",
    )
    parser.add_argument(
        "--create-duplicate",
        action="store_true",
        help="Create a new Drive file even if a same-named file already exists in the target folder.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Authenticate and verify target folder access, but do not upload the file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without authenticating or contacting Drive.",
    )
    return parser.parse_args()


def load_google_api_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise SystemExit(
            "Missing Google Drive API dependencies.\n"
            "Install them with:\n"
            "  ./.venv/bin/python -m pip install -r requirements-google-drive.txt"
        ) from exc
    return Request, Credentials, InstalledAppFlow, build, HttpError, MediaFileUpload


def resolve_existing_file(path_text: str) -> Path:
    path = resolve_repo_relative_path(path_text)
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"Local file does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"Local path is not a file: {path}")
    return path


def resolve_repo_relative_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def drive_query_string(value: str) -> str:
    """Escape a string value for a Google Drive `q` query literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_credentials(credentials_path: Path, token_path: Path) -> Any:
    Request, Credentials, InstalledAppFlow, _build, _HttpError, _MediaFileUpload = load_google_api_modules()

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise SystemExit(
                    "Missing Google OAuth credentials JSON.\n"
                    f"Expected: {credentials_path}\n"
                    "Create a Desktop OAuth client in Google Cloud, download its JSON, "
                    "and save it at that path or pass --credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        try:
            token_path.chmod(0o600)
        except OSError:
            pass

    return creds


def build_drive_service(credentials_path: Path, token_path: Path) -> Any:
    _Request, _Credentials, _InstalledAppFlow, build, _HttpError, _MediaFileUpload = load_google_api_modules()
    creds = get_credentials(credentials_path, token_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_current_user(service: Any) -> dict[str, Any]:
    return service.about().get(fields="user(displayName,emailAddress)").execute()["user"]


def get_folder(service: Any, folder_id: str) -> dict[str, Any]:
    folder = (
        service.files()
        .get(
            fileId=folder_id,
            fields="id,name,mimeType,webViewLink,capabilities(canAddChildren)",
            supportsAllDrives=True,
        )
        .execute()
    )
    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise SystemExit(f"Target is not a Drive folder: {folder_id}")
    if not folder.get("capabilities", {}).get("canAddChildren", False):
        raise SystemExit(
            "Authenticated account cannot add files to this folder.\n"
            f"Folder: {folder.get('name')} ({folder_id})"
        )
    return folder


def find_existing_files(service: Any, folder_id: str, drive_name: str) -> list[dict[str, Any]]:
    query = (
        f"name = '{drive_query_string(drive_name)}' "
        f"and '{drive_query_string(folder_id)}' in parents "
        "and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=10,
            fields="files(id,name,webViewLink,modifiedTime,size)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
    )
    return response.get("files", [])


def upload_or_update_file(
    service: Any,
    local_file: Path,
    folder_id: str,
    drive_name: str,
    *,
    replace_existing: bool,
) -> tuple[str, dict[str, Any]]:
    _Request, _Credentials, _InstalledAppFlow, _build, _HttpError, MediaFileUpload = load_google_api_modules()

    mime_type = mimetypes.guess_type(str(local_file))[0] or "application/octet-stream"
    media = MediaFileUpload(str(local_file), mimetype=mime_type, resumable=True)
    fields = "id,name,mimeType,size,webViewLink,parents,modifiedTime"
    existing_files = find_existing_files(service, folder_id, drive_name)

    if replace_existing and existing_files:
        existing = existing_files[0]
        result = (
            service.files()
            .update(
                fileId=existing["id"],
                media_body=media,
                fields=fields,
                supportsAllDrives=True,
            )
            .execute()
        )
        return "updated", result

    metadata = {"name": drive_name, "parents": [folder_id]}
    result = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields=fields,
            supportsAllDrives=True,
        )
        .execute()
    )
    return "created", result


def main() -> int:
    args = parse_args()
    local_file = resolve_existing_file(args.file)
    drive_name = args.name or local_file.name
    credentials_path = resolve_repo_relative_path(args.credentials)
    token_path = resolve_repo_relative_path(args.token)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "local_file": str(local_file),
                    "drive_name": drive_name,
                    "folder_id": args.folder_id,
                    "credentials": str(credentials_path),
                    "token": str(token_path),
                    "replace_existing": not args.create_duplicate,
                },
                indent=2,
            )
        )
        return 0

    service = build_drive_service(credentials_path, token_path)
    user = get_current_user(service)
    folder = get_folder(service, args.folder_id)

    if args.check_only:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "authenticated_as": user,
                    "target_folder": folder,
                    "local_file": str(local_file),
                },
                indent=2,
            )
        )
        return 0

    action, uploaded = upload_or_update_file(
        service,
        local_file,
        args.folder_id,
        drive_name,
        replace_existing=not args.create_duplicate,
    )
    print(
        json.dumps(
            {
                "status": action,
                "authenticated_as": user,
                "target_folder": {
                    "id": folder["id"],
                    "name": folder["name"],
                    "webViewLink": folder.get("webViewLink"),
                },
                "file": uploaded,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
