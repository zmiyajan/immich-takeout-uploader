#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Immich Takeout Uploader.

A small local web UI that drives immich-go to import a Google Photos takeout
into an Immich server. It reads the zip parts in place, checks that each one is
intact, verifies the API key's permissions before starting, and streams the
live log while the upload runs.

Zero dependencies, standard library only. Python 3.6+.

    python3 app.py     then open http://127.0.0.1:8765
"""

import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import webbrowser
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

PORT = int(os.environ.get("IG_PORT", "8765"))
MAX_LOG_LINES = 20000
IS_MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Everything after cookie:/authorization:/api-key: is hidden before it reaches the UI.
SECRET_PAT = re.compile(
    r"(?im)^(.*?\b(?:cookie|set-cookie|authorization|x-api-key|api[-_ ]?key)\b\s*[:=]\s*).+$"
)


def scrub(text):
    return SECRET_PAT.sub(lambda m: m.group(1) + "********", text)


def find_binary():
    """Locate the immich-go executable."""
    p = shutil.which("immich-go")
    if p:
        return p
    for cand in (os.path.join(HOME, ".local/bin/immich-go"),
                 os.path.join(APP_DIR, "immich-go"),
                 "/usr/local/bin/immich-go"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ------------------------------------------------------- immich-go installer

RELEASES = "https://api.github.com/repos/simulot/immich-go/releases/latest"


def release_asset_name():
    """Map this machine to the matching immich-go release asset."""
    machine = os.uname()[4].lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None, "arch:" + machine
    if sys.platform == "darwin":
        system = "Darwin"
    elif sys.platform.startswith("linux"):
        system = "Linux"
    elif sys.platform.startswith("freebsd"):
        system = "Freebsd"
    else:
        return None, "os:" + sys.platform
    return "immich-go_%s_%s.tar.gz" % (system, arch), None


def install_immich_go():
    """Fetch the correct immich-go release and place it in ~/.local/bin.

    Downloading here rather than through the browser also avoids macOS marking
    the binary as quarantined, which would make Gatekeeper kill it on launch.
    """
    asset, err = release_asset_name()
    if not asset:
        return {"error": "unsupported_platform", "arg": err}

    ctx = ssl.create_default_context()
    try:
        req = Request(RELEASES, headers={"Accept": "application/vnd.github+json",
                                         "User-Agent": "immich-takeout-uploader"})
        data = json.loads(urlopen(req, timeout=20, context=ctx).read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": "release_lookup_failed", "arg": str(exc)[:80]}

    url = None
    for a in data.get("assets", []):
        if a.get("name") == asset:
            url = a.get("browser_download_url")
            break
    if not url:
        return {"error": "asset_not_found", "arg": asset}

    dest_dir = os.path.join(HOME, ".local", "bin")
    try:
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        tmp = os.path.join(dest_dir, ".immich-go.download")
        req = Request(url, headers={"User-Agent": "immich-takeout-uploader"})
        with urlopen(req, timeout=180, context=ctx) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)

        import tarfile
        extracted = os.path.join(dest_dir, ".immich-go.extracted")
        with tarfile.open(tmp) as tar:
            member = None
            for m in tar.getmembers():
                if os.path.basename(m.name) == "immich-go" and m.isfile():
                    member = m
                    break
            if member is None:
                os.remove(tmp)
                return {"error": "binary_not_in_archive", "arg": asset}
            with tar.extractfile(member) as src, open(extracted, "wb") as f:
                shutil.copyfileobj(src, f)

        final = os.path.join(dest_dir, "immich-go")
        os.chmod(extracted, 0o755)
        os.rename(extracted, final)
        os.remove(tmp)
    except Exception as exc:  # noqa: BLE001
        return {"error": "install_failed", "arg": str(exc)[:80]}

    try:
        out = subprocess.check_output([final, "--version"], stderr=subprocess.STDOUT,
                                      universal_newlines=True).strip()
    except Exception:  # noqa: BLE001
        out = ""
    return {"ok": True, "path": final, "version": out, "tag": data.get("tag_name", "")}


class Runner(object):
    """Runs one child process, streaming its output into a bounded buffer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.lines = deque(maxlen=MAX_LOG_LINES)
        self.base = 0
        self.running = False
        self.exit_code = None

    def snapshot(self, offset):
        with self.lock:
            start = max(offset - self.base, 0)
            return {"lines": list(self.lines)[start:],
                    "offset": self.base + len(self.lines),
                    "running": self.running, "exitCode": self.exit_code}

    def log(self, text):
        with self.lock:
            if len(self.lines) == self.lines.maxlen:
                self.base += 1
            self.lines.append(scrub(text))

    def reset(self):
        with self.lock:
            self.lines.clear()
            self.base = 0
            self.exit_code = None

    def run_sync(self, argv, env=None):
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                universal_newlines=True, errors="replace", env=env)
            for line in self.proc.stdout:
                self.log(line.rstrip("\n"))
            self.proc.wait()
            return self.proc.returncode
        except Exception as exc:  # noqa: BLE001
            self.log("!! %s" % exc)
            return -1

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            return True
        return False


UPLOAD = Runner()


# ---------------------------------------------------------------- disks

def list_volumes():
    out = []
    roots = ["/Volumes"] if IS_MAC else ["/media", "/mnt", "/srv"]
    for base in roots:
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if not os.path.isdir(path) or os.path.realpath(path) == "/":
                continue
            try:
                u = shutil.disk_usage(path)
                free, total = u.free, u.total
            except OSError:
                free = total = 0
            # statfs can succeed while readdir is denied (macOS privacy protection),
            # so probe the listing separately and report it to the UI.
            readable = True
            try:
                os.listdir(path)
            except OSError:
                readable = False
            out.append({"name": name, "path": path, "free": free,
                        "total": total, "readable": readable})
    try:
        u = shutil.disk_usage(HOME)
        out.append({"name": "~", "path": HOME, "free": u.free,
                    "total": u.total, "readable": True, "isHome": True})
    except OSError:
        pass
    return out


