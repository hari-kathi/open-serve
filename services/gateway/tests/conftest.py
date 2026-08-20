"""Shared test fixtures for the gateway.

The gateway reads its key map and routing tables from the environment at
import time, so this conftest materializes a temp key-map file and points the
relevant env vars at it BEFORE any test module imports `main`.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# gateway/ is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_API_KEY = "test-key-123"
TEST_SOURCE = "test-suite"
BACKEND_A = "http://backend-a.test"
BACKEND_B = "http://backend-b.test"
STATUS_URL = "http://status.test"

MODEL_ROUTES = {
    "qwen3-8b": BACKEND_A,
    "llama-3-70b": BACKEND_B,
    "embed-small": BACKEND_B,
}

_key_map = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({TEST_API_KEY: TEST_SOURCE}, _key_map)
_key_map.close()

os.environ["API_KEY_MAP_FILE"] = _key_map.name
os.environ["MODEL_ROUTES"] = json.dumps(MODEL_ROUTES)
os.environ["PUBLIC_ROUTES"] = json.dumps({"/status": STATUS_URL, "/static/": STATUS_URL})
