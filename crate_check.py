#!/usr/bin/env python3
"""
crate_check.py - Which Spotify playlist tracks are not yet in RekordBox?

For every RekordBox playlist that has a Spotify playlist with the same name,
lists the Spotify tracks you have NOT downloaded into RekordBox yet.

Nothing is ever written to RekordBox:
  * RekordBox: master.db is COPIED to a temp folder and the copy is opened.
  * Spotify:   read-only scopes, unless you ask for --make-playlist (then one extra scope
               to create a new playlist; existing playlists are never changed).

Usage:
    python crate_check.py                      # Spotify via API
    python crate_check.py --csv-dir ~/exportify  # Spotify via Exportify CSVs
    python crate_check.py --only "House" "Disco" # just these playlists
    python crate_check.py --only "House" --make-playlist   # + temp Spotify playlist
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

DEFAULT_RB_DB = Path.home() / "Library/Pioneer/rekordbox/master.db"
REDIRECT_URI = "http://127.0.0.1:8888/callback"  # Spotify requires loopback IP, not "localhost"

# Thresholds (0-100). >= MATCH counts as "already have it",
# between UNSURE and MATCH is listed separately for a human glance.
MATCH = 90
UNSURE = 78


# --------------------------------------------------------------------------
# Data + normalisation
# --------------------------------------------------------------------------
@dataclass
class Track:
    title: str
    artist: str
    url: str = ""
    uri: str = ""
    key_title: str = field(init=False, default="")
    key_artist: str = field(init=False, default="")

    def __post_init__(self):
        self.key_title = norm_title(self.title)
        self.key_artist = norm_artist(self.artist)

    def __str__(self):
        return f"{self.artist} - {self.title}"


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Mix descriptors that don't change which song it is
_DROPPABLE = r"(original|radio|club|album|main|full|clean|dirty|explicit|remaster(ed)?( \d{4})?|\d{4} remaster(ed)?)( mix| version| edit)?"


def norm_title(t: str) -> str:
    t = _strip_accents(t or "").lower().replace("&", "and")
    t = re.sub(r"\b(feat|ft|featuring)\.?\s+[^()\[\]-]+", " ", t)  # feat. X in title
    # unify "- Foo Remix" into "(foo remix)"
    t = re.sub(r"\s+-\s+([^-()\[\]]*?(remix|edit|mix|vip|bootleg|rework|version|dub)[^-()\[\]]*)$", r" (\1)", t)
    # drop harmless descriptors inside brackets
    def _bracket(m):
        inner = m.group(1).strip()
        return " " if re.fullmatch(_DROPPABLE, inner) else f" {inner} "
    t = re.sub(r"[\(\[]([^()\[\]]*)[\)\]]", _bracket, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_artist(a: str) -> str:
    a = _strip_accents(a or "").lower().replace("&", " ")
    a = re.sub(r"\b(feat|ft|featuring|vs|x|and|with)\b\.?", " ", a)
    a = re.sub(r"[^a-z0-9 ]+", " ", a)
    return re.sub(r"\s+", " ", a).strip()


def norm_name(n: str) -> str:
    """Playlist-name key: ignores case, accents, punctuation, emoji and spaces ('Afro House' == 'AfroHouse')."""
    n = _strip_accents(n or "").lower().replace("_", " ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", "", n)


_REMIX_WORDS = re.compile(r"extended|remix|edit|vip|bootleg|rework|dub|flip|mashup|live|acoustic|instrumental|sped|slowed")


def _mix_flavour(title_key: str) -> frozenset:
    return frozenset(w for w in title_key.split() if _REMIX_WORDS.fullmatch(w))


def score(sp: Track, rb: Track) -> float:
    """0-100 similarity between a Spotify track and a RekordBox track."""
    # Compare title tokens order-insensitively but penalise differing remix/live flavours
    title_s = fuzz.token_sort_ratio(sp.key_title, rb.key_title)
    if sp.key_title and sp.key_title in rb.key_title or rb.key_title and rb.key_title in sp.key_title:
        title_s = max(title_s, 90)
    artist_s = fuzz.token_set_ratio(sp.key_artist, rb.key_artist) if sp.key_artist and rb.key_artist else 60
    s = 0.65 * title_s + 0.35 * artist_s
    if _mix_flavour(sp.key_title) != _mix_flavour(rb.key_title):
        s = min(s, UNSURE + 5)   # "X (Remix)" must not silently match plain "X"
    return s


def best_match(sp: Track, pool: list[Track]) -> tuple[float, Track | None]:
    best, best_t = 0.0, None
    for rb in pool:
        s = score(sp, rb)
        if s > best:
            best, best_t = s, rb
    return best, best_t


# --------------------------------------------------------------------------
# RekordBox side (read-only, on a copy)
# --------------------------------------------------------------------------
def load_rekordbox(db_path: Path):
    """Returns (playlists, collection, paths, outline).

    playlists: {name: [Track]}          real playlists only (folders and smart playlists excluded)
    collection: [Track]                 every track in the RekordBox collection
    paths:     {playlist name: [folder names from the top down]}
    outline:   [(depth, name, kind, n_tracks)] the whole playlist tree, for --tree
    """
    from pyrekordbox import Rekordbox6Database  # imported late so --help works without it

    if not db_path.exists():
        sys.exit(f"RekordBox database not found: {db_path}\nPass the right location with --rb-db.")

    tmp = Path(tempfile.mkdtemp(prefix="rbcopy_"))
    try:
        for f in db_path.parent.glob(db_path.name + "*"):  # master.db, -wal, -shm if present
            shutil.copy2(f, tmp / f.name)
        db = Rekordbox6Database(path=tmp / db_path.name)

        def to_track(content) -> Track | None:
            if content is None or getattr(content, "rb_local_deleted", 0):
                return None
            title = content.Title or Path(content.FileNameL or "").stem
            return Track(title=title, artist=content.ArtistName or "")

        all_pl = [p for p in db.get_playlist().all() if not getattr(p, "rb_local_deleted", 0)]
        by_id = {p.ID: p for p in all_pl}
        parents = {p.ParentID for p in all_pl}      # anything that is somebody's parent is a folder

        def ancestors(p) -> list[str]:
            out, cur, guard = [], by_id.get(p.ParentID), 0
            while cur is not None and guard < 50:
                out.append(cur.Name)
                cur, guard = by_id.get(cur.ParentID), guard + 1
            return out[::-1]

        def kind(p) -> str:
            if p.is_folder or p.ID in parents:
                return "folder"
            return "smart" if p.is_smart_playlist else "playlist"

        playlists: dict[str, list[Track]] = {}
        paths: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for pl in all_pl:
            if kind(pl) != "playlist":      # skip folders and smart playlists
                continue
            tracks = []
            for song in pl.Songs:
                if getattr(song, "rb_local_deleted", 0):
                    continue
                t = to_track(song.Content)
                if t:
                    tracks.append(t)
            playlists.setdefault(pl.Name, []).extend(tracks)
            paths[pl.Name] = ancestors(pl)
            counts[pl.ID] = len(tracks)

        outline: list[tuple] = []
        def walk(parent_id, depth):
            for p in sorted((x for x in all_pl if x.ParentID == parent_id), key=lambda x: x.Seq or 0):
                outline.append((depth, p.Name, kind(p), counts.get(p.ID, 0)))
                walk(p.ID, depth + 1)
        walk("root", 0)

        collection = [t for t in map(to_track, db.get_content().all()) if t]
        db.close()
        return playlists, collection, paths, outline
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def expand_only(only, rb_playlists, rb_paths) -> list[str]:
    """Turns --only names into RekordBox playlist names.

    A name can be a playlist, or a FOLDER (meaning every playlist inside it, at any depth).
    """
    pl_keys = {norm_name(n): n for n in rb_playlists}
    names: list[str] = []
    for o in only:
        k, found = norm_name(o), False
        inside = [n for n, anc in rb_paths.items() if k in {norm_name(a) for a in anc}]
        if inside:
            print(f"  Folder '{o}' -> {len(inside)} playlists: {', '.join(inside)}")
            names += inside
            found = True
        if k in pl_keys:
            names.append(pl_keys[k])
            found = True
        if not found:
            print(f"  ! '{o}' is not a RekordBox playlist or folder. Playlists: "
                  + ", ".join(sorted(rb_playlists)) + "  (run with --tree to see the folder structure)")
    return list(dict.fromkeys(names))


# --------------------------------------------------------------------------
# Spotify side
# --------------------------------------------------------------------------
class SpotifyClient:
    """Minimal Spotify Web API client (Feb-2026 endpoints). Login is cached between runs."""

    BASE = "https://api.spotify.com/v1"

    def __init__(self, write: bool = False):
        import requests
        from spotipy.oauth2 import CacheFileHandler, SpotifyPKCE

        client_id = os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID")
        if not client_id:
            sys.exit("Set SPOTIPY_CLIENT_ID (see README) or use --csv-dir with Exportify files.")
        scope = "playlist-read-private playlist-read-collaborative"
        if write:
            scope += " playlist-modify-private"   # only requested when you pass --make-playlist
        self.auth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=REDIRECT_URI,
            scope=scope,
            cache_handler=CacheFileHandler(
                cache_path=str(Path.home() / (".crate_check_token_rw" if write else ".crate_check_token"))),
        )
        self.session = requests.Session()

    def _token(self) -> str:
        # Returns the cached token, refreshing it or opening the browser login when needed.
        # (Depending on the spotipy version this is a str or a dict.)
        tok = self.auth.get_access_token()
        return tok["access_token"] if isinstance(tok, dict) else tok

    def _request(self, method, url, **kw):
        for _ in range(5):
            r = self.session.request(
                method, url, timeout=30,
                headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
                continue
            if r.status_code == 403:
                sys.exit(
                    f"Spotify returned 403 for {url}.\n"
                    "Dev-mode apps need: Premium on the app-owner account, and your account added "
                    "under 'User Management' in the Spotify dashboard. Or use --csv-dir (Exportify)."
                )
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def get(self, url, **params):
        return self._request("GET", url, params=params)

    def create_playlist(self, name: str, description: str) -> dict:
        return self._request("POST", f"{self.BASE}/me/playlists",
                             json={"name": name, "public": False, "description": description})

    def add_items(self, playlist_id: str, uris: list[str]):
        # Spotify documents 100 per request, but some Feb-2026 clients report 10; 10 is safe either way.
        for i in range(0, len(uris), 10):
            self._request("POST", f"{self.BASE}/playlists/{playlist_id}/items",
                          json={"uris": uris[i:i + 10]})


def make_temp_playlist(client: SpotifyClient, results, name: str) -> str | None:
    """Gathers every 'not downloaded yet' track from `results` into one new private playlist."""
    seen, uris = set(), []
    for r in results:
        for t in list(r["missing"]) + [sp for sp, _, _ in r["unsure"]]:
            if t.uri and t.uri not in seen:
                seen.add(t.uri)
                uris.append(t.uri)
    if not uris:
        print("Nothing to download - no playlist created.")
        return None
    pl = client.create_playlist(name, "Tracks not yet downloaded to RekordBox. Safe to delete.")
    client.add_items(pl["id"], uris)
    return (pl.get("external_urls") or {}).get("spotify", pl["id"])


def load_spotify_api(client: SpotifyClient, only=None) -> dict[str, list[Track]]:
    """Reads your own playlists via the Web API (Feb-2026 '/items' endpoints).

    With `only`, track data is requested just for those playlists (Spotify's developer rules say to
    request only the data you need); the playlist-name list is still fetched to find them.
    """
    get = client.get
    only_keys = {norm_name(o) for o in only} if only else None

    me = get("https://api.spotify.com/v1/me")["id"]
    out: dict[str, list[Track]] = {}
    client.foreign_names = []                 # playlists in your library that belong to someone else
    url, params = "https://api.spotify.com/v1/me/playlists", {"limit": 50}
    while url:
        page = get(url, **params)
        for pl in page["items"]:
            if pl["owner"]["id"] != me:       # only playlists you own expose their items
                client.foreign_names.append(pl["name"])
                continue
            if only_keys and norm_name(pl["name"]) not in only_keys:
                out.setdefault(pl["name"], [])   # name only, so it is still reported as "not requested"
                continue
            tracks = []
            iurl, iparams = f"https://api.spotify.com/v1/playlists/{pl['id']}/items", {"limit": 50}
            while iurl:
                ipage = get(iurl, **iparams)
                for it in ipage["items"]:
                    obj = it.get("item") or it.get("track")  # 'item' since Feb 2026
                    if not obj or obj.get("type", "track") != "track" or obj.get("is_local"):
                        continue
                    tracks.append(
                        Track(
                            title=obj["name"],
                            artist=", ".join(a["name"] for a in obj.get("artists", [])),
                            url=(obj.get("external_urls") or {}).get("spotify", ""),
                            uri=obj.get("uri", ""),
                        )
                    )
                iurl, iparams = ipage.get("next"), {}
            out.setdefault(pl["name"], []).extend(tracks)
        url, params = page.get("next"), {}
    return out


def load_spotify_csv(folder: Path) -> dict[str, list[Track]]:
    """Reads Exportify CSVs (one per playlist; file name = playlist name)."""
    out: dict[str, list[Track]] = {}
    for f in sorted(folder.expanduser().glob("*.csv")):
        with f.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        tracks = []
        for r in rows:
            title = r.get("Track Name") or r.get("Name") or ""
            artist = (r.get("Artist Name(s)") or r.get("Artist Name") or "").replace(";", ", ")
            uri = r.get("Track URI", "")
            url = f"https://open.spotify.com/track/{uri.split(':')[-1]}" if uri.startswith("spotify:track:") else ""
            if title:
                tracks.append(Track(title, artist, url, uri if uri.startswith("spotify:track:") else ""))
        out[f.stem] = tracks
    return out


# --------------------------------------------------------------------------
# Comparison + report
# --------------------------------------------------------------------------
def load_aliases(path: Path) -> dict[str, str]:
    """Optional aliases.json: {"RekordBox playlist name": "Spotify playlist name"} for names that differ."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read {path.name}: {e}")
    return {norm_name(k): str(v) for k, v in raw.items() if not str(k).startswith("_")}


