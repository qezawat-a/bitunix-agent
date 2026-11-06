"""Test configuration.

Isolates tests from real user state: settings.json / memory.json / journal.jsonl
live in STATE_DIR, resolved from JRROCK_STATE_DIR at import time. We set a temp
dir BEFORE any jrock module is imported so tests never touch real user state.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="jrock-test-state-")
os.environ["JRROCK_STATE_DIR"] = _tmp
