"""
Phi + Power BI DAX Agent
-------------------------
Integra Phi-3.5 Mini (Ollama) come LLM host con il modello semantico
Power BI come fonte dati, interrogato tramite Power BI REST executeQueries.

Flusso:
  1. Recupera la definizione TMDL del modello semantico via Fabric REST API
     (getDefinition) - include tabelle, colonne, misure, relazioni e
     tutte le descrizioni "Prep for AI"
  2. Costruisce un system prompt ricco con lo schema reale
  3. Phi riceve la domanda e genera la query DAX corretta
  4. pbi_executor.py esegue la query DAX sul modello semantico
  5. Il risultato torna a Phi che lo formula in risposta leggibile

Token necessari (due scope diversi):
  - access_token  -> scope analysis.windows.net  (Power BI executeQueries)
  - fabric_token  -> scope api.fabric.microsoft.com (Fabric getDefinition)

Configurazione (.env):
  PBI_WORKSPACE_ID  - ID workspace Fabric
  PBI_DATASET_ID    - ID modello semantico
  OLLAMA_URL        - URL Ollama (default: http://localhost:11434)
  OLLAMA_MODEL      - modello (default: phi3.5)
"""

import base64
import json
import os
import time
import requests
from dotenv import load_dotenv
from pbi_executor import PBIExecutor, PBI_WORKSPACE_ID, PBI_DATASET_ID

load_dotenv()

OLLAMA_BASE_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434')
OLLAMA_CHAT_URL = f'{OLLAMA_BASE_URL}/api/chat'
OLLAMA_MODEL    = os.getenv('OLLAMA_MODEL', 'phi3.5')
OLLAMA_TIMEOUT  = int(os.getenv('OLLAMA_TIMEOUT', '120'))

FABRIC_API_BASE = 'https://api.fabric.microsoft.com/v1'
LRO_MAX_WAIT_S  = int(os.getenv('PBI_SCHEMA_TIMEOUT', '120'))

# Mappa codici numerici DataType (INFO.COLUMNS) -> stringa leggibile
_DATATYPE_MAP = {
    2: 'string', 6: 'int64', 8: 'double', 9: 'dateTime',
    10: 'decimal', 11: 'boolean', 17: 'int64',
}

_SYSTEM_PROMPT_BASE = """
You are a data analyst assistant with access to a Power BI semantic model.

To answer any question about data, output ONLY this JSON block (nothing else before it):

```json
{"action": "execute_dax", "dax_query": "EVALUATE ..."}
```

DAX rules — follow exactly:
1. EVALUATE must return a TABLE, never a scalar.
   - WRONG: EVALUATE COUNTROWS('tokens')
   - RIGHT:  EVALUATE {COUNTROWS('tokens')}   -- wraps scalar in a 1-row table
2. To group and aggregate, use SUMMARIZE:
   EVALUATE SUMMARIZE('tokens', 'tokens'[gps_data.country.nome], "N", [#Questions])
3. To use a measure in SUMMARIZE, add it as a named column at the end:
   EVALUATE SUMMARIZE('table', 'table'[col], "Label", [Measure Name])
4. Column references: 'TableName'[ColumnName]  — always include the table name.
5. Use ONLY exact names from the schema. Do NOT invent names.
6. After receiving results, summarize them clearly in the user's language.

"""


_SYSTEM_PROMPT_REMINDER = """

---
IMPORTANT REMINDER: To query the data, output this JSON block (use ```json fences):
```json
{"action": "execute_dax", "dax_query": "EVALUATE SUMMARIZE(...)"}
```
Use ONLY column and measure names from the schema above. EVALUATE must return a table.
"""


# -- TMDL Parser --------------------------------------------------------------

def _decode_part(payload: str) -> str:
    try:
        return base64.b64decode(payload).decode('utf-8', errors='replace')
    except Exception:
        return ''


def _parse_table_tmdl(tmdl: str) -> dict:
    """
    Analizza il testo TMDL di una singola tabella.
    Restituisce dict con name, description, columns, measures.
    """
    lines = tmdl.splitlines()
    table_name   = ''
    table_desc   = ''
    table_hidden = False
    columns      = []
    measures     = []
    current_col  = None
    current_meas = None
    in_backtick  = False

    for line in lines:
        stripped = line.strip()

        # Salta blocchi espressione multiriga (```)
        if '```' in stripped:
            in_backtick = not in_backtick
            continue
        if in_backtick:
            continue

        # Nome tabella
        if not table_name and stripped.startswith('table '):
            table_name = stripped[6:].strip().strip("'\"")
            continue

        # Nascosta a livello tabella (prima di qualsiasi colonna/misura)
        if table_name and not columns and not measures and not current_col and not current_meas:
            if stripped == 'isHidden':
                table_hidden = True
                continue

        # Descrizione tabella
        if table_name and not columns and not measures and stripped.startswith('description:'):
            table_desc = stripped[12:].strip().strip('"')
            continue

        # Nuova colonna
        if stripped.startswith('column '):
            if current_col:
                columns.append(current_col)
            if current_meas:
                measures.append(current_meas)
                current_meas = None
            col_name = stripped[7:].strip().strip("'\"")
            current_col = {'name': col_name, 'dataType': '', 'description': '', 'hidden': False}
            continue

        # Nuova misura
        if stripped.startswith('measure '):
            if current_col:
                columns.append(current_col)
                current_col = None
            if current_meas:
                measures.append(current_meas)
            meas_name = stripped[8:].split('=')[0].strip().strip("'\"")
            current_meas = {'name': meas_name, 'description': '', 'hidden': False}
            continue

        # Attributi colonna
        if current_col:
            if stripped.startswith('dataType:'):
                current_col['dataType'] = stripped[9:].strip()
            elif stripped.startswith('description:'):
                current_col['description'] = stripped[12:].strip().strip('"')
            elif stripped == 'isHidden':
                current_col['hidden'] = True
            continue

        # Attributi misura
        if current_meas:
            if stripped.startswith('description:'):
                current_meas['description'] = stripped[12:].strip().strip('"')
            elif stripped == 'isHidden':
                current_meas['hidden'] = True

    if current_col:
        columns.append(current_col)
    if current_meas:
        measures.append(current_meas)

    return {'name': table_name, 'description': table_desc, 'hidden': table_hidden,
            'columns': columns, 'measures': measures}


def _parse_model_tmdl(tmdl: str) -> list:
    """
    Analizza model.tmdl ed estrae le relazioni.
    Formato TMDL: fromColumn: TableName.ColumnName
    """
    relationships = []
    current = None

    for line in tmdl.splitlines():
        stripped = line.strip()

        if stripped.startswith('relationship '):
            if current:
                relationships.append(current)
            current = {'fromTable': '', 'fromColumn': '', 'toTable': '', 'toColumn': '',
                       'active': True, 'crossFilter': 'single'}
            continue

        if current is None:
            continue

        if stripped.startswith('fromColumn:'):
            val = stripped[11:].strip()
            if '.' in val:
                t, c = val.split('.', 1)
                current['fromTable'], current['fromColumn'] = t.strip(), c.strip()
        elif stripped.startswith('toColumn:'):
            val = stripped[9:].strip()
            if '.' in val:
                t, c = val.split('.', 1)
                current['toTable'], current['toColumn'] = t.strip(), c.strip()
        elif stripped == 'isActive: false':
            current['active'] = False
        elif stripped.startswith('crossFilteringBehavior:'):
            current['crossFilter'] = 'both' if 'both' in stripped.lower() else 'single'

    if current:
        relationships.append(current)

    return relationships


def _build_schema_text(tables: list, relationships: list) -> str:
    lines = ['## Semantic Model Schema\n', '### Tables, Columns and Measures\n']

    for table in tables:
        if not table['name'] or table.get('hidden'):
            continue
        header = f"**{table['name']}**"
        if table['description']:
            header += f" - {table['description']}"
        lines.append(header)

        for col in table['columns']:
            if col['hidden']:
                continue
            entry = f"  - '{table['name']}'[{col['name']}]"
            if col['dataType']:
                entry += f" ({col['dataType']})"
            if col['description']:
                entry += f" - {col['description']}"
            lines.append(entry)

        for meas in table['measures']:
            if meas['hidden']:
                continue
            entry = f"  - [{meas['name']}]  (measure)"
            if meas['description']:
                entry += f" - {meas['description']}"
            lines.append(entry)

        lines.append('')

    if relationships:
        lines.append('### Relationships\n')
        for rel in relationships:
            if not rel['fromTable'] or not rel['toTable']:
                continue
            status = '' if rel['active'] else ' [inactive]'
            cf = ' (bidirectional)' if rel['crossFilter'] == 'both' else ''
            lines.append(
                f"  - '{rel['fromTable']}'[{rel['fromColumn']}]"
                f" -> '{rel['toTable']}'[{rel['toColumn']}]{cf}{status}"
            )

    return '\n'.join(lines)


# -- Agent --------------------------------------------------------------------