def browse(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {"error": "path_missing", "arg": path}
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        code = "read_denied_mac" if (getattr(exc, "errno", None) == 1 and IS_MAC) else "read_denied"
        return {"error": code, "arg": exc.strerror or str(exc)}
    dirs, zips = [], []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
            elif name.lower().endswith(".zip"):
                zips.append({"name": name, "path": full, "size": os.path.getsize(full)})
        except OSError:
            continue
    parent = os.path.dirname(path)
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        free = 0
    return {"path": path, "parent": parent if parent != path else None,
            "dirs": dirs, "zips": zips, "free": free}


# ------------------------------------------------------- connection preflight

# Read scopes are confirmed with a plain GET.
#
# Write scopes are confirmed without creating anything: the request carries a
# deliberately empty body, so Immich rejects it during validation. Authorisation
# is checked before validation, which makes the status code the answer:
#   403 -> the key lacks the scope
#   401 -> the key is not valid at all
#   anything else (400, 404, 422) -> the scope is present, the body was refused
# A 2xx would mean the payload was accepted after all, so it is reported rather
# than counted as a pass.
PERM_CHECKS = [
    ("user.read", "GET", "/api/users/me", None),
    ("server.about", "GET", "/api/server/about", None),
    ("asset.statistics", "GET", "/api/assets/statistics", None),
    ("album.read", "GET", "/api/albums", None),
    ("asset.read", "POST", "/api/search/metadata", {}),
    ("asset.upload", "POST", "/api/assets", {}),
    ("asset.update", "PUT", "/api/assets", {}),
    ("album.create", "POST", "/api/albums", {}),
    ("albumAsset.create", "PUT",
     "/api/albums/00000000-0000-0000-0000-000000000000/assets", {}),
    ("tag.create", "POST", "/api/tags", {}),
    ("tag.asset", "PUT", "/api/tags/assets", {}),
]


def http_call(url, key, method="GET", body=None, timeout=12):
    data = None
    headers = {"x-api-key": key, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers)
    req.get_method = lambda: method
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urlopen(req, timeout=timeout, context=ctx)


def http_get(url, key, timeout=12):
    return http_call(url, key, "GET", None, timeout)


def normalize(server):
    server = (server or "").strip().rstrip("/")
    if server and not server.startswith("http"):
        server = "http://" + server
    return server


def preflight(server, key):
    server = normalize(server)
    key = (key or "").strip()
    if not server:
        return {"error": "no_server"}
    if not key:
        return {"error": "no_key"}

    try:
        body = http_get(server + "/api/server/ping", key, timeout=8).read().decode("utf-8", "replace")
        if '"pong"' not in body:
            return {"reach": False, "note": body[:70]}
    except HTTPError as e:
        return {"reach": False, "note": "HTTP %s" % e.code}
    except URLError as e:
        return {"reach": False, "note": str(getattr(e, "reason", e))[:70]}
    except Exception as exc:  # noqa: BLE001
        return {"reach": False, "note": str(exc)[:70]}

    perms = []
    for perm, method, path, body in PERM_CHECKS:
        try:
            http_call(server + path, key, method, body, timeout=10)
            # A write probe is not supposed to succeed; say so instead of
            # silently treating it as a pass.
            note = "" if body is None else "accepted"
            perms.append({"perm": perm, "ok": True, "note": note})
        except HTTPError as e:
            if e.code == 403:
                perms.append({"perm": perm, "ok": False, "note": "forbidden"})
            elif e.code == 401:
                perms.append({"perm": perm, "ok": False, "note": "unauthorized"})
            else:
                # Rejected by validation, which means authorisation passed.
                perms.append({"perm": perm, "ok": True, "note": ""})
        except Exception:  # noqa: BLE001
            perms.append({"perm": perm, "ok": False, "note": "failed"})

    return {"reach": True, "server": server, "perms": perms}


# ---------------------------------------------------------------- upload

def build_argv(cfg):
    binary = find_binary()
    if not binary:
        return None, "no_binary", None

    server = normalize(cfg.get("server"))
    api_key = (cfg.get("apiKey") or "").strip()
    admin_key = (cfg.get("adminKey") or "").strip()
    zips = cfg.get("zips") or []

    if not server:
        return None, "no_server", None
    if not api_key:
        return None, "no_key", None
    if not zips:
        return None, "no_zips", None
    # immich-go accepts either the zip parts or one extracted takeout folder.
    for z in zips:
        if not os.path.exists(z):
            return None, "missing_file", os.path.basename(z.rstrip("/"))

    # caffeinate keeps a Mac awake for the whole transfer; Linux servers do not sleep.
    argv = ["caffeinate", "-i"] if IS_MAC else []
    argv += [binary, "upload", "from-google-photos", "--no-ui",
             "--server", server, "--api-key", api_key]

    if admin_key:
        argv += ["--admin-api-key", admin_key]
    else:
        # Pausing Immich jobs needs an admin-linked key; without one it would 403.
        argv += ["--pause-immich-jobs=false"]

    # Boolean flags immich-go enables by default; only emit them when turned off.
    for key, flag in (("syncAlbums", "--sync-albums"),
                      ("peopleTag", "--people-tag"),
                      ("takeoutTag", "--takeout-tag"),
                      ("includeArchived", "--include-archived"),
                      ("includePartner", "--include-partner")):
        if not cfg.get(key, True):
            argv.append(flag + "=false")

    # Boolean flags that are off by default; only emit them when turned on.
    for key, flag in (("includeTrashed", "--include-trashed"),
                      ("includeUnmatched", "--include-unmatched"),
                      ("includeUntitled", "--include-untitled-albums"),
                      ("epsonFastFoto", "--manage-epson-fastfoto"),
                      ("sessionTag", "--session-tag"),
                      ("overwrite", "--overwrite"),
                      ("skipVerifySSL", "--skip-verify-ssl"),
                      ("apiTrace", "--api-trace"),
                      ("dryRun", "--dry-run")):
        if cfg.get(key):
            argv.append(flag)

    if cfg.get("continueOnError", True):
        argv += ["--on-errors", "continue"]

    # Free-text and choice flags, skipped entirely when left at their default.
    for key, flag, default in (("includeType", "--include-type", "all"),
                               ("manageRawJpeg", "--manage-raw-jpeg", "NoStack"),
                               ("manageHeicJpeg", "--manage-heic-jpeg", "NoStack"),
                               ("manageBurst", "--manage-burst", "NoStack")):
        val = (cfg.get(key) or "").strip()
        if val and val != default:
            argv += [flag, val]

    for key, flag in (("fromAlbum", "--from-album-name"),
                      ("partnerAlbum", "--partner-shared-album"),
                      ("dateRange", "--date-range"),
                      ("includeExt", "--include-extensions"),
                      ("excludeExt", "--exclude-extensions"),
                      ("timeZone", "--time-zone"),
                      ("clientTimeout", "--client-timeout")):
        val = (cfg.get(key) or "").strip()
        if val:
            argv += [flag, val]

    # Tags may be hierarchical (parent/child) and repeat for each value.
    for tag in (cfg.get("tags") or "").split(","):
        tag = tag.strip()
        if tag:
            argv += ["--tag", tag]

    try:
        tasks = int(cfg.get("concurrent") or 6)
    except (TypeError, ValueError):
        tasks = 6
    argv += ["--concurrent-tasks", str(max(1, min(20, tasks)))]
    argv += ["--log-file", os.path.join(APP_DIR, "last-run.log")]
    argv += zips
    return argv, None, None


def upload_worker(argv, zips):
    UPLOAD.reset()
    with UPLOAD.lock:
        UPLOAD.running = True

    # Keep scratch files on the same disk as the archives, so a small system
    # drive is never touched no matter how large the takeout is.
    env = os.environ.copy()
    tmp = None
    if zips:
        tmp = os.path.join(os.path.dirname(os.path.abspath(zips[0])), ".immich-go-tmp")
        try:
            if not os.path.isdir(tmp):
                os.makedirs(tmp)
            env["IMMICHGO_TEMPDIR"] = tmp
            env["TMPDIR"] = tmp
            UPLOAD.log("temp dir: %s" % tmp)
        except OSError:
            tmp = None

    code = UPLOAD.run_sync(argv, env=env)

    if tmp and os.path.isdir(tmp):
        try:
            if not os.listdir(tmp):
                os.rmdir(tmp)
        except OSError:
            pass

    with UPLOAD.lock:
        UPLOAD.exit_code = code
        UPLOAD.running = False
    UPLOAD.log("")
    UPLOAD.log("=== finished · exit code %s ===" % code)


def mask_argv(argv):
    safe = list(argv)
    for i, tok in enumerate(safe):
        if tok in ("--api-key", "--admin-api-key") and i + 1 < len(safe):
            safe[i + 1] = "********"
    return " ".join(shlex.quote(t) for t in safe)


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        parsed = urlparse(self.path)
        route, qs = parsed.path, parse_qs(parsed.query)
        if route == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif route == "/env":
            self._json({"volumes": list_volumes(), "home": HOME,
                        "binary": find_binary(), "isMac": IS_MAC,
                        "host": os.uname()[1]})
        elif route == "/browse":
            self._json(browse((qs.get("dir") or [HOME])[0]))
        elif route == "/logs":
            try:
                off = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                off = 0
            self._json(UPLOAD.snapshot(off))
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            cfg = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json({"error": "bad_data"}, 400)
            return

        if route == "/test":
            self._json(preflight(cfg.get("server"), cfg.get("apiKey")))

        elif route == "/verify":
            zips = cfg.get("zips") or []
            if not zips:
                self._json({"error": "no_zips"}, 400)
                return
            out = []
            for z in zips:
                item = {"name": os.path.basename(z), "ok": False, "size": 0,
                        "entries": 0, "error": ""}
                try:
                    if os.path.isdir(z):
                        item["error"] = "not_an_archive"
                        out.append(item)
                        continue
                    item["size"] = os.path.getsize(z)
                    # Reading the central directory is enough to prove the archive
                    # is complete, and stays fast even on a 50 GB part.
                    with zipfile.ZipFile(z) as zf:
                        item["entries"] = len(zf.namelist())
                    item["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    item["error"] = str(exc)[:110]
                out.append(item)
            self._json({"results": out})

        elif route == "/start":
            argv, err, arg = build_argv(cfg)
            if err:
                self._json({"error": err, "arg": arg}, 400)
                return
            with UPLOAD.lock:
                if UPLOAD.running:
                    self._json({"error": "already_running"}, 409)
                    return
            threading.Thread(target=upload_worker,
                             args=(argv, cfg.get("zips") or []), daemon=True).start()
            self._json({"ok": True, "cmd": mask_argv(argv)})

        elif route == "/install-tool":
            self._json(install_immich_go())

        elif route == "/stop":
            self._json({"ok": UPLOAD.stop()})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


PAGE = u"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHhtbG5zOnhsaW5rPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5L3hsaW5rIiB2aWV3Qm94PSIwIDAgNzkyIDc5MiI+IDxnPiA8cGF0aCBmaWxsPSIjRkEyOTIxIiBkPSJNMzc1LjQ4LDI2Ny42M2MzOC42NCwzNC4yMSw2OS43OCw3MC44Nyw4OS44MiwxMDUuNDJjMzQuNDItNjEuNTYsNTcuNDItMTM0LjcxLDU3LjcxLTE4MS4zIGMwLTAuMzMsMC0wLjYzLDAtMC45MWMwLTY4Ljk0LTY4Ljc3LTk1Ljc3LTEyOC4wMS05NS43N3MtMTI4LjAxLDI2LjgzLTEyOC4wMSw5NS43N2MwLDAuOTQsMCwyLjIsMCwzLjcyIEMzMDAuMDEsMjA5LjI0LDMzOS4xNSwyMzUuNDcsMzc1LjQ4LDI2Ny42M3oiLz4gPHBhdGggZmlsbD0iI0VENzlCNSIgZD0iTTE2NC43LDQ1NS42M2MyNC4xNS0yNi44Nyw2MS4yLTU1Ljk5LDEwMy4wMS04MC42MWM0NC40OC0yNi4xOCw4OC45Ny00NC40NywxMjguMDItNTIuODQgYy00Ny45MS01MS43Ni0xMTAuMzctOTYuMjQtMTU0LjYtMTEwLjkxYy0wLjMxLTAuMS0wLjYtMC4xOS0wLjg2LTAuMjhjLTY1LjU3LTIxLjMtMTEyLjM0LDM1LjgxLTEzMC42NCw5Mi4xNSBjLTE4LjMsNTYuMzQtMTQuMDQsMTMwLjA0LDUxLjUzLDE1MS4zNEMxNjIuMDUsNDU0Ljc3LDE2My4yNSw0NTUuMTYsMTY0LjcsNDU1LjYzeiIvPiA8cGF0aCBmaWxsPSIjRkZCNDAwIiBkPSJNNjgxLjA3LDMwMi4xOWMtMTguMy01Ni4zNC02NS4wNy0xMTMuNDUtMTMwLjY0LTkyLjE1Yy0wLjksMC4yOS0yLjEsMC42OC0zLjU0LDEuMTUgYy0zLjc1LDM1LjkzLTE2LjYsODEuMjctMzUuOTYsMTI1Ljc2Yy0yMC41OSw0Ny4zMi00NS44NCw4OC4yNy03Mi41MSwxMThjNjkuMTgsMTMuNzIsMTQ1Ljg2LDEyLjk4LDE5MC4yNi0xLjE0IGMwLjMxLTAuMSwwLjYtMC4yLDAuODYtMC4yOEM2OTUuMTEsNDMyLjIyLDY5OS4zNywzNTguNTIsNjgxLjA3LDMwMi4xOXoiLz4gPHBhdGggZmlsbD0iIzFFODNGNyIgZD0iTTMzNi41NCw1MTAuNzFjLTExLjE1LTUwLjM5LTE0LjgtOTguMzYtMTAuNy0xMzguMDhjLTY0LjAzLDI5LjU3LTEyNS42Myw3NS4yMy0xNTMuMjYsMTEyLjc2IGMtMC4xOSwwLjI2LTAuMzcsMC41MS0wLjUzLDAuNzNjLTQwLjUyLDU1Ljc4LTAuNjYsMTE3LjkxLDQ3LjI3LDE1Mi43MmM0Ny45MiwzNC44MiwxMTkuMzMsNTMuNTQsMTU5Ljg2LTIuMjQgYzAuNTYtMC43NiwxLjMtMS43OCwyLjE5LTMuMDFDMzYzLjI4LDYwMi4zMiwzNDcuMDIsNTU4LjA4LDMzNi41NCw1MTAuNzF6Ii8+IDxwYXRoIGZpbGw9IiMxOEMyNDkiIGQ9Ik02MTcuNTcsNDgyLjUyYy0zNS4zMyw3LjU0LTgyLjQyLDkuMzMtMTMwLjcyLDQuNjZjLTUxLjM3LTQuOTYtOTguMTEtMTYuMzItMTM0LjYzLTMyLjUgYzguMzMsNzAuMDMsMzIuNzMsMTQyLjczLDU5Ljg4LDE4MC42YzAuMTksMC4yNiwwLjM3LDAuNTEsMC41MywwLjczYzQwLjUyLDU1Ljc4LDExMS45MywzNy4wNiwxNTkuODYsMi4yNCBjNDcuOTItMzQuODIsODcuNzktOTYuOTUsNDcuMjctMTUyLjcyQzYxOS4yLDQ4NC43Nyw2MTguNDYsNDgzLjc1LDYxNy41Nyw0ODIuNTJ6Ii8+IDwvZz4gPC9zdmc+">
<title>Immich Takeout Uploader</title>
<style>
  /* Immich brand tokens — pulled from immich-app/immich web/src/app.css
     light : primary #4250AF · bg #FFFFFF · fg #000000
     dark  : primary #ACCBFA · bg #0A0A0A · fg #E5E7EB · gray #212121
     logo  : #FA2921 #FFB400 #18C249 #1E83F7 #ED79B5                      */
  :root{
    --bg:#ffffff; --surface:#f6f7f9; --raised:#eceef1; --edge:#e0e3e8;
    --primary:#4250af; --on-primary:#ffffff; --primary-soft:rgba(66,80,175,.09);
    --fg:#000000; --dim:#5b6270; --faint:#8b93a1;
    --ok:#12923a; --bad:#d81f18; --warn:#a97400;
    --ok-bg:rgba(18,146,58,.1); --bad-bg:rgba(216,31,24,.09); --warn-bg:rgba(169,116,0,.11);
    --log-bg:#f6f7f9; --log-fg:#3b4252; --shadow:0 1px 2px rgba(0,0,0,.05);
    --red:#fa2921; --amber:#ffb400; --green:#18c249; --blue:#1e83f7; --pink:#ed79b5;
    --sans:"Google Sans","SF Arabic","Geeza Pro",-apple-system,system-ui,"Segoe UI",sans-serif;
    --mono:"Google Sans Code","SF Mono",ui-monospace,Menlo,Consolas,monospace;
    --spectrum:linear-gradient(90deg,var(--red),var(--amber),var(--green),var(--blue),var(--pink));
    color-scheme:light;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --bg:#0a0a0a; --surface:#141414; --raised:#212121; --edge:#2e2e2e;
      --primary:#accbfa; --on-primary:#0d1b2e; --primary-soft:rgba(172,203,250,.13);
      --fg:#e5e7eb; --dim:#9ca3af; --faint:#6b7280;
      --ok:#18c249; --bad:#fa2921; --warn:#ffb400;
      --ok-bg:rgba(24,194,73,.11); --bad-bg:rgba(250,41,33,.1); --warn-bg:rgba(255,180,0,.1);
      --log-bg:#000000; --log-fg:#b9bec9; --shadow:none;
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
    --bg:#0a0a0a; --surface:#141414; --raised:#212121; --edge:#2e2e2e;
    --primary:#accbfa; --on-primary:#0d1b2e; --primary-soft:rgba(172,203,250,.13);
    --fg:#e5e7eb; --dim:#9ca3af; --faint:#6b7280;
    --ok:#18c249; --bad:#fa2921; --warn:#ffb400;
    --ok-bg:rgba(24,194,73,.11); --bad-bg:rgba(250,41,33,.1); --warn-bg:rgba(255,180,0,.1);
    --log-bg:#000000; --log-fg:#b9bec9; --shadow:none;
    color-scheme:dark;
  }

  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans)}
  body{font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased}
  /* Immich tracks Latin text by .1px, but any tracking breaks the cursive joins
     in Arabic, so it is applied only when the interface is in English. */
  html[lang="en"] body{letter-spacing:.1px}
  html[lang="ar"]{letter-spacing:normal}
  html[lang="ar"] .tag,html[lang="ar"] .mono{letter-spacing:.12em}
  /* Always render Western digits, in both languages. */
  body,input,button{font-variant-numeric:lining-nums tabular-nums}
  .wrap{max-width:900px;margin:0 auto;padding:26px 22px 80px}

  /* header */
  .top{display:flex;align-items:flex-start;gap:15px;margin-bottom:8px}
  .logo{width:44px;height:44px;flex:none;display:block}
  .titles{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
  h1{font-size:24px;font-weight:600;margin:0;letter-spacing:-.2px;line-height:1.3}
  .tag{font-family:var(--mono);font-size:10.5px;color:var(--faint);
       letter-spacing:.14em;text-transform:uppercase;direction:ltr;line-height:1.5}
  html[dir=rtl] .tag{text-align:right}
  .toggles{display:flex;gap:7px;flex:none}
  .tog{background:var(--surface);color:var(--dim);border:1px solid var(--edge);border-radius:999px;
       padding:6px 13px;font-size:12.5px;cursor:pointer;transition:.18s;box-shadow:var(--shadow);
       display:inline-flex;align-items:center;gap:6px;line-height:1.6}
  .tog:hover{border-color:var(--primary);color:var(--primary)}
  .langsel select{background:transparent;border:0;color:inherit;font-family:var(--sans);
       font-size:12.5px;cursor:pointer;padding:0 2px;outline:none}
  .langsel select option{background:var(--surface);color:var(--fg)}
  .lede{color:var(--dim);font-size:14px;margin:10px 0 0;max-width:64ch}
  .pills{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px;margin-bottom:24px}
  .pill{display:inline-flex;align-items:center;gap:8px;background:var(--surface);
        border:1px solid var(--edge);border-radius:999px;padding:7px 16px;font-size:13px;
        color:var(--dim);box-shadow:var(--shadow);white-space:nowrap}
  .pill .sep{color:var(--faint);opacity:.5}
  .pill b{color:var(--fg);font-weight:600}
  .ic{fill:currentColor;flex:none;vertical-align:-.18em}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}
  .dot.on{background:var(--ok)} .dot.off{background:var(--bad)} .dot.warn{background:var(--warn)}

  /* steps */
  .step{background:var(--surface);border:1px solid var(--edge);border-radius:20px;
        padding:21px 23px;margin-bottom:14px;transition:border-color .25s;box-shadow:var(--shadow)}
  .step.active{border-color:var(--primary)}
  .step.done{border-color:var(--ok)}
  .shead{display:flex;align-items:center;gap:12px;margin-bottom:16px}
  .num{width:36px;height:36px;border-radius:50%;flex:none;display:grid;place-items:center;
       background:var(--raised);color:var(--faint);transition:.25s}
  .step.active .num{background:var(--primary-soft);color:var(--primary)}
  .step.done .num{background:var(--ok-bg);color:var(--ok)}
  .shead h2{font-size:16.5px;font-weight:600;margin:0}
  .shead .sub{margin-inline-start:auto}
  .shead .sub{font-size:12.5px;color:var(--faint);margin-inline-start:auto}

  /* fields */
  .field{margin-bottom:15px}
  .field:last-child{margin-bottom:0}
  label{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--dim);margin-bottom:7px;font-weight:500}
  input[type=text],input[type=password],input[type=number]{
    width:100%;padding:11px 15px;background:var(--bg);border:1px solid var(--edge);border-radius:12px;
    color:var(--fg);font-size:14px;font-family:var(--mono);direction:ltr;text-align:left;transition:.18s}
  input::placeholder{color:var(--faint);font-family:var(--sans)}
  html[dir=rtl] input::placeholder{direction:rtl;text-align:right}
  input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)}
  .cols{display:flex;gap:13px;flex-wrap:wrap;align-items:flex-start}
  .cols>*{flex:1;min-width:200px}

  /* Help is a small button beside the label; the panel opens directly under the
     field it belongs to, so the explanation is never far from the control. */
  .i{width:18px;height:18px;border-radius:50%;border:0;background:var(--raised);color:var(--faint);
     font-family:var(--mono);font-size:10.5px;line-height:1;cursor:pointer;display:grid;
     place-items:center;padding:0;flex:none;transition:.18s}
  .i:hover,.i[aria-expanded="true"]{background:var(--primary-soft);color:var(--primary)}
  .help{display:none;margin-top:9px;background:var(--bg);border:1px solid var(--edge);
        border-radius:12px;padding:13px 16px;font-size:13px;color:var(--dim);
        position:relative;overflow:hidden}
  .help::before{content:"";position:absolute;top:0;bottom:0;width:3px;
                background:var(--spectrum);inset-inline-start:0}
  .help.open{display:block}
  .help b{color:var(--fg);font-weight:600}
  .help p{margin:10px 0 0}
  .help p:first-child{margin-top:0}
  /* BiDi isolation. A Latin run inside Arabic prose drags the neutral characters
     around it (colon, dash, comma, arrows) to the wrong side unless it is isolated. */
  bdi,code,.path{unicode-bidi:isolate}
  code{font-family:var(--mono);font-size:12.5px;background:var(--raised);padding:2px 7px;
       border-radius:6px;color:var(--primary);direction:ltr;display:inline-block}
  .path{font-family:var(--mono);font-size:12px;background:var(--raised);padding:2px 9px;
        border-radius:6px;color:var(--fg);direction:ltr;display:inline-block;white-space:nowrap}
  .perms{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:5px;margin:11px 0 0}
  .perms .chip{width:100%}
  .chip{justify-content:center;gap:0;padding:5px 10px;border-radius:8px;vertical-align:-.2em;
        background:var(--raised);border:1px solid transparent;font-family:var(--mono);
        font-size:12px;color:var(--primary);direction:ltr}
  .chip:hover{border-color:var(--primary);color:var(--primary)}
  /* The icon takes no width until hover, so the label sits centred at rest and
     slides aside to make room for the copy glyph. */
  .chip .ci{width:0;height:14px;margin-inline-start:0;opacity:0;overflow:hidden;flex:none;
            display:grid;place-items:center;
            transition:width .15s ease,margin .15s ease,opacity .15s ease}
  .chip:hover .ci,.chip:focus-visible .ci,.chip.copied .ci{width:14px;margin-inline-start:6px;opacity:.8}
  .chip.copied{color:var(--ok);border-color:var(--ok)}
  .copybar{margin-top:9px}
  .hintline{font-size:12px;color:var(--faint);margin:6px 0 0;line-height:1.55}
  .copyhint{font-size:12px;color:var(--faint)}

  /* buttons */
  button{font-family:var(--sans);font-size:14px;cursor:pointer;border-radius:999px;border:1px solid var(--edge);
         padding:9px 18px;transition:.18s;background:var(--raised);color:var(--fg);
         display:inline-flex;align-items:center;gap:7px;line-height:1.5}
  button:hover:not(:disabled){border-color:var(--primary);color:var(--primary)}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.go{background:var(--primary);color:var(--on-primary);border-color:var(--primary);font-weight:600}
  button.go:hover:not(:disabled){filter:brightness(1.08);color:var(--on-primary)}
  button.stop{background:transparent;color:var(--bad);border-color:var(--bad)}
  button.stop:hover:not(:disabled){background:var(--bad-bg);color:var(--bad)}
  button.sm{padding:7px 14px;font-size:13px;gap:6px}
  button.icon-only{padding:8px;width:34px;height:34px;justify-content:center}
  .bar{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
  :focus-visible{outline:2px solid var(--primary);outline-offset:2px}

  .seg{display:inline-flex;background:var(--raised);border:1px solid var(--edge);
       border-radius:999px;padding:3px;gap:2px;margin-bottom:12px}
  .segbtn{border:0;background:transparent;border-radius:999px;padding:6px 15px;
          font-size:13px;color:var(--dim);gap:6px}
  .segbtn:hover:not(.on){color:var(--fg)}
  .segbtn.on{background:var(--bg);color:var(--primary);box-shadow:var(--shadow)}

  /* file list */
  .strip{background:var(--bg);border:1px solid var(--edge);border-radius:16px;overflow:hidden;margin-top:13px}
  .spectrum{height:3px;background:var(--spectrum)}
  .frames{max-height:290px;overflow-y:auto}
  .frame{display:flex;align-items:center;gap:11px;padding:10px 15px;font-size:13px}
  .frame+.frame{border-top:1px solid var(--edge)}
  .frame:hover{background:var(--surface)}
  .frame input[type=checkbox]{width:15px;height:15px;accent-color:var(--primary);flex:none;cursor:pointer}
  .frame .nm{flex:1;font-family:var(--mono);font-size:12.5px;direction:ltr;text-align:left;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
  .frame .meta{font-family:var(--mono);font-size:11.5px;color:var(--faint);flex:none;direction:ltr}
  .frame.dir .nm{color:var(--primary)}
  .frame.dir .mark{color:var(--primary)}
  .frame .mark{flex:none;width:18px;display:grid;place-items:center;color:var(--faint)}
  .frame.ok .meta{color:var(--ok)} .frame.ok .mark{color:var(--ok)}
  .frame.bad{background:var(--bad-bg)}
  .frame.bad .meta,.frame.bad .mark{color:var(--bad)}
  .empty{padding:24px;text-align:center;color:var(--faint);font-size:13px}
  /* The path is monospaced and LTR; the label beside it is prose in the UI language. */
  .crumb{font-size:12px;color:var(--faint);padding:10px 15px;background:var(--surface);
         border-bottom:1px solid var(--edge);overflow-x:auto;white-space:nowrap}
  .crumb bdi{font-family:var(--mono);font-size:11.5px;direction:ltr}
  .size{font-family:var(--mono);font-size:11px;color:var(--faint);direction:ltr;unicode-bidi:isolate}

  /* Collapsible option groups keep the common case short while still exposing
     every immich-go flag for the people who need one. */
  .grp{border:1px solid var(--edge);border-radius:14px;margin-top:11px;overflow:hidden;background:var(--bg)}
  .grp>summary{cursor:pointer;padding:11px 15px;font-size:14px;font-weight:500;color:var(--fg);
               display:flex;align-items:center;gap:9px;list-style:none;user-select:none}
  .grp>summary::-webkit-details-marker{display:none}
  .grp>summary::after{content:"";width:6px;height:6px;border-inline-end:1.5px solid var(--faint);
        border-block-end:1.5px solid var(--faint);transform:rotate(45deg);margin-inline-start:auto;transition:transform .2s}
  .grp[open]>summary::after{transform:rotate(-135deg)}
  .grp>summary:hover{background:var(--surface)}
  .grp .inner{padding:4px 15px 15px;border-top:1px solid var(--edge)}
  /* Risk is shown on the row itself, so nobody has to open the explanation to
     find out a switch is the dangerous one. */
  .risk{font-size:11px;border-radius:999px;padding:2px 9px;white-space:nowrap;
        display:inline-flex;align-items:center;gap:5px;line-height:1.7}
  .risk.caution{background:var(--warn-bg);color:var(--warn)}
  .risk.danger{background:var(--bad-bg);color:var(--bad)}
  .risk.privacy{background:var(--primary-soft);color:var(--primary)}
  .risk::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor;flex:none}
  .opt .head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .opt .help{margin-top:8px}
  .warnbox{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:8px 12px;
           font-size:12.5px;margin-top:6px;line-height:1.55}
  .warnbox code{background:transparent;color:inherit;padding:0;font-size:12px}
  select{width:100%;padding:10px 13px;background:var(--bg);border:1px solid var(--edge);border-radius:12px;
         color:var(--fg);font-size:14px;font-family:var(--sans);cursor:pointer}
  select:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)}
  .grp select,.grp input{background:var(--surface)}

  /* options */
  .opt{display:flex;align-items:flex-start;gap:11px;padding:12px 0}
  .opt+.opt{border-top:1px solid var(--edge)}
  .opt input[type=checkbox]{width:16px;height:16px;accent-color:var(--primary);margin-top:4px;flex:none;cursor:pointer}
  .opt .body{flex:1;min-width:0}
  .opt .t{font-size:14px;cursor:pointer;display:block;font-weight:400;color:var(--fg);margin:0}
  /* Permission identifiers are Latin, so they keep the mono face; Arabic prose
     must not, since a monospaced face breaks the cursive joins. */
  /* The sub-line names two different things, so each is introduced by a word
     rather than run together in one undifferentiated grey string. */
  /* The flag name and required scopes live at the foot of the explanation,
     not on the row: they are reference detail, not something to scan past. */
  .meta2{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:12px;
         padding-top:11px;border-top:1px solid var(--edge)}
  .meta2 .lbl{font-size:11px;color:var(--faint)}
  .meta2 .lbl:not(:first-child){margin-inline-start:8px}
  code.tiny{font-family:var(--mono);font-size:10.5px;padding:1px 6px;border-radius:5px;
            background:var(--raised);color:var(--dim);direction:ltr;unicode-bidi:isolate}
  .opt .p{font-size:12px;color:var(--faint);margin-top:3px}

  /* results */
  .res{margin-top:14px;border-radius:14px;padding:13px 17px;font-size:13.5px;
       background:var(--raised);color:var(--dim)}
  .res.good{background:var(--ok-bg);color:var(--ok)}
  .res.bad{background:var(--bad-bg);color:var(--bad)}
  .res.warn{background:var(--warn-bg);color:var(--warn)}
  .res b{color:inherit;font-weight:600}
  .checks{margin-top:12px;border:1px solid var(--edge);border-radius:14px;overflow:hidden;background:var(--bg)}
  .chk{display:flex;align-items:center;gap:10px;padding:9px 15px;font-size:13px}
  .chk+.chk{border-top:1px solid var(--edge)}
  .chk .p{font-family:var(--mono);font-size:12px;flex:none;min-width:158px;direction:ltr;text-align:left}
  .chk .w{flex:1;color:var(--dim);font-size:12.5px}
  .chk .n{font-size:11.5px;color:var(--faint)}
  .chk.y .p{color:var(--ok)} .chk.n2 .p{color:var(--bad)} .chk.u .p{color:var(--faint)}

  #log{background:var(--log-bg);border:1px solid var(--edge);border-radius:16px;padding:16px;
       height:330px;overflow-y:auto;font-family:var(--mono);font-size:11.5px;line-height:1.65;
       direction:ltr;text-align:left;white-space:pre-wrap;word-break:break-word;
       color:var(--log-fg);margin-top:14px}
  footer{margin-top:26px;text-align:center;font-size:12px;color:var(--faint)}
  footer a{color:var(--faint)}

  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
  @media (max-width:600px){.wrap{padding:20px 14px 60px}h1{font-size:21px}
    .step{padding:17px;border-radius:16px}.top{flex-wrap:wrap}}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="top">
      <svg class="logo" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 792 792"> <g> <path fill="#FA2921" d="M375.48,267.63c38.64,34.21,69.78,70.87,89.82,105.42c34.42-61.56,57.42-134.71,57.71-181.3 c0-0.33,0-0.63,0-0.91c0-68.94-68.77-95.77-128.01-95.77s-128.01,26.83-128.01,95.77c0,0.94,0,2.2,0,3.72 C300.01,209.24,339.15,235.47,375.48,267.63z"/> <path fill="#ED79B5" d="M164.7,455.63c24.15-26.87,61.2-55.99,103.01-80.61c44.48-26.18,88.97-44.47,128.02-52.84 c-47.91-51.76-110.37-96.24-154.6-110.91c-0.31-0.1-0.6-0.19-0.86-0.28c-65.57-21.3-112.34,35.81-130.64,92.15 c-18.3,56.34-14.04,130.04,51.53,151.34C162.05,454.77,163.25,455.16,164.7,455.63z"/> <path fill="#FFB400" d="M681.07,302.19c-18.3-56.34-65.07-113.45-130.64-92.15c-0.9,0.29-2.1,0.68-3.54,1.15 c-3.75,35.93-16.6,81.27-35.96,125.76c-20.59,47.32-45.84,88.27-72.51,118c69.18,13.72,145.86,12.98,190.26-1.14 c0.31-0.1,0.6-0.2,0.86-0.28C695.11,432.22,699.37,358.52,681.07,302.19z"/> <path fill="#1E83F7" d="M336.54,510.71c-11.15-50.39-14.8-98.36-10.7-138.08c-64.03,29.57-125.63,75.23-153.26,112.76 c-0.19,0.26-0.37,0.51-0.53,0.73c-40.52,55.78-0.66,117.91,47.27,152.72c47.92,34.82,119.33,53.54,159.86-2.24 c0.56-0.76,1.3-1.78,2.19-3.01C363.28,602.32,347.02,558.08,336.54,510.71z"/> <path fill="#18C249" d="M617.57,482.52c-35.33,7.54-82.42,9.33-130.72,4.66c-51.37-4.96-98.11-16.32-134.63-32.5 c8.33,70.03,32.73,142.73,59.88,180.6c0.19,0.26,0.37,0.51,0.53,0.73c40.52,55.78,111.93,37.06,159.86,2.24 c47.92-34.82,87.79-96.95,47.27-152.72C619.2,484.77,618.46,483.75,617.57,482.52z"/> </g> </svg>
      <span class="titles">
        <h1 data-i18n="title">Takeout Uploader</h1>
        <span class="tag">google takeout &rarr; immich</span>
      </span>
      <span class="toggles">
        <span class="tog langsel"><span data-icon="lang" data-size="16"></span><select id="langSel" aria-label="Language"></select></span>
        <button type="button" class="tog icon-only" id="themeBtn" onclick="toggleTheme()" title="theme"></button>
      </span>
    </div>
    <p class="lede" data-i18n="lede"></p>
    <div id="installBar"></div>
    <div class="pills">
      <span class="pill"><span class="dot" id="d-bin"></span><span data-i18n="pill_bin"></span><span class="sep">·</span><b id="v-bin">…</b></span>
      <span class="pill"><span class="dot" id="d-disk"></span><span data-i18n="pill_disk"></span><span class="sep">·</span><b id="v-disk">…</b></span>
      <span class="pill"><span class="dot" id="d-conn"></span><span data-i18n="pill_conn"></span><span class="sep">·</span><b id="v-conn">…</b></span>
    </div>
  </header>

  <section class="step active" id="s1"><form onsubmit="return false" autocomplete="off">
    <div class="shead"><span class="num" data-icon="server" data-size="20"></span><h2 data-i18n="s1"></h2></div>
    <div class="cols">
      <div class="field">
        <label><span data-i18n="l_server"></span><button type="button" class="i" data-help="h-srv" aria-expanded="false">i</button></label>
        <input type="text" id="server" placeholder="http://192.168.1.50:2283">
        <div class="help" id="h-srv" data-i18n-html="d_server"></div>
      </div>
      <div class="field">
        <label><span data-i18n="l_key"></span><button type="button" class="i" data-help="h-key" aria-expanded="false">i</button></label>
        <input type="password" id="apiKey" data-i18n-ph="ph_key">
        <div class="help" id="h-key" data-i18n-html="d_perms"></div>
      </div>
    </div>
    <div class="field">
      <label><span data-i18n="l_admin"></span><button type="button" class="i" data-help="h-adm" aria-expanded="false">i</button></label>
      <input type="password" id="adminKey" data-i18n-ph="ph_admin">
      <div class="help" id="h-adm" data-i18n-html="d_admin"></div>
    </div>
    <div class="bar" style="margin-top:16px">
      <button type="button" class="go" id="testBtn" onclick="test()"><span data-icon="shield" data-size="17"></span><span data-i18n="b_test"></span></button>
      <span id="testState" style="font-size:13px;color:var(--dim)"></span>
    </div>
    <div id="testOut"></div>
    </form>
  </section>

  <section class="step" id="s2">
    <div class="shead"><span class="num" data-icon="zip" data-size="20"></span><h2 data-i18n="s2"></h2><span class="sub" id="selInfo"></span>
      <button type="button" class="i" data-help="h-files" aria-expanded="false">i</button></div>
    <div class="help" id="h-files" data-i18n-html="d_why" style="margin:0 0 14px"></div>
    <div class="seg" role="group">
      <button type="button" class="segbtn on" data-src="zip"><span data-icon="zip" data-size="15"></span><span data-i18n="src_zip"></span></button>
      <button type="button" class="segbtn" data-src="folder"><span data-icon="folder" data-size="15"></span><span data-i18n="src_folder"></span></button>
    </div>
    <div class="bar">
      <span style="font-size:13px;color:var(--dim)" data-i18n="disks"></span>
      <span id="disks" style="font-size:13px;color:var(--faint)">…</span>
      <button type="button" class="sm" onclick="loadEnv()"><span data-icon="refresh" data-size="15"></span><span data-i18n="b_refresh"></span></button>
    </div>
    <div class="strip">
      <div class="spectrum"></div>
      <div class="crumb" id="crumb">…</div>
      <div class="frames" id="frames"><div class="empty" data-i18n="e_pick"></div></div>
    </div>
    <div class="bar" style="margin-top:12px">
      <button type="button" class="sm" onclick="goUp()"><span data-icon="up" data-size="15"></span><span data-i18n="b_up"></span></button>
      <button type="button" class="sm" onclick="selectAll(true)"><span data-icon="checkall" data-size="15"></span><span data-i18n="b_all"></span></button>
      <button type="button" class="sm" onclick="selectAll(false)"><span data-icon="clear" data-size="15"></span><span data-i18n="b_none"></span></button>
      <button type="button" class="sm" id="verifyBtn" onclick="verify()"><span data-icon="ok" data-size="15"></span><span data-i18n="b_verify"></span></button>
      <button type="button" class="sm" id="useDirBtn" onclick="useCurrentDir()" style="display:none"><span data-icon="checkall" data-size="15"></span><span data-i18n="b_usedir"></span></button>
    </div>
    <div class="res warn" id="folderHint" data-i18n-html="folder_hint" style="display:none"></div>
    <div id="verifyOut"></div>
  </section>

  <section class="step" id="s3">
    <div class="shead"><span class="num" data-icon="tune" data-size="20"></span><h2 data-i18n="s3"></h2><span class="sub" data-i18n="s3sub"></span></div>

    <div class="field">
      <label><span data-i18n="l_preset"></span><button type="button" class="i" data-help="hf-h_preset-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_preset-1" data-i18n-html="h_preset" style="margin-bottom:8px"></div>
      <select id="preset" onchange="applyPreset()">
        <option value="custom" data-i18n="p_custom"></option>
        <option value="small"  data-i18n="p_small"></option>
        <option value="medium" data-i18n="p_medium"></option>
        <option value="large"  data-i18n="p_large"></option>
        <option value="slow"   data-i18n="p_slow"></option>
      </select>
      <p class="hintline" data-i18n="d_preset"></p>
    </div>

    <div class="opt"><input type="checkbox" id="syncAlbums" checked>
          <div class="body">
            <div class="head"><label class="t" for="syncAlbums" data-i18n="o_albums"></label><button type="button" class="i" data-help="hp-syncAlbums" aria-expanded="false">i</button></div>
            <div class="help" id="hp-syncAlbums" data-flag="--sync-albums" data-perms="album.read,album.create,albumAsset.create" data-i18n-html="h_syncAlbums"></div>
          </div></div>
    <div class="opt"><input type="checkbox" id="peopleTag" checked>
          <div class="body">
            <div class="head"><label class="t" for="peopleTag" data-i18n="o_people"></label><button type="button" class="i" data-help="hp-peopleTag" aria-expanded="false">i</button></div>
            <div class="help" id="hp-peopleTag" data-flag="--people-tag" data-perms="tag.create,tag.asset" data-i18n-html="h_peopleTag"></div>
          </div></div>
    <div class="opt"><input type="checkbox" id="takeoutTag" checked>
          <div class="body">
            <div class="head"><label class="t" for="takeoutTag" data-i18n="o_takeout"></label><button type="button" class="i" data-help="hp-takeoutTag" aria-expanded="false">i</button></div>
            <div class="help" id="hp-takeoutTag" data-flag="--takeout-tag" data-perms="tag.create,tag.asset" data-i18n-html="h_takeoutTag"></div>
          </div></div>
    <div class="opt"><input type="checkbox" id="continueOnError" checked>
          <div class="body">
            <div class="head"><label class="t" for="continueOnError" data-i18n="o_continue"></label><button type="button" class="i" data-help="hp-continueOnError" aria-expanded="false">i</button></div>
            <div class="help" id="hp-continueOnError" data-flag="--on-errors=continue" data-perms="" data-i18n-html="h_continueOnError"></div>
          </div></div>

    <details class="grp">
      <summary><span data-icon="zip" data-size="17"></span><span data-i18n="g_content"></span></summary>
      <div class="inner">
        <div class="opt"><input type="checkbox" id="includeArchived" checked>
          <div class="body">
            <div class="head"><label class="t" for="includeArchived" data-i18n="o_archived"></label><button type="button" class="i" data-help="hp-includeArchived" aria-expanded="false">i</button></div>
            <div class="help" id="hp-includeArchived" data-flag="--include-archived" data-perms="" data-i18n-html="h_includeArchived"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="includePartner" checked>
          <div class="body">
            <div class="head"><label class="t" for="includePartner" data-i18n="o_partner"></label><button type="button" class="i" data-help="hp-includePartner" aria-expanded="false">i</button></div>
            <div class="help" id="hp-includePartner" data-flag="--include-partner" data-perms="" data-i18n-html="h_includePartner"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="includeUntitled">
          <div class="body">
            <div class="head"><label class="t" for="includeUntitled" data-i18n="o_untitled"></label><span class="risk caution" data-i18n="r_caution"></span><button type="button" class="i" data-help="hp-includeUntitled" aria-expanded="false">i</button></div>
            <div class="help" id="hp-includeUntitled" data-flag="--include-untitled-albums" data-perms="" data-i18n-html="h_includeUntitled"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="includeTrashed">
          <div class="body">
            <div class="head"><label class="t" for="includeTrashed" data-i18n="o_trashed"></label><span class="risk caution" data-i18n="r_caution"></span><button type="button" class="i" data-help="hp-includeTrashed" aria-expanded="false">i</button></div>
            <div class="help" id="hp-includeTrashed" data-flag="--include-trashed" data-perms="" data-i18n-html="h_includeTrashed"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="includeUnmatched">
          <div class="body">
            <div class="head"><label class="t" for="includeUnmatched" data-i18n="o_unmatched"></label><span class="risk caution" data-i18n="r_caution"></span><button type="button" class="i" data-help="hp-includeUnmatched" aria-expanded="false">i</button></div>
            <div class="help" id="hp-includeUnmatched" data-flag="--include-unmatched" data-perms="" data-i18n-html="h_includeUnmatched"></div>
          </div></div>
        <div class="cols" style="margin-top:13px">
          <div class="field"><label><span data-i18n="l_type"></span><button type="button" class="i" data-help="hf-h_includeType-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_includeType-1" data-flag="--include-type" data-perms="" data-i18n-html="h_includeType" style="margin-bottom:8px"></div>
            <select id="includeType">
              <option value="all" data-i18n="v_all"></option>
              <option value="IMAGE" data-i18n="v_image"></option>
              <option value="VIDEO" data-i18n="v_video"></option>
            </select></div>
          <div class="field"><label><span data-i18n="l_album"></span><button type="button" class="i" data-help="hf-h_fromAlbum-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_fromAlbum-1" data-flag="--from-album-name" data-perms="" data-i18n-html="h_fromAlbum" style="margin-bottom:8px"></div>
            <input type="text" id="fromAlbum" placeholder="--from-album-name"></div>
        </div>
        <div class="cols">
          <div class="field"><label><span data-i18n="l_incext"></span><button type="button" class="i" data-help="hf-h_extensions-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_extensions-1" data-flag="--include-extensions" data-perms="" data-i18n-html="h_extensions" style="margin-bottom:8px"></div>
            <input type="text" id="includeExt" placeholder=".jpg,.heic"></div>
          <div class="field"><label><span data-i18n="l_excext"></span><button type="button" class="i" data-help="hf-h_extensions-2" aria-expanded="false">i</button></label><div class="help" id="hf-h_extensions-2" data-flag="--exclude-extensions" data-perms="" data-i18n-html="h_extensions" style="margin-bottom:8px"></div>
            <input type="text" id="excludeExt" placeholder=".gif,.mp"></div>
        </div>
        <div class="field"><label><span data-i18n="l_daterange"></span><button type="button" class="i" data-help="hf-h_dateRange-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_dateRange-1" data-flag="--date-range" data-perms="" data-i18n-html="h_dateRange" style="margin-bottom:8px"></div>
          <input type="text" id="dateRange" placeholder="2015-01-01,2018-12-31">
          <p class="hintline" data-i18n-html="d_daterange"></p></div>
      </div>
    </details>

    <details class="grp">
      <summary><span data-icon="checkall" data-size="17"></span><span data-i18n="g_stack"></span></summary>
      <div class="inner">
        <p class="hintline" data-i18n="d_pairs" style="margin-top:11px"></p>
        <div class="cols">
          <div class="field"><label><span data-i18n="l_rawjpeg"></span><button type="button" class="i" data-help="hf-h_pairs-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_pairs-1" data-flag="--manage-raw-jpeg" data-perms="" data-i18n-html="h_pairs" style="margin-bottom:8px"></div>
            <select id="manageRawJpeg">
              <option>NoStack</option><option>KeepRaw</option><option>KeepJPG</option>
              <option>StackCoverRaw</option><option>StackCoverJPG</option>
            </select></div>
          <div class="field"><label><span data-i18n="l_heicjpeg"></span><button type="button" class="i" data-help="hf-h_pairs-2" aria-expanded="false">i</button></label><div class="help" id="hf-h_pairs-2" data-flag="--manage-heic-jpeg" data-perms="" data-i18n-html="h_pairs" style="margin-bottom:8px"></div>
            <select id="manageHeicJpeg">
              <option>NoStack</option><option>KeepHeic</option><option>KeepJPG</option>
              <option>StackCoverHeic</option><option>StackCoverJPG</option>
            </select></div>
        </div>
        <div class="field"><label><span data-i18n="l_burst"></span><button type="button" class="i" data-help="hf-h_burst-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_burst-1" data-flag="--manage-burst" data-perms="" data-i18n-html="h_burst" style="margin-bottom:8px"></div>
          <select id="manageBurst">
            <option>NoStack</option><option>Stack</option><option>StackKeepRaw</option><option>StackKeepJPEG</option>
          </select>
          <p class="hintline" data-i18n="d_burst"></p></div>
        <div class="opt"><input type="checkbox" id="epsonFastFoto">
          <div class="body">
            <div class="head"><label class="t" for="epsonFastFoto" data-i18n="o_epson"></label><button type="button" class="i" data-help="hp-epsonFastFoto" aria-expanded="false">i</button></div>
            <div class="help" id="hp-epsonFastFoto" data-flag="--manage-epson-fastfoto" data-perms="" data-i18n-html="h_epson"></div>
          </div></div>
      </div>
    </details>

    <details class="grp">
      <summary><span data-icon="folder" data-size="17"></span><span data-i18n="g_tags"></span></summary>
      <div class="inner">
        <div class="opt"><input type="checkbox" id="sessionTag">
          <div class="body">
            <div class="head"><label class="t" for="sessionTag" data-i18n="o_session"></label><button type="button" class="i" data-help="hp-sessionTag" aria-expanded="false">i</button></div>
            <div class="help" id="hp-sessionTag" data-flag="--session-tag" data-perms="tag.create,tag.asset" data-i18n-html="h_sessionTag"></div>
          </div></div>
        <div class="field" style="margin-top:13px"><label><span data-i18n="l_tags"></span><button type="button" class="i" data-help="hf-h_tags-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_tags-1" data-flag="--tag" data-perms="tag.create,tag.asset" data-i18n-html="h_tags" style="margin-bottom:8px"></div>
          <input type="text" id="tags" placeholder="migration/2015, family">
          <p class="hintline" data-i18n-html="d_tags"></p></div>
        <div class="field"><label><span data-i18n="l_partneralbum"></span><button type="button" class="i" data-help="hf-h_partnerAlbum-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_partnerAlbum-1" data-flag="--partner-shared-album" data-perms="" data-i18n-html="h_partnerAlbum" style="margin-bottom:8px"></div>
          <input type="text" id="partnerAlbum" placeholder="--partner-shared-album"></div>
      </div>
    </details>

    <details class="grp">
      <summary><span data-icon="tune" data-size="17"></span><span data-i18n="g_perf"></span></summary>
      <div class="inner">
        <div class="cols" style="margin-top:13px">
          <div class="field"><label><span data-i18n="l_conc"></span><button type="button" class="i" data-help="hf-h_concurrent-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_concurrent-1" data-flag="--concurrent-tasks" data-perms="" data-i18n-html="h_concurrent" style="margin-bottom:8px"></div>
            <input type="number" id="concurrent" value="6" min="1" max="20">
            <p class="hintline" data-i18n-html="d_conc"></p></div>
          <div class="field"><label><span data-i18n="l_timeout"></span><button type="button" class="i" data-help="hf-h_clientTimeout-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_clientTimeout-1" data-flag="--client-timeout" data-perms="" data-i18n-html="h_clientTimeout" style="margin-bottom:8px"></div>
            <input type="text" id="clientTimeout" placeholder="20m">
            <p class="hintline" data-i18n-html="d_timeout"></p></div>
        </div>
        <div class="field"><label><span data-i18n="l_tz"></span><button type="button" class="i" data-help="hf-h_timeZone-1" aria-expanded="false">i</button></label><div class="help" id="hf-h_timeZone-1" data-flag="--time-zone" data-perms="" data-i18n-html="h_timeZone" style="margin-bottom:8px"></div>
          <input type="text" id="timeZone" placeholder="Asia/Riyadh"></div>
        <div class="opt"><input type="checkbox" id="overwrite">
          <div class="body">
            <div class="head"><label class="t" for="overwrite" data-i18n="o_overwrite"></label><span class="risk danger" data-i18n="r_danger"></span><button type="button" class="i" data-help="hp-overwrite" aria-expanded="false">i</button></div>
            <div class="help" id="hp-overwrite" data-flag="--overwrite" data-perms="asset.delete" data-i18n-html="h_overwrite"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="skipVerifySSL">
          <div class="body">
            <div class="head"><label class="t" for="skipVerifySSL" data-i18n="o_ssl"></label><span class="risk privacy" data-i18n="r_privacy"></span><button type="button" class="i" data-help="hp-skipVerifySSL" aria-expanded="false">i</button></div>
            <div class="help" id="hp-skipVerifySSL" data-flag="--skip-verify-ssl" data-perms="" data-i18n-html="h_skipVerifySSL"></div>
          </div></div>
        <div class="opt"><input type="checkbox" id="apiTrace">
          <div class="body">
            <div class="head"><label class="t" for="apiTrace" data-i18n="o_trace"></label><span class="risk privacy" data-i18n="r_privacy"></span><button type="button" class="i" data-help="hp-apiTrace" aria-expanded="false">i</button></div>
            <div class="help" id="hp-apiTrace" data-flag="--api-trace" data-perms="" data-i18n-html="h_apiTrace"></div>
          </div></div>
      </div>
    </details>
  </section>

  <section class="step" id="s4">
    <div class="shead"><span class="num" data-icon="upload" data-size="20"></span><h2 data-i18n="s4"></h2><span class="sub" id="runState"></span>
      <button type="button" class="i" data-help="h-run" aria-expanded="false">i</button></div>
    <div class="help" id="h-run" data-i18n-html="d_run" style="margin:0 0 14px"></div>
    <div class="bar">
      <button type="button" class="go" id="dryBtn" onclick="start(true)"><span data-icon="flask" data-size="17"></span><span data-i18n="b_dry"></span></button>
      <button type="button" class="go" id="startBtn" onclick="start(false)"><span data-icon="upload" data-size="17"></span><span data-i18n="b_start"></span></button>
      <button type="button" class="stop" id="stopBtn" onclick="stop()" disabled><span data-icon="stop" data-size="17"></span><span data-i18n="b_stop"></span></button>
    </div>
    <div id="err"></div>
    <div id="log" data-i18n="log_wait"></div>
  </section>

  <footer>
    <span data-i18n="foot"></span> ·
    <a href="https://github.com/simulot/immich-go" target="_blank" rel="noopener">immich-go</a> ·
    <a href="https://immich.app" target="_blank" rel="noopener">immich</a>
  </footer>
