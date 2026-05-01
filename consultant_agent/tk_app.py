"""CustomTkinter-based consultant desktop app.

This alternative frontend presents the consultant flow as a large, clickable
workflow while reusing the existing backend/controller from highgui_app.py.
"""

from __future__ import annotations

import argparse
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - depends on local Tk support
    raise SystemExit(
        "CustomTkinter UI requires a Tk-enabled Python build plus the customtkinter package."
    ) from exc

import cv2
from PIL import Image


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from consultant_agent.highgui_app import ConsultantDesktopApp


class ConsultantTkApp:
    def __init__(self, *, base_url: str, camera_index: int = 0):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("green")

        self.backend = ConsultantDesktopApp(base_url=base_url, camera_index=camera_index)
        self.root = ctk.CTk()
        self.root.title("Consultant Agent")
        self.root.geometry("1500x980")
        self.root.minsize(1320, 860)
        self.root.configure(fg_color="#ece7df")

        self.preview_image: ctk.CTkImage | None = None
        self.company_names = [company.name for company in self.backend.company_portals]
        self.company_var = tk.StringVar(value=self.backend.selected_company.name)
        self.current_step = 1
        self.busy = False

        self.step_titles = {
            1: "Select Company",
            2: "Receipt Intake",
            3: "Running Commentary",
        }
        self.stepper_canvas: tk.Canvas | None = None
        self.step_frames: dict[int, ctk.CTkFrame] = {}
        self.company_buttons: list[ctk.CTkButton] = []
        self._last_commentary = ""
        self._last_summary = ""

        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self._go_to_step(1)
        self._refresh_ui()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self.root)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Load Photo...", command=self._load_photo, accelerator="U")
        file_menu.add_command(label="Clear Receipt", command=self._clear_receipt, accelerator="R")
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close, accelerator="Q")
        menu_bar.add_cascade(label="File", menu=file_menu)

        actions_menu = tk.Menu(menu_bar, tearoff=False)
        actions_menu.add_command(label="Start or Stop Camera", command=self._toggle_camera, accelerator="C")
        actions_menu.add_command(label="Capture Photo", command=self._capture_photo, accelerator="Space")
        actions_menu.add_separator()
        actions_menu.add_command(label="Analyze Receipt", command=self._analyze_only, accelerator="A")
        actions_menu.add_command(label="Start Processing", command=self._launch_agent, accelerator="G")
        menu_bar.add_cascade(label="Actions", menu=actions_menu)

        company_menu = tk.Menu(menu_bar, tearoff=False)
        for index, company in enumerate(self.backend.company_portals):
            company_menu.add_radiobutton(
                label=company.name,
                value=company.name,
                variable=self.company_var,
                command=lambda idx=index: self._select_company(idx),
            )
        menu_bar.add_cascade(label="Company", menu=company_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="Keyboard Shortcuts", command=self._show_shortcuts)
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=menu_bar)

    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        self.header = ctk.CTkFrame(
            self.root,
            fg_color="#f6f4ef",
            corner_radius=24,
            border_width=1,
            border_color="#d8d4cc",
        )
        self.header.grid(row=0, column=0, padx=24, pady=(18, 8), sticky="nsew")
        self.header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.header,
            text="Consultant-side receipt intake",
            text_color="#8b877d",
            font=ctk.CTkFont(size=12, weight="normal"),
        ).grid(row=0, column=0, padx=20, pady=(14, 0), sticky="w")
        ctk.CTkLabel(
            self.header,
            text="Consultant Agent",
            text_color="#32261a",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).grid(row=1, column=0, padx=20, pady=(0, 14), sticky="w")
        ctk.CTkLabel(
            self.header,
            text="Move through the workflow, review the live commentary, and resubmit the same receipt to a different company when needed.",
            text_color="#5a6670",
            justify="left",
            wraplength=560,
            font=ctk.CTkFont(size=14),
        ).grid(row=0, column=1, rowspan=2, padx=(16, 16), pady=14, sticky="w")

        self.company_chip = ctk.CTkLabel(
            self.header,
            text=self.backend.selected_company.name,
            text_color="#507c5d",
            fg_color="#edf4ef",
            corner_radius=16,
            padx=14,
            pady=8,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.company_chip.grid(row=0, column=2, rowspan=2, padx=(8, 20), pady=16, sticky="e")

        self.stepper_canvas = tk.Canvas(
            self.root,
            height=52,
            bg="#ece7df",
            highlightthickness=0,
            bd=0,
        )
        self.stepper_canvas.grid(row=1, column=0, padx=24, pady=(0, 10), sticky="ew")
        self.stepper_canvas.bind("<Configure>", lambda _event: self._draw_stepper())

        self.content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content.grid(row=2, column=0, padx=24, pady=(0, 24), sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self._build_step_company()
        self._build_step_receipt()
        self._build_step_commentary()

    def _build_step_company(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.step_frames[1] = frame

        selection_card = self._card(frame)
        selection_card.grid(row=0, column=0, sticky="nsew")
        selection_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            selection_card,
            text="Select Company",
            text_color="#32261a",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(22, 2))
        ctk.CTkLabel(
            selection_card,
            text="Choose one of the three portals.",
            text_color="#6a7580",
            font=ctk.CTkFont(size=14),
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 14))

        self.company_button_panel = ctk.CTkFrame(selection_card, fg_color="transparent")
        self.company_button_panel.grid(row=2, column=0, sticky="ew", padx=20)
        self.company_button_panel.grid_columnconfigure(0, weight=1)
        for index, company in enumerate(self.backend.company_portals):
            button = ctk.CTkButton(
                self.company_button_panel,
                text=company.name,
                command=lambda idx=index: self._select_company(idx),
                height=92,
                corner_radius=24,
                anchor="w",
                font=ctk.CTkFont(size=24, weight="bold"),
                text_color="#33424c",
                fg_color="#fdfbf8",
                hover_color="#f2eee7",
                border_width=1,
                border_color="#d8d4cc",
            )
            button.grid(row=index, column=0, sticky="ew", padx=4, pady=8)
            self.company_buttons.append(button)

    def _build_step_receipt(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=3)
        frame.grid_columnconfigure(1, weight=2)
        self.step_frames[2] = frame

        self.preview_card = self._card(frame)
        self.preview_card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self.preview_card.grid_rowconfigure(1, weight=1)
        self.preview_card.grid_columnconfigure(0, weight=1)

        preview_header = ctk.CTkFrame(self.preview_card, fg_color="transparent")
        preview_header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 12))
        preview_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            preview_header,
            text="Step 2",
            text_color="#7d7a72",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            preview_header,
            text="Upload or capture the receipt",
            text_color="#32261a",
            font=ctk.CTkFont(size=30, weight="bold"),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.preview_chip = ctk.CTkLabel(
            preview_header,
            text="Awaiting capture",
            text_color="#8b7445",
            fg_color="#f3efe8",
            corner_radius=18,
            padx=18,
            pady=10,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.preview_chip.grid(row=0, column=1, rowspan=2, sticky="e")

        self.preview_label = ctk.CTkLabel(
            self.preview_card,
            text="No receipt loaded yet\n\nUse the camera or upload a receipt to get started.",
            text_color="#69757f",
            fg_color="#f1f2ef",
            corner_radius=28,
            justify="center",
            wraplength=720,
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        self.preview_label.grid(row=1, column=0, padx=24, pady=(0, 24), sticky="nsew")

        self.action_panel = ctk.CTkFrame(frame, fg_color="transparent")
        self.action_panel.grid(row=0, column=1, sticky="nsew")
        self.action_panel.grid_rowconfigure(2, weight=1)
        self.action_panel.grid_columnconfigure(0, weight=1)

        company_card = self._card(self.action_panel)
        company_card.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            company_card,
            text="Current company",
            text_color="#7d7a72",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=22, pady=(20, 0))
        self.receipt_company_label = ctk.CTkLabel(
            company_card,
            text=self.backend.selected_company.name,
            text_color="#32261a",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        self.receipt_company_label.pack(anchor="w", padx=22, pady=(4, 4))
        self.receipt_company_hint = ctk.CTkLabel(
            company_card,
            text="You can return to Step 1 at any time and reroute the same receipt.",
            text_color="#5a6670",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=15),
        )
        self.receipt_company_hint.pack(anchor="w", padx=22, pady=(0, 20))

        controls_card = self._card(self.action_panel)
        controls_card.grid(row=1, column=0, sticky="ew", pady=16)
        ctk.CTkLabel(
            controls_card,
            text="Receipt actions",
            text_color="#32261a",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(anchor="w", padx=22, pady=(22, 4))
        self.receipt_state_label = ctk.CTkLabel(
            controls_card,
            text="No receipt loaded.",
            text_color="#6a7580",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=16),
        )
        self.receipt_state_label.pack(anchor="w", padx=22, pady=(0, 18))

        button_grid = ctk.CTkFrame(controls_card, fg_color="transparent")
        button_grid.pack(fill="x", padx=18, pady=(0, 12))
        button_grid.grid_columnconfigure((0, 1), weight=1)

        self.camera_button = ctk.CTkButton(
            button_grid,
            text="Start Camera",
            command=self._toggle_camera,
            height=56,
            corner_radius=22,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.capture_button = ctk.CTkButton(
            button_grid,
            text="Capture Photo",
            command=self._capture_photo,
            height=56,
            corner_radius=22,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.load_button = ctk.CTkButton(
            button_grid,
            text="Upload Receipt",
            command=self._load_photo,
            height=56,
            corner_radius=22,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.clear_button = ctk.CTkButton(
            button_grid,
            text="Clear Receipt",
            command=self._clear_receipt,
            height=56,
            corner_radius=22,
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color="#d6cdbf",
            hover_color="#c8bca9",
            text_color="#3a342d",
        )
        self.camera_button.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        self.capture_button.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        self.load_button.grid(row=1, column=0, padx=6, pady=6, sticky="ew")
        self.clear_button.grid(row=1, column=1, padx=6, pady=6, sticky="ew")

        self.analyze_button = ctk.CTkButton(
            controls_card,
            text="Analyze Only",
            command=self._analyze_only,
            height=54,
            corner_radius=22,
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color="#e6ecef",
            hover_color="#d9e2e7",
            text_color="#33424c",
        )
        self.analyze_button.pack(fill="x", padx=22, pady=(10, 10))
        self.launch_button = ctk.CTkButton(
            controls_card,
            text="Start Processing",
            command=self._launch_agent,
            height=64,
            corner_radius=24,
            font=ctk.CTkFont(size=20, weight="bold"),
            fg_color="#6fbf83",
            hover_color="#5cad72",
        )
        self.launch_button.pack(fill="x", padx=22, pady=(0, 22))

        nav_card = self._card(self.action_panel)
        nav_card.grid(row=2, column=0, sticky="nsew")
        ctk.CTkLabel(
            nav_card,
            text="Workflow navigation",
            text_color="#32261a",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(anchor="w", padx=22, pady=(22, 10))
        ctk.CTkLabel(
            nav_card,
            text="Use the buttons below to move between steps. The commentary step stays live while the agent is running.",
            text_color="#5a6670",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=15),
        ).pack(anchor="w", padx=22, pady=(0, 18))
        self.back_to_company_button = ctk.CTkButton(
            nav_card,
            text="Back to Step 1",
            command=lambda: self._go_to_step(1),
            height=52,
            corner_radius=20,
            font=ctk.CTkFont(size=17, weight="bold"),
            fg_color="#f1ece3",
            hover_color="#e6ded0",
            text_color="#3b342b",
        )
        self.back_to_company_button.pack(fill="x", padx=22, pady=(0, 10))
        self.open_commentary_button = ctk.CTkButton(
            nav_card,
            text="Open Step 3 Commentary",
            command=lambda: self._go_to_step(3),
            height=52,
            corner_radius=20,
            font=ctk.CTkFont(size=17, weight="bold"),
            fg_color="#e6ecef",
            hover_color="#d9e2e7",
            text_color="#33424c",
        )
        self.open_commentary_button.pack(fill="x", padx=22, pady=(0, 22))

    def _build_step_commentary(self) -> None:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.step_frames[3] = frame

        commentary_card = self._card(frame)
        commentary_card.grid(row=0, column=0, sticky="nsew")
        commentary_card.grid_rowconfigure(2, weight=1)
        commentary_card.grid_columnconfigure(0, weight=1)

        commentary_header = ctk.CTkFrame(commentary_card, fg_color="transparent")
        commentary_header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        commentary_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            commentary_header,
            text="Step 3",
            text_color="#7d7a72",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            commentary_header,
            text="Running commentary",
            text_color="#32261a",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=1, column=0, sticky="w", pady=(0, 0))
        ctk.CTkLabel(
            commentary_header,
            text="Detailed live trace of tools, decisions, and UI analysis.",
            text_color="#5a6670",
            font=ctk.CTkFont(size=13),
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.commentary_chip = ctk.CTkLabel(
            commentary_header,
            text="STANDBY",
            text_color="#8b7445",
            fg_color="#f3efe8",
            corner_radius=14,
            padx=12,
            pady=6,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.commentary_chip.grid(row=0, column=1, rowspan=3, sticky="e")

        context_strip = ctk.CTkFrame(
            commentary_card,
            fg_color="#fbfaf7",
            corner_radius=16,
            border_width=1,
            border_color="#e2ddd3",
        )
        context_strip.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        context_strip.grid_columnconfigure(0, weight=1)
        context_strip.grid_columnconfigure(1, weight=1)
        context_strip.grid_columnconfigure(2, weight=1)
        self.commentary_target_label = ctk.CTkLabel(
            context_strip,
            text="Target: --",
            text_color="#355341",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.commentary_target_label.grid(row=0, column=0, sticky="w", padx=16, pady=10)
        self.commentary_step_label = ctk.CTkLabel(
            context_strip,
            text="Stage: Waiting",
            text_color="#4f5d68",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.commentary_step_label.grid(row=0, column=1, sticky="w", padx=16, pady=10)
        self.commentary_session_label = ctk.CTkLabel(
            context_strip,
            text="Session: --",
            text_color="#4f5d68",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.commentary_session_label.grid(row=0, column=2, sticky="w", padx=16, pady=10)

        self.commentary_text = ctk.CTkTextbox(
            commentary_card,
            wrap="word",
            border_width=0,
            corner_radius=28,
            fg_color="#fbfaf7",
            text_color="#34434d",
            font=ctk.CTkFont(size=18),
            activate_scrollbars=True,
        )
        self.commentary_text.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 14))

        summary_card = self._card(frame)
        summary_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.summary_text = ctk.CTkTextbox(
            summary_card,
            height=56,
            wrap="word",
            border_width=0,
            fg_color="transparent",
            text_color="#4f5d68",
            font=ctk.CTkFont(size=14),
        )
        self.summary_text.pack(fill="x", expand=False, padx=16, pady=12)

    def _card(self, parent) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            parent,
            fg_color="#f6f4ef",
            corner_radius=28,
            border_width=1,
            border_color="#d9d4ca",
        )

    def _bind_keys(self) -> None:
        self.root.bind("1", lambda _event: self._select_company(0))
        self.root.bind("2", lambda _event: self._select_company(1))
        self.root.bind("3", lambda _event: self._select_company(2))
        self.root.bind("c", lambda _event: self._toggle_camera())
        self.root.bind("u", lambda _event: self._load_photo())
        self.root.bind("a", lambda _event: self._analyze_only())
        self.root.bind("g", lambda _event: self._launch_agent())
        self.root.bind("r", lambda _event: self._clear_receipt())
        self.root.bind("<space>", lambda _event: self._capture_photo())
        self.root.bind("q", lambda _event: self._on_close())

    def _go_to_step(self, step: int) -> None:
        if step not in self.step_frames:
            return
        self.current_step = step
        self.step_frames[step].tkraise()
        self._refresh_step_buttons()

    def _refresh_step_buttons(self) -> None:
        self._draw_stepper()

    def _draw_stepper(self) -> None:
        if self.stepper_canvas is None:
            return

        canvas = self.stepper_canvas
        width = max(canvas.winfo_width(), 900)
        height = max(canvas.winfo_height(), 52)
        canvas.delete("all")

        step_count = len(self.step_titles)
        arrow = max(20, min(28, width // 30))
        segment = width / step_count
        top = 6
        bottom = height - 6
        mid = height / 2
        separator = "#f7f4ef"
        complete_fill = "#bfe7ff"
        future_fill = "#dff1ff"
        active_fill = "#ef5d5d"
        complete_text = "#31586b"
        future_text = "#557185"
        active_text = "#fffaf7"

        for step in range(1, step_count + 1):
            base_left = (step - 1) * segment
            x1 = step * segment
            points = [
                base_left,
                top,
                x1 - arrow,
                top,
                x1,
                mid,
                x1 - arrow,
                bottom,
                base_left,
                bottom,
            ]

            is_active = step == self.current_step
            is_complete = step < self.current_step
            if is_active:
                fill = active_fill
                text_color = active_text
            elif is_complete:
                fill = complete_fill
                text_color = complete_text
            else:
                fill = future_fill
                text_color = future_text

            tag = f"step_{step}"
            canvas.create_polygon(
                points,
                fill=fill,
                outline="",
                smooth=False,
                tags=(tag, "step"),
            )

            if step > 1:
                separator_points = [
                    base_left - arrow + 4,
                    top,
                    base_left - 5,
                    top,
                    base_left + 9,
                    mid,
                    base_left - 5,
                    bottom,
                    base_left - arrow + 4,
                    bottom,
                    base_left - 8,
                    mid,
                ]
                canvas.create_polygon(
                    separator_points,
                    fill=separator,
                    outline=separator,
                )

            icon_x = base_left + (segment * 0.14)
            text_x = base_left + (segment * 0.54)
            if step == 1:
                icon_x = base_left + (segment * 0.16)
                text_x = base_left + (segment * 0.55)

            if is_complete:
                canvas.create_line(
                    icon_x - 7,
                    mid + 1,
                    icon_x - 2,
                    mid + 6,
                    icon_x + 8,
                    mid - 6,
                    fill="#2f9968",
                    width=3,
                    capstyle=tk.ROUND,
                    joinstyle=tk.ROUND,
                    tags=(tag, "step"),
                )
            else:
                canvas.create_text(
                    icon_x,
                    mid,
                    text=str(step),
                    fill=text_color,
                    font=("Helvetica", 12, "bold"),
                    tags=(tag, "step"),
                )
            canvas.create_text(
                text_x,
                mid,
                text=self.step_titles[step],
                fill=text_color,
                font=("Helvetica", 14, "bold"),
                tags=(tag, "step"),
            )
            canvas.tag_bind(tag, "<Button-1>", lambda _event, step_index=step: self._go_to_step(step_index))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.camera_button,
            self.capture_button,
            self.load_button,
            self.clear_button,
            self.analyze_button,
            self.launch_button,
        ):
            button.configure(state=state)

    def _run_backend_action(self, action, *, busy_message: str) -> None:
        if self.busy:
            return

        self.backend.status = busy_message
        self._go_to_step(3)
        self._set_busy(True)
        self._refresh_ui()

        def worker() -> None:
            try:
                action()
            except Exception as exc:  # pragma: no cover - UI thread reporting
                self.backend.status = f"Action failed: {exc}"
            finally:
                self.root.after(0, self._finish_backend_action)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_backend_action(self) -> None:
        self._set_busy(False)
        self._refresh_ui()

    def _select_company(self, index: int) -> None:
        self.backend.select_company(index)
        if self.current_step == 1:
            self._go_to_step(2)
        self._refresh_ui()

    def _select_company_by_name(self, company_name: str) -> None:
        try:
            index = self.company_names.index(company_name)
        except ValueError:
            return
        self._select_company(index)

    def _toggle_camera(self) -> None:
        if self.busy:
            return
        self._go_to_step(2)
        self.backend.toggle_camera()
        self._refresh_ui()

    def _capture_photo(self) -> None:
        if self.busy:
            return
        self._go_to_step(2)
        self.backend.capture_photo()
        self._refresh_ui()

    def _load_photo(self) -> None:
        if self.busy:
            return
        self._go_to_step(2)
        self.backend.load_photo()
        self._refresh_ui()

    def _analyze_only(self) -> None:
        self._run_backend_action(
            lambda: self.backend.analyze(open_portal=False),
            busy_message="Analyzing the receipt and preparing a summary...",
        )

    def _launch_agent(self) -> None:
        self._run_backend_action(
            lambda: self.backend.analyze(open_portal=True),
            busy_message="Analyzing the receipt and starting the live portal workflow...",
        )

    def _clear_receipt(self) -> None:
        if self.busy:
            return
        self.backend.clear()
        self._go_to_step(2)
        self._refresh_ui()

    def _poll(self) -> None:
        if self.backend.camera is not None:
            self.backend.update_camera_frame()
        self._refresh_ui()
        self.root.after(120 if self.backend.camera is None else 90, self._poll)

    def _refresh_ui(self) -> None:
        self.company_chip.configure(text=self.backend.selected_company.name)
        self.company_var.set(self.backend.selected_company.name)
        self._refresh_step_buttons()
        self._refresh_company_step()
        self._refresh_receipt_step()
        self._refresh_commentary_step()

    def _refresh_company_step(self) -> None:
        for index, button in enumerate(self.company_buttons):
            is_selected = index == self.backend.company_index
            button.configure(
                fg_color="#e8f1e9" if is_selected else "#fdfbf8",
                hover_color="#dfeade" if is_selected else "#f2eee7",
                text_color="#355341" if is_selected else "#33424c",
                border_color="#b7ccb8" if is_selected else "#d8d4cc",
            )

    def _refresh_receipt_step(self) -> None:
        self.receipt_company_label.configure(text=self.backend.selected_company.name)
        self.camera_button.configure(text="Stop Camera" if self.backend.camera is not None else "Start Camera")
        self.open_commentary_button.configure(
            state="normal" if (self.backend.last_result is not None or self.backend.last_session_id is not None or self.busy) else "disabled"
        )

        preview_source = (
            self.backend.live_frame
            if self.backend.camera is not None and self.backend.live_frame is not None
            else self.backend.current_image
        )
        if self.backend.camera is not None and self.backend.live_frame is not None:
            self.preview_chip.configure(text="Live camera", fg_color="#e6f2ef", text_color="#39766b")
            receipt_state = "Camera is live. Capture a frame when the receipt is readable."
        elif self.backend.current_image is not None:
            self.preview_chip.configure(text="Receipt ready", fg_color="#edf3f8", text_color="#3d7c93")
            receipt_state = "Receipt is loaded. Start processing when you are ready."
        else:
            self.preview_chip.configure(text="Awaiting capture", fg_color="#f3efe8", text_color="#8b7445")
            receipt_state = "No receipt loaded. Use the camera or upload an image."
        self.receipt_state_label.configure(text=receipt_state)

        self.capture_button.configure(state="normal" if self.backend.camera is not None and not self.busy else "disabled")
        self.analyze_button.configure(state="normal" if self.backend.current_image_path is not None and not self.busy else "disabled")
        self.launch_button.configure(state="normal" if self.backend.current_image_path is not None and not self.busy else "disabled")
        self.clear_button.configure(state="normal" if self.backend.current_image_path is not None and not self.busy else "disabled")

        if preview_source is not None:
            rgb = cv2.cvtColor(preview_source, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            image.thumbnail((860, 620))
            self.preview_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
            self.preview_label.configure(image=self.preview_image, text="", fg_color="#eff1ee")
        else:
            self.preview_label.configure(
                image=None,
                text="No receipt loaded yet\n\nUse the camera or upload a receipt to get started.",
                fg_color="#eff1ee",
                text_color="#69757f",
            )

    def _refresh_commentary_step(self) -> None:
        snapshot = self._current_session_snapshot()
        commentary = self._commentary_text(snapshot)
        summary = self._summary_text(snapshot)

        if commentary != self._last_commentary:
            self._replace_text(self.commentary_text, commentary)
            self._last_commentary = commentary
        if summary != self._last_summary:
            self._replace_text(self.summary_text, summary)
            self._last_summary = summary

        if snapshot is not None:
            self.commentary_target_label.configure(text=f"Target: {self._company_name_for_slug(snapshot.company_slug)}")
            self.commentary_step_label.configure(
                text=f"Stage: {snapshot.current_step.replace('_', ' ').title() or 'Starting'}"
            )
            self.commentary_session_label.configure(text=f"Session: {snapshot.session_id}")
            if snapshot.status in {"completed"}:
                self.commentary_chip.configure(text="COMPLETED", fg_color="#e8f1e9", text_color="#355341")
            elif snapshot.status in {"error", "needs_review"}:
                self.commentary_chip.configure(text="ATTENTION", fg_color="#f5e8e3", text_color="#8b4d3c")
            else:
                self.commentary_chip.configure(text="RUNNING", fg_color="#edf4f8", text_color="#3d7c93")
        elif self.busy:
            self.commentary_target_label.configure(text=f"Target: {self.backend.selected_company.name}")
            self.commentary_step_label.configure(text="Stage: Preparing the run")
            self.commentary_session_label.configure(text="Session: creating...")
            self.commentary_chip.configure(text="RUNNING", fg_color="#edf4f8", text_color="#3d7c93")
        else:
            self.commentary_target_label.configure(text=f"Target: {self.backend.selected_company.name}")
            self.commentary_step_label.configure(text="Stage: Waiting")
            self.commentary_session_label.configure(text="Session: --")
            self.commentary_chip.configure(text="STANDBY", fg_color="#f3efe8", text_color="#8b7445")

    def _current_session_snapshot(self):
        session_id = self.backend.last_session_id
        if not session_id:
            return None
        try:
            return self.backend.session_store.get_session(session_id)
        except KeyError:
            return None

    def _commentary_text(self, snapshot) -> str:
        if snapshot is not None:
            lines: list[str] = []
            for event in snapshot.events[-40:]:
                stamp = event.created_at.astimezone().strftime("%I:%M:%S %p").lstrip("0")
                lines.append(f"[{stamp}] {self._event_heading(event.kind)}\n{event.message}")
            if snapshot.error_text:
                lines.append(f"[ERROR] ERROR\n{snapshot.error_text}")
            return "\n\n".join(lines) or "The agent has started but has not written any commentary yet."

        if self.busy:
            return self.backend.status

        if self.backend.last_result is not None:
            fields = self.backend.last_result.extraction.get("fields", {})
            extraction = self.backend.last_result.extraction
            reasoning = extraction.get("reasoning_summary") or "The extraction engine summarized the visible receipt evidence."
            lines = [
                "Step 1 · Receipt intake",
                f"Target: {self.backend.last_result.company_name}. Saved run folder: {self.backend.last_result.run_dir.name}.",
                "",
                "Tool · check_image_quality",
                (
                    f"Blur detector verdict: {self.backend.last_result.blur.get('verdict', 'unknown')}. "
                    f"Score: {self.backend.last_result.blur.get('score', 'unknown')}. "
                    f"Confidence: {self.backend.last_result.blur.get('confidence', 'unknown')}."
                ),
                "",
                "Tool · extract_receipt_data",
                (
                    f"Engine: {self._vision_engine_label(extraction)}. "
                    f"Document: {extraction.get('document_label', 'unknown')}. "
                    f"Visibility: {extraction.get('receipt_visibility', 'unknown')}. "
                    f"Image quality: {extraction.get('image_quality', 'unknown')}. "
                    f"Reasoning: {reasoning}"
                ),
                "",
                "Finding · receipt facts",
                f"Vendor: {fields.get('vendor', 'unknown')}.",
                f"Date: {fields.get('transaction_date', 'unknown')}.",
                f"Total: {fields.get('total', 'unknown')} {fields.get('currency', '')}".strip(),
                f"Category: {fields.get('category', 'unknown')}.",
                f"Claim amount: {self.backend.last_result.claim_amount_local}.",
            ]
            if self.backend.last_result.warnings:
                lines.extend(["", "Warning", self.backend.last_result.warnings[0]])
            return "\n".join(lines)

        return (
            "Start processing from Step 2 to watch a live commentary here.\n\n"
            "This panel will stream the agent's tool calls, model usage, receipt checks, policy decisions, UI inspection, "
            "browser actions, and final submission outcome."
        )

    def _summary_text(self, snapshot) -> str:
        if snapshot is not None:
            company_name = self._company_name_for_slug(snapshot.company_slug)
            vision_engine = self._vision_engine_label(snapshot.extraction.model_dump(mode="json")) if snapshot.extraction else "Pending"
            policy_engine = self._policy_engine_label(snapshot)
            browser_engine = (
                "Qwen + Playwright" if self.backend.settings.hf_api_token and snapshot.portal_state.open_portal_url else
                "Playwright" if snapshot.portal_state.open_portal_url else
                "Not opened"
            )
            return "  |  ".join(
                [
                    f"Target: {company_name}",
                    f"Status: {snapshot.status.replace('_', ' ').title()}",
                    f"Vision: {vision_engine}",
                    f"Policy: {policy_engine}",
                    f"Browser: {browser_engine}",
                    f"Action: {snapshot.portal_state.last_agent_action or 'Waiting for the next action.'}",
                ]
            )

        if self.backend.last_result is not None:
            fields = self.backend.last_result.extraction.get("fields", {})
            return "  |  ".join(
                [
                    f"Target: {self.backend.last_result.company_name}",
                    f"Vision: {self._vision_engine_label(self.backend.last_result.extraction)}",
                    f"Vendor: {fields.get('vendor', 'Unknown')}",
                    f"Claim: {self.backend.last_result.claim_amount_local}",
                    f"Saved: {self.backend.last_result.run_dir.name}",
                ]
            )

        return (
            f"Target: {self.backend.selected_company.name}  |  "
            f"Receipt: {'Ready' if self.backend.current_image_path else 'Missing'}  |  "
            f"Camera: {'Live' if self.backend.camera is not None else 'Idle'}  |  "
            "Next: Use Step 2 to upload or capture a receipt."
        )

    def _company_name_for_slug(self, slug: str) -> str:
        for company in self.backend.company_portals:
            if company.slug == slug:
                return company.name
        return self.backend.selected_company.name

    @staticmethod
    def _event_heading(kind: str) -> str:
        return {
            "action": "TRACE",
            "success": "SUCCESS",
            "warning": "CHECK",
            "error": "ERROR",
            "info": "INFO",
        }.get(kind, kind.upper())

    def _vision_engine_label(self, extraction: dict) -> str:
        source = extraction.get("source", "")
        if source == "huggingface_qwen3_vl":
            return self.backend.settings.hf_model.split("/")[-1]
        return "Pending"

    def _policy_engine_label(self, snapshot) -> str:
        if snapshot.policy_review is None:
            return "Pending"
        if any("local fallback" in warning.lower() for warning in snapshot.policy_review.warnings):
            return "Local rules"
        if self.backend.settings.hf_api_token:
            return self.backend.settings.hf_model.split("/")[-1]
        return "Local rules"

    @staticmethod
    def _replace_text(widget: ctk.CTkTextbox, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _on_close(self) -> None:
        if self.backend.camera is not None:
            self.backend.camera.release()
        self.root.destroy()

    def _show_shortcuts(self) -> None:
        messagebox.showinfo(
            "Keyboard Shortcuts",
            "1 / 2 / 3  Select company\n"
            "C  Start or stop camera\n"
            "Space  Capture photo\n"
            "U  Load photo\n"
            "A  Analyze receipt\n"
            "G  Start processing\n"
            "R  Clear current receipt\n"
            "Q  Quit",
            parent=self.root,
        )

    def run(self) -> None:
        self.root.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CustomTkinter consultant-side intake app.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011", help="Base URL where the company portals are served.")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index for OpenCV VideoCapture.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ConsultantTkApp(base_url=args.base_url, camera_index=args.camera_index).run()


if __name__ == "__main__":
    main()
