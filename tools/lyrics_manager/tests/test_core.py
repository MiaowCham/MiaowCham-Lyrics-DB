# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

import json
import tempfile
import unittest
from pathlib import Path

from tools.lyrics_manager.core import LyricsDatabase, _atomic_text, parse_lrcn, parse_ttml, preview_lrcn
from tools.lyrics_manager.gui import remove_track_record


TTML = '''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata" xmlns:amll="http://www.example.com/ns/amll" xml:lang="ja"><head><metadata><ttm:title>Old</ttm:title><amll:meta key="artists" value="A"/><amll:meta key="album" value="Keep"/><amll:meta key="unknown" value="untouched"/></metadata></head><body><div><p xml:id="L1"><span>原文</span></p></div></body></tt>'''
LRCN = '''[Lyrics Next]\n[version:2.3]\n[ti:Old]\n[ar:A]\n[songwriter:One]\n[custom:keep]\n[lyrics: format@Lyrics Next]\n[1.0,2.0,v1,L1]<1.0,2.0>原文\n[translate: format@LRCN Trans]\n[L1]译文\n[transliteration: format@LRCN Trans]\n[L1]roman\n'''


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.track_dir = self.root / "Lyrics" / "Artist" / "Folder Title"
        self.track_dir.mkdir(parents=True)
        (self.track_dir / "song.ttml").write_text(TTML, encoding="utf-8")
        (self.track_dir / "song.lrcn").write_text(LRCN, encoding="utf-8")
        self.db = LyricsDatabase(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_text_skips_identical_write(self):
        """Rewriting identical content must not touch the file (nor its mtime).

        Git relies on the index stat cache to detect changes; an unnecessary
        rewrite under ``core.autocrlf=true`` makes Git report an unchanged file
        as modified with an empty diff.  The manager should therefore leave the
        file untouched when the new text is byte-for-byte identical.
        """
        import time
        target = self.root / "unchanged.txt"
        content = "one\ntwo\n"
        _atomic_text(target, content)
        first_mtime = target.stat().st_mtime_ns
        time.sleep(0.02)
        _atomic_text(target, content)
        self.assertEqual(target.stat().st_mtime_ns, first_mtime, "identical rewrite should be skipped")
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        time.sleep(0.02)
        _atomic_text(target, "changed\n")
        self.assertNotEqual(target.stat().st_mtime_ns, first_mtime, "changed content should be rewritten")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed\n")

    def test_extract_ttml_and_lrcn(self):
        ttml = parse_ttml(self.track_dir / "song.ttml")
        self.assertEqual(ttml["track"]["title"], "Old")
        self.assertEqual(ttml["track"]["artists"], ["A"])
        lrcn = parse_lrcn(self.track_dir / "song.lrcn")
        self.assertEqual(lrcn["track"]["songwriters"], ["One"])
        self.assertEqual(lrcn["source"]["metadata"]["custom"], ["keep"])

    def test_manual_metadata_wins_and_arrays_survive_scan(self):
        manual = {"track": {"title": "Manual", "artists": ["X", "Y"], "songwriters": []}, "custom": {"x": 1}}
        (self.track_dir / ".metadata").write_text(json.dumps(manual), encoding="utf-8")
        record = self.db.scan()[0]
        track = next(iter(record["tracks"].values()))
        self.assertEqual(track["title"], "Manual")
        self.assertEqual(track["artists"], ["X", "Y"])
        self.assertEqual(track["songwriters"], [])
        self.assertEqual(record["custom"], {"x": 1})
        self.assertTrue((self.track_dir / "lyrics.metadata").exists())
        self.assertTrue((self.root / "lyrics-index.json").exists())

    def test_save_does_not_sync_and_explicit_sync_preserves_body(self):
        before_ttml = (self.track_dir / "song.ttml").read_text(encoding="utf-8")
        before_lrcn = (self.track_dir / "song.lrcn").read_text(encoding="utf-8")
        metadata = self.db.load_metadata(self.track_dir)
        track = next(iter(metadata["tracks"].values()))
        track.update({"title": "New & Better", "artists": ["X", "Y"], "album": "Album", "language": "zh-CN", "songwriters": ["W1", "W2"], "platforms": {"NCM": ["1", "2"]}})
        self.db.save_metadata(self.track_dir, metadata)
        self.assertEqual((self.track_dir / "song.ttml").read_text(encoding="utf-8"), before_ttml)
        self.assertEqual((self.track_dir / "song.lrcn").read_text(encoding="utf-8"), before_lrcn)
        result = self.db.sync_to_sources(self.track_dir, metadata)
        self.assertEqual(len(result["changed_files"]), 2)
        changed_ttml = (self.track_dir / "song.ttml").read_text(encoding="utf-8")
        self.assertIn("<ttm:title>New &amp; Better</ttm:title>", changed_ttml)
        self.assertIn('key="unknown" value="untouched"', changed_ttml)
        self.assertIn('<body><div><p xml:id="L1"><span>原文</span></p></div></body>', changed_ttml)
        changed_lrcn = (self.track_dir / "song.lrcn").read_text(encoding="utf-8")
        self.assertIn("[custom:keep]", changed_lrcn)
        self.assertIn("[ar:X]", changed_lrcn)
        self.assertIn("[ar:Y]", changed_lrcn)
        self.assertIn("[platform:NCM@1]", changed_lrcn)
        self.assertIn("[1.0,2.0,v1,L1]<1.0,2.0>原文", changed_lrcn)

    def test_three_column_lrcn_preview(self):
        rows = preview_lrcn(self.track_dir / "song.lrcn")
        self.assertEqual(rows[0]["original"], "原文")
        self.assertEqual(rows[0]["translation"], "译文")
        self.assertEqual(rows[0]["transliteration"], "roman")
        self.assertEqual(rows[0]["line_number"], "1")

    def test_two_tracks_sync_only_bound_files(self):
        # Existing pair is song A. Add a second independently identified pair.
        second_ttml = TTML.replace("<ttm:title>Old</ttm:title>", "<ttm:title>Second</ttm:title>").replace('value="A"', 'value="B"')
        second_lrcn = LRCN.replace("[ti:Old]", "[ti:Second]").replace("[ar:A]", "[ar:B]")
        (self.track_dir / "second.ttml").write_text(second_ttml, encoding="utf-8")
        (self.track_dir / "second.lrcn").write_text(second_lrcn, encoding="utf-8")
        metadata = self.db.scan()[0]
        self.assertEqual(len(metadata["tracks"]), 2)
        first_ref = next(ref for ref, track in metadata["tracks"].items() if track["title"] == "Old")
        first_files = [item["name"] for item in metadata["files"] if item["metadataRef"] == first_ref]
        self.assertEqual(set(first_files), {"song.ttml", "song.lrcn"})
        metadata["tracks"][first_ref]["title"] = "Only First Changed"
        before_second = {(self.track_dir / name).read_text(encoding="utf-8") for name in ("second.ttml", "second.lrcn")}
        result = self.db.sync_to_sources(self.track_dir, metadata, metadata_refs=[first_ref])
        self.assertEqual(set(Path(name).name for name in result["changed_files"]), {"song.ttml", "song.lrcn"})
        after_second = {(self.track_dir / name).read_text(encoding="utf-8") for name in ("second.ttml", "second.lrcn")}
        self.assertEqual(before_second, after_second)
        self.assertIn("Only First Changed", (self.track_dir / "song.ttml").read_text(encoding="utf-8"))
        self.assertIn("[ti:Only First Changed]", (self.track_dir / "song.lrcn").read_text(encoding="utf-8"))

    def test_deleted_track_stays_deleted_after_rescan(self):
        metadata = self.db.load_metadata(self.track_dir)
        ref = next(iter(metadata["tracks"]))
        self.db.save_metadata(self.track_dir, remove_track_record(metadata, ref))
        reloaded = self.db.load_metadata(self.track_dir)
        self.assertNotIn(ref, reloaded["tracks"])
        self.assertIsNone(reloaded["files"][0]["metadataRef"])

    def test_sync_linked_file_uses_canonical_track(self):
        metadata = self.db.load_metadata(self.track_dir)
        ref, track = next(iter(metadata["tracks"].items()))
        track = {**track, "title": "Canonical Title"}
        result = self.db.sync_file_to_track(self.track_dir / "song.ttml", track)
        self.assertEqual(len(result["changed_files"]), 1)
        self.assertIn("Canonical Title", (self.track_dir / "song.ttml").read_text(encoding="utf-8"))

    def test_ttml_preview_accepts_itunes_key(self):
        source = TTML.replace('xml:id="L1"', 'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:key="L1"')
        (self.track_dir / "song.ttml").write_text(source, encoding="utf-8")
        self.assertEqual(self.db.preview(self.track_dir / "song.ttml")[0]["line_id"], "L1")

    def test_legacy_body_preview_and_explicit_conversion(self):
        source = '''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata" xmlns:itunes="http://music.apple.com/lyric-ttml-internal"><head><metadata><custom keep="yes"/></metadata></head><body><p itunes:key="L1" ttm:agent="v1"><span>main</span><span ttm:role="x-bg"><span>back</span><span ttm:role="x-translation">背景译</span></span><span ttm:role="x-translation" xml:lang="zh-CN">主译</span><span ttm:role="x-roman"><span>ro</span><span>man</span></span></p></body></tt>'''
        path = self.track_dir / "legacy.ttml"
        path.write_text(source, encoding="utf-8")
        rows = self.db.preview(path)
        self.assertEqual((rows[0]["original"], rows[0]["translation"], rows[0]["transliteration"]),
                         ("main", "主译", "ro\\man"))
        self.assertEqual((rows[1]["line_number"], rows[1]["original"], rows[1]["translation"]),
                         ("bg", "back", "背景译"))
        result = self.db.convert_legacy_ttml(path)
        self.assertTrue(result["changed"])
        backup = path.with_name(path.name + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), source)
        converted = path.read_text(encoding="utf-8")
        self.assertIn('keep="yes"', converted)
        self.assertEqual(self.db.preview(path)[0]["translation"], "主译")
        self.assertFalse(self.db.convert_legacy_ttml(path)["changed"])
        self.db.restore_ttml_backup(path)
        self.assertEqual(path.read_text(encoding="utf-8"), source)
        self.assertFalse(backup.exists())

    def test_lys_agents_and_bg_numbering(self):
        path = self.track_dir / "sample.lys"
        path.write_text("[1]左声道 (1,20)主唱\n[6]背景 (21,20)和声\n[3]第二句\n[8]右侧背景\n", encoding="utf-8")
        rows = self.db.preview(path)
        self.assertEqual(
            [(r["line_number"], r["agent"], r["original"]) for r in rows],
            [("1", "1", "左声道 主唱"), ("bg", "6", "背景 和声"), ("2", "3", "第二句"), ("bg", "8", "右侧背景")],
        )

    def test_qrc_preview_removes_word_timestamps_and_has_no_agent(self):
        path = self.track_dir / "sample.qrc"
        path.write_text("[100,200]Hello (100,80)world(180,120)!\n[300,100]Next line\n", encoding="utf-8")
        rows = self.db.preview(path)
        self.assertEqual([(r["line_number"], r["agent"], r["original"]) for r in rows],
                         [("1", "", "Hello world!"), ("2", "", "Next line")])

    def test_lrcn_background_is_not_numbered(self):
        path = self.track_dir / "background.lrcn"
        path.write_text("[lyrics: format@Lyrics Next]\n[1,2,v1,L1]main\n[x-bg]back\n[translate: format@LRCN Trans]\n[L1]主译\n[x-bg]背景译\n", encoding="utf-8")
        rows = self.db.preview(path)
        self.assertEqual([(r["line_number"], r["original"], r["translation"]) for r in rows],
                         [("1", "main", "主译"), ("bg", "back", "背景译")])

    def test_syllable_transliteration_uses_backslash_separator(self):
        ttml = self.track_dir / "roman.ttml"
        ttml.write_text('''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal"><head><metadata><iTunesMetadata xmlns="http://music.apple.com/lyric-ttml-internal"><transliterations><transliteration><text for="L1"><span>ro</span><span>man</span></text></transliteration></transliterations></iTunesMetadata></metadata></head><body><p itunes:key="L1">原文</p></body></tt>''', encoding="utf-8")
        self.assertEqual(self.db.preview(ttml)[0]["transliteration"], "ro\\man")
        lrcn = self.track_dir / "roman.lrcn"
        lrcn.write_text('''[lyrics: format@Lyrics Next]\n[1,2,,L1]原文\n[transliteration: format@LRCN Trans]\n[L1]<1,1.5>ro<1.5,2>man\n''', encoding="utf-8")
        self.assertEqual(self.db.preview(lrcn)[0]["transliteration"], "ro\\man")

    def test_line_transliteration_has_no_separator(self):
        lrcn = self.track_dir / "line-roman.lrcn"
        lrcn.write_text('''[lyrics: format@Lyrics Next]\n[1,2,,L1]原文\n[transliteration: format@LRCN Trans]\n[L1]roman words\n''', encoding="utf-8")
        self.assertEqual(self.db.preview(lrcn)[0]["transliteration"], "roman words")

    def test_repeated_ids_writers_and_empty_values_round_trip(self):
        metadata = self.db.load_metadata(self.track_dir)
        ref, track = next(iter(metadata["tracks"].items()))
        track.update({"songwriters": ["W1", "W2"], "isrc": "CODE", "platforms": {"ncmMusicId": ["11", "22"]}})
        self.db.sync_to_sources(self.track_dir, metadata, metadata_refs=[ref])
        ttml = (self.track_dir / "song.ttml").read_text(encoding="utf-8")
        lrcn = (self.track_dir / "song.lrcn").read_text(encoding="utf-8")
        self.assertEqual(ttml.count('key="songwriters"'), 2)
        self.assertEqual(ttml.count('key="ncmMusicId"'), 2)
        self.assertIn('key="isrc" value="CODE"', ttml)
        self.assertIn("[songwriter:W1]", lrcn)
        self.assertIn("[songwriter:W2]", lrcn)
        self.assertIn("[platform:ncmMusicId@22]", lrcn)
        track.update({"songwriters": [], "isrc": "", "platforms": {"ncmMusicId": []}})
        self.db.sync_to_sources(self.track_dir, metadata, metadata_refs=[ref])
        ttml = (self.track_dir / "song.ttml").read_text(encoding="utf-8")
        lrcn = (self.track_dir / "song.lrcn").read_text(encoding="utf-8")
        self.assertNotIn('key="songwriters"', ttml)
        self.assertNotIn('key="ncmMusicId"', ttml)
        self.assertNotIn('key="isrc"', ttml)
        self.assertNotIn("[songwriter:", lrcn)
        self.assertNotIn("[platform:", lrcn)
        self.assertNotIn("[isrc:", lrcn)


if __name__ == "__main__":
    unittest.main()
