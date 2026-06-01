import os
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from bd_iso_state import (
    BdIsoStateStore,
    GRID_COLUMNS,
    GRID_PAGE_SIZE,
    GRID_ROWS,
    STATUS_COPIED,
    STATUS_FAILED,
    STATUS_NOT_TRIED,
    STATUS_VERIFIED,
    STATUS_ZERO_FILLED,
    chunk_offset_for_index,
    chunk_page_bounds,
    get_state_path,
    page_count_for_chunks,
)
from bd_utils import close_tray, eject_disc, get_block_device_size, resolve_source_path, touch_disc
from compare_bd_iso import DEFAULT_CHUNK_SIZE, verify_iso_range, compare_bd_to_iso, format_bytes
from create_iso_from_bd import (
    DEFAULT_MIN_READ_SIZE,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY,
    create_iso_from_bd,
    copy_iso_range_from_bd,
)


STATUS_COLORS = {
    STATUS_NOT_TRIED: "#9ca3af",
    STATUS_ZERO_FILLED: "#eab308",
    STATUS_COPIED: "#3b82f6",
    STATUS_FAILED: "#ef4444",
    STATUS_VERIFIED: "#22c55e",
}
BLOCK_GRID_BACKGROUND = "#fff7cc"
ENTRY_TEXT_COLOR = "#111827"
ENTRY_PLACEHOLDER_COLOR = "#6b7280"

BASE_FONT_SIZE = 13
HEADING_FONT_SIZE = 14
BUTTON_FONT_SIZE = 12
CELL_SIZE = 15
CELL_GAP = 2
CANVAS_PADDING = 12
GRID_CANVAS_WIDTH = CANVAS_PADDING * 2 + GRID_COLUMNS * (CELL_SIZE + CELL_GAP) - CELL_GAP
GRID_CANVAS_HEIGHT = CANVAS_PADDING * 2 + GRID_ROWS * (CELL_SIZE + CELL_GAP) - CELL_GAP
SCREEN_PADDING = 80
MAIN_WINDOW_WIDTH = max(1100, GRID_CANVAS_WIDTH + 160)
MAIN_WINDOW_HEIGHT = GRID_CANVAS_HEIGHT + 250
DETAIL_WINDOW_WIDTH = max(1000, GRID_CANVAS_WIDTH + 160)
DETAIL_WINDOW_HEIGHT = GRID_CANVAS_HEIGHT + 250
POLL_INTERVAL_MS = 125


def set_default_window_size(window, desired_width, desired_height):
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    width = min(desired_width, max(800, screen_width - SCREEN_PADDING))
    height = min(desired_height, max(700, screen_height - SCREEN_PADDING))
    window.geometry(f"{width}x{height}")
    window.minsize(width, height)


class PlaceholderEntry(tk.Entry):
    def __init__(self, master, *, textvariable, placeholder, **kwargs):
        super().__init__(
            master,
            fg=ENTRY_TEXT_COLOR,
            insertbackground=ENTRY_TEXT_COLOR,
            relief="solid",
            bd=1,
            **kwargs,
        )
        self.variable = textvariable
        self.placeholder = placeholder
        self.placeholder_visible = False
        self.syncing = False

        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<KeyRelease>", self._on_key_release)
        self.variable.trace_add("write", self._on_variable_changed)
        self._refresh_from_variable()

    def commit(self):
        if not self.placeholder_visible:
            self._set_variable(self.get())

    def _on_focus_in(self, _event=None):
        if self.placeholder_visible:
            self.delete(0, tk.END)
            self.placeholder_visible = False
            self.configure(fg=ENTRY_TEXT_COLOR)

    def _on_focus_out(self, _event=None):
        self.commit()
        if not self.variable.get():
            self._show_placeholder()

    def _on_key_release(self, _event=None):
        if not self.placeholder_visible:
            self._set_variable(self.get())

    def _on_variable_changed(self, *_args):
        if not self.syncing:
            self._refresh_from_variable()

    def _set_variable(self, value):
        if self.variable.get() == value:
            return
        self.syncing = True
        try:
            self.variable.set(value)
        finally:
            self.syncing = False

    def _refresh_from_variable(self):
        value = self.variable.get()
        self.delete(0, tk.END)
        if value:
            self.placeholder_visible = False
            self.configure(fg=ENTRY_TEXT_COLOR)
            self.insert(0, value)
        elif self.focus_get() == self:
            self.placeholder_visible = False
            self.configure(fg=ENTRY_TEXT_COLOR)
        else:
            self._show_placeholder()

    def _show_placeholder(self):
        self.delete(0, tk.END)
        self.placeholder_visible = True
        self.configure(fg=ENTRY_PLACEHOLDER_COLOR)
        self.insert(0, self.placeholder)


