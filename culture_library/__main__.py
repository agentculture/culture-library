"""Entry point for ``python -m culture_library``."""

from __future__ import annotations

import sys

from culture_library.cli import main

if __name__ == "__main__":
    sys.exit(main())
