"""
Test standalone del Fabric Data Agent
--------------------------------------
Modalità di autenticazione (in ordine di priorità):

  1. Azure CLI  [CONSIGLIATA per test — nessuna App Registration necessaria]
     Esegui prima: az login
     Poi: python test_agent.py

  2. Device Code Flow  [se non hai Azure CLI]
     Richiede AZURE_CLIENT_ID e AZURE_TENANT_ID nel .env
     Apre il browser per il login Microsoft

Uso:
    python test_agent.py
    python test_agent.py "Qual è il fatturato totale del 2024?"
"""

import sys
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

FABRIC_SCOPE = 'https://api.fabric.microsoft.com/.default'
CACHE_FILE   = '.token_cache.json'


def get_token_from_azure_cli() -> str:
    """Usa le credenziali di 'az login' — nessuna App Registration necessaria."""
    try:
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
        token = credential.get_token(FABRIC_SCOPE)
        return token.token
    except ImportError:
        raise RuntimeError("Installa azure-identity: pip install azure-identity")
    except Exception as e:
        raise RuntimeError(f"Azure CLI non disponibile o non loggato: {e}\n→ Esegui prima: az login")


def get_token_device_code() -> str:
    """Device Code Flow tramite App Registration (richiede AZURE_CLIENT_ID e AZURE_TENANT_ID)."""
    import msal

    client_id = os.getenv('AZURE_CLIENT_ID')
    tenant_id = os.getenv('AZURE_TENANT_ID')

    if not client_id or not tenant_id:
        raise RuntimeError("AZURE_CLIENT_ID e AZURE_TENANT_ID sono obbligatori per il Device Code Flow")

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
            print(f"✅ Token dalla cache (utente: {accounts[0]['username']})")
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
    """Prova Azure CLI, poi fallback a Device Code Flow."""
    try:
        token = get_token_from_azure_cli()
        print("✅ Autenticato tramite Azure CLI\n")
        return token
    except RuntimeError as cli_err:
        print(f"⚠️  Azure CLI non disponibile ({cli_err})")
        print("→ Uso Device Code Flow...\n")
        return get_token_device_code()


def main():
    if not os.getenv('FABRIC_AGENT_PUBLISHED_URL'):
        print("❌ FABRIC_AGENT_PUBLISHED_URL mancante nel .env")
        sys.exit(1)

    question = sys.argv[1] if len(sys.argv) > 1 else "Quali dati hai a disposizione?"
    print(f"Domanda: {question}\n")

    token = get_token()

    from fabric_agent import ask_fabric_agent
    print("⏳ Interrogazione Fabric Data Agent in corso...\n")
    result = ask_fabric_agent(token, question)

    print("─" * 60)
    if result['success']:
        import json, re
        text = result['response'].strip()
        # Estrai JSON anche se l'agente aggiunge testo attorno
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if parsed.get('chart'):
                    print(f"📊 RISPOSTA GRAFICO rilevata!")
                    print(f"   Tipo    : {parsed.get('chartType')}")
                    print(f"   Titolo  : {parsed.get('title')}")
                    print(f"   X key   : {parsed.get('xKey')}")
                    print(f"   Righe   : {len(parsed.get('data', []))}")
                    print(f"\n   JSON completo:\n{json.dumps(parsed, indent=2, ensure_ascii=False)}")
                else:
                    print("✅ Risposta testuale (JSON senza 'chart'):\n")
                    print(text)
            except json.JSONDecodeError:
                print("✅ Risposta testuale:\n")
                print(text)
        else:
            print("✅ Risposta testuale:\n")
            print(text)
    else:
        print(f"❌ Errore: {result['error']}")
    print("─" * 60)


if __name__ == '__main__':
    main()