def sp_name(rb_name: str, aliases: dict | None) -> str:
    """The Spotify playlist name that belongs to a RekordBox playlist (via aliases.json, else the same)."""
    return (aliases or {}).get(norm_name(rb_name), rb_name)


def compare(rb_playlists, rb_collection, sp_playlists, only=None, aliases=None):
    # Keys are on the Spotify side's naming, so an alias makes the two lists meet.
    rb_by_key = {norm_name(sp_name(n, aliases)): (n, t) for n, t in rb_playlists.items()}
    sp_by_key = {norm_name(n): (n, t) for n, t in sp_playlists.items()}
    only_keys = {norm_name(sp_name(o, aliases)) for o in only} if only else None

    results, rb_only, sp_only = [], [], []
    for key, (rb_name, rb_tracks) in rb_by_key.items():
        if only_keys and key not in only_keys:
            continue
        if key not in sp_by_key:
            rb_only.append(rb_name)
            continue
        _sp_pl_name, sp_tracks = sp_by_key[key]
        missing, elsewhere, unsure = [], [], []
        have = 0
        for sp in sp_tracks:
            s, m = best_match(sp, rb_tracks)
            if s >= MATCH:
                have += 1
                continue
            if s >= UNSURE:
                unsure.append((sp, m, s))
                continue
            s2, m2 = best_match(sp, rb_collection)
            if s2 >= MATCH:
                elsewhere.append((sp, m2))
            elif s2 >= UNSURE:
                unsure.append((sp, m2, s2))
            else:
                missing.append(sp)
        results.append(dict(name=rb_name, total=len(sp_tracks), have=have,
                            missing=missing, elsewhere=elsewhere, unsure=unsure))
    sp_only = [n for k, (n, _) in sp_by_key.items() if k not in rb_by_key and not (only_keys and k not in only_keys)]
    return results, rb_only, sp_only


