"""Builds dist/ProcView-portable.zip: Proc View + the official embeddable Python (signed by the
Python Software Foundation) + dependencies. Users unzip it and run "Start Proc View.bat";
nothing is installed and no internet is needed.

Run:  python build_portable.py
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PY_VERSION = "3.14.6"          # embeddable Python bundled in the zip
PACKAGES = ["psutil", "pystray", "pillow"]

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build", "portable")
DIST = os.path.join(ROOT, "dist")
CACHE = os.path.join(ROOT, "build", "cache")
APPDIR = os.path.join(BUILD, "ProcView")

START_BAT = r'''@echo off
rem Starts Proc View with the bundled Python (no install needed).
start "" "%~dp0python\pythonw.exe" "%~dp0app\procview.py" %*
'''

REMOVE_BAT = r'''@echo off
rem Removes every registry priority rule Proc View wrote. Run this before deleting Proc View.
start "" "%~dp0python\pythonw.exe" "%~dp0app\procview.py" --remove-registry-rules
'''

README_TXT = """Proc View {ver} (portable)
https://github.com/KuzuCevirme/proc-view

1. Extract this zip to a folder (do not run it from inside the zip).
2. Double-click "Start Proc View.bat".
   It asks for administrator rights and opens http://127.0.0.1:8792 in your browser.
   The tray icon (bottom right) has Open and Quit.

Your settings and rules are saved in app\\config.json.
Before deleting Proc View, run "Remove Registry Rules.bat" if you used "Also in registry".

Bundled: official embeddable Python {py} (python.org) and psutil, pystray, pillow.
"""


def version():
    src = open(os.path.join(ROOT, "procview.py"), encoding="utf-8").read()
    return re.search(r'__version__ = "([^"]+)"', src).group(1)


def fetch_python():
    os.makedirs(CACHE, exist_ok=True)
    name = f"python-{PY_VERSION}-embed-amd64.zip"
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        url = f"https://www.python.org/ftp/python/{PY_VERSION}/{name}"
        print("downloading", url)
        urllib.request.urlretrieve(url, path)
    return path


def main():
    ver = version()
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(APPDIR)

    # 1. embeddable Python
    pydir = os.path.join(APPDIR, "python")
    with zipfile.ZipFile(fetch_python()) as z:
        z.extractall(pydir)
    pth = next(f for f in os.listdir(pydir) if f.endswith("._pth"))
    with open(os.path.join(pydir, pth), "a", encoding="utf-8") as f:
        f.write("Lib\\site-packages\n")

    # 2. dependencies as prebuilt wheels for that Python
    short = ".".join(PY_VERSION.split(".")[:2])
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
        "--target", os.path.join(pydir, "Lib", "site-packages"),
        "--only-binary=:all:", "--platform", "win_amd64",
        "--python-version", short, "--implementation", "cp", *PACKAGES])

    # 3. the app (no config.json: users start clean)
    app = os.path.join(APPDIR, "app")
    os.makedirs(app)
    shutil.copy2(os.path.join(ROOT, "procview.py"), app)
    shutil.copytree(os.path.join(ROOT, "web"), os.path.join(app, "web"))
    for name in ("LICENSE", "config.example.json"):
        shutil.copy2(os.path.join(ROOT, name), app)

    for name, text in (("Start Proc View.bat", START_BAT),
                       ("Remove Registry Rules.bat", REMOVE_BAT),
                       ("README.txt", README_TXT.format(ver=ver, py=PY_VERSION))):
        with open(os.path.join(APPDIR, name), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text)

    # 4. zip
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "ProcView-portable.zip")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, _, files in os.walk(APPDIR):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, BUILD))
    sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
    print(f"Done: {out} ({os.path.getsize(out) / 1e6:.1f} MB)\nSHA-256: {sha}")


if __name__ == "__main__":
    main()
