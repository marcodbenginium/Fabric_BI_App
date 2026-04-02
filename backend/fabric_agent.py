"""
Fabric Data Agent Client
------------------------
Chiama il Fabric Data Agent tramite l'API OpenAI Assistants (formato compatibile).
Il token Azure AD dell'utente viene propagato a Fabric, che applica automaticamente RLS.

Documentazione ufficiale:
https://learn.microsoft.com/en-us/fabric/data-science/data-agent-end-to-end-tutorial#use-the-fabric-data-agent-programmatically
"""

import os
import time
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

# URL agente BI fatturato (Fabric: TEST_COPILOT) — usato solo come fallback diretto
FABRIC_AGENT_URL = os.getenv('FABRIC_AGENT_TEST_COPILOT_URL')
API_VERSION      = '2024-05-01-preview'
TIMEOUT_SECONDS  = int(os.getenv('FABRIC_AGENT_TIMEOUT', '120'))


class _FabricAgentClient:
    """Client REST per il Fabric Data Agent (formato OpenAI Assistants API)."""

    def __init__(self, access_token: str, base_url: str):
        self._base = base_url.rstrip('/')
        self._params = {'api-version': API_VERSION}
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'ActivityId': str(uuid.uuid4()),     # correlazione per diagnostics Fabric
        })

    def _post(self, path: str, body: dict = None) -> dict:
        r = self._session.post(
            f'{self._base}/{path}', json=body or {}, params=self._params, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict:
        r = self._session.get(f'{self._base}/{path}', params=self._params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        self._session.delete(f'{self._base}/{path}', params=self._params, timeout=10)

    # ── OpenAI Assistants API ──────────────────────────────────────────────────

    def create_assistant(self) -> dict:
        # model="not used": ignorato da Fabric, che gestisce il proprio LLM
        return self._post('assistants', {'model': 'not used'})

    def create_thread(self) -> dict:
        return self._post('threads')

    def add_message(self, thread_id: str, content: str) -> dict:
        return self._post(f'threads/{thread_id}/messages', {
            'role': 'user',
            'content': content,
        })

    def create_run(self, thread_id: str, assistant_id: str) -> dict:
        return self._post(f'threads/{thread_id}/runs', {
            'assistant_id': assistant_id,
        })

    def get_run(self, thread_id: str, run_id: str) -> dict:
        return self._get(f'threads/{thread_id}/runs/{run_id}')

    def list_messages(self, thread_id: str) -> dict:
        return self._get(f'threads/{thread_id}/messages')

    def delete_thread(self, thread_id: str) -> None:
        self._delete(f'threads/{thread_id}')


def ask_fabric_agent(
    access_token:  str,
    question:      str,
    agent_url:     str = None,
    thread_id:     str = None,
    assistant_id:  str = None,
) -> dict:
    """
    Invia una domanda al Fabric Data Agent e restituisce la risposta.

    Supporta thread persistenti: se thread_id e assistant_id sono forniti
    vengono riutilizzati (il contesto conversazionale rimane lato Fabric).
    In caso di thread scaduto/non valido, ricrea automaticamente thread e assistant.

    Args:
        access_token:  Token Azure AD dell'utente (preserva RLS su Fabric).
        question:      Domanda in linguaggio naturale.
        agent_url:     URL dell'agente da chiamare. Se None usa FABRIC_AGENT_TEST_COPILOT_URL.
        thread_id:     Thread esistente da riutilizzare (opzionale).
        assistant_id:  Assistant esistente da riutilizzare (opzionale).

    Returns:
        {
          'success': True,
          'response': '...',
          'thread_id': '...',      # da conservare per le prossime chiamate
          'assistant_id': '...',   # da conservare per le prossime chiamate
        }
        oppure {'success': False, 'error': '...'}
    """
    url = agent_url or FABRIC_AGENT_URL
    if not url:
        return {
            'success': False,
            'error': 'URL agente non configurato nel file .env.',
        }

    client = _FabricAgentClient(access_token, url)

    def _do_run(t_id: str, a_id: str) -> dict:
        """Aggiunge messaggio e attende completamento del run."""
        client.add_message(t_id, question)
        run = client.create_run(t_id, a_id)

        terminal_states = {'completed', 'failed', 'cancelled', 'requires_action', 'expired'}
        start = time.time()
        while run['status'] not in terminal_states:
            if time.time() - start > TIMEOUT_SECONDS:
                return {'success': False, 'error': "Timeout: l'agente non ha risposto in tempo"}
            time.sleep(2)
            run = client.get_run(t_id, run['id'])

        if run['status'] != 'completed':
            return {'success': False, 'error': f"Run terminato con stato: {run['status']}"}

        messages_data = client.list_messages(t_id)
        messages = messages_data.get('data', [])
        response_text = ''
        # list_messages restituisce i messaggi dal più recente al più vecchio:
        # iteriamo senza reversed() per prendere l'ultimo messaggio dell'assistant
        for msg in messages:
            if msg.get('role') == 'assistant':
                for block in msg.get('content', []):
                    if block.get('type') == 'text':
                        response_text = block['text']['value']
                        break
                if response_text:
                    break

        return {
            'success':      True,
            'response':     response_text or "(Nessuna risposta dall'agente)",
            'thread_id':    t_id,
            'assistant_id': a_id,
        }

    try:
        # ── Prova a riutilizzare thread e assistant esistenti ────────────────
        if thread_id and assistant_id:
            try:
                return _do_run(thread_id, assistant_id)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status not in (404, 400):
                    raise   # errore diverso, propaga
                # Thread scaduto → ricrea sotto

        # ── Crea nuovi assistant e thread ────────────────────────────────────
        assistant  = client.create_assistant()
        new_thread = client.create_thread()
        return _do_run(new_thread['id'], assistant['id'])

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '?'
        body   = e.response.text[:300] if e.response is not None else ''
        return {'success': False, 'error': f'Fabric API error {status}: {body}'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'Impossibile connettersi al Fabric Data Agent.'}
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Timeout nella connessione al Fabric Data Agent.'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
