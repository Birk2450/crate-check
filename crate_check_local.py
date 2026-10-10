#!/usr/bin/env python3
"""
Personal add-on, NOT part of the repo (keep it out of git, see README of this change).

Runs the normal Crate check window, but after "Create playlist" the bar also has
  - "Copy link": copies the new playlist's Spotify link (also tried automatically),
  - "Open TuneMyMusic": opens TuneMyMusic's transfer page in a new tab.

    python crate_check_local.py            # same options as crate_check_ui.py
"""
import crate_check_ui as ui

TMM_URL = "https://www.tunemymusic.com/transfer"

_OLD = ('safeSpotify(res.url) ? h("a", {class: "primary", href: res.url, target: "_blank", '
        'rel: "noopener", text: "Open in Spotify"}) : null);')

_NEW = r'''
      ...(safeSpotify(res.url) ? [
        h("button", {class: "primary", type: "button", text: "Copy link", onclick: (e) => copyLink(res.url, e.target)}),
        h("a", {class: "primary", href: "__TMM__", target: "_blank", rel: "noopener", text: "Open TuneMyMusic"}),
        h("a", {class: "primary", href: res.url, target: "_blank", rel: "noopener", text: "Open in Spotify"})
      ] : []));
    if (safeSpotify(res.url)) copyLink(res.url, null);   // may be refused by the browser; the button always works
'''

_HELPER = r'''
async function copyLink(url, btn) {
  try {
    await navigator.clipboard.writeText(url);
    if (btn) { btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy link"; }, 2000); }
  } catch (_) {
    if (btn) btn.textContent = "Could not copy";
  }
}
async function create(data) {'''


def patch():
    page = ui.PAGE
    for old in (_OLD, "async function create(data) {"):
        assert page.count(old) == 1, "crate_check_ui.py changed; update crate_check_local.py"
    page = page.replace(_OLD, _NEW.replace("__TMM__", TMM_URL).strip("\n"))
    page = page.replace("async function create(data) {", _HELPER.strip("\n"))
    ui.PAGE = page


if __name__ == "__main__":
    patch()
    ui.main()
