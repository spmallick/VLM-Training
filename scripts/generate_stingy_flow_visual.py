#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "app" / "data" / "expense_agent.db"
POLICY_PATH = ROOT / "app" / "portal_site" / "policies" / "stingy_corp.md"
OUTPUT_PATH = ROOT / "output" / "stingy_agent_flow_visual.png"
RECEIPT_FALLBACK = ROOT / "output" / "cli_smoke_runs" / "20260425_120246_790951_stingy" / "receipt.png"
STEP1_IMAGE = ROOT / "output" / "stingy_slide_assets" / "stingy_step1_policy.png"
STEP2_IMAGE = ROOT / "output" / "stingy_slide_assets" / "stingy_step2_form.png"
STEP3_IMAGE = ROOT / "output" / "stingy_slide_assets" / "stingy_step3_review.png"
STEP4_IMAGE = ROOT / "output" / "stingy_slide_assets" / "stingy_step4_success.png"

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_MONO = "/System/Library/Fonts/Menlo.ttc"

BG = (247, 243, 236)
PANEL = (255, 255, 255)
PANEL_SOFT = (241, 245, 249)
NAVY = (21, 31, 54)
MUTED = (88, 99, 120)
BORDER = (213, 220, 231)
BLUE = (39, 104, 255)
PURPLE = (112, 83, 230)
GREEN = (18, 155, 112)
AMBER = (230, 138, 28)
TEAL = (16, 149, 164)
RED = (226, 72, 72)


