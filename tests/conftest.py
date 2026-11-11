import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests must NEVER touch a real/deployed database, even if a .env with DATABASE_URL exists
# (python-dotenv does not override variables that are already set). Default: a throw-away
# SQLite file. To run the same suite against PostgreSQL:
#     TEST_DATABASE_URL=postgresql://user@127.0.0.1:5432/smat_test pytest
# Existing tests expect the next player to open immediately; hold behaviour has its own tests.
os.environ.setdefault("BID_GRACE_SECONDS", "0")
os.environ.setdefault("RESULT_HOLD_SOLD", "0")
os.environ.setdefault("RESULT_HOLD_UNSOLD", "0")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or (
    "sqlite:///" + str(Path(tempfile.mkdtemp(prefix="smat-test-")) / "test.db"))
