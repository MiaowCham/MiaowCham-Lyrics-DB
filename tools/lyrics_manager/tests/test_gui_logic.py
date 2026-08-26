# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Pure metadata-operation tests for GUI commands."""

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.lyrics_manager.gui import (
    link_file_record,
    link_file_to_external_target,
    make_linked_file_independent,
    move_file_records_to_target,
    rename_file_record,
    detach_external_link_record,
    remove_file_record,
    remove_track_record,
    scan_library_data,
    split_file_record,
)


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

    def test_link_to_external_target_keeps_source_binding_and_records_target(self) -> None:
        updated = link_file_to_external_target(
            self.metadata,
            "song.ttml",
            "Other Artist/Other Song",
            "other-track",
        )
        record = updated["files"][0]
        # The local record remains bound to its source entity.  The external
        # reference is explicit, so it cannot collide with a same-named ref
        # in another directory.
        self.assertEqual(record["metadataRef"], "first")
        self.assertTrue(record["linked"])
        self.assertEqual(record["linkedFrom"], "first")
        self.assertEqual(record["linkedTarget"], {
            "path": "Other Artist/Other Song",
            "metadataRef": "other-track",
        })
        self.assertEqual(self.metadata["files"][0]["metadataRef"], "first")
        self.assertNotIn("linkedTarget", self.metadata["files"][0])

    def test_external_link_rejects_missing_target_parts(self) -> None:
        for path, ref in (("", "target"), ("Other/Song", "")):
            with self.subTest(path=path, ref=ref), self.assertRaises(ValueError):
                link_file_to_external_target(self.metadata, "song.ttml", path, ref)

    def test_move_records_transfers_selected_files_and_marks_target_link(self) -> None:
        source = copy.deepcopy(self.metadata)
        source["files"].append({"name": "second.lrcn", "format": "lrcn", "metadataRef": "second"})
        target = {
            "tracks": {"target": {"title": "目标曲目"}},
            "files": [{"name": "already.ttml", "format": "ttml", "metadataRef": "target"}],
            "sources": [],
        }
        moved_source, moved_target = move_file_records_to_target(
            source, target, ["song.ttml", "second.lrcn"], "target"
        )
        self.assertEqual([item["name"] for item in moved_source["files"]], [])
        self.assertEqual(
            [item["name"] for item in moved_target["files"]],
            ["already.ttml", "song.ttml", "second.lrcn"],
        )
        for item, source_ref in zip(moved_target["files"][1:], ("first", "second"), strict=True):
            self.assertEqual(item["metadataRef"], "target")
            self.assertTrue(item["linked"])
            self.assertEqual(item["linkedFrom"], source_ref)
        self.assertEqual(source["files"][0]["metadataRef"], "first")
        self.assertEqual(target["files"], [{"name": "already.ttml", "format": "ttml", "metadataRef": "target"}])

    def test_move_records_rejects_unknown_target_or_files(self) -> None:
        target = {"tracks": {}, "files": []}
        with self.assertRaises(ValueError):
            move_file_records_to_target(self.metadata, target, ["song.ttml"], "missing")
        target["tracks"]["target"] = {"title": "目标"}
        with self.assertRaises(ValueError):
            move_file_records_to_target(self.metadata, target, ["absent.ttml"], "target")
        target["files"].append({"name": "song.ttml", "metadataRef": "target"})
        with self.assertRaises(ValueError):
            move_file_records_to_target(self.metadata, target, ["song.ttml"], "target")

    def test_rename_updates_file_format_and_source_references(self) -> None:
        updated = rename_file_record(self.metadata, "song.ttml", "renamed.lrcn")
        self.assertEqual(updated["files"][0]["name"], "renamed.lrcn")
        self.assertEqual(updated["files"][0]["format"], "lrcn")
        self.assertEqual(updated["sources"][0]["file"], "renamed.lrcn")

    def test_remove_file_record_unbinds_file_and_source_reference(self) -> None:
        self.metadata["sources"] = [{"file": "song.ttml", "url": "https://example.invalid"}]
        updated = remove_file_record(self.metadata, "song.ttml")
        self.assertEqual(updated["files"], [])
        self.assertNotIn("file", updated["sources"][0])
        self.assertEqual(self.metadata["files"][0]["name"], "song.ttml")
        with self.assertRaises(ValueError):
            remove_file_record(self.metadata, "missing.ttml")

    def test_remove_track_keeps_external_links_and_detach_restores_source_binding(self) -> None:
        external = link_file_to_external_target(self.metadata, "song.ttml", "Other/Song", "target")
        removed = remove_track_record(external, "first")
        self.assertNotIn("first", removed["tracks"])
        self.assertEqual(removed["files"][0]["linkedTarget"]["metadataRef"], "target")
        detached = detach_external_link_record(removed, "song.ttml")
        self.assertEqual(detached["files"][0]["metadataRef"], "first")
        self.assertNotIn("linked", detached["files"][0])
        with self.assertRaises(ValueError):
            remove_track_record(self.metadata, "missing")

    def test_linked_file_can_become_independent_entity(self) -> None:
        linked = link_file_to_external_target(self.metadata, "song.ttml", "Other/Song", "target")
        updated = make_linked_file_independent(linked, "song.ttml", "own-copy", {"title": "独立曲目"})
        self.assertEqual(updated["tracks"]["own-copy"]["title"], "独立曲目")
        record = updated["files"][0]
        self.assertEqual(record["metadataRef"], "own-copy")
        self.assertNotIn("linked", record)
        self.assertNotIn("linkedTarget", record)
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
