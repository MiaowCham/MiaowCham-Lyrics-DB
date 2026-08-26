# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

"""Build the public GitHub Pages snapshot from lyrics-index.json."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import quote


PLATFORM_ALIASES = {
    "ncmMusicId": ("netease", "ncm", "wyyyy"),
    "qqMusicId": ("qq", "qqmusic", "qq-music", "qm"),
    "appleMusicId": ("apple", "am", "apple-music", "applemusic"),
    "spotifyId": ("spotify",),
}


def export(repo: Path, output: Path, source_commit: str, github_repository: str) -> Path:
    index = json.loads((repo / "lyrics-index.json").read_text(encoding="utf-8"))
    if not isinstance(index.get("tracks"), list):
        raise ValueError("lyrics-index.json 缺少 tracks 数组")
    template = repo / "tools" / "pages-template"
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(template, output)
    public_tracks = []
    for entry in index["tracks"]:
        track = entry.get("track", {})
        directory = str(entry.get("path", ""))
        files = []
        for name in entry.get("files", []):
            relative = "/".join(("Lyrics", directory, str(name)))
            encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
            raw_url = f"https://raw.githubusercontent.com/{github_repository}/{source_commit}/{encoded}"
            files.append({
                "name": str(name),
                "path": relative,
                "url": raw_url,
                "githubUrl": f"https://github.com/{github_repository}/blob/{source_commit}/{encoded}",
            })
        routes = []
        for key, aliases in PLATFORM_ALIASES.items():
            raw_ids = entry.get("platforms", {}).get(key, [])
            ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
            for value in ids:
                value = str(value).strip()
                if not value:
                    continue
                for alias in aliases:
                    for file in files:
                        suffix = Path(file["name"]).suffix or ".txt"
                        routes.append({
                            "platform": alias,
                            "id": value,
                            "file": file["name"],
                            "url": file["url"],
                            "virtualPath": raw_url,
                            "displayPath": f"/{alias}/{quote(value, safe='')}{suffix}",
                        })
        public_tracks.append({
            "path": directory,
            "metadataRef": entry.get("metadataRef", ""),
            "title": track.get("title", ""),
            "artists": track.get("artists", []),
            "album": track.get("album", ""),
            "language": track.get("language", ""),
            "platformIds": entry.get("platforms", {}),
            "files": files,
            "routes": routes,
            "metadata": entry,
        })
    data_dir = output / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / "index.json"
    destination.write_text(json.dumps({
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "tracks": public_tracks,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(repo / "LICENSE-APACHE", output / "LICENSE")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--github-repository", default="MiaowCham/MiaowCham-Lyrics-DB")
    args = parser.parse_args()
    print(export(args.repo.resolve(), args.output.resolve(), args.source_commit, args.github_repository))


if __name__ == "__main__":
    main()
