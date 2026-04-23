import os
from importlib import util

MODULE_PATH = os.path.join(os.path.dirname(__file__), "fabric_DB_SEMANTIC_SEARCH.py")
spec = util.spec_from_file_location("fdb", MODULE_PATH)
mod = util.module_from_spec(spec)
spec.loader.exec_module(mod)

cred = mod.get_credential()
print(mod.get_token_claims(cred))
