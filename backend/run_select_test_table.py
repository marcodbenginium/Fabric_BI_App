import os
import json
import traceback
from importlib import util

MODULE_PATH = os.path.join(os.path.dirname(__file__), "fabric_DB_SEMANTIC_SEARCH.py")
spec = util.spec_from_file_location("fdb", MODULE_PATH)
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Ensure driver interactive auth (browser) is used by default
os.environ.setdefault("USE_DRIVER_AUTH", "1")

try:
    rows = mod.execute_query("SELECT * FROM dbo.TEST_TABLE")
    print(json.dumps(rows, default=str, indent=2, ensure_ascii=False))
except Exception:
    traceback.print_exc()
