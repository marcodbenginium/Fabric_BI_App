"""
Router multi-agente con Phi-3.5 Mini (locale via Ollama)
----------------------------------------------------------
Decide quale agente Fabric chiamare in base alla domanda dell'utente.

Strategia (in ordine di priorità):
  1. Phi-3.5 Mini (Ollama locale)      ← principale, zero costi
  2. Keyword-based fallback             ← se Ollama non è disponibile
  3. Primo agente disponibile           ← ultimo fallback

Prerequisiti:
  - Ollama installato: https://ollama.com/download
  - Modello scaricato: ollama pull phi3.5
  - Ollama in esecuzione (si avvia automaticamente dopo l'installazione)
"""

import json
import logging
import re
from typing import Optional

import requests

from agents_registry import AgentDefinition, get_available_agents

logger = logging.getLogger(__name__)

OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = 'phi3.5'
ROUTER_TIMEOUT = 30   # secondi


# ── Router principale (Phi-3.5 Mini) ─────────────────────────────────────────

def _build_routing_prompt(question: str, agents: list[AgentDefinition]) -> str:
    """Costruisce il prompt di classificazione per Phi-3.5."""
    agents_desc = '\n'.join(
        f'- {a.id}: {a.description}' for a in agents
    )
    ids = ', '.join(a.id for a in agents)
    return (
        f'You are a routing assistant. Your only task is to classify a user question '
        f'and decide which agent should answer it.\n\n'
        f'Available agents:\n{agents_desc}\n\n'
        f'User question: "{question}"\n\n'
        f'Reply with ONLY the agent id (one of: {ids}). '
        f'No explanation, no punctuation, just the id.'
    )


def _route_with_phi(question: str, agents: list[AgentDefinition]) -> Optional[str]:
    """
    Chiama Phi-3.5 Mini via Ollama per scegliere l'agente.
    Restituisce l'id dell'agente scelto, o None in caso di errore.
    """
    prompt = _build_routing_prompt(question, agents)
    valid_ids = {a.id for a in agents}

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                'model': OLLAMA_MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
                'options': {
                    'temperature': 0,      # determinismo massimo per classificazione
                    'num_predict': 20,     # bastano pochi token per l'id
                },
            },
            timeout=ROUTER_TIMEOUT,
        )
        response.raise_for_status()
        raw = response.json().get('message', {}).get('content', '').strip().lower()

        # Estrai l'id dalla risposta (pulisce eventuali caratteri extra)
        # Cerca prima una corrispondenza esatta
        for agent_id in valid_ids:
            if agent_id in raw:
                logger.info(f'[Router] Phi-3.5 → {agent_id} (raw: "{raw}")')
                return agent_id

        logger.warning(f'[Router] Phi-3.5 risposta non riconosciuta: "{raw}"')
        return None

    except requests.exceptions.ConnectionError:
        logger.warning('[Router] Ollama non disponibile (ConnectionError) — uso fallback keyword')
        return None
    except requests.exceptions.Timeout:
        logger.warning('[Router] Ollama timeout — uso fallback keyword')
        return None
    except Exception as e:
        logger.warning(f'[Router] Errore Phi-3.5: {e} — uso fallback keyword')
        return None


# ── Fallback keyword-based ────────────────────────────────────────────────────

def _route_with_keywords(question: str, agents: list[AgentDefinition]) -> Optional[str]:
    """
    Routing semplice per corrispondenza di parole chiave.
    Vince l'agente con più keyword trovate nella domanda.
    """
    q = question.lower()
    scores: dict[str, int] = {}

    for agent in agents:
        score = sum(1 for kw in agent.keywords if kw in q)
        if score > 0:
            scores[agent.id] = score

    if not scores:
        return None

    best_id = max(scores, key=lambda k: scores[k])
    logger.info(f'[Router] Keyword fallback → {best_id} (scores: {scores})')
    return best_id


# ── Entry point pubblico ──────────────────────────────────────────────────────

def route(question: str) -> AgentDefinition:
    """
    Sceglie l'agente Fabric più adatto per rispondere alla domanda.

    Ordine:
      1. Phi-3.5 Mini (Ollama)
      2. Keyword-based fallback
      3. Primo agente disponibile

    Args:
        question: La domanda dell'utente in linguaggio naturale.

    Returns:
        L'AgentDefinition dell'agente scelto.

    Raises:
        RuntimeError: Se nessun agente è configurato nel .env.
    """
    agents = get_available_agents()

    if not agents:
        raise RuntimeError(
            'Nessun agente Fabric configurato. '
            'Controlla le variabili d\'ambiente nel .env.'
        )

    # Con un solo agente disponibile non serve routing
    if len(agents) == 1:
        logger.info(f'[Router] Un solo agente disponibile: {agents[0].id}')
        return agents[0]

    # 1. Prova con Phi-3.5 Mini
    chosen_id = _route_with_phi(question, agents)

    # 2. Fallback keyword
    if not chosen_id:
        chosen_id = _route_with_keywords(question, agents)

    # 3. Fallback: primo agente disponibile
    if not chosen_id:
        logger.warning('[Router] Nessun routing riuscito — uso primo agente disponibile')
        return agents[0]

    # Recupera e restituisce l'agente scelto
    agent = next((a for a in agents if a.id == chosen_id), agents[0])
    return agent


def check_ollama_status() -> dict:
    """
    Verifica se Ollama è disponibile e se il modello phi3.5 è installato.
    Usato dall'endpoint /api/router/status in app.py.
    """
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=5)
        r.raise_for_status()
        models = [m['name'] for m in r.json().get('models', [])]
        phi_ready = any('phi3.5' in m or 'phi3' in m for m in models)
        return {
            'ollama': True,
            'phi_ready': phi_ready,
            'models': models,
        }
    except Exception:
        return {'ollama': False, 'phi_ready': False, 'models': []}