</div>
<script>/* Material Design Icons, the same set Immich uses (@mdi/js v7).
   Inlined so the tool stays dependency-free and works offline. */
var ICON={"server":"M13,19H14A1,1 0 0,1 15,20H22V22H15A1,1 0 0,1 14,23H10A1,1 0 0,1 9,22H2V20H9A1,1 0 0,1 10,19H11V17H4A1,1 0 0,1 3,16V12A1,1 0 0,1 4,11H20A1,1 0 0,1 21,12V16A1,1 0 0,1 20,17H13V19M4,3H20A1,1 0 0,1 21,4V8A1,1 0 0,1 20,9H4A1,1 0 0,1 3,8V4A1,1 0 0,1 4,3M9,7H10V5H9V7M9,15H10V13H9V15M5,5V7H7V5H5M5,13V15H7V13H5Z","key":"M22,18V22H18V19H15V16H12L9.74,13.74C9.19,13.91 8.61,14 8,14A6,6 0 0,1 2,8A6,6 0 0,1 8,2A6,6 0 0,1 14,8C14,8.61 13.91,9.19 13.74,9.74L22,18M7,5A2,2 0 0,0 5,7A2,2 0 0,0 7,9A2,2 0 0,0 9,7A2,2 0 0,0 7,5Z","zip":"M20 6H12L10 4H4C2.9 4 2 4.9 2 6V18C2 19.1 2.9 20 4 20H20C21.1 20 22 19.1 22 18V8C22 6.9 21.1 6 20 6M20 18H16V16H14V18H4V8H14V10H16V8H20V18M16 12V10H18V12H16M14 12H16V14H14V12M18 16H16V14H18V16Z","tune":"M8 13C6.14 13 4.59 14.28 4.14 16H2V18H4.14C4.59 19.72 6.14 21 8 21S11.41 19.72 11.86 18H22V16H11.86C11.41 14.28 9.86 13 8 13M8 19C6.9 19 6 18.1 6 17C6 15.9 6.9 15 8 15S10 15.9 10 17C10 18.1 9.1 19 8 19M19.86 6C19.41 4.28 17.86 3 16 3S12.59 4.28 12.14 6H2V8H12.14C12.59 9.72 14.14 11 16 11S19.41 9.72 19.86 8H22V6H19.86M16 9C14.9 9 14 8.1 14 7C14 5.9 14.9 5 16 5S18 5.9 18 7C18 8.1 17.1 9 16 9Z","upload":"M6.5 20Q4.22 20 2.61 18.43 1 16.85 1 14.58 1 12.63 2.17 11.1 3.35 9.57 5.25 9.15 5.88 6.85 7.75 5.43 9.63 4 12 4 14.93 4 16.96 6.04 19 8.07 19 11 20.73 11.2 21.86 12.5 23 13.78 23 15.5 23 17.38 21.69 18.69 20.38 20 18.5 20H13Q12.18 20 11.59 19.41 11 18.83 11 18V12.85L9.4 14.4L8 13L12 9L16 13L14.6 14.4L13 12.85V18H18.5Q19.55 18 20.27 17.27 21 16.55 21 15.5 21 14.45 20.27 13.73 19.55 13 18.5 13H17V11Q17 8.93 15.54 7.46 14.08 6 12 6 9.93 6 8.46 7.46 7 8.93 7 11H6.5Q5.05 11 4.03 12.03 3 13.05 3 14.5 3 15.95 4.03 17 5.05 18 6.5 18H9V20M12 13Z","ok":"M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z","alert":"M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z","info":"M11,9H13V7H11M12,20C7.59,20 4,16.41 4,12C4,7.59 7.59,4 12,4C16.41,4 20,7.59 20,12C20,16.41 16.41,20 12,20M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M11,17H13V11H11V17Z","sun":"M3.55 19.09L4.96 20.5L6.76 18.71L5.34 17.29M12 6C8.69 6 6 8.69 6 12S8.69 18 12 18 18 15.31 18 12C18 8.68 15.31 6 12 6M20 13H23V11H20M17.24 18.71L19.04 20.5L20.45 19.09L18.66 17.29M20.45 5L19.04 3.6L17.24 5.39L18.66 6.81M13 1H11V4H13M6.76 5.39L4.96 3.6L3.55 5L5.34 6.81L6.76 5.39M1 13H4V11H1M13 20H11V23H13","moon":"M17.75,4.09L15.22,6.03L16.13,9.09L13.5,7.28L10.87,9.09L11.78,6.03L9.25,4.09L12.44,4L13.5,1L14.56,4L17.75,4.09M21.25,11L19.61,12.25L20.2,14.23L18.5,13.06L16.8,14.23L17.39,12.25L15.75,11L17.81,10.95L18.5,9L19.19,10.95L21.25,11M18.97,15.95C19.8,15.87 20.69,17.05 20.16,17.8C19.84,18.25 19.5,18.67 19.08,19.07C15.17,23 8.84,23 4.94,19.07C1.03,15.17 1.03,8.83 4.94,4.93C5.34,4.53 5.76,4.17 6.21,3.85C6.96,3.32 8.14,4.21 8.06,5.04C7.79,7.9 8.75,10.87 10.95,13.06C13.14,15.26 16.1,16.22 18.97,15.95M17.33,17.97C14.5,17.81 11.7,16.64 9.53,14.5C7.36,12.31 6.2,9.5 6.04,6.68C3.23,9.82 3.34,14.64 6.35,17.66C9.37,20.67 14.19,20.78 17.33,17.97Z","auto":"M7.5,2C5.71,3.15 4.5,5.18 4.5,7.5C4.5,9.82 5.71,11.85 7.53,13C4.46,13 2,10.54 2,7.5A5.5,5.5 0 0,1 7.5,2M19.07,3.5L20.5,4.93L4.93,20.5L3.5,19.07L19.07,3.5M12.89,5.93L11.41,5L9.97,6L10.39,4.3L9,3.24L10.75,3.12L11.33,1.47L12,3.1L13.73,3.13L12.38,4.26L12.89,5.93M9.59,9.54L8.43,8.81L7.31,9.59L7.65,8.27L6.56,7.44L7.92,7.35L8.37,6.06L8.88,7.33L10.24,7.36L9.19,8.23L9.59,9.54M19,13.5A5.5,5.5 0 0,1 13.5,19C12.28,19 11.15,18.6 10.24,17.93L17.93,10.24C18.6,11.15 19,12.28 19,13.5M14.6,20.08L17.37,18.93L17.13,22.28L14.6,20.08M18.93,17.38L20.08,14.61L22.28,17.15L18.93,17.38M20.08,12.42L18.94,9.64L22.28,9.88L20.08,12.42M9.63,18.93L12.4,20.08L9.87,22.27L9.63,18.93Z","lang":"M12.87,15.07L10.33,12.56L10.36,12.53C12.1,10.59 13.34,8.36 14.07,6H17V4H10V2H8V4H1V6H12.17C11.5,7.92 10.44,9.75 9,11.35C8.07,10.32 7.3,9.19 6.69,8H4.69C5.42,9.63 6.42,11.17 7.67,12.56L2.58,17.58L4,19L9,14L12.11,17.11L12.87,15.07M18.5,10H16.5L12,22H14L15.12,19H19.87L21,22H23L18.5,10M15.88,17L17.5,12.67L19.12,17H15.88Z","refresh":"M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z","up":"M22 8V13.81C21.39 13.46 20.72 13.22 20 13.09V8H4V18H13.09C13.04 18.33 13 18.66 13 19C13 19.34 13.04 19.67 13.09 20H4C2.9 20 2 19.11 2 18V6C2 4.89 2.89 4 4 4H10L12 6H20C21.1 6 22 6.89 22 8M16 18H18V22H20V18H22L19 15L16 18Z","folder":"M20,18H4V8H20M20,6H12L10,4H4C2.89,4 2,4.89 2,6V18A2,2 0 0,0 4,20H20A2,2 0 0,0 22,18V8C22,6.89 21.1,6 20,6Z","disk":"M6,2H18A2,2 0 0,1 20,4V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V4A2,2 0 0,1 6,2M12,4A6,6 0 0,0 6,10C6,13.31 8.69,16 12.1,16L11.22,13.77C10.95,13.29 11.11,12.68 11.59,12.4L12.45,11.9C12.93,11.63 13.54,11.79 13.82,12.27L15.74,14.69C17.12,13.59 18,11.9 18,10A6,6 0 0,0 12,4M12,9A1,1 0 0,1 13,10A1,1 0 0,1 12,11A1,1 0 0,1 11,10A1,1 0 0,1 12,9M7,18A1,1 0 0,0 6,19A1,1 0 0,0 7,20A1,1 0 0,0 8,19A1,1 0 0,0 7,18M12.09,13.27L14.58,19.58L17.17,18.08L12.95,12.77L12.09,13.27Z","usb":"M8 15C8.55 15 9 15.45 9 16C9 16.55 8.55 17 8 17C7.45 17 7 16.55 7 16C7 15.45 7.45 15 8 15M15.07 4.69L16.5 6.1L15.07 7.5L13.66 6.1L15.07 4.69M17.9 7.5L19.31 8.93L17.9 10.34L16.5 8.93L17.9 7.5M8 13C6.34 13 5 14.34 5 16C5 17.66 6.34 19 8 19C9.66 19 11 17.66 11 16C11 14.34 9.66 13 8 13M9.77 4.33L10.5 5.08L14.29 1.29C14.47 1.11 14.72 1 15 1C15.28 1 15.53 1.11 15.71 1.29L22.78 8.36L22.78 8.37C22.92 8.54 23 8.76 23 9C23 9.3 22.87 9.57 22.66 9.76L22.66 9.76L18.93 13.5L19.67 14.23L12.95 20.95C11.68 22.22 9.93 23 8 23C4.13 23 1 19.87 1 16C1 14.07 1.78 12.32 3.05 11.05L9.77 4.33M20.59 9L15 3.41L11.93 6.5L17.5 12.08L20.59 9Z","lock":"M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17M18,8A2,2 0 0,1 20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1A5,5 0 0,1 17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z","stop":"M18,18H6V6H18V18Z","flask":"M5,19A1,1 0 0,0 6,20H18A1,1 0 0,0 19,19C19,18.79 18.93,18.59 18.82,18.43L13,8.35V4H11V8.35L5.18,18.43C5.07,18.59 5,18.79 5,19M6,22A3,3 0 0,1 3,19C3,18.4 3.18,17.84 3.5,17.37L9,7.81V6A1,1 0 0,1 8,5V4A2,2 0 0,1 10,2H14A2,2 0 0,1 16,4V5A1,1 0 0,1 15,6V7.81L20.5,17.37C20.82,17.84 21,18.4 21,19A3,3 0 0,1 18,22H6M13,16L14.34,14.66L16.27,18H7.73L10.39,13.39L13,16M12.5,12A0.5,0.5 0 0,1 13,12.5A0.5,0.5 0 0,1 12.5,13A0.5,0.5 0 0,1 12,12.5A0.5,0.5 0 0,1 12.5,12Z","checkall":"M0.41,13.41L6,19L7.41,17.58L1.83,12M22.24,5.58L11.66,16.17L7.5,12L6.07,13.41L11.66,19L23.66,7M18,7L16.59,5.58L10.24,11.93L11.66,13.34L18,7Z","clear":"M20 20V17H22V20C22 21.11 21.1 22 20 22H17V20H20M2 20V17H4V20H7V22H4C2.9 22 2 21.1 2 20M10 20H14V22H10V20M14.59 8L12 10.59L9.41 8L8 9.41L10.59 12L8 14.59L9.41 16L12 13.41L14.59 16L16 14.59L13.41 12L16 9.41L14.59 8M20 10H22V14H20V10M2 10H4V14H2V10M2 4C2 2.89 2.9 2 4 2H7V4H4V7H2V4M22 4V7H20V4H17V2H20C21.1 2 22 2.9 22 4M10 2H14V4H10V2Z","shield":"M21,11C21,16.55 17.16,21.74 12,23C6.84,21.74 3,16.55 3,11V5L12,1L21,5V11M12,21C15.75,20 19,15.54 19,11.22V6.3L12,3.18L5,6.3V11.22C5,15.54 8.25,20 12,21M10,17L6,13L7.41,11.59L10,14.17L16.59,7.58L18,9","copy":"M19,21H8V7H19M19,5H8A2,2 0 0,0 6,7V21A2,2 0 0,0 8,23H19A2,2 0 0,0 21,21V7A2,2 0 0,0 19,5M16,1H4A2,2 0 0,0 2,3V17H4V3H16V1Z","check":"M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z"};
function icon(n,s){var d=ICON[n];if(!d){return "";}s=s||18;
  return '<svg class="ic" viewBox="0 0 24 24" width="'+s+'" height="'+s+'" aria-hidden="true"><path d="'+d+'"/></svg>';}
