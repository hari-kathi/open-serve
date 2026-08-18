"""Shared test fixtures for the probe.

The probe reads its config file and key map at import time, so this conftest
materializes temp config files and points the env vars at them BEFORE any
test module imports `main`.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# probe/ is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BEARER_TOKEN = "probe-token-123"

_config = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
_config.write(
    """\
schedule:
  intervalSeconds: 60
defaultTimeoutSeconds: 10
externalUrl: https://models.example.com
authBearerKey: probe
targets:
  - modelId: qwen3-8b
    runner: chat
    internalUrl: http://qwen3-8b-serve-svc:8000
  - modelId: qwen3-embed
    runner: embedding
"""
)
_config.close()

_key_map = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({BEARER_TOKEN: "probe"}, _key_map)
_key_map.close()

os.environ["PROBE_CONFIG_FILE"] = _config.name
os.environ["API_KEY_MAP_FILE"] = _key_map.name
