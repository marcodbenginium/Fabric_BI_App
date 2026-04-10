"""
Test standalone — Phi + Power BI DAX Agent
-------------------------------------------
Testa phi_pbi_agent.py: Phi-3.5 Mini genera query DAX
ed esegue interrogazioni dirette sul modello semantico Power BI.

Lo schema del modello (tabelle, colonne, misure, relazioni, descrizioni
Prep for AI) viene recuperato automaticamente via DAX DMV e iniettato
nel system prompt prima di ogni chiamata a Phi.

NOTA: lo scope di autenticazione è diverso dal Fabric Data Agent MCP:
  analysis.windows.net/powerbi/api/.default  (Power BI REST API)
  vs api.fabric.microsoft.com/.default       (Fabric Data Agent)

Autenticazione:
  az login (Azure CLI) — stesso account con accesso al workspace Fabric

Uso:
    py test_phi_pbi.py
    py test_phi_pbi.py "Qual è il gross margin per paese?"
    py test_phi_pbi.py --verbose "Mostra il top 5 prodotti per fatturato"
    py test_phi_pbi.py --dax-only "EVALUATE TOPN(5, Sales)"
    py test_phi_pbi.py --schema-only
"""

import sys
import os

# Fix Windows cp1252 terminal — consente emoji e caratteri Unicode nei print
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

PBI_SCOPE    = 'https://analysis.windows.net/powerbi/api/.default'
FABRIC_SCOPE = 'https://api.fabric.microsoft.com/.default'


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_tokens() -> tuple:
    """Restituisce (pbi_token, fabric_token) entrambi via Azure CLI."""
    try:
        from azure.identity import AzureCliCredential
        cred         = AzureCliCredential()
        pbi_token    = cred.get_token(PBI_SCOPE).token
        fabric_token = cred.get_token(FABRIC_SCOPE).token
        print("✅ Autenticato tramite Azure CLI")
        print("   scope PBI    : analysis.windows.net (executeQueries)")
        print("   scope Fabric : api.fabric.microsoft.com (getDefinition TMDL)\n")
        return pbi_token, fabric_token
    except Exception as e:
        raise RuntimeError(
            f"Azure CLI non disponibile: {e}\n→ Esegui prima: az login"
        )


# ── Test schema ───────────────────────────────────────────────────────────────

def test_schema(token: str, fabric_token: str) -> None:
    """Mostra lo schema recuperato via Fabric getDefinition (TMDL)."""
    from phi_pbi_agent import PhiPBIAgent
    from pbi_executor import PBI_WORKSPACE_ID, PBI_DATASET_ID

    print(f"🔗 Workspace : {PBI_WORKSPACE_ID}")
    print(f"🔗 Dataset   : {PBI_DATASET_ID}\n")
    print("⏳ Recupero schema via Fabric getDefinition (TMDL)...\n")

    agent  = PhiPBIAgent(access_token=token, fabric_token=fabric_token)
    schema = agent.get_schema()

    print("─" * 60)
    print(schema)
    print("─" * 60)


# ── Test DAX diretto (senza Phi) ──────────────────────────────────────────────

def test_dax_direct(token: str, dax_query: str) -> None:
    """Esegue una query DAX direttamente senza passare per Phi."""
    from pbi_executor import PBIExecutor, PBI_WORKSPACE_ID, PBI_DATASET_ID

    print(f"🔗 Workspace : {PBI_WORKSPACE_ID}")
    print(f"🔗 Dataset   : {PBI_DATASET_ID}")
    print(f"📝 DAX       : {dax_query}\n")
    print("⏳ Esecuzione DAX in corso...\n")

    executor = PBIExecutor(token)
    result   = executor.execute(dax_query)

    print("─" * 60)
    if result['success']:
        print(executor.to_text(result))
    else:
        print(f"❌ Errore: {result['error']}")
    print("─" * 60)


# ── Test con Phi ──────────────────────────────────────────────────────────────

def test_phi_pbi(token: str, fabric_token: str, question: str, verbose: bool = False) -> None:
    """Usa Phi per generare DAX e interrogare il modello semantico."""
    from phi_pbi_agent import PhiPBIAgent, OLLAMA_MODEL, OLLAMA_BASE_URL
    from pbi_executor import PBI_WORKSPACE_ID, PBI_DATASET_ID

    print(f"🤖 LLM       : {OLLAMA_MODEL} via {OLLAMA_BASE_URL}")
    print(f"🔗 Workspace : {PBI_WORKSPACE_ID}")
    print(f"🔗 Dataset   : {PBI_DATASET_ID}")
    print(f"❓ Domanda   : {question}\n")
    print("⏳ Recupero schema + esecuzione (getDefinition → Phi → DAX → Power BI)...\n")

    agent  = PhiPBIAgent(access_token=token, fabric_token=fabric_token)
    result = agent.ask(question)

    print("─" * 60)
    if not result['success']:
        print(f"❌ Errore: {result.get('error', 'Errore sconosciuto')}")
        return

    if verbose:
        print("📋 Schema iniettato nel prompt:")
        print(result.get('schema', '(non disponibile)'))
        print()

    if result['dax_query']:
        print(f"📝 DAX generato da Phi:\n   {result['dax_query']}\n")
        if verbose and result['dax_result']:
            print("📥 Risultato grezzo DAX:")
            print(result['dax_result'])
            print()
        print("💬 Risposta elaborata da Phi:\n")
    else:
        print("💬 Risposta diretta da Phi (nessun DAX generato):\n")

    print(result['response'])
    print("─" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args        = [a for a in sys.argv[1:] if not a.startswith('--')]
    verbose     = '--verbose' in sys.argv
    dax_only    = '--dax-only' in sys.argv
    schema_only = '--schema-only' in sys.argv

    question = args[0] if args else "Qual è il gross margin totale per paese?"

    print("=" * 60)
    print("  Phi + Power BI DAX Agent — Test")
    print("=" * 60)

    try:
        pbi_token, fabric_token = get_tokens()
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)

    if schema_only:
        test_schema(pbi_token, fabric_token)
    elif dax_only:
        test_dax_direct(pbi_token, question)
    else:
        try:
            test_phi_pbi(pbi_token, fabric_token, question, verbose=verbose)
        except RuntimeError as e:
            print(f"❌ Errore: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