function paintIcons(root){(root||document).querySelectorAll("[data-icon]").forEach(function(el){
  el.innerHTML=icon(el.getAttribute("data-icon"),+el.getAttribute("data-size")||18);});}

/* Interface strings. To add a language, copy the English block, translate the
   values, keep every key, and add the code plus its native name to LANGS. */
var LANGS=[["en", "English"], ["ar", "العربية"], ["es", "Español"], ["fr", "Français"], ["de", "Deutsch"], ["pt", "Português"], ["zh", "中文"]];
var RTL=["ar"];
var TR={"ar": {"b_all": "اختر الكل", "b_dry": "جرّب بدون رفع", "b_install": "ثبّت immich-go", "b_none": "ألغِ الكل", "b_refresh": "تحديث", "b_start": "ابدأ الرفع", "b_stop": "أوقف", "b_test": "اختبر الاتصال", "b_up": "للأعلى", "b_usedir": "استخدم هذا المجلد", "b_verify": "افحص السلامة", "copied": "نُسخ", "copy_hint": "اضغط أي صلاحية لنسخها", "d_admin": "يوقف مهام Immich الخلفية أثناء الرفع فيصير أسرع على الأجهزة المتواضعة. يحتاج حساب أدمن.", "d_burst": "وش يصير للسلسلة المتلاحقة.", "d_conc": "كم صورة ترفع في نفس الوقت. <b>6</b> مناسبة لأغلب الحالات، و<b>2–4</b> لو الشبكة ضعيفة أو السيرفر جهاز صغير.", "d_daterange": "تاريخان مفصولان بفاصلة.", "d_files": "اختر كل أجزاء الأرشيف دفعة وحدة، وافحص سلامتها قبل ما تبدأ.", "d_key": "أنشئه من إعدادات حسابك في Immich. تسع صلاحيات تكفي — ما تحتاج تعطيه كل شي.", "d_pairs": "وش يصير لما تكون نفس اللقطة موجودة بصيغتين.", "d_perms": "في Immich افتح <span class='path'>Account Settings → API Keys → New API Key</span> ثم استخدم خانة البحث وأشّر على هذي:<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>زد <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> و <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> لو تبغى وسوم الأشخاص. ولا تختر «تحديد الكل»، لأنها تعطي صلاحية المسح وإنشاء مفاتيح جديدة.</p>", "d_preset": "يطبّق الإعدادات اللي يوصي بها immich-go لهذا الحجم.", "d_run": "ابدأ بتجربة بدون رفع — تكشف نقص الصلاحيات والملفات التالفة خلال دقائق بدل ساعات.", "d_server": "نفس العنوان اللي تفتح فيه Immich من المتصفح، مع المنفذ. الافتراضي <code>2283</code>.", "d_tags": "مفصولة بفواصل. استخدم شرطة مائلة للتدرّج، مثل <code>migration/2015</code>.", "d_timeout": "زدها لو الاتصال بطيء. تقبل <code>30m</code> و <code>2h</code>.", "d_why": "<p>جوجل يقسّم الأرشيف بالحجم لا بالمنطق، فالصورة تطلع في جزء وملف الـ JSON حقها — اللي فيه التاريخ والموقع والألبوم — في جزء ثاني. لو رفعتها جزء جزء، الصور اللي ما لقت ملفاتها تنرفض ولا ترتفع أصلاً.</p><p><b>فحص السلامة</b> يقرأ فهرس كل أرشيف، وهذا سريع حتى لو الملف 50 جيجا. يكشف الملفات الناقصة أو اللي نزّل مكانها جوجل صفحة خطأ.</p>", "disks": "الأقراص:", "e_already_running": "في عملية شغّالة", "e_asset_not_found": "الإصدار ما فيه {arg}", "e_bad_data": "بيانات غير صالحة", "e_binary_not_in_archive": "ما فيه ملف immich-go داخل {arg}", "e_install_failed": "فشل التثبيت: {arg}", "e_missing_file": "ملف غير موجود: <bdi>{arg}</bdi>", "e_no_binary": "ما لقيت أمر immich-go على هذا الجهاز", "e_no_key": "اكتب مفتاح API", "e_no_server": "اكتب عنوان السيرفر", "e_no_zips": "اختر ملف واحد على الأقل", "e_none": "ما فيه مجلدات ولا ملفات zip هنا", "e_path_missing": "المسار غير موجود: <bdi>{arg}</bdi>", "e_pick": "اختر قرصاً من فوق", "e_read_denied": "تعذّر فتح المجلد: <bdi>{arg}</bdi>", "e_read_denied_mac": "تعذّر فتح المجلد: <bdi>{arg}</bdi><br>السبب أن macOS يمنع التيرمنال من قراءة هذا القرص. افتح <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> وفعّل <bdi>Terminal</bdi>، ثم أعد تشغيله.", "e_release_lookup_failed": "ما وصلنا لـ GitHub: {arg}", "e_unsupported_platform": "ما فيه إصدار immich-go لهذي المنصة ({arg})", "folder_hint": "اختر المجلد اللي فيه مجلد <code>Takeout</code> بعد فك الضغط. فحص السلامة ينطبق على ملفات zip فقط.", "foot": "أداة محلية تعمل على جهازك فقط", "free": "فاضي", "g_content": "المحتوى", "g_perf": "الأداء والاتصال", "g_stack": "التجميع", "g_tags": "الألبومات والوسوم", "h_apiTrace": "<b>الافتراضي: مطفي.</b> يكتب تفاصيل كل نداء API في ملف السجل.<p>للتشخيص أو لفتح بلاغ مشكلة. يكبّر السجل كثيراً ويسجّل محتوى الطلبات، وقد يتضمن أسماء ملفات وتفاصيل شخصية.</p><p>المفتاح نفسه مخفي في هذي الواجهة، لكن تعامل مع سجل التتبع كملف حساس وراجعه قبل ما تشاركه.</p>", "h_burst": "<b>الافتراضي: NoStack.</b> يتحكم بالسلسلة المتلاحقة، لما ضغطة وحدة تنتج لقطات كثيرة.<p><b>Stack</b> يحتفظ بكل اللقطات لكن يجمعها تحت غلاف واحد، فتاخذ السلسلة مكاناً واحداً في الخط الزمني بدل ثلاثين.</p><p><b>StackKeepRaw</b> و <b>StackKeepJPEG</b> يهملان الصيغة الأخرى داخل السلسلة كمان. هذولا يحذفان ملفات؛ أما <b>Stack</b> العادي فلا.</p>", "h_clientTimeout": "<b>الافتراضي: 20m.</b> كم ننتظر الطلب الواحد قبل ما نتخلى عنه.<p>زدها لو الفيديوهات الكبيرة تفشل على اتصال بطيء: <code>60m</code> أو <code>2h</code>. وما تكلفك شي وقت السلامة، لأن المهلة ما تنطبق إلا على طلب عالق أصلاً.</p>", "h_concurrent": "<b>الافتراضي: 6.</b> كم صورة ترتفع في نفس الوقت.<p>الزيادة تسرّع فقط طالما الشبكة والسيرفر قادرين. وبعد تلك النقطة تبدأ الطلبات تنتهي مهلتها والرفع يصير <i>أبطأ</i>.</p><p><b>2–4</b> لاتصال ضعيف أو راسبيري باي، و<b>6–8</b> للاستخدام العادي، و<b>12–16</b> فقط على شبكة سلكية وسيرفر قوي. تغييرها آمن وتقدر تعيد المحاولة — ما يتلف شي لو أخطأت.</p>", "h_continueOnError": "<b>الافتراضي: مفعّل.</b> الملف اللي يفشل يُسجَّل ويُتخطى، والرفع يكمّل.<p><b>لو أطفيته</b> أول ملف خربان يوقف كل شي. وفي أرشيف فيه عشرات آلاف الصور، ملف واحد تالف ينهي رفعة صار لها ساعات.</p><p>ما يختفي شي في الحالتين — كل فشل يظهر في السجل.</p>", "h_dateRange": "<b>الافتراضي: فاضي (كل التواريخ).</b> يرفع الصور المأخوذة داخل النطاق فقط.<p>تاريخان مفصولان بفاصلة، مثل <code>2015-01-01,2018-12-31</code>. التاريخ يجي من الملف الجانبي، فالصور اللي بلا ملف يتحكم فيها الخيار اللي فوق.</p><p>هذي أنظف طريقة لتقسيم مكتبة ضخمة على عدة رفعات حسب السنة.</p>", "h_epson": "<b>الافتراضي: مطفي.</b> يتعرّف على نمط تسمية ماسح Epson FastFoto ويجمّع كل مسح مع نسخته المصحّحة وظهر الصورة.<p>فعّله فقط لو أرشيفك فعلاً فيه مسوحات FastFoto. وما له أي أثر غير كذا.</p>", "h_extensions": "<b>الافتراضي: فاضي (كل الأنواع).</b> يحصر حسب امتداد الملف.<p><b>هذي فقط</b> ما ترفع غيرها. و<b>تجاهل هذي</b> ترفع كل شي ما عداها. اكتبها مفصولة بفواصل مع النقطة، مثل <code>.jpg,.heic</code>.</p><p>مفيد لاستثناء <code>.gif</code> أو ملفات الصور المتحركة <code>.mp</code> حق جوجل.</p>", "h_fromAlbum": "<b>الافتراضي: فاضي (كل شي).</b> يرفع صور ألبوم واحد باسمه فقط.<p>اكتب اسم الألبوم مثل ما هو في Google Photos بالضبط. ممتاز لتجربة كل المسار على ألبوم صغير قبل ما تلتزم بالمكتبة كاملة.</p>", "h_includeArchived": "<b>الافتراضي: مفعّل.</b> الصور اللي أرشفتها في Google Photos ترتفع كمان.<p>الأرشفة معناها مخفية من الخط الزمني، مو محذوفة. وتوصل Immich كصور عادية.</p><p><b>لو أطفيته</b> تُتخطى تماماً — يعني ما بتكون موجودة في Immich أصلاً.</p>", "h_includePartner": "<b>الافتراضي: مفعّل.</b> يرفع الصور اللي شاركها معك شريكك عبر ميزة المشاركة في جوجل.<p>هذي صور شريكك اللي جوجل شاركها في مكتبتك، وبترتفع في حساب Immich <i>حقك أنت</i>.</p><p>أطفيه لو تبغى مكتبتك تكون صورك أنت فقط.</p>", "h_includeTrashed": "<b>الافتراضي: مطفي.</b> يرفع الصور الموجودة في سلة محذوفات Google Photos.<p><b>هذي صور أنت اخترت تحذفها.</b> تفعيله يرجّعها لمكتبتك مخلوطة مع الباقي.</p><p>آمن بمعنى أنه ما يتلف شي — لكن قد ترجع صوراً تخلصت منها بقصد، وإيجادها بعدين يعني تفتيش في كل ما رفعته.</p>", "h_includeType": "<b>الافتراضي: الكل.</b> يحصر الرفع في الصور فقط أو الفيديو فقط.<p>يفيد لتقسيم أرشيف ضخم على مرحلتين، أو لما الفيديوهات تملأ السيرفر. المستثنى ببساطة ما يرتفع، وتقدر تشغّل مرة ثانية بالإعداد الآخر.</p>", "h_includeUnmatched": "<b>الافتراضي: مطفي.</b> يرفع الصور اللي ما لها ملف JSON مطابق في الأرشيف.<p>عادةً كل صورة لها ملف جانبي فيه تاريخها وموقعها وألبومها. ولما يكون مفقوداً، immich-go يتخطى الصورة بدل ما يرفعها ببيانات غلط.</p><p><b>لو فعّلته</b> ترتفع تلك الصور <b>بلا تاريخ ولا موقع ولا ألبوم</b>. يرجع immich-go لبيانات EXIF أو اسم الملف، وغالباً تطلع غلط — توقّع صوراً مؤرخة بيوم رفعك لها.</p><p>ما تستخدمه إلا لو التجربة بدون رفع بيّنت إن ملفات كثيرة تُتخطى. والأفضل غالباً إصلاح الأرشيف: الملفات الجانبية كثيراً ما تكون في جزء zip ثاني، فتأكد أول إنك حددتها كلها.</p>", "h_includeUntitled": "<b>الافتراضي: مطفي.</b> جوجل يحط الألبومات اللي ما سمّيتها في مجلدات اسمها <i>Untitled</i> أو <i>بدون عنوان</i>.<p>المشكلة أن جوجل قد يدمج عدة ألبومات بلا اسم في مجلد واحد، فما يمكن استرجاع التقسيم الأصلي. تفعيله قد ينتج ألبوماً كبيراً يخلط مجموعات ما لها علاقة ببعض.</p><p>الصور نفسها ترتفع في الحالتين؛ الخيار يتحكم بإعادة بناء تلك الألبومات فقط.</p>", "h_overwrite": "<b>الافتراضي: مطفي. وهذا الخيار الوحيد اللي يحذف بيانات.</b><p>عادةً الصورة الموجودة على السيرفر تُعرَف وتُتخطى، فإعادة التشغيل ما تضر. ومع تفعيله، immich-go <b>يحذف نسخة السيرفر ويرفع الملف المحلي مكانها</b>.</p><p>يحتاج صلاحية <code>asset.delete</code> في مفتاحك. وإعطاء هذي الصلاحية معناه أن المفتاح يقدر يحذف صوراً من مكتبتك نهائياً — فلا تضيفها للمفتاح إلا إذا كنت محتاجها تحديداً.</p><p>وأي شي أضافه Immich بعد الرفع الأول — تعديل، ألبوم يدوي، تفضيل — كله تابع للنسخة اللي بتُحذف ويروح معها. استخدمه فقط لو كنت متعمّداً استبدال ملفات بنسخ أفضل، وخذ نسخة احتياطية قبل.</p>", "h_pairs": "<b>الافتراضي: NoStack — الملفان يرتفعان منفصلين.</b> يتحكم بما يصير لما تكون نفس اللقطة موجودة مرتين، RAW و JPEG أو HEIC و JPEG.<p><b>KeepRaw</b> و <b>KeepJPG</b> ترفع صيغة واحدة وتهمل الثانية — والمهملة ما توصل Immich أبداً.</p><p><b>StackCover…</b> ترفع الاثنين وتجمّعهما في Immich وتظهر الصيغة المذكورة في الأعلى. ما يضيع شي والخط الزمني يبقى مرتباً. هذا الخيار الآمن لو تبغى التجميع.</p>", "h_partnerAlbum": "<b>الافتراضي: فاضي.</b> يحط الصور الجاية من مشاركة الشريك في ألبوم باسم تختاره.<p>طريقة مرتبة تخلي صور الشريك مميّزة بدل ما تكون متناثرة في الخط الزمني. يؤثر على صور الشريك فقط، وبشرط أن رفعها مفعّل.</p>", "h_peopleTag": "<b>الافتراضي: مفعّل.</b> جوجل يحفظ أسماء الأشخاص اللي وسمتهم داخل ملف JSON لكل صورة، وهذا الخيار ينقلها كوسوم في Immich باسم <code>people/الاسم</code>.<p>هذي وسوم، مو التعرّف على الوجوه. Immich يكتشف الوجوه بنفسه على أي حال؛ الخيار هذا بس يحفظ الأسماء اللي جوجل يعرفها أصلاً.</p><p>أطفيه لو مفتاحك ما فيه الصلاحيات المذكورة تحت.</p>", "h_preset": "<b>الافتراضي: مخصص.</b> يعبّي عدد العمليات والمهلة والتجميع بالقيم اللي يوصي بها دليل immich-go لمكتبة بهذا الحجم.<p>يضبط تلك الحقول فقط — تقدر تغيّر أي شي بعدها، وأول ما تغيّر يرجع الاختيار إلى «مخصص». وما فيه شي هنا يؤثر على أي صور تُرفع.</p>", "h_sessionTag": "<b>الافتراضي: مطفي.</b> يوسم كل ما رُفع في هذه التشغيلة بـ <code>{immich-go}/التاريخ والوقت</code>.<p>الفايدة قابلية التراجع: لو راحت الرفعة غلط، الوسم يحدد لك بالضبط اللي رفعته فتراجعه أو تحذفه، بدون ما تمس شيئاً رفعته سابقاً.</p><p>يستاهل التفعيل في أول محاولة على مكتبة كبيرة.</p>", "h_skipVerifySSL": "<b>الافتراضي: مطفي.</b> يوقف التحقق من صلاحية شهادة HTTPS للسيرفر.<p>فعّله فقط لشهادة موقّعة ذاتياً على شبكة أنت تتحكم فيها. لأنه يعطّل الفحص اللي يثبت إنك تكلّم سيرفرك أنت، فعلى أي شبكة غير موثوقة يقدر أحد يعترض الاتصال في المنتصف — <b>ويأخذ مفتاحك</b>.</p><p>وما تحتاجه أصلاً لعنوان <code>http://</code> عادي في شبكتك المحلية.</p>", "h_syncAlbums": "<b>الافتراضي: مفعّل.</b> يعيد إنشاء كل ألبوم موجود في أرشيفك داخل Immich، ويرجّع الصور له.<p><b>لو أطفيته</b> الصور ترتفع عادي، بس تنزل في الخط الزمني بدون ألبومات. ما يضيع شي — تقدر تبني الألبومات يدوياً بعدين.</p><p>وبدون الصلاحيات المذكورة تحت، الرفع يوقف بخطأ 403.</p>", "h_tags": "<b>الافتراضي: فاضي.</b> يضيف وسومك أنت لكل صورة في هذه التشغيلة.<p>مفصولة بفواصل. والشرطة المائلة تصنع تدرّجاً، فـ <code>migration/2015</code> ينشئ <i>2015</i> داخل <i>migration</i>.</p><p>إضافة بحتة — ما يغيّر الصور أبداً، بس يعنونها.</p>", "h_takeoutTag": "<b>الافتراضي: مفعّل.</b> يضيف وسماً واحداً <code>{takeout}/takeout-التاريخ</code> لكل صور هذا التصدير.<p>فايدته تظهر بعدين: يخليك تلقى أو تحذف بالضبط اللي جاء من هذا الأرشيف، وهذا مهم لو رفعت أكثر من تصدير مع الوقت.</p><p>يحتاج الصلاحيات المذكورة تحت.</p>", "h_timeZone": "<b>الافتراضي: المنطقة الزمنية لجهازك.</b> تُستخدم لما يكون تاريخ الصورة بدون منطقة زمنية.<p>اضبطها لو رفعت من جهاز في منطقة مختلفة وطلعت الصور بفارق ساعات. الصيغة <code>المنطقة/المدينة</code>، مثل <code>Asia/Riyadh</code>.</p>", "home_label": "المجلد الرئيسي", "install_hint": "ينزّل الإصدار المناسب لهذا الجهاز في ~/.local/bin", "install_ok": "تم تثبيت {v}", "installing": "جاري التنزيل…", "l_admin": "مفتاح الأدمن — اختياري", "l_album": "من هذا الألبوم فقط", "l_burst": "الصور المتلاحقة", "l_conc": "عمليات متوازية", "l_daterange": "نطاق التاريخ", "l_excext": "تجاهل هذي الامتدادات", "l_heicjpeg": "أزواج HEIC + JPEG", "l_incext": "هذي الامتدادات فقط", "l_key": "مفتاح API", "l_partneralbum": "ضع صور الشريك في ألبوم", "l_preset": "إعداد جاهز", "l_rawjpeg": "أزواج RAW + JPEG", "l_server": "عنوان Immich", "l_tags": "وسوم إضافية", "l_timeout": "مهلة الطلب", "l_type": "نوع الملف", "l_tz": "المنطقة الزمنية", "lede": "ينقل أرشيف صور جوجل إلى سيرفر Immich بالتواريخ والمواقع والألبومات. يقرأ ملفات zip من مكانها، بدون فك ضغط وبدون نسخ.", "log_wait": "في انتظار البدء…", "m_perms": "الصلاحيات المطلوبة", "m_why": "ليش كل الأجزاء مرة وحدة؟", "mac_block": "القرص <bdi>{n}</bdi> محجوب، لأن macOS يمنع التيرمنال من قراءة الأقراص الخارجية.<br>الحل: افتح <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> وفعّل <bdi>Terminal</bdi>، ثم أغلق التيرمنال وافتحه من جديد.", "n_accepted": "غير متوقع: الطلب قُبل", "n_failed": "فشل الفحص", "n_flag": "يضبط", "n_forbidden": "المفتاح ما عنده هذي الصلاحية", "n_needs": "يحتاج", "n_unauthorized": "المفتاح غير صالح", "o_albums": "أعد بناء ألبومات جوجل", "o_archived": "الصور المؤرشفة", "o_continue": "كمّل رغم الأخطاء", "o_continue_p": "لا يتوقف الرفع كله بسبب ملف واحد تالف", "o_epson": "مسوحات Epson FastFoto", "o_overwrite": "استبدل الصور الموجودة على السيرفر", "o_partner": "صور الشريك", "o_people": "وسوم الأشخاص من ملفات JSON", "o_session": "وسم كل صور هذه الجلسة", "o_ssl": "تخطَّ فحص الشهادة", "o_takeout": "وسم يجمع صور هذا التصدير", "o_trace": "سجّل كل نداء API", "o_trashed": "الصور المحذوفة", "o_unmatched": "الصور بلا ملف JSON", "o_untitled": "الألبومات بلا عنوان", "p_custom": "مخصص", "p_large": "أكثر من 100k صورة", "p_medium": "10k–100k صورة", "p_slow": "شبكة بطيئة أو غير مستقرة", "p_small": "أقل من 10k صورة", "ph_admin": "اتركه فاضي إذا ما عندك", "ph_key": "الصقه هنا", "pill_bin": "immich-go", "pill_conn": "الاتصال", "pill_disk": "الأقراص", "r_caution": "يغيّر ما يُرفع", "r_danger": "يحذف بيانات", "r_done": "تمّت بنجاح", "r_dry": "تجربة جارية…", "r_privacy": "خطر أمني أو على الخصوصية", "r_stop": "توقفت · رمز {c}", "r_up": "رفع جارٍ…", "s1": "السيرفر والمفتاح", "s2": "ملفات الأرشيف", "s3": "الخيارات", "s3sub": "كل خيار وصلاحيته", "s4": "التنفيذ", "sel": "{n} ملف مختار", "sel_dir": "{n} مجلد مختار", "src_folder": "مجلد", "src_zip": "ملفات ZIP", "st_avail": "متاح {n}", "st_blocked": "محجوب", "st_failed": "فشل", "st_incomplete": "ناقص", "st_missing": "غير مثبت", "st_none": "لا يوجد", "st_ok": "سليم", "st_ready": "جاهز", "st_untested": "لم يُختبر", "t_details": "التفاصيل", "t_good": "الاتصال سليم والصلاحيات المقروءة كاملة. الباقي يتأكد منه في التجربة بدون رفع.", "t_hint": "<br>تأكد من العنوان والمنفذ، غالباً 2283.", "t_missing": "ناقص {n} صلاحية. ضفها للمفتاح من Immich، أو اطفِ الخيار اللي يحتاجها في الخطوة 3.", "t_missing_short": "ناقص {m} من {n} صلاحية", "t_ok_short": "الصلاحيات {n} كاملة", "t_unreach": "<b>ما وصلنا للسيرفر.</b> ", "t_unver": "يتأكد منها في التجربة", "testing": "جاري الفحص…", "title": "ناقل الصور", "v_all": "الكل", "v_bad": "<b>{n} ملف تالف أو ناقص:</b> {names}<br>نزّلها من جديد قبل الرفع، لأنك لو رفعتها كذا بتضيع صور بصمت.", "v_checking": "جاري فحص {n} ملف…", "v_good": "<b>كل الملفات سليمة</b> — {t} عنصر داخل {f} أرشيف. جاهز للرفع.", "v_image": "صور فقط", "v_video": "فيديو فقط", "w_aadd": "إضافة الصور للألبومات", "w_about": "معرفة إصدار السيرفر", "w_acreate": "إنشاء ألبومات جوجل", "w_albums": "قراءة الألبومات", "w_overwrite": "يمسح نسخة السيرفر ويرفع من جديد. يحتاج صلاحية <code>asset.delete</code>.", "w_search": "البحث عن المكرر قبل الرفع", "w_ssl": "للشهادات الموقّعة ذاتياً على شبكة تثق فيها فقط.", "w_stats": "عدّ الصور الموجودة", "w_trace": "السجل بيحتوي تفاصيل الطلبات، وقد تتضمن بيانات شخصية.", "w_trashed": "هذي صور حذفتها من Google Photos.", "w_unmatched": "ترتفع بلا تاريخ ولا موقع ولا ألبوم — immich-go يرجع لبيانات EXIF أو اسم الملف.", "w_update": "ضبط التاريخ والموقع بعد الرفع", "w_upload": "رفع الصور", "w_user": "قراءة بيانات الحساب"}, "de": {"b_all": "alle auswählen", "b_dry": "Testlauf", "b_install": "immich-go installieren", "b_none": "Auswahl aufheben", "b_refresh": "aktualisieren", "b_start": "Upload starten", "b_stop": "Stoppen", "b_test": "Verbindung testen", "b_up": "nach oben", "b_usedir": "diesen Ordner verwenden", "b_verify": "Integrität prüfen", "copied": "kopiert", "copy_hint": "klicke eine Berechtigung an, um sie zu kopieren", "d_admin": "Pausiert die Hintergrundjobs von Immich während des Uploads, was auf schwacher Hardware deutlich hilft. Erfordert ein Admin-Konto.", "d_burst": "Was mit einer Serienaufnahme passiert.", "d_conc": "Wie viele Fotos gleichzeitig hochgeladen werden. <b>6</b> passt meistens, <b>2–4</b> bei schwachem Netz oder kleinem Server.", "d_daterange": "Zwei Daten, durch Komma getrennt.", "d_files": "Wähle alle Teile des Archivs auf einmal aus und prüfe ihre Integrität, bevor du startest.", "d_key": "Lege ihn in den Kontoeinstellungen von Immich an. Neun Berechtigungen genügen – Vollzugriff ist nicht nötig.", "d_pairs": "Was passiert, wenn dieselbe Aufnahme in zwei Formaten vorliegt.", "d_perms": "Öffne in Immich <span class='path'>Account Settings → API Keys → New API Key</span> und hake über die Suche diese an:<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>Ergänze <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> und <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> für Personen-Tags. Wähle nicht <b>Alle auswählen</b> – das gewährt Lösch- und Schlüsselrechte.</p>", "d_preset": "Übernimmt die Einstellungen, die immich-go für diese Größe empfiehlt.", "d_run": "Beginne mit einem Testlauf: Er zeigt fehlende Berechtigungen und beschädigte Archive in Minuten statt Stunden.", "d_server": "Dieselbe Adresse, mit der du Immich öffnest, samt Port. Standard ist <code>2283</code>.", "d_tags": "Durch Komma getrennt. Ein Schrägstrich erzeugt eine Hierarchie, etwa <code>migration/2015</code>.", "d_timeout": "Bei langsamer Verbindung erhöhen. Nimmt <code>30m</code>, <code>2h</code>.", "d_why": "<p>Google teilt das Archiv nach Größe statt nach Logik: Ein Foto landet in einem Teil, während seine JSON-Datei – mit Datum, Ort und Album – in einem anderen liegt. Lädt man sie einzeln hoch, werden Fotos ohne passende JSON-Datei komplett abgewiesen.</p><p>Die <b>Integritätsprüfung</b> liest das Inhaltsverzeichnis jedes Archivs und bleibt selbst bei 50 GB schnell. Sie erkennt abgeschnittene Dateien und die Fehlerseite, die Google bei abgelaufenem Link ausliefert.</p>", "disks": "Laufwerke:", "e_already_running": "Es läuft bereits ein Durchlauf", "e_asset_not_found": "Die Version enthält {arg} nicht", "e_bad_data": "Ungültige Anfrage", "e_binary_not_in_archive": "Kein immich-go-Programm in {arg}", "e_install_failed": "Installation fehlgeschlagen: {arg}", "e_missing_file": "Datei nicht gefunden: <bdi>{arg}</bdi>", "e_no_binary": "immich-go wurde auf diesem Rechner nicht gefunden", "e_no_key": "Gib den API-Schlüssel ein", "e_no_server": "Gib die Serveradresse ein", "e_no_zips": "Wähle mindestens eine Datei", "e_none": "hier gibt es keine Ordner oder ZIP-Dateien", "e_path_missing": "Pfad nicht gefunden: <bdi>{arg}</bdi>", "e_pick": "wähle oben ein Laufwerk", "e_read_denied": "Ordner konnte nicht geöffnet werden: <bdi>{arg}</bdi>", "e_read_denied_mac": "Ordner konnte nicht geöffnet werden: <bdi>{arg}</bdi><br>macOS lässt Terminal dieses Laufwerk nicht lesen. Öffne <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> und aktiviere <bdi>Terminal</bdi>, dann starte es neu.", "e_release_lookup_failed": "GitHub nicht erreichbar: {arg}", "e_unsupported_platform": "Keine immich-go-Version für diese Plattform ({arg})", "folder_hint": "Wähle den Ordner, der das bereits entpackte <code>Takeout</code>-Verzeichnis enthält. Die Integritätsprüfung gilt nur für ZIP-Dateien.", "foot": "Ein lokales Werkzeug, das nur auf deinem Rechner läuft", "free": "frei", "g_content": "Inhalt", "g_perf": "Leistung und Verbindung", "g_stack": "Gruppierung", "g_tags": "Alben und Tags", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "Persönlicher Ordner", "install_hint": "Lädt die zu diesem Rechner passende Version nach ~/.local/bin", "install_ok": "{v} installiert", "installing": "wird geladen…", "l_admin": "Admin-Schlüssel — optional", "l_album": "Nur dieses Album", "l_burst": "Serienbilder", "l_conc": "Parallele Uploads", "l_daterange": "Zeitraum", "l_excext": "Diese Endungen überspringen", "l_heicjpeg": "HEIC-+-JPEG-Paare", "l_incext": "Nur diese Endungen", "l_key": "API-Schlüssel", "l_partneralbum": "Partnerfotos in dieses Album legen", "l_preset": "Voreinstellung", "l_rawjpeg": "RAW-+-JPEG-Paare", "l_server": "Immich-Adresse", "l_tags": "Zusätzliche Tags", "l_timeout": "Zeitlimit pro Anfrage", "l_type": "Dateityp", "l_tz": "Zeitzone", "lede": "Überträgt einen Google-Photos-Takeout auf deinen Immich-Server – mit Datum, Ort und Alben. Liest die ZIP-Dateien an Ort und Stelle: ohne Entpacken, ohne Kopieren.", "log_wait": "warte auf den Start…", "m_perms": "Erforderliche Berechtigungen", "m_why": "Warum alle Teile auf einmal?", "mac_block": "<bdi>{n}</bdi> ist blockiert – macOS lässt Terminal keine externen Laufwerke lesen.<br>Lösung: Öffne <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> und aktiviere <bdi>Terminal</bdi>, dann beende und starte es neu.", "n_accepted": "unerwartet: Anfrage wurde angenommen", "n_failed": "Prüfung fehlgeschlagen", "n_flag": "setzt", "n_forbidden": "Schlüssel hat diese Berechtigung nicht", "n_needs": "braucht", "n_unauthorized": "Schlüssel ungültig", "o_albums": "Google-Alben wiederherstellen", "o_archived": "Archivierte Fotos", "o_continue": "bei Fehlern weitermachen", "o_continue_p": "eine defekte Datei stoppt nicht den gesamten Upload", "o_epson": "Epson-FastFoto-Scans", "o_overwrite": "Bereits hochgeladene Fotos ersetzen", "o_partner": "Fotos der Partnerin oder des Partners", "o_people": "Personen-Tags aus den JSON-Dateien", "o_session": "Alles aus diesem Durchlauf taggen", "o_ssl": "Zertifikatsprüfung überspringen", "o_takeout": "Tag für diesen Takeout", "o_trace": "Jeden API-Aufruf protokollieren", "o_trashed": "Gelöschte Fotos", "o_unmatched": "Fotos ohne JSON-Datei", "o_untitled": "Alben ohne Titel", "p_custom": "Eigene", "p_large": "Über 100k Fotos", "p_medium": "10k–100k Fotos", "p_slow": "Langsames oder instabiles Netz", "p_small": "Unter 10k Fotos", "ph_admin": "leer lassen, falls nicht vorhanden", "ph_key": "hier einfügen", "pill_bin": "immich-go", "pill_conn": "Verbindung", "pill_disk": "Laufwerke", "r_caution": "ändert, was importiert wird", "r_danger": "löscht Daten", "r_done": "erfolgreich beendet", "r_dry": "Testlauf läuft…", "r_privacy": "Sicherheits- oder Datenschutzrisiko", "r_stop": "gestoppt · Code {c}", "r_up": "Upload läuft…", "s1": "Server und Schlüssel", "s2": "Archivdateien", "s3": "Optionen", "s3sub": "jede Option und ihre Berechtigung", "s4": "Ausführen", "sel": "{n} ausgewählt", "sel_dir": "{n} Ordner ausgewählt", "src_folder": "Ordner", "src_zip": "ZIP-Dateien", "st_avail": "{n} verfügbar", "st_blocked": "blockiert", "st_failed": "fehlgeschlagen", "st_incomplete": "unvollständig", "st_missing": "nicht installiert", "st_none": "keine", "st_ok": "in Ordnung", "st_ready": "bereit", "st_untested": "nicht geprüft", "t_details": "Details", "t_good": "Die Verbindung steht und alle prüfbaren Berechtigungen sind vorhanden. Der Rest wird im Testlauf bestätigt.", "t_hint": "<br>Prüfe Adresse und Port, üblicherweise 2283.", "t_missing": "{n} Berechtigung(en) fehlen. Ergänze sie in Immich am Schlüssel, oder schalte die zugehörige Option in Schritt 3 ab.", "t_missing_short": "{m} von {n} Berechtigungen fehlen", "t_ok_short": "Alle {n} Berechtigungen vorhanden", "t_unreach": "<b>Server nicht erreichbar.</b> ", "t_unver": "wird im Testlauf bestätigt", "testing": "wird geprüft…", "title": "Takeout-Uploader", "v_all": "Alle", "v_bad": "<b>{n} Datei(en) beschädigt oder abgeschnitten:</b> {names}<br>Lade sie vor dem Upload erneut herunter, sonst fehlen später Fotos ohne Hinweis.", "v_checking": "{n} Datei(en) werden geprüft…", "v_good": "<b>Alle Archive sind intakt</b> — {t} Einträge in {f} Datei(en). Bereit zum Upload.", "v_image": "Nur Fotos", "v_video": "Nur Videos", "w_aadd": "Fotos zu Alben hinzufügen", "w_about": "Serverversion lesen", "w_acreate": "Google-Alben anlegen", "w_albums": "Alben lesen", "w_overwrite": "Löscht die Kopie auf dem Server und lädt sie neu hoch. Braucht die Berechtigung <code>asset.delete</code>.", "w_search": "Duplikate vor dem Upload finden", "w_ssl": "Nur für ein selbstsigniertes Zertifikat in einem Netz, dem du vertraust.", "w_stats": "vorhandene Fotos zählen", "w_trace": "Das Protokoll enthält Anfragedetails, die persönliche Daten enthalten können.", "w_trashed": "Das sind Fotos, die du in Google Photos gelöscht hast.", "w_unmatched": "Sie kommen ohne Datum, Ort und Album an; immich-go greift auf EXIF oder den Dateinamen zurück.", "w_update": "Datum und Ort nach dem Upload setzen", "w_upload": "Fotos hochladen", "w_user": "Kontodaten lesen"}, "en": {"b_all": "select all", "b_dry": "Dry run", "b_install": "Install immich-go", "b_none": "clear", "b_refresh": "refresh", "b_start": "Start upload", "b_stop": "Stop", "b_test": "Test connection", "b_up": "up", "b_usedir": "use this folder", "b_verify": "check integrity", "copied": "copied", "copy_hint": "click any permission to copy it", "d_admin": "Pauses Immich's background jobs during the upload, which speeds things up on modest hardware. Requires an admin account.", "d_burst": "What to do with a rapid-fire sequence.", "d_conc": "How many photos upload at once. <b>6</b> suits most setups, <b>2–4</b> on a weak network or a small server.", "d_daterange": "Two dates, comma separated.", "d_files": "Select every part of the archive at once, and check their integrity before you start.", "d_key": "Create one in your Immich account settings. Nine permissions are enough — it doesn't need full access.", "d_pairs": "What to do when the same shot exists in two formats.", "d_perms": "In Immich open <span class='path'>Account Settings → API Keys → New API Key</span> then use the search box and tick these:<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>Add <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> and <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> for people tags. Don't pick <b>Select all</b> — it grants delete and key-creation rights.</p>", "d_preset": "Applies the settings immich-go recommends for that size.", "d_run": "Start with a dry run — it surfaces missing permissions and corrupt archives in minutes instead of hours.", "d_server": "The same address you open Immich with, including the port. Default is <code>2283</code>.", "d_tags": "Comma separated. Use a slash for a hierarchy, like <code>migration/2015</code>.", "d_timeout": "Raise it on a slow link. Accepts <code>30m</code>, <code>2h</code>.", "d_why": "<p>Google splits the archive by size, not by logic, so a photo lands in one part while its JSON sidecar — holding the date, location and album — lands in another. Upload them one at a time and the photos whose sidecars are missing get rejected outright.</p><p><b>Integrity check</b> reads each archive's index, which stays fast even on a 50 GB part. It catches truncated files and the error page Google serves when a link expires.</p>", "disks": "Disks:", "e_already_running": "A run is already in progress", "e_asset_not_found": "Release does not contain {arg}", "e_bad_data": "Invalid request", "e_binary_not_in_archive": "No immich-go binary inside {arg}", "e_install_failed": "Install failed: {arg}", "e_missing_file": "File not found: <bdi>{arg}</bdi>", "e_no_binary": "immich-go was not found on this machine", "e_no_key": "Enter the API key", "e_no_server": "Enter the server address", "e_no_zips": "Select at least one file", "e_none": "no folders or zip files here", "e_path_missing": "Path not found: <bdi>{arg}</bdi>", "e_pick": "pick a disk above", "e_read_denied": "Could not open folder: <bdi>{arg}</bdi>", "e_read_denied_mac": "Could not open folder: <bdi>{arg}</bdi><br>macOS won't let Terminal read this disk. Open <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> and enable <bdi>Terminal</bdi>, then relaunch it.", "e_release_lookup_failed": "Could not reach GitHub: {arg}", "e_unsupported_platform": "No immich-go release for this platform ({arg})", "folder_hint": "Pick the folder that contains the extracted <code>Takeout</code> directory. Integrity checking only applies to zip files.", "foot": "A local tool that runs only on your machine", "free": "free", "g_content": "Content", "g_perf": "Performance and connection", "g_stack": "Grouping", "g_tags": "Albums and tags", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "Home", "install_hint": "Downloads the release that matches this machine into ~/.local/bin", "install_ok": "Installed {v}", "installing": "downloading…", "l_admin": "Admin key — optional", "l_album": "Only this album", "l_burst": "Burst photos", "l_conc": "Parallel uploads", "l_daterange": "Date range", "l_excext": "Skip these extensions", "l_heicjpeg": "HEIC + JPEG pairs", "l_incext": "Only these extensions", "l_key": "API key", "l_partneralbum": "Put partner photos in album", "l_preset": "Preset", "l_rawjpeg": "RAW + JPEG pairs", "l_server": "Immich address", "l_tags": "Extra tags", "l_timeout": "Request timeout", "l_type": "File type", "l_tz": "Time zone", "lede": "Moves a Google Photos takeout into your Immich server with dates, locations and albums intact. Reads the zip files in place — no extracting, no copying.", "log_wait": "waiting to start…", "m_perms": "Required permissions", "m_why": "Why all parts at once?", "mac_block": "<bdi>{n}</bdi> is blocked — macOS won't let Terminal read external disks.<br>Fix: open <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> and enable <bdi>Terminal</bdi>, then quit and reopen it.", "n_accepted": "unexpected: request was accepted", "n_failed": "check failed", "n_flag": "sets", "n_forbidden": "key lacks this permission", "n_needs": "needs", "n_unauthorized": "key is invalid", "o_albums": "Rebuild Google albums", "o_archived": "Archived photos", "o_continue": "Continue past errors", "o_continue_p": "one bad file won't stop the whole upload", "o_epson": "Epson FastFoto scans", "o_overwrite": "Replace photos already on the server", "o_partner": "Partner's photos", "o_people": "People tags from JSON sidecars", "o_session": "Tag everything from this session", "o_ssl": "Skip certificate check", "o_takeout": "Tag grouping this takeout", "o_trace": "Log every API call", "o_trashed": "Trashed photos", "o_unmatched": "Photos with no JSON sidecar", "o_untitled": "Untitled albums", "p_custom": "Custom", "p_large": "Over 100k photos", "p_medium": "10k–100k photos", "p_slow": "Slow or unstable network", "p_small": "Under 10k photos", "ph_admin": "leave empty if you don't have one", "ph_key": "paste it here", "pill_bin": "immich-go", "pill_conn": "connection", "pill_disk": "disks", "r_caution": "changes what is imported", "r_danger": "deletes data", "r_done": "finished successfully", "r_dry": "dry run in progress…", "r_privacy": "security or privacy risk", "r_stop": "stopped · code {c}", "r_up": "uploading…", "s1": "Server and key", "s2": "Archive files", "s3": "Options", "s3sub": "each option and its permission", "s4": "Run", "sel": "{n} selected", "sel_dir": "{n} folder(s) selected", "src_folder": "Folder", "src_zip": "ZIP files", "st_avail": "{n} available", "st_blocked": "blocked", "st_failed": "failed", "st_incomplete": "incomplete", "st_missing": "not installed", "st_none": "none", "st_ok": "good", "st_ready": "ready", "st_untested": "not tested", "t_details": "details", "t_good": "Connection works and every readable permission is present. The rest is confirmed by the dry run.", "t_hint": "<br>Check the address and port, usually 2283.", "t_missing": "{n} permission(s) missing. Add them to the key in Immich, or turn off the option that needs them in step 3.", "t_missing_short": "{m} of {n} permissions missing", "t_ok_short": "All {n} permissions present", "t_unreach": "<b>Could not reach the server.</b> ", "t_unver": "confirmed by dry run", "testing": "testing…", "title": "Takeout Uploader", "v_all": "All", "v_bad": "<b>{n} file(s) corrupt or truncated:</b> {names}<br>Download them again before uploading — otherwise photos go missing silently.", "v_checking": "checking {n} file(s)…", "v_good": "<b>All archives are intact</b> — {t} entries across {f} file(s). Ready to upload.", "v_image": "Photos only", "v_video": "Videos only", "w_aadd": "add photos to albums", "w_about": "read server version", "w_acreate": "create Google albums", "w_albums": "read albums", "w_overwrite": "Deletes the server's copy and uploads again. Needs the <code>asset.delete</code> scope.", "w_search": "find duplicates before upload", "w_ssl": "Only for a self-signed certificate on a network you trust.", "w_stats": "count existing photos", "w_trace": "The log will contain request details, which can include personal data.", "w_trashed": "These are photos you deleted in Google Photos.", "w_unmatched": "They arrive with no date, location or album — immich-go falls back to EXIF or the filename.", "w_update": "set date and location after upload", "w_upload": "upload photos", "w_user": "read account details"}, "es": {"b_all": "seleccionar todo", "b_dry": "Prueba en seco", "b_install": "Instalar immich-go", "b_none": "limpiar", "b_refresh": "actualizar", "b_start": "Iniciar subida", "b_stop": "Detener", "b_test": "Probar conexión", "b_up": "subir", "b_usedir": "usar esta carpeta", "b_verify": "comprobar integridad", "copied": "copiado", "copy_hint": "haz clic en un permiso para copiarlo", "d_admin": "Pausa las tareas en segundo plano de Immich durante la subida, lo que acelera el proceso en equipos modestos. Requiere una cuenta de administrador.", "d_burst": "Qué hacer con una secuencia en ráfaga.", "d_conc": "Cuántas fotos se suben a la vez. <b>6</b> va bien en la mayoría de casos; <b>2–4</b> con una red débil o un servidor pequeño.", "d_daterange": "Dos fechas separadas por coma.", "d_files": "Selecciona todas las partes del archivo a la vez y comprueba su integridad antes de empezar.", "d_key": "Créala en los ajustes de tu cuenta de Immich. Nueve permisos bastan; no necesita acceso total.", "d_pairs": "Qué hacer cuando la misma toma existe en dos formatos.", "d_perms": "En Immich abre <span class='path'>Account Settings → API Keys → New API Key</span> y usa el buscador para marcar estos:<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>Añade <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> y <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> para las etiquetas de personas. No elijas <b>Seleccionar todo</b>: concede permisos de borrado y de creación de claves.</p>", "d_preset": "Aplica los ajustes que immich-go recomienda para ese tamaño.", "d_run": "Empieza con una prueba en seco: revela permisos que faltan y archivos corruptos en minutos en lugar de horas.", "d_server": "La misma dirección con la que abres Immich, incluido el puerto. Por defecto es <code>2283</code>.", "d_tags": "Separadas por comas. Usa una barra para jerarquías, como <code>migration/2015</code>.", "d_timeout": "Súbelo en conexiones lentas. Acepta <code>30m</code>, <code>2h</code>.", "d_why": "<p>Google divide el archivo por tamaño, no por lógica, así que una foto cae en una parte mientras su archivo JSON — que guarda la fecha, la ubicación y el álbum — cae en otra. Si las subes de una en una, las fotos cuyo JSON falta se rechazan por completo.</p><p>La <b>comprobación de integridad</b> lee el índice de cada archivo, algo rápido incluso en una parte de 50 GB. Detecta archivos truncados y la página de error que Google entrega cuando un enlace caduca.</p>", "disks": "Discos:", "e_already_running": "Ya hay una ejecución en curso", "e_asset_not_found": "La versión no contiene {arg}", "e_bad_data": "Solicitud no válida", "e_binary_not_in_archive": "No hay binario immich-go dentro de {arg}", "e_install_failed": "Falló la instalación: {arg}", "e_missing_file": "Archivo no encontrado: <bdi>{arg}</bdi>", "e_no_binary": "No se encontró immich-go en este equipo", "e_no_key": "Escribe la clave API", "e_no_server": "Escribe la dirección del servidor", "e_no_zips": "Selecciona al menos un archivo", "e_none": "aquí no hay carpetas ni archivos zip", "e_path_missing": "Ruta no encontrada: <bdi>{arg}</bdi>", "e_pick": "elige un disco arriba", "e_read_denied": "No se pudo abrir la carpeta: <bdi>{arg}</bdi>", "e_read_denied_mac": "No se pudo abrir la carpeta: <bdi>{arg}</bdi><br>macOS no deja que Terminal lea este disco. Abre <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> y activa <bdi>Terminal</bdi>, luego reinícialo.", "e_release_lookup_failed": "No se pudo contactar con GitHub: {arg}", "e_unsupported_platform": "No hay versión de immich-go para esta plataforma ({arg})", "folder_hint": "Elige la carpeta que contiene el directorio <code>Takeout</code> ya extraído. La comprobación de integridad solo se aplica a los zip.", "foot": "Una herramienta local que se ejecuta solo en tu equipo", "free": "libres", "g_content": "Contenido", "g_perf": "Rendimiento y conexión", "g_stack": "Agrupación", "g_tags": "Álbumes y etiquetas", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "Inicio", "install_hint": "Descarga en ~/.local/bin la versión que corresponde a este equipo", "install_ok": "Instalado {v}", "installing": "descargando…", "l_admin": "Clave de administrador — opcional", "l_album": "Solo este álbum", "l_burst": "Ráfagas", "l_conc": "Subidas en paralelo", "l_daterange": "Rango de fechas", "l_excext": "Omitir estas extensiones", "l_heicjpeg": "Pares HEIC + JPEG", "l_incext": "Solo estas extensiones", "l_key": "Clave API", "l_partneralbum": "Poner las fotos de la pareja en el álbum", "l_preset": "Preajuste", "l_rawjpeg": "Pares RAW + JPEG", "l_server": "Dirección de Immich", "l_tags": "Etiquetas adicionales", "l_timeout": "Tiempo de espera", "l_type": "Tipo de archivo", "l_tz": "Zona horaria", "lede": "Traslada un takeout de Google Photos a tu servidor Immich conservando fechas, ubicaciones y álbumes. Lee los archivos zip donde están: sin extraer ni copiar.", "log_wait": "esperando para empezar…", "m_perms": "Permisos necesarios", "m_why": "¿Por qué todas las partes a la vez?", "mac_block": "<bdi>{n}</bdi> está bloqueado: macOS no deja que Terminal lea discos externos.<br>Solución: abre <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> y activa <bdi>Terminal</bdi>, luego ciérralo y ábrelo de nuevo.", "n_accepted": "inesperado: la petición fue aceptada", "n_failed": "la comprobación falló", "n_flag": "establece", "n_forbidden": "la clave no tiene este permiso", "n_needs": "necesita", "n_unauthorized": "la clave no es válida", "o_albums": "Reconstruir álbumes de Google", "o_archived": "Fotos archivadas", "o_continue": "Continuar pese a los errores", "o_continue_p": "un archivo defectuoso no detendrá toda la subida", "o_epson": "Escaneos Epson FastFoto", "o_overwrite": "Reemplazar las fotos ya subidas", "o_partner": "Fotos de la pareja", "o_people": "Etiquetas de personas desde los JSON", "o_session": "Etiquetar todo lo de esta sesión", "o_ssl": "Omitir la comprobación del certificado", "o_takeout": "Etiqueta que agrupa este takeout", "o_trace": "Registrar cada llamada a la API", "o_trashed": "Fotos en la papelera", "o_unmatched": "Fotos sin archivo JSON", "o_untitled": "Álbumes sin título", "p_custom": "Personalizado", "p_large": "Más de 100k fotos", "p_medium": "10k–100k fotos", "p_slow": "Red lenta o inestable", "p_small": "Menos de 10k fotos", "ph_admin": "déjalo vacío si no tienes una", "ph_key": "pégala aquí", "pill_bin": "immich-go", "pill_conn": "conexión", "pill_disk": "discos", "r_caution": "cambia lo que se importa", "r_danger": "borra datos", "r_done": "terminó correctamente", "r_dry": "prueba en curso…", "r_privacy": "riesgo de seguridad o privacidad", "r_stop": "detenido · código {c}", "r_up": "subiendo…", "s1": "Servidor y clave", "s2": "Archivos", "s3": "Opciones", "s3sub": "cada opción y su permiso", "s4": "Ejecutar", "sel": "{n} seleccionado(s)", "sel_dir": "{n} carpeta(s) seleccionada(s)", "src_folder": "Carpeta", "src_zip": "Archivos ZIP", "st_avail": "{n} disponible(s)", "st_blocked": "bloqueado", "st_failed": "falló", "st_incomplete": "incompleto", "st_missing": "no instalado", "st_none": "ninguno", "st_ok": "correcto", "st_ready": "listo", "st_untested": "sin probar", "t_details": "detalles", "t_good": "La conexión funciona y están todos los permisos comprobables. El resto se confirma con la prueba en seco.", "t_hint": "<br>Comprueba la dirección y el puerto, normalmente 2283.", "t_missing": "Faltan {n} permiso(s). Añádelos a la clave en Immich o desactiva la opción que los necesita en el paso 3.", "t_missing_short": "Faltan {m} de {n} permisos", "t_ok_short": "Los {n} permisos están presentes", "t_unreach": "<b>No se pudo contactar con el servidor.</b> ", "t_unver": "se confirma en la prueba", "testing": "probando…", "title": "Cargador de Takeout", "v_all": "Todo", "v_bad": "<b>{n} archivo(s) corrupto(s) o truncado(s):</b> {names}<br>Descárgalos de nuevo antes de subir; si no, se perderán fotos sin aviso.", "v_checking": "comprobando {n} archivo(s)…", "v_good": "<b>Todos los archivos están intactos</b> — {t} entradas en {f} archivo(s). Listo para subir.", "v_image": "Solo fotos", "v_video": "Solo vídeos", "w_aadd": "añadir fotos a álbumes", "w_about": "leer la versión del servidor", "w_acreate": "crear álbumes de Google", "w_albums": "leer álbumes", "w_overwrite": "Borra la copia del servidor y vuelve a subirla. Necesita el permiso <code>asset.delete</code>.", "w_search": "buscar duplicados antes de subir", "w_ssl": "Solo para un certificado autofirmado en una red de confianza.", "w_stats": "contar las fotos existentes", "w_trace": "El registro incluirá detalles de las peticiones, que pueden contener datos personales.", "w_trashed": "Son fotos que borraste en Google Photos.", "w_unmatched": "Llegan sin fecha, ubicación ni álbum; immich-go recurre al EXIF o al nombre del archivo.", "w_update": "fijar fecha y ubicación tras subir", "w_upload": "subir fotos", "w_user": "leer datos de la cuenta"}, "fr": {"b_all": "tout sélectionner", "b_dry": "Simulation", "b_install": "Installer immich-go", "b_none": "effacer", "b_refresh": "actualiser", "b_start": "Démarrer l'envoi", "b_stop": "Arrêter", "b_test": "Tester la connexion", "b_up": "remonter", "b_usedir": "utiliser ce dossier", "b_verify": "vérifier l'intégrité", "copied": "copié", "copy_hint": "cliquez sur une autorisation pour la copier", "d_admin": "Met en pause les tâches d'arrière-plan d'Immich pendant l'envoi, ce qui accélère les choses sur du matériel modeste. Nécessite un compte administrateur.", "d_burst": "Que faire d'une séquence en rafale.", "d_conc": "Nombre de photos envoyées simultanément. <b>6</b> convient à la plupart des cas, <b>2–4</b> sur un réseau faible ou un petit serveur.", "d_daterange": "Deux dates séparées par une virgule.", "d_files": "Sélectionnez toutes les parties de l'archive d'un coup, et vérifiez leur intégrité avant de commencer.", "d_key": "Créez-la dans les paramètres de votre compte Immich. Neuf autorisations suffisent : l'accès complet est inutile.", "d_pairs": "Que faire quand la même prise existe dans deux formats.", "d_perms": "Dans Immich, ouvrez <span class='path'>Account Settings → API Keys → New API Key</span> puis utilisez la recherche pour cocher celles-ci :<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>Ajoutez <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> et <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> pour les tags de personnes. Ne choisissez pas <b>Tout sélectionner</b> : cela accorde les droits de suppression et de création de clés.</p>", "d_preset": "Applique les réglages recommandés par immich-go pour cette taille.", "d_run": "Commencez par une simulation : elle révèle les autorisations manquantes et les archives corrompues en quelques minutes au lieu d'heures.", "d_server": "La même adresse que pour ouvrir Immich, port compris. Par défaut <code>2283</code>.", "d_tags": "Séparés par des virgules. Utilisez une barre oblique pour une hiérarchie, comme <code>migration/2015</code>.", "d_timeout": "À augmenter sur une liaison lente. Accepte <code>30m</code>, <code>2h</code>.", "d_why": "<p>Google découpe l'archive par taille et non par logique : une photo se retrouve dans une partie tandis que son fichier JSON — qui contient la date, le lieu et l'album — atterrit dans une autre. En les envoyant une par une, les photos dont le JSON manque sont purement rejetées.</p><p>La <b>vérification d'intégrité</b> lit l'index de chaque archive, ce qui reste rapide même sur une partie de 50 Go. Elle détecte les fichiers tronqués et la page d'erreur que Google renvoie quand un lien expire.</p>", "disks": "Disques :", "e_already_running": "Une exécution est déjà en cours", "e_asset_not_found": "La version ne contient pas {arg}", "e_bad_data": "Requête invalide", "e_binary_not_in_archive": "Aucun binaire immich-go dans {arg}", "e_install_failed": "Échec de l'installation : {arg}", "e_missing_file": "Fichier introuvable : <bdi>{arg}</bdi>", "e_no_binary": "immich-go est introuvable sur cette machine", "e_no_key": "Saisissez la clé API", "e_no_server": "Saisissez l'adresse du serveur", "e_no_zips": "Sélectionnez au moins un fichier", "e_none": "aucun dossier ni fichier zip ici", "e_path_missing": "Chemin introuvable : <bdi>{arg}</bdi>", "e_pick": "choisissez un disque ci-dessus", "e_read_denied": "Impossible d'ouvrir le dossier : <bdi>{arg}</bdi>", "e_read_denied_mac": "Impossible d'ouvrir le dossier : <bdi>{arg}</bdi><br>macOS empêche Terminal de lire ce disque. Ouvrez <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> et activez <bdi>Terminal</bdi>, puis relancez-le.", "e_release_lookup_failed": "GitHub injoignable : {arg}", "e_unsupported_platform": "Aucune version d'immich-go pour cette plateforme ({arg})", "folder_hint": "Choisissez le dossier contenant le répertoire <code>Takeout</code> déjà extrait. La vérification d'intégrité ne concerne que les fichiers zip.", "foot": "Un outil local qui s'exécute uniquement sur votre machine", "free": "libres", "g_content": "Contenu", "g_perf": "Performances et connexion", "g_stack": "Regroupement", "g_tags": "Albums et tags", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "Dossier personnel", "install_hint": "Télécharge dans ~/.local/bin la version correspondant à cette machine", "install_ok": "Installé {v}", "installing": "téléchargement…", "l_admin": "Clé administrateur — facultatif", "l_album": "Cet album uniquement", "l_burst": "Rafales", "l_conc": "Envois en parallèle", "l_daterange": "Plage de dates", "l_excext": "Ignorer ces extensions", "l_heicjpeg": "Paires HEIC + JPEG", "l_incext": "Ces extensions uniquement", "l_key": "Clé API", "l_partneralbum": "Mettre les photos du partenaire dans l'album", "l_preset": "Préréglage", "l_rawjpeg": "Paires RAW + JPEG", "l_server": "Adresse Immich", "l_tags": "Tags supplémentaires", "l_timeout": "Délai d'attente", "l_type": "Type de fichier", "l_tz": "Fuseau horaire", "lede": "Transfère un takeout Google Photos vers votre serveur Immich en conservant dates, lieux et albums. Lit les fichiers zip sur place : sans extraction ni copie.", "log_wait": "en attente du démarrage…", "m_perms": "Autorisations requises", "m_why": "Pourquoi toutes les parties d'un coup ?", "mac_block": "<bdi>{n}</bdi> est bloqué : macOS empêche Terminal de lire les disques externes.<br>Solution : ouvrez <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> et activez <bdi>Terminal</bdi>, puis quittez-le et rouvrez-le.", "n_accepted": "inattendu : la requête a été acceptée", "n_failed": "échec du test", "n_flag": "définit", "n_forbidden": "la clé n'a pas cette autorisation", "n_needs": "nécessite", "n_unauthorized": "clé invalide", "o_albums": "Reconstruire les albums Google", "o_archived": "Photos archivées", "o_continue": "Continuer malgré les erreurs", "o_continue_p": "un fichier défectueux n'arrêtera pas tout l'envoi", "o_epson": "Numérisations Epson FastFoto", "o_overwrite": "Remplacer les photos déjà envoyées", "o_partner": "Photos du partenaire", "o_people": "Tags de personnes depuis les JSON", "o_session": "Taguer tout ce qui vient de cette session", "o_ssl": "Ignorer la vérification du certificat", "o_takeout": "Tag regroupant ce takeout", "o_trace": "Journaliser chaque appel API", "o_trashed": "Photos supprimées", "o_unmatched": "Photos sans fichier JSON", "o_untitled": "Albums sans titre", "p_custom": "Personnalisé", "p_large": "Plus de 100k photos", "p_medium": "10k–100k photos", "p_slow": "Réseau lent ou instable", "p_small": "Moins de 10k photos", "ph_admin": "laissez vide si vous n'en avez pas", "ph_key": "collez-la ici", "pill_bin": "immich-go", "pill_conn": "connexion", "pill_disk": "disques", "r_caution": "modifie ce qui est importé", "r_danger": "supprime des données", "r_done": "terminé avec succès", "r_dry": "simulation en cours…", "r_privacy": "risque de sécurité ou de confidentialité", "r_stop": "arrêté · code {c}", "r_up": "envoi en cours…", "s1": "Serveur et clé", "s2": "Fichiers d'archive", "s3": "Options", "s3sub": "chaque option et son autorisation", "s4": "Exécution", "sel": "{n} sélectionné(s)", "sel_dir": "{n} dossier(s) sélectionné(s)", "src_folder": "Dossier", "src_zip": "Fichiers ZIP", "st_avail": "{n} disponible(s)", "st_blocked": "bloqué", "st_failed": "échec", "st_incomplete": "incomplet", "st_missing": "non installé", "st_none": "aucun", "st_ok": "correct", "st_ready": "prêt", "st_untested": "non testé", "t_details": "détails", "t_good": "La connexion fonctionne et toutes les autorisations vérifiables sont présentes. Le reste est confirmé par la simulation.", "t_hint": "<br>Vérifiez l'adresse et le port, généralement 2283.", "t_missing": "{n} autorisation(s) manquante(s). Ajoutez-les à la clé dans Immich, ou désactivez l'option correspondante à l'étape 3.", "t_missing_short": "{m} autorisations manquantes sur {n}", "t_ok_short": "Les {n} autorisations sont présentes", "t_unreach": "<b>Serveur injoignable.</b> ", "t_unver": "confirmé par la simulation", "testing": "test en cours…", "title": "Chargeur Takeout", "v_all": "Tout", "v_bad": "<b>{n} fichier(s) corrompu(s) ou tronqué(s) :</b> {names}<br>Retéléchargez-les avant l'envoi, sinon des photos disparaîtront sans avertissement.", "v_checking": "vérification de {n} fichier(s)…", "v_good": "<b>Toutes les archives sont intactes</b> — {t} entrées dans {f} fichier(s). Prêt à envoyer.", "v_image": "Photos seulement", "v_video": "Vidéos seulement", "w_aadd": "ajouter les photos aux albums", "w_about": "lire la version du serveur", "w_acreate": "créer les albums Google", "w_albums": "lire les albums", "w_overwrite": "Supprime la copie du serveur et la renvoie. Nécessite l'autorisation <code>asset.delete</code>.", "w_search": "repérer les doublons avant l'envoi", "w_ssl": "Uniquement pour un certificat auto-signé sur un réseau de confiance.", "w_stats": "compter les photos existantes", "w_trace": "Le journal contiendra le détail des requêtes, qui peut inclure des données personnelles.", "w_trashed": "Ce sont les photos que vous avez supprimées dans Google Photos.", "w_unmatched": "Elles arrivent sans date, lieu ni album ; immich-go se rabat sur l'EXIF ou le nom du fichier.", "w_update": "définir date et lieu après l'envoi", "w_upload": "envoyer les photos", "w_user": "lire les infos du compte"}, "pt": {"b_all": "selecionar tudo", "b_dry": "Simulação", "b_install": "Instalar o immich-go", "b_none": "limpar", "b_refresh": "atualizar", "b_start": "Iniciar envio", "b_stop": "Parar", "b_test": "Testar ligação", "b_up": "subir", "b_usedir": "usar esta pasta", "b_verify": "verificar integridade", "copied": "copiado", "copy_hint": "clique numa permissão para a copiar", "d_admin": "Suspende as tarefas em segundo plano do Immich durante o envio, o que acelera bastante em equipamento modesto. Requer uma conta de administrador.", "d_burst": "O que fazer com uma sequência rápida.", "d_conc": "Quantas fotos são enviadas ao mesmo tempo. <b>6</b> serve na maioria dos casos, <b>2–4</b> numa rede fraca ou num servidor pequeno.", "d_daterange": "Duas datas separadas por vírgula.", "d_files": "Selecione todas as partes do arquivo de uma vez e verifique a integridade antes de começar.", "d_key": "Crie uma nas definições da sua conta Immich. Nove permissões chegam — não precisa de acesso total.", "d_pairs": "O que fazer quando a mesma fotografia existe em dois formatos.", "d_perms": "No Immich abra <span class='path'>Account Settings → API Keys → New API Key</span> e use a caixa de pesquisa para marcar estas:<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>Acrescente <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> e <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button> para etiquetas de pessoas. Não escolha <b>Selecionar tudo</b> — concede direitos de eliminação e de criação de chaves.</p>", "d_preset": "Aplica as definições que o immich-go recomenda para esse tamanho.", "d_run": "Comece com uma simulação: revela permissões em falta e arquivos corrompidos em minutos em vez de horas.", "d_server": "O mesmo endereço com que abre o Immich, incluindo a porta. Por omissão é <code>2283</code>.", "d_tags": "Separadas por vírgulas. Use uma barra para hierarquia, como <code>migration/2015</code>.", "d_timeout": "Aumente numa ligação lenta. Aceita <code>30m</code>, <code>2h</code>.", "d_why": "<p>O Google divide o arquivo por tamanho e não por lógica, portanto uma foto fica numa parte enquanto o respetivo ficheiro JSON — com a data, o local e o álbum — fica noutra. Se as enviar uma a uma, as fotos sem o JSON correspondente são rejeitadas por completo.</p><p>A <b>verificação de integridade</b> lê o índice de cada arquivo, o que continua rápido mesmo numa parte de 50 GB. Deteta ficheiros truncados e a página de erro que o Google devolve quando uma ligação expira.</p>", "disks": "Discos:", "e_already_running": "Já está uma execução em curso", "e_asset_not_found": "A versão não contém {arg}", "e_bad_data": "Pedido inválido", "e_binary_not_in_archive": "Não há binário immich-go dentro de {arg}", "e_install_failed": "A instalação falhou: {arg}", "e_missing_file": "Ficheiro não encontrado: <bdi>{arg}</bdi>", "e_no_binary": "O immich-go não foi encontrado neste computador", "e_no_key": "Introduza a chave de API", "e_no_server": "Introduza o endereço do servidor", "e_no_zips": "Selecione pelo menos um ficheiro", "e_none": "não há pastas nem ficheiros zip aqui", "e_path_missing": "Caminho não encontrado: <bdi>{arg}</bdi>", "e_pick": "escolha um disco acima", "e_read_denied": "Não foi possível abrir a pasta: <bdi>{arg}</bdi>", "e_read_denied_mac": "Não foi possível abrir a pasta: <bdi>{arg}</bdi><br>O macOS não deixa o Terminal ler este disco. Abra <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> e ative o <bdi>Terminal</bdi>, depois reinicie-o.", "e_release_lookup_failed": "Não foi possível contactar o GitHub: {arg}", "e_unsupported_platform": "Não há versão do immich-go para esta plataforma ({arg})", "folder_hint": "Escolha a pasta que contém o diretório <code>Takeout</code> já extraído. A verificação de integridade aplica-se apenas a ficheiros zip.", "foot": "Uma ferramenta local que corre apenas no seu computador", "free": "livres", "g_content": "Conteúdo", "g_perf": "Desempenho e ligação", "g_stack": "Agrupamento", "g_tags": "Álbuns e etiquetas", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "Pasta pessoal", "install_hint": "Transfere para ~/.local/bin a versão correspondente a este computador", "install_ok": "Instalado {v}", "installing": "a transferir…", "l_admin": "Chave de administrador — opcional", "l_album": "Apenas este álbum", "l_burst": "Sequências rápidas", "l_conc": "Envios em paralelo", "l_daterange": "Intervalo de datas", "l_excext": "Ignorar estas extensões", "l_heicjpeg": "Pares HEIC + JPEG", "l_incext": "Apenas estas extensões", "l_key": "Chave de API", "l_partneralbum": "Colocar as fotos do parceiro no álbum", "l_preset": "Predefinição", "l_rawjpeg": "Pares RAW + JPEG", "l_server": "Endereço do Immich", "l_tags": "Etiquetas adicionais", "l_timeout": "Tempo limite do pedido", "l_type": "Tipo de ficheiro", "l_tz": "Fuso horário", "lede": "Move um takeout do Google Photos para o seu servidor Immich preservando datas, locais e álbuns. Lê os ficheiros zip onde estão: sem extrair nem copiar.", "log_wait": "à espera do início…", "m_perms": "Permissões necessárias", "m_why": "Porquê todas as partes de uma vez?", "mac_block": "<bdi>{n}</bdi> está bloqueado — o macOS não deixa o Terminal ler discos externos.<br>Solução: abra <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> e ative o <bdi>Terminal</bdi>, depois feche-o e abra-o de novo.", "n_accepted": "inesperado: o pedido foi aceite", "n_failed": "a verificação falhou", "n_flag": "define", "n_forbidden": "a chave não tem esta permissão", "n_needs": "precisa de", "n_unauthorized": "chave inválida", "o_albums": "Reconstruir álbuns do Google", "o_archived": "Fotos arquivadas", "o_continue": "Continuar apesar dos erros", "o_continue_p": "um ficheiro defeituoso não trava o envio inteiro", "o_epson": "Digitalizações Epson FastFoto", "o_overwrite": "Substituir as fotos já enviadas", "o_partner": "Fotos do parceiro", "o_people": "Etiquetas de pessoas a partir dos JSON", "o_session": "Etiquetar tudo desta sessão", "o_ssl": "Ignorar a verificação do certificado", "o_takeout": "Etiqueta que agrupa este takeout", "o_trace": "Registar cada chamada à API", "o_trashed": "Fotos no lixo", "o_unmatched": "Fotos sem ficheiro JSON", "o_untitled": "Álbuns sem título", "p_custom": "Personalizado", "p_large": "Mais de 100k fotos", "p_medium": "10k–100k fotos", "p_slow": "Rede lenta ou instável", "p_small": "Menos de 10k fotos", "ph_admin": "deixe vazio se não tiver", "ph_key": "cole aqui", "pill_bin": "immich-go", "pill_conn": "ligação", "pill_disk": "discos", "r_caution": "altera o que é importado", "r_danger": "apaga dados", "r_done": "terminou com sucesso", "r_dry": "simulação em curso…", "r_privacy": "risco de segurança ou privacidade", "r_stop": "parado · código {c}", "r_up": "a enviar…", "s1": "Servidor e chave", "s2": "Ficheiros do arquivo", "s3": "Opções", "s3sub": "cada opção e a sua permissão", "s4": "Executar", "sel": "{n} selecionado(s)", "sel_dir": "{n} pasta(s) selecionada(s)", "src_folder": "Pasta", "src_zip": "Ficheiros ZIP", "st_avail": "{n} disponível(is)", "st_blocked": "bloqueado", "st_failed": "falhou", "st_incomplete": "incompleto", "st_missing": "não instalado", "st_none": "nenhum", "st_ok": "correto", "st_ready": "pronto", "st_untested": "não testado", "t_details": "detalhes", "t_good": "A ligação funciona e todas as permissões verificáveis estão presentes. O resto é confirmado pela simulação.", "t_hint": "<br>Verifique o endereço e a porta, normalmente 2283.", "t_missing": "Faltam {n} permissão(ões). Adicione-as à chave no Immich, ou desligue a opção que as exige no passo 3.", "t_missing_short": "Faltam {m} de {n} permissões", "t_ok_short": "As {n} permissões estão presentes", "t_unreach": "<b>Não foi possível contactar o servidor.</b> ", "t_unver": "confirmado na simulação", "testing": "a testar…", "title": "Carregador de Takeout", "v_all": "Tudo", "v_bad": "<b>{n} ficheiro(s) corrompido(s) ou truncado(s):</b> {names}<br>Volte a transferi-los antes de enviar; caso contrário perdem-se fotos sem aviso.", "v_checking": "a verificar {n} ficheiro(s)…", "v_good": "<b>Todos os arquivos estão intactos</b> — {t} entradas em {f} ficheiro(s). Pronto para enviar.", "v_image": "Apenas fotos", "v_video": "Apenas vídeos", "w_aadd": "adicionar fotos aos álbuns", "w_about": "ler a versão do servidor", "w_acreate": "criar álbuns do Google", "w_albums": "ler álbuns", "w_overwrite": "Apaga a cópia do servidor e envia de novo. Precisa da permissão <code>asset.delete</code>.", "w_search": "encontrar duplicados antes do envio", "w_ssl": "Apenas para um certificado autoassinado numa rede em que confia.", "w_stats": "contar as fotos existentes", "w_trace": "O registo conterá detalhes dos pedidos, que podem incluir dados pessoais.", "w_trashed": "São fotos que apagou no Google Photos.", "w_unmatched": "Chegam sem data, local nem álbum; o immich-go recorre ao EXIF ou ao nome do ficheiro.", "w_update": "definir data e local após o envio", "w_upload": "enviar fotos", "w_user": "ler dados da conta"}, "zh": {"b_all": "全选", "b_dry": "试运行", "b_install": "安装 immich-go", "b_none": "清空", "b_refresh": "刷新", "b_start": "开始上传", "b_stop": "停止", "b_test": "测试连接", "b_up": "上一层", "b_usedir": "使用此文件夹", "b_verify": "检查完整性", "copied": "已复制", "copy_hint": "点击任意权限即可复制", "d_admin": "上传期间暂停 Immich 的后台任务，在配置一般的设备上能明显提速。需要管理员账户。", "d_burst": "如何处理连拍序列。", "d_conc": "同时上传多少张照片。<b>6</b> 适合大多数情况；网络较弱或服务器较小时用 <b>2–4</b>。", "d_daterange": "两个日期，用逗号分隔。", "d_files": "一次选中归档的全部分卷，并在开始前检查完整性。", "d_key": "在 Immich 的账户设置里创建。九项权限就够了，不需要完全访问权。", "d_pairs": "同一张照片存在两种格式时如何处理。", "d_perms": "在 Immich 中打开 <span class='path'>Account Settings → API Keys → New API Key</span>，用搜索框勾选这些：<div class='perms'><button type='button' class='chip' data-copy='user.read'><span class='cn'>user.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='server.about'><span class='cn'>server.about</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.read'><span class='cn'>asset.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.statistics'><span class='cn'>asset.statistics</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.upload'><span class='cn'>asset.upload</span><span class='ci'></span></button><button type='button' class='chip' data-copy='asset.update'><span class='cn'>asset.update</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.read'><span class='cn'>album.read</span><span class='ci'></span></button><button type='button' class='chip' data-copy='album.create'><span class='cn'>album.create</span><span class='ci'></span></button><button type='button' class='chip' data-copy='albumAsset.create'><span class='cn'>albumAsset.create</span><span class='ci'></span></button></div><div class='copybar'><span class='copyhint'></span></div><p>需要人物标签的话再加上 <button type='button' class='chip' data-copy='tag.create'><span class='cn'>tag.create</span><span class='ci'></span></button> 和 <button type='button' class='chip' data-copy='tag.asset'><span class='cn'>tag.asset</span><span class='ci'></span></button>。不要选 <b>全选</b>，那会授予删除和创建密钥的权限。</p>", "d_preset": "套用 immich-go 针对该规模推荐的设置。", "d_run": "先做一次试运行：它能在几分钟内发现缺失的权限和损坏的归档，而不是几小时后才发现。", "d_server": "和你打开 Immich 时用的地址一样，含端口。默认是 <code>2283</code>。", "d_tags": "用逗号分隔。用斜杠表示层级，例如 <code>migration/2015</code>。", "d_timeout": "网络较慢时调大。可填 <code>30m</code>、<code>2h</code>。", "d_why": "<p>Google 按体积而非逻辑切分归档，所以一张照片可能在某个分卷里，而记录日期、位置和相册的 JSON 文件却在另一个分卷里。逐个上传时，找不到对应 JSON 的照片会被直接拒绝。</p><p><b>完整性检查</b>只读取每个归档的索引，即使 50 GB 的分卷也很快。它能识别被截断的文件，以及链接过期时 Google 返回的错误页面。</p>", "disks": "磁盘：", "e_already_running": "已有任务在运行", "e_asset_not_found": "该版本不包含 {arg}", "e_bad_data": "请求无效", "e_binary_not_in_archive": "{arg} 中没有 immich-go 可执行文件", "e_install_failed": "安装失败：{arg}", "e_missing_file": "找不到文件：<bdi>{arg}</bdi>", "e_no_binary": "本机未找到 immich-go", "e_no_key": "请填写 API 密钥", "e_no_server": "请填写服务器地址", "e_no_zips": "至少选择一个文件", "e_none": "这里没有文件夹或 zip 文件", "e_path_missing": "找不到路径：<bdi>{arg}</bdi>", "e_pick": "请先在上方选择磁盘", "e_read_denied": "无法打开文件夹：<bdi>{arg}</bdi>", "e_read_denied_mac": "无法打开文件夹：<bdi>{arg}</bdi><br>macOS 不允许终端读取此磁盘。请打开 <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> 并启用 <bdi>Terminal</bdi>，然后重启终端。", "e_release_lookup_failed": "无法连接 GitHub：{arg}", "e_unsupported_platform": "没有适用于该平台的 immich-go 版本（{arg}）", "folder_hint": "选择包含已解压 <code>Takeout</code> 目录的文件夹。完整性检查仅适用于 zip 文件。", "foot": "完全在你本机运行的本地工具", "free": "可用", "g_content": "内容", "g_perf": "性能与连接", "g_stack": "归组", "g_tags": "相册与标签", "h_apiTrace": "<b>Default: off.</b> Writes the full detail of every API call into the log file.<p>For diagnosing a problem or filing a bug report. It makes the log very large and it records request contents, which can include file names and other personal details.</p><p>The key itself is masked in this interface, but treat a trace log as sensitive and check it before sharing.</p>", "h_burst": "<b>Default: NoStack.</b> Controls a rapid-fire sequence, where one press produced many frames.<p><b>Stack</b> keeps every frame but groups them under one cover, so the sequence takes one slot in the timeline instead of thirty.</p><p><b>StackKeepRaw</b> / <b>StackKeepJPEG</b> also drop the other format inside the burst. Those discard files; plain <b>Stack</b> does not.</p>", "h_clientTimeout": "<b>Default: 20m.</b> How long to wait for one request before giving up on it.<p>Raise it if large videos fail on a slow connection: <code>60m</code>, <code>2h</code>. It costs nothing when things are healthy, because the timeout only ever applies to a request that is already stuck.</p>", "h_concurrent": "<b>Default: 6.</b> How many photos are uploaded at the same time.<p>Higher is faster only while the network and the server can keep up. Past that point uploads start timing out and the run gets <i>slower</i>.</p><p><b>2–4</b> for a weak link or a Raspberry Pi, <b>6–8</b> for ordinary use, <b>12–16</b> only on a wired LAN with a capable server. Safe to change and retry — nothing is corrupted by getting it wrong.</p>", "h_continueOnError": "<b>Default: on.</b> A file that fails is logged and skipped, and the run keeps going.<p><b>Turn it off</b> and the very first bad file stops everything. On a takeout with tens of thousands of photos, one corrupt file would end a run that was hours in.</p><p>Nothing is hidden either way — every failure appears in the log.</p>", "h_dateRange": "<b>Default: empty (all dates).</b> Imports only photos taken inside the range.<p>Two dates separated by a comma, like <code>2015-01-01,2018-12-31</code>. The date comes from the JSON sidecar, so photos without one are affected by the setting above.</p><p>This is the clean way to split a very large library into several runs by year.</p>", "h_epson": "<b>Default: off.</b> Recognises the naming pattern an Epson FastFoto scanner produces and groups each scan with its corrected and back-side versions.<p>Only turn this on if your takeout actually contains FastFoto scans. It has no effect otherwise.</p>", "h_extensions": "<b>Default: empty (all file types).</b> Restricts by file extension.<p><b>Only these</b> imports nothing else. <b>Skip these</b> imports everything except them. Write them comma separated with the dot, like <code>.jpg,.heic</code>.</p><p>Handy for leaving out <code>.gif</code> or Google's Motion Photo <code>.mp</code> files.</p>", "h_fromAlbum": "<b>Default: empty (everything).</b> Imports only the photos in one named Google album.<p>Type the album name exactly as it appears in Google Photos. Good for testing the whole flow on a small album before committing to the full library.</p>", "h_includeArchived": "<b>Default: on.</b> Photos you archived in Google Photos are imported too.<p>Archived means hidden from the main Google timeline, not deleted. They arrive in Immich as normal photos.</p><p><b>Turn it off</b> and they are skipped entirely — they will not be in Immich at all.</p>", "h_includePartner": "<b>Default: on.</b> Imports photos shared with you through Google's partner sharing.<p>These are your partner's photos that Google shared into your library. They upload into <i>your</i> Immich account.</p><p>Turn it off to keep your library to your own photos only.</p>", "h_includeTrashed": "<b>Default: off.</b> Imports photos sitting in Google Photos' trash.<p><b>These are photos you chose to delete.</b> Turning this on brings them back into your library, mixed in with everything else.</p><p>Safe in the sense that nothing is destroyed — but you may end up restoring pictures you deliberately got rid of, and finding them again afterwards means hunting through the whole import.</p>", "h_includeType": "<b>Default: all.</b> Restricts the import to photos only or videos only.<p>Useful for splitting a huge takeout into two passes, or when videos would fill the server. Anything excluded is simply not uploaded; you can run again later with the other setting.</p>", "h_includeUnmatched": "<b>Default: off.</b> Uploads photos that have no matching JSON file in the takeout.<p>Normally every photo has a sidecar holding its date, GPS and album. When one is missing, immich-go skips the photo rather than importing it wrong.</p><p><b>Turn it on</b> and those photos upload with <b>no date, no location and no album</b>. immich-go falls back to EXIF data or the filename, which is often wrong — expect photos dated the day you uploaded them.</p><p>Reach for this only if a dry run shows many files being skipped. Fixing the takeout is usually better: sidecars often live in a different zip part, so first make sure you selected them all.</p>", "h_includeUntitled": "<b>Default: off.</b> Google puts albums you never named into folders like <i>Untitled</i>, <i>Sin título</i>, <i>Sans titre</i>.<p>The catch: Google may collapse several unnamed albums into one folder, so the original grouping cannot be recovered. Turning this on can produce one large album that mixes unrelated sets.</p><p>The photos themselves upload either way; this only controls whether those albums are recreated.</p>", "h_overwrite": "<b>Default: off. This is the one option that deletes data.</b><p>Normally a photo already on the server is recognised and skipped, so re-running is harmless. With this on, immich-go instead <b>deletes the server's copy and uploads the local file in its place</b>.</p><p>It requires the <code>asset.delete</code> scope on your key. Granting that scope means the key can permanently remove photos from your library — so leave it off the key unless you specifically need this.</p><p>Anything Immich added since the first import — an edit, a manual album, a favourite — belongs to the copy being deleted and goes with it. Only use this to deliberately replace files with better versions, and take a backup first.</p>", "h_pairs": "<b>Default: NoStack — both files upload separately.</b> Controls what happens when the same shot exists twice, as RAW and JPEG or as HEIC and JPEG.<p><b>KeepRaw</b> / <b>KeepJPG</b> upload only that format and discard the other — the discarded one never reaches Immich.</p><p><b>StackCover…</b> uploads both and stacks them in Immich, showing the named format on top. Nothing is lost and the timeline stays tidy. This is the safe choice if you want grouping.</p>", "h_partnerAlbum": "<b>Default: empty.</b> Puts photos that came from partner sharing into one album you name.<p>A tidy way to keep a partner's photos identifiable instead of scattered through the timeline. Only affects partner photos, and only if importing them is switched on.</p>", "h_peopleTag": "<b>Default: on.</b> Google stores the names you tagged people with in each photo's JSON file. This copies them across as Immich tags named <code>people/Name</code>.<p>These are tags, not Immich's face recognition. Immich still detects faces on its own; this just preserves the names Google already had.</p><p>Turn it off if your key lacks the scopes listed below.</p>", "h_preset": "<b>Default: Custom.</b> Fills in concurrency, timeout and grouping with the values immich-go's own guide recommends for a library of that size.<p>It only sets those fields — you can change anything afterwards, and doing so switches this back to Custom. Nothing here affects which photos are imported.</p>", "h_sessionTag": "<b>Default: off.</b> Tags everything from this one run with <code>{immich-go}/DATE TIME</code>.<p>The point is reversibility: if a run goes wrong, that tag selects exactly what it uploaded so you can review or remove it, without touching anything imported earlier.</p><p>Worth turning on for a first attempt on a big library.</p>", "h_skipVerifySSL": "<b>Default: off.</b> Stops checking whether the server's HTTPS certificate is valid.<p>Only turn this on for a self-signed certificate on a network you control. It disables the check that proves you are talking to your own server, so on any untrusted network someone in the middle could intercept the connection — <b>including your API key</b>.</p><p>You do not need this for a plain <code>http://</code> address on your LAN.</p>", "h_syncAlbums": "<b>Default: on.</b> Recreates every album from your takeout inside Immich and puts the photos back into it.<p><b>Turn it off</b> and the photos still upload, they just land in the main timeline with no albums. Nothing is lost — you can build albums later by hand.</p><p>Without the scopes listed below, the run stops with a 403.</p>", "h_tags": "<b>Default: empty.</b> Adds your own tags to every photo in this run.<p>Comma separated. A slash makes a hierarchy, so <code>migration/2015</code> creates <i>2015</i> nested under <i>migration</i>.</p><p>Purely additive — it never changes the photos, only labels them.</p>", "h_takeoutTag": "<b>Default: on.</b> Adds one tag, <code>{takeout}/takeout-DATE</code>, to everything from this export.<p>Useful later: it lets you find or remove exactly what came from this takeout, which matters if you import several exports over time.</p><p>Needs the scopes listed below.</p>", "h_timeZone": "<b>Default: your computer's time zone.</b> Used when a photo's date has no zone attached.<p>Set it if you imported from a machine in a different zone and photos land a few hours off. Format is <code>Area/City</code>, like <code>Asia/Riyadh</code> or <code>Europe/Paris</code>.</p>", "home_label": "主目录", "install_hint": "将与本机匹配的版本下载到 ~/.local/bin", "install_ok": "已安装 {v}", "installing": "正在下载…", "l_admin": "管理员密钥 — 可选", "l_album": "仅此相册", "l_burst": "连拍照片", "l_conc": "并行上传数", "l_daterange": "日期范围", "l_excext": "跳过这些扩展名", "l_heicjpeg": "HEIC + JPEG 组合", "l_incext": "仅这些扩展名", "l_key": "API 密钥", "l_partneralbum": "把伴侣的照片放入相册", "l_preset": "预设", "l_rawjpeg": "RAW + JPEG 组合", "l_server": "Immich 地址", "l_tags": "额外标签", "l_timeout": "请求超时", "l_type": "文件类型", "l_tz": "时区", "lede": "把 Google 相册的 Takeout 导入你自己的 Immich 服务器，保留日期、位置和相册。直接读取 zip 文件，无需解压、无需复制。", "log_wait": "等待开始…", "m_perms": "所需权限", "m_why": "为什么必须一次选全部分卷？", "mac_block": "<bdi>{n}</bdi> 被阻止 — macOS 不允许终端读取外置磁盘。<br>解决方法：打开 <span class='path'>System Settings → Privacy &amp; Security → Full Disk Access</span> 并启用 <bdi>Terminal</bdi>，然后退出并重新打开终端。", "n_accepted": "意外：请求被接受", "n_failed": "检测失败", "n_flag": "设置", "n_forbidden": "密钥没有此权限", "n_needs": "需要", "n_unauthorized": "密钥无效", "o_albums": "重建 Google 相册", "o_archived": "已归档的照片", "o_continue": "出错后继续", "o_continue_p": "一个坏文件不会中断整次上传", "o_epson": "Epson FastFoto 扫描件", "o_overwrite": "替换服务器上已有的照片", "o_partner": "伴侣的照片", "o_people": "从 JSON 读取人物标签", "o_session": "为本次运行的全部内容加标签", "o_ssl": "跳过证书校验", "o_takeout": "为本次 Takeout 添加标签", "o_trace": "记录每次 API 调用", "o_trashed": "回收站中的照片", "o_unmatched": "没有 JSON 文件的照片", "o_untitled": "无标题相册", "p_custom": "自定义", "p_large": "超过 10 万张", "p_medium": "1 万–10 万张", "p_slow": "网络慢或不稳定", "p_small": "少于 1 万张", "ph_admin": "没有就留空", "ph_key": "粘贴到这里", "pill_bin": "immich-go", "pill_conn": "连接", "pill_disk": "磁盘", "r_caution": "会改变导入的内容", "r_danger": "会删除数据", "r_done": "已成功完成", "r_dry": "试运行进行中…", "r_privacy": "安全或隐私风险", "r_stop": "已停止 · 代码 {c}", "r_up": "上传中…", "s1": "服务器与密钥", "s2": "归档文件", "s3": "选项", "s3sub": "每个选项及其权限", "s4": "执行", "sel": "已选 {n} 个", "sel_dir": "已选 {n} 个文件夹", "src_folder": "文件夹", "src_zip": "ZIP 文件", "st_avail": "{n} 个可用", "st_blocked": "被阻止", "st_failed": "失败", "st_incomplete": "不完整", "st_missing": "未安装", "st_none": "无", "st_ok": "正常", "st_ready": "就绪", "st_untested": "未测试", "t_details": "详情", "t_good": "连接正常，可检测的权限齐全。其余权限由试运行确认。", "t_hint": "<br>请检查地址和端口，通常是 2283。", "t_missing": "缺少 {n} 项权限。请在 Immich 中为密钥补上，或在第 3 步关闭需要它的选项。", "t_missing_short": "{n} 项权限中缺少 {m} 项", "t_ok_short": "{n} 项权限齐全", "t_unreach": "<b>无法连接到服务器。</b> ", "t_unver": "由试运行确认", "testing": "测试中…", "title": "Takeout 上传器", "v_all": "全部", "v_bad": "<b>{n} 个文件损坏或被截断：</b>{names}<br>请重新下载后再上传，否则会悄无声息地丢失照片。", "v_checking": "正在检查 {n} 个文件…", "v_good": "<b>所有归档完好</b> — {f} 个文件中共 {t} 个条目。可以上传了。", "v_image": "仅照片", "v_video": "仅视频", "w_aadd": "把照片加入相册", "w_about": "读取服务器版本", "w_acreate": "创建 Google 相册", "w_albums": "读取相册", "w_overwrite": "会删除服务器上的副本并重新上传，需要 <code>asset.delete</code> 权限。", "w_search": "上传前查找重复", "w_ssl": "仅适用于你信任的网络中的自签名证书。", "w_stats": "统计已有照片", "w_trace": "日志会包含请求详情，其中可能有个人数据。", "w_trashed": "这些是你在 Google 相册中删除的照片。", "w_unmatched": "它们没有日期、位置和相册，immich-go 只能退回到 EXIF 或文件名。", "w_update": "上传后设置日期和位置", "w_upload": "上传照片", "w_user": "读取账户信息"}};

