"""
get_visual_fields.py
====================
Recupera i campi (colonne/misure) utilizzati nei visual di una o più pagine
di un report Fabric/Power BI, direttamente via Fabric REST API (getDefinition).

Uso:
    py get_visual_fields.py <workspace_id> <report_id> [<page_id>]

    Se <page_id> è omesso, vengono analizzate tutte le pagine del report.

Autenticazione: AzureCliCredential (az login).
"""

import sys
import json
import base64
import time
import re
import requests
from azure.identity import AzureCliCredential

# ---------------------------------------------------------------------------
# Auth + API
# ---------------------------------------------------------------------------

BASE_URL = "https://api.fabric.microsoft.com/v1"


def get_token() -> str:
    credential = AzureCliCredential()
    return credential.get_token("https://api.fabric.microsoft.com/.default").token


def _wait_for_lro(session: requests.Session, location: str, max_attempts: int = 12) -> dict:
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(5)
        resp = session.get(location, timeout=30)
        if not resp.ok:
            return {"error": f"LRO poll failed: {resp.status_code} {resp.text[:200]}"}
        data = resp.json()
        status = data.get("status", "").lower()
        if status == "succeeded":
            r = session.get(f"{location}/result", timeout=60)
            return r.json() if r.ok else {"error": "Cannot fetch LRO result"}
        if status == "failed":
            return {"error": data.get("error", "LRO failed")}
    return {"error": "LRO timed out"}


def get_report_definition(session: requests.Session, workspace_id: str, report_id: str) -> dict:
    url = f"{BASE_URL}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    resp = session.post(url, timeout=30)

    if resp.status_code == 200:
        raw = resp.json()
    elif resp.status_code == 202:
        location = resp.headers.get("Location")
        if not location:
            return {"error": "202 senza Location header"}
        raw = _wait_for_lro(session, location)
    else:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    if "error" in raw:
        return raw

    for part in raw.get("definition", {}).get("parts", []):
        if part.get("payloadType") == "InlineBase64" and "payload" in part:
            try:
                decoded = base64.b64decode(part["payload"]).decode("utf-8")
                try:
                    part["payload"] = json.loads(decoded)
                except json.JSONDecodeError:
                    part["payload"] = decoded
            except Exception:
                pass

    return raw


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _resolve_field_ref(field: dict) -> str:
    """
    Dato un nodo 'field' del queryState, restituisce una stringa leggibile
    che rappresenta la colonna o la misura, es:
      - "Query.COUNTRY"
      - "Sum(Query.REVENUE)"
      - "Count(Query.ORDERS)"
    """
    AGG_MAP = {0: "Sum", 1: "Avg", 2: "Min", 3: "Max", 4: "Count",
               5: "CountNonNull", 6: "StandardDeviation", 7: "Variance",
               8: "Median"}

    if "Column" in field:
        entity = field["Column"]["Expression"]["SourceRef"]["Entity"]
        prop = field["Column"]["Property"]
        return f"{entity}.{prop}"

    if "Aggregation" in field:
        agg_node = field["Aggregation"]
        func_id = agg_node.get("Function", 0)
        func_name = AGG_MAP.get(func_id, f"Agg{func_id}")
        inner = _resolve_field_ref(agg_node["Expression"])
        return f"{func_name}({inner})"

    if "Measure" in field:
        entity = field["Measure"]["Expression"]["SourceRef"]["Entity"]
        prop = field["Measure"]["Property"]
        return f"{entity}[{prop}]"

    if "HierarchyLevel" in field:
        hl = field["HierarchyLevel"]
        entity = hl["Expression"]["SourceRef"]["Entity"]
        hierarchy = hl.get("Hierarchy", "")
        level = hl.get("Level", "")
        return f"{entity}.{hierarchy}.{level}"

    return json.dumps(field)  # fallback: mostra il JSON grezzo


