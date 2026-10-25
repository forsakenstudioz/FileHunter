"""FileHunter - find every file of a type on all your drives and collect them in one folder."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import filehunter_core as core
from filehunter_core import APP_NAME, VERSION, human_size, human_time

try:
    from PIL import Image, ImageOps, ImageTk  # optional: thumbnails for JPG/WEBP/BMP/...
    Image.MAX_IMAGE_PIXELS = 300_000_000
except Exception:  # noqa: BLE001
    Image = None

THEMES = ("System", "Light", "Dark")
PALETTES = {
    "Light": dict(bg="#f5f6f8", panel="#ffffff", fg="#1d2026", muted="#6b7280", border="#d6d9df",
                  accent="#0f8b8d", accent_hover="#0b7375", accent_fg="#ffffff", select="#d3eeee",
                  entry="#ffffff", trough="#e4e7eb", ok="#1f9d63", warn="#b7791f", err="#d64545",
                  log_bg="#fbfbfc", stripe="#f8f9fa", heading="#eef0f3"),
    "Dark": dict(bg="#1e1f22", panel="#2a2c30", fg="#e8e9eb", muted="#9aa0a7", border="#3a3d44",
                 accent="#2ec4b6", accent_hover="#56d6ca", accent_fg="#0b1a19", select="#1d4340",
                 entry="#202227", trough="#33363c", ok="#46c08a", warn="#e0a53a", err="#ff6b6b",
                 log_bg="#18191c", stripe="#24262a", heading="#2a2c30"),
}


# ----------------------------------------------------------------------------------- helpers
def system_theme() -> str:
    try:
        if sys.platform == "win32":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return "Light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "Dark"
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "Dark" if "Dark" in out else "Light"
        for cmd in (["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                    ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]):
            try:
                if "dark" in subprocess.run(cmd, capture_output=True, text=True,
                                            timeout=2).stdout.lower():
                    return "Dark"
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return "Light"


def ui_font() -> tuple:
    if sys.platform == "win32":
        return ("Segoe UI", 10)
    try:
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:  # noqa: BLE001
        fam = "Helvetica"
    return (fam, 13 if sys.platform == "darwin" else 10)


def mono_font() -> tuple:
    if sys.platform == "win32":
        return ("Consolas", 9)
    if sys.platform == "darwin":
        return ("Menlo", 11)
    return ("DejaVu Sans Mono", 9) if "DejaVu Sans Mono" in tkfont.families() else ("TkFixedFont",)


def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex(a)
    rb, gb, bb = _hex(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t),
                              round(ba + (bb - ba) * t))


def check_image(master, state: str, pal: dict, size: int = 16) -> tk.PhotoImage:
    """Draw a checkbox picture: 'on', 'off', 'partial' or 'none' (blank)."""
    img = tk.PhotoImage(master=master, width=size, height=size)
    if state == "none":
        img.put(pal["bg"], to=(0, 0, size, size))
        for y in range(size):
            for x in range(size):
                img.transparency_set(x, y, True)
        return img
    lo, hi, r = 1, size - 2, 3
    filled = state in ("on", "partial")
    box = pal["accent"] if filled else pal["entry"]
    edge = pal["accent"] if filled else _mix(pal["border"], pal["fg"], 0.35)
    rows = []
    inside = set()
    for y in range(size):
        row = []
        for x in range(size):
            ins = lo <= x <= hi and lo <= y <= hi
            # rounded corners
            for cx, cy in ((lo + r, lo + r), (hi - r, lo + r), (lo + r, hi - r), (hi - r, hi - r)):
                if ((x < lo + r and cx == lo + r) or (x > hi - r and cx == hi - r)) and \
                        ((y < lo + r and cy == lo + r) or (y > hi - r and cy == hi - r)):
                    if (x - cx) ** 2 + (y - cy) ** 2 > (r + 0.5) ** 2:
                        ins = False
            if ins:
                inside.add((x, y))
                row.append(box)
            else:
                row.append(pal["bg"])
        rows.append(row)
    # 1px border ring
    for (x, y) in inside:
        if any((nx, ny) not in inside for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))):
            rows[y][x] = edge
    mark = pal["accent_fg"]
    if state == "on":
        s = size / 16
        pts = [(4 * s, 8.2 * s), (6.8 * s, 11 * s), (12 * s, 5.2 * s)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 3) + 1
            for i in range(steps + 1):
                px = x0 + (x1 - x0) * i / steps
                py = y0 + (y1 - y0) * i / steps
                for dx, dy in ((0, 0), (0, 1), (1, 0)) if size >= 16 else ((0, 0),):
                    xx, yy = int(round(px)) + dx, int(round(py)) + dy
                    if (xx, yy) in inside:
                        rows[yy][xx] = mark
    elif state == "partial":
        y0 = size // 2
        for x in range(4 * size // 16, size - 4 * size // 16):
            for yy in (y0 - 1, y0):
                rows[yy][x] = mark
    img.put(" ".join("{" + " ".join(r) + "}" for r in rows))
    for y in range(size):
        for x in range(size):
            if (x, y) not in inside:
                img.transparency_set(x, y, True)
    return img


def app_icon(master, accent: str, size: int = 64) -> tk.PhotoImage:
    """Teal rounded square with a white magnifying glass."""
    import math
    img = tk.PhotoImage(master=master, width=size, height=size)
    s = size / 64
    rows, clear = [], []
    cx, cy = 27 * s, 27 * s
    for y in range(size):
        row = []
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            rr = 14 * s
            ox = min(max(px, rr), size - rr)
            oy = min(max(py, rr), size - rr)
            if (px - ox) ** 2 + (py - oy) ** 2 > rr ** 2:
                row.append("#000000")
                clear.append((x, y))
                continue
            d = math.hypot(px - cx, py - cy)
            # handle from (39,39) to (52,52)
            t = max(0.0, min(1.0, ((px - 38 * s) * 1 + (py - 38 * s) * 1) / (2 * 14 * s)))
            hx, hy = 38 * s + 14 * s * t, 38 * s + 14 * s * t
            dh = math.hypot(px - hx, py - hy)
            white = (10.5 * s <= d <= 16 * s) or dh <= 4 * s
            dot = math.hypot(px - 23 * s, py - 23 * s) <= 3 * s
            row.append("#ffffff" if white or dot else accent)
        rows.append(row)
    img.put(" ".join("{" + " ".join(r) + "}" for r in rows))
    for x, y in clear:
        img.transparency_set(x, y, True)
    return img


def open_path(path: str):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:  # noqa: BLE001
        messagebox.showerror(APP_NAME, f"Couldn't open:\n{path}\n\n{e}")


def reveal(path: str):
    """Open the folder that contains this file, with the file highlighted where possible."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or path])
    except Exception as e:  # noqa: BLE001
        messagebox.showerror(APP_NAME, f"Couldn't open the folder:\n{e}")


