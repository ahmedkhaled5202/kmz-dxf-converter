# -*- coding: utf-8 -*-
"""
GUI wrapper for kmz_dxf_converter_v2.py
Place BOTH files in the same folder:
  - kmz_dxf_converter_v2.py  (core logic)
  - kmz_dxf_gui.py           (this file)
"""
import contextlib
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ------------------------------------------------------------------ queue writer
class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s) if s else 0

    def flush(self):
        pass


# ------------------------------------------------------------------ worker thread
def run_conversion(paths, epsg, fill, explode, labels, q):
    try:
        import importlib
        core = importlib.import_module("kmz_dxf_converter_v2")
        # Disable interactive questions (GUI handles them)
        core.INTERACTIVE = False

        opts = core.Opts(
            epsg=epsg,
            forced=(epsg is not None and epsg != 4326) or (epsg == 4326),
            fill=fill,
            explode=explode,
            labels=labels,
        )
        # If 4326 (lon/lat) -> epsg None
        if epsg == 4326:
            opts.epsg = None
            opts.forced = True

        with contextlib.redirect_stdout(QueueWriter(q)), \
             contextlib.redirect_stderr(QueueWriter(q)):
            ok_count = 0
            for p in paths:
                if core.process(p, opts):
                    ok_count += 1
            q.put(("done", ok_count))
    except SystemExit as e:
        q.put(("error", str(e)))
    except Exception as e:
        q.put(("error", f"{type(e).__name__}: {e}"))


# ------------------------------------------------------------------ main app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KMZ ↔ DXF/DWG Converter")
        self.geometry("650x650")
        self.minsize(600, 600)
        self.q = queue.Queue()
        self.busy = False
        self.paths = []
        pad = {"padx": 10, "pady": 5}

        # 1. files
        f1 = ttk.LabelFrame(self, text="1. Files (KMZ, KML, DXF, DWG)")
        f1.pack(fill="x", **pad)
        self.listbox = tk.Listbox(f1, height=5)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=8)
        sb = ttk.Scrollbar(f1, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y", pady=8)
        btn_frame = ttk.Frame(f1)
        btn_frame.pack(side="right", fill="y", padx=6, pady=8)
        ttk.Button(btn_frame, text="Add files...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="Remove selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="Clear all", command=self.clear_all).pack(fill="x", pady=2)

        # 2. coordinate system
        f2 = ttk.LabelFrame(self, text="2. Coordinate system")
        f2.pack(fill="x", **pad)
        self.epsg_var = tk.StringVar(value="auto")
        ttk.Radiobutton(f2, text="Auto (best effort)", variable=self.epsg_var,
                        value="auto").grid(row=0, column=0, sticky="w", padx=8, pady=3)
        ttk.Radiobutton(f2, text="Force EPSG code:", variable=self.epsg_var,
                        value="epsg").grid(row=1, column=0, sticky="w", padx=8, pady=3)
        self.epsg_entry = ttk.Entry(f2, width=12)
        self.epsg_entry.insert(0, "32636")
        self.epsg_entry.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Radiobutton(f2, text="Force lon/lat (WGS84)", variable=self.epsg_var,
                        value="lonlat").grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=3)

        # 3. options
        f3 = ttk.LabelFrame(self, text="3. Options")
        f3.pack(fill="x", **pad)
        self.fill_var = tk.BooleanVar(value=True)
        self.explode_var = tk.BooleanVar(value=False)
        self.labels_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f3, text="Fill polygons in KMZ", variable=self.fill_var).grid(row=0, column=0, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f3, text="Explode blocks (DXF→KMZ)", variable=self.explode_var).grid(row=1, column=0, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(f3, text="Add text labels (KMZ→DXF)", variable=self.labels_var).grid(row=2, column=0, sticky="w", padx=8, pady=2)

        # run button
        self.btn = ttk.Button(self, text="Convert", command=self.start)
        self.btn.pack(fill="x", padx=10, pady=(8, 4), ipady=6)
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.pack(fill="x", padx=10, pady=2)

        # log
        f4 = ttk.LabelFrame(self, text="Log")
        f4.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(f4, height=14, wrap="word", state="normal")
        lsb = ttk.Scrollbar(f4, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)

        self.after(100, self.poll)

    # ---- file ops
    def add_files(self):
        files = filedialog.askopenfilenames(
            title="Choose files",
            filetypes=[("CAD / KML", "*.dxf *.dwg *.kmz *.kml"), ("All files", "*.*")])
        for f in files:
            if f not in self.paths:
                self.paths.append(f)
                self.listbox.insert("end", f)

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.paths[i]

    def clear_all(self):
        self.listbox.delete(0, "end")
        self.paths.clear()

    # ---- run
    def start(self):
        if self.busy:
            return
        if not self.paths:
            messagebox.showwarning("No files", "Please add at least one file.")
            return
        epsg = None
        mode = self.epsg_var.get()
        if mode == "epsg":
            try:
                epsg = int(self.epsg_entry.get().strip())
            except ValueError:
                messagebox.showerror("Invalid EPSG", "Please enter a numeric EPSG code.")
                return
        elif mode == "lonlat":
            epsg = 4326
        # auto -> epsg stays None, forced stays False -> interactive disabled => uses guess

        self.busy = True
        self.btn.state(["disabled"])
        self.bar.start(12)
        self.log.delete("1.0", "end")
        args = (list(self.paths), epsg, self.fill_var.get(),
                self.explode_var.get(), self.labels_var.get(), self.q)
        threading.Thread(target=run_conversion, args=args, daemon=True).start()

    def finish(self):
        self.busy = False
        self.bar.stop()
        self.btn.state(["!disabled"])

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", val)
                    self.log.see("end")
                elif kind == "done":
                    self.finish()
                    messagebox.showinfo("Done", f"Finished converting {val} file(s).\nCheck the log for outputs.")
                elif kind == "error":
                    self.finish()
                    self.log.insert("end", f"\nERROR: {val}\n")
                    self.log.see("end")
                    messagebox.showerror("Error", val)
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()