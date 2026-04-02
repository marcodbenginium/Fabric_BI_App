"""
Fabric Data Agent - MCP Client
-------------------------------
Chiama il Fabric Data Agent tramite il protocollo MCP (Model Context Protocol)
usando il trasporto HTTP Streamable (JSON-RPC over HTTP POST).

Rispetto a fabric_agent.py (REST/Assistants API), questo approccio:
  - Non richiede gestione di assistant_id / thread_id
  - Non usa polling: la risposta è sincrona o in streaming SSE
  - Segue lo standard aperto MCP (Anthropic / Microsoft)

Endpoint MCP (hardcoded come fallback, override tramite .env):
  FABRIC_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/.../agent

Spec MCP:
  https://modelcontextprotocol.io/docs/concepts/transports
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

# URL dell'endpoint MCP del Data Agent — può essere sovrascritto nel .env
FABRIC_MCP_URL: str = os.getenv(
    'FABRIC_MCP_URL',
    'https://api.fabric.microsoft.com/v1/mcp/workspaces/'
    '4420d297-caea-4545-ba0f-802eb5ed9266/dataagents/'
    '0181a0f2-47fe-48c3-81e1-2c425ea5f25e/agent',
)

MCP_PROTOCOL_VERSION = '2024-11-05'
TIMEOUT_SECONDS      = int(os.getenv('FABRIC_AGENT_TIMEOUT', '120'))


class FabricMCPClient:
    """
    Client MCP (Model Context Protocol) per Fabric Data Agent.

    Implementa il trasporto HTTP Streamable: tutte le operazioni sono
    POST allo stesso endpoint MCP, codificate come JSON-RPC 2.0.
    La risposta può essere JSON puro o SSE (Server-Sent Events).
    """

    def __init__(self, access_token: str, url: str):
        self._url  = url
        self._id   = 0
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            # Accettiamo sia JSON che SSE — Fabric sceglie in base al contesto
            'Accept': 'application/json, text/event-stream',
        })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, timeout: int = TIMEOUT_SECONDS) -> dict:
        """Invia un messaggio JSON-RPC e restituisce la risposta deserializzata."""
        r = self._session.post(self._url, json=payload, timeout=timeout)
        r.raise_for_status()
        content_type = r.headers.get('Content-Type', '')
        if 'text/event-stream' in content_type:
            return self._parse_sse(r.text)
        return r.json()

    @staticmethod
    def _parse_sse(sse_text: str) -> dict:
        """
        Estrae il primo oggetto JSON significativo da un flusso SSE.
        Formato atteso:  data: {"jsonrpc":"2.0", ...}
        """
        for line in sse_text.splitlines():
            if line.startswith('data:'):
                data = line[5:].strip()
                if data and data != '[DONE]':
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError:
                        continue
        return {}

    # ── Protocollo MCP ────────────────────────────────────────────────────────

    def initialize(self) -> dict:
        """
        Fase 1 del handshake MCP: initialize + notifications/initialized.
        Restituisce il risultato del server (protocolVersion, capabilities…).
        """
        payload = {
            'jsonrpc': '2.0',
            'method': 'initialize',
            'params': {
                'protocolVersion': MCP_PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {
                    'name': 'fabric-bi-app-mcp',
                    'version': '1.0.0',
                },
            },
            'id': self._next_id(),
        }
        result = self._post(payload)

        # Notifica "initialized" — fire-and-forget, gli errori non sono bloccanti
        try:
            self._session.post(
                self._url,
                json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                timeout=10,
            )
        except Exception:
            pass

        return result

    def list_tools(self) -> list[dict]:
        """
        Restituisce la lista degli strumenti esposti dall'agente MCP.
        Ogni tool ha: name, description, inputSchema.
        """
        payload = {
            'jsonrpc': '2.0',
            'method': 'tools/list',
            'id': self._next_id(),
        }
        result = self._post(payload)
        return result.get('result', {}).get('tools', [])

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        Chiama uno strumento MCP e restituisce il testo della risposta.
        Segue la struttura standard MCP: result.content[].type == 'text'.
        """
        payload = {
            'jsonrpc': '2.0',
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments,
            },
            'id': self._next_id(),
        }
        result = self._post(payload)

        # Gestione errore JSON-RPC
        if 'error' in result:
            raise RuntimeError(f"MCP tool error: {result['error']}")

        # Estrai il testo dalla struttura standard MCP
        content = result.get('result', {}).get('content', [])
        for block in content:
            if block.get('type') == 'text':
                return block.get('text', '')

        # Fallback: restituisci la risposta grezza
        return json.dumps(result, ensure_ascii=False)


# ── API pubblica ──────────────────────────────────────────────────────────────

def ask_fabric_agent_mcp(access_token: str, question: str) -> dict:
    """
    Invia una domanda al Fabric Data Agent tramite MCP.

    Stessa firma di ask_fabric_agent() in fabric_agent.py, per poter essere
    usata come drop-in replacement in app.py.

    Args:
        access_token: Token Azure AD dell'utente (preserva RLS su Fabric).
        question:     Domanda in linguaggio naturale.

    Returns:
        {'success': True,  'response': '...'} oppure
        {'success': False, 'error':    '...'}
    """
    client = FabricMCPClient(access_token, FABRIC_MCP_URL)

    try:
        # 1. Handshake MCP
        init_result = client.initialize()
        if 'error' in init_result:
            return {
                'success': False,
                'error': f"Handshake MCP fallito: {init_result['error']}",
            }

        # 2. Scopri gli strumenti disponibili
        tools = client.list_tools()
        if not tools:
            return {
                'success': False,
                'error': 'Nessuno strumento MCP disponibile. '
                         'Verifica che l\'agente sia pubblicato e l\'URL sia corretto.',
            }

        # 3. Scegli lo strumento più adatto per le query in linguaggio naturale
        tool_name = tools[0]['name']  # default: primo strumento
        priority_keywords = ('query', 'ask', 'chat', 'question', 'search')
        for t in tools:
            if any(kw in t['name'].lower() for kw in priority_keywords):
                tool_name = t['name']
                break

        # 4. Determina il nome del parametro di input dallo schema del tool
        selected_tool = next((t for t in tools if t['name'] == tool_name), tools[0])
        input_schema  = selected_tool.get('inputSchema', {})
        properties    = input_schema.get('properties', {})

        # Cerca il campo testuale principale (query, question, input, text, message…)
        text_param = 'query'
        for candidate in ('query', 'question', 'input', 'text', 'message', 'prompt'):
            if candidate in properties:
                text_param = candidate
                break

        arguments = {text_param: question}

        # 5. Esegui la chiamata
        response_text = client.call_tool(tool_name, arguments)

        return {
            'success': True,
            'response': response_text or '(Nessuna risposta dall\'agente)',
        }

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '?'
        body   = e.response.text[:400] if e.response is not None else ''
        return {'success': False, 'error': f'Fabric MCP HTTP {status}: {body}'}
    except requests.exceptions.ConnectionError:
        return {
            'success': False,
            'error': 'Impossibile connettersi all\'endpoint MCP. Verifica FABRIC_MCP_URL.',
        }
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Timeout nella connessione al Fabric Data Agent MCP.'}
    except RuntimeError as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': f'Errore inatteso: {e}'}