class BdIsoController:
    def __init__(self):
        self.event_queue = queue.Queue()
        self.worker_thread = None
        self.cancel_event = None
        self.active_job = None
        self.store = None
        self.last_message = "Idle."
        self.current_source_path = ""
        self.current_iso_path = ""

    def _observer(self, event, **payload):
        self.event_queue.put({"type": "worker_event", "event": event, **payload})

    def _set_paths(self, source_path, iso_path):
        self.current_source_path = source_path
        self.current_iso_path = iso_path

    def _get_state_path(self, iso_path):
        return get_state_path(iso_path)

    def _resolve_device_size(self, source_path, iso_path):
        if source_path:
            try:
                return get_block_device_size(os.path.realpath(source_path))
            except Exception:
                pass
        if iso_path and os.path.exists(iso_path):
            try:
                return os.path.getsize(iso_path)
            except OSError:
                pass
        return None

    def resolve_active_source_path(self, source_path, iso_path):
        candidate = source_path or ""
        expected_size = None

        if self.store is not None:
            stored_size = self.store.state.get("device_size")
            if stored_size:
                expected_size = stored_size
            if not candidate:
                candidate = self.store.state.get("source_path") or ""

        if expected_size is None:
            expected_size = self._resolve_device_size(candidate, iso_path)

        resolved = resolve_source_path(candidate, expected_size=expected_size)
        self.current_source_path = resolved
        if resolved and resolved != (source_path or "") and self.active_job is None:
            self.last_message = f"Using auto-detected drive {resolved}."
        return resolved

    def load_state(self, source_path, iso_path):
        self._set_paths(source_path, iso_path)
        if not iso_path:
            self.store = None
            return None

        state_path = self._get_state_path(iso_path)
        if os.path.exists(state_path):
            self.store = BdIsoStateStore.load(iso_path, state_path=state_path)
            try:
                resolved_source = self.resolve_active_source_path(source_path, iso_path)
            except Exception:
                resolved_source = source_path or self.store.state.get("source_path") or ""
            if resolved_source:
                self.current_source_path = resolved_source
                if self.store.state.get("source_path") != resolved_source:
                    self.store.ensure_metadata(source_path=resolved_source)
                    self.store.save()
            return self.store

        if (
            os.path.exists(iso_path)
            or os.path.exists(f"{iso_path}.copy.txt")
            or os.path.exists(f"{iso_path}.txt")
        ):
            device_size = self._resolve_device_size(source_path, iso_path)
            resolved_source = source_path or ""
            try:
                resolved_source = resolve_source_path(source_path or "", expected_size=device_size)
            except Exception:
                pass
            self.store = BdIsoStateStore.load_or_hydrate(
                iso_path,
                source_path=resolved_source or None,
                device_size=device_size,
                chunk_size=DEFAULT_CHUNK_SIZE,
                state_path=state_path,
            )
            self.current_source_path = resolved_source or source_path
            return self.store

        self.store = None
        return None

    def refresh_state(self):
        if not self.current_iso_path:
            self.store = None
            return None
        return self.load_state(self.current_source_path, self.current_iso_path)

    def _validate_fixed_chunk_size(self):
        if self.store is not None and self.store.chunk_size != DEFAULT_CHUNK_SIZE:
            raise ValueError(
                "The UI is fixed to 4 MiB chunks. The current state/logs use a different chunk size."
            )

    def _run_in_thread(self, label, target, *args, **kwargs):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            raise RuntimeError("A job is already running.")

        self.cancel_event = threading.Event()
        self.active_job = label
        kwargs["observer"] = self._observer
        kwargs["cancel_event"] = self.cancel_event

        def runner():
            try:
                result = target(*args, **kwargs)
                self.event_queue.put(
                    {"type": "job_finished", "job": label, "result": bool(result)}
                )
            except Exception as exc:
                self.event_queue.put(
                    {"type": "job_error", "job": label, "error": str(exc)}
                )

        self.worker_thread = threading.Thread(target=runner, daemon=True)
        self.worker_thread.start()

    def start_copy(self, source_path, iso_path, *, resume=False, restart=False):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        state_path = self._get_state_path(iso_path)
        self._run_in_thread(
            "copy",
            create_iso_from_bd,
            source_path,
            iso_path,
            chunk_size=DEFAULT_CHUNK_SIZE,
            retries=DEFAULT_RETRIES,
            retry_delay=DEFAULT_RETRY_DELAY,
            min_read_size=DEFAULT_MIN_READ_SIZE,
            resume=resume,
            restart=restart,
            verify=False,
            state_path=state_path,
        )

    def verify_from_chunk(self, source_path, iso_path, chunk_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = chunk_offset_for_index(chunk_index, self.store.chunk_size)
        state_path = self._get_state_path(iso_path)
        self._run_in_thread(
            "verify_from_here",
            compare_bd_to_iso,
            source_path,
            iso_path,
            chunk_size=DEFAULT_CHUNK_SIZE,
            start_offset=offset,
            state_path=state_path,
        )

    def eject_disc(self, source_path, iso_path):
        self.load_state(source_path, iso_path)
        source_path = self.resolve_active_source_path(source_path, iso_path)
        self._run_in_thread("eject_disc", self._eject_disc_job, source_path)

    def close_tray(self, source_path, iso_path):
        self.load_state(source_path, iso_path)
        source_path = self.resolve_active_source_path(source_path, iso_path)
        self._run_in_thread("close_tray", self._close_tray_job, source_path)

    def touch_disc(self, source_path, iso_path):
        self.load_state(source_path, iso_path)
        source_path = self.resolve_active_source_path(source_path, iso_path)
        expected_size = None
        if self.store is not None:
            expected_size = self.store.state.get("device_size")
        self._run_in_thread(
            "touch_disc",
            self._touch_disc_job,
            source_path,
            expected_size,
        )

    def _eject_disc_job(self, source_path, *, observer=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        eject_disc(source_path)
        if observer is not None:
            observer("disc_ejected", source_path=source_path)
        return True

    def _close_tray_job(self, source_path, *, observer=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        close_tray(source_path)
        if observer is not None:
            observer("tray_closed", source_path=source_path)
        return True

    def _touch_disc_job(self, source_path, expected_size, *, observer=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return False
        message = touch_disc(source_path, expected_size=expected_size)
        if observer is not None:
            observer("disc_touched", source_path=source_path, message=message)
        return True

    def retry_chunk(self, source_path, iso_path, chunk_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = chunk_offset_for_index(chunk_index, self.store.chunk_size)
        size = self.store.chunk_size_for_index(chunk_index)
        self._run_in_thread(
            "retry_chunk",
            copy_iso_range_from_bd,
            source_path,
            iso_path,
            offset,
            size,
            state_path=self._get_state_path(iso_path),
            scope="chunk",
        )

    def resume_from_chunk(self, source_path, iso_path, chunk_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = self.store.prepare_resume_from_chunk(chunk_index)
        self._run_in_thread(
            "resume_here",
            create_iso_from_bd,
            source_path,
            iso_path,
            chunk_size=DEFAULT_CHUNK_SIZE,
            retries=DEFAULT_RETRIES,
            retry_delay=DEFAULT_RETRY_DELAY,
            min_read_size=DEFAULT_MIN_READ_SIZE,
            resume=True,
            verify=False,
            state_path=self._get_state_path(iso_path),
            start_offset_override=offset,
        )

    def retry_from_chunk(self, source_path, iso_path, chunk_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset, _stop_offset = self.store.prepare_retry_from_chunk(chunk_index)
        self._run_in_thread(
            "retry_from_here",
            create_iso_from_bd,
            source_path,
            iso_path,
            chunk_size=DEFAULT_CHUNK_SIZE,
            retries=DEFAULT_RETRIES,
            retry_delay=DEFAULT_RETRY_DELAY,
            min_read_size=DEFAULT_MIN_READ_SIZE,
            resume=True,
            verify=False,
            state_path=self._get_state_path(iso_path),
            start_offset_override=offset,
            prepare_resume_state=False,
        )

    def retry_next_chunks(self, source_path, iso_path, chunk_index, chunk_count):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        if chunk_count <= 0:
            raise ValueError("Chunk count must be greater than 0.")
        offset, stop_offset = self.store.prepare_retry_from_chunk(
            chunk_index,
            max_chunks=chunk_count,
        )
        self._run_in_thread(
            "retry_next_chunks",
            create_iso_from_bd,
            source_path,
            iso_path,
            chunk_size=DEFAULT_CHUNK_SIZE,
            retries=DEFAULT_RETRIES,
            retry_delay=DEFAULT_RETRY_DELAY,
            min_read_size=DEFAULT_MIN_READ_SIZE,
            resume=True,
            verify=False,
            state_path=self._get_state_path(iso_path),
            start_offset_override=offset,
            stop_offset_override=stop_offset,
            prepare_resume_state=False,
        )

    def verify_chunk(self, source_path, iso_path, chunk_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = chunk_offset_for_index(chunk_index, self.store.chunk_size)
        size = self.store.chunk_size_for_index(chunk_index)
        self._run_in_thread(
            "verify_chunk",
            verify_iso_range,
            source_path,
            iso_path,
            offset,
            size,
            state_path=self._get_state_path(iso_path),
        )

    def retry_sector(self, source_path, iso_path, chunk_index, sector_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = (
            chunk_offset_for_index(chunk_index, self.store.chunk_size)
            + sector_index * self.store.sector_size
        )
        size = min(
            self.store.sector_size,
            self.store.chunk_size_for_index(chunk_index) - sector_index * self.store.sector_size,
        )
        self._run_in_thread(
            "retry_sector",
            copy_iso_range_from_bd,
            source_path,
            iso_path,
            offset,
            size,
            state_path=self._get_state_path(iso_path),
            scope="sector",
        )

    def verify_sector(self, source_path, iso_path, chunk_index, sector_index):
        self.load_state(source_path, iso_path)
        self._validate_fixed_chunk_size()
        source_path = self.resolve_active_source_path(source_path, iso_path)
        if self.store is None:
            raise RuntimeError("No state is loaded.")
        offset = (
            chunk_offset_for_index(chunk_index, self.store.chunk_size)
            + sector_index * self.store.sector_size
        )
        size = min(
            self.store.sector_size,
            self.store.chunk_size_for_index(chunk_index) - sector_index * self.store.sector_size,
        )
        self._run_in_thread(
            "verify_sector",
            verify_iso_range,
            source_path,
            iso_path,
            offset,
            size,
            state_path=self._get_state_path(iso_path),
        )

    def stop(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.last_message = "Stopping current job..."

    def handle_message(self, message):
        message_type = message.get("type")
        if message_type == "worker_event":
            self.last_message = self._message_for_event(message)
            self.refresh_state()
        elif message_type == "job_finished":
            self.active_job = None
            if message.get("result") and message["job"] in {
                "eject_disc",
                "close_tray",
                "touch_disc",
            }:
                return self.last_message
            self.last_message = (
                f"{message['job']} finished successfully."
                if message.get("result")
                else f"{message['job']} finished with a failure."
            )
            self.refresh_state()
        elif message_type == "job_error":
            self.active_job = None
            self.last_message = f"{message['job']} crashed: {message['error']}"
        return self.last_message

    def _message_for_event(self, message):
        event = message.get("event")
        if event == "copy_start":
            return "Copy started."
        if event == "copy_progress":
            if message.get("skipped"):
                status = message.get("chunk_status")
                action = (
                    "Zero-filling marked chunks"
                    if status == STATUS_ZERO_FILLED
                    else "Skipping already completed chunks"
                )
                return (
                    f"{action}: {format_bytes(message['current'])} / "
                    f"{format_bytes(message['total'])}"
                )
            return (
                f"Copying {format_bytes(message['current'])} / "
                f"{format_bytes(message['total'])}"
            )
        if event == "copy_chunk_failed":
            return (
                f"Copy failed at {message['offset']} "
                f"({format_bytes(message['size'])})"
            )
        if event == "copy_complete":
            if message.get("range_limited"):
                return "Retry range complete."
            return "Copy complete."
        if event == "verify_start":
            return "Verification started."
        if event == "verify_progress":
            return (
                f"Verified {format_bytes(message['current'])} / "
                f"{format_bytes(message['total'])}"
            )
        if event == "verify_chunk_failed":
            return f"Mismatch at chunk offset {message['offset']}."
        if event == "verify_complete":
            return "Verification complete." if message.get("success") else "Verification found mismatches."
        if event == "disc_ejected":
            return f"Ejected {message['source_path']}."
        if event == "tray_closed":
            return f"Closed tray for {message['source_path']}."
        if event == "disc_touched":
            return message["message"]
        return event or "Working..."


class ChunkDetailWindow(tk.Toplevel):
    def __init__(self, app, chunk_index):
        super().__init__(app)
        self.app = app
        self.chunk_index = chunk_index
        self.selected_sector_index = None
        self.retry_count_var = tk.StringVar(value="5")
        self.title(f"Chunk {chunk_index}")
        set_default_window_size(self, DETAIL_WINDOW_WIDTH, DETAIL_WINDOW_HEIGHT)
        self.resizable(True, True)

        self.info_var = tk.StringVar()
        self.selected_var = tk.StringVar(value="No sector selected.")

        info_label = ttk.Label(self, textvariable=self.info_var, justify="left")
        info_label.pack(fill="x", padx=12, pady=(12, 8))

        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=12, pady=(0, 8))

        self.retry_chunk_button = ttk.Button(
            action_frame,
            text="Retry Chunk",
            command=self._retry_chunk,
        )
        self.retry_chunk_button.pack(side="left", padx=(0, 8))

        self.resume_here_button = ttk.Button(
            action_frame,
            text="Resume Here",
            command=self._resume_here,
        )
        self.resume_here_button.pack(side="left", padx=(0, 8))

        self.retry_from_here_button = ttk.Button(
            action_frame,
            text="Retry From Here",
            command=self._retry_from_here,
        )
        self.retry_from_here_button.pack(side="left", padx=(0, 8))

        self.verify_chunk_button = ttk.Button(
            action_frame,
            text="Verify Chunk",
            command=self._verify_chunk,
        )
        self.verify_chunk_button.pack(side="left", padx=(0, 8))

        self.retry_sector_button = ttk.Button(
            action_frame,
            text="Retry Sector",
            command=self._retry_sector,
            state="disabled",
        )
        self.retry_sector_button.pack(side="left", padx=(0, 8))

        self.verify_sector_button = ttk.Button(
            action_frame,
            text="Verify Sector",
            command=self._verify_sector,
            state="disabled",
        )
        self.verify_sector_button.pack(side="left")

        retry_range_frame = ttk.Frame(self)
        retry_range_frame.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(retry_range_frame, text="Retry next").pack(side="left")
        self.retry_count_entry = ttk.Entry(
            retry_range_frame,
            textvariable=self.retry_count_var,
            width=6,
        )
        self.retry_count_entry.pack(side="left", padx=(6, 6))
        ttk.Label(retry_range_frame, text="chunks from here").pack(side="left")
        self.retry_next_button = ttk.Button(
            retry_range_frame,
            text="Retry",
            command=self._retry_next_chunks,
        )
        self.retry_next_button.pack(side="left", padx=(8, 0))

        selected_label = ttk.Label(self, textvariable=self.selected_var)
        selected_label.pack(fill="x", padx=12, pady=(0, 8))

        self.canvas = tk.Canvas(
            self,
            width=GRID_CANVAS_WIDTH,
            height=GRID_CANVAS_HEIGHT,
            background=BLOCK_GRID_BACKGROUND,
            highlightthickness=0,
        )
        self.canvas.pack(padx=12, pady=(0, 12))
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.item_to_sector = {}

        self.refresh()

    def refresh(self):
        store = self.app.controller.store
        if store is None or self.chunk_index >= store.chunk_count:
            self.destroy()
            return

        info = store.chunk_info(self.chunk_index)
        self.info_var.set(
            "\n".join(
                [
                    f"Chunk {self.chunk_index}",
                    f"Offset: {info['offset']} (0x{info['offset']:X})",
                    f"Size: {format_bytes(info['size'])}",
                    f"Status: {info['status']}",
                    f"Retries: {info['retry_count']} | Verifies: {info['verify_count']}",
                    f"Last Error: {info['last_error'] or '-'}",
                ]
            )
        )
        self._draw_sector_grid()
        self._refresh_buttons()

    def _draw_sector_grid(self):
        store = self.app.controller.store
        self.canvas.delete("all")
        self.item_to_sector.clear()
        sector_count = store.sector_count_for_chunk(self.chunk_index)

        for sector_index in range(sector_count):
            row = sector_index // GRID_COLUMNS
            col = sector_index % GRID_COLUMNS
            x1 = CANVAS_PADDING + col * (CELL_SIZE + CELL_GAP)
            y1 = CANVAS_PADDING + row * (CELL_SIZE + CELL_GAP)
            x2 = x1 + CELL_SIZE
            y2 = y1 + CELL_SIZE
            status = store.effective_sector_status(self.chunk_index, sector_index)
            outline = "#f8fafc" if sector_index == self.selected_sector_index else ""
            item_id = self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=STATUS_COLORS[status],
                outline=outline,
                width=2 if outline else 0,
            )
            self.item_to_sector[item_id] = sector_index

    def _on_canvas_click(self, event):
        item_id = self.canvas.find_closest(event.x, event.y)
        if not item_id:
            return
        sector_index = self.item_to_sector.get(item_id[0])
        if sector_index is None:
            return
        self.selected_sector_index = sector_index
        self.selected_var.set(f"Selected sector: {sector_index}")
        self._draw_sector_grid()
        self._refresh_buttons()

    def _refresh_buttons(self):
        has_sector = self.selected_sector_index is not None
        running = self.app.controller.active_job is not None
        sector_state = "normal" if has_sector and not running else "disabled"
        chunk_state = "normal" if not running else "disabled"
        self.retry_chunk_button.configure(state=chunk_state)
        self.resume_here_button.configure(state=chunk_state)
        self.retry_from_here_button.configure(state=chunk_state)
        self.retry_next_button.configure(state=chunk_state)
        self.retry_count_entry.configure(state=chunk_state)
        self.verify_chunk_button.configure(state=chunk_state)
        self.retry_sector_button.configure(state=sector_state)
        self.verify_sector_button.configure(state=sector_state)

    def _retry_chunk(self):
        self.app.run_action(lambda: self.app.controller.retry_chunk(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
        ))

    def _resume_here(self):
        self.app.run_action(lambda: self.app.controller.resume_from_chunk(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
        ))

    def _retry_from_here(self):
        self.app.run_action(lambda: self.app.controller.retry_from_chunk(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
        ))

    def _retry_next_chunks(self):
        def action():
            try:
                chunk_count = int(self.retry_count_var.get().strip(), 0)
            except ValueError as exc:
                raise ValueError("Retry chunk count must be an integer.") from exc
            self.app.controller.retry_next_chunks(
                self.app.device_var.get().strip(),
                self.app.iso_var.get().strip(),
                self.chunk_index,
                chunk_count,
            )

        self.app.run_action(action)

    def _verify_chunk(self):
        self.app.run_action(lambda: self.app.controller.verify_chunk(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
        ))

    def _retry_sector(self):
        if self.selected_sector_index is None:
            return
        self.app.run_action(lambda: self.app.controller.retry_sector(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
            self.selected_sector_index,
        ))

    def _verify_sector(self):
        if self.selected_sector_index is None:
            return
        self.app.run_action(lambda: self.app.controller.verify_sector(
            self.app.device_var.get().strip(),
            self.app.iso_var.get().strip(),
            self.chunk_index,
            self.selected_sector_index,
        ))


class BdIsoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BD ISO Progress")
        set_default_window_size(self, MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)

        self.controller = BdIsoController()
        self.device_var = tk.StringVar()
        self.iso_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle.")
        self.summary_var = tk.StringVar(value="No state loaded.")
        self.page_var = tk.StringVar(value="Page 0 / 0")
        self.current_page = 0
        self.selected_chunk_index = None
        self.detail_window = None
        self.item_to_chunk = {}

        self._configure_fonts()
        self._build_layout()
        self.after(POLL_INTERVAL_MS, self._poll_events)

    def _configure_fonts(self):
        default_font = tkfont.nametofont("TkDefaultFont")
        text_font = tkfont.nametofont("TkTextFont")
        heading_font = tkfont.nametofont("TkHeadingFont")
        fixed_font = tkfont.nametofont("TkFixedFont")

        default_font.configure(size=BASE_FONT_SIZE)
        text_font.configure(size=BASE_FONT_SIZE)
        heading_font.configure(size=HEADING_FONT_SIZE)
        fixed_font.configure(size=BASE_FONT_SIZE)

        style = ttk.Style(self)
        style.configure(".", font=default_font)
        style.configure("TButton", font=("TkDefaultFont", BUTTON_FONT_SIZE), padding=(10, 6))
        style.configure("TLabel", font=("TkDefaultFont", BASE_FONT_SIZE))
        style.configure("TEntry", font=("TkDefaultFont", BASE_FONT_SIZE))

    def _build_layout(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        self.device_entry = PlaceholderEntry(
            top,
            textvariable=self.device_var,
            placeholder="Drive path (blank = auto)",
            width=34,
        )
        self.device_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        auto_button = ttk.Button(top, text="Find Drive", command=self._auto_detect_device)
        auto_button.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        self.iso_entry = PlaceholderEntry(
            top,
            textvariable=self.iso_var,
            placeholder="ISO path",
            width=54,
        )
        self.iso_entry.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        browse_button = ttk.Button(top, text="Choose ISO", command=self._browse_iso)
        browse_button.grid(row=0, column=3, sticky="ew")

        top.columnconfigure(0, weight=1)
        top.columnconfigure(2, weight=2)

        button_row = ttk.Frame(self, padding=(12, 0, 12, 8))
        button_row.pack(fill="x")

        self.start_button = ttk.Button(button_row, text="Start Copy", command=self._start_copy)
        self.start_button.pack(side="left", padx=(0, 8))
        self.resume_button = ttk.Button(button_row, text="Resume", command=self._resume_copy)
        self.resume_button.pack(side="left", padx=(0, 8))
        self.restart_button = ttk.Button(button_row, text="Restart", command=self._restart_copy)
        self.restart_button.pack(side="left", padx=(0, 8))
        self.verify_button = ttk.Button(button_row, text="Verify From Here", command=self._verify_from_here)
        self.verify_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(button_row, text="Refresh", command=self._refresh_state)
        self.refresh_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(button_row, text="Stop", command=self.controller.stop)
        self.stop_button.pack(side="left", padx=(0, 8))
        self.eject_button = ttk.Button(button_row, text="Eject Disc", command=self._eject_disc)
        self.eject_button.pack(side="left", padx=(0, 8))
        self.close_tray_button = ttk.Button(button_row, text="Close Tray", command=self._close_tray)
        self.close_tray_button.pack(side="left", padx=(0, 8))
        self.touch_disc_button = ttk.Button(button_row, text="Touch Disc", command=self._touch_disc)
        self.touch_disc_button.pack(side="left")

        ttk.Label(self, textvariable=self.status_var, padding=(12, 0)).pack(fill="x")
        ttk.Label(self, textvariable=self.summary_var, padding=(12, 4)).pack(fill="x")

        nav_legend_row = ttk.Frame(self, padding=(12, 8))
        nav_legend_row.pack(fill="x")

        page_frame = ttk.Frame(nav_legend_row)
        page_frame.pack(side="left")
        ttk.Button(page_frame, text="Prev", command=self._prev_page).pack(side="left")
        ttk.Label(page_frame, textvariable=self.page_var).pack(side="left", padx=12)
        ttk.Button(page_frame, text="Next", command=self._next_page).pack(side="left")

        legend = ttk.Frame(nav_legend_row)
        legend.pack(side="left", padx=(24, 0))
        for status, label in [
            (STATUS_NOT_TRIED, "Not tried"),
            (STATUS_ZERO_FILLED, "Zero filled"),
            (STATUS_COPIED, "Copied"),
            (STATUS_FAILED, "Failed"),
            (STATUS_VERIFIED, "Verified"),
        ]:
            swatch = tk.Canvas(legend, width=12, height=12, highlightthickness=0)
            swatch.create_rectangle(0, 0, 12, 12, fill=STATUS_COLORS[status], outline="")
            swatch.pack(side="left")
            ttk.Label(legend, text=label).pack(side="left", padx=(4, 12))

        self.canvas = tk.Canvas(
            self,
            width=GRID_CANVAS_WIDTH,
            height=GRID_CANVAS_HEIGHT,
            background=BLOCK_GRID_BACKGROUND,
            highlightthickness=0,
        )
        self.canvas.pack(padx=12, pady=(0, 12))
        self.canvas.bind("<Button-1>", self._on_canvas_click)

    def run_action(self, action):
        try:
            action()
            self._refresh_state()
            self._refresh_buttons()
        except Exception as exc:
            messagebox.showerror("BD ISO Progress", str(exc))

    def _browse_iso(self):
        iso_path = filedialog.askopenfilename(
            title="Select ISO path",
            filetypes=[("ISO image", "*.iso"), ("All files", "*.*")],
        )
        if iso_path:
            self.iso_var.set(iso_path)
            self._refresh_state()

    def _commit_path_entries(self):
        if hasattr(self, "device_entry"):
            self.device_entry.commit()
        if hasattr(self, "iso_entry"):
            self.iso_entry.commit()

    def _auto_detect_device(self):
        self._commit_path_entries()
        try:
            resolved = self.controller.resolve_active_source_path(
                self.device_var.get().strip(),
                self.iso_var.get().strip(),
            )
        except Exception as exc:
            messagebox.showerror("BD ISO Progress", str(exc))
            return
        self.device_var.set(resolved)
        self.status_var.set(f"Using {resolved}.")
        if self.iso_var.get().strip():
            self._refresh_state()

    def _require_paths(self):
        self._commit_path_entries()
        source_path = self.device_var.get().strip()
        iso_path = self.iso_var.get().strip()
        if not iso_path:
            raise ValueError("ISO path is required.")
        return source_path, iso_path

    def _disc_action_paths(self):
        self._commit_path_entries()
        return self.device_var.get().strip(), self.iso_var.get().strip()

    def _start_copy(self):
        self.run_action(lambda: self.controller.start_copy(*self._require_paths()))

    def _resume_copy(self):
        self.run_action(lambda: self.controller.start_copy(*self._require_paths(), resume=True))

    def _restart_copy(self):
        self.run_action(lambda: self.controller.start_copy(*self._require_paths(), restart=True))

    def _verify_from_here(self):
        if self.selected_chunk_index is None:
            messagebox.showerror("BD ISO Progress", "Select a chunk first.")
            return
        self.run_action(lambda: self.controller.verify_from_chunk(
            *self._require_paths(),
            self.selected_chunk_index,
        ))

    def _eject_disc(self):
        self.run_action(lambda: self.controller.eject_disc(*self._disc_action_paths()))

    def _close_tray(self):
        self.run_action(lambda: self.controller.close_tray(*self._disc_action_paths()))

    def _touch_disc(self):
        self.run_action(lambda: self.controller.touch_disc(*self._disc_action_paths()))

    def _refresh_state(self):
        self._commit_path_entries()
        try:
            source_path, iso_path = self._require_paths()
        except ValueError:
            source_path = self.device_var.get().strip()
            iso_path = self.iso_var.get().strip()
        self.controller.load_state(source_path, iso_path)
        self._refresh_ui()

    def _refresh_ui(self):
        store = self.controller.store
        self.status_var.set(self.controller.last_message)
        if self.controller.current_source_path and self.device_var.get().strip() != self.controller.current_source_path:
            self.device_var.set(self.controller.current_source_path)
        if store is None:
            self.summary_var.set("No state loaded.")
            self.page_var.set("Page 0 / 0")
            self.canvas.delete("all")
            return

        counts = store.summarize_counts()
        self.summary_var.set(
            " | ".join(
                [
                    f"Chunks: {store.chunk_count}",
                    f"Not tried: {counts[STATUS_NOT_TRIED]}",
                    f"Zero filled: {counts[STATUS_ZERO_FILLED]}",
                    f"Copied: {counts[STATUS_COPIED]}",
                    f"Failed: {counts[STATUS_FAILED]}",
                    f"Verified: {counts[STATUS_VERIFIED]}",
                ]
            )
        )

        total_pages = max(page_count_for_chunks(store.chunk_count), 1)
        self.current_page = min(self.current_page, total_pages - 1)
        self.page_var.set(f"Page {self.current_page + 1} / {total_pages}")
        self._draw_overview()
        self._refresh_buttons()

        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.refresh()
        else:
            self.detail_window = None

    def _draw_overview(self):
        store = self.controller.store
        self.canvas.delete("all")
        self.item_to_chunk.clear()
        if store is None:
            return

        page_start, page_end = chunk_page_bounds(self.current_page, store.chunk_count)
        for page_offset, chunk_index in enumerate(range(page_start, page_end)):
            row = page_offset // GRID_COLUMNS
            col = page_offset % GRID_COLUMNS
            x1 = CANVAS_PADDING + col * (CELL_SIZE + CELL_GAP)
            y1 = CANVAS_PADDING + row * (CELL_SIZE + CELL_GAP)
            x2 = x1 + CELL_SIZE
            y2 = y1 + CELL_SIZE
            status = store.effective_chunk_status(chunk_index)
            outline = "#f8fafc" if chunk_index == self.selected_chunk_index else ""
            item_id = self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=STATUS_COLORS[status],
                outline=outline,
                width=2 if outline else 0,
            )
            self.item_to_chunk[item_id] = chunk_index

    def _on_canvas_click(self, event):
        item_id = self.canvas.find_closest(event.x, event.y)
        if not item_id:
            return
        chunk_index = self.item_to_chunk.get(item_id[0])
        if chunk_index is None:
            return
        self.selected_chunk_index = chunk_index
        self._draw_overview()
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.destroy()
        self.detail_window = ChunkDetailWindow(self, chunk_index)

    def _prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self._refresh_ui()

    def _next_page(self):
        store = self.controller.store
        if store is None:
            return
        max_page = max(page_count_for_chunks(store.chunk_count) - 1, 0)
        if self.current_page < max_page:
            self.current_page += 1
            self._refresh_ui()

    def _refresh_buttons(self):
        running = self.controller.active_job is not None
        default_state = "disabled" if running else "normal"
        self.start_button.configure(state=default_state)
        self.resume_button.configure(state=default_state)
        self.restart_button.configure(state=default_state)
        self.verify_button.configure(state=default_state)
        self.refresh_button.configure(state="normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.eject_button.configure(state=default_state)
        self.close_tray_button.configure(state=default_state)
        self.touch_disc_button.configure(state=default_state)

    def _poll_events(self):
        updated = False
        while True:
            try:
                message = self.controller.event_queue.get_nowait()
            except queue.Empty:
                break
            self.controller.handle_message(message)
            updated = True
        if updated:
            self._refresh_ui()
        self.after(POLL_INTERVAL_MS, self._poll_events)


def main():
    app = BdIsoApp()
    app.mainloop()


if __name__ == "__main__":
    main()