/* ------------------------------------------------------------------ i18n */

var lang = pickLanguage();

/* Stored choice wins, then the browser's preference, then English.
   The parentheses matter: without them `a || b === 0 ? x : y` would test the
   whole disjunction rather than b. */
function pickLanguage() {
  var stored = localStorage.getItem("ig_lang");
  if (stored && TR[stored]) { return stored; }
  var nav = (navigator.language || "en").toLowerCase();
  for (var i = 0; i < LANGS.length; i++) {
    if (nav.indexOf(LANGS[i][0]) === 0) { return LANGS[i][0]; }
  }
  return "en";
}

/* Look up a string and substitute {placeholders}. Falls back to English so a
   partial translation degrades to a readable interface rather than blanks. */
function t(key, vars) {
  var s = (TR[lang] && TR[lang][key]);
  if (s === undefined) { s = TR.en[key]; }
  if (s === undefined) { return key; }
  if (vars) {
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) {
        s = s.split("{" + k + "}").join(vars[k]);
      }
    }
  }
  return s;
}

function applyLang() {
  document.documentElement.lang = lang;
  document.documentElement.dir = (RTL.indexOf(lang) > -1) ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach(function (el) {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-html]").forEach(function (el) {
    el.innerHTML = t(el.getAttribute("data-i18n-html"));
    appendMeta(el);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
    el.placeholder = t(el.getAttribute("data-i18n-ph"));
  });
  document.title = t("title") + " — Immich";
  paintIcons();
  paintCopy();
  if (lastEnv) { renderEnv(lastEnv); }
  if (curDir) { nav(curDir); }
  steps();
}

