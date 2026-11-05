"""Root-level entrypoint for Railway / platforms that expect main.py at root."""
import sys
from pathlib import Path

# Ensure the project root is on sys.path so jrock imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jrock.main import main

if __name__ == "__main__":
    main()