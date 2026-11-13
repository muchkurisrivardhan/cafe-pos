"""CafePOS.exe control window: runs the POS + SQLite database, keeps Tailscale connected,
publishes the public link with Tailscale Funnel, and shows a live log.
Dev run: python gui.py      Exe build: see .github/workflows/build-exe.yml"""
import json, logging, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request, webbrowser
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, ttk

import app

PORT = app.PORT
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't flash a console for every tailscale call
TS_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale"
log = logging.getLogger("pos")
COLORS = {"green": "#17794f", "amber": "#c98a00", "red": "#b3122b", "grey": "#9a958c"}


# ---------------- Tailscale helpers ----------------
def ts_exe():
    p = TS_DIR / "tailscale.exe"
    return str(p) if p.exists() else shutil.which("tailscale")


def ts(*args, timeout=30):
    r = subprocess.run([ts_exe(), *args], capture_output=True, text=True, timeout=timeout,
                       creationflags=NOWIN, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def running(image):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture_output=True, text=True,
                         creationflags=NOWIN).stdout
    return image.lower() in out.lower()


class Window:
    def __init__(self, root):
        self.root, self.q, self.server = root, queue.Queue(), None
        self.closing, self.opened_urls, self.helper = False, set(), None  # helper: running tailscale login/funnel
        self.dns_fixed = False
        root.title("Guns & Roses Café POS")
        root.geometry("900x600")
        root.minsize(680, 440)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Guns & Roses Café POS", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(top, text="Keep this window open while the café is running.", foreground=COLORS["grey"]).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        top.columnconfigure(2, weight=1)

        self.rows = {}
        specs = [("pos", "POS server", [("Open POS", lambda: self.open_url(f"http://localhost:{PORT}"))]),
                 ("pub", "Public link", [("Open", lambda: self.open_url(self.rows["pub"]["var"].get())),
                                         ("Copy", lambda: self.copy("pub"))]),
                 ("lan", "Same Wi-Fi", [("Copy", lambda: self.copy("lan"))]),
                 ("ts", "Tailscale", [("Sign in", None)]),  # action button, shown only when Tailscale needs you
                 ("db", "Database", [("Open folder", lambda: os.startfile(app.DB.parent))])]
        for i, (key, title, buttons) in enumerate(specs, start=2):
            dot = tk.Label(top, text="●", fg=COLORS["grey"], font=("Segoe UI", 13))
            dot.grid(row=i, column=0, padx=(0, 6))
            ttk.Label(top, text=title, width=12).grid(row=i, column=1, sticky="w")
            var = tk.StringVar(value="…")
            ttk.Entry(top, textvariable=var, state="readonly").grid(row=i, column=2, sticky="ew", padx=6, pady=3)
            btns = []
            for j, (label, cmd) in enumerate(buttons):
                b = ttk.Button(top, text=label, command=cmd)
                b.grid(row=i, column=3 + j, padx=2)
                btns.append(b)
            self.rows[key] = {"dot": dot, "var": var, "btns": btns}
        self.rows["ts"]["btns"][0].grid_remove()

        ttk.Label(root, text="Live log", font=("Segoe UI", 10, "bold"), padding=(14, 6, 0, 2)).pack(anchor="w")
        box = ttk.Frame(root, padding=(14, 0, 14, 14))
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, font=("Consolas", 10), wrap="word", state="disabled", bg="#1c1a17", fg="#e8e4dc",
                            insertbackground="#e8e4dc", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("warn", foreground="#f2b84b")
        self.text.tag_configure("err", foreground="#ff6b6b")
        root.after(150, self.pump)

    # ---- thread-safe UI updates (tkinter may only be touched from the main thread) ----
    def set(self, key, color, text):
        self.q.put(("set", key, color, text))

    def need_action(self, label, url):
        """Show the Tailscale button (Sign in / Enable Funnel) and open the page once."""
        self.q.put(("action", label, url))
        if url not in self.opened_urls:
            self.opened_urls.add(url)
            webbrowser.open(url)

    def pump(self):
        while not self.q.empty():
            msg = self.q.get()
            if msg[0] == "log":
                _, level, line = msg
                self.text.configure(state="normal")
                self.text.insert("end", line + "\n", "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else "")
                if int(self.text.index("end-1c").split(".")[0]) > 3000:  # keep the window light; full log is on disk
                    self.text.delete("1.0", "500.0")
                self.text.configure(state="disabled")
                self.text.see("end")
            elif msg[0] == "set":
                _, key, color, text = msg
                self.rows[key]["dot"].configure(fg=COLORS[color])
                self.rows[key]["var"].set(text)
            elif msg[0] == "action":
                _, label, url = msg
                b = self.rows["ts"]["btns"][0]
                if url:
                    b.configure(text=label, command=lambda u=url: webbrowser.open(u))
                    b.grid()
                else:
                    b.grid_remove()
        self.root.after(150, self.pump)

    def open_url(self, url):
        if url.startswith("http"):
            webbrowser.open(url)

    def copy(self, key):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.rows[key]["var"].get())
        log.info("Copied: %s", self.rows[key]["var"].get())

    # ---------------- POS server ----------------
    def start_server(self):
        try:
            self.server = app.make_server(PORT)
        except OSError as e:
            self.set("pos", "red", f"Port {PORT} is busy - is the POS already open in another window?")
            log.error("Could not start the POS on port %s: %s", PORT, e)
            messagebox.showerror("POS already running?",
                                 f"Port {PORT} is already in use.\nClose the other POS window (or the old black window) and open this again.")
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.set("pos", "green", f"Running - http://localhost:{PORT}")
        self.set("db", "green", str(app.DB))
        ips = [ip for ip in app.lan_ips() if not ip.startswith("100.")]  # 100.x is Tailscale's own address
        self.set("lan", "green", "   ".join(f"http://{ip}:{PORT}" for ip in ips))
        log.info("POS started on port %s. Database: %s", PORT, app.DB)
        return True

    def pos_alive(self):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5).status == 200
        except OSError:
            return False

    # ---------------- Tailscale: install, connect, publish ----------------
    def run_helper(self, args, label):
        """Run a tailscale command that may wait for the user (login / funnel); stream its output to the log."""
        if self.helper and self.helper.poll() is None:
            return  # still waiting for the user on the previous one
        p = subprocess.Popen([ts_exe(), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             creationflags=NOWIN, encoding="utf-8", errors="replace")
        self.helper = p

        def read():
            for line in p.stdout:
                if line.strip():
                    log.info("Tailscale: %s", line.strip())
                    m = re.search(r"https://login\.tailscale\.com/\S+", line)
                    if m:
                        self.need_action(label, m.group(0))
        threading.Thread(target=read, daemon=True).start()

    def ensure_tailscale(self):
        if not ts_exe():
            self.set("ts", "amber", "Installing Tailscale (approve the Windows prompt)…")
            log.info("Tailscale not found - installing it with winget (one time, needs internet)")
            winget = shutil.which("winget")
            if not winget:
                self.set("ts", "red", "Install Tailscale from tailscale.com/download, then reopen this app")
                self.need_action("Download Tailscale", "https://tailscale.com/download/windows")
                return
            r = subprocess.run([winget, "install", "-e", "--id", "Tailscale.Tailscale", "--accept-package-agreements",
                                "--accept-source-agreements"], capture_output=True, text=True, creationflags=NOWIN,
                               encoding="utf-8", errors="replace", timeout=900)
            log.info("winget: %s", (r.stdout.strip().splitlines() or ["(no output)"])[-1])
            if not ts_exe():
                self.set("ts", "red", "Tailscale install failed - install it from tailscale.com/download")
                self.need_action("Download Tailscale", "https://tailscale.com/download/windows")
                return

        # On Windows the Tailscale engine only connects while its tray app is running.
        if not running("tailscale-ipn.exe") and (TS_DIR / "tailscale-ipn.exe").exists():
            log.info("Starting the Tailscale tray app")
            subprocess.Popen([str(TS_DIR / "tailscale-ipn.exe")], creationflags=NOWIN)
            time.sleep(5)

        code, out, err = ts("status", "--json")
        try:
            st = json.loads(out)
        except ValueError:
            self.set("ts", "amber", f"Starting… ({err or out or 'no answer yet'})"[:120])
            return
        state = st.get("BackendState")
        if state == "NeedsLogin":
            self.set("ts", "amber", "Sign in to Tailscale - a browser page has opened")
            if st.get("AuthURL"):
                self.need_action("Sign in", st["AuthURL"])
            else:
                self.run_helper(["login"], "Sign in")
            return
        if state == "Stopped":
            log.info("Tailscale was switched off - reconnecting")
            ts("up", timeout=60)
            return
        if state != "Running":
            self.set("ts", "amber", f"Starting… ({state})")
            return

        me = st.get("Self", {})
        self.set("ts", "green", f"Connected as {me.get('HostName', '?')} ({', '.join(me.get('TailscaleIPs', [])[:1])})")
        if not self.dns_fixed:
            # Tailscale DNS made websites stop loading on some PCs (seen with VPNs); the POS doesn't need it.
            ts("set", "--accept-dns=false")
            self.dns_fixed = True

        public = "https://" + me.get("DNSName", "").rstrip(".")
        _, cfg, _ = ts("funnel", "status", "--json")
        if f"127.0.0.1:{PORT}" in cfg and "AllowFunnel" in cfg:
            if self.rows["pub"]["var"].get() != public:
                log.info("Public link is live: %s (first visit can take ~30 s while the certificate is issued)", public)
            self.q.put(("action", "", None))
            self.set("pub", "green", public)
        else:
            self.set("pub", "amber", "Turning on the public link… (approve 'Enable Funnel' if a page opens)")
            self.run_helper(["funnel", "--bg", str(PORT)], "Enable Funnel")

    def watchdog(self):
        """Every 20 s: check the POS answers and Tailscale + public link are up; repair what isn't."""
        while not self.closing:
            if self.server and not self.pos_alive():
                self.set("pos", "red", "Not responding - restart this app")
                log.error("POS server is not responding")
            try:
                self.ensure_tailscale()
            except Exception as e:  # keep the POS running even if Tailscale misbehaves
                log.exception("Tailscale check failed: %s", e)
                self.set("ts", "red", f"Problem: {e}"[:120])
            for _ in range(20):
                if self.closing:
                    return
                time.sleep(1)

    def on_close(self):
        if not messagebox.askyesno("Close the POS?", "Closing stops the POS on every tablet and turns off the public link.\n\nClose now?"):
            return
        self.closing = True
        log.info("Shutting down…")
        if ts_exe():
            try:  # don't leave a public link pointing at a stopped POS
                ts("funnel", "--https=443", "off", timeout=15)
            except Exception:
                pass
        if self.server:
            self.server.shutdown()
        self.root.destroy()


class UIHandler(logging.Handler):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def emit(self, record):
        self.win.q.put(("log", record.levelno, self.format(record)))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    win = Window(root)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    ui = UIHandler(win)
    ui.setFormatter(fmt)
    logs = app.DB.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    disk = logging.FileHandler(logs / f"pos-{date.today()}.log", encoding="utf-8")
    disk.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(ui)
    log.addHandler(disk)
    if win.start_server():
        threading.Thread(target=win.watchdog, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
