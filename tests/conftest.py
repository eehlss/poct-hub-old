import os, sys, pathlib
HERE = pathlib.Path(__file__).resolve()
KIT = HERE.parents[1]
for p in (str(KIT),):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ["DATABASE_URL"] = "sqlite:////tmp/poct_hub_pytest.db"  # never touch a real DB in tests
os.environ.setdefault("SEED_ON_START", "false")
os.environ.pop("META_API_KEY", None)
os.environ["DASHBOARD_USER"] = "demo"
os.environ["DASHBOARD_PASSWORD"] = "pytest-only-not-a-secret"
os.environ.pop("ALLOW_NO_AUTH", None)
