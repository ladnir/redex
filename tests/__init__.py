"""Test helpers and contract tests for Redex."""

from pathlib import Path
import sys
import types


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
PACKAGE_DIR = SRC_DIR / "redex"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

existing = sys.modules.get("redex")
existing_file = getattr(existing, "__file__", "")
if existing is None or (isinstance(existing_file, str) and existing_file.endswith("redex.py")):
    package = types.ModuleType("redex")
    package.__path__ = [str(PACKAGE_DIR)]
    package.__package__ = "redex"
    sys.modules["redex"] = package