/* Appends the immich-go flag and required scopes to the foot of a help panel.
   Rebuilt on every language change because setting innerHTML wipes it. */
function appendMeta(el) {
  var flag = el.getAttribute("data-flag");
  if (!flag) { return; }
  var perms = (el.getAttribute("data-perms") || "").split(",").filter(Boolean);
  var html = '<div class="meta2"><span class="lbl">' + t("n_flag") + "</span>"
    + '<code class="tiny">' + flag + "</code>";
  if (perms.length) {
    html += '<span class="lbl">' + t("n_needs") + "</span>"
      + perms.map(function (p) { return '<code class="tiny">' + p + "</code>"; }).join(" ");
  }
  el.insertAdjacentHTML("beforeend", html + "</div>");
}

function buildLangSelect() {
  var sel = $("langSel");
  sel.innerHTML = LANGS.map(function (l) {
    return '<option value="' + l[0] + '"' + (l[0] === lang ? " selected" : "") + ">" + l[1] + "</option>";
  }).join("");
  sel.onchange = function () {
    lang = sel.value;
    localStorage.setItem("ig_lang", lang);
    applyLang();
  };
}

/* ----------------------------------------------------------------- theme */

/* Three states: auto follows the OS, light and dark are explicit. */
function applyTheme() {
  var m = localStorage.getItem("ig_theme") || "auto";
  if (m === "auto") { document.documentElement.removeAttribute("data-theme"); }
  else { document.documentElement.setAttribute("data-theme", m); }
  $("themeBtn").innerHTML = icon(m === "auto" ? "auto" : (m === "dark" ? "moon" : "sun"), 17);
}

