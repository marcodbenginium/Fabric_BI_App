"""
Power BI REST API — DAX Executor
----------------------------------
Esegue query DAX direttamente su un modello semantico Fabric/Power BI
tramite l'endpoint executeQueries della Power BI REST API.

Documentazione:
  https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/execute-queries

Endpoint:
  POST https://api.powerbi.com/v1.0/myorg/groups/{workspaceId}/datasets/{datasetId}/executeQueries

Autenticazione:
  Token delegato utente — scope: https://analysis.windows.net/powerbi/api/.default
  (diverso dal Fabric Data Agent che usa api.fabric.microsoft.com)

Configurazione (.env):
  PBI_WORKSPACE_ID  — ID del workspace Fabric
  PBI_DATASET_ID    — ID del modello semantico (dataset)
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

PBI_WORKSPACE_ID = os.getenv('PBI_WORKSPACE_ID', '4420d297-caea-4545-ba0f-802eb5ed9266')
PBI_DATASET_ID   = os.getenv('PBI_DATASET_ID',   '89ec5c05-e6b9-4ed5-a22d-8f63e8f78489')
PBI_SCOPE        = 'https://analysis.windows.net/powerbi/api/.default'
TIMEOUT_SECONDS  = int(os.getenv('PBI_TIMEOUT', '60'))

_BASE_URL = (
    f'https://api.powerbi.com/v1.0/myorg/groups/{PBI_WORKSPACE_ID}'
    f'/datasets/{PBI_DATASET_ID}/executeQueries'
)


class PBIExecutor:
    """
    Client per l'esecuzione di query DAX su un modello semantico Power BI.

    Args:
        access_token: Token delegato Azure AD con scope PBI_SCOPE.
        workspace_id: Override workspace ID (default da .env).
        dataset_id:   Override dataset ID (default da .env).
    """

    def __init__(
        self,
        access_token: str,
        workspace_id: str = PBI_WORKSPACE_ID,
        dataset_id: str = PBI_DATASET_ID,
    ):
        self._url = (
            f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
            f'/datasets/{dataset_id}/executeQueries'
        )
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        })

    def execute(self, dax_query: str) -> dict:
        """
        Esegue una query DAX e restituisce i risultati.

        Args:
            dax_query: Query DAX valida (es. "EVALUATE TOPN(10, Sales)")

        Returns:
            {
              'success': True,
              'columns': [{'name': ..., 'dataType': ...}, ...],
              'rows':    [{'col1': val1, ...}, ...],
              'raw':     <risposta API completa>
            }
            oppure
            {
              'success': False,
              'error': str
            }
        """
        payload = {
            'queries': [{'query': dax_query}],
            'serializerSettings': {'includeNulls': True},
        }

        try:
            r = self._session.post(self._url, json=payload, timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            return {'success': False, 'error': f'HTTP {r.status_code}: {detail}'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'error': f'Errore di connessione: {e}'}
        except requests.exceptions.Timeout:
            return {'success': False, 'error': f'Timeout dopo {TIMEOUT_SECONDS}s'}

        data = r.json()

        # Estrai risultati dalla struttura API
        try:
            result_table = data['results'][0]['tables'][0]
            columns = result_table.get('columns', [])
            rows_raw = result_table.get('rows', [])

            # Normalizza i nomi colonna (rimuove prefisso "TabellaNome[ColonnaNome]")
            col_names = [c['name'] for c in columns]
            rows = []
            for row in rows_raw:
                # Le chiavi delle row hanno il formato "TabellaNome[ColonnaNome]"
                # oppure corrispondono direttamente ai column names
                normalized = {}
                for i, col in enumerate(col_names):
                    # prova chiave diretta, poi cerca per posizione
                    val = row.get(col)
                    if val is None and i < len(row):
                        val = list(row.values())[i]
                    normalized[col] = val
                rows.append(normalized)

            return {
                'success': True,
                'columns': columns,
                'rows': rows,
                'raw': data,
            }
        except (KeyError, IndexError) as e:
            return {
                'success': False,
                'error': f'Struttura risposta inattesa: {e}. Raw: {json.dumps(data)[:500]}',
            }

    def to_text(self, result: dict, max_rows: int = 50) -> str:
        """
        Converte il risultato della query in testo leggibile da un LLM.

        Args:
            result:   Output di execute()
            max_rows: Limite righe da includere nel testo

        Returns:
            Stringa formattata con intestazione colonne e righe.
        """
        if not result.get('success'):
            return f"Errore: {result.get('error', 'Errore sconosciuto')}"

        columns = [c['name'] for c in result['columns']]
        rows    = result['rows'][:max_rows]
        total   = len(result['rows'])

        if not rows:
            return 'La query non ha restituito risultati.'

        lines = [' | '.join(columns)]
        lines.append('-' * min(len(lines[0]), 120))
        for row in rows:
            lines.append(' | '.join(str(row.get(c, '')) for c in columns))

        if total > max_rows:
            lines.append(f'... ({total - max_rows} righe omesse, totale {total})')

        return '\n'.join(lines)
