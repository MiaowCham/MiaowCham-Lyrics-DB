# Copyright 2026 MiaowCham Lyrics DB contributors
# Licensed under the Apache License, Version 2.0. See http://www.apache.org/licenses/LICENSE-2.0

"""Windows double-click launcher for MiaowCham Lyrics Manager."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lyrics_manager.gui import main  # noqa: E402


if __name__ == "__main__":
    main()
