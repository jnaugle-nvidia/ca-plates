#!/usr/bin/env python3
"""
CA Personalized Plate Checker
=============================

A small tkinter GUI (standard library only, no pip installs) that asks the
California DMV's own "Special Interest & Personalized License Plates" web
application whether a plate configuration is available.

It talks to the same endpoint the DMV page uses when you press
"Check Availability":

    GET  https://www.dmv.ca.gov/wasapp/ipp2/initPers.do    -> starts a session
    POST https://www.dmv.ca.gov/wasapp/ipp2/startPers.do   -> acknowledges the terms page
    POST https://www.dmv.ca.gov/wasapp/ipp2/checkPers.do   -> {"success":true,"code":"AVAILABLE",...}

Typing rules in the plate box (one plate per line):
    letters / digits   plate characters (case-insensitive)
    space              a full space (an empty slot on the plate)
    /                  a half space (on 7-char types it gets its own extra slot)
    *                  where the Kids-plate symbol goes (Kids plate only; defaults to the first slot)

Usage:
    python ca_plate_checker.py                      # GUI
    python ca_plate_checker.py --cli LUVCATS 2FAST  # terminal mode, same engine
    python ca_plate_checker.py --cli --type Q --vehicle AUTO PINK1

The DMV decides availability; this tool only asks. Keep the delay between
checks reasonable so the DMV site is not hammered.
"""

import argparse
import csv
import http.cookiejar
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.dmv.ca.gov/wasapp/ipp2/"
INIT_URL = BASE_URL + "initPers.do"
START_URL = BASE_URL + "startPers.do"
CHECK_URL = BASE_URL + "checkPers.do"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25  # seconds

# code, display name, max characters, order fee, annual renewal fee
PLATE_TYPES = [
    ("R", "Environmental", 7, 53, 43),
    ("Q", "Breast Cancer Awareness", 6, 103, 83),
    ("J", "California Museums", 6, 103, 83),
    ("Z", "California 1960s Legacy", 7, 50, 40),
    ("I", "Pet Lover's", 6, 103, 83),
    ("D", "California Agriculture", 6, 103, 83),
    ("G", "California Memorial", 6, 103, 83),
    ("W", "California Coastal Commission", 7, 103, 83),
    ("H", "Lake Tahoe Conservancy", 7, 103, 83),
    ("Y", "Yosemite Foundation", 7, 103, 83),
    ("A", "California Arts Council", 6, 103, 83),
    ("V", "Veterans' Organization", 6, 78, 40),
    ("K", "Kids", 7, 50, 40),  # 1 symbol + 2-6 characters
]
PLATE_BY_CODE = {p[0]: p for p in PLATE_TYPES}

VEHICLE_TYPES = [
    ("AUTO", "Auto"),
    ("COMM", "Commercial"),
    ("TRAI", "Trailer"),
    ("MOTO", "Motorcycle"),
]
# Trailers and motorcycles can only order the Environmental plate.
ENVIRONMENTAL_ONLY_VEHICLES = {"TRAI", "MOTO"}