def extract_fields(visual_json: dict) -> list[dict]:
    """
    Ritorna una lista di dict con:
      { "role": <nome del bucket>, "field": <stringa leggibile>, "query_ref": <queryRef> }
    """
    results = []
    query_state = visual_json.get("visual", {}).get("query", {}).get("queryState", {})

    for role, role_data in query_state.items():
        projections = role_data.get("projections", [])
        for proj in projections:
            field_ref = proj.get("field", {})
            query_ref = proj.get("queryRef", "")
            native_ref = proj.get("nativeQueryRef", "")
            try:
                field_str = _resolve_field_ref(field_ref)
            except Exception:
                field_str = query_ref or "?"
            results.append({
                "role": role,
                "field": field_str,
                "query_ref": native_ref or query_ref,
            })

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze_report(workspace_id: str, report_id: str, page_filter: str | None = None):
    token = get_token()
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })

    print(f"\nFetch definition — workspace: {workspace_id}  report: {report_id}")
    definition = get_report_definition(session, workspace_id, report_id)

    if "error" in definition:
        print(f"ERRORE: {definition['error']}")
        sys.exit(1)

    parts = definition.get("definition", {}).get("parts", [])
    if not parts:
        print("Nessuna part trovata nella definizione.")
        sys.exit(1)

    page_re = re.compile(r"definition/pages/([^/]+)/page\.json")
    visual_re = re.compile(r"definition/pages/([^/]+)/visuals/([^/]+)/visual\.json")

    pages: dict[str, dict] = {}

    for part in parts:
        path = part.get("path", "")
        payload = part.get("payload", {})

        m = page_re.match(path)
        if m:
            pid = m.group(1)
            if page_filter and pid != page_filter:
                continue
            pages.setdefault(pid, {"display_name": "", "visuals": []})
            if isinstance(payload, dict):
                pages[pid]["display_name"] = payload.get("displayName", pid)
            continue

        m = visual_re.match(path)
        if m:
            pid, vid = m.group(1), m.group(2)
            if page_filter and pid != page_filter:
                continue
            pages.setdefault(pid, {"display_name": pid, "visuals": []})
            pages[pid]["visuals"].append({"id": vid, "payload": payload})

    if not pages:
        print("Nessuna pagina trovata (verifica il page_id se specificato).")
        sys.exit(1)

    # Raccoglie anche i campi distinti per riepilogo
    all_fields: dict[str, set] = {}  # field_str → set of visual_types

    for pid, pdata in pages.items():
        display = pdata["display_name"] or pid
        visuals = pdata["visuals"]

        print(f"\n{'='*72}")
        print(f"Pagina: {display}  (id: {pid})")
        print(f"{'='*72}")

        for v in visuals:
            vid = v["id"]
            payload = v["payload"]
            if not isinstance(payload, dict):
                continue

            vtype = payload.get("visual", {}).get("visualType", "?")
            fields = extract_fields(payload)

            if not fields:
                print(f"\n  [{vtype}] {vid}  — nessun campo (shape/textbox/decorativo)")
                continue

            print(f"\n  [{vtype}] {vid}")
            print(f"  {'Ruolo':<20} {'Campo':<40} {'Query ref'}")
            print(f"  {'-'*20} {'-'*40} {'-'*30}")
            for f in fields:
                print(f"  {f['role']:<20} {f['field']:<40} {f['query_ref']}")
                all_fields.setdefault(f["field"], set()).add(vtype)

    # Riepilogo campi distinti
    print(f"\n{'='*72}")
    print("RIEPILOGO CAMPI DISTINTI")
    print(f"{'='*72}")
    print(f"{'Campo':<40} {'Usato in (tipi visual)'}")
    print(f"{'-'*40} {'-'*30}")
    for field, vtypes in sorted(all_fields.items()):
        print(f"{field:<40} {', '.join(sorted(vtypes))}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    ws_id = sys.argv[1]
    rp_id = sys.argv[2]
    pg_id = sys.argv[3] if len(sys.argv) > 3 else None

    analyze_report(ws_id, rp_id, pg_id)
