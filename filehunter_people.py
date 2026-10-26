"""FileHunter 'Sort photos by face' window."""
from __future__ import annotations

import base64
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import filehunter_core as core
import filehunter_faces as faces
from filehunter_core import APP_NAME, human_size


MAX_ROWS = 300   # people rows drawn at once (drawing thousands of faces would freeze the window)


class PeopleWindow(tk.Toplevel):
    def __init__(self, app, photos: list[core.FileItem], source_note: str):
        super().__init__(app)
        self.app = app
        self.photos = photos
        self.by_path = {it.path: it for it in photos}
        self.pal, self.s = app.pal, app.scale
        self.people: list[faces.Person] = []
        self.no_face: list[str] = []
        self.errors: dict[str, str] = {}
        self.scan: faces.FaceScan | None = None
        self.job: threading.Thread | None = None
        self.grouping: threading.Thread | None = None
        self.group_stop = threading.Event()
        self.group_progress = (0, 1, "")
        self.job_msg = ""
        self.job_progress = (0, 0)
        self.thumbs: dict[int, tk.PhotoImage] = {}
        self.title(f"{APP_NAME} · sort photos by face")
        self.configure(bg=self.pal["bg"])
        self.geometry(f"{int(1000 * self.s)}x{int(760 * self.s)}")
        self.minsize(int(860 * self.s), int(620 * self.s))
        self.transient(app)
        self._build(source_note)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._check_setup()
        self.after(150, self._tick)

    # ------------------------------------------------------------------ layout
    def _build(self, source_note):
        p, s = self.pal, self.s
        st = ttk.Style(self)
        st.configure("People.Treeview", rowheight=int(80 * max(1.0, s)), background=p["panel"],
                     fieldbackground=p["panel"], foreground=p["fg"])
        st.map("People.Treeview", background=[("selected", p["select"])],
               foreground=[("selected", p["fg"])])
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # step 1: find faces
        a = ttk.LabelFrame(main, text=" 1 · Find the faces ", padding=8)
        a.grid(row=0, column=0, sticky="ew")
        a.columnconfigure(1, weight=1)
        ttk.Label(a, text=f"{len(self.photos):,} photos {source_note}.").grid(row=0, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(a)
        btns.grid(row=0, column=2, sticky="e")
        self.btn_setup = ttk.Button(btns, text="Set up", command=self._do_setup)
        self.btn_find = ttk.Button(btns, text="Find faces", style="Accent.TButton", command=self._find)
        self.btn_find.pack(side="left")
        self.btn_pause = ttk.Button(btns, text="Pause", command=self._pause)
        self.btn_pause.pack(side="left", padx=(6, 0))
        self.btn_stop = ttk.Button(btns, text="Stop", command=self._stop)
        self.btn_stop.pack(side="left", padx=(6, 0))
        self.bar = ttk.Progressbar(a, mode="determinate", maximum=1000)
        self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.msg = ttk.Label(a, text="", style="Muted.TLabel", wraplength=int(940 * s), justify="left")
        self.msg.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # step 2: people
        b = ttk.LabelFrame(main, text=" 2 · People found: name them and put them in priority order ",
                           padding=8)
        b.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        b.columnconfigure(0, weight=1)
        b.rowconfigure(1, weight=1)
        ttk.Label(b, text="The list order is the priority. A group photo goes to the person highest in "
                          "this list. Double-click to rename.", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        lf = ttk.Frame(b)
        lf.grid(row=1, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.tv = ttk.Treeview(lf, columns=("photos", "alone", "group", "status"), style="People.Treeview",
                               selectmode="extended")
        heads = {"#0": ("Priority · name", 300), "photos": ("Photos", 80), "alone": ("Alone", 80),
                 "group": ("In group photos", 120), "status": ("", 200)}
        for c, (t, w) in heads.items():
            self.tv.heading(c, text=t, anchor="w")
            self.tv.column(c, width=int(w * s), anchor="w", stretch=c in ("#0", "status"))
        self.tv.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.tv.yview)
        ys.grid(row=0, column=1, sticky="ns")
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.tag_configure("off", foreground=p["muted"])
        self.tv.tag_configure("known", foreground=p["ok"])
        self.tv.bind("<Double-1>", lambda e: self._rename())
        self.tv.bind("<Return>", lambda e: self._rename())
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        side = ttk.Frame(b)
        side.grid(row=1, column=1, sticky="ns", padx=(10, 0))
        self.side_btns = []
        for text, cmd in (("Higher priority", lambda: self._move(-1)),
                          ("Lower priority", lambda: self._move(1)),
                          ("Move to top", lambda: self._move(-10 ** 6)),
                          ("Rename…", self._rename),
                          ("See their faces…", self._gallery),
                          ("Same person: merge", self._merge),
                          ("Don't sort / sort", self._toggle_include)):
            bt = ttk.Button(side, text=text, command=cmd)
            bt.pack(fill="x", pady=(0, 4))
            self.side_btns.append(bt)
        self.show_single = tk.BooleanVar(value=False)
        self.cb_single = ttk.Checkbutton(b, text="Show people seen in only one photo",
                                         variable=self.show_single, command=self._refresh)
        self.cb_single.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # step 3: folders
        c = ttk.LabelFrame(main, text=" 3 · Make a folder for each person ", padding=8)
        c.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        c.columnconfigure(1, weight=1)
        ttk.Label(c, text="Group photos:").grid(row=0, column=0, sticky="w")
        self.rule = tk.StringVar(value="priority")
        r = ttk.Frame(c)
        r.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(r, text="go to the person highest in the list", value="priority",
                        variable=self.rule, command=self._refresh_summary).pack(side="left")
        self.rb_all = ttk.Radiobutton(r, text="go into every listed person's folder (copies)", value="all",
                                      variable=self.rule, command=self._refresh_summary)
        self.rb_all.pack(side="left", padx=(14, 0))
        ttk.Label(c, text="Nobody from the list:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        r2 = ttk.Frame(c)
        r2.grid(row=1, column=1, columnspan=3, sticky="w", pady=(4, 0))
        self.others_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="put those photos in a folder called", variable=self.others_on,
                        command=self._refresh_summary).pack(side="left")
        self.others_name = tk.StringVar(value="Others")
        e = ttk.Entry(r2, textvariable=self.others_name, width=14)
        e.pack(side="left", padx=(6, 0))
        e.bind("<KeyRelease>", lambda ev: self._refresh_summary())
        ttk.Label(r2, text="(includes photos with no face)", style="Muted.TLabel").pack(side="left", padx=(6, 0))

        ttk.Label(c, text="Put the folders in:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.dest = tk.StringVar(value=os.path.join(self.app.dest_var.get().strip() or
                                                    str(core.Path.home() / APP_NAME), "People"))
        ttk.Entry(c, textvariable=self.dest).grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(c, text="Browse…", command=self._browse).grid(row=2, column=2, pady=(6, 0))
        r3 = ttk.Frame(c)
        r3.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.mode = tk.StringVar(value="copy")
        ttk.Radiobutton(r3, text="Copy", value="copy", variable=self.mode,
                        command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(r3, text="Move (cut & paste)", value="move", variable=self.mode,
                        command=self._mode_changed).pack(side="left", padx=(10, 16))
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="Remember names and order next time", variable=self.remember).pack(side="left")
        ttk.Button(r3, text="Forget people & saved results", command=self._forget).pack(side="left", padx=(8, 0))
        self.btn_go = ttk.Button(r3, text="Sort photos", style="Accent.TButton", command=self._go)
        self.btn_go.pack(side="right")
        self.summary = ttk.Label(c, text="", style="Muted.TLabel", wraplength=int(940 * s), justify="left")
        self.summary.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._update_buttons()

    # ------------------------------------------------------------------ setup (OpenCV + models)
    def _check_setup(self) -> bool:
        ok, info = faces.opencv_status()
        if not ok:
            self.setup_needed = "opencv"
            self.msg.configure(text=f"Face sorting needs the free OpenCV add-on ({info.split(' (')[0].lower()}). "
                                    "Press Set up to install it (about 40 MB, one time).",
                               style="Warn.TLabel")
        elif faces.missing_models():
            self.setup_needed = "models"
            self.msg.configure(text=f"Face sorting needs two face models, about {faces.MODELS_MB} MB, "
                                    "downloaded once. Your photos never leave this computer. "
                                    "Press Set up to download them.", style="Warn.TLabel")
        else:
            self.setup_needed = ""
            self.msg.configure(text="Ready. Press Find faces. Everything happens on this computer.",
                               style="Muted.TLabel")
        if self.setup_needed:
            self.btn_setup.pack(side="left", before=self.btn_find, padx=(0, 6))
        else:
            self.btn_setup.pack_forget()
        self._update_buttons()
        return not self.setup_needed

    def _do_setup(self):
        if self.job:
            return
        what = self.setup_needed

        def work():
            try:
                if what == "opencv":
                    self.job_msg = "Installing the OpenCV add-on… (this can take a few minutes)"
                    if not faces.install_opencv(log=self.app._log):
                        raise OSError("the install did not work (see Log). Check the internet "
                                      "connection, or run:  pip install opencv-python-headless")
                if faces.missing_models():
                    self.job_msg = "Downloading the face models…"
                    faces.download_models(progress=lambda d, t: setattr(self, "job_progress", (d, t)))
                self.job_msg = ""
            except Exception as e:  # noqa: BLE001
                self.job_msg = f"Setup failed: {e}"
        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()
        self._update_buttons()

    # ------------------------------------------------------------------ find faces
    def _find(self):
        if not self._check_setup() or self.scan:
            return
        self.people = []
        self._refresh()
        self.scan = faces.FaceScan([it.path for it in self.photos], workers=faces.default_workers())
        self.scan.start()
        self._update_buttons()

    def _pause(self):
        if self.scan and not self.scan.done:
            self.scan.resume() if self.scan.paused else self.scan.pause()
            self._update_buttons()

    def _stop(self):
        if self.scan and not self.scan.done:
            self.scan.stop()
        if self.grouping is not None:
            self.group_stop.set()

    def _tick(self):
        if not self.winfo_exists():
            return
        sc = self.scan
        if sc is not None:
            n = max(1, len(sc.paths))
            self.bar["value"] = sc.done_count * 1000 // n
            if not sc.done:
                el = time.time() - sc.started
                fresh = sc.done_count - sc.from_cache
                todo = n - sc.done_count
                if fresh >= 3:
                    left = el * todo / fresh
                    eta = f" · about {int(left // 3600)}h {int(left % 3600 // 60):02d}m left" if left >= 3600 \
                        else f" · about {int(left // 60)}m {int(left % 60):02d}s left"
                else:
                    eta = ""
                saved = f" ({sc.from_cache:,} already done before)" if sc.from_cache else ""
                if sc.paused:
                    state = "Paused"
                elif sc.done_count == 0 or sc.stage != "Looking for faces":
                    state = sc.stage.rstrip("…")
                else:
                    state = f"Looking for faces on {sc.workers} core{'s' if sc.workers > 1 else ''}"
                self.msg.configure(text=f"{state}… {sc.done_count:,} of {n:,} photos{saved} · "
                                        f"{len(sc.faces):,} faces{eta}   {os.path.basename(sc.current)}",
                                   style="Muted.TLabel")
            else:
                self._scan_done()
        if self.grouping is not None:
            d, t, text = self.group_progress
            self.bar["value"] = d * 1000 // max(1, t)
            if self.grouping.is_alive():
                self.msg.configure(text=f"{text}… {d:,} of {t:,} faces. This takes a moment for big "
                                        "collections; the window stays usable.", style="Muted.TLabel")
            else:
                self.grouping = None
                self._grouping_done()
        if self.job is not None:
            d, t = self.job_progress
            if t:
                self.bar["value"] = d * 1000 // t
            if self.job.is_alive():
                self.msg.configure(text=self.job_msg + (f"  {human_size(d)} of {human_size(t)}" if t else ""),
                                   style="Muted.TLabel")
            else:
                self.job = None
                failed = self.job_msg
                self.bar["value"] = 0
                if self._check_setup() and not failed:
                    self.msg.configure(text="All set. Press Find faces.", style="Ok.TLabel")
                elif failed:
                    self.msg.configure(text=failed, style="Err.TLabel")
                self._update_buttons()
        self.after(150, self._tick)

    def _scan_done(self):
        sc, self.scan = self.scan, None
        if sc.fatal:
            self.msg.configure(text=sc.fatal, style="Err.TLabel")
            self._update_buttons()
            return
        self.no_face, self.errors = sc.no_face, sc.errors
        self.done_paths = list(sc.done_paths)
        self._last_scan = sc
        self.group_stop.clear()
        self.group_progress = (0, max(1, len(sc.faces)), "Grouping faces into people")
        found = sc.faces
        known = faces.load_known()

        def work():
            try:
                self._grouped = faces.group_faces(
                    found, known, stop=self.group_stop,
                    progress=lambda d, t, text: setattr(self, "group_progress", (d, t, text)))
                self._group_err = ""
            except Exception as e:  # noqa: BLE001
                self._grouped, self._group_err = [], str(e)
        self.grouping = threading.Thread(target=work, daemon=True)
        self.grouping.start()
        self._update_buttons()

    def _grouping_done(self):
        sc = self._last_scan
        if self._group_err:
            self.msg.configure(text=f"Grouping the faces failed: {self._group_err}", style="Err.TLabel")
            self.app._log(f"faces: grouping failed: {self._group_err}")
            self._update_buttons()
            return
        if self.group_stop.is_set():
            self.msg.configure(text="Stopped.", style="Muted.TLabel")
            self._update_buttons()
            return
        self.people = self._grouped
        n_people = sum(1 for p in self.people if len(p.photos) >= 2)
        singles = len(self.people) - n_people
        known = sum(1 for p in self.people if p.known)
        took = int(time.time() - sc.started)
        txt = (f"Done in {took // 60}m {took % 60:02d}s: {len(sc.faces):,} faces in "
               f"{sc.done_count - len(sc.no_face) - len(sc.errors):,} photos, {n_people:,} people seen in "
               f"2 or more photos")
        if singles:
            txt += f", {singles:,} seen only once (hidden)"
        if known:
            txt += f". {known} recognised from last time"
        txt += f". {len(sc.no_face):,} photos have no face."
        if sc.errors:
            txt += f" {len(sc.errors):,} couldn't be read (see Log)."
            for pth, e in sc.errors.items():
                self.app._log(f"  faces: couldn't read {pth} ({e})")
        if sc.from_cache:
            txt += f" ({sc.from_cache:,} photos were already analysed before, so they were instant.)"
        if sc.stop_ev.is_set():
            txt = ("Stopped. Press Find faces again later to carry on where it stopped. " + txt)
        self.msg.configure(text=txt, style="Ok.TLabel")
        self.app._log(txt)
        self._refresh()

    # ------------------------------------------------------------------ people list
    def _min_photos(self):
        return 1 if self.show_single.get() else 2

    def _visible(self):
        m = self._min_photos()
        return [p for p in self.people if len(p.photos) >= m]

    def _image(self, thumb: bytes, master=None) -> tk.PhotoImage:
        data = faces.thumb_png(thumb)
        if not data:
            return tk.PhotoImage(master=master or self, width=faces.THUMB, height=faces.THUMB)
        img = tk.PhotoImage(master=master or self, data=base64.b64encode(data).decode("ascii"))
        if self.s >= 1.75:
            img = img.zoom(2)
        return img

    def _thumb(self, person: faces.Person) -> tk.PhotoImage:
        f = max((f for f in person.faces if not f.excluded), key=lambda f: f.quality, default=person.faces[0])
        return self._image(f.thumb)

    def _refresh(self, keep_sel=None):
        sel = keep_sel if keep_sel is not None else [self.people[int(i)] for i in self.tv.selection()
                                                       if int(i) < len(self.people)]
        self.tv.delete(*self.tv.get_children())
        self.thumbs = {}
        rank = 0
        drawn = 0
        more = 0
        keep = {id(x) for x in sel}
        for n, p in enumerate(self.people):
            photos = p.photos
            if len(photos) < self._min_photos():
                continue
            if p.include:
                rank += 1
            if drawn >= MAX_ROWS and id(p) not in keep:
                more += 1
                continue
            drawn += 1
            alone = sum(1 for ph in photos if self._faces_in(ph) == 1)
            img = self.thumbs[n] = self._thumb(p)
            status = "not sorted" if not p.include else ("recognised from last time" if p.known else "")
            self.tv.insert("", "end", iid=str(n), image=img,
                           text=f"  {rank}.  {p.name}" if p.include else f"  –   {p.name}",
                           values=(len(photos), alone, len(photos) - alone, status),
                           tags=("off",) if not p.include else (("known",) if p.known else ()))
        ids = [str(self.people.index(p)) for p in sel if p in self.people]
        ids = [i for i in ids if self.tv.exists(i)]
        if ids:
            self.tv.selection_set(ids)
            self.tv.see(ids[0])
        hidden = sum(1 for p in self.people if len(p.photos) < 2)
        self.cb_single.configure(text=f"Show people seen in only one photo ({hidden:,})"
                                 + (f"     ·  {more:,} more people with fewer photos are not listed "
                                    "(they are still sorted)" if more else ""))
        self._refresh_summary()
        self._update_buttons()

    def _faces_in(self, photo):
        if not hasattr(self, "_count_cache") or self._count_cache[0] != id(self.people):
            cnt = {}
            for p in self.people:
                for f in p.faces:
                    if not f.excluded:
                        cnt[f.photo] = cnt.get(f.photo, 0) + 1
            self._count_cache = (id(self.people), cnt)
        return self._count_cache[1].get(photo, 0)

    def _selected(self):
        return [self.people[int(i)] for i in self.tv.selection()]

    def _move(self, d):
        sel = self._selected()
        if len(sel) != 1:
            return
        p = sel[0]
        vis = self._visible()
        i = vis.index(p)
        j = max(0, min(len(vis) - 1, i + d))
        if i == j:
            return
        other = vis[j]
        self.people.remove(p)
        k = self.people.index(other)
        self.people.insert(k if d < 0 else k + 1, p)
        self._renumber()
        self._refresh(keep_sel=[p])

    def _renumber(self):
        for n, p in enumerate(self.people):
            p.pid = n
            for f in p.faces:
                f.person = n
        self._count_cache = (None, {})

    def _rename(self):
        sel = self._selected()
        if len(sel) != 1:
            return
        p = sel[0]
        new = simpledialog.askstring(APP_NAME, "Name for this person (also the folder name):",
                                     initialvalue=p.name, parent=self)
        if new and new.strip():
            p.name = new.strip()
            self._refresh(keep_sel=[p])

    def _merge(self):
        sel = self._selected()
        if len(sel) < 2:
            messagebox.showinfo(APP_NAME, "Select two or more rows of the same person (Ctrl+click), "
                                          "then press this.", parent=self)
            return
        keep = sel[0]
        if not messagebox.askyesno(APP_NAME, f"Treat these {len(sel)} as one person, named "
                                             f"\"{keep.name}\"?", parent=self):
            return
        faces.merge_people(self.people, keep, sel[1:])
        self._count_cache = (None, {})
        self._refresh(keep_sel=[keep])

    def _toggle_include(self):
        sel = self._selected()
        if not sel:
            return
        on = not all(p.include for p in sel)
        for p in sel:
            p.include = on
        self._refresh(keep_sel=sel)

    def _gallery(self):
        sel = self._selected()
        if len(sel) != 1:
            return
        p = sel[0]
        win = tk.Toplevel(self)
        win.title(f"{p.name}: faces")
        win.configure(bg=self.pal["bg"])
        win.transient(self)
        top = ttk.Frame(win, padding=(12, 10, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text=f"Untick any face that isn't {p.name}. That photo then won't go into "
                            f"{p.name}'s folder.", wraplength=int(640 * self.s)).pack(anchor="w")
        cv = tk.Canvas(win, bg=self.pal["bg"], highlightthickness=0, width=int(680 * self.s),
                       height=int(460 * self.s))
        ys = ttk.Scrollbar(win, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=ys.set)
        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        cv.pack(fill="both", expand=True, padx=(12, 0), pady=8)
        inner = ttk.Frame(cv)
        cv.create_window(0, 0, window=inner, anchor="nw")
        imgs, vars_ = [], []
        cols = 6
        shown = sorted(p.faces, key=lambda f: -f.quality)[:240]
        for k, f in enumerate(shown):
            cell = ttk.Frame(inner, padding=4)
            cell.grid(row=k // cols, column=k % cols)
            im = self._image(f.thumb, master=win)
            imgs.append(im)
            lbl = ttk.Label(cell, image=im)
            lbl.pack()
            v = tk.BooleanVar(value=not f.excluded)
            vars_.append((v, f))
            ttk.Checkbutton(cell, text=mid_name(os.path.basename(f.photo), 14), variable=v).pack()
            lbl.bind("<Double-1>", lambda e, path=f.photo: _open(path))
        if len(p.faces) > len(shown):
            ttk.Label(inner, text=f"(+{len(p.faces) - len(shown):,} more faces not shown)",
                      style="Muted.TLabel").grid(row=999, column=0, columnspan=cols, pady=6)
        inner.update_idletasks()
        cv.configure(scrollregion=cv.bbox("all"))
        cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units"))
        win._imgs = imgs  # keep alive

        def done():
            for v, f in vars_:
                f.excluded = not v.get()
            p.update_centroid()
            self._count_cache = (None, {})
            cv.unbind_all("<MouseWheel>")
            win.destroy()
            self._refresh(keep_sel=[p])
        ttk.Button(bar, text="Done", style="Accent.TButton", command=done).pack(side="right")
        ttk.Label(bar, text="Double-click a face to open its photo.", style="Muted.TLabel").pack(side="left")
        win.protocol("WM_DELETE_WINDOW", done)

    # ------------------------------------------------------------------ plan + sort
    def _plan(self):
        others = self.others_name.get().strip() or "Others"
        photos = [ph for ph in getattr(self, "done_paths", []) if ph not in self.errors]
        return faces.plan_folders(self.people, photos, self.no_face, group_rule=self.rule.get(),
                                  others=others if self.others_on.get() else None,
                                  min_photos=self._min_photos())

    def _refresh_summary(self):
        if not self.people:
            self.summary.configure(text="")
            self._update_buttons()
            return
        plan = self._plan()
        per: dict[str, int] = {}
        for folders in plan.values():
            for f in folders:
                per[f] = per.get(f, 0) + 1
        copies = sum(len(v) for v in plan.values())
        rank = {name: i for i, name in enumerate(faces.folder_names(self.people))}
        parts = [f"{k}: {v:,}" for k, v in sorted(per.items(), key=lambda kv: rank.get(kv[0], 10 ** 9))]
        txt = " · ".join(parts[:12]) + (" · …" if len(parts) > 12 else "")
        left_out = len(getattr(self, "done_paths", [])) - len(plan)
        txt += f"\n{len(plan):,} photos into {len(per):,} folders"
        if copies != len(plan):
            txt += f" ({copies:,} copies, because group photos go to everyone in them)"
        if left_out:
            txt += f". {left_out:,} photos left where they are."
        self.summary.configure(text=txt)
        verb = "Move" if self.mode.get() == "move" else "Copy"
        self.btn_go.configure(text=f"{verb} {len(plan):,} photos into folders" if plan else "Sort photos")
        self._update_buttons()

    def _mode_changed(self):
        if self.mode.get() == "move" and self.rule.get() == "all":
            self.rule.set("priority")
        self.rb_all.state(["disabled"] if self.mode.get() == "move" else ["!disabled"])
        self._refresh_summary()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.dest.get(), title="Where to make the folders")
        if d:
            self.dest.set(os.path.abspath(d))

    def _forget(self):
        if messagebox.askyesno(APP_NAME, "Forget every remembered person (names and face data), and the "
                                         "saved face-search results?\n\nThe next Find faces will look "
                                         "at every photo again.", parent=self):
            faces.forget_known()
            faces.clear_cache()
            for p in self.people:
                p.known = False
            self._refresh()

    def _go(self):
        plan = self._plan()
        if not plan or self.app.busy:
            if self.app.busy:
                messagebox.showinfo(APP_NAME, "Wait for the current job in the main window to finish.", parent=self)
            return
        dest = os.path.abspath(os.path.expanduser(self.dest.get().strip()))
        mode = self.mode.get()
        items, subs = [], []
        for ph, folders in plan.items():
            it = self.by_path.get(ph)
            if it is None:
                continue
            for f in folders:
                items.append(it)
                subs.append(f)
        try:
            os.makedirs(dest, exist_ok=True)
            need = core.bytes_to_write(items, dest, mode)
            free = core.shutil.disk_usage(dest).free
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't use that folder:\n{dest}\n\n{e.strerror or e}", parent=self)
            return
        if need > free - 50 * 1024 * 1024:
            messagebox.showerror(APP_NAME, f"Not enough space there.\n\nNeeded: {human_size(need)}\n"
                                           f"Free: {human_size(free)}", parent=self)
            return
        verb = "Move" if mode == "move" else "Copy"
        n_folders = len(set(subs))
        q = f"{verb} {len(plan):,} photos into {n_folders:,} folders inside\n{dest}?"
        if mode == "move":
            q += "\n\nThey are removed from where they are now. \"Undo last\" can put them back."
        if not messagebox.askokcancel(APP_NAME, q, parent=self):
            return
        if self.remember.get():
            try:
                faces.save_known(self.people)
            except OSError as e:
                self.app._log(f"Couldn't remember people: {e}")
        self.app._launch_transfer(items, dest, mode, layout="flat", conflict="rename",
                                  skip_identical=True, verify=self.app.verify_var.get(), subfolders=subs)
        self._close(force=True)

    # ------------------------------------------------------------------ state
    def _update_buttons(self):
        def st(w, on):
            w.state(["!disabled"] if on else ["disabled"])
        scanning = self.scan is not None and not self.scan.done
        busy = scanning or self.job is not None or self.grouping is not None
        st(self.btn_find, not busy and not getattr(self, "setup_needed", "") and bool(self.photos))
        st(self.btn_setup, not busy)
        st(self.btn_pause, scanning)
        st(self.btn_stop, scanning or self.grouping is not None)
        self.btn_pause.configure(text="Resume" if scanning and self.scan.paused else "Pause")
        sel = self.tv.selection() if hasattr(self, "tv") else ()
        for bt in self.side_btns:
            st(bt, bool(sel) and not busy)
        st(self.btn_go, bool(self.people) and not busy and not self.app.busy)

    def _close(self, force=False):
        if not force and self.scan and not self.scan.done:
            if not messagebox.askyesno(APP_NAME, "Stop looking for faces and close?", parent=self):
                return
            self.scan.stop()
        self.group_stop.set()
        if not force and self.people and self.remember.get() and \
                any(not p.name.startswith("Person ") for p in self.people):
            try:
                faces.save_known(self.people)
            except OSError:
                pass
        self.destroy()


def mid_name(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 6] + "…" + s[-5:]


def _open(path):
    import subprocess
    import sys
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except Exception:  # noqa: BLE001
        pass