def load_session_snapshot() -> dict[str, Any]:
    query = """
        select session_id, receipt_image_path, extraction_payload, policy_payload, memory_payload, portal_payload
        from sessions
        where company_slug='stingy' and status='completed'
        order by updated_at desc
        limit 1
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(query).fetchone()
    if row is None:
        raise RuntimeError("No completed Stingy session found in the session store.")
    session_id, receipt_image_path, extraction_payload, policy_payload, memory_payload, portal_payload = row
    return {
        "session_id": session_id,
        "receipt_image_path": receipt_image_path,
        "extraction": json.loads(extraction_payload),
        "policy": json.loads(policy_payload),
        "memory": json.loads(memory_payload),
        "portal": json.loads(portal_payload),
    }


def load_policy_bullets() -> list[str]:
    bullets: list[str] = []
    for line in POLICY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else (FONT_BOLD if bold else FONT_REGULAR)
    return ImageFont.truetype(path, size=size)


def rounded_panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], fill=PANEL, outline=BORDER, radius: int = 26):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2)


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    *,
    fill=NAVY,
    font_obj=None,
    line_gap: int = 8,
    bullet_indent: int = 0,
):
    x0, y0, x1, y1 = box
    current_y = y0
    for paragraph in text.split("\n"):
        prefix = ""
        body = paragraph
        x = x0
        if paragraph.startswith("• "):
            prefix = "• "
            body = paragraph[2:]
        words = body.split()
        line = ""
        lines: list[str] = []
        for word in words:
            trial = word if not line else f"{line} {word}"
            width = draw.textbbox((0, 0), prefix + trial if not lines else trial, font=font_obj)[2]
            if x0 + width > x1 and line:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        if not lines:
            lines = [""]
        for idx, wrapped_line in enumerate(lines):
            display = (prefix + wrapped_line) if idx == 0 and prefix else wrapped_line
            draw.text((x, current_y), display, fill=fill, font=font_obj)
            current_y += font_obj.size + line_gap
            x = x0 + bullet_indent
        current_y += 4
        if current_y > y1:
            break


def fit_image(path: Path, size: tuple[int, int]) -> Image.Image:
    image = Image.open(path).convert("RGB")
    target_w, target_h = size
    scale = min(target_w / image.width, target_h / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    left = (target_w - resized.width) // 2
    top = (target_h - resized.height) // 2
    canvas.paste(resized, (left, top))
    return canvas


def paste_card_image(base: Image.Image, image_path: Path, xy: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = xy
    fitted = fit_image(image_path, (x1 - x0, y1 - y0))
    base.paste(fitted, (x0, y0))


def pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], text: str, *, fill, text_fill):
    draw.rounded_rectangle(xy, radius=(xy[3] - xy[1]) // 2, fill=fill)
    text_box = draw.textbbox((0, 0), text, font=font(22, bold=True))
    text_w = text_box[2] - text_box[0]
    text_h = text_box[3] - text_box[1]
    x = xy[0] + ((xy[2] - xy[0]) - text_w) // 2
    y = xy[1] + ((xy[3] - xy[1]) - text_h) // 2 - 1
    draw.text((x, y), text, fill=text_fill, font=font(22, bold=True))


def build_visual() -> Path:
    snapshot = load_session_snapshot()
    extraction = snapshot["extraction"]
    policy = snapshot["policy"]
    memory = snapshot["memory"]
    policy_bullets = load_policy_bullets()

    receipt_path = Path(snapshot["receipt_image_path"] or RECEIPT_FALLBACK)
    if not receipt_path.exists():
        receipt_path = RECEIPT_FALLBACK

    W, H = 2000, 2300
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)

    # Header
    draw.rectangle((0, 0, W, 220), fill=(250, 235, 204))
    draw.polygon([(W - 260, 0), (W, 0), (W, 220), (W - 200, 220)], fill=(223, 235, 247))
    draw.text((90, 52), "One Stingy Corp. Run", fill=NAVY, font=font(68, bold=True))
    draw.text(
        (92, 138),
        "receipt -> extract -> policy -> open portal -> Qwen plans next action -> Playwright executes -> repeat until submit",
        fill=MUTED,
        font=font(28),
    )

    # Panel 1: receipt and extraction
    rounded_panel(draw, (70, 270, 980, 1040))
    rounded_panel(draw, (1020, 270, 1930, 1040), fill=PANEL_SOFT)
    pill(draw, (96, 296, 292, 350), "1. Receipt", fill=(233, 244, 255), text_fill=BLUE)
    pill(draw, (1046, 296, 1328, 350), "2. Extracted facts", fill=(235, 245, 255), text_fill=BLUE)

    paste_card_image(image, receipt_path, (120, 374, 530, 980))
    draw.text((570, 388), "Receipt highlights", fill=NAVY, font=font(34, bold=True))
    receipt_points = [
        f"Vendor: {extraction['fields']['vendor']}",
        f"Date: {extraction['fields']['transaction_date']}",
        f"Receipt total: {extraction['fields']['total']} {extraction['fields']['currency']}",
        f"Category: {extraction['fields']['category']}",
        f"Payment: {extraction['fields']['payment_method']}",
        f"Tip spotted: {extraction['semantic_amounts']['tip_amount']}",
    ]
    draw_wrapped(
        draw,
        "\n".join(f"• {item}" for item in receipt_points),
        (570, 438, 930, 720),
        font_obj=font(24),
        fill=NAVY,
        bullet_indent=22,
    )
    draw.text((570, 760), "Line items", fill=NAVY, font=font(30, bold=True))
    draw_wrapped(
        draw,
        "\n".join(f"• {item}" for item in extraction["line_item_summary"]),
        (570, 810, 930, 980),
        font_obj=font(24),
        fill=MUTED,
        bullet_indent=22,
    )

    draw.text((1050, 388), "Qwen receipt extraction", fill=NAVY, font=font(34, bold=True))
    extract_text = (
        '{\n'
        f'  "vendor": "{extraction["fields"]["vendor"]}",\n'
        f'  "transaction_date": "{extraction["fields"]["transaction_date"]}",\n'
        f'  "total": "{extraction["fields"]["total"]}",\n'
        f'  "category": "{extraction["fields"]["category"]}",\n'
        f'  "tip_amount": "{extraction["semantic_amounts"]["tip_amount"]}",\n'
        f'  "payment_method": "{extraction["fields"]["payment_method"]}"\n'
        '}'
    )
    draw_wrapped(draw, extract_text, (1050, 448, 1880, 760), font_obj=font(24, mono=True), fill=NAVY, line_gap=6)
    draw.text((1050, 812), "Why this matters", fill=NAVY, font=font(30, bold=True))
    why_text = (
        "The extracted facts become the agent's goal values.\n"
        "Stingy later uses them to fill the form, compute the reduced claim, and explain the excluded tip."
    )
    draw_wrapped(draw, why_text, (1050, 860, 1880, 980), font_obj=font(24), fill=MUTED)

    # Panel 2: policy and reduced claim
    rounded_panel(draw, (70, 1080, 1930, 1485))
    pill(draw, (96, 1106, 330, 1160), "3. Policy review", fill=(233, 251, 240), text_fill=GREEN)
    draw.text((110, 1208), "What policy was read", fill=NAVY, font=font(34, bold=True))
    trimmed_policy = [
        policy_bullets[1],
        policy_bullets[3],
        policy_bullets[4],
    ]
    draw_wrapped(
        draw,
        "\n".join(f"• {item}" for item in trimmed_policy),
        (112, 1260, 980, 1448),
        font_obj=font(24),
        fill=NAVY,
        bullet_indent=24,
    )
    draw.text((1040, 1208), "Policy decision and claim math", fill=NAVY, font=font(34, bold=True))
    draw.text((1060, 1280), "12.00", fill=BLUE, font=font(62, bold=True))
    draw.text((1230, 1292), "-", fill=MUTED, font=font(54, bold=True))
    draw.text((1295, 1280), "2.16", fill=RED, font=font(62, bold=True))
    draw.text((1475, 1292), "=", fill=MUTED, font=font(54, bold=True))
    draw.text((1540, 1280), "9.84", fill=GREEN, font=font(62, bold=True))
    draw.text((1058, 1360), "receipt total", fill=MUTED, font=font(22))
    draw.text((1298, 1360), "tip excluded", fill=MUTED, font=font(22))
    draw.text((1540, 1360), "claim amount", fill=MUTED, font=font(22))
    reviewer_text = (
        f'Reviewer result: {policy["recommended_action"]}\n'
        f'Risk: {policy["risk_level"]}    Confidence: {policy["confidence"]:.2f}\n\n'
        f'Adjustment note written into the portal:\n'
        f'"{memory["derived_values"]["adjustment_note"]["value"]}"'
    )
    draw_wrapped(draw, reviewer_text, (1058, 1410, 1860, 1460), font_obj=font(22), fill=NAVY)

    # Panel 3: portal loop
    rounded_panel(draw, (70, 1520, 1930, 2230), fill=PANEL_SOFT)
    pill(draw, (96, 1546, 420, 1600), "4. Open portal and loop", fill=(232, 239, 255), text_fill=PURPLE)
    draw.text((112, 1640), "Qwen sees screenshots of the current page, the visible controls, and the end goal.", fill=NAVY, font=font(32, bold=True))
    draw.text((112, 1686), "Then Playwright executes exactly the next actions Qwen chose.", fill=MUTED, font=font(24))

    screenshot_boxes = [
        (112, 1745, 670, 2085, STEP1_IMAGE, "Policy gate", ["check policy box", "click Continue to form"], BLUE),
        (722, 1745, 1280, 2085, STEP2_IMAGE, "Expense form", ["fill fields", "upload receipt", "click Review request"], PURPLE),
        (1332, 1745, 1890, 2085, STEP3_IMAGE, "Final review", ["check review box", "submit workflow packet"], GREEN),
    ]
    for x0, y0, x1, y1, path, title, bullets, accent in screenshot_boxes:
        rounded_panel(draw, (x0, y0, x1, y1), fill=PANEL)
        paste_card_image(image, path, (x0 + 16, y0 + 16, x1 - 16, y0 + 232))
        pill(draw, (x0 + 18, y0 + 18, x0 + 190, y0 + 62), title, fill=(255, 255, 255), text_fill=accent)
        draw_wrapped(draw, "\n".join(f"• {item}" for item in bullets), (x0 + 24, y0 + 248, x1 - 24, y1 - 20), font_obj=font(22), fill=NAVY, bullet_indent=20)

    draw.line((670, 1915, 722, 1915), fill=BORDER, width=6)
    draw.polygon([(706, 1900), (706, 1930), (732, 1915)], fill=BORDER)
    draw.line((1280, 1915, 1332, 1915), fill=BORDER, width=6)
    draw.polygon([(1316, 1900), (1316, 1930), (1342, 1915)], fill=BORDER)

    # Panel 4 footer
    draw.rounded_rectangle((112, 2118, 1100, 2210), radius=20, fill=(20, 30, 51))
    footer_prompt = (
        "Qwen plan example: { action: 'check', target: 'review ack' } -> { action: 'submit', target: 'Submit workflow packet' }"
    )
    draw_wrapped(draw, footer_prompt, (142, 2142, 1070, 2200), font_obj=font(22, mono=True), fill=(255, 255, 255))
    rounded_panel(draw, (1138, 2118, 1890, 2210), fill=(231, 248, 238), outline=(180, 222, 199), radius=20)
    draw.text(
        (1168, 2140),
        f"Submitted successfully: {snapshot['portal']['submission_id']}",
        fill=GREEN,
        font=font(24, bold=True),
    )
    draw.text(
        (1168, 2172),
        f"Session {snapshot['session_id']} at {snapshot['portal']['submission_timestamp']}",
        fill=MUTED,
        font=font(20),
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT_PATH)
    return OUTPUT_PATH


def main() -> None:
    output = build_visual()
    print(output)


if __name__ == "__main__":
    main()