KIDS_SYMBOLS = ["heart", "star", "hand", "plus"]
KIDS_MARKER = "*"
HALF_SPACE = "/"
ALLOWED_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /*")

# Messages the DMV endpoint has been seen to return, in plainer words.
DMV_MESSAGES = {
    "PLATE_AVAILABLE": "Available",
    "PLATE_NOT_AVAILABLE": "Taken, reserved, or refused by DMV",
    "two.characters": "Needs at least 2 characters",
    "atleast.one.kids": "Kids plate needs a symbol",
    "pow.plate": "POW configuration is reserved",
    "two.halfspaces": "Only one half space is allowed",
    "message.global": "DMV rejected the session (no valid session cookie)",
}


# --------------------------------------------------------------------------
# Plate text -> DMV form fields
# --------------------------------------------------------------------------
class PlateError(ValueError):
    """Raised when a plate string cannot be turned into a valid request."""


def normalize_plate(text):
    """Upper-case, trim, and collapse internal whitespace runs to one space."""
    return " ".join(text.strip().upper().split())


def plate_to_slots(text, plate_code, kids_symbol="heart"):
    """
    Turn user text into the list of slot values the DMV form sends.

    Returns (slots, kids_plate_value). Each slot is '' (full space), '/'
    (half space), '.' (Kids symbol) or a single letter/digit.
    """
    text = normalize_plate(text)
    if not text:
        raise PlateError("Empty plate")
    bad = sorted(set(text) - ALLOWED_CHARS)
    if bad:
        raise PlateError("Invalid character(s): " + " ".join(bad))

    code, name, max_chars, _fee, _renew = PLATE_BY_CODE[plate_code]
    is_kids = code == "K"
    seven_layout = max_chars == 7

    if KIDS_MARKER in text and not is_kids:
        raise PlateError("'*' (symbol) is only valid on the Kids plate")
    if is_kids and KIDS_MARKER not in text:
        text = KIDS_MARKER + text  # DMV default: symbol in the first slot
    if text.count(KIDS_MARKER) > 1:
        raise PlateError("Only one Kids symbol is allowed")
    if text.count(HALF_SPACE) > 1:
        raise PlateError(DMV_MESSAGES["two.halfspaces"])

    slots = []
    for ch in text:
        if ch == " ":
            slots.append("")
        elif ch == KIDS_MARKER:
            slots.append(".")
        else:
            slots.append(ch)

    # Leading/trailing full spaces are meaningless on a plate.
    while slots and slots[0] == "":
        slots.pop(0)
    while slots and slots[-1] == "":
        slots.pop()

    real_chars = [s for s in slots if s not in ("", HALF_SPACE, ".")]
    if len(real_chars) < 2:
        raise PlateError(DMV_MESSAGES["two.characters"])

    # The DMV page shows 7 slots for 7-character types, plus an 8th slot when a
    # half space is present, and 6 slots for 6-character types (a half space
    # uses one of them). Kids plates use the 7-slot layout: symbol + up to 6.
    slot_budget = max_chars + (1 if (seven_layout and HALF_SPACE in slots) else 0)
    if len(slots) > slot_budget:
        raise PlateError(
            "Too long: %d slots used, %s allows %d" % (len(slots), name, slot_budget)
        )
    if is_kids and len(real_chars) > 6:
        raise PlateError("Kids plate allows 1 symbol plus at most 6 characters")

    kids_value = kids_symbol if is_kids else ""
    return slots, kids_value


def build_form(text, plate_code, vehicle_code="AUTO", kids_symbol="heart"):
    """Build the exact field set the DMV page posts to checkPers.do."""
    code, name, max_chars, _fee, _renew = PLATE_BY_CODE[plate_code]
    if vehicle_code in ENVIRONMENTAL_ONLY_VEHICLES and code != "R":
        raise PlateError("Trailers and motorcycles can only order the Environmental plate")

    slots, kids_value = plate_to_slots(text, plate_code, kids_symbol)
    chars = [""] * 14
    offset = 0 if max_chars == 7 else 8  # 7-char types use slots 0-7, 6-char types 8-13
    for i, value in enumerate(slots):
        chars[offset + i] = value

    fields = [
        ("plateType", code),
        ("kidsPlate", kids_value),
        ("plateNameLow", name.lower()),
        ("plateName", name),
        ("plateLength", str(max_chars)),
        ("vetDecalCd", ""),
        ("centeredPlateLength", "0"),
        ("platechecked", "no"),
        ("imageSelected", "none"),
        ("vehicleType", vehicle_code),
    ]
    fields += [("plateChar%d" % i, chars[i]) for i in range(14)]
    fields.append(("vetDecalDesc", ""))
    return fields


def display_plate(text, plate_code, kids_symbol="heart"):
    """What the plate would read like, for the results table."""
    try:
        slots, _ = plate_to_slots(text, plate_code, kids_symbol)
    except PlateError:
        return normalize_plate(text)
    out = []
    for s in slots:
        if s == "":
            out.append(" ")
        elif s == ".":
            out.append("[%s]" % kids_symbol)
        else:
            out.append(s)
    return "".join(out)


# --------------------------------------------------------------------------
# DMV session
# --------------------------------------------------------------------------
class DmvSession:
    """One browsing session with the DMV plate application."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.ready = False

    def _request(self, url, data=None, extra_headers=None):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if extra_headers:
            headers.update(extra_headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://www.dmv.ca.gov"
        req = urllib.request.Request(url, data=body, headers=headers)
        with self.opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read()

    def start(self):
        """Open the application and acknowledge the terms page, like a browser would."""
        self._request(INIT_URL)
        # The availability endpoint only answers sessions that have passed the
        # terms page, so this POST is required even though its HTML reply is ignored.
        self._request(
            START_URL,
            data=[("acknowledged", "true"), ("_acknowledged", "on")],
            extra_headers={"Referer": INIT_URL},
        )
        self.ready = True

    def check(self, fields, _retry=True):
        """
        POST a plate configuration. Returns a dict with keys:
        status  'AVAILABLE' | 'TAKEN' | 'INVALID' | 'ERROR'
        detail  human-readable explanation
        raw     the DMV JSON (or None)
        """
        try:
            if not self.ready:
                self.start()
            status_code, body = self._request(
                CHECK_URL,
                data=fields,
                extra_headers={
                    "Referer": START_URL,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
        except urllib.error.HTTPError as exc:
            return {"status": "ERROR", "detail": "HTTP %d from DMV" % exc.code, "raw": None}
        except (urllib.error.URLError, OSError) as exc:
            return {"status": "ERROR", "detail": "Network error: %s" % exc, "raw": None}

        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            snippet = body[:120].decode("utf-8", "replace").strip()
            return {
                "status": "ERROR",
                "detail": "Unexpected response (HTTP %d): %s" % (status_code, snippet),
                "raw": None,
            }

        code = data.get("code", "")
        message = data.get("message", "")
        if data.get("success"):
            return {"status": "AVAILABLE", "detail": DMV_MESSAGES["PLATE_AVAILABLE"], "raw": data}
        if code == "NOT_AVAILABLE":
            return {"status": "TAKEN", "detail": DMV_MESSAGES["PLATE_NOT_AVAILABLE"], "raw": data}
        if code == "VALIDATION":
            if message == "message.global" and _retry:
                # Session expired or was never accepted: start fresh and try once more.
                self.ready = False
                return self.check(fields, _retry=False)
            return {
                "status": "INVALID",
                "detail": DMV_MESSAGES.get(message, "DMV validation: %s" % message),
                "raw": data,
            }
        return {
            "status": "ERROR",
            "detail": "DMV error %s %s" % (code, message),
            "raw": data,
        }


def check_plate(session, text, plate_code, vehicle_code="AUTO", kids_symbol="heart"):
    """Validate locally, then ask the DMV. Never raises."""
    try:
        fields = build_form(text, plate_code, vehicle_code, kids_symbol)
    except PlateError as exc:
        return {"status": "INVALID", "detail": str(exc), "raw": None}
    try:
        return session.check(fields)
    except Exception as exc:  # last line of defence so a worker thread never dies
        return {"status": "ERROR", "detail": "%s: %s" % (type(exc).__name__, exc), "raw": None}


# --------------------------------------------------------------------------
# Command-line mode
# --------------------------------------------------------------------------
def run_cli(args):
    session = DmvSession()
    plates = [p for p in args.plates if p.strip()]
    if not plates:
        print("No plates given.", file=sys.stderr)
        return 2
    name = PLATE_BY_CODE[args.type][1]
    print("Plate type: %s (%s)   Vehicle: %s" % (name, args.type, args.vehicle))
    print("-" * 64)
    exit_code = 0
    for i, plate in enumerate(plates):
        result = check_plate(session, plate, args.type, args.vehicle, args.symbol)
        shown = display_plate(plate, args.type, args.symbol)
        print("%-14s %-10s %s" % (shown, result["status"], result["detail"]))
        if result["status"] == "ERROR":
            exit_code = 1
        if i < len(plates) - 1:
            time.sleep(args.delay)
    return exit_code


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui(selftest_ms=0):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title("CA Personalized Plate Checker")
    root.minsize(900, 560)
    root.geometry("1040x640")

    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")

    # Tk 8.6.9 ignored Treeview tag colours unless the style map is cleaned up.
    def fixed_map(option):
        return [
            elm
            for elm in style.map("Treeview", query_opt=option)
            if elm[:2] != ("!disabled", "!selected")
        ]

    style.map(
        "Treeview",
        foreground=fixed_map("foreground"),
        background=fixed_map("background"),
    )

    results_queue = queue.Queue()
    worker = {"thread": None, "stop": threading.Event()}
    session = DmvSession()

    # ---- Options row -----------------------------------------------------
    options = ttk.LabelFrame(root, text="Plate options", padding=(10, 6))
    options.pack(fill="x", padx=10, pady=(10, 4))

    ttk.Label(options, text="Vehicle:").grid(row=0, column=0, sticky="w")
    vehicle_var = tk.StringVar(value=VEHICLE_TYPES[0][1])
    vehicle_combo = ttk.Combobox(
        options,
        textvariable=vehicle_var,
        values=[v[1] for v in VEHICLE_TYPES],
        state="readonly",
        width=13,
    )
    vehicle_combo.grid(row=0, column=1, padx=(4, 16))

    ttk.Label(options, text="Plate type:").grid(row=0, column=2, sticky="w")
    plate_labels = [
        "%s  (%d chars, $%d / $%d renewal)" % (p[1], p[2], p[3], p[4]) for p in PLATE_TYPES
    ]
    plate_var = tk.StringVar(value=plate_labels[0])
    plate_combo = ttk.Combobox(
        options, textvariable=plate_var, values=plate_labels, state="readonly", width=46
    )
    plate_combo.grid(row=0, column=3, padx=(4, 16))

    ttk.Label(options, text="Kids symbol:").grid(row=0, column=4, sticky="w")
    symbol_var = tk.StringVar(value=KIDS_SYMBOLS[0])
    symbol_combo = ttk.Combobox(
        options, textvariable=symbol_var, values=KIDS_SYMBOLS, state="disabled", width=8
    )
    symbol_combo.grid(row=0, column=5, padx=(4, 16))

    ttk.Label(options, text="Delay (s):").grid(row=0, column=6, sticky="w")
    delay_var = tk.StringVar(value="1.5")
    ttk.Spinbox(
        options, from_=0.5, to=30, increment=0.5, textvariable=delay_var, width=5
    ).grid(row=0, column=7, padx=(4, 0))

    def current_vehicle():
        for code, label in VEHICLE_TYPES:
            if label == vehicle_var.get():
                return code
        return "AUTO"

    def current_plate():
        idx = plate_labels.index(plate_var.get()) if plate_var.get() in plate_labels else 0
        return PLATE_TYPES[idx][0]

    def on_plate_change(_event=None):
        symbol_combo.configure(state="readonly" if current_plate() == "K" else "disabled")

    def on_vehicle_change(_event=None):
        if current_vehicle() in ENVIRONMENTAL_ONLY_VEHICLES:
            plate_combo.configure(values=[plate_labels[0]])
            plate_var.set(plate_labels[0])
        else:
            plate_combo.configure(values=plate_labels)
        on_plate_change()

    vehicle_combo.bind("<<ComboboxSelected>>", on_vehicle_change)
    plate_combo.bind("<<ComboboxSelected>>", on_plate_change)

    # ---- Main area: input on the left, results on the right ---------------
    body = ttk.Frame(root, padding=(10, 4))
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=2)
    body.columnconfigure(1, weight=5)
    body.rowconfigure(1, weight=1)

    ttk.Label(body, text="Plates to check (one per line):").grid(row=0, column=0, sticky="w")
    input_frame = ttk.Frame(body)
    input_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
    input_frame.rowconfigure(0, weight=1)
    input_frame.columnconfigure(0, weight=1)
    plate_text = tk.Text(input_frame, width=22, font=("Consolas", 13), undo=True, wrap="none")
    plate_text.grid(row=0, column=0, sticky="nsew")
    input_scroll = ttk.Scrollbar(input_frame, orient="vertical", command=plate_text.yview)
    input_scroll.grid(row=0, column=1, sticky="ns")
    plate_text.configure(yscrollcommand=input_scroll.set)
    plate_text.focus_set()

    hint = (
        "space = full space    / = half space\n"
        "* = Kids symbol position    letters and digits only\n"
        "Ctrl+Enter checks"
    )
    ttk.Label(body, text=hint, foreground="#555").grid(row=2, column=0, sticky="w", pady=(4, 0))

    ttk.Label(body, text="Results:").grid(row=0, column=1, sticky="w")
    results_frame = ttk.Frame(body)
    results_frame.grid(row=1, column=1, rowspan=2, sticky="nsew")
    results_frame.rowconfigure(0, weight=1)
    results_frame.columnconfigure(0, weight=1)

    columns = ("plate", "type", "status", "detail")
    tree = ttk.Treeview(results_frame, columns=columns, show="headings", selectmode="extended")
    tree.heading("plate", text="Plate")
    tree.heading("type", text="Plate type")
    tree.heading("status", text="Result")
    tree.heading("detail", text="Detail")
    tree.column("plate", width=130, anchor="w")
    tree.column("type", width=190, anchor="w")
    tree.column("status", width=100, anchor="center")
    tree.column("detail", width=330, anchor="w")
    tree.grid(row=0, column=0, sticky="nsew")
    tree_scroll = ttk.Scrollbar(results_frame, orient="vertical", command=tree.yview)
    tree_scroll.grid(row=0, column=1, sticky="ns")
    tree.configure(yscrollcommand=tree_scroll.set)

    tree.tag_configure("AVAILABLE", background="#d7f5d7")
    tree.tag_configure("TAKEN", background="#f8d7d7")
    tree.tag_configure("INVALID", background="#fff1c2")
    tree.tag_configure("ERROR", background="#e4e4e4")
    tree.tag_configure("PENDING", foreground="#777")

    # ---- Bottom bar -----------------------------------------------------
    bar = ttk.Frame(root, padding=(10, 4, 10, 10))
    bar.pack(fill="x")

    status_var = tk.StringVar(value="Ready.")
    progress = ttk.Progressbar(bar, mode="determinate", length=220)

    def set_status(text):
        status_var.set(text)

    def selected_plates():
        seen = set()
        plates = []
        for line in plate_text.get("1.0", "end").splitlines():
            norm = normalize_plate(line)
            if norm and norm not in seen:
                seen.add(norm)
                plates.append(norm)
        return plates

    def set_running(running):
        check_btn.configure(state="disabled" if running else "normal")
        stop_btn.configure(state="normal" if running else "disabled")
        for widget in (vehicle_combo, plate_combo):
            widget.configure(state="disabled" if running else "readonly")
        if running:
            symbol_combo.configure(state="disabled")
        else:
            on_plate_change()

    def worker_main(plates, plate_code, vehicle_code, kids_symbol, delay, row_ids):
        for i, (plate, row_id) in enumerate(zip(plates, row_ids)):
            if worker["stop"].is_set():
                results_queue.put(("row", row_id, "ERROR", "Stopped"))
                continue
            results_queue.put(("status", "Checking %s  (%d of %d)..." % (plate, i + 1, len(plates))))
            result = check_plate(session, plate, plate_code, vehicle_code, kids_symbol)
            results_queue.put(("row", row_id, result["status"], result["detail"]))
            results_queue.put(("progress", i + 1))
            if i < len(plates) - 1 and not worker["stop"].is_set():
                worker["stop"].wait(delay)
        results_queue.put(("done",))

    def start_checks():
        if worker["thread"] is not None and worker["thread"].is_alive():
            return
        plates = selected_plates()
        if not plates:
            messagebox.showinfo("Nothing to check", "Type at least one plate, one per line.")
            return
        try:
            delay = max(0.3, float(delay_var.get()))
        except ValueError:
            delay = 1.5
        plate_code = current_plate()
        vehicle_code = current_vehicle()
        kids_symbol = symbol_var.get()
        type_name = PLATE_BY_CODE[plate_code][1]

        row_ids = []
        for plate in plates:
            shown = display_plate(plate, plate_code, kids_symbol)
            row_ids.append(
                tree.insert("", "end", values=(shown, type_name, "...", "waiting"), tags=("PENDING",))
            )
        tree.see(row_ids[0])

        progress.configure(maximum=len(plates), value=0)
        progress.pack(side="right", padx=(8, 0))
        worker["stop"].clear()
        set_running(True)
        worker["thread"] = threading.Thread(
            target=worker_main,
            args=(plates, plate_code, vehicle_code, kids_symbol, delay, row_ids),
            daemon=True,
        )
        worker["thread"].start()

    def stop_checks():
        worker["stop"].set()
        set_status("Stopping after the current check...")

    def poll_queue():
        try:
            while True:
                item = results_queue.get_nowait()
                kind = item[0]
                if kind == "row":
                    _, row_id, status, detail = item
                    if not tree.exists(row_id):
                        continue
                    values = list(tree.item(row_id, "values"))
                    values[2] = status
                    values[3] = detail
                    tree.item(row_id, values=values, tags=(status,))
                    tree.see(row_id)
                elif kind == "status":
                    set_status(item[1])
                elif kind == "progress":
                    progress.configure(value=item[1])
                elif kind == "done":
                    set_running(False)
                    progress.pack_forget()
                    rows = tree.get_children()
                    available = sum(1 for r in rows if tree.item(r, "values")[2] == "AVAILABLE")
                    set_status("Done. %d available of %d in the results list." % (available, len(rows)))
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    def clear_results():
        for row in tree.get_children():
            tree.delete(row)
        set_status("Ready.")

    def export_csv():
        rows = tree.get_children()
        if not rows:
            messagebox.showinfo("Nothing to export", "Run a check first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile="ca-plates-%s.csv" % time.strftime("%Y%m%d-%H%M%S"),
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["plate", "plate_type", "result", "detail", "vehicle"])
            for row in rows:
                writer.writerow(list(tree.item(row, "values")) + [vehicle_var.get()])
        set_status("Exported %d rows to %s" % (len(rows), path))

    def copy_available():
        plates = [
            tree.item(r, "values")[0]
            for r in tree.get_children()
            if tree.item(r, "values")[2] == "AVAILABLE"
        ]
        if not plates:
            set_status("No available plates to copy.")
            return
        root.clipboard_clear()
        root.clipboard_append("\n".join(plates))
        set_status("Copied %d available plate(s) to the clipboard." % len(plates))

    def remove_selected(_event=None):
        for row in tree.selection():
            tree.delete(row)

    check_btn = ttk.Button(bar, text="Check availability", command=start_checks)
    check_btn.pack(side="left")
    stop_btn = ttk.Button(bar, text="Stop", command=stop_checks, state="disabled")
    stop_btn.pack(side="left", padx=(6, 0))
    ttk.Button(bar, text="Clear results", command=clear_results).pack(side="left", padx=(6, 0))
    ttk.Button(bar, text="Copy available", command=copy_available).pack(side="left", padx=(6, 0))
    ttk.Button(bar, text="Export CSV...", command=export_csv).pack(side="left", padx=(6, 0))
    ttk.Label(bar, textvariable=status_var, anchor="w").pack(
        side="left", padx=(14, 0), fill="x", expand=True
    )

    tree.bind("<Delete>", remove_selected)

    def check_shortcut(_event=None):
        start_checks()
        return "break"

    root.bind("<Control-Return>", check_shortcut)
    plate_text.bind("<Control-Return>", check_shortcut)

    def on_close():
        worker["stop"].set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll_queue)

    if selftest_ms:
        root.geometry("1040x640+0+0")
        plate_text.insert("1.0", "QZXJ7VK\nLUV CATS\n2FAST4U\nTESLA\nX\n")
        root.after(400, start_checks)
        root.after(selftest_ms, on_close)

    root.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check California personalized plate availability (GUI by default)."
    )
    parser.add_argument("--cli", action="store_true", help="run in the terminal instead of the GUI")
    parser.add_argument(
        "--type",
        default="R",
        choices=[p[0] for p in PLATE_TYPES],
        help="plate type code (default R = Environmental). "
        + "  ".join("%s=%s" % (p[0], p[1]) for p in PLATE_TYPES),
    )
    parser.add_argument(
        "--vehicle", default="AUTO", choices=[v[0] for v in VEHICLE_TYPES], help="vehicle type"
    )
    parser.add_argument("--symbol", default="heart", choices=KIDS_SYMBOLS, help="Kids plate symbol")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between checks in --cli mode")
    parser.add_argument("--selftest-gui", type=int, default=0, metavar="MS", help=argparse.SUPPRESS)
    parser.add_argument("plates", nargs="*", help="plate configurations to check (--cli mode)")
    args = parser.parse_args(argv)

    if args.cli:
        return run_cli(args)
    return run_gui(selftest_ms=args.selftest_gui)


if __name__ == "__main__":
    sys.exit(main())
