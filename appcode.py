"""File Handling - browse your computer and create, open, edit, rename and delete text files.

Run:  python file_handling.py      (it opens http://127.0.0.1:8765 in your browser)
Only the Python standard library is used. The server listens on your own computer only,
and every request needs a secret token, so other websites and other machines can't use it.
You can open and change any file that your own user account is allowed to change.
"""

import json
import os
import secrets
import string
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOKEN = secrets.token_urlsafe(16)
MAX_BODY = 8 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024
MAX_LIST = 2000


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- file operations ----------
def norm(path):
    path = (path or "").strip()
    if not path:
        raise ApiError("Enter a path.")
    return Path(os.path.abspath(os.path.expanduser(path)))


def valid_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\\x00"):
        raise ApiError("Use a plain name, without slashes.")
    return name


def existing_file(path):
    p = norm(path)
    if not p.is_file():
        raise ApiError("No such file exists.", 404)
    return p


def eol_of(value):
    return value if value in ("\n", "\r\n") else "\n"


def roots():
    if os.name == "nt":
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    return ["/"]


def browse(path, hidden):
    p = norm(path) if path else Path.home()
    if p.is_file():
        return {"is_file": True, "path": str(p), "dir": str(p.parent)}
    if not p.is_dir():
        raise ApiError("That folder doesn't exist.", 404)
    entries, truncated = [], False
    try:
        with os.scandir(p) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir()
                    size = 0 if is_dir else e.stat().st_size
                except OSError:
                    continue
                entries.append({"name": e.name, "dir": is_dir, "size": size, "path": str(p / e.name)})
                if len(entries) >= MAX_LIST:
                    truncated = True
                    break
    except PermissionError:
        raise ApiError("You don't have permission to open this folder.", 403)
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "home": str(Path.home()), "roots": roots(),
            "entries": entries, "truncated": truncated}


def read_file(path):
    p = existing_file(path)
    if p.stat().st_size > MAX_FILE:
        raise ApiError("That file is larger than 5 MB, so it can't be edited here.")
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        raise ApiError("That isn't a plain text file, so it can't be opened here.")
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError("That file isn't UTF-8 text, so it can't be opened safely here.")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return {"content": raw.replace("\r\n", "\n"), "eol": eol}


def write_text(p, mode, text, eol):
    text = text.replace("\r\n", "\n").replace("\n", eol)
    with open(p, mode, encoding="utf-8", newline="") as fs:
        fs.write(text)


def create_file(directory, name, content=""):
    d = norm(directory)
    if not d.is_dir():
        raise ApiError("That folder doesn't exist.", 404)
    p = d / valid_name(name)
    try:
        write_text(p, "x", content, "\n")
    except FileExistsError:
        raise ApiError("A file with that name already exists.", 409)
    return {"ok": True, "path": str(p)}


def make_dir(directory, name):
    d = norm(directory)
    try:
        (d / valid_name(name)).mkdir()
    except FileExistsError:
        raise ApiError("A folder with that name already exists.", 409)
    except FileNotFoundError:
        raise ApiError("That folder doesn't exist.", 404)
    return {"ok": True}


def update_file(path, mode, content, eol):
    p, eol = existing_file(path), eol_of(eol)
    if mode == "overwrite":
        write_text(p, "w", content, eol)
    elif mode == "append":
        size = p.stat().st_size
        needs_break = False
        if size:
            with open(p, "rb") as fs:
                fs.seek(-1, os.SEEK_END)
                needs_break = fs.read(1) != b"\n"
        write_text(p, "a", (eol if needs_break else "") + content, eol)
    else:
        raise ApiError("Unknown update mode.")
    return {"ok": True}


def rename_file(path, new_name):
    p = existing_file(path)
    target = p.with_name(valid_name(new_name))
    if target.exists() and not (target.name != p.name and target.samefile(p)):
        raise ApiError("A file with that name already exists.", 409)
    p.rename(target)
    return {"ok": True, "path": str(target)}


def delete_file(path):
    existing_file(path).unlink()
    return {"ok": True}


