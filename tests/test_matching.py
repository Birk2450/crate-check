import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crate_check as c

T = c.Track


def s(a_title, a_artist, b_title, b_artist):
    return c.score(T(a_title, a_artist), T(b_title, b_artist))


def test_identical_is_a_match():
    assert s("Song", "Artist", "Song", "Artist") >= c.MATCH


def test_feat_and_descriptors_are_ignored():
    assert s("Song (feat. Guest)", "Artist, Guest", "Song", "Artist") >= c.MATCH
    assert s("Song - Original Mix", "Artist", "Song", "Artist") >= c.MATCH


def test_extended_mix_is_not_the_plain_song():
    assert s("Song", "Artist", "Song (Extended Mix)", "Artist") < c.MATCH


def test_same_extended_mix_matches_in_both_spellings():
    assert s("Song - Extended Mix", "Artist", "Song [Extended Mix]", "Artist") >= c.MATCH


def test_remix_is_not_the_plain_song():
    assert s("Song (Some Remix)", "Artist", "Song", "Artist") < c.MATCH


def test_different_songs_do_not_match():
    assert s("Alpha", "Artist", "Omega", "Other") < c.UNSURE


def test_playlist_names_ignore_case_spaces_accents_emoji():
    assert c.norm_name("Afro House") == c.norm_name("AfroHouse") == c.norm_name("🔥 afro-house")
    assert c.norm_name("Café") == c.norm_name("cafe")


def test_aliases(tmp_path):
    f = tmp_path / "aliases.json"
    f.write_text('{"_about": "x", "A / B": "B / A"}', encoding="utf-8")
    aliases = c.load_aliases(f)
    assert c.sp_name("A / B", aliases) == "B / A"
    assert c.sp_name("Other", aliases) == "Other"
    assert c.load_aliases(tmp_path / "missing.json") == {}


def test_folder_expansion(capsys):
    playlists = {"Amapiano": [], "Deep": [], "Solo": []}
    paths = {"Amapiano": ["House"], "Deep": ["House"], "Solo": []}
    assert sorted(c.expand_only(["House"], playlists, paths)) == ["Amapiano", "Deep"]
    assert c.expand_only(["Solo"], playlists, paths) == ["Solo"]
    assert c.expand_only(["Nope"], playlists, paths) == []
