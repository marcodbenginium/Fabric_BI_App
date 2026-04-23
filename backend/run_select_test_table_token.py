import os
import json
import traceback
import argparse
from importlib import util

MODULE_PATH = os.path.join(os.path.dirname(__file__), "fabric_DB_SEMANTIC_SEARCH.py")
spec = util.spec_from_file_location("fdb", MODULE_PATH)
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)

parser = argparse.ArgumentParser(description="Test token-based SELECT with optional claims printing")
parser.add_argument("--no-claims", action="store_true", help="Disable printing token claims")
args = parser.parse_args()

print("Using app-side token authentication (use_driver_auth=False)")

conn = None
try:
    cred = mod.get_credential()
    token = cred.get_token(mod._AZURE_SCOPE).token
    claims = mod._decode_jwt(token)

    if not args.no_claims:
        print("Token claims:")
        print(json.dumps(claims, indent=2, ensure_ascii=False))

    conn = mod.get_connection(use_driver_auth=False, credential=cred)
    rows = mod.execute_query("SELECT * FROM dbo.TEST_TABLE", conn=conn)
    print(json.dumps(rows, default=str, indent=2, ensure_ascii=False))

except Exception:
    traceback.print_exc()

finally:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            traceback.print_exc()