"""Shared test fixtures.

Adds the parent `status/` dir to sys.path so tests can `import main` without
installing the service as a package.
"""

import sys
from pathlib import Path

# status/ is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