def explain_unmatched(name: str, owned: list[str], foreign: list[str]) -> str:
    """Why did this RekordBox playlist find no Spotify partner? Gives the nearest Spotify names."""
    k = norm_name(name)
    if any(norm_name(f) == k for f in foreign):
        return ("a Spotify playlist with this name is in your library but owned by another account, "
                "so this app cannot read its tracks (copy its tracks into a playlist of your own)")
    cands = [(fuzz.ratio(k, norm_name(c)), c, False) for c in owned]
    cands += [(fuzz.ratio(k, norm_name(c)), c, True) for c in foreign]
    cands = sorted((c for c in cands if c[0] >= 60), reverse=True)[:3]
    if not cands:
        return "no similar playlist name found in your Spotify"
    return "closest Spotify names: " + ", ".join(
        f'"{c}"{" (not yours)" if f else ""} {s:.0f}%' for s, c, f in cands)


def write_report(results, rb_only, sp_only, out_dir: Path, hints: dict | None = None):
    hints = hints or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    md = ["# To download from Spotify -> RekordBox\n"]
    for r in results:
        md.append(f"## {r['name']}  ({r['have']}/{r['total']} already in RekordBox playlist)\n")
        if r["missing"]:
            md.append(f"**Not downloaded yet ({len(r['missing'])}):**\n")
            md += [f"- [ ] {t}" + (f"  ([open]({t.url}))" if t.url else "") for t in r["missing"]]
            md.append("")
        if r["elsewhere"]:
            md.append(f"**In your RekordBox collection but not in this playlist ({len(r['elsewhere'])}):**\n")
            md += [f"- {sp}  <- as _{rb}_" for sp, rb in r["elsewhere"]]
            md.append("")
        if r["unsure"]:
            md.append(f"**Unsure matches - please eyeball ({len(r['unsure'])}):**\n")
            md += [f"- Spotify: {sp}  |  RekordBox: {rb}  ({s:.0f}%)" for sp, rb, s in r["unsure"]]
            md.append("")
        if not (r["missing"] or r["elsewhere"] or r["unsure"]):
            md.append("Everything is downloaded.\n")
    if rb_only:
        md.append("## NOT CHECKED: RekordBox playlists with no same-named Spotify playlist\n")
        md.append("These were skipped, so nothing is known about their missing tracks.\n")
        md += [f"- {n}" + (f"  ({hints[n]})" if n in hints else "") for n in rb_only] + [""]
    if sp_only:
        md.append("## Spotify playlists with no same-named RekordBox playlist\n")
        md += [f"- {n}" for n in sp_only] + [""]
    md.append("---\nTrack information supplied by Spotify. Personal checklist: delete this folder when you are done.\n")
    (out_dir / "to_download.md").write_text("\n".join(md), encoding="utf-8")

    links = [t.url for r in results for t in r["missing"] if t.url]
    (out_dir / "to_download_links.txt").write_text("\n".join(dict.fromkeys(links)), encoding="utf-8")

    with (out_dir / "to_download.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["playlist", "artist", "title", "spotify_url"])
        for r in results:
            for t in r["missing"]:
                w.writerow([r["name"], t.artist, t.title, t.url])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rb-db", type=Path, default=DEFAULT_RB_DB, help="path to RekordBox master.db")
    ap.add_argument("--csv-dir", type=Path, help="use Exportify CSVs from this folder instead of the Spotify API")
    ap.add_argument("--only", nargs="+", help="only check these playlists; a folder name means all playlists inside it")
    ap.add_argument("--tree", action="store_true", help="print your RekordBox folder/playlist structure and exit")
    ap.add_argument("--make-playlist", action="store_true",
                    help="also create a private Spotify playlist with all not-yet-downloaded tracks (API mode only)")
    ap.add_argument("--playlist-name", help="name for that playlist (default: 'RB to download - <date>')")
    ap.add_argument("--out", type=Path, default=Path("report"), help="output folder (default ./report)")
    args = ap.parse_args()

    print("Reading RekordBox (on a copy)...")
    rb_playlists, rb_collection, rb_paths, outline = load_rekordbox(args.rb_db.expanduser())
    print(f"  {len(rb_playlists)} playlists, {len(rb_collection)} tracks in collection")

    if args.tree:
        for depth, name, kind, n in outline:
            label = "[folder]" if kind == "folder" else ("[smart]" if kind == "smart" else f"{n} tracks")
            print(f"  {'    ' * depth}{name}  {label}")
        return

    only = expand_only(args.only, rb_playlists, rb_paths) if args.only else None
    if args.only and not only:
        sys.exit("Nothing to check.")

    print("Reading Spotify...")
    if args.make_playlist and args.csv_dir:
        sys.exit("--make-playlist needs the Spotify API (leave out --csv-dir). "
                 "With CSVs, paste the links in report/to_download_links.txt into a new playlist instead.")
    client = None if args.csv_dir else SpotifyClient(write=args.make_playlist)
    aliases = load_aliases(Path(__file__).with_name("aliases.json"))
    sp_wanted = [sp_name(n, aliases) for n in only] if only else None
    sp_playlists = load_spotify_csv(args.csv_dir) if args.csv_dir else load_spotify_api(client, sp_wanted)
    print(f"  {len(sp_playlists)} playlists")

    results, rb_only, sp_only = compare(rb_playlists, rb_collection, sp_playlists, only, aliases)
    foreign = getattr(client, "foreign_names", [])
    hints = {n: explain_unmatched(n, list(sp_playlists), foreign) for n in rb_only}
    write_report(results, rb_only, sp_only, args.out, hints)

    if rb_only:
        print("\n!! NOT CHECKED (no same-named Spotify playlist):")
        for n in rb_only:
            print(f"   {n}  ->  {hints[n]}")

    print("\n=== Summary ===")
    for r in results:
        print(f"{r['name']:<35} {len(r['missing']):>4} to download   "
              f"({r['have']}/{r['total']} done, {len(r['unsure'])} unsure, {len(r['elsewhere'])} elsewhere)")
    if args.make_playlist:
        name = args.playlist_name or f"RB to download - {time.strftime('%Y-%m-%d')}"
        link = make_temp_playlist(client, results, name)
        if link:
            print(f"\nCreated Spotify playlist '{name}': {link}")

    print(f"\nFull report: {args.out / 'to_download.md'}  and  {args.out / 'to_download.csv'}")


if __name__ == "__main__":
    main()
