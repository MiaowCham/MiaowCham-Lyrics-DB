# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Pure metadata-operation tests for GUI commands."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.lyrics_manager.gui import link_file_record, rename_file_record, scan_library_data, split_file_record


class GuiMetadataLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "tracks": {
                "first": {"title": "第一首", "artists": ["A"]},
                "second": {"title": "第二首", "artists": ["B"]},
            },
            "files": [{"name": "song.ttml", "format": "ttml", "metadataRef": "first"}],
            "sources": [{"file": "song.ttml", "name": "embedded"}, {"manual": True}],
        }

    def test_link_records_target_and_original_reference(self) -> None:
        updated = link_file_record(self.metadata, "song.ttml", "second")
        record = updated["files"][0]
        self.assertEqual(record["metadataRef"], "second")
        self.assertTrue(record["linked"])
        self.assertEqual(record["linkedFrom"], "first")
        self.assertEqual(self.metadata["files"][0]["metadataRef"], "first")

    def test_split_clones_target_and_clears_link_markers(self) -> None:
        linked = link_file_record(self.metadata, "song.ttml", "second")
        updated = split_file_record(linked, "song.ttml", "second", "second-song")
        self.assertEqual(updated["tracks"]["second-song"], updated["tracks"]["second"])
        self.assertIsNot(updated["tracks"]["second-song"], updated["tracks"]["second"])
        record = updated["files"][0]
        self.assertEqual(record["metadataRef"], "second-song")
        self.assertNotIn("linked", record)
        self.assertNotIn("linkedFrom", record)

    def test_rename_updates_file_format_and_source_references(self) -> None:
        updated = rename_file_record(self.metadata, "song.ttml", "renamed.lrcn")
        self.assertEqual(updated["files"][0]["name"], "renamed.lrcn")
        self.assertEqual(updated["files"][0]["format"], "lrcn")
        self.assertEqual(updated["sources"][0]["file"], "renamed.lrcn")
        self.assertEqual(self.metadata["files"][0]["name"], "song.ttml")

    def test_rename_rejects_unsafe_or_reserved_names(self) -> None:
        for name in ("", "../song.ttml", "lyrics.metadata", ".metadata"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                rename_file_record(self.metadata, "song.ttml", name)

    def test_scan_library_data_has_no_gui_dependency(self) -> None:
        with TemporaryDirectory() as temp:
            repo = Path(temp)
            directory = repo / "Lyrics" / "Artist" / "Song"
            directory.mkdir(parents=True)
            (directory / "song.lrc").write_text("[00:00.00]line", encoding="utf-8")

            class Adapter:
                def scan(self) -> list[Path]:
                    return [directory]

                def load(self, _path: Path) -> dict:
                    return self_metadata

            self_metadata = self.metadata
            directories, cache, model = scan_library_data(Adapter(), repo)
            self.assertEqual(directories, [directory])
            self.assertIs(cache[directory], self.metadata)
            self.assertEqual(model.kind, "root")


if __name__ == "__main__":
    unittest.main()
