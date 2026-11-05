"""Entrypoint shim for platforms that expect app/main.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jrock.main import main

if __name__ == "__main__":
    main()
