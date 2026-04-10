"""
get_visual_titles.py
====================
Recupera i titoli dei visual di una o più pagine di un report Fabric/Power BI
direttamente via Fabric REST API (getDefinition), senza MCP server.

Uso:
    py get_visual_titles.py <workspace_id> <report_id> [<page_id>]

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
# Auth
# ---------------------------------------------------------------------------

def get_token() -> str:
    credential = AzureCliCredential()
    token = credential.get_token("https://api.fabric.microsoft.com/.default")
    return token.token


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

BASE_URL = "https://api.fabric.microsoft.com/v1"


def _wait_for_lro(session: requests.Session, location: str, max_attempts: int = 12) -> dict:
    """Poll a Fabric LRO until Succeeded/Failed."""
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(5)
        resp = session.get(location, timeout=30)
        if not resp.ok:
            return {"error": f"LRO poll failed: {resp.status_code} {resp.text[:200]}"}
        data = resp.json()
        status = data.get("status", "").lower()
        if status == "succeeded":
            result_resp = session.get(f"{location}/result", timeout=60)
            return result_resp.json() if result_resp.ok else {"error": "Cannot fetch LRO result"}
        if status == "failed":
            return {"error": data.get("error", "LRO failed")}
    return {"error": "LRO timed out"}


def get_report_definition(session: requests.Session, workspace_id: str, report_id: str) -> dict:
    """POST getDefinition e gestisce 200 sincrono e 202 asincrono."""
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

    # Decodifica payload base64 → JSON/testo
    for part in raw.get("definition", {}).get("parts", []):
        if part.get("payloadType") == "InlineBase64" and "payload" in part:
            try:
                decoded = base64.b64decode(part["payload"]).decode("utf-8")
                try:
                    part["payload"] = json.loads(decoded)
                except json.JSONDecodeError:
                    part["payload"] = decoded
            except Exception:
                pass  # lascia il payload originale

    return raw


# ---------------------------------------------------------------------------
# Title extraction helpers
# ---------------------------------------------------------------------------

def _literal_value(obj) -> str | None:
    """Estrae il valore da una struttura expr.Literal.Value (rimuove le virgolette singole)."""
    try:
        raw = obj["expr"]["Literal"]["Value"]
        return raw.strip("'")
    except (KeyError, TypeError):
        return None


def _textbox_text(visual_obj: dict) -> str | None:
    try:
        runs = visual_obj["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
        return " ".join(r.get("value", "") for r in runs).strip() or None
    except (KeyError, TypeError, IndexError):
        return None


def extract_visual_title(visual_json: dict) -> str | None:
    """Restituisce il titolo del visual o None se assente."""
    visual = visual_json.get("visual", {})
    visual_type = visual.get("visualType", "")

    # 1. Titolo esplicito nel container header
    try:
        title_props = visual["visualContainerObjects"]["title"][0]["properties"]
        show = _literal_value(title_props.get("show", {}))
        if show != "false":
            text = _literal_value(title_props.get("text", {}))
            if text:
                return text
    except (KeyError, TypeError, IndexError):
        pass

    # 2. Textbox: usa il testo del paragrafo
    if visual_type == "textbox":
        return _textbox_text(visual)

    return None


# ---------------------------------------------------------------------------
# Main logic
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

    # Raggruppa le parts per pagina
    page_pattern = re.compile(r"definition/pages/([^/]+)/page\.json")
    visual_pattern = re.compile(r"definition/pages/([^/]+)/visuals/([^/]+)/visual\.json")

    pages: dict[str, dict] = {}  # page_id → {"display_name": ..., "visuals": [...]}

    for part in parts:
        path = part.get("path", "")
        payload = part.get("payload", {})

        m = page_pattern.match(path)
        if m:
            pid = m.group(1)
            if page_filter and pid != page_filter:
                continue
            pages.setdefault(pid, {"display_name": "", "visuals": []})
            if isinstance(payload, dict):
                pages[pid]["display_name"] = payload.get("displayName", pid)
            continue

        m = visual_pattern.match(path)
        if m:
            pid, vid = m.group(1), m.group(2)
            if page_filter and pid != page_filter:
                continue
            pages.setdefault(pid, {"display_name": pid, "visuals": []})
            pages[pid]["visuals"].append({"id": vid, "payload": payload})

    if not pages:
        print("Nessuna pagina trovata (verifica il page_id se specificato).")
        sys.exit(1)

    # Output
    for pid, pdata in pages.items():
        display = pdata["display_name"] or pid
        visuals = pdata["visuals"]
        print(f"\n{'='*70}")
        print(f"Pagina: {display}  (id: {pid})")
        print(f"{'='*70}")
        print(f"{'#':<4} {'Visual ID':<24} {'Tipo':<28} {'Titolo'}")
        print(f"{'-'*4} {'-'*24} {'-'*28} {'-'*30}")

        for i, v in enumerate(visuals, 1):
            vid = v["id"]
            vpayload = v["payload"]
            vtype = vpayload.get("visual", {}).get("visualType", "?") if isinstance(vpayload, dict) else "?"
            title = extract_visual_title(vpayload) if isinstance(vpayload, dict) else None
            title_str = title if title else "(nessun titolo)"
            print(f"{i:<4} {vid:<24} {vtype:<28} {title_str}")

        print(f"\nTotale visual: {len(visuals)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    ws_id = sys.argv[1]
    rp_id = sys.argv[2]
    pg_id = sys.argv[3] if len(sys.argv) > 3 else None

    analyze_report(ws_id, rp_id, pg_id)
