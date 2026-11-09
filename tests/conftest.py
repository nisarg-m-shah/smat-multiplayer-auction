import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tests never connect to the user's configured/deployed DB. TEST_DATABASE_URL is
# an explicit opt-in for PostgreSQL integration testing.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or (
    "sqlite:///" + str(Path(tempfile.mkdtemp(prefix="smat-test-")) / "test.db"))