function toggleTheme() {
  var m = localStorage.getItem("ig_theme") || "auto";
  localStorage.setItem("ig_theme", { auto: "light", light: "dark", dark: "auto" }[m]);
  applyTheme();
}

/* --------------------------------------------------------------- helpers */

var curDir = "", selected = {}, verified = {}, offset = 0, poller = null, lastEnv = null;

/* "zip" selects individual archive parts; "folder" selects one already extracted
   takeout directory. immich-go accepts either form as its source argument. */
var srcMode = localStorage.getItem("ig_src") || "zip";

function setSrc(mode) {
  srcMode = mode;
  localStorage.setItem("ig_src", mode);
  selected = {};
  verified = {};
  document.querySelectorAll("[data-src]").forEach(function (b) {
    b.className = "segbtn" + (b.getAttribute("data-src") === mode ? " on" : "");
  });
  $("verifyBtn").style.display = (mode === "zip") ? "" : "none";
  $("useDirBtn").style.display = (mode === "folder") ? "" : "none";
  $("folderHint").style.display = (mode === "folder") ? "" : "none";
  $("verifyOut").innerHTML = "";
  if (curDir) { nav(curDir); }
  steps();
}

/* Folder mode usually means "the directory I am looking at", so offer that
   directly instead of forcing a level up to tick its checkbox. */
function useCurrentDir() {
  selected = {};
  selected[curDir] = true;
  nav(curDir);
  steps();
}

function $(id) { return document.getElementById(id); }

/* Western digits in every language, never Eastern Arabic-Indic ones. */
function num(n) { return Number(n).toLocaleString("en-US"); }

function fmt(bytes) {
  if (!bytes) { return "0 B"; }
  var units = ["B", "KB", "MB", "GB", "TB"], i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return bytes.toFixed(i ? 1 : 0) + " " + units[i];
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
}

/* Everything the user tuned is remembered, so a re-run after an interruption
   does not mean setting fourteen switches again. */
function save() {
  ["server", "apiKey", "adminKey"].forEach(function (k) {
    localStorage.setItem("ig_" + k, $(k).value);
  });
  var state = {};
  CHECKS.forEach(function (id) { state[id] = $(id).checked; });
  TEXTS.forEach(function (id) { state[id] = $(id).value; });
  state.preset = $("preset").value;
  localStorage.setItem("ig_opts", JSON.stringify(state));
}

function load() {
  ["server", "apiKey", "adminKey"].forEach(function (k) {
    var v = localStorage.getItem("ig_" + k);
    if (v) { $(k).value = v; }
  });
  var raw = localStorage.getItem("ig_opts");
  if (!raw) { return; }
  try {
    var state = JSON.parse(raw);
    CHECKS.forEach(function (id) {
      if (typeof state[id] === "boolean") { $(id).checked = state[id]; }
    });
    TEXTS.forEach(function (id) {
      if (typeof state[id] === "string") { $(id).value = state[id]; }
    });
    if (state.preset) { $("preset").value = state.preset; }
  } catch (err) { /* stored state from an older build; ignore it */ }
}

function showRes(id, cls, html) {
  $(id).innerHTML = '<div class="res ' + cls + '">' + html + "</div>";
}

/* The server returns error codes, not sentences, so they can be translated. */
function srvErr(d) { return t("e_" + d.error, { arg: esc(d.arg || "") }); }

/* Light up each step as its precondition is met. */
function steps() {
  var n = Object.keys(selected).length;
  var connOk = $("d-conn").className.indexOf("on") > -1;
  $("s1").className = "step" + (connOk ? " done" : " active");
  $("s2").className = "step" + (n ? " done" : "");
  var allOk = n > 0 && (srcMode === "folder"
    || Object.keys(selected).every(function (p) { return verified[p] === true; }));
  $("s3").className = "step" + (allOk ? " done" : "");
  $("selInfo").textContent = n ? t(srcMode === "folder" ? "sel_dir" : "sel", { n: num(n) }) : "";
}

/* ------------------------------------------------------------ environment */

function loadEnv() {
  fetch("/env").then(function (r) { return r.json(); }).then(function (d) {
    lastEnv = d;
    renderEnv(d);
    if (!curDir) { nav(d.home); }
  });
}

function renderEnv(d) {
  $("d-bin").className = "dot " + (d.binary ? "on" : "off");
  $("v-bin").textContent = t(d.binary ? "st_ready" : "st_missing");

  var ext = d.volumes.filter(function (v) { return !v.isHome; });
  var blocked = ext.filter(function (v) { return !v.readable; });
  $("d-disk").className = "dot " + (blocked.length ? "warn" : (ext.length ? "on" : ""));
  $("v-disk").textContent = blocked.length ? t("st_blocked")
    : (ext.length ? t("st_avail", { n: num(ext.length) }) : t("st_none"));

  var conn = $("v-conn");
  if (!conn.dataset.set) { conn.textContent = t("st_untested"); }

  $("disks").innerHTML = d.volumes.map(function (v) {
    var name = v.isHome ? t("home_label") : v.name;
    return '<button type="button" class="sm" data-nav="' + esc(v.path) + '">'
      + icon(v.readable ? (v.isHome ? "folder" : "disk") : "lock", 15)
      + "<span>" + esc(name) + "</span>"
      + '<span class="size">' + fmt(v.free) + "</span></button>";
  }).join(" ");

  if (blocked.length) {
    showRes("verifyOut", "warn", t("mac_block", { n: esc(blocked[0].name) }));
  }

  /* immich-go is the only prerequisite the machine may be missing, so offer to
     fetch it rather than sending the reader off to a releases page. */
  var bar = $("installBar");
  if (d.binary) { bar.innerHTML = ""; return; }
  bar.innerHTML = '<div class="res warn" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
    + '<button type="button" class="go" id="installBtn">' + icon("upload", 17)
    + "<span>" + t("b_install") + "</span></button>"
    + '<span style="font-size:12.5px">' + t("install_hint") + "</span></div>";
  $("installBtn").onclick = installTool;
}

function installTool() {
  var btn = $("installBtn");
  btn.disabled = true;
  btn.querySelector("span").textContent = t("installing");
  fetch("/install-tool", { method: "POST", body: "{}" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.error) {
        showRes("installBar", "bad", t("e_" + d.error, { arg: esc(d.arg || "") }));
        return;
      }
      showRes("installBar", "good", t("install_ok", { v: esc(d.version || d.tag || "") }));
      loadEnv();
    })
    .catch(function () {
      btn.disabled = false;
      btn.querySelector("span").textContent = t("b_install");
    });
}

/* -------------------------------------------------------------- browsing */

function nav(dir) {
  fetch("/browse?dir=" + encodeURIComponent(dir))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.error) {
        $("frames").innerHTML = '<div class="empty">' + srvErr(d) + "</div>";
        return;
      }
      curDir = d.path;
      $("crumb").innerHTML = "<bdi>" + esc(d.path) + "</bdi>  ·  "
        + '<span class="size">' + fmt(d.free) + "</span> " + esc(t("free"));

      var h = "";
      d.dirs.forEach(function (x) {
        h += '<div class="frame dir"><span class="mark">' + icon("folder", 16) + "</span>"
          + (srcMode === "folder"
              ? '<input type="checkbox" data-pick="' + esc(x.path) + '"' + (selected[x.path] ? " checked" : "") + ">"
              : "")
          + '<span class="nm" data-nav="' + esc(x.path) + '">' + esc(x.name) + "</span></div>";
      });
      if (srcMode === "folder") { steps(); return void ($("frames").innerHTML = h || '<div class="empty">' + t("e_none") + "</div>"); }
      d.zips.forEach(function (z) {
        var v = verified[z.path];
        var cls = v === true ? " ok" : (v === false ? " bad" : "");
        var mark = v === true ? icon("ok", 16) : (v === false ? icon("alert", 16) : icon("zip", 16));
        h += '<div class="frame' + cls + '"><span class="mark">' + mark + "</span>"
          + '<input type="checkbox" data-pick="' + esc(z.path) + '"' + (selected[z.path] ? " checked" : "") + ">"
          + '<span class="nm" data-lbl="' + esc(z.path) + '">' + esc(z.name) + "</span>"
          + '<span class="meta">' + fmt(z.size) + "</span></div>";
      });
      $("frames").innerHTML = h || '<div class="empty">' + t("e_none") + "</div>";
      steps();
    });
}

function goUp() {
  fetch("/browse?dir=" + encodeURIComponent(curDir))
    .then(function (r) { return r.json(); })
    .then(function (d) { if (d.parent) { nav(d.parent); } });
}

/* --------------------------------------------------------------- copying */

/* localhost counts as a secure context, so the async clipboard API is
   available; the textarea path covers browsers where it is not. */
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise(function (resolve, reject) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(ta);
    if (ok) { resolve(); } else { reject(); }
  });
}

/* Swap the icon to a tick and the label to "copied" for a moment, then restore
   both. Only the two inner spans are touched, never the button's own markup. */
function flashCopied(btn) {
  if (btn.dataset.flashing) { return; }
  btn.dataset.flashing = "1";
  var slot = btn.querySelector(".ci");
  var name = btn.querySelector(".cn");
  var prev = name ? name.textContent : null;
  if (slot) { slot.innerHTML = icon("check", 14); }
  if (name) { name.textContent = t("copied"); }
  btn.classList.add("copied");
  setTimeout(function () {
    btn.classList.remove("copied");
    if (slot) { slot.innerHTML = icon("copy", 14); }
    if (name && prev !== null) { name.textContent = prev; }
    delete btn.dataset.flashing;
  }, 1100);
}

/* Fill in the copy affordances after any panel is (re)rendered. */
function paintCopy() {
  document.querySelectorAll(".chip .ci").forEach(function (el) {
    if (!el.innerHTML) { el.innerHTML = icon("copy", 14); }
  });
  document.querySelectorAll(".copyhint").forEach(function (el) {
    el.textContent = t("copy_hint");
  });
}

/* Delegated so rows can be re-rendered freely without rebinding handlers. */
document.addEventListener("click", function (e) {
  var chip = e.target.closest && e.target.closest("[data-copy]");
  if (chip) {
    copyText(chip.getAttribute("data-copy"))
      .then(function () { flashCopied(chip); })
      .catch(function () { chip.title = "copy failed"; });
    return;
  }
  var help = e.target.closest && e.target.closest("[data-help]");
  if (help) {
    var panel = $(help.getAttribute("data-help"));
    var open = panel.classList.toggle("open");
    help.setAttribute("aria-expanded", open ? "true" : "false");
    return;
  }
  var src = e.target.closest && e.target.closest("[data-src]");
  if (src) { setSrc(src.getAttribute("data-src")); return; }
  var n = e.target.closest && e.target.closest("[data-nav]");
  if (n) { nav(n.getAttribute("data-nav")); return; }
  var l = e.target.closest && e.target.closest("[data-lbl]");
  if (l) {
    var p = l.getAttribute("data-lbl");
    var cb = document.querySelector('[data-pick="' + CSS.escape(p) + '"]');
    if (cb) { cb.checked = !cb.checked; pick(p, cb.checked); }
  }
});

document.addEventListener("change", function (e) {
  if (e.target.hasAttribute && e.target.hasAttribute("data-pick")) {
    pick(e.target.getAttribute("data-pick"), e.target.checked);
    return;
  }
  /* Touching any tuned control means the choice is no longer a stock preset. */
  var id = e.target.id;
  if (id && id !== "preset" && (CHECKS.indexOf(id) > -1 || TEXTS.indexOf(id) > -1)) {
    $("preset").value = "custom";
    save();
  }
});

function pick(path, on) {
  if (on) { selected[path] = true; } else { delete selected[path]; }
  steps();
}

function selectAll(on) {
  document.querySelectorAll("[data-pick]").forEach(function (cb) {
    cb.checked = on;
    pick(cb.getAttribute("data-pick"), on);
  });
}

/* ------------------------------------------------------------- preflight */

var PERM_WHY = {
  "user.read": "w_user", "server.about": "w_about", "asset.statistics": "w_stats",
  "album.read": "w_albums", "asset.read": "w_search", "asset.upload": "w_upload",
  "asset.update": "w_update", "album.create": "w_acreate", "albumAsset.create": "w_aadd"
};

function test() {
  save();
  $("testBtn").disabled = true;
  $("testState").textContent = t("testing");
  $("testOut").innerHTML = "";

  fetch("/test", {
    method: "POST",
    body: JSON.stringify({ server: $("server").value, apiKey: $("apiKey").value })
  }).then(function (r) { return r.json(); }).then(function (d) {
    $("testBtn").disabled = false;
    $("testState").textContent = "";
    var conn = $("v-conn");
    conn.dataset.set = "1";

    if (d.error) { showRes("testOut", "bad", srvErr(d)); return; }
    if (!d.reach) {
      $("d-conn").className = "dot off";
      conn.textContent = t("st_failed");
      showRes("testOut", "bad", t("t_unreach") + "<bdi>" + esc(d.note) + "</bdi>" + t("t_hint"));
      steps();
      return;
    }

    var missing = d.perms.filter(function (p) { return !p.ok; });
    var total = d.perms.length;

    /* The verdict is one line. Anyone who wants the per-scope breakdown can
       open it; most people only need to see that the light went green. */
    var rows = d.perms.map(function (p) {
      return '<div class="chk ' + (p.ok ? "y" : "n2") + '">'
        + '<span class="p">' + (p.ok ? "✓ " : "✕ ") + p.perm + "</span>"
        + '<span class="w">' + t(PERM_WHY[p.perm]) + "</span>"
        + '<span class="n">' + (p.note ? t("n_" + p.note) : "") + "</span></div>";
    }).join("");

    $("testOut").innerHTML =
      '<div class="res ' + (missing.length ? "bad" : "good") + '">'
      + (missing.length
          ? t("t_missing_short", { m: num(missing.length), n: num(total) })
          : t("t_ok_short", { n: num(total) }))
      + "</div>"
      + '<details class="grp" style="margin-top:10px">'
      + "<summary>" + icon("info", 16) + "<span>" + t("t_details") + "</span></summary>"
      + '<div class="inner" style="padding:0"><div class="checks" style="border:0;border-radius:0">'
      + rows + "</div></div></details>";

    $("d-conn").className = "dot " + (missing.length ? "warn" : "on");
    conn.textContent = missing.length ? t("st_incomplete") : t("st_ok");
    steps();
  });
}

/* ------------------------------------------------------------- integrity */

function verify() {
  var zips = Object.keys(selected);
  if (!zips.length) { showRes("verifyOut", "warn", t("e_no_zips")); return; }
  showRes("verifyOut", "", t("v_checking", { n: num(zips.length) }));

  fetch("/verify", { method: "POST", body: JSON.stringify({ zips: zips }) })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.error) { showRes("verifyOut", "bad", srvErr(d)); return; }
      var bad = [], total = 0;
      d.results.forEach(function (x, i) {
        verified[zips[i]] = x.ok;
        total += x.entries;
        if (!x.ok) { bad.push(x); }
      });
      showRes("verifyOut", bad.length ? "bad" : "good", bad.length
        ? t("v_bad", {
            n: num(bad.length),
            names: bad.map(function (x) { return "<bdi>" + esc(x.name) + "</bdi>"; }).join(", ")
          })
        : t("v_good", { t: num(total), f: num(d.results.length) }));
      nav(curDir);
    });
}

/* ------------------------------------------------------------------- run */

/* Presets mirror the configurations immich-go's best-practices guide
   recommends per library size, so the common cases need no tuning. */
var PRESETS = {
  small:  { concurrent: 12, clientTimeout: "",    manageRawJpeg: "StackCoverRaw",
            manageHeicJpeg: "StackCoverJPG", manageBurst: "Stack", sessionTag: false },
  medium: { concurrent: 8,  clientTimeout: "",    manageRawJpeg: "StackCoverRaw",
            manageHeicJpeg: "NoStack",       manageBurst: "Stack", sessionTag: false },
  large:  { concurrent: 4,  clientTimeout: "60m", manageRawJpeg: "NoStack",
            manageHeicJpeg: "NoStack",       manageBurst: "NoStack", sessionTag: true },
  slow:   { concurrent: 2,  clientTimeout: "120m", manageRawJpeg: "NoStack",
            manageHeicJpeg: "NoStack",       manageBurst: "NoStack", sessionTag: true }
};

function applyPreset() {
  var p = PRESETS[$("preset").value];
  if (!p) { return; }
  $("concurrent").value = p.concurrent;
  $("clientTimeout").value = p.clientTimeout;
  $("manageRawJpeg").value = p.manageRawJpeg;
  $("manageHeicJpeg").value = p.manageHeicJpeg;
  $("manageBurst").value = p.manageBurst;
  $("sessionTag").checked = p.sessionTag;
  $("continueOnError").checked = true;
}

var CHECKS = ["continueOnError", "syncAlbums", "peopleTag", "takeoutTag",
              "includeArchived", "includePartner", "includeTrashed", "includeUnmatched",
              "includeUntitled", "epsonFastFoto", "sessionTag", "overwrite",
              "skipVerifySSL", "apiTrace"];
var TEXTS = ["includeType", "fromAlbum", "partnerAlbum", "dateRange", "includeExt",
             "excludeExt", "timeZone", "clientTimeout", "tags", "concurrent",
             "manageRawJpeg", "manageHeicJpeg", "manageBurst"];

function start(dry) {
  save();
  $("err").innerHTML = "";
  var body = {
    server: $("server").value, apiKey: $("apiKey").value, adminKey: $("adminKey").value,
    dryRun: dry, zips: Object.keys(selected)
  };
  CHECKS.forEach(function (id) { body[id] = $(id).checked; });
  TEXTS.forEach(function (id) { body[id] = $(id).value; });

  fetch("/start", { method: "POST", body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (!res.ok) { showRes("err", "bad", srvErr(res.d)); return; }
      offset = 0;
      $("log").textContent = "";
      $("startBtn").disabled = true;
      $("dryBtn").disabled = true;
      $("stopBtn").disabled = false;
      $("runState").textContent = t(dry ? "r_dry" : "r_up");
      if (poller) { clearInterval(poller); }
      poller = setInterval(poll, 1000);
      poll();
    });
}

function stop() { fetch("/stop", { method: "POST", body: "{}" }); }

function poll() {
  fetch("/logs?offset=" + offset).then(function (r) { return r.json(); }).then(function (d) {
    if (d.lines.length) {
      var el = $("log");
      var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
      el.textContent += d.lines.join("\\n") + "\\n";
      if (atBottom) { el.scrollTop = el.scrollHeight; }
    }
    offset = d.offset;
    if (!d.running) {
      if (poller) { clearInterval(poller); poller = null; }
      $("startBtn").disabled = false;
      $("dryBtn").disabled = false;
      $("stopBtn").disabled = true;
      $("runState").textContent = d.exitCode === 0 ? t("r_done")
        : (d.exitCode === null ? "" : t("r_stop", { c: d.exitCode }));
    }
  });
}

paintIcons();
applyTheme();
buildLangSelect();
applyLang();
setSrc(srcMode);
load();
loadEnv();
poll();
</script>
</body>
</html>
"""


def main():
    print("")
    print("  Immich Takeout Uploader")
    print("  " + "-" * 44)
    print("  host      : %s (%s)" % (os.uname()[1], "macOS" if IS_MAC else "Linux"))
    print("  immich-go : %s" % (find_binary() or "NOT INSTALLED"))
    print("  open      : http://127.0.0.1:%d/" % PORT)
    print("  stop      : Ctrl+C")
    print("")
    # Bound to loopback only: the API key travels through this page.
    # For remote access use an SSH tunnel, never 0.0.0.0.
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if IS_MAC and os.environ.get("IG_NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
