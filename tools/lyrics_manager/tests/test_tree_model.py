# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

import tempfile
import unittest
from pathlib import Path

from tools.lyrics_manager.tree_model import build_registry_tree, filter_registry_tree


class RegistryTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lyrics = Path(self.temp.name) / "Lyrics"
        self.directory = self.lyrics / "Singer" / "Album"
        self.directory.mkdir(parents=True)
        (self.directory / "bound.ttml").write_text("", encoding="utf-8")
        (self.directory / "loose.lrcn").write_text("", encoding="utf-8")
        (self.directory / "lyrics.metadata").write_text("{}", encoding="utf-8")
        (self.directory / ".metadata").write_text("{}", encoding="utf-8")
        (self.directory / "cache.tmp").write_text("", encoding="utf-8")
        self.metadata = {self.directory: {
            "schemaVersion": 2,
            "tracks": {"song-ref": {"title": "Needle Song", "album": "Hidden Album"}},
            "files": [{"name": "bound.ttml", "metadataRef": "song-ref", "linked": True, "linkedFrom": "old-ref"}],
        }}

    def tearDown(self):
        self.temp.cleanup()

    def test_builds_entities_and_keeps_unbound_files(self):
        root = build_registry_tree(self.lyrics, [self.directory], self.metadata)
        directory = root.children[0].children[0]
        self.assertEqual([node.kind for node in directory.children], ["track", "track"])
        self.assertEqual(directory.children[0].metadata_ref, "song-ref")
        self.assertEqual(directory.children[0].children[0].file_name, "bound.ttml")
        self.assertTrue(directory.children[0].children[0].linked)
        self.assertEqual(directory.children[0].children[0].linked_from, "old-ref")
        self.assertEqual(directory.children[1].label, "（未关联）")
        self.assertEqual(directory.children[1].children[0].file_name, "loose.lrcn")
        listed = [file.file_name for entity in directory.children for file in entity.children]
        self.assertNotIn("lyrics.metadata", listed)
        self.assertNotIn(".metadata", listed)
        self.assertNotIn("cache.tmp", listed)

    def test_filter_matches_track_metadata_and_preserves_ancestors(self):
        root = build_registry_tree(self.lyrics, [self.directory], self.metadata)
        filtered = filter_registry_tree(root, "Hidden Album")
        self.assertIsNotNone(filtered)
        track = filtered.children[0].children[0].children[0]
        self.assertEqual(track.metadata_ref, "song-ref")
        self.assertEqual(track.children, ())

    def test_filter_matches_file_name_and_preserves_entity(self):
        root = build_registry_tree(self.lyrics, [self.directory], self.metadata)
        filtered = filter_registry_tree(root, "bound.ttml")
        track = filtered.children[0].children[0].children[0]
        self.assertEqual(track.children[0].file_name, "bound.ttml")


if __name__ == "__main__":
    unittest.main()
