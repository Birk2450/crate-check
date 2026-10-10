#!/usr/bin/env python3
"""
Crate Check - a small window around crate_check.

    python crate_check_ui.py

Type "All" or playlist / folder names, press Check, then (optionally) create a Spotify playlist
with every song that is not in RekordBox yet. Everything runs on this computer: the page is served
on 127.0.0.1 only and every request needs a one-time token.

Needs crate_check.py in the same folder (and the same setup: SPOTIPY_CLIENT_ID, see README).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import crate_check as d


class UserError(Exception):
    """An error whose message is safe and useful to show on screen as it is."""


# --------------------------------------------------------------------------
# State and the actual work
# --------------------------------------------------------------------------
class State:
    def __init__(self, rb_db: Path, folder_name: str, aliases: dict, skip_folders=None, also_check=None):
        self.also_check = list(also_check or [])       # Spotify-only playlists, e.g. "Unidentified"
        self.skip = {d.norm_name(f) for f in (skip_folders or [])}
        self.skip_labels = list(skip_folders or [])
        self.rb_db = rb_db
        self.folder_name = folder_name
        self.aliases = aliases
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.busy = threading.Lock()           # one check / create at a time
        self.outline: list = []
        self.rb_counts = (0, 0)
        self.client = None                     # read-only Spotify client
        self.wclient = None                    # created on the first "Create playlist"
        self.sp_state = "connecting"           # connecting | ready | error
        self.sp_error = ""
        self.sp_user = ""
        self.sp_event = threading.Event()
        self.sp_thread_lock = threading.Lock()
        self.last: dict | None = None

    # ---- RekordBox -------------------------------------------------------
    def refresh_rb(self):
        playlists, collection, paths, outline = d.load_rekordbox(self.rb_db)
        self.outline = outline
        self.rb_counts = (len(playlists), len(collection))
        return playlists, collection, paths

    # ---- Spotify ---------------------------------------------------------
    def connect(self):
        """Logs in to Spotify (opens the browser the first time). Safe to call again after an error."""
        with self.sp_thread_lock:
            if self.sp_state == "ready":
                return
            self.sp_state, self.sp_error = "connecting", ""
            self.sp_event.clear()
        try:
            if not (os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID")):
                raise UserError('SPOTIPY_CLIENT_ID is not set. In Terminal run: export SPOTIPY_CLIENT_ID="your client id" '
                                "and start this again.")
            client = d.SpotifyClient(write=False)
            me = client.get("https://api.spotify.com/v1/me")
            self.client = client
            self.sp_user = me.get("display_name") or me.get("id", "")
            self.sp_state = "ready"
        except BaseException as e:            # SystemExit too: the client exits with a message on 403
            self.sp_state = "error"
            self.sp_error = str(e.code) if isinstance(e, SystemExit) and e.code else (str(e) or type(e).__name__)
        finally:
            self.sp_event.set()

    def spotify(self):
        if self.sp_state != "ready":
            self.sp_event.wait(timeout=300)
        if self.sp_state != "ready":
            raise UserError(self.sp_error or "Spotify is not connected yet. Approve the login in your browser, then try again.")
        return self.client

    # ---- API -------------------------------------------------------------
    def status(self) -> dict:
        return {
            "rb": {"playlists": self.rb_counts[0], "tracks": self.rb_counts[1]},
            "outline": [{"depth": dp, "name": n, "kind": k, "count": c} for dp, n, k, c in self.outline],
            "spotify": {"state": self.sp_state, "user": self.sp_user, "error": self.sp_error},
            "folder": self.folder_name,
            "extras": self.also_check,
        }

    def check(self, query: str) -> dict:
        playlists, collection, paths = self.refresh_rb()          # fresh read every time
        q = (query or "").strip()
        notes: list[str] = []
        if q.lower() in ("", "all", "*"):
            names = [n for n in playlists
                     if not any(d.norm_name(f) in self.skip for f in paths.get(n, []))]
            extras = list(self.also_check)
            mode = "all"
            left_out = len(playlists) - len(names)
            if left_out:
                notes.append(f"Left out {left_out} playlists in the folders {', '.join(self.skip_labels)}. "
                             "Type a folder name to check one anyway.")
        else:
            tokens = [t.strip() for t in re.split(r"[,;\n]+", q) if t.strip()]
            extra_keys = {d.norm_name(e): e for e in self.also_check}
            extras = [extra_keys[d.norm_name(t)] for t in tokens if d.norm_name(t) in extra_keys]
            rest = [t for t in tokens if d.norm_name(t) not in extra_keys]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                names = d.expand_only(rest, playlists, paths) if rest else []
            notes = [ln.strip().split(" Playlists:")[0].replace(" -> ", " has ")
                     for ln in buf.getvalue().splitlines() if ln.strip()]
            mode = "some"
            if not names and not extras:
                bad = ", ".join(f"'{t}'" for t in tokens)
                raise UserError(f"No RekordBox playlist or folder is called {bad}. Try the suggestions in the list.")

        client = self.spotify()
        wanted = [d.sp_name(n, self.aliases) for n in names] + extras
        sp_playlists = d.load_spotify_api(client, wanted)        # only reads tracks of the wanted ones
        results, rb_only, _ = d.compare(playlists, collection, sp_playlists, names, self.aliases, extras)

        def tr(t):
            return {"title": t.title, "artist": t.artist, "url": t.url, "uri": t.uri}

        out_results = [{
            "name": r["name"], "total": r["total"], "have": r["have"], "extra": bool(r.get("extra")),
            "missing": [tr(t) for t in r["missing"]],
            "elsewhere": [{"spotify": tr(a), "rekordbox": tr(b)} for a, b in r["elsewhere"]],
            "unsure": [{"spotify": tr(a), "rekordbox": tr(b), "score": round(s)} for a, b, s in r["unsure"]],
        } for r in results]

        # Close matches go in too: a wrong "already have it" would hide a song (e.g. Extended vs Remix)
        uris = list(dict.fromkeys(
            t["uri"] for r in out_results
            for t in (r["missing"] + [u["spotify"] for u in r["unsure"]]) if t["uri"]))
        owned = list(sp_playlists)
        taken = {n.lower() for n in owned}
        base = f"new-songs-for-download-{time.strftime('%Y-%m-%d')}"
        name, i = base, 2
        while name.lower() in taken:
            name, i = f"{base}-{i}", i + 1

        foreign = getattr(client, "foreign_names", [])
        not_checked = [{"name": n, "hint": d.explain_unmatched(n, owned, foreign)} for n in rb_only]

        check_id = secrets.token_hex(6)
        self.last = {"id": check_id, "uris": uris, "name": name, "created": None}
        return {
            "id": check_id, "mode": mode, "notes": notes, "results": out_results,
            "not_checked": not_checked, "playlist_name": name, "song_count": len(uris),
            "outline": self.status()["outline"],
        }

    def create(self, check_id: str) -> dict:
        last = self.last
        if not last or last["id"] != check_id:
            raise UserError("These results are out of date. Press Check again, then create the playlist.")
        if last["created"]:
            return last["created"]
        if not last["uris"]:
            raise UserError("There are no missing songs to put in a playlist.")
        if self.wclient is None:
            self.wclient = d.SpotifyClient(write=True)             # first time: Spotify asks for one more permission
        pl = self.wclient.create_playlist(last["name"], "Songs from my Spotify playlists that are not in RekordBox yet.")
        self.wclient.add_items(pl["id"], last["uris"])
        last["created"] = {"name": last["name"], "count": len(last["uris"]),
                           "url": (pl.get("external_urls") or {}).get("spotify", "")}
        return last["created"]


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src data:; connect-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CrateCheck"

        def log_message(self, *args):          # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict):
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{state.port}", f"localhost:{state.port}")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "Wrong host."})
            path = self.path.split("?")[0]
            if path == "/":
                page = PAGE.replace("__TOKEN__", json.dumps(state.token))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/status":
                if self.headers.get("X-Crate-Token") != state.token:
                    return self._json(403, {"error": "Missing token."})
                return self._json(200, state.status())
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            self._json(404, {"error": "Not found."})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Crate-Token") != state.token:
                return self._json(403, {"error": "Not allowed."})
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{state.port}":
                return self._json(403, {"error": "Not allowed."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
            except (ValueError, OSError):
                return self._json(400, {"error": "Could not read the request."})

            path = self.path.split("?")[0]
            if path == "/api/connect":
                threading.Thread(target=state.connect, daemon=True).start()
                return self._json(200, {"ok": True})
            if path not in ("/api/check", "/api/create"):
                return self._json(404, {"error": "Not found."})
            if not state.busy.acquire(blocking=False):
                return self._json(409, {"error": "Another check is still running. Give it a moment."})
            try:
                if path == "/api/check":
                    payload = state.check(str(body.get("query", "")))
                else:
                    payload = state.create(str(body.get("id", "")))
                self._json(200, payload)
            except UserError as e:
                self._json(400, {"error": str(e)})
            except SystemExit as e:
                self._json(502, {"error": str(e.code) if e.code else "Spotify stopped the request."})
            except Exception as e:             # shown on screen, details in the terminal
                traceback.print_exc()
                self._json(500, {"error": f"Something went wrong: {type(e).__name__}: {e}"})
            finally:
                state.busy.release()

    return Handler


def open_window(url: str, plain_tab: bool):
    """A small app-style Chrome window on macOS if Chrome is installed, otherwise the default browser."""
    chrome = Path("/Applications/Google Chrome.app")
    if sys.platform == "darwin" and chrome.exists() and not plain_tab:
        try:
            subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}", "--window-size=600,880"])
            return
        except OSError:
            pass
    webbrowser.open(url)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Crate check</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M3 11h11l2-5h7l2 5h4v17H3z' fill='%23ff6b94'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Sofia+Sans+Extra+Condensed:wght@700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#14172b; --surface:#1d2142; --raised:#272c56; --line:#343a6b;
  --text:#eceaf7; --muted:#a4a7d0; --accent:#ff6b94; --accent-ink:#1a1030;
  --have:#74e3c4; --warn:#ffc85c; --bad:#ff8585;
  --display:"Sofia Sans Extra Condensed","Arial Narrow","Helvetica Neue",sans-serif;
  --ui:"Hanken Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#f0eff8; --surface:#ffffff; --raised:#f5f4fc; --line:#d7d5ea;
    --text:#1b1d3a; --muted:#5b5e88; --accent:#d81b60; --accent-ink:#ffffff;
    --have:#0b8a6e; --warn:#8f5f00; --bad:#c62828;
  }
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:400 15px/1.45 var(--ui);min-height:100vh}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.app{max-width:640px;margin:0 auto;padding:22px 18px 150px}

/* header */
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:26px}
.brand{margin:0;font:800 34px/1 var(--display);letter-spacing:.01em}
.stats{display:grid;gap:4px;justify-items:end}
.stat{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted);text-align:right}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.stat.ok .dot{background:var(--have)} .stat.wait .dot{background:var(--warn)} .stat.err .dot{background:var(--bad)}
.stat button{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px}

/* the question */
label.q{display:block;font-weight:600;margin:0 0 8px}
.wrap{position:relative}
.field{display:flex;align-items:center;gap:8px;background:var(--surface);border:1.5px solid var(--line);border-radius:18px;padding:7px 7px 7px 18px}
.field:focus-within{border-color:var(--accent)}
.field input{flex:1;min-width:0;border:0;background:none;outline:none;font-size:20px;height:44px}
.field input::placeholder{color:var(--muted);opacity:.8}
.primary{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:44px;padding:0 22px;border:0;border-radius:12px;background:var(--accent);color:var(--accent-ink);font-weight:700;font-size:16px;cursor:pointer;text-decoration:none;white-space:nowrap}
.primary:hover{filter:brightness(1.06)}
.primary:disabled{opacity:.55;cursor:default}
.hint{margin:8px 2px 0;font-size:13px;color:var(--muted)}
#sugg{position:absolute;left:0;right:0;top:calc(100% + 6px);z-index:5;list-style:none;margin:0;padding:6px;background:var(--raised);border:1px solid var(--line);border-radius:14px;max-height:300px;overflow:auto;box-shadow:0 12px 30px rgb(0 0 0 / .28)}
#sugg svg{width:16px;height:16px;flex:none;opacity:.75}
#sugg li{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;cursor:pointer}
#sugg li[aria-selected=true],#sugg li:hover{background:var(--surface)}
#sugg .kind{margin-left:auto;font-size:12.5px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border:1px solid var(--line);border-radius:999px;background:none;font-size:13.5px;font-weight:500;cursor:pointer}
.chip:hover{border-color:var(--accent)}
.chip svg{width:14px;height:14px;opacity:.75}

/* busy */
.busy{margin:26px 0 0}
.busy .track{height:3px;border-radius:3px;background:var(--line);overflow:hidden}
.busy .track i{display:block;height:100%;width:34%;background:var(--accent);border-radius:3px;animation:slide 1.05s ease-in-out infinite alternate}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(194%)}}
.busy p{margin:10px 0 0;font-size:13.5px;color:var(--muted)}
@media (prefers-reduced-motion: reduce){.busy .track i{animation:none;width:100%;opacity:.5}}

/* results */
#out{margin-top:30px}
.err{border-left:3px solid var(--bad);background:var(--surface);padding:12px 14px;border-radius:4px 12px 12px 4px;margin:0 0 18px}
.err b{display:block;margin-bottom:2px}
.note{margin:0 0 6px;font-size:13.5px;color:var(--muted)}
.note.warn{color:var(--warn)}
.summary{margin:14px 0 6px}
.summary h2{margin:0;font:800 38px/1.04 var(--display);letter-spacing:.005em}
.summary .sub{margin:6px 0 0;color:var(--muted)}

.crate{margin-top:30px}
.tab{--x:14px;display:flex;align-items:center;gap:10px;width:max-content;max-width:calc(100% - var(--x));margin:0 0 -1px var(--x);padding:9px 16px 8px;position:relative;z-index:1;background:var(--surface);border:1px solid var(--line);border-bottom-color:var(--surface);border-radius:13px 13px 0 0;font:800 25px/1 var(--display);letter-spacing:.01em}
.tab.pos1{--x:24%} .tab.pos2{--x:46%}
.tab span.nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{flex:none;min-width:26px;padding:3px 8px 2px;border-radius:999px;background:var(--accent);color:var(--accent-ink);font:800 17px/1.1 var(--display);text-align:center;font-variant-numeric:tabular-nums}
.crate-body{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px 8px}
.meterrow{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.meter{flex:1;height:6px;border-radius:6px;background:var(--line);overflow:hidden}
.meter i{display:block;height:100%;background:var(--have);border-radius:6px}
.meterrow p{margin:0;font-size:13px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.tracks,.pairs,.skips,.names{list-style:none;margin:0;padding:0}
.tracks li{display:flex;align-items:center;gap:12px;padding:11px 0;border-top:1px solid var(--line)}
.meta{flex:1;min-width:0;display:grid}
.t{font-weight:600;overflow-wrap:anywhere}
.a{font-size:13.5px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.open{flex:none;display:grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--raised);color:var(--text)}
.open:hover{outline:1.5px solid var(--accent)}
.open svg{width:16px;height:16px}
.clear{margin:10px 0 6px;color:var(--muted)}
details.more{border-top:1px solid var(--line);padding:2px 0}
details.more>summary{cursor:pointer;padding:10px 0;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s}
details.more[open]>summary::before{transform:rotate(45deg)}
.pairs li{padding:8px 0 10px;border-top:1px dashed var(--line);font-size:14px}
.pairs p{margin:0;overflow-wrap:anywhere}
.pairs b{display:inline-block;min-width:72px;color:var(--muted);font-weight:500}
.pairs .score{color:var(--warn);margin-left:6px;font-size:13px}
.skips li{display:grid;gap:1px;padding:8px 0;border-top:1px dashed var(--line)}
.skips li span{font-size:13.5px;color:var(--muted)}
.outside{margin-top:30px}
.outside details.more{border:1px solid var(--line);border-radius:12px;padding:0 14px;background:var(--surface);margin-top:10px}
.outside details.alert{border-color:var(--warn)}
.names li{display:flex;align-items:center;gap:9px;padding:8px 0;border-top:1px dashed var(--line)}
.names svg{width:15px;height:15px;color:var(--have);flex:none}
.foot{margin:34px 0 0;font-size:12.5px;color:var(--muted)}

/* action bar */
.bar{position:fixed;left:0;right:0;bottom:0;z-index:4;background:color-mix(in srgb,var(--bg) 86%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-top:1px solid var(--line)}
.bar-in{max-width:640px;margin:0 auto;padding:12px 18px calc(12px + env(safe-area-inset-bottom));display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.bar-text{flex:1;min-width:200px;display:grid;gap:1px}
.bar-text span{font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.bar.made .bar-text strong{color:var(--have)}
.bar .help{flex-basis:100%;margin:-4px 0 0;font-size:12.5px;color:var(--muted)}

@media (max-width:420px){
  .brand{font-size:30px} .summary h2{font-size:33px}
  .primary{padding:0 16px}
}
</style>
</head>
<body>
<main class="app">
  <header>
    <h1 class="brand">Crate check</h1>
    <div class="stats">
      <div class="stat" id="stRb"><i class="dot"></i><span>Reading RekordBox</span></div>
      <div class="stat wait" id="stSp"><i class="dot"></i><span>Connecting to Spotify</span></div>
    </div>
  </header>

  <form id="form" autocomplete="off">
    <label class="q" for="q">Playlists to check</label>
    <div class="wrap">
      <div class="field">
        <input id="q" type="text" placeholder="All, or e.g. Amapiano, House" spellcheck="false" role="combobox" aria-expanded="false" aria-controls="sugg" aria-autocomplete="list">
        <button class="primary" id="go" type="submit">Check</button>
      </div>
      <ul id="sugg" role="listbox" hidden></ul>
    </div>
    <p class="hint">A folder name checks every playlist inside it. Separate several names with commas.</p>
    <div class="chips" id="chips"></div>
  </form>

  <div class="busy" id="busy" hidden aria-live="polite">
    <div class="track"><i></i></div>
    <p id="busyText"></p>
  </div>

  <section id="out" aria-live="polite"></section>
</main>

<div class="bar" id="bar" hidden><div class="bar-in" id="barIn"></div></div>

<script>
(() => {
const TOKEN = __TOKEN__;
const $ = (s, el = document) => el.querySelector(s);
const ICON = {
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M8 7h9v9"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 7h6l2 2h10v10H3z"/></svg>'
};

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;          // only ever used with the constants above
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", "X-Crate-Token": TOKEN},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let data;
  try { data = await r.json(); } catch (_) { data = {error: "The app sent a reply this page could not read."}; }
  if (!r.ok || data.error) throw new Error(data.error || ("Request failed (" + r.status + ")"));
  return data;
}

const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
const safeSpotify = (u) => typeof u === "string" && u.startsWith("https://open.spotify.com/");

/* ---------- status, suggestions, chips ---------- */
let items = [];
let extraNames = [];
let folderName = "your DJ folder";
const q = $("#q"), sugg = $("#sugg");

function applyStatus(s) {
  folderName = s.folder || folderName;
  extraNames = s.extras || [];
  const rb = $("#stRb");
  rb.className = "stat ok";
  rb.lastChild.textContent = "RekordBox: " + plural(s.rb.playlists, "playlist", "playlists");
  const sp = $("#stSp");
  if (s.spotify.state === "ready") {
    sp.className = "stat ok";
    sp.lastChild.textContent = "Spotify: " + (s.spotify.user ? "connected as " + s.spotify.user : "connected");
  } else if (s.spotify.state === "error") {
    sp.className = "stat err";
    sp.replaceChildren(h("i", {class: "dot"}), h("span", {text: "Spotify: not connected "}),
      h("button", {type: "button", text: "Try again", onclick: retryConnect}));
  } else {
    sp.className = "stat wait";
    sp.replaceChildren(h("i", {class: "dot"}), h("span", {text: "Spotify: approve the login in your browser"}));
  }
  buildItems(s.outline);
  renderChips();
  return s;
}

function buildItems(outline) {
  items = [{name: "All", kind: "all"}];
  for (const o of outline || []) if (o.kind === "folder" || o.kind === "playlist") items.push({name: o.name, kind: o.kind});
  for (const e of extraNames) items.push({name: e, kind: "extra"});
}

async function pollStatus() {
  try {
    const s = applyStatus(await api("/api/status"));
    if (s.spotify.state === "error") showSpotifyError(s.spotify.error);
    if (s.spotify.state === "connecting") setTimeout(pollStatus, 1200);
  } catch (_) { setTimeout(pollStatus, 3000); }
}
async function retryConnect() {
  clearOut();
  try { await api("/api/connect", {}); } catch (_) {}
  pollStatus();
}
function showSpotifyError(msg) {
  const out = $("#out");
  if (!$(".err", out)) out.prepend(h("div", {class: "err", role: "alert"}, h("b", {text: "Spotify is not connected"}), msg));
}

function recent() { try { return JSON.parse(localStorage.getItem("cc.recent") || "[]"); } catch (_) { return []; } }
function remember(v) {
  try {
    const list = [v, ...recent().filter(x => x.toLowerCase() !== v.toLowerCase())].slice(0, 4);
    localStorage.setItem("cc.recent", JSON.stringify(list));
  } catch (_) {}
}
function renderChips() {
  const box = $("#chips");
  const folders = items.filter(i => i.kind === "folder").slice(0, 4);
  const seen = new Set();
  const chips = [];
  const add = (label, value, icon) => {
    if (seen.has(value.toLowerCase())) return;
    seen.add(value.toLowerCase());
    chips.push(h("button", {class: "chip", type: "button", onclick: () => { q.value = value; run(value); }},
      icon ? h("span", {html: ICON.folder, "aria-hidden": "true", style: "display:contents"}) : null, label));
  };
  add("All", "All");
  folders.forEach(f => add(f.name, f.name, true));
  recent().forEach(r => add(r, r));
  box.replaceChildren(...chips);
}

let active = -1, shown = [];
function lastToken(v) {
  const i = v.lastIndexOf(",");
  return {head: i >= 0 ? v.slice(0, i + 1) + " " : "", tok: (i >= 0 ? v.slice(i + 1) : v).trim()};
}
function hideSugg() { sugg.hidden = true; q.setAttribute("aria-expanded", "false"); active = -1; }
function showSugg() {
  const {tok} = lastToken(q.value);
  if (!tok) return hideSugg();
  const low = tok.toLowerCase();
  shown = items.filter(i => i.name.toLowerCase().includes(low) && i.name.toLowerCase() !== low).slice(0, 8);
  if (!shown.length) return hideSugg();
  sugg.replaceChildren(...shown.map((it, idx) => h("li", {role: "option", "aria-selected": String(idx === active),
    onmousedown: (e) => { e.preventDefault(); pick(it); }},
    it.kind === "folder" ? h("span", {html: ICON.folder, style: "display:contents"}) : null,
    it.name, h("span", {class: "kind", text: it.kind === "folder" ? "folder" : it.kind === "all" ? "everything" : it.kind === "extra" ? "Spotify only" : "playlist"}))));
  sugg.hidden = false; q.setAttribute("aria-expanded", "true");
}
function pick(it) {
  const {head} = lastToken(q.value);
  q.value = it.kind === "all" ? "All" : head + it.name;
  hideSugg(); q.focus();
}
q.addEventListener("input", () => { active = -1; showSugg(); });
q.addEventListener("blur", hideSugg);
q.addEventListener("keydown", (e) => {
  if (sugg.hidden) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    active = (active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
    [...sugg.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
  } else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pick(shown[active]); }
  else if (e.key === "Escape") hideSugg();
});

/* ---------- running a check ---------- */
function clearOut() { $("#out").replaceChildren(); $("#bar").hidden = true; }
function setBusy(on, text) {
  $("#busy").hidden = !on; $("#busyText").textContent = text || "";
  $("#go").disabled = on; q.disabled = on;
  for (const c of document.querySelectorAll(".chip")) c.disabled = on;
}
function showError(msg, title) {
  const out = $("#out");
  out.prepend(h("div", {class: "err", role: "alert"}, h("b", {text: title || "That did not work"}), msg));
}

let running = false;
async function run(query) {
  if (running) return;
  running = true; hideSugg(); clearOut();
  setBusy(true, "Reading RekordBox and Spotify. This takes a few seconds, longer for a lot of playlists.");
  try {
    const data = await api("/api/check", {query});
    remember(query.trim() || "All");
    if (data.outline) { buildItems(data.outline); renderChips(); }
    render(data);
  } catch (e) {
    showError(e.message);
  } finally {
    setBusy(false); running = false;
  }
}
$("#form").addEventListener("submit", (e) => { e.preventDefault(); run(q.value); });

/* ---------- drawing results ---------- */
function trackRow(t) {
  return h("li", {},
    h("div", {class: "meta"}, h("span", {class: "t", text: t.title}), h("span", {class: "a", text: t.artist})),
    safeSpotify(t.url) ? h("a", {class: "open", href: t.url, target: "_blank", rel: "noopener",
      "aria-label": "Open " + t.title + " in Spotify", title: "Open in Spotify", html: ICON.open}) : null);
}
const label = (t) => (t.artist ? t.artist + " - " : "") + t.title;
function pairRow(p, withScore) {
  return h("li", {},
    h("p", {}, h("b", {text: "Spotify"}), label(p.spotify)),
    h("p", {}, h("b", {text: "RekordBox"}), label(p.rekordbox), withScore ? h("span", {class: "score", text: p.score + "% alike"}) : null));
}
function group(title, rows, open) {
  if (!rows.length) return null;
  return h("details", {class: "more", open: !!open}, h("summary", {text: title + " (" + rows.length + ")"}), h("ul", {class: "pairs"}, rows));
}
function crate(r, pos) {
  const pct = r.total ? Math.round(100 * r.have / r.total) : 0;
  return h("section", {class: "crate"},
    h("h3", {class: "tab pos" + pos, title: r.name}, h("span", {class: "nm", text: r.name}),
      (r.missing.length + r.unsure.length) ? h("span", {class: "badge", text: r.missing.length + r.unsure.length,
        "aria-label": (r.missing.length + r.unsure.length) + " to download"}) : null),
    h("div", {class: "crate-body"},
      r.extra ? h("p", {class: "note", text: "Not a RekordBox playlist. Its songs are compared with your whole RekordBox library."}) : null,
      h("div", {class: "meterrow"},
        h("div", {class: "meter", role: "img", "aria-label": r.have + " of " + r.total + " in RekordBox"}, h("i", {style: "width:" + pct + "%"})),
        h("p", {text: r.have + " of " + r.total + " in RekordBox"})),
      r.missing.length ? h("ul", {class: "tracks"}, r.missing.map(trackRow)) : r.unsure.length ? null : h("p", {class: "clear", text: "Nothing is missing here."}),
      group("Close matches, added to the playlist too", r.unsure.map(u => pairRow(u, true)), true),
      group("In RekordBox, but in another playlist", r.elsewhere.map(e => pairRow(e, false)))));
}

function render(data) {
  const out = $("#out"); out.replaceChildren();
  const results = [...data.results].sort((a, b) =>
    ((b.missing.length + b.unsure.length) - (a.missing.length + a.unsure.length)) || a.name.localeCompare(b.name));
  const total = results.reduce((n, r) => n + r.missing.length + r.unsure.length, 0);
  const open = results.filter(r => r.missing.length || r.unsure.length || r.elsewhere.length);
  const complete = results.filter(r => !(r.missing.length || r.unsure.length || r.elsewhere.length));

  for (const n of data.notes) out.append(h("p", {class: "note" + (n.startsWith("!") ? " warn" : ""), text: n.replace(/^!\s*/, "")}));

  const withSongs = results.filter(r => r.missing.length || r.unsure.length).length;
  out.append(h("div", {class: "summary"},
    h("h2", {text: total === 0 ? "Nothing new to download" : plural(total, "song", "songs") + " to download"}),
    h("p", {class: "sub", text: results.length
      ? "Checked " + plural(results.length, "playlist", "playlists") + (withSongs ? ". " + withSongs + " of them " + (withSongs === 1 ? "has" : "have") + " songs to download." : ".")
      : "None of the playlists you asked for has a Spotify playlist with the same name."})));

  let pos = 0;
  for (const r of open) out.append(crate(r, pos++ % 3));

  const outside = h("div", {class: "outside"});
  if (complete.length) {
    outside.append(h("details", {class: "more" + (open.length ? "" : " alert"), open: !open.length},
      h("summary", {text: "Fully in RekordBox (" + complete.length + ")"}),
      h("ul", {class: "names"}, complete.map(r => h("li", {}, h("span", {html: ICON.check}), r.name)))));
  }
  if (data.not_checked.length) {
    const explicit = data.mode === "some";
    outside.append(h("details", {class: "more" + (explicit ? " alert" : ""), open: explicit},
      h("summary", {text: (explicit ? "Not checked: no Spotify playlist with the same name" : "Skipped: no Spotify playlist with the same name") + " (" + data.not_checked.length + ")"}),
      h("ul", {class: "skips"}, data.not_checked.map(x => h("li", {}, h("strong", {text: x.name}), h("span", {text: x.hint}))))));
  }
  out.append(outside);
  out.append(h("p", {class: "foot", text: "Song information from Spotify. Each song links back to it."}));
  renderBar(data, total);
}

/* ---------- creating the playlist ---------- */
function renderBar(data, total) {
  const bar = $("#bar"), inner = $("#barIn");
  bar.className = "bar";
  if (!total || !data.song_count) { bar.hidden = true; return; }
  bar.hidden = false;
  inner.replaceChildren(
    h("div", {class: "bar-text"},
      h("strong", {text: plural(data.song_count, "song", "songs")}),
      h("span", {text: "will go into " + data.playlist_name})),
    h("button", {class: "primary", id: "create", type: "button", text: "Create playlist", onclick: () => create(data)}));
}
async function create(data) {
  const btn = $("#create");
  btn.disabled = true; btn.textContent = "Creating...";
  const inner = $("#barIn");
  const help = h("p", {class: "help", text: "The first time, Spotify asks you to approve editing your playlists. Check your browser."});
  inner.append(help);
  try {
    const res = await api("/api/create", {id: data.id});
    $("#bar").className = "bar made";
    inner.replaceChildren(
      h("div", {class: "bar-text"},
        h("strong", {text: "Created " + res.name}),
        h("span", {text: plural(res.count, "song", "songs") + " added. Spotify does not let apps put playlists into folders, so drag it into " + folderName + "."})),
      safeSpotify(res.url) ? h("a", {class: "primary", href: res.url, target: "_blank", rel: "noopener", text: "Open in Spotify"}) : null);
  } catch (e) {
    help.remove(); btn.disabled = false; btn.textContent = "Create playlist";
    showError(e.message, "The playlist was not created");
    window.scrollTo({top: 0, behavior: "smooth"});
  }
}

pollStatus();
q.focus();
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rb-db", type=Path, default=d.DEFAULT_RB_DB, help="path to RekordBox master.db")
    ap.add_argument("--folder-name", default="your Spotify folder", help="your Spotify folder, only used in the reminder after creating")
    ap.add_argument("--port", type=int, default=0, help="port to use (default: any free one)")
    ap.add_argument("--plain-tab", action="store_true", help="open in a normal browser tab instead of a small window")
    ap.add_argument("--no-open", action="store_true", help="do not open a window, just print the address")
    args = ap.parse_args()

    here = Path(__file__).parent
    settings = d.load_settings(here / "settings.json")
    state = State(args.rb_db.expanduser(), args.folder_name, d.load_aliases(here / "aliases.json"),
                  settings["skip_folders"], settings["also_check"])
    print("Reading RekordBox (on a copy)...")
    state.refresh_rb()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    state.port = server.server_address[1]
    url = f"http://127.0.0.1:{state.port}/"
    threading.Thread(target=state.connect, daemon=True).start()

    print(f"Crate check is running at {url}\nPress Ctrl+C here to quit.")
    if not args.no_open:
        open_window(url, args.plain_tab)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
