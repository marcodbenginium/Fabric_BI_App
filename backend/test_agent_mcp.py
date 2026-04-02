"""
Test standalone del Fabric Data Agent - MCP
--------------------------------------------
Testa fabric_agent_mcp.py usando il protocollo MCP invece della REST API.

Autenticazione (stessa logica di test_agent.py):
  1. Azure CLI  [CONSIGLIATA]  →  az login
  2. Device Code Flow          →  richiede AZURE_CLIENT_ID e AZURE_TENANT_ID nel .env

Uso:
    python test_agent_mcp.py
    python test_agent_mcp.py "Quali dati hai a disposizione?"

Tip: per vedere i tool disponibili prima di fare domande, usa --list-tools
    python test_agent_mcp.py --list-tools
"""

import sys
import os
import json
import re
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

FABRIC_SCOPE = 'https://api.fabric.microsoft.com/.default'
CACHE_FILE   = '.token_cache_mcp.json'


# ── Auth (uguale a test_agent.py) ─────────────────────────────────────────────

def get_token_from_azure_cli() -> str:
    try:
        from azure.identity import AzureCliCredential
        return AzureCliCredential().get_token(FABRIC_SCOPE).token
    except ImportError:
        raise RuntimeError("Installa azure-identity: pip install azure-identity")
    except Exception as e:
        raise RuntimeError(f"Azure CLI non disponibile: {e}\n→ Esegui prima: az login")


def get_token_device_code() -> str:
    import msal

    client_id = os.getenv('AZURE_CLIENT_ID')
    tenant_id = os.getenv('AZURE_TENANT_ID')
    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID e AZURE_TENANT_ID richiesti nel .env per il Device Code Flow")

    scopes = ['https://api.fabric.microsoft.com/Item.Read.All']
    cache  = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        cache.deserialize(open(CACHE_FILE).read())

    app = msal.PublicClientApplication(
        client_id,
        authority=f'https://login.microsoftonline.com/{tenant_id}',
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if result and 'access_token' in result:
            print(f"✅ Token dalla cache ({accounts[0]['username']})")
            _save_cache(cache)
            return result['access_token']

    flow = app.initiate_device_flow(scopes=scopes)
    if 'user_code' not in flow:
        raise RuntimeError(f"Errore Device Flow: {flow}")

    print("\n" + "=" * 60)
    print(flow['message'])
    print("=" * 60 + "\n")

    result = app.acquire_token_by_device_flow(flow)
    if 'error' in result:
        raise RuntimeError(f"Autenticazione fallita: {result.get('error_description', result['error'])}")

    _save_cache(cache)
    print(f"✅ Autenticato come: {result.get('id_token_claims', {}).get('preferred_username', '?')}\n")
    return result['access_token']


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        with open(CACHE_FILE, 'w') as f:
            f.write(cache.serialize())


def get_token() -> str:
    try:
        token = get_token_from_azure_cli()
        print("✅ Autenticato tramite Azure CLI\n")
        return token
    except RuntimeError as cli_err:
        print(f"⚠️  Azure CLI non disponibile ({cli_err})")
        print("→ Uso Device Code Flow...\n")
        return get_token_device_code()


# ── Comandi ───────────────────────────────────────────────────────────────────

def cmd_list_tools(token: str) -> None:
    """Mostra gli strumenti disponibili sull'endpoint MCP."""
    from fabric_agent_mcp import FabricMCPClient, FABRIC_MCP_URL

    client = FabricMCPClient(token, FABRIC_MCP_URL)
    print(f"🔗 Endpoint MCP: {FABRIC_MCP_URL}\n")

    print("⏳ Handshake MCP in corso...")
    init = client.initialize()
    server_info = init.get('result', {})
    print(f"✅ Server MCP: {server_info.get('serverInfo', {})}")
    print(f"   Versione protocollo: {server_info.get('protocolVersion', '?')}\n")

    print("⏳ Recupero strumenti disponibili...")
    tools = client.list_tools()

    if not tools:
        print("❌ Nessuno strumento trovato.")
        return

    print(f"✅ {len(tools)} strumento/i disponibile/i:\n")
    for i, t in enumerate(tools, 1):
        print(f"  [{i}] {t['name']}")
        if t.get('description'):
            print(f"      {t['description']}")
        schema = t.get('inputSchema', {})
        props  = schema.get('properties', {})
        if props:
            print(f"      Parametri: {list(props.keys())}")
        print()


def cmd_ask(token: str, question: str) -> None:
    """Invia una domanda al Data Agent via MCP e mostra la risposta."""
    from fabric_agent_mcp import ask_fabric_agent_mcp, FABRIC_MCP_URL

    print(f"🔗 Endpoint MCP: {FABRIC_MCP_URL}")
    print(f"❓ Domanda: {question}\n")
    print("⏳ Chiamata MCP in corso...\n")

    result = ask_fabric_agent_mcp(token, question)

    print("─" * 60)
    if result['success']:
        text = result['response'].strip()

        # Stessa logica di parsing di test_agent.py
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if parsed.get('chart'):
                    print("📊 RISPOSTA GRAFICO rilevata!")
                    print(f"   Tipo    : {parsed.get('chartType')}")
                    print(f"   Titolo  : {parsed.get('title')}")
                    print(f"   X key   : {parsed.get('xKey')}")
                    print(f"   Righe   : {len(parsed.get('data', []))}")
                    print(f"\n   JSON completo:\n{json.dumps(parsed, indent=2, ensure_ascii=False)}")
                else:
                    print("✅ Risposta testuale:\n")
                    print(text)
            except json.JSONDecodeError:
                print("✅ Risposta:\n")
                print(text)
        else:
            print("✅ Risposta:\n")
            print(text)
    else:
        print(f"❌ Errore: {result['error']}")

    print("─" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if '--list-tools' in args:
        token = get_token()
        cmd_list_tools(token)
        return

    question = args[0] if args else "Quali dati hai a disposizione?"
    token    = get_token()
    cmd_ask(token, question)


if __name__ == '__main__':
    main()