POST_ROUTES = {
    "create": lambda d: create_file(d.get("dir"), d.get("name"), d.get("content", "")),
    "mkdir": lambda d: make_dir(d.get("dir"), d.get("name")),
    "update": lambda d: update_file(d.get("path"), d.get("mode"), d.get("content", ""), d.get("eol")),
    "rename": lambda d: rename_file(d.get("path"), d.get("new_name")),
    "delete": lambda d: delete_file(d.get("path")),
}


# ---------- tiny local web server ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _run(self, fn):
        try:
            self._send(200, fn())
        except ApiError as err:
            self._send(err.status, {"error": str(err)})
        except PermissionError:
            self._send(403, {"error": "You don't have permission to do that."})
        except FileNotFoundError:
            self._send(404, {"error": "That file or folder no longer exists."})
        except OSError as err:
            self._send(500, {"error": f"Couldn't complete that: {err.strerror or err}"})

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "Forbidden"})
        url = urlparse(self.path)
        if url.path == "/":
            page = PAGE.replace("__TOKEN__", TOKEN).encode()
            return self._send(200, page, "text/html; charset=utf-8")
        if not self._authorized():
            return self._send(403, {"error": "Forbidden"})
        q = parse_qs(url.query)
        if url.path == "/api/browse":
            return self._run(lambda: browse(q.get("path", [""])[0], q.get("hidden", ["0"])[0] == "1"))
        if url.path == "/api/file":
            return self._run(lambda: read_file(q.get("path", [""])[0]))
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._allowed() or not self._authorized():
            return self._send(403, {"error": "Forbidden"})
        action = POST_ROUTES.get(urlparse(self.path).path.removeprefix("/api/"))
        if action is None:
            return self._send(404, {"error": "Not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "That text is too large."})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "Bad request."})
        self._run(lambda: action(data))


def main():
    server = None
    for port in list(range(8765, 8776)) + [0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"File Handling is running at {url}\nPress Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Handling</title>
<style>
:root{--desk:#DCE5E0;--desk2:#CCD8D1;--paper:#FDFEFB;--ink:#17302B;--muted:#5B746D;--line:#B9C9C1;
--accent:#2B50E8;--accent-d:#1F3FC4;--mark:#FFE27A;--danger:#C8321F;--ok:#1E7A4F;--lh:30px}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--desk);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column}
header{padding:24px 32px 6px;display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
h1{font:600 36px/1 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;letter-spacing:-.02em;margin:0}
.folder{color:var(--muted);font:13px ui-monospace,Menlo,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:62vw}
main{flex:1;min-height:0;display:grid;grid-template-columns:370px 1fr;gap:26px;padding:16px 32px 30px}
.drawer{display:flex;flex-direction:column;min-height:0}
.row{display:flex;gap:8px;margin-bottom:10px;align-items:center}
.field{flex:1;min-width:0;font:inherit;padding:9px 12px;border:1.5px solid var(--line);border-radius:8px;background:var(--paper);color:var(--ink)}
#path{font:13px ui-monospace,Menlo,Consolas,monospace}
select.field{flex:none;max-width:84px;padding:9px 6px}
.small{padding:8px 12px}
.list{overflow:auto;display:flex;flex-direction:column;gap:7px;padding:2px 4px 2px 0;flex:1;min-height:0}
.item{all:unset;box-sizing:border-box;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 14px;border-left:7px solid var(--tab,var(--muted));border-radius:4px 12px 12px 4px;background:var(--paper);box-shadow:0 1px 0 var(--line);transition:transform .12s,background .12s}
.item:hover{transform:translateX(3px)}
.item:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.item.on{background:var(--mark);border-left-color:var(--ink)}
.item.dir{background:#EEF3F0;--tab:#7C8F88}
.item.dir .n::before{content:"\25B8  ";color:var(--muted)}
.item .n{font:600 14px ui-monospace,Menlo,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .s{color:var(--muted);font-size:12px;flex:none}
.none{color:var(--muted);margin:18px 4px}
.foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:12px 4px 0;color:var(--muted);font-size:13px}
.foot label{display:flex;gap:6px;align-items:center;cursor:pointer}
.sheet{background:var(--paper);border-radius:6px;box-shadow:0 1px 0 var(--line),0 18px 36px -18px rgba(23,48,43,.45);display:flex;flex-direction:column;min-height:0}
.sheet-head{display:flex;align-items:center;gap:14px;padding:20px 30px 14px 72px;min-height:68px}
.sheet-head h2{margin:0;font:600 24px/1.1 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-size:12px;font-weight:600;padding:3px 11px;border-radius:99px;display:none;flex:none}
.pill.saved{display:inline-block;background:#D5EFE1;color:var(--ok)}
.pill.unsaved{display:inline-block;background:var(--mark);color:#5B4600}
.body{position:relative;flex:1;min-height:0;display:flex}
#editor{flex:1;resize:none;border:0;outline:0;color:var(--ink);font:15px/var(--lh) ui-monospace,Menlo,Consolas,monospace;padding:0 30px 0 72px;white-space:pre;overflow:auto;
background:linear-gradient(90deg,transparent 54px,#E9A79D 54px,#E9A79D 56px,transparent 56px),repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--lh) - 1px),#D3E0F4 calc(var(--lh) - 1px),#D3E0F4 var(--lh));background-attachment:scroll,local}
#editor:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.empty{position:absolute;inset:0;background:var(--paper);display:flex;flex-direction:column;align-items:flex-start;justify-content:center;padding:0 72px}
.empty h2{margin:0 0 8px;font:600 30px/1.1 "Iowan Old Style",Palatino,Georgia,serif}
.empty p{margin:0 0 20px;color:var(--muted)}
.sheet-foot{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 30px 18px 72px;border-top:1.5px solid var(--line)}
.acts{display:flex;gap:10px;flex-wrap:wrap}
#stats{color:var(--muted);font-size:13px}
.btn{font:600 14px system-ui,sans-serif;padding:10px 18px;border-radius:8px;border:1.5px solid var(--ink);background:transparent;color:var(--ink);cursor:pointer;transition:background .12s,color .12s;white-space:nowrap}
.btn:hover{background:var(--desk)}
.btn:focus-visible,.field:focus-visible,dialog input:focus-visible,dialog textarea:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:var(--accent-d)}
.btn.danger{color:var(--danger);border-color:var(--danger)}
.btn.danger:hover{background:#F8E1DC}
.btn.danger-solid{background:var(--danger);border-color:var(--danger);color:#fff}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn:disabled:hover{background:transparent}
#toast{position:fixed;left:50%;top:22px;transform:translate(-50%,-14px);background:var(--ink);color:var(--paper);padding:11px 20px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;max-width:90vw;z-index:5}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{background:var(--danger)}
dialog{border:0;border-radius:10px;padding:26px;background:var(--paper);color:var(--ink);width:min(440px,92vw);box-shadow:0 30px 60px -20px rgba(0,0,0,.5)}
dialog::backdrop{background:rgba(23,48,43,.5)}
dialog h3{margin:0 0 6px;font:600 22px/1.2 "Iowan Old Style",Palatino,Georgia,serif}
dialog p{margin:0 0 16px;color:var(--muted);overflow-wrap:anywhere}
dialog input,dialog textarea{width:100%;font:15px ui-monospace,Menlo,Consolas,monospace;padding:10px 12px;border:1.5px solid var(--line);border-radius:8px;background:#fff;color:var(--ink)}
dialog textarea{min-height:110px;resize:vertical}
.dbtns{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
@media (max-width:860px){body{height:auto}main{grid-template-columns:1fr;padding:12px 16px 24px}header{padding:20px 16px 4px}.list{max-height:260px;flex:none}.sheet{min-height:460px}.sheet-head,.sheet-foot{padding-left:40px}.empty{padding:0 40px}
#editor{padding-left:40px;background:repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--lh) - 1px),#D3E0F4 calc(var(--lh) - 1px),#D3E0F4 var(--lh));background-attachment:local}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<header><h1>File Handling</h1><span class="folder" id="folder"></span></header>
<main>
  <section class="drawer" aria-label="Files and folders">
    <div class="row">
      <select class="field" id="roots" aria-label="Drive" hidden></select>
      <button class="btn small" id="up" title="Go up one folder">Up</button>
      <button class="btn small" id="home" title="Go to your home folder">Home</button>
      <input id="path" class="field" type="text" spellcheck="false" aria-label="Folder path. Press Enter to go there, or paste a file path to open it">
    </div>
    <div class="row">
      <input id="search" class="field" type="search" placeholder="Filter this folder" aria-label="Filter this folder">
      <button class="btn primary small" id="new">New file</button>
      <button class="btn small" id="newdir">New folder</button>
    </div>
    <div class="list" id="list"></div>
    <div class="foot"><span id="count"></span><label><input type="checkbox" id="hidden"> Show hidden</label></div>
  </section>
  <section class="sheet" aria-label="Editor">
    <div class="sheet-head"><h2 id="title"></h2><span class="pill" id="pill"></span></div>
    <div class="body">
      <textarea id="editor" spellcheck="false" wrap="off" aria-label="File content"></textarea>
      <div class="empty" id="empty">
        <h2>Nothing open yet</h2>
        <p>Pick any file from your computer in the list, or paste its full path above.</p>
        <button class="btn primary" id="new2">New file</button>
      </div>
    </div>
    <div class="sheet-foot">
      <div class="acts">
        <button class="btn primary" id="save">Save changes</button>
        <button class="btn" id="append">Append text</button>
        <button class="btn" id="rename">Rename</button>
        <button class="btn danger" id="del">Delete</button>
      </div>
      <span id="stats"></span>
    </div>
  </section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<dialog id="dlg">
  <h3 id="dt"></h3><p id="dm"></p>
  <input id="din" type="text" autocomplete="off"><textarea id="dta"></textarea>
  <div class="dbtns"><button class="btn" id="dcancel">Cancel</button><button class="btn primary" id="dok">OK</button></div>
</dialog>
<script>
const T = "__TOKEN__";
const $ = s => document.querySelector(s);
const state = {dir: "", parent: null, home: "", entries: [], truncated: false, cur: null, eol: "\n", dirty: false, q: "", hidden: false};
const baseName = p => p.split(/[\\/]/).filter(Boolean).pop() || p;
const store = {
  get() { try { return localStorage.getItem("fh.dir") || ""; } catch (e) { return ""; } },
  set(v) { try { localStorage.setItem("fh.dir", v); } catch (e) {} }
};

async function api(path, body) {
  const r = await fetch("/api/" + path, {
    method: body ? "POST" : "GET",
    headers: {"X-Token": T, "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || "Something went wrong.");
  return d;
}

let tt;
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(tt);
  tt = setTimeout(() => t.className = "", 3400);
}

function ask({title, msg = "", field = null, value = "", ok = "OK", danger = false}) {
  return new Promise(resolve => {
    const d = $("#dlg"), inp = $("#din"), ta = $("#dta"), okb = $("#dok");
    $("#dt").textContent = title;
    $("#dm").textContent = msg;
    inp.hidden = ta.hidden = true;
    const f = field === "multi" ? ta : field ? inp : null;
    if (f) { f.hidden = false; f.value = value; }
    okb.textContent = ok;
    okb.className = "btn " + (danger ? "danger-solid" : "primary");
    let done = false;
    const finish = v => { if (done) return; done = true; d.close(); resolve(v); };
    okb.onclick = () => finish(f ? f.value : true);
    $("#dcancel").onclick = () => finish(f ? null : false);
    d.oncancel = e => { e.preventDefault(); finish(f ? null : false); };
    d.onkeydown = e => {
      if (e.key === "Enter" && (f !== ta || e.ctrlKey || e.metaKey)) { e.preventDefault(); okb.click(); }
    };
    d.showModal();
    if (f) { f.focus(); if (f.select) f.select(); } else okb.focus();
  });
}

const act = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };
const tabColor = ext => ({txt: "#2B50E8", md: "#7A3FE0", py: "#1E7A4F", csv: "#D9791A", json: "#C2398B", js: "#B58900", html: "#D9481F"})[ext] || "#5B746D";
const sizeText = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";

function render() {
  const list = $("#list");
  list.replaceChildren();
  const q = state.q.toLowerCase();
  const shown = state.entries.filter(e => e.name.toLowerCase().includes(q));
  const dirs = state.entries.filter(e => e.dir).length, files = state.entries.length - dirs;
  const part = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  $("#count").textContent = (q ? shown.length + " of " : "") + part(dirs, "folder") + ", " + part(files, "file") + (state.truncated ? " (first 2000)" : "");
  if (!shown.length) {
    const p = document.createElement("p");
    p.className = "none";
    p.textContent = q ? "Nothing here matches your filter." : "This folder is empty.";
    list.append(p);
  }
  for (const e of shown) {
    const b = document.createElement("button");
    b.className = "item" + (e.dir ? " dir" : "") + (state.cur === e.path ? " on" : "");
    if (!e.dir) b.style.setProperty("--tab", tabColor(e.name.split(".").pop().toLowerCase()));
    const n = document.createElement("span"); n.className = "n"; n.textContent = e.name; n.title = e.path;
    const s = document.createElement("span"); s.className = "s"; s.textContent = e.dir ? "folder" : sizeText(e.size);
    b.append(n, s);
    b.onclick = act(() => e.dir ? browse(e.path) : openFile(e.path));
    list.append(b);
  }
}

async function browse(path) {
  let d = await api("browse?path=" + encodeURIComponent(path || "") + "&hidden=" + (state.hidden ? 1 : 0));
  const fileToOpen = d.is_file ? d.path : null;
  if (fileToOpen) d = await api("browse?path=" + encodeURIComponent(d.dir) + "&hidden=" + (state.hidden ? 1 : 0));
  Object.assign(state, {dir: d.path, parent: d.parent, home: d.home, entries: d.entries, truncated: d.truncated});
  $("#path").value = d.path;
  $("#up").disabled = !d.parent;
  const sel = $("#roots");
  sel.hidden = d.roots.length < 2;
  if (!sel.hidden) {
    sel.replaceChildren(...d.roots.map(r => new Option(r, r)));
    const mine = d.roots.find(r => d.path.toUpperCase().startsWith(r.toUpperCase()));
    if (mine) sel.value = mine;
  }
  store.set(d.path);
  $("#search").value = ""; state.q = "";
  render();
  if (fileToOpen) await openFile(fileToOpen);
}

function updateStats() {
  const v = $("#editor").value;
  const lines = v ? v.split("\n").length : 0;
  $("#stats").textContent = state.cur ? lines + " lines · " + v.length + " characters" : "";
}

function setDirty(v) {
  state.dirty = v;
  const p = $("#pill");
  p.className = "pill" + (state.cur ? (v ? " unsaved" : " saved") : "");
  p.textContent = v ? "Unsaved changes" : "Saved";
}

function updateSheet() {
  const open = !!state.cur;
  $("#title").textContent = open ? baseName(state.cur) : "";
  $("#folder").textContent = open ? state.cur : "";
  $("#folder").title = open ? state.cur : "";
  $("#empty").hidden = open;
  for (const id of ["save", "append", "rename", "del"]) $("#" + id).disabled = !open;
  setDirty(state.dirty);
  updateStats();
}

const discardOk = async () => !state.dirty || ask({
  title: "Unsaved changes",
  msg: "'" + baseName(state.cur) + "' has changes you haven't saved. Discard them?",
  ok: "Discard", danger: true
});

async function openFile(path, force) {
  if (!force && state.cur === path) return;
  if (!force && !(await discardOk())) return;
  const d = await api("file?path=" + encodeURIComponent(path));
  state.cur = path; state.eol = d.eol; state.dirty = false;
  $("#editor").value = d.content;
  $("#editor").scrollTop = 0;
  updateSheet();
  render();
}

const newFile = act(async () => {
  if (!(await discardOk())) return;
  const v = await ask({title: "New file", msg: "It will be created in " + state.dir, field: "one", ok: "Create"});
  if (v === null) return;
  if (!v.trim()) return toast("Enter a file name.", true);
  const r = await api("create", {dir: state.dir, name: v.trim(), content: ""});
  state.dirty = false;
  await browse(state.dir);
  await openFile(r.path, true);
  $("#editor").focus();
  toast("Created " + baseName(r.path) + ". Type your text, then save.");
});

const newDir = act(async () => {
  const v = await ask({title: "New folder", msg: "It will be created in " + state.dir, field: "one", ok: "Create"});
  if (v === null) return;
  if (!v.trim()) return toast("Enter a folder name.", true);
  await api("mkdir", {dir: state.dir, name: v.trim()});
  await browse(state.dir);
  toast("Created folder " + v.trim() + ".");
});

const save = act(async quiet => {
  if (!state.cur) return;
  await api("update", {path: state.cur, mode: "overwrite", content: $("#editor").value, eol: state.eol});
  setDirty(false);
  await browse(state.dir);
  if (quiet !== true) toast("Saved " + baseName(state.cur) + ".");
});

const append = act(async () => {
  if (!state.cur) return;
  const v = await ask({title: "Append text", msg: "This text is added to the end of " + baseName(state.cur) + ". Your current edits are saved first.", field: "multi", ok: "Append"});
  if (!v) return;
  if (state.dirty) await save(true);
  if (state.dirty) return;
  await api("update", {path: state.cur, mode: "append", content: v, eol: state.eol});
  await openFile(state.cur, true);
  const ed = $("#editor"); ed.scrollTop = ed.scrollHeight;
  await browse(state.dir);
  toast("Added text to " + baseName(state.cur) + ".");
});

const rename = act(async () => {
  if (!state.cur) return;
  const v = await ask({title: "Rename file", msg: "Choose a new name. The file stays in its folder.", field: "one", value: baseName(state.cur), ok: "Rename"});
  if (v === null) return;
  const nn = v.trim();
  if (!nn || nn === baseName(state.cur)) return;
  const r = await api("rename", {path: state.cur, new_name: nn});
  state.cur = r.path;
  await browse(state.dir);
  updateSheet();
  toast("Renamed to " + nn + ".");
});

const del = act(async () => {
  if (!state.cur) return;
  const path = state.cur;
  const yes = await ask({title: "Delete file", msg: "Permanently delete " + path + "? This can't be undone.", ok: "Delete", danger: true});
  if (!yes) return;
  await api("delete", {path});
  state.cur = null; state.dirty = false;
  $("#editor").value = "";
  await browse(state.dir);
  updateSheet();
  toast("Deleted " + baseName(path) + ".");
});

$("#new").onclick = $("#new2").onclick = newFile;
$("#newdir").onclick = newDir;
$("#save").onclick = () => save();
$("#append").onclick = append;
$("#rename").onclick = rename;
$("#del").onclick = del;
$("#up").onclick = act(() => browse(state.parent));
$("#home").onclick = act(() => browse(state.home));
$("#roots").onchange = act(e => browse(e.target.value));
$("#hidden").onchange = act(e => { state.hidden = e.target.checked; return browse(state.dir); });
$("#search").oninput = e => { state.q = e.target.value; render(); };
$("#editor").oninput = () => { setDirty(true); updateStats(); };
$("#path").onkeydown = act(async e => {
  if (e.key !== "Enter") return;
  try { await browse(e.target.value); }
  catch (err) { $("#path").value = state.dir; throw err; }
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
});
window.addEventListener("beforeunload", e => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

(async () => {
  try { await browse(store.get()); } catch (e) { try { await browse(""); } catch (e2) { toast(e2.message, true); } }
  updateSheet();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()