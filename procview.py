"""Proc View - an advanced live process viewer for Windows.

Run:  pythonw procview.py   (asks for admin rights so it can see and manage every process)
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psutil

__version__ = "1.0.0"
DONATE_URL = "https://buymeacoffee.com/kuzucevirme"

FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller .exe
# config.json lives next to the .exe / script; bundled files (web/) come from the package
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", BASE)
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8792,
    "open_browser_on_start": True,
    "sample_interval_seconds": 1.5,
    "apply_rules_interval_seconds": 3,
    "auto_elevate": True,
    "priority_rules": {},   # {"game.exe": {"priority": "high", "io": "high", "affinity": [0,1]}}
}

PRIORITIES = {
    "idle": psutil.IDLE_PRIORITY_CLASS,
    "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "normal": psutil.NORMAL_PRIORITY_CLASS,
    "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
    "high": psutil.HIGH_PRIORITY_CLASS,
    "realtime": psutil.REALTIME_PRIORITY_CLASS,
}
PRIO_NAME = {int(v): k for k, v in PRIORITIES.items()}
IO_PRIORITIES = {
    "very_low": psutil.IOPRIO_VERYLOW,
    "low": psutil.IOPRIO_LOW,
    "normal": psutil.IOPRIO_NORMAL,
    "high": psutil.IOPRIO_HIGH,
}
IO_NAME = {int(v): k for k, v in IO_PRIORITIES.items()}

_cfg_lock = threading.Lock()


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin():
    exe = sys.executable
    if FROZEN:
        argv = sys.argv[1:]
    else:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
        argv = [os.path.abspath(__file__)] + sys.argv[1:]
    args = " ".join(f'"{a}"' for a in argv)
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, BASE, 0) > 32


# ---------------------------------------------------------------- suspend / resume
_ntdll = ctypes.windll.ntdll
_k32 = ctypes.windll.kernel32
PROCESS_SUSPEND_RESUME = 0x0800


def _nt_call(pid, fn):
    h = _k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        raise psutil.AccessDenied(pid)
    try:
        st = fn(h)
        if st != 0:
            raise OSError(f"NTSTATUS 0x{st & 0xFFFFFFFF:08X}")
    finally:
        _k32.CloseHandle(h)


def suspend(pid):
    _nt_call(pid, _ntdll.NtSuspendProcess)


def resume(pid):
    _nt_call(pid, _ntdll.NtResumeProcess)


# ---------------------------------------------------------------- registry (IFEO PerfOptions)
# Windows itself applies these at process start, even when Proc View is not running.
IFEO = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
REG_CPU = {"idle": 1, "normal": 2, "high": 3, "below_normal": 5, "above_normal": 6}
REG_IO = {"very_low": 0, "low": 1, "normal": 2}


def _ifeo_name(name):
    if not name or any(c in name for c in '\\/:*?"<>|'):
        raise ValueError(f"invalid program name for registry: {name!r}")
    return name


def registry_write(name, rule):
    import winreg
    name = _ifeo_name(name)
    prio, io = rule.get("priority"), rule.get("io")
    if prio and prio not in REG_CPU:
        raise ValueError("Windows registry can't store Realtime priority - pick another or untick registry")
    if io and io not in REG_IO:
        raise ValueError("Windows registry can't store High I/O priority - pick another or untick registry")
    if not prio and not io:
        raise ValueError("registry needs a CPU or I/O priority (affinity can't be stored there)")
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, IFEO + "\\" + name + r"\PerfOptions", 0,
                            winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
        for vname, table, val in (("CpuPriorityClass", REG_CPU, prio), ("IoPriority", REG_IO, io)):
            if val:
                winreg.SetValueEx(k, vname, 0, winreg.REG_DWORD, table[val])
            else:
                try:
                    winreg.DeleteValue(k, vname)
                except FileNotFoundError:
                    pass
    return ", ".join(x for x in (prio and f"CPU {prio}", io and f"I/O {io}") if x)


def registry_remove(name):
    """Remove only what we wrote; keep the IFEO key if anything else lives there."""
    import winreg
    name = _ifeo_name(name)
    acc = winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
    base = IFEO + "\\" + name
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + r"\PerfOptions", 0, acc) as k:
            for v in ("CpuPriorityClass", "IoPriority"):
                try:
                    winreg.DeleteValue(k, v)
                except FileNotFoundError:
                    pass
            empty = winreg.QueryInfoKey(k)[:2] == (0, 0)
        if empty:
            winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, base + r"\PerfOptions", winreg.KEY_WOW64_64KEY)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, acc) as k:
            empty = winreg.QueryInfoKey(k)[:2] == (0, 0)
        if empty:
            winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, base, winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- sampler
class Sampler:
    def __init__(self, app):
        self.app = app
        self.cache = {}          # pid -> dict(proc, static, last_io, last_t)
        self.snapshot = {"procs": [], "system": {}, "t": 0}
        self.suspended = set()
        self.ncpu = psutil.cpu_count() or 1
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.last_net = None

    def _static(self, p):
        s = {}
        for k, fn in (("name", p.name), ("exe", p.exe), ("user", p.username),
                      ("create", p.create_time), ("cmdline", p.cmdline)):
            try:
                v = fn()
                if k == "cmdline":
                    v = subprocess.list2cmdline(v) if v else ""
                s[k] = v
            except (psutil.Error, OSError, SystemError):
                s[k] = None
        if s["user"] and "\\" in s["user"]:
            s["user"] = s["user"].split("\\", 1)[1]
        return s

    def sample(self):
        now = time.time()
        seen = set()
        out = []
        for p in psutil.process_iter():
            pid = p.pid
            seen.add(pid)
            c = self.cache.get(pid)
            if c is None:
                c = {"proc": p, "static": self._static(p), "io": None, "t": now}
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
                self.cache[pid] = c
            proc = c["proc"]
            st = c["static"]
            row = {"pid": pid, "name": st["name"] or f"pid {pid}", "exe": st["exe"],
                   "user": st["user"], "create": st["create"], "cmdline": st["cmdline"]}
            try:
                with proc.oneshot():
                    row["cpu"] = round(proc.cpu_percent(None) / self.ncpu, 1)
                    row["ppid"] = proc.ppid()
                    try:
                        mi = proc.memory_info()
                        row["mem"] = mi.rss
                        row["private"] = getattr(mi, "private", None)
                    except psutil.Error:
                        row["mem"] = row["private"] = None
                    for k, fn in (("threads", proc.num_threads), ("handles", proc.num_handles)):
                        try:
                            row[k] = fn()
                        except psutil.Error:
                            row[k] = None
                    try:
                        row["prio"] = PRIO_NAME.get(int(proc.nice()), str(proc.nice()))
                    except psutil.Error:
                        row["prio"] = None
                    try:
                        io = proc.io_counters()
                        dt = max(now - c["t"], 0.001)
                        if c["io"]:
                            row["io_r"] = max(0, (io.read_bytes - c["io"].read_bytes) / dt)
                            row["io_w"] = max(0, (io.write_bytes - c["io"].write_bytes) / dt)
                        else:
                            row["io_r"] = row["io_w"] = 0
                        c["io"] = io
                    except psutil.Error:
                        row["io_r"] = row["io_w"] = None
                    c["t"] = now
                    try:
                        row["status"] = proc.status()
                    except psutil.Error:
                        row["status"] = None
            except psutil.NoSuchProcess:
                seen.discard(pid)
                continue
            except psutil.Error:
                row.setdefault("cpu", None)
            if pid in self.suspended:
                row["status"] = "suspended"
            row["rule"] = bool(st["name"] and st["name"].lower() in self.app.rules_lower())
            out.append(row)
        for pid in list(self.cache):
            if pid not in seen:
                del self.cache[pid]
                self.suspended.discard(pid)

        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        nr = nw = 0
        if self.last_net:
            dt = max(now - self.last_net[0], 0.001)
            nr = (net.bytes_recv - self.last_net[1].bytes_recv) / dt
            nw = (net.bytes_sent - self.last_net[1].bytes_sent) / dt
        self.last_net = (now, net)
        system = {
            "cpu": psutil.cpu_percent(None),
            "per_cpu": psutil.cpu_percent(None, percpu=True),
            "ncpu": self.ncpu,
            "mem_used": vm.used, "mem_total": vm.total, "mem_pct": vm.percent,
            "net_down": nr, "net_up": nw,
            "count": len(out),
            "threads": sum(r.get("threads") or 0 for r in out),
            "handles": sum(r.get("handles") or 0 for r in out),
            "boot": psutil.boot_time(),
            "admin": is_admin(),
        }
        with self.lock:
            self.snapshot = {"procs": out, "system": system, "t": now}

    def run(self):
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        while not self.stop_evt.is_set():
            try:
                self.sample()
            except Exception as e:  # keep sampling no matter what
                self.app.log(f"sample error: {e}")
            self.stop_evt.wait(float(self.app.cfg.get("sample_interval_seconds", 1.5)))


# ---------------------------------------------------------------- app
class App:
    def __init__(self):
        self.cfg = load_config()
        self.sampler = Sampler(self)
        self.events = []
        self.applied = {}  # pid -> create_time already handled by rules

    def log(self, msg):
        self.events.append({"t": time.time(), "msg": msg})
        del self.events[:-200]

    def rules_lower(self):
        return {k.lower(): v for k, v in self.cfg.get("priority_rules", {}).items()}

    def url(self):
        return f"http://{self.cfg['host']}:{self.cfg['port']}/"

    # ----- actions
    def proc(self, pid):
        return psutil.Process(int(pid))

    def set_priority(self, pid, prio, p=None):
        p = p or self.proc(pid)
        p.nice(PRIORITIES[prio])
        return f"{p.name()} ({p.pid}) priority -> {prio}"

    def set_io(self, pid, io, p=None):
        p = p or self.proc(pid)
        p.ionice(IO_PRIORITIES[io])
        return f"{p.name()} ({p.pid}) I/O priority -> {io}"

    def set_affinity(self, pid, cpus, p=None):
        p = p or self.proc(pid)
        p.cpu_affinity([int(c) for c in cpus])
        return f"{p.name()} ({p.pid}) affinity -> {len(cpus)} cores"

    def kill(self, pid, tree=False):
        p = self.proc(pid)
        name = p.name()
        targets = (p.children(recursive=True) if tree else []) + [p]
        for t in targets:
            try:
                t.kill()
            except psutil.NoSuchProcess:
                pass
        return f"killed {name} ({pid})" + (f" + {len(targets) - 1} children" if tree else "")

    def details(self, pid):
        p = self.proc(pid)
        d = {"pid": p.pid}
        calls = {
            "name": p.name, "exe": p.exe, "cwd": p.cwd, "user": p.username, "ppid": p.ppid,
            "create": p.create_time, "status": p.status, "threads": p.num_threads,
            "handles": p.num_handles, "affinity": p.cpu_affinity,
        }
        for k, fn in calls.items():
            try:
                d[k] = fn()
            except (psutil.Error, OSError):
                d[k] = None
        try:
            d["cmdline"] = subprocess.list2cmdline(p.cmdline())
        except (psutil.Error, OSError):
            d["cmdline"] = None
        try:
            d["prio"] = PRIO_NAME.get(int(p.nice()))
        except psutil.Error:
            d["prio"] = None
        try:
            d["io_prio"] = IO_NAME.get(int(p.ionice()))
        except (psutil.Error, OSError):
            d["io_prio"] = None
        try:
            m = p.memory_full_info()
            d["memory"] = {k: getattr(m, k) for k in m._fields}
        except (psutil.Error, OSError):
            try:
                m = p.memory_info()
                d["memory"] = {k: getattr(m, k) for k in m._fields}
            except psutil.Error:
                d["memory"] = None
        try:
            io = p.io_counters()
            d["io"] = {k: getattr(io, k) for k in io._fields}
        except psutil.Error:
            d["io"] = None
        try:
            ct = p.cpu_times()
            d["cpu_times"] = {"user": ct.user, "system": ct.system}
        except psutil.Error:
            d["cpu_times"] = None
        try:
            d["children"] = [{"pid": c.pid, "name": c.name()} for c in p.children()]
        except psutil.Error:
            d["children"] = []
        try:
            par = p.parent()
            d["parent_name"] = par.name() if par else None
        except psutil.Error:
            d["parent_name"] = None
        try:
            d["connections"] = [{
                "type": "TCP" if c.type == 1 else "UDP",
                "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                "status": c.status,
            } for c in p.net_connections(kind="inet")]
        except (psutil.Error, OSError):
            d["connections"] = None
        try:
            d["modules"] = sorted({m.path for m in p.memory_maps()}, key=str.lower)[:400]
        except (psutil.Error, OSError):
            d["modules"] = None
        try:
            d["files"] = [f.path for f in p.open_files()][:300]
        except (psutil.Error, OSError):
            d["files"] = None
        rule = self.rules_lower().get((d.get("name") or "").lower())
        d["rule"] = rule
        d["suspended"] = p.pid in self.sampler.suspended
        return d

    # ----- persistent rules
    def save_rule(self, name, rule):
        rules = self.cfg.setdefault("priority_rules", {})
        old = next((v for k, v in rules.items() if k.lower() == name.lower()), None)
        extra = ""
        if rule and rule.get("registry"):
            extra = " + registry (" + registry_write(name, rule) + ")"
        elif old and old.get("registry"):
            registry_remove(name)
            extra = " (removed from registry)"
        for k in list(rules):
            if k.lower() == name.lower():
                del rules[k]
        if rule:
            rules[name] = rule
        save_config(self.cfg)
        self.applied.clear()
        return extra

    def unregister_all(self):
        """Remove every registry entry Proc View wrote (keeps the rules in config.json)."""
        n = 0
        for name, r in self.cfg.get("priority_rules", {}).items():
            if r.get("registry"):
                registry_remove(name)
                r.pop("registry", None)
                n += 1
        save_config(self.cfg)
        return f"removed {n} rule(s) from the Windows registry"

    def apply_rules_loop(self):
        while True:
            rules = self.rules_lower()
            if rules:
                live = set()
                for p in psutil.process_iter(["name", "create_time"]):
                    n = (p.info["name"] or "").lower()
                    if n not in rules:
                        continue
                    key = (p.pid, p.info["create_time"])
                    live.add(key)
                    if key in self.applied:
                        continue
                    r = rules[n]
                    msgs = []
                    try:
                        if r.get("priority") in PRIORITIES and PRIO_NAME.get(int(p.nice())) != r["priority"]:
                            msgs.append(self.set_priority(p.pid, r["priority"], p))
                        if r.get("io") in IO_PRIORITIES:
                            try:
                                if IO_NAME.get(int(p.ionice())) != r["io"]:
                                    msgs.append(self.set_io(p.pid, r["io"], p))
                            except (psutil.Error, OSError) as e:
                                msgs.append(f"I/O priority failed: {e}")
                        if r.get("affinity"):
                            if sorted(p.cpu_affinity()) != sorted(r["affinity"]):
                                msgs.append(self.set_affinity(p.pid, r["affinity"], p))
                        self.applied[key] = True
                    except psutil.NoSuchProcess:
                        continue
                    except (psutil.Error, OSError) as e:
                        self.applied[key] = True
                        msgs.append(f"rule for {n} ({p.pid}) failed: {e}")
                    for m in msgs:
                        self.log("[rule] " + m)
                for k in list(self.applied):
                    if k not in live:
                        del self.applied[k]
            time.sleep(float(self.cfg.get("apply_rules_interval_seconds", 3)))

    # ----- http
    def handle_action(self, body):
        a = body.get("action")
        pid = body.get("pid")
        if a == "priority":
            return self.set_priority(pid, body["value"])
        if a == "io":
            return self.set_io(pid, body["value"])
        if a == "affinity":
            return self.set_affinity(pid, body["value"])
        if a == "kill":
            return self.kill(pid, bool(body.get("tree")))
        if a == "suspend":
            suspend(int(pid))
            self.sampler.suspended.add(int(pid))
            return f"suspended {pid}"
        if a == "resume":
            resume(int(pid))
            self.sampler.suspended.discard(int(pid))
            return f"resumed {pid}"
        if a == "open_location":
            exe = self.proc(pid).exe()
            subprocess.Popen(["explorer", "/select,", exe])
            return f"opened {exe}"
        if a == "unregister_all":
            return self.unregister_all()
        if a == "rule":
            name = body["name"]
            rule = {k: v for k, v in (body.get("rule") or {}).items() if v not in (None, "", [], False)}
            if set(rule) == {"registry"}:
                rule = {}
            extra = self.save_rule(name, rule)
            return f"rule {'saved' if rule else 'removed'} for {name}{extra}"
        raise ValueError(f"unknown action {a}")

    def serve(self):
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, ctype="application/json"):
                body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(RES, "web", "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    with app.sampler.lock:
                        snap = dict(app.sampler.snapshot)
                    snap["events"] = app.events[-50:]
                    snap["rules"] = app.cfg.get("priority_rules", {})
                    return self._send(200, snap)
                if u.path == "/api/details":
                    try:
                        pid = int(parse_qs(u.query)["pid"][0])
                        return self._send(200, app.details(pid))
                    except psutil.NoSuchProcess:
                        return self._send(404, {"error": "process has exited"})
                    except Exception as e:
                        return self._send(400, {"error": str(e)})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if self.headers.get("Origin") not in (None, app.url().rstrip("/"),
                                                      f"http://localhost:{app.cfg['port']}"):
                    return self._send(403, {"error": "bad origin"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    msg = app.handle_action(body)
                    app.log(msg)
                    self._send(200, {"ok": True, "msg": msg})
                except psutil.AccessDenied:
                    self._send(403, {"error": "Access denied (protected/system process"
                                     + ("" if is_admin() else " - run as admin") + ")"})
                except psutil.NoSuchProcess:
                    self._send(404, {"error": "process has exited"})
                except Exception as e:
                    self._send(400, {"error": str(e)})

        self.server = ThreadingHTTPServer((self.cfg["host"], int(self.cfg["port"])), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # ----- tray
    def make_icon(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), 12, fill=(24, 28, 40, 255))
        for i, h in enumerate((18, 34, 26, 46, 30)):
            x = 9 + i * 10
            d.rectangle((x, 56 - h, x + 6, 56), fill=(60, 200, 140, 255))
        return img

    def run_tray(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda i, it: webbrowser.open(self.url()), default=True),
            pystray.MenuItem("Buy me a coffee", lambda i, it: webbrowser.open(DONATE_URL)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda i, it: i.stop()),
        )
        self.icon = pystray.Icon("proc-view", self.make_icon(), f"Proc View {__version__}", menu)
        self.icon.run()

    def run(self):
        self.serve()
        threading.Thread(target=self.sampler.run, daemon=True).start()
        threading.Thread(target=self.apply_rules_loop, daemon=True).start()
        if not is_admin():
            self.log("Not running as admin: some system processes are hidden from management.")
        if self.cfg.get("open_browser_on_start", True) and "--no-browser" not in sys.argv:
            threading.Timer(1.0, lambda: webbrowser.open(self.url())).start()
        try:
            self.run_tray()
        finally:
            self.sampler.stop_evt.set()
            self.server.shutdown()


def port_in_use(host, port):
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main():
    if "--remove-registry-rules" in sys.argv:  # cleanup before uninstalling
        if not is_admin():
            relaunch_as_admin()
            return
        app = App()
        msg = app.unregister_all()
        ctypes.windll.user32.MessageBoxW(None, msg, "Proc View", 0x40)
        return
    cfg = load_config()
    if port_in_use(cfg["host"], int(cfg["port"])):
        webbrowser.open(f"http://{cfg['host']}:{cfg['port']}/")
        return
    if cfg.get("auto_elevate", True) and not is_admin() and "--no-elevate" not in sys.argv:
        if relaunch_as_admin():
            return
    App().run()


if __name__ == "__main__":
    main()
