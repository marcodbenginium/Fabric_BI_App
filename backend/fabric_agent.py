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

# URL pubblicato dell'agente: Fabric Data Agent → Settings → Published URL
FABRIC_AGENT_URL = os.getenv('FABRIC_AGENT_PUBLISHED_URL')
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


def ask_fabric_agent(access_token: str, question: str) -> dict:
    """
    Invia una domanda al Fabric Data Agent e restituisce la risposta.

    Args:
        access_token: Token Azure AD dell'utente (preserva RLS su Fabric).
        question:     Domanda in linguaggio naturale.

    Returns:
        {'success': True, 'response': '...'} oppure
        {'success': False, 'error': '...'}
    """
    if not FABRIC_AGENT_URL:
        return {
            'success': False,
            'error': 'FABRIC_AGENT_PUBLISHED_URL non configurato nel file .env. '
                     'Vai su Fabric Data Agent → Settings → Published URL.',
        }

    client = _FabricAgentClient(access_token, FABRIC_AGENT_URL)
    thread_id = None

    try:
        # 1. Crea assistant (handle — il modello è gestito internamente da Fabric)
        assistant = client.create_assistant()

        # 2. Crea thread di conversazione
        thread    = client.create_thread()
        thread_id = thread['id']

        # 3. Aggiungi il messaggio dell'utente
        client.add_message(thread_id, question)

        # 4. Avvia il run (elaborazione asincrona dell'agente Fabric)
        run = client.create_run(thread_id, assistant['id'])

        # 5. Polling fino al completamento
        terminal_states = {'completed', 'failed', 'cancelled', 'requires_action', 'expired'}
        start = time.time()

        while run['status'] not in terminal_states:
            if time.time() - start > TIMEOUT_SECONDS:
                return {'success': False, 'error': 'Timeout: l\'agente non ha risposto in tempo'}
            time.sleep(2)
            run = client.get_run(thread_id, run['id'])

        if run['status'] != 'completed':
            return {
                'success': False,
                'error': f"Il run è terminato con stato: {run['status']}",
            }

        # 6. Leggi l'ultimo messaggio dell'assistant
        messages_data = client.list_messages(thread_id)
        messages      = messages_data.get('data', [])

        response_text = ''
        for msg in reversed(messages):
            if msg.get('role') == 'assistant':
                for block in msg.get('content', []):
                    if block.get('type') == 'text':
                        response_text = block['text']['value']
                        break
                if response_text:
                    break

        return {'success': True, 'response': response_text or '(Nessuna risposta dall\'agente)'}

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '?'
        body   = e.response.text[:300] if e.response is not None else ''
        return {'success': False, 'error': f'Fabric API error {status}: {body}'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'Impossibile connettersi al Fabric Data Agent. Verifica FABRIC_AGENT_PUBLISHED_URL.'}
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Timeout nella connessione al Fabric Data Agent.'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        # Cleanup: elimina il thread per liberare risorse Fabric
        if thread_id:
            try:
                client.delete_thread(thread_id)
            except Exception:
                pass