def mid(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n // 2 - 1] + "…" + s[-(n - n // 2):]


def dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


class Tooltip:
    def __init__(self, widget, text: str | callable, delay: int = 450):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._sched, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _sched(self, _e=None):
        self._hide()
        self.job = self.w.after(self.delay, self._show)

    def _show(self):
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        app = self.w.winfo_toplevel()
        p = getattr(app, "pal", PALETTES["Light"])
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=text, justify="left", wraplength=380, bg=p["panel"], fg=p["fg"],
                 relief="solid", bd=1, padx=8, pady=5, font=ui_font()).pack()

    def _hide(self, _e=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ===================================================================================== App
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = core.load_settings()
        self.title(f"{APP_NAME}  {VERSION}")
        self.scale = self.winfo_fpixels("1i") / 96.0
        self.minsize(int(980 * min(self.scale, 1.5)), int(660 * min(self.scale, 1.5)))
        geo = self.cfg.get("geometry") or ""
        if not geo:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w = min(int(1280 * self.scale), int(sw * 0.94))
            h = min(int(940 * self.scale), int(sh * 0.88))
            geo = f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}"
        self.geometry(geo)

        self.drives: dict[str, core.Drive] = {}      # iid -> drive
        self.drive_checked: set[str] = set()
        self.items: list[core.FileItem] = []
        self.checked: set[int] = set()
        self.visible: list[int] = []
        self.inserted: set[int] = set()
        self.pending: list[int] = []
        self.sort_col: str | None = None
        self.sort_rev = False
        self.scanner: core.Scanner | None = None
        self.transfer: core.Transfer | None = None
        self.dup_job: threading.Thread | None = None
        self.dup_stop = threading.Event()
        self.dup_progress = (0, 0)
        self.dup_groups: list = []
        self.log_lines: list[str] = []
        self.thumb_q: queue.Queue = queue.Queue()
        self.ui_q: queue.Queue = queue.Queue()  # work handed back from background threads
        self.thumb_for = None
        self._thumb = None
        self._drive_poll_busy = False
        self._filter_job = None
        self.busy = False
        self.deleter: core.Deleter | None = None

        self._build()
        self.apply_theme(self.cfg.get("theme", "System"))
        self._load_drives(first=True)
        self._refresh_counts()
        self._update_buttons()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(120, self._tick)
        self.after(3000, self._poll_drives)

    # =============================================================================== layout
    def _build(self):
        main = ttk.Frame(self, padding=(12, 8, 12, 6))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # ---- header
        head = ttk.Frame(main)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.logo_lbl = ttk.Label(head)
        self.logo_lbl.pack(side="left", padx=(0, 8))
        tbox = ttk.Frame(head)
        tbox.pack(side="left")
        ttk.Label(tbox, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(tbox, text="Find any type of file buried in your drives, then gather it in one folder.",
                  style="Muted.TLabel").pack(anchor="w")
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", "System"))
        cb = ttk.Combobox(head, textvariable=self.theme_var, values=THEMES, width=8, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_theme(self.theme_var.get()))
        ttk.Label(head, text="Theme:", style="Muted.TLabel").pack(side="right", padx=(0, 4))
        self.btn_log = ttk.Button(head, text="Log", command=self._show_log)
        self.btn_log.pack(side="right", padx=(0, 12))

        # ---- step 1 + 2
        top = ttk.Frame(main)
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=5, uniform="top")
        top.columnconfigure(1, weight=6, uniform="top")
        self._build_drives(top)
        self._build_search(top)

        # ---- step 3
        self._build_results(main)

        # ---- step 4
        self._build_collect(main)


        self.bind_all("<Control-f>", lambda e: (self.filter_entry.focus_set(), "break"))
        self.bind("<F5>", lambda e: self._load_drives())
        self.bind("<Escape>", lambda e: self._stop_any())

    def _build_drives(self, parent):
        box = ttk.LabelFrame(parent, text=" 1 · Where to look ", padding=8)
        box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        cols = ("type", "size", "free")
        self.dtree = ttk.Treeview(box, columns=cols, height=5, selectmode="browse",
                                  style="Drives.Treeview")
        self.dtree.heading("#0", text="Drive", anchor="w")
        self.dtree.heading("type", text="Type", anchor="w")
        self.dtree.heading("size", text="Size", anchor="e")
        self.dtree.heading("free", text="Free", anchor="e")
        s = self.scale
        self.dtree.column("#0", width=int(190 * s), stretch=True)
        self.dtree.column("type", width=int(130 * s), stretch=True)
        self.dtree.column("size", width=int(74 * s), anchor="e", stretch=False)
        self.dtree.column("free", width=int(74 * s), anchor="e", stretch=False)
        self.dtree.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.dtree.yview)
        ys.grid(row=0, column=1, sticky="ns")
        self.dtree.configure(yscrollcommand=ys.set)
        self.dtree.bind("<Button-1>", self._drive_click)
        self.dtree.bind("<space>", lambda e: self._toggle_drive(self.dtree.focus()))
        self.dtree.bind("<Double-1>", lambda e: "break")

        row = ttk.Frame(box)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.drive_btns = [
            ttk.Button(row, text="All", width=5, command=lambda: self._check_drives(True)),
            ttk.Button(row, text="None", width=5, command=lambda: self._check_drives(False)),
            ttk.Button(row, text="Refresh", command=self._load_drives),
            ttk.Button(row, text="Add folder…", command=self._add_folder),
        ]
        for b in self.drive_btns:
            b.pack(side="left", padx=(0, 4))
        self.btn_remove_folder = ttk.Button(row, text="Remove", command=self._remove_folder)
        self.btn_remove_folder.pack(side="left")
        self.drive_btns.append(self.btn_remove_folder)
        self.drive_note = ttk.Label(box, text="Plug in a pendrive or disk and it shows up here by itself.",
                                    style="Muted.TLabel")
        self.drive_note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_search(self, parent):
        box = ttk.LabelFrame(parent, text=" 2 · What to look for ", padding=8)
        box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)

        cats = ttk.Frame(box)
        cats.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.cat_vars: dict[str, tk.BooleanVar] = {}
        chosen = set(self.cfg.get("categories") or [])
        for i, name in enumerate(core.CATEGORIES):
            v = tk.BooleanVar(value=name in chosen)
            cbx = ttk.Checkbutton(cats, text=name, variable=v, command=self._search_changed)
            cbx.grid(row=i // 5, column=i % 5, sticky="w", padx=(0, 10), pady=1)
            Tooltip(cbx, "  ".join("." + e for e in core.CATEGORIES[name]))
            self.cat_vars[name] = v
        for c in range(5):
            cats.columnconfigure(c, weight=1)

        ttk.Label(box, text="Other extensions:").grid(row=1, column=0, sticky="w", pady=(6, 2))
        self.ext_var = tk.StringVar(value=self.cfg.get("custom_exts", ""))
        e = ttk.Entry(box, textvariable=self.ext_var)
        e.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 2), padx=(6, 0))
        Tooltip(e, "Type any extensions, separated by commas or spaces.\nExample:  img, iso, mp3, exe")
        self.ext_var.trace_add("write", lambda *a: self._search_changed())
        self.ext_summary = ttk.Label(box, text="", style="Muted.TLabel")
        self.ext_summary.grid(row=2, column=0, columnspan=4, sticky="w")

        ttk.Label(box, text="Name contains:").grid(row=3, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=self.cfg.get("name_filter", ""))
        ne = ttk.Entry(box, textvariable=self.name_var, width=16)
        ne.grid(row=3, column=1, sticky="ew", padx=(6, 10), pady=2)
        Tooltip(ne, "Optional. Plain text matches anywhere in the name.\n"
                    "Use * and ? as wildcards, and ; for several:  holiday*; *2023*\n"
                    "With no file type chosen, this searches every kind of file.")
        ttk.Label(box, text="Modified:").grid(row=3, column=2, sticky="w", pady=2)
        self.age_var = tk.StringVar(value=self.cfg.get("age", "Any time"))
        ttk.Combobox(box, textvariable=self.age_var, values=list(core.AGE_CHOICES), state="readonly",
                     width=16).grid(row=3, column=3, sticky="ew", padx=(6, 0), pady=2)

        ttk.Label(box, text="Size from:").grid(row=4, column=0, sticky="w", pady=2)
        sz = ttk.Frame(box)
        sz.grid(row=4, column=1, columnspan=3, sticky="w", padx=(6, 0), pady=2)
        self.min_var = tk.StringVar(value=self.cfg.get("min_size", ""))
        self.max_var = tk.StringVar(value=self.cfg.get("max_size", ""))
        a = ttk.Entry(sz, textvariable=self.min_var, width=9)
        a.pack(side="left")
        ttk.Label(sz, text=" to ").pack(side="left")
        b = ttk.Entry(sz, textvariable=self.max_var, width=9)
        b.pack(side="left")
        ttk.Label(sz, text="  e.g. 10 MB, 2 GB (leave empty for any size)",
                  style="Muted.TLabel").pack(side="left")

        opts = ttk.Frame(box)
        opts.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.skip_sys_var = tk.BooleanVar(value=self.cfg.get("skip_system", True))
        self.hidden_var = tk.BooleanVar(value=self.cfg.get("include_hidden", False))
        c1 = ttk.Checkbutton(opts, text="Skip Windows, program & app-data folders",
                             variable=self.skip_sys_var, command=self._save_cfg)
        c1.pack(side="left")
        Tooltip(c1, "Recommended. Skips Windows, Program Files, ProgramData, AppData, the Recycle Bin, "
                    "System Volume Information, node_modules and similar.\nThese hold thousands of "
                    "program files and caches you rarely want, and make scans much slower.")
        c2 = ttk.Checkbutton(opts, text="Include hidden files", variable=self.hidden_var,
                             command=self._save_cfg)
        c2.pack(side="left", padx=(12, 0))

        btns = ttk.Frame(box)
        btns.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.btn_scan = ttk.Button(btns, text="Start scan", style="Accent.TButton", command=self._start_scan)
        self.btn_scan.pack(side="left")
        self.btn_scan_pause = ttk.Button(btns, text="Pause", command=self._pause_scan)
        self.btn_scan_pause.pack(side="left", padx=(6, 0))
        self.btn_scan_stop = ttk.Button(btns, text="Stop", command=self._stop_scan)
        self.btn_scan_stop.pack(side="left", padx=(6, 0))
        self.search_widgets = [w for w in self._all_children(box)
                               if isinstance(w, (ttk.Entry, ttk.Combobox, ttk.Checkbutton))]

    def _build_results(self, main):
        box = ttk.LabelFrame(main, text=" 3 · Results ", padding=8)
        box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(2, weight=1)

        # scan progress
        pr = ttk.Frame(box)
        pr.grid(row=0, column=0, sticky="ew")
        pr.columnconfigure(1, weight=1)
        self.scan_bar = ttk.Progressbar(pr, mode="indeterminate", length=int(120 * self.scale),
                                        style="Thin.Horizontal.TProgressbar")
        self.scan_lbl = ttk.Label(pr, text="Choose drives and file types, then press Start scan.",
                                  style="Muted.TLabel")
        self.scan_lbl.grid(row=0, column=1, sticky="w")

        # toolbar
        tb = ttk.Frame(box)
        tb.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        ttk.Label(tb, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(tb, textvariable=self.filter_var, width=22)
        self.filter_entry.pack(side="left", padx=(4, 6))
        Tooltip(self.filter_entry, "Type to show only results whose name or folder contains this text. (Ctrl+F)")
        self.filter_var.trace_add("write", lambda *a: self._filter_soon())
        self.type_var = tk.StringVar(value="All types")
        self.type_cb = ttk.Combobox(tb, textvariable=self.type_var, values=["All types"],
                                    state="readonly", width=16)
        self.type_cb.pack(side="left")
        self.type_cb.bind("<<ComboboxSelected>>", lambda e: self._apply_view())
        self.only_dup_var = tk.BooleanVar(value=False)
        self.only_dup_cb = ttk.Checkbutton(tb, text="Only duplicates", variable=self.only_dup_var,
                                           command=self._apply_view)
        self.only_dup_cb.pack(side="left", padx=(8, 0))

        self.btn_more = ttk.Menubutton(tb, text="More")
        self.btn_more.pack(side="right")
        self.more_menu = tk.Menu(self.btn_more, tearoff=False)
        self.btn_more["menu"] = self.more_menu
        self.more_menu.add_command(label="Find duplicate files", command=self._find_dups)
        self.more_menu.add_command(label="Check extra copies (keep the oldest of each)",
                                   command=self._check_extra_copies)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Check files larger than…", command=self._check_larger)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Export list (CSV for Excel)…", command=self._export)
        self.more_menu.add_command(label="Copy checked paths to clipboard", command=self._copy_paths)
        self.btn_invert = ttk.Button(tb, text="Invert", command=lambda: self._check_visible(None))
        self.btn_invert.pack(side="right", padx=(0, 6))
        self.btn_none = ttk.Button(tb, text="Check none", command=lambda: self._check_visible(False))
        self.btn_none.pack(side="right", padx=(0, 4))
        self.btn_all = ttk.Button(tb, text="Check all shown", command=lambda: self._check_visible(True))
        self.btn_all.pack(side="right", padx=(0, 4))

        # list + preview
        pw = ttk.PanedWindow(box, orient="horizontal")
        pw.grid(row=2, column=0, sticky="nsew")
        lf = ttk.Frame(pw)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        cols = ("type", "size", "modified", "dup", "folder")
        self.tree = ttk.Treeview(lf, columns=cols, selectmode="extended")
        s = self.scale
        heads = {"#0": ("Name", 250, "w"), "type": ("Type", 56, "w"), "size": ("Size", 80, "e"),
                 "modified": ("Modified", 122, "w"), "dup": ("Duplicate", 104, "w"),
                 "folder": ("Folder", 360, "w")}
        for c, (t, w, a) in heads.items():
            self.tree.heading(c, text=t, anchor=a, command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=int(w * s), anchor=a,
                             stretch=c in ("#0", "folder"), minwidth=int(40 * s))
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(lf, orient="horizontal", command=self.tree.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<space>", self._space_toggle)
        self.tree.bind("<Double-1>", self._tree_double)
        self.tree.bind("<Return>", self._tree_double)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._preview())
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Button-2>", self._tree_menu)
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break"))
        pw.add(lf, weight=4)

        pv = ttk.Frame(pw, padding=(10, 0, 0, 0))
        self.pv_canvas = tk.Canvas(pv, width=int(220 * s), height=int(150 * s), highlightthickness=0, bd=0)
        self.pv_canvas.pack(fill="x")
        self.pv_name = ttk.Label(pv, text="", style="Strong.TLabel", wraplength=int(220 * s))
        self.pv_name.pack(anchor="w", pady=(8, 2))
        pbr = ttk.Frame(pv)
        pbr.pack(anchor="w", pady=(2, 6))
        self.btn_open = ttk.Button(pbr, text="Open", command=lambda: self._act_selected(open_path))
        self.btn_open.pack(side="left")
        self.btn_reveal = ttk.Button(pbr, text="Show in folder", command=lambda: self._act_selected(reveal))
        self.btn_reveal.pack(side="left", padx=(6, 0))
        self.pv_info = ttk.Label(pv, text="Select a file to see it here.", style="Muted.TLabel",
                                 wraplength=int(220 * s), justify="left")
        self.pv_info.pack(anchor="w")
        pw.add(pv, weight=1)

        bot = ttk.Frame(box)
        bot.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.count_lbl = ttk.Label(bot, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="left")
        self.status = ttk.Label(bot, text="", style="Muted.TLabel")
        self.status.pack(side="right")

        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Open", command=lambda: self._act_selected(open_path))
        self.menu.add_command(label="Show in folder", command=lambda: self._act_selected(reveal))
        self.menu.add_command(label="Copy path", command=self._copy_selected_path)
        self.menu.add_separator()
        self.menu.add_command(label="Check selected rows", command=lambda: self._check_selection(True))
        self.menu.add_command(label="Uncheck selected rows", command=lambda: self._check_selection(False))
        self.menu.add_command(label="Check everything in this folder", command=self._check_same_folder)
        self.menu.add_command(label="Check every file of this type", command=self._check_same_type)

    def _build_collect(self, main):
        box = ttk.LabelFrame(main, text=" 4 · Gather the checked files ", padding=8)
        box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Put them in:").grid(row=0, column=0, sticky="w")
        self.dest_var = tk.StringVar(value=self.cfg.get("dest") or str(Path.home() / APP_NAME))
        de = ttk.Entry(box, textvariable=self.dest_var)
        de.grid(row=0, column=1, sticky="ew", padx=6)
        de.bind("<FocusOut>", lambda e: self._save_cfg())
        self.btn_browse = ttk.Button(box, text="Browse…", command=self._browse)
        self.btn_browse.grid(row=0, column=2)
        self.btn_open_dest = ttk.Button(box, text="Open folder", command=self._open_dest)
        self.btn_open_dest.grid(row=0, column=3, padx=(6, 0))

        r = ttk.Frame(box)
        r.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.mode_var = tk.StringVar(value=self.cfg.get("mode", "copy"))
        ttk.Radiobutton(r, text="Copy", value="copy", variable=self.mode_var,
                        command=self._mode_changed).pack(side="left")
        mv = ttk.Radiobutton(r, text="Move (cut & paste)", value="move", variable=self.mode_var,
                             command=self._mode_changed)
        mv.pack(side="left", padx=(10, 18))
        Tooltip(mv, "The files are removed from their old places. You can put them back with "
                    "\"Undo last\".")
        ttk.Label(r, text="Arrange:").pack(side="left")
        self.layout_var = tk.StringVar(value=core.LAYOUTS.get(self.cfg.get("layout"), core.LAYOUTS["flat"]))
        ttk.Combobox(r, textvariable=self.layout_var, values=list(core.LAYOUTS.values()),
                     state="readonly", width=31).pack(side="left", padx=(4, 14))
        ttk.Label(r, text="Same name already there:").pack(side="left")
        self.conflict_var = tk.StringVar(value=core.CONFLICTS.get(self.cfg.get("conflict"),
                                                                  core.CONFLICTS["rename"]))
        ttk.Combobox(r, textvariable=self.conflict_var, values=list(core.CONFLICTS.values()),
                     state="readonly", width=22).pack(side="left", padx=(4, 0))

        r2 = ttk.Frame(box)
        r2.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.ident_var = tk.BooleanVar(value=self.cfg.get("skip_identical", True))
        self.verify_var = tk.BooleanVar(value=self.cfg.get("verify", False))
        ci = ttk.Checkbutton(r2, text="Skip files that are already there (identical)",
                             variable=self.ident_var, command=self._save_cfg)
        ci.pack(side="left")
        cv = ttk.Checkbutton(r2, text="Double-check every copy (slower)", variable=self.verify_var,
                             command=self._save_cfg)
        cv.pack(side="left", padx=(12, 0))
        Tooltip(cv, "Reads each copied file back and compares its contents with the original.")
        self.btn_go = ttk.Button(r2, text="Copy checked files", style="Accent.TButton", command=self._start_transfer)
        self.btn_go.pack(side="right")
        self.btn_undo = ttk.Button(r2, text="Undo last", command=self._undo)
        self.btn_undo.pack(side="right", padx=(0, 6))
        self.btn_delete = ttk.Button(r2, text="Delete checked…", style="Danger.TButton",
                                     command=self._delete_checked)
        self.btn_delete.pack(side="right", padx=(0, 18))
        Tooltip(self.btn_delete, f"Sends the ticked files to the {core.TRASH_NAME}, "
                                 "or deletes them permanently. You confirm first.")

        r3 = self.xfer_row = ttk.Frame(box)
        r3.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        r3.grid_remove()
        r3.columnconfigure(0, weight=1)
        self.xfer_bar = ttk.Progressbar(r3, mode="determinate", maximum=1000)
        self.xfer_bar.grid(row=0, column=0, sticky="ew")
        self.btn_x_pause = ttk.Button(r3, text="Pause", command=self._pause_transfer)
        self.btn_x_pause.grid(row=0, column=1, padx=(6, 0))
        self.btn_x_stop = ttk.Button(r3, text="Stop", command=self._stop_transfer)
        self.btn_x_stop.grid(row=0, column=2, padx=(6, 0))
        self.xfer_lbl = ttk.Label(r3, text="", style="Muted.TLabel")
        self.xfer_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.collect_widgets = [w for w in self._all_children(box)
                                if isinstance(w, (ttk.Entry, ttk.Combobox, ttk.Checkbutton,
                                                  ttk.Radiobutton))] + [self.btn_browse]

    def _all_children(self, w):
        out = []
        for c in w.winfo_children():
            out.append(c)
            out.extend(self._all_children(c))
        return out

    # =============================================================================== theme
    def _set_theme(self, choice: str):
        self.cfg["theme"] = choice
        core.save_settings(self.cfg)
        self.apply_theme(choice)

    def apply_theme(self, choice: str):
        mode = system_theme() if choice == "System" else choice
        p = self.pal = PALETTES.get(mode, PALETTES["Light"])
        st = ttk.Style(self)
        st.theme_use("clam")
        base_font = ui_font()
        self.configure(bg=p["bg"])
        st.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["entry"],
                     bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                     troughcolor=p["trough"], focuscolor=p["accent"], selectbackground=p["accent"],
                     selectforeground=p["accent_fg"], insertcolor=p["fg"], font=base_font,
                     arrowcolor=p["fg"])
        st.configure("TFrame", background=p["bg"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        st.configure("Warn.TLabel", background=p["bg"], foreground=p["warn"])
        st.configure("Err.TLabel", background=p["bg"], foreground=p["err"])
        st.configure("Danger.TButton", background=p["panel"], foreground=p["err"],
                     bordercolor=p["border"], padding=(10, 4), relief="flat")
        st.map("Danger.TButton", background=[("disabled", _mix(p["bg"], p["panel"], 0.45)),
                                             ("pressed", p["err"]), ("active", p["err"])],
               foreground=[("disabled", p["muted"]), ("active", "#ffffff"), ("pressed", "#ffffff")])
        st.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
        st.configure("Strong.TLabel", background=p["bg"], foreground=p["fg"],
                     font=(base_font[0], base_font[1], "bold"))
        st.configure("Title.TLabel", background=p["bg"], foreground=p["fg"],
                     font=(base_font[0], base_font[1] + 6, "bold"))
        st.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], relief="solid")
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["muted"],
                     font=(base_font[0], base_font[1], "bold"))
        st.configure("TButton", background=p["panel"], foreground=p["fg"], bordercolor=p["border"],
                     padding=(10, 4), relief="flat")
        st.map("TButton", background=[("disabled", _mix(p["bg"], p["panel"], 0.45)), ("pressed", p["select"]),
                                      ("active", p["select"])],
               foreground=[("disabled", p["muted"])])
        st.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
                     bordercolor=p["accent"], font=(base_font[0], base_font[1], "bold"),
                     padding=(16, 5))
        st.map("Accent.TButton", background=[("disabled", p["trough"]), ("pressed", p["accent_hover"]),
                                             ("active", p["accent_hover"])],
               foreground=[("disabled", p["muted"])], bordercolor=[("disabled", p["trough"])])
        st.configure("TMenubutton", background=p["panel"], foreground=p["fg"], bordercolor=p["border"],
                     padding=(10, 4), relief="flat", arrowcolor=p["fg"])
        st.map("TMenubutton", background=[("active", p["select"]), ("disabled", _mix(p["bg"], p["panel"], 0.45))],
               foreground=[("disabled", p["muted"])])
        st.configure("TCheckbutton", background=p["bg"], foreground=p["fg"],
                     indicatorbackground=p["entry"], indicatorforeground=p["accent"])
        st.map("TCheckbutton", background=[("active", p["bg"])],
               indicatorbackground=[("selected", p["accent"]), ("disabled", p["trough"])],
               indicatorforeground=[("selected", p["accent_fg"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TRadiobutton", background=p["bg"], foreground=p["fg"],
                     indicatorbackground=p["entry"], indicatorforeground=p["accent"])
        st.map("TRadiobutton", background=[("active", p["bg"])],
               indicatorbackground=[("selected", p["accent"]), ("disabled", p["trough"])],
               indicatorforeground=[("selected", p["accent_fg"])],
               foreground=[("disabled", p["muted"])])
        st.configure("TEntry", fieldbackground=p["entry"], foreground=p["fg"], padding=(5, 3))
        st.map("TEntry", fieldbackground=[("disabled", p["bg"]), ("readonly", p["bg"])],
               foreground=[("disabled", p["muted"])], bordercolor=[("focus", p["accent"])])
        st.configure("TCombobox", fieldbackground=p["entry"], foreground=p["fg"], background=p["panel"],
                     padding=(5, 2))
        st.map("TCombobox", fieldbackground=[("readonly", p["entry"]), ("disabled", p["bg"])],
               foreground=[("readonly", p["fg"]), ("disabled", p["muted"])],
               selectbackground=[("readonly", p["entry"])], selectforeground=[("readonly", p["fg"])],
               bordercolor=[("focus", p["accent"])])
        self.option_add("*TCombobox*Listbox.background", p["entry"])
        self.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])
        st.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["trough"],
                     bordercolor=p["trough"], lightcolor=p["accent"], darkcolor=p["accent"],
                     thickness=int(10 * self.scale))
        st.configure("Thin.Horizontal.TProgressbar", background=p["accent"], troughcolor=p["trough"],
                     bordercolor=p["trough"], lightcolor=p["accent"], darkcolor=p["accent"],
                     thickness=int(6 * self.scale))
        lh = tkfont.Font(font=base_font).metrics("linespace")
        rowh = int(lh + 8 * max(1.0, self.scale))
        for name in ("Treeview", "Drives.Treeview"):
            st.configure(name, background=p["panel"], fieldbackground=p["panel"], foreground=p["fg"],
                         bordercolor=p["border"], rowheight=rowh)
            st.map(name, background=[("selected", p["select"])], foreground=[("selected", p["fg"])])
        st.configure("Treeview.Heading", background=p["heading"], foreground=p["muted"],
                     bordercolor=p["border"], relief="flat", font=(base_font[0], base_font[1] - 1, "bold"),
                     padding=(6, 3))
        st.map("Treeview.Heading", background=[("active", p["select"])])
        st.configure("Vertical.TScrollbar", background=p["panel"], troughcolor=p["bg"],
                     bordercolor=p["bg"], arrowcolor=p["muted"])
        st.configure("Horizontal.TScrollbar", background=p["panel"], troughcolor=p["bg"],
                     bordercolor=p["bg"], arrowcolor=p["muted"])
        st.configure("TPanedwindow", background=p["bg"])
        st.configure("Sash", sashthickness=6)
        for t in (self.tree, self.dtree):
            t.tag_configure("muted", foreground=p["muted"])
            t.tag_configure("stripe", background=p["stripe"])
            t.tag_configure("system", foreground=p["warn"])
            t.tag_configure("moved", foreground=p["ok"])
        for m in (self.menu, self.more_menu):
            m.configure(bg=p["panel"], fg=p["fg"], activebackground=p["accent"],
                        activeforeground=p["accent_fg"], bd=0)
        self.pv_canvas.configure(bg=p["bg"])

        size = 16
        self.imgs = {k: check_image(self, k, p, size) for k in ("on", "off", "partial", "none")}
        if self.scale >= 1.75:
            self.imgs = {k: v.zoom(2) for k, v in self.imgs.items()}
        self._style_checkbuttons(st, p, mode)
        self._icon = app_icon(self, PALETTES["Light"]["accent"])
        self.iconphoto(True, self._icon)
        self._logo = self._icon.subsample(2) if self.scale < 1.4 else self._icon
        self.logo_lbl.configure(image=self._logo)
        for iid in self.dtree.get_children():
            self._refresh_drive_row(iid)
        for i in list(self.inserted):
            self._refresh_row(i)
        self._preview()

    def _style_checkbuttons(self, st, p, mode):
        """Use the same teal tick boxes as the lists for every Checkbutton."""
        dim = dict(p, accent=_mix(p["accent"], p["bg"], 0.55), entry=p["bg"])
        imgs = (self.imgs["off"], self.imgs["on"], check_image(self, "off", dim),
                check_image(self, "on", dim))
        if self.scale >= 1.75:
            imgs = tuple(i.zoom(2) if i.width() < 20 else i for i in imgs)
        self._cb_imgs = getattr(self, "_cb_imgs", []) + [imgs]  # keep alive
        name = f"FH{len(self._cb_imgs)}.indicator"
        st.element_create(name, "image", imgs[0], ("disabled", "selected", imgs[3]),
                          ("disabled", imgs[2]), ("selected", imgs[1]), sticky="w",
                          padding=(0, 0, int(6 * max(1, self.scale)), 0))
        st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])

    # =============================================================================== drives
    def _load_drives(self, first=False):
        drives = core.list_drives()
        extra = [core.folder_as_drive(p) for p in self.cfg.get("extra_folders", []) if os.path.isdir(p)]
        self._set_drives(drives + extra, first=first)

    def _set_drives(self, drives: list[core.Drive], first=False, announce_new=False):
        old = set(self.drives)
        unchecked = set(self.cfg.get("unchecked_drives") or [])
        new_map = {d.key: d for d in drives}
        if not first:
            unchecked = {k for k in old if k not in self.drive_checked} | (unchecked - old)
        self.dtree.delete(*self.dtree.get_children())
        self.drives = {}
        added = []
        for d in drives:
            iid = d.key
            if iid in self.drives:
                continue
            self.drives[iid] = d
            if d.ready and iid not in unchecked:
                self.drive_checked.add(iid)
            else:
                self.drive_checked.discard(iid)
            self.dtree.insert("", "end", iid=iid)
            self._refresh_drive_row(iid)
            if not first and iid not in old:
                added.append(d)
        self.drive_checked &= set(self.drives)
        if announce_new and added:
            names = ", ".join(d.title for d in added)
            self.drive_note.configure(text=f"New: {names} — it's checked and will be included.",
                                      style="Ok.TLabel")
            self._log(f"Drive connected: {names}")
        removed = [k for k in old if k not in new_map]
        if announce_new and removed:
            self.drive_note.configure(text="A drive was removed.", style="Muted.TLabel")
        self._update_buttons()

    def _refresh_drive_row(self, iid):
        d = self.drives.get(iid)
        if not d:
            return
        if not d.ready:
            img, tags = self.imgs["none"], ("muted",)
        else:
            img = self.imgs["on" if iid in self.drive_checked else "off"]
            tags = ()
        kind = d.kind + (" · Windows" if d.system and core.IS_WIN else "")
        if not d.ready:
            kind += " (empty)"
        title = d.title
        self.dtree.item(iid, text=" " + title, image=img, tags=tags,
                        values=(kind, human_size(d.total) if d.total else "",
                                human_size(d.free) if d.ready and d.total else ""))

    def _drive_click(self, e):
        iid = self.dtree.identify_row(e.y)
        if not iid:
            return
        if self.dtree.identify_region(e.x, e.y) == "tree" and e.x < 34 * self.scale:
            self._toggle_drive(iid)
            return "break"
        self.dtree.focus(iid)
        self.dtree.selection_set(iid)
        self._toggle_drive(iid)
        return "break"

    def _toggle_drive(self, iid):
        if not iid or self.busy or not self.drives.get(iid) or not self.drives[iid].ready:
            return
        if iid in self.drive_checked:
            self.drive_checked.discard(iid)
        else:
            self.drive_checked.add(iid)
        self._refresh_drive_row(iid)
        self._save_drive_cfg()
        self._update_buttons()

    def _check_drives(self, on: bool):
        for iid, d in self.drives.items():
            if on and d.ready:
                self.drive_checked.add(iid)
            elif not on:
                self.drive_checked.discard(iid)
            self._refresh_drive_row(iid)
        self._save_drive_cfg()
        self._update_buttons()

    def _save_drive_cfg(self):
        self.cfg["unchecked_drives"] = sorted(k for k in self.drives if k not in self.drive_checked)
        core.save_settings(self.cfg)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder to search", parent=self)
        if not d:
            return
        d = os.path.abspath(d)
        extra = list(self.cfg.get("extra_folders") or [])
        if d not in extra:
            extra.append(d)
        self.cfg["extra_folders"] = extra
        core.save_settings(self.cfg)
        drv = core.folder_as_drive(d)
        drives = [x for x in self.drives.values()] + [drv]
        self._set_drives(drives)
        self.drive_checked.add(drv.key)
        self._refresh_drive_row(drv.key)
        self._save_drive_cfg()
        self._update_buttons()

    def _remove_folder(self):
        iid = self.dtree.focus()
        d = self.drives.get(iid)
        if not d or d.kind != "Folder":
            messagebox.showinfo(APP_NAME, "Select a folder you added (not a drive) to remove it.", parent=self)
            return
        self.cfg["extra_folders"] = [p for p in self.cfg.get("extra_folders", [])
                                     if os.path.normcase(p) != iid]
        core.save_settings(self.cfg)
        self._set_drives([x for k, x in self.drives.items() if k != iid])

    def _poll_drives(self):
        """Notice pendrives and disks being plugged in or removed."""
        if not self.busy and not self._drive_poll_busy:
            self._drive_poll_busy = True

            def work():
                try:
                    drives = core.list_drives()
                except Exception:  # noqa: BLE001
                    drives = None
                self.ui_q.put(lambda: self._drives_polled(drives))
            threading.Thread(target=work, daemon=True).start()
        self.after(3000, self._poll_drives)

    def _drives_polled(self, drives):
        self._drive_poll_busy = False
        if drives is None or self.busy:
            return
        folders = [d for d in self.drives.values() if d.kind == "Folder"]
        cur = {k: d.ready for k, d in self.drives.items() if d.kind != "Folder"}
        new = {d.key: d.ready for d in drives}
        if cur != new:
            self._set_drives(drives + folders, announce_new=True)
        else:  # just refresh free space quietly
            for d in drives:
                if d.key in self.drives and (self.drives[d.key].free != d.free):
                    self.drives[d.key] = d
                    self._refresh_drive_row(d.key)

    # =============================================================================== search setup
    def _exts(self) -> list[str]:
        out: list[str] = []
        for name, v in self.cat_vars.items():
            if v.get():
                out += [e for e in core.CATEGORIES[name] if e not in out]
        out += [e for e in core.parse_exts(self.ext_var.get()) if e not in out]
        return out

    def _search_changed(self):
        ex = self._exts()
        if ex:
            txt = "Looking for: " + ", ".join("." + e for e in ex)
            self.ext_summary.configure(text=mid(txt, 110), style="Muted.TLabel")
        else:
            self.ext_summary.configure(text="No file type chosen yet: tick a box or type extensions above.",
                                       style="Warn.TLabel")
        self._save_cfg()

    def _filters(self) -> core.ScanFilters | None:
        try:
            mn = core.parse_size(self.min_var.get())
            mx = core.parse_size(self.max_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Size must look like 500 KB, 10 MB or 2 GB.", parent=self)
            return None
        f = core.ScanFilters(exts=self._exts(), name=self.name_var.get().strip(), min_size=mn,
                             max_size=mx, skip_system=self.skip_sys_var.get(),
                             include_hidden=self.hidden_var.get(),
                             exclude=[self.dest_var.get().strip()])
        core.age_to_filters(self.age_var.get(), f)
        return f

    # =============================================================================== scanning
    def _start_scan(self):
        if self.busy:
            return
        roots = [self.drives[k].path for k in self.dtree.get_children() if k in self.drive_checked]
        if not roots:
            messagebox.showinfo(APP_NAME, "Tick at least one drive or folder in step 1.", parent=self)
            return
        f = self._filters()
        if f is None:
            return
        if not f.exts and not f.name:
            messagebox.showinfo(APP_NAME, "Choose at least one file type in step 2 "
                                          "(or type a name to search every kind of file).", parent=self)
            return
        self._save_cfg()
        self._clear_results()
        self.scanner = core.Scanner(roots, f, threads=4)
        self.scanner.start()
        self.busy = True
        self.scan_bar.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.scan_bar.start(12)
        self._log(f"Scan started: {', '.join(roots)}  types: {', '.join(f.exts) or 'any'}")
        self._update_buttons()

    def _pause_scan(self):
        s = self.scanner
        if not s or s.done:
            return
        if s.paused:
            s.resume()
            self.scan_bar.start(12)
        else:
            s.pause()
            self.scan_bar.stop()
        self._update_buttons()

    def _stop_scan(self):
        if self.scanner and not self.scanner.done:
            self.scanner.stop()

    def _stop_any(self):
        self._stop_scan()
        self.dup_stop.set()

    def _scan_tick(self):
        s = self.scanner
        batch = s.take_batch()
        if batch:
            start = len(self.items)
            self.items.extend(batch)
            self.pending.extend(range(start, start + len(batch)))
        if self.pending:
            self._insert_pending(limit=2500)
        el = time.time() - s.started
        if not s.done:
            state = "Paused" if s.paused else "Scanning"
            self.scan_lbl.configure(
                text=f"{state}…  {s.dirs:,} folders · {s.files_seen:,} files checked · "
                     f"{s.found:,} found ({human_size(s.found_bytes)}) · {dur(el)}   "
                     f"{mid(s.current, 70)}", style="Muted.TLabel")
            return
        if self.pending:
            return  # finish inserting first
        self.scan_bar.stop()
        self.scan_bar.grid_remove()
        took = dur((s.finished or time.time()) - s.started)
        word = "Stopped" if s.stop_ev.is_set() else "Done"
        msg = (f"{word} in {took}: {s.found:,} files found ({human_size(s.found_bytes)}) in "
               f"{s.dirs:,} folders.")
        if s.errors:
            msg += f"  {s.errors:,} places couldn't be opened (see Log)."
            for line in s.error_samples:
                self._log("  no access: " + line)
            if s.errors > len(s.error_samples):
                self._log(f"  … and {s.errors - len(s.error_samples):,} more")
        self.scan_lbl.configure(text=msg, style="Ok.TLabel" if s.found else "Muted.TLabel")
        self._log(msg)
        self.scanner = None
        self.busy = False
        self._fill_types()
        if self.sort_col:
            self._apply_view()
        self._update_buttons()
        self._refresh_counts()
        if not s.found and not s.stop_ev.is_set():
            self.scan_lbl.configure(text=msg + "  Try more drives, more types, or untick \"Skip Windows…\".")

    # =============================================================================== results list
    def _clear_results(self):
        self.tree.delete(*self.tree.get_children())
        self.items, self.checked, self.visible, self.pending = [], set(), [], []
        self.inserted = set()
        self.dup_groups = []
        self.only_dup_var.set(False)
        self.type_var.set("All types")
        self.type_cb.configure(values=["All types"])
        self._refresh_counts()
        self._preview()

    def _row_values(self, it: core.FileItem):
        dup = ""
        if it.dup:
            g = self.dup_groups[it.dup - 1] if it.dup - 1 < len(self.dup_groups) else []
            dup = f"Set {it.dup} · " + ("oldest" if g and g[0] is it else "extra copy")
        return (it.ext.upper(), human_size(it.size), human_time(it.mtime), dup, it.folder)

    def _insert_pending(self, limit=2500):
        chunk, self.pending = self.pending[:limit], self.pending[limit:]
        ok = self._matcher()
        for i in chunk:
            if not ok(self.items[i]):
                continue
            self._insert_row(i)
        self._refresh_counts()

    def _insert_row(self, i, index="end"):
        it = self.items[i]
        iid = str(i)
        tags = ("stripe",) if len(self.visible) % 2 else ()
        self.tree.insert("", index, iid=iid, text=" " + it.name,
                         image=self.imgs["on" if i in self.checked else "off"],
                         values=self._row_values(it), tags=tags)
        self.inserted.add(i)
        self.visible.append(i)

    def _refresh_row(self, i):
        if i not in self.inserted:
            return
        it = self.items[i]
        self.tree.item(str(i), image=self.imgs["on" if i in self.checked else "off"],
                       text=" " + it.name, values=self._row_values(it))

    def _matcher(self):
        q = self.filter_var.get().strip().lower()
        t = self.type_var.get()
        t = "" if t.startswith("All types") else t.split(" ")[0].lower()
        only_dup = self.only_dup_var.get()

        def ok(it: core.FileItem) -> bool:
            if it.gone:
                return False
            if t and it.ext != t:
                return False
            if only_dup and not it.dup:
                return False
            if q and q not in it.path.lower():
                return False
            return True
        return ok

    def _filter_soon(self):
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(250, self._apply_view)

    def _apply_view(self):
        """Rebuild the visible list (filter + sort)."""
        self._filter_job = None
        ok = self._matcher()
        pend = set(self.pending)
        idx = [i for i in range(len(self.items)) if i not in pend and ok(self.items[i])]
        self._set_heading_arrows()
        key = self._sort_key()
        if key:
            idx.sort(key=lambda i: key(self.items[i]), reverse=self.sort_rev)
        self.tree.delete(*self.tree.get_children())
        self.inserted = set()
        self.visible = []
        for i in idx:
            self._insert_row(i)
        self._refresh_counts()

    def _sort_key(self):
        c = self.sort_col
        return {"#0": lambda it: it.name.lower(), "type": lambda it: (it.ext, it.name.lower()),
                "size": lambda it: it.size, "modified": lambda it: it.mtime,
                "dup": lambda it: (it.dup == 0, it.dup, it.mtime),
                "folder": lambda it: (it.folder.lower(), it.name.lower())}.get(c)

    def _sort_by(self, col):
        if self.busy and self.scanner:
            return
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, col in ("size", "modified")
        self._apply_view()

    def _set_heading_arrows(self):
        names = {"#0": "Name", "type": "Type", "size": "Size", "modified": "Modified",
                 "dup": "Duplicate", "folder": "Folder"}
        for c, t in names.items():
            arrow = (" ▼" if self.sort_rev else " ▲") if c == self.sort_col else ""
            self.tree.heading(c, text=t + arrow)

    def _fill_types(self):
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it.ext] = counts.get(it.ext, 0) + 1
        vals = [f"All types ({len(self.items):,})"] + [
            f"{e.upper() or '(none)'} ({n:,})" for e, n in sorted(counts.items(), key=lambda x: -x[1])]
        self.type_cb.configure(values=vals)
        self.type_var.set(vals[0])

    def _refresh_counts(self):
        live = [it for it in self.items if not it.gone]
        n = len(live)
        tot = sum(it.size for it in live)
        ck = len(self.checked)
        cb = sum(self.items[i].size for i in self.checked)
        shown = len(self.visible)
        txt = f"{n:,} files found ({human_size(tot)})"
        if shown != n:
            txt += f" · {shown:,} shown"
        txt += f" · {ck:,} checked ({human_size(cb)})"
        if n:
            txt += "    Tick the boxes · Space ticks selected rows · double-click opens"
        self.count_lbl.configure(text=txt)
        verb = "Move" if self.mode_var.get() == "move" else "Copy"
        self.btn_go.configure(text=f"{verb} {ck:,} file{'s' if ck != 1 else ''} ({human_size(cb)})"
                              if ck else f"{verb} checked files")
        self._update_buttons()

    # -- checking
    def _tree_click(self, e):
        region = self.tree.identify_region(e.x, e.y)
        if region == "heading":
            return
        iid = self.tree.identify_row(e.y)
        if iid and region == "tree" and e.x < 34 * self.scale:
            self._toggle(int(iid))
            self._refresh_counts()
            return "break"

    def _toggle(self, i: int, value: bool | None = None):
        on = (i not in self.checked) if value is None else value
        if on:
            self.checked.add(i)
        else:
            self.checked.discard(i)
        self._refresh_row(i)

    def _space_toggle(self, _e=None):
        sel = [int(x) for x in self.tree.selection()]
        if not sel:
            return "break"
        on = not all(i in self.checked for i in sel)
        for i in sel:
            self._toggle(i, on)
        self._refresh_counts()
        return "break"

    def _check_visible(self, on: bool | None):
        for i in self.visible:
            self._toggle(i, None if on is None else on)
        self._refresh_counts()

    def _check_selection(self, on: bool):
        for x in self.tree.selection():
            self._toggle(int(x), on)
        self._refresh_counts()

    def _check_same_folder(self):
        sel = self.tree.selection()
        if not sel:
            return
        folder = self.items[int(sel[0])].folder
        for i in self.visible:
            if self.items[i].folder == folder:
                self._toggle(i, True)
        self._refresh_counts()

    def _check_same_type(self):
        sel = self.tree.selection()
        if not sel:
            return
        ext = self.items[int(sel[0])].ext
        for i in self.visible:
            if self.items[i].ext == ext:
                self._toggle(i, True)
        self._refresh_counts()

    def _check_larger(self):
        from tkinter import simpledialog
        t = simpledialog.askstring(APP_NAME, "Tick every shown file larger than (e.g. 100 MB):",
                                   parent=self)
        if not t:
            return
        try:
            n = core.parse_size(t) or 0
        except ValueError:
            messagebox.showerror(APP_NAME, "That doesn't look like a size.", parent=self)
            return
        for i in self.visible:
            self._toggle(i, self.items[i].size > n)
        self._refresh_counts()

    def _tree_double(self, e=None):
        if e is not None and getattr(e, "y", None) is not None:
            if self.tree.identify_region(e.x, e.y) == "heading":
                return
        self._act_selected(open_path)
        return "break"

    def _tree_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.menu.tk_popup(e.x_root, e.y_root)

    def _act_selected(self, fn):
        sel = self.tree.selection()
        if sel:
            fn(self.items[int(sel[0])].path)

    def _copy_selected_path(self):
        sel = self.tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(self.items[int(x)].path for x in sel))
            self.status.configure(text=f"Copied {len(sel)} path(s) to the clipboard.")

    def _copy_paths(self):
        if not self.checked:
            messagebox.showinfo(APP_NAME, "Tick some files first.", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(self.items[i].path for i in sorted(self.checked)))
        self.status.configure(text=f"Copied {len(self.checked):,} paths to the clipboard.")

    def _export(self):
        if not self.items:
            messagebox.showinfo(APP_NAME, "Nothing to export yet. Run a scan first.", parent=self)
            return
        p = filedialog.asksaveasfilename(parent=self, title="Save the list", defaultextension=".csv",
                                         initialfile="FileHunter results.csv",
                                         filetypes=[("CSV (opens in Excel)", "*.csv")])
        if not p:
            return
        items = [self.items[i] for i in (self.visible or range(len(self.items)))]
        try:
            core.export_csv(items, p)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save:\n{e}", parent=self)
            return
        self.status.configure(text=f"Saved {len(items):,} rows to {p}")

    # =============================================================================== preview
    def _preview(self):
        c = self.pv_canvas
        c.delete("all")
        p = self.pal if hasattr(self, "pal") else PALETTES["Light"]
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        w = int(c.cget("width"))
        h = int(c.cget("height"))
        if not sel:
            self.pv_name.configure(text="")
            self.pv_info.configure(text="Select a file to see it here.")
            self._draw_badge(c, "", p, w, h)
            self.thumb_for = None
            return
        if len(sel) > 1:
            its = [self.items[int(x)] for x in sel]
            self.pv_name.configure(text=f"{len(its):,} rows selected")
            self.pv_info.configure(text=f"{human_size(sum(i.size for i in its))} in total.\n"
                                        "Press Space to tick or untick them all.")
            self._draw_badge(c, str(len(its)), p, w, h)
            self.thumb_for = None
            return
        it = self.items[int(sel[0])]
        self.pv_name.configure(text=it.name)
        info = [f"{human_size(it.size)}  ·  {it.ext.upper() or 'no extension'}",
                f"Modified {human_time(it.mtime)}", "", it.folder]
        if it.dup and it.dup - 1 < len(self.dup_groups):
            g = self.dup_groups[it.dup - 1]
            info.append("")
            info.append(f"Same contents as {len(g) - 1} other file{'s' if len(g) > 2 else ''}.")
        if core.looks_like_system(it.path):
            info.append("")
            info.append("Inside a system or program folder: copying is safe, moving may break a program.")
        self.pv_info.configure(text="\n".join(info))
        self._draw_badge(c, it.ext.upper()[:5] or "?", p, w, h)
        if it.ext in core.IMAGE_EXTS and (Image is not None or it.ext in ("png", "gif")):
            self.thumb_for = it.path
            c.create_text(w // 2, h - 12, text="Loading preview…", fill=p["muted"], font=ui_font())
            threading.Thread(target=self._load_thumb, args=(it.path, w, h), daemon=True).start()
        else:
            self.thumb_for = None

    def _draw_badge(self, c, text, p, w, h):
        bw, bh = int(w * 0.46), int(h * 0.62)
        x0, y0 = (w - bw) // 2, (h - bh) // 2
        fold = int(bw * 0.28)
        pts = [x0, y0, x0 + bw - fold, y0, x0 + bw, y0 + fold, x0 + bw, y0 + bh, x0, y0 + bh]
        c.create_polygon(pts, fill=p["panel"], outline=p["border"], width=2)
        c.create_polygon([x0 + bw - fold, y0, x0 + bw - fold, y0 + fold, x0 + bw, y0 + fold],
                         fill=p["trough"], outline=p["border"], width=2)
        if text:
            f = ui_font()
            c.create_rectangle(x0 - 10, y0 + bh - int(bh * 0.42), x0 + bw - 14, y0 + bh - int(bh * 0.14),
                               fill=p["accent"], outline="")
            c.create_text(x0 + (bw - 24) // 2, y0 + bh - int(bh * 0.28), text=text,
                          fill=p["accent_fg"], font=(f[0], f[1] + 4, "bold"))

    def _load_thumb(self, path, w, h):
        try:
            if Image is not None:
                im = Image.open(path)
                try:
                    im.draft("RGB", (w * 2, h * 2))
                except Exception:  # noqa: BLE001
                    pass
                im = ImageOps.exif_transpose(im)
                im.thumbnail((w, h))
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA")
                self.thumb_q.put((path, im))
            else:
                self.thumb_q.put((path, "tk"))
        except Exception:  # noqa: BLE001
            self.thumb_q.put((path, None))

    def _thumb_tick(self):
        while True:
            try:
                path, im = self.thumb_q.get_nowait()
            except queue.Empty:
                return
            if path != self.thumb_for:
                continue
            c = self.pv_canvas
            w, h = int(c.cget("width")), int(c.cget("height"))
            try:
                if im == "tk":
                    ph = tk.PhotoImage(file=path)
                    f = max(1, max(ph.width() // w, ph.height() // h) + 1)
                    ph = ph.subsample(f) if f > 1 else ph
                elif im is None:
                    raise ValueError
                else:
                    ph = ImageTk.PhotoImage(im)
            except Exception:  # noqa: BLE001
                c.delete("all")
                self._draw_badge(c, os.path.splitext(path)[1][1:].upper()[:5], self.pal, w, h)
                continue
            self._thumb = ph
            c.delete("all")
            c.create_image(w // 2, h // 2, image=ph)

    # =============================================================================== duplicates
    def _find_dups(self):
        if self.busy or not self.items:
            if not self.items:
                messagebox.showinfo(APP_NAME, "Run a scan first.", parent=self)
            return
        self.busy = True
        self.dup_stop.clear()
        self.dup_progress = (0, 1)
        items = list(self.items)

        def work():
            groups = core.find_duplicates(items, self.dup_stop,
                                          progress=lambda d, t, s: setattr(self, "dup_progress", (d, t)))
            self.ui_q.put(lambda: self._dups_done(groups))
        self.dup_job = threading.Thread(target=work, daemon=True)
        self.dup_job.start()
        self.scan_bar.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.scan_bar.start(12)
        self._update_buttons()

    def _dups_done(self, groups):
        self.dup_job = None
        self.busy = False
        self.scan_bar.stop()
        self.scan_bar.grid_remove()
        if self.dup_stop.is_set():
            self.scan_lbl.configure(text="Duplicate search stopped.", style="Muted.TLabel")
            for it in self.items:
                it.dup = 0
            self.dup_groups = []
        else:
            self.dup_groups = groups
            extra = sum(len(g) - 1 for g in groups)
            waste = sum(g[0].size * (len(g) - 1) for g in groups)
            if groups:
                msg = (f"Found {len(groups):,} sets of identical files: {extra:,} extra copies using "
                       f"{human_size(waste)}. Use More ▾ › Check extra copies to tick them.")
                self.only_dup_var.set(True)
            else:
                msg = "No duplicates: every file found is unique."
            self.scan_lbl.configure(text=msg, style="Ok.TLabel")
            self._log(msg)
        self.sort_col = "dup" if groups and not self.dup_stop.is_set() else self.sort_col
        self.sort_rev = False
        self._apply_view()
        self._update_buttons()

    def _check_extra_copies(self):
        if not self.dup_groups:
            messagebox.showinfo(APP_NAME, "Use More ▾ › Find duplicate files first.", parent=self)
            return
        self.checked.clear()
        for g in self.dup_groups:
            for it in g[1:]:
                self.checked.add(self._index_of(it))
        for i in list(self.inserted):
            self._refresh_row(i)
        self._refresh_counts()
        self.status.configure(text="Ticked every extra copy; the oldest file of each set stays unticked.")

    def _index_of(self, it):
        if not hasattr(self, "_idx_cache") or self._idx_cache[0] is not self.items or \
                len(self._idx_cache[1]) != len(self.items):
            self._idx_cache = (self.items, {id(x): n for n, x in enumerate(self.items)})
        return self._idx_cache[1][id(it)]

    # =============================================================================== copy / move
    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.home()),
                                    title="Choose where to put the files", parent=self)
        if d:
            self.dest_var.set(str(Path(d)))
            self._save_cfg()

    def _open_dest(self):
        d = self.dest_var.get().strip()
        if d and os.path.isdir(d):
            open_path(d)
        else:
            messagebox.showinfo(APP_NAME, "That folder doesn't exist yet. It's created when files are "
                                          "copied into it.", parent=self)

    def _mode_changed(self):
        self._save_cfg()
        self._refresh_counts()

    def _layout_key(self):
        return {v: k for k, v in core.LAYOUTS.items()}.get(self.layout_var.get(), "flat")

    def _conflict_key(self):
        return {v: k for k, v in core.CONFLICTS.items()}.get(self.conflict_var.get(), "rename")

    def _start_transfer(self):
        if self.busy:
            return
        sel = [self.items[i] for i in sorted(self.checked)]
        if not sel:
            messagebox.showinfo(APP_NAME, "Tick the files you want in the results list first.", parent=self)
            return
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showinfo(APP_NAME, "Choose a folder to put the files in.", parent=self)
            return
        dest = os.path.abspath(os.path.expanduser(dest))
        mode = self.mode_var.get()
        verb = "Move" if mode == "move" else "Copy"

        if mode == "move":
            sysf = [it for it in sel if core.looks_like_system(it.path)]
            if sysf:
                ans = messagebox.askyesnocancel(
                    APP_NAME,
                    f"{len(sysf):,} of these files are inside Windows, program or app-data folders.\n"
                    "Moving them could stop programs from working.\n\n"
                    "Yes = leave those where they are and move the rest\n"
                    "No = move them anyway\nCancel = do nothing", parent=self, icon="warning")
                if ans is None:
                    return
                if ans:
                    ids = {id(x) for x in sysf}
                    sel = [it for it in sel if id(it) not in ids]
                    if not sel:
                        return
        # don't try to put files inside themselves / into a folder being collected
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't create that folder:\n{dest}\n\n{e.strerror or e}",
                                 parent=self)
            return
        need = core.bytes_to_write(sel, dest, mode)
        try:
            free = core.shutil.disk_usage(dest).free
        except OSError:
            free = None
        if free is not None and need > free - 50 * 1024 * 1024:
            messagebox.showerror(
                APP_NAME, f"Not enough space in the destination.\n\nNeeded: {human_size(need)}\n"
                          f"Free: {human_size(free)}\n\nChoose another drive, or tick fewer files.",
                parent=self)
            return
        total = sum(it.size for it in sel)
        text = f"{verb} {len(sel):,} files ({human_size(total)}) to\n{dest}?"
        if mode == "move":
            text += "\n\nThey will be removed from where they are now. You can put them back " \
                    "afterwards with \"Undo last\"."
        if not messagebox.askokcancel(APP_NAME, text, parent=self):
            return
        names = {}
        for k, d in self.drives.items():
            if core.IS_WIN and len(d.path) <= 3:
                names[k] = f"Drive {d.path[0].upper()}"
            elif d.path == "/":
                names[k] = "System"
            else:
                names[k] = d.label or d.name
        self._save_cfg()
        self.transfer = core.Transfer(sel, dest, mode=mode, layout=self._layout_key(),
                                      conflict=self._conflict_key(), skip_identical=self.ident_var.get(),
                                      verify=self.verify_var.get(), drive_names=names)
        self.transfer.start()
        self.xfer_row.grid()
        self.busy = True
        self._last_speed = (time.time(), 0)
        self._log(f"{verb} started: {len(sel):,} files to {dest}")
        self._update_buttons()

    def _pause_transfer(self):
        t = self.transfer
        if t and not t.done:
            t.resume() if t.paused else t.pause()
            self._update_buttons()

    def _stop_transfer(self):
        if self.transfer and not self.transfer.done:
            self.transfer.stop()

    def _transfer_tick(self):
        t = self.transfer
        tot = max(1, t.total_bytes)
        now = t.bytes_now
        self.xfer_bar["value"] = int(now * 1000 / tot)
        el = max(0.001, time.time() - t.started)
        spd = now / el
        if not t.done:
            eta = (t.total_bytes - now) / spd if spd > 1 else 0
            state = "Paused" if t.paused else ("Moving" if t.mode == "move" else "Copying")
            self.xfer_lbl.configure(
                text=f"{state} {t.index + 1:,} of {len(t.items):,} · {human_size(now)} of "
                     f"{human_size(t.total_bytes)} · {human_size(spd)}/s · about {dur(eta)} left   "
                     f"{mid(os.path.basename(t.current), 60)}", style="Muted.TLabel")
            return
        self._transfer_done()

    def _transfer_done(self):
        t = self.transfer
        self.transfer = None
        self.busy = False
        c = t.counts
        verb = "Moved" if t.mode == "move" else "Copied"
        parts = [f"{verb} {c['ok']:,}"]
        if c["identical"]:
            parts.append(f"{c['identical']:,} already there")
        if c["skipped"]:
            parts.append(f"{c['skipped']:,} skipped")
        if c["partial"]:
            parts.append(f"{c['partial']:,} copied but original kept")
        if c["failed"]:
            parts.append(f"{c['failed']:,} failed")
        stopped = t.stop_ev.is_set()
        msg = (" · ".join(parts) + f" in {dur(t.finished - t.started)}"
               + ("  (stopped)" if stopped else ""))
        bad = c["failed"] or c["partial"]
        self.xfer_lbl.configure(text=msg + ("  — see Log for details." if bad else ""),
                                style="Err.TLabel" if c["failed"] else "Ok.TLabel")
        self.xfer_bar["value"] = 1000 if not stopped else self.xfer_bar["value"]
        self._log(msg)
        for line in t.log:
            self._log("  " + line)
        # moved files now live somewhere else: update the list
        if t.mode == "move":
            by_path = {e["src"]: e for e in t.journal if e["status"] == "ok"}
            for i, it in enumerate(self.items):
                e = by_path.get(it.path)
                if e:
                    it.path, it.name = e["dst"], os.path.basename(e["dst"])
                    self.checked.discard(i)
                    if i in self.inserted:
                        self._refresh_row(i)
                        self.tree.item(str(i), tags=("moved",))
        self._refresh_counts()
        self._update_buttons()
        if c["ok"] and not stopped:
            if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the folder now?", parent=self):
                open_path(t.dest)

    def _undo(self):
        if self.busy:
            return
        run = core.last_undoable()
        if not run:
            messagebox.showinfo(APP_NAME, "There's nothing to undo.", parent=self)
            return
        n = len(run["entries"])
        when = run["time"].replace("T", " ")
        if run["mode"] == "move":
            q = (f"Put back the {n:,} files moved on {when}?\n\nThey go back to their original folders.")
        else:
            q = (f"Delete the {n:,} copies made on {when} in\n{run['dest']}?\n\n"
                 "The original files are not touched.")
        if not messagebox.askyesno(APP_NAME, q, parent=self):
            return
        self.busy = True
        self._update_buttons()
        self.xfer_row.grid()
        self.xfer_lbl.configure(text="Undoing…", style="Muted.TLabel")

        def work():
            res = core.undo_run(run)
            self.ui_q.put(lambda: self._undo_done(run, res))
        threading.Thread(target=work, daemon=True).start()

    def _undo_done(self, run, res):
        self.busy = False
        if run["mode"] == "move":
            msg = f"Put back {res['restored']:,} files."
            back = {e["dst"]: e["src"] for e in run["entries"]}
            for i, it in enumerate(self.items):
                if it.path in back:
                    it.path = back[it.path]
                    it.name = os.path.basename(it.path)
                    if i in self.inserted:
                        self._refresh_row(i)
                        self.tree.item(str(i), tags=())
        else:
            msg = f"Removed {res['removed']:,} copies."
        if res["failed"]:
            msg += f" {res['failed']:,} couldn't be undone (see Log)."
        for m in res["messages"]:
            self._log("  undo: " + m)
        self._log(msg)
        self.xfer_lbl.configure(text=msg, style="Err.TLabel" if res["failed"] else "Ok.TLabel")
        self._update_buttons()

    # =============================================================================== delete
    def _delete_checked(self):
        if self.busy or not self.checked:
            return
        idx = sorted(self.checked)
        sel = [self.items[i] for i in idx]
        total = sum(it.size for it in sel)
        drives = list(self.drives.values())
        no_bin = [it for it in sel if not core.can_recycle(it.path, drives)]
        sysf = [it for it in sel if core.looks_like_system(it.path)]
        choice = self._ask_delete(len(sel), total, len(no_bin), len(sysf))
        if not choice:
            return
        if choice == "skip_system":
            ids = {id(x) for x in sysf}
            idx = [i for i in idx if id(self.items[i]) not in ids]
            choice = self._last_delete_mode
        permanent = choice == "permanent"
        if not idx:
            return
        if permanent and not messagebox.askyesno(
                APP_NAME, f"Really delete {len(idx):,} files permanently?\n\n"
                          "They will NOT go to the " + core.TRASH_NAME + " and cannot be brought back.",
                icon="warning", default="no", parent=self):
            return
        self._del_idx = idx
        self.deleter = core.Deleter([self.items[i].path for i in idx], permanent=permanent)
        self.deleter.start()
        self.busy = True
        self.xfer_row.grid()
        self.xfer_bar["value"] = 0
        self._log(("Permanent delete" if permanent else f"Moving to {core.TRASH_NAME}") +
                  f": {len(idx):,} files")
        self._update_buttons()

    def _ask_delete(self, n, total, n_no_bin, n_sys):
        """Small dialog: Recycle Bin / permanent, with warnings. Returns the choice or None."""
        p = self.pal
        win = tk.Toplevel(self)
        win.title("Delete files")
        win.configure(bg=p["bg"])
        win.transient(self)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Delete {n:,} checked file{'s' if n != 1 else ''} ({human_size(total)})?",
                  style="Strong.TLabel").pack(anchor="w")
        mode = tk.StringVar(value="recycle")
        ttk.Radiobutton(body, text=f"Move to the {core.TRASH_NAME} (you can restore them from there)",
                        value="recycle", variable=mode).pack(anchor="w", pady=(10, 2))
        ttk.Radiobutton(body, text="Delete permanently (cannot be undone)", value="permanent",
                        variable=mode).pack(anchor="w")
        wl = int(470 * self.scale)
        if n_no_bin:
            ttk.Label(body, style="Warn.TLabel", wraplength=wl, justify="left",
                      text=f"{n_no_bin:,} of these are on a pendrive, memory card or network drive. "
                           f"Windows has no {core.TRASH_NAME} there, so those are deleted permanently "
                           "either way.").pack(anchor="w", pady=(10, 0))
        skip_sys = tk.BooleanVar(value=bool(n_sys))
        if n_sys:
            ttk.Label(body, style="Err.TLabel", wraplength=wl, justify="left",
                      text=f"{n_sys:,} are inside Windows, program or app-data folders. Deleting them "
                           "can stop programs from working.").pack(anchor="w", pady=(10, 2))
            ttk.Checkbutton(body, text="Leave those ones alone", variable=skip_sys).pack(anchor="w")
        res = {"v": None}

        def ok():
            res["v"] = mode.get()
            self._last_delete_mode = mode.get()
            if n_sys and skip_sys.get():
                res["v"] = "skip_system"
            win.destroy()
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(16, 0))
        ttk.Button(bar, text="Delete", style="Danger.TButton", command=ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 6))
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()
        win.focus_set()
        self.wait_window(win)
        return res["v"]

    def _delete_tick(self):
        d = self.deleter
        n = max(1, len(d.paths))
        self.xfer_bar["value"] = d.done_count * 1000 // n
        if not d.done:
            self.xfer_lbl.configure(text=f"Deleting… {d.done_count:,} of {len(d.paths):,}",
                                    style="Muted.TLabel")
            return
        self.deleter = None
        self.busy = False
        gone = set(d.deleted)
        for i in self._del_idx:
            it = self.items[i]
            if it.path in gone:
                it.gone = True
                self.checked.discard(i)
                if i in self.inserted:
                    self.tree.delete(str(i))
                    self.inserted.discard(i)
        self.visible = [i for i in self.visible if not self.items[i].gone]
        where = "permanently" if d.permanent else f"to the {core.TRASH_NAME}"
        msg = f"Deleted {len(gone):,} file{'s' if len(gone) != 1 else ''} {where}."
        fails = d.failed
        if fails:
            msg += f"  {len(fails):,} couldn't be deleted (see Log)."
            for pth, e in fails.items():
                self._log(f"  not deleted: {pth}  ({e})")
        self._log(msg)
        self.xfer_lbl.configure(text=msg, style="Err.TLabel" if fails else "Ok.TLabel")
        self.xfer_bar["value"] = 1000
        self._refresh_counts()
        self._preview()
        self._update_buttons()

    # =============================================================================== log
    def _log(self, line: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {line}")
        if len(self.log_lines) > 5000:
            self.log_lines = self.log_lines[-4000:]

    def _show_log(self):
        p = self.pal
        win = tk.Toplevel(self)
        win.title(f"{APP_NAME} log")
        win.geometry(f"{int(820 * self.scale)}x{int(420 * self.scale)}")
        win.configure(bg=p["bg"])
        txt = tk.Text(win, wrap="none", bg=p["log_bg"], fg=p["fg"], insertbackground=p["fg"],
                      font=mono_font(), relief="flat", padx=8, pady=6)
        ys = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        bar = ttk.Frame(win, padding=6)
        bar.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", "\n".join(self.log_lines) or "Nothing logged yet.")
        txt.configure(state="disabled")

        def save():
            fp = filedialog.asksaveasfilename(parent=win, defaultextension=".txt",
                                              initialfile="FileHunter log.txt")
            if fp:
                Path(fp).write_text("\n".join(self.log_lines), encoding="utf-8")
        ttk.Button(bar, text="Save…", command=save).pack(side="right")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right", padx=(0, 6))

    # =============================================================================== state
    def _update_buttons(self):
        scanning = self.scanner is not None and not self.scanner.done
        moving = self.transfer is not None and not self.transfer.done
        dups = self.dup_job is not None
        idle = not self.busy

        def st(w, on):
            w.state(["!disabled"] if on else ["disabled"])
        has_drive = bool(self.drive_checked)
        st(self.btn_scan, idle and has_drive)
        st(self.btn_scan_pause, scanning)
        st(self.btn_scan_stop, scanning or dups)
        self.btn_scan_pause.configure(text="Resume" if scanning and self.scanner.paused else "Pause")
        for w in self.drive_btns + self.search_widgets:
            st(w, idle)
        for w in self.collect_widgets:
            st(w, not moving)
        st(self.btn_go, idle and bool(self.checked))
        st(self.btn_undo, idle and core.last_undoable() is not None)
        st(self.btn_delete, idle and bool(self.checked))
        st(self.btn_x_pause, moving)
        st(self.btn_x_stop, moving)
        self.btn_x_pause.configure(text="Resume" if moving and self.transfer.paused else "Pause")
        st(self.btn_more, idle and bool(self.items))
        for w in (self.btn_all, self.btn_none, self.btn_invert):
            st(w, bool(self.visible) and not moving)
        st(self.btn_open, bool(self.tree.selection()))
        st(self.btn_reveal, bool(self.tree.selection()))
        if not has_drive and idle:
            self.drive_note.configure(text="Tick at least one drive to search.", style="Warn.TLabel")
        elif str(self.drive_note.cget("style")) == "Warn.TLabel":
            self.drive_note.configure(text="Plug in a pendrive or disk and it shows up here by itself.",
                                      style="Muted.TLabel")

    def _save_cfg(self):
        self.cfg.update(
            categories=[k for k, v in self.cat_vars.items() if v.get()],
            custom_exts=self.ext_var.get(), name_filter=self.name_var.get(),
            min_size=self.min_var.get(), max_size=self.max_var.get(), age=self.age_var.get(),
            skip_system=self.skip_sys_var.get(), include_hidden=self.hidden_var.get(),
            dest=self.dest_var.get().strip() or self.cfg["dest"], mode=self.mode_var.get(),
            layout=self._layout_key(), conflict=self._conflict_key(),
            skip_identical=self.ident_var.get(), verify=self.verify_var.get(),
            theme=self.theme_var.get())
        core.save_settings(self.cfg)

    def _tick(self):
        try:
            while True:
                try:
                    fn = self.ui_q.get_nowait()
                except queue.Empty:
                    break
                fn()
            if self.scanner is not None:
                self._scan_tick()
            elif self.pending:
                self._insert_pending()
            if self.transfer is not None:
                self._transfer_tick()
            if self.deleter is not None:
                self._delete_tick()
            if self.dup_job is not None:
                d, t = self.dup_progress
                pct = d * 100 // t if t else 0
                self.scan_lbl.configure(text=f"Looking for identical files… {pct}% "
                                             f"({human_size(d)} of {human_size(t)} compared). Esc stops.",
                                        style="Muted.TLabel")
            self._thumb_tick()
        finally:
            self.after(120, self._tick)

    def _close(self):
        if self.transfer is not None and not self.transfer.done:
            if not messagebox.askyesno(APP_NAME, "Files are still being copied or moved.\n"
                                                 "Stop and quit? (The file in progress is cleaned up.)",
                                       parent=self):
                return
            self.transfer.stop()
            for _ in range(50):
                if self.transfer.done:
                    break
                self.update()
                time.sleep(0.05)
        if self.scanner is not None:
            self.scanner.stop()
        self.dup_stop.set()
        self.cfg["geometry"] = self.geometry() if self.state() == "normal" else ""
        self._save_cfg()
        self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FileHunter.App")
        except Exception:  # noqa: BLE001
            pass
    app = App()
    app._search_changed()
    app.mainloop()


if __name__ == "__main__":
    main()
