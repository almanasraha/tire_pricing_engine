# Ensures the project root (one level up from tests/) is importable
# regardless of how the test runner is invoked (e.g. `python3 -m unittest
# discover` from the project root, or `python3 tests/test_engine.py`
# directly from within tests/), since none of the engine modules are
# installed as a package -- they're run in place.
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