class PhiPBIAgent:
    """
    Agente Phi + Power BI semantic model via Fabric getDefinition (TMDL).

    Args:
        access_token:  scope analysis.windows.net  (executeQueries)
        fabric_token:  scope api.fabric.microsoft.com (getDefinition TMDL)
        workspace_id, dataset_id: override da .env
    """

    def __init__(
        self,
        access_token: str,
        fabric_token: str = None,
        workspace_id: str = PBI_WORKSPACE_ID,
        dataset_id: str = PBI_DATASET_ID,
    ):
        self._executor     = PBIExecutor(access_token, workspace_id, dataset_id)
        self._fabric_token = fabric_token
        self._workspace_id = workspace_id
        self._dataset_id   = dataset_id
        self._schema_cache = None

    # -- Schema retrieval -----------------------------------------------------

    def _call_get_definition(self) -> dict:
        if not self._fabric_token:
            raise RuntimeError(
                'fabric_token mancante. Serve scope api.fabric.microsoft.com/.default '
                'per recuperare lo schema del modello.'
            )

        url = (
            f'{FABRIC_API_BASE}/workspaces/{self._workspace_id}'
            f'/semanticModels/{self._dataset_id}/getDefinition?format=TMDL'
        )
        headers = {'Authorization': f'Bearer {self._fabric_token}'}
        r = requests.post(url, headers=headers, timeout=30)

        if r.status_code == 200:
            return r.json()

        if r.status_code == 202:
            operation_url = r.headers.get('Location')
            retry_after   = int(r.headers.get('Retry-After', '5'))
            if not operation_url:
                raise RuntimeError('getDefinition 202 senza Location header.')
            elapsed = 0
            while elapsed < LRO_MAX_WAIT_S:
                time.sleep(retry_after)
                elapsed += retry_after
                poll = requests.get(operation_url, headers=headers, timeout=30)
                if poll.status_code not in (200, 202):
                    raise RuntimeError(f'getDefinition LRO fallito: {poll.status_code} {poll.text[:200]}')
                body   = poll.json()
                status = body.get('status', '')
                if status == 'Succeeded':
                    result_url = poll.headers.get('Location')
                    if not result_url:
                        raise RuntimeError('getDefinition LRO Succeeded ma senza Location header per il risultato.')
                    res = requests.get(result_url, headers=headers, timeout=30)
                    res.raise_for_status()
                    return res.json()
                if status == 'Failed':
                    raise RuntimeError(f"getDefinition LRO fallito: {body.get('error')}")
                # ancora in esecuzione — continua il polling
            raise TimeoutError(f'getDefinition LRO timeout dopo {LRO_MAX_WAIT_S}s')

        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f'getDefinition HTTP {r.status_code}: {detail}')

    def _fetch_schema(self) -> str:
        definition = self._call_get_definition()
        parts      = definition.get('definition', {}).get('parts', [])

        tables        = []
        model_tmdl    = ''
        rel_tmdl      = ''   # relazioni in file separato (Fabric PBIP)

        for part in parts:
            path    = part.get('path', '')
            payload = part.get('payload', '')
            tmdl    = _decode_part(payload)

            if path.startswith('definition/tables/') and path.endswith('.tmdl'):
                table = _parse_table_tmdl(tmdl)
                if table['name']:
                    tables.append(table)
            elif path == 'definition/model.tmdl':
                model_tmdl = tmdl
            elif path == 'definition/relationships.tmdl':
                rel_tmdl = tmdl

        # Le relazioni possono stare in relationships.tmdl o in model.tmdl
        source_tmdl   = rel_tmdl if rel_tmdl else model_tmdl
        relationships = _parse_model_tmdl(source_tmdl) if source_tmdl else []
        return _build_schema_text(tables, relationships)

    def get_schema(self, force_refresh: bool = False) -> str:
        if self._schema_cache is None or force_refresh:
            self._schema_cache = self._fetch_schema()
        return self._schema_cache

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT_BASE + self.get_schema() + _SYSTEM_PROMPT_REMINDER

    # -- Ollama ---------------------------------------------------------------

    def _call_phi(self, messages: list) -> str:
        """Calls Ollama and returns the assistant content string."""
        try:
            r = requests.post(
                OLLAMA_CHAT_URL,
                json={
                    'model': OLLAMA_MODEL,
                    'messages': messages,
                    'stream': False,
                    'options': {'temperature': 0.1},
                },
                timeout=OLLAMA_TIMEOUT,
            )
            r.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise RuntimeError(f'Ollama non raggiungibile su {OLLAMA_BASE_URL}.')
        return r.json().get('message', {}).get('content', '').strip()

    # -- Prompt-based tool call extraction ------------------------------------

    def _extract_dax(self, text: str) -> str | None:
        """
        Cerca una query DAX nella risposta di Phi.
        Prova in ordine:
          1. Blocco ```json {"action":"execute_dax", "dax_query": ...}
          2. Inline JSON con dax_query
          3. Blocco ```dax / ```sql contenente EVALUATE
          4. Linea che inizia con EVALUATE (fallback)
        """
        import re

        # 1. Blocco ```json con action=execute_dax
        for match in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
            try:
                obj = json.loads(match.group(1))
                if obj.get('dax_query'):
                    return obj['dax_query'].strip()
            except json.JSONDecodeError:
                pass

        # 2. JSON inline con dax_query
        for match in re.finditer(r'\{[^{}]*"dax_query"[^{}]*\}', text, re.DOTALL):
            try:
                obj = json.loads(match.group(0))
                if obj.get('dax_query'):
                    return obj['dax_query'].strip()
            except json.JSONDecodeError:
                pass

        # 3. Blocco ```dax / ```sql / ``` contenente EVALUATE
        for match in re.finditer(r'```(?:dax|sql|DAX)?\s*(EVALUATE\b.*?)\s*```', text, re.DOTALL | re.IGNORECASE):
            query = match.group(1).strip()
            if query:
                return query

        # 4. Ultimo fallback: prima occorrenza di una riga EVALUATE
        for match in re.finditer(r'(EVALUATE\b[^`"]+)', text, re.IGNORECASE):
            query = match.group(1).strip()
            # prendi tutto fino a riga vuota o fine stringa
            query = re.split(r'\n\s*\n', query)[0].strip()
            if query:
                return query

        return None

    def _run_dax(self, dax_query: str) -> str:
        result = self._executor.execute(dax_query)
        if not result['success']:
            return f"Errore esecuzione DAX: {result['error']}"
        return self._executor.to_text(result)

    def _fix_dax(self, dax: str) -> str:
        """
        Minimal auto-fix: se la query non inizia con EVALUATE, prova a
        wrappare l'espressione scalare in una tabella con {}.
        Patterns comuni prodotti da modelli piccoli:
          - VAR x = expr RETURN x  -> EVALUATE { expr }  (se scalare)
          - espressione senza EVALUATE -> EVALUATE { expr }
        """
        import re
        stripped = dax.strip()
        if re.match(r'evaluate\b', stripped, re.IGNORECASE):
            return stripped
        # VAR ... RETURN expr -> prova EVALUATE ADDCOLUMNS({""}, "Result", <returnexpr>)
        var_return = re.search(r'\bRETURN\s+(.+)$', stripped, re.IGNORECASE | re.DOTALL)
        if var_return:
            return_expr = var_return.group(1).strip()
            return f'EVALUATE {{\n{return_expr}\n}}'
        # Generic fallback
        return f'EVALUATE {{\n{stripped}\n}}'

    # -- Main -----------------------------------------------------------------

    def ask(self, question: str) -> dict:
        """
        Risponde a una domanda in linguaggio naturale interrogando il modello semantico.
        Implementa un ciclo di auto-correzione (max 2 tentativi DAX).

        Returns dict con: success, response, dax_query, dax_result, schema
        """
        system_prompt = self._build_system_prompt()
        schema        = self.get_schema()

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': question},
        ]

        dax_query  = None
        dax_result = None

        # Ciclo DAX: max 2 tentativi (prima chiamata + 1 auto-correzione)
        for attempt in range(2):
            phi_text  = self._call_phi(messages)
            dax_query = self._extract_dax(phi_text)

            if not dax_query:
                # Phi ha risposto direttamente senza query
                return {
                    'success': True,
                    'response': phi_text,
                    'dax_query': None,
                    'dax_result': None,
                    'schema': schema,
                }

            # Auto-fix se manca EVALUATE
            dax_query = self._fix_dax(dax_query)

            dax_result = self._run_dax(dax_query)
            is_error   = dax_result.startswith('Errore esecuzione DAX:')

            if not is_error:
                break  # esecuzione riuscita

            if attempt < 1:
                # Chiedi a Phi di correggere la query
                messages.append({'role': 'assistant', 'content': phi_text})
                messages.append({
                    'role': 'user',
                    'content': (
                        f'The DAX query failed with this error:\n\n{dax_result}\n\n'
                        'Please write a corrected DAX query. '
                        'Remember: EVALUATE must return a TABLE. '
                        'For grouping use EVALUATE SUMMARIZE(\'table\', \'table\'[col], "Label", [Measure]).\n'
                        'Output the corrected query in the ```json format.'
                    ),
                })

        # Riassunto finale da Phi
        messages.append({'role': 'assistant', 'content': phi_text})
        if not dax_result.startswith('Errore esecuzione DAX:'):
            messages.append({
                'role': 'user',
                'content': (
                    f'Query executed successfully. Results:\n\n{dax_result}\n\n'
                    'Please summarize these results clearly in the user\'s language.'
                ),
            })
        else:
            messages.append({
                'role': 'user',
                'content': (
                    f'The DAX query could not be executed.\n\n{dax_result}\n\n'
                    'Explain the issue briefly in the user\'s language and suggest what information '
                    'is available in the schema that could answer the original question.'
                ),
            })
        final_text = self._call_phi(messages)

        return {
            'success': True,
            'response': final_text or dax_result,
            'dax_query': dax_query,
            'dax_result': dax_result,
            'schema': schema,
        }
