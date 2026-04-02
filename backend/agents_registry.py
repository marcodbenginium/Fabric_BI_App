"""
Registro degli agenti Fabric disponibili
-----------------------------------------
Per aggiungere un nuovo agente:
  1. Aggiungi la variabile d'ambiente con l'URL nel .env
  2. Aggiungi una voce in AGENTS_REGISTRY con:
       - id:          identificatore univoco (stringa breve)
       - name:        nome leggibile
       - description: usata dal router per decidere → SPIEGA CHIARAMENTE I DATI DISPONIBILI
       - env_var:     nome della variabile d'ambiente con l'URL dell'agente
       - keywords:    parole chiave di fallback (usate se Phi non è disponibile)
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentDefinition:
    id:          str
    name:        str
    description: str          # Descrizione usata dal router LLM
    env_var:     str          # Nome variabile d'ambiente con l'URL
    keywords:    list[str] = field(default_factory=list)  # Fallback keyword-based

    @property
    def url(self) -> Optional[str]:
        return os.getenv(self.env_var)

    @property
    def available(self) -> bool:
        return bool(self.url)


# ── Registro agenti ───────────────────────────────────────────────────────────
# Ordine irrilevante: il router sceglie in base alla description e alle keywords.

AGENTS_REGISTRY: list[AgentDefinition] = [

    AgentDefinition(
        id          = 'bi_agent',
        name        = 'Agente BI / Revenue',
        description = (
            'Risponde a domande su vendite, fatturato, revenue, margini, '
            'KPI commerciali, prodotti, clienti, country, mercati, '
            'performance finanziarie, budget, forecast e analytics di business.'
        ),
        env_var     = 'FABRIC_AGENT_TEST_COPILOT_URL',  # Agente Fabric: TEST_COPILOT
        keywords    = [
            'revenue', 'fatturato', 'vendite', 'sales', 'margine', 'profit',
            'kpi', 'country', 'prodotto', 'product', 'cliente', 'customer',
            'mercato', 'market', 'budget', 'forecast', 'quarterly', 'trimestre',
            'anno', 'year', 'mese', 'month', 'andamento', 'trend',
        ],
    ),

    AgentDefinition(
        id          = 'hr_agent',
        name        = 'Agente HR / Dipendenti',
        description = (
            'Risponde a domande sull\'anagrafica dei dipendenti, organizzazione '
            'aziendale, reparti, ruoli, headcount, assunzioni, turnover, '
            'stipendi, benefit, presenze, ferie e tutto ciò che riguarda '
            'le risorse umane.'
        ),
        env_var     = 'FABRIC_AGENT_DASHBOARD_HR_URL',  # Agente Fabric: AGENTE_DASHBOARD_HR
        keywords    = [
            'dipendente', 'dipendenti', 'employee', 'employees', 'hr', 'risorse umane',
            'reparto', 'department', 'ruolo', 'role', 'headcount', 'assunzione',
            'hiring', 'turnover', 'stipendio', 'salary', 'ferie', 'leave',
            'presenza', 'attendance', 'manager', 'team', 'organizzazione', 'org',
        ],
    ),

    # ── Template per agenti futuri ─────────────────────────────────────────
    # AgentDefinition(
    #     id          = 'supply_agent',
    #     name        = 'Agente Supply Chain',
    #     description = 'Risponde a domande su magazzino, scorte, ordini fornitori...',
    #     env_var     = 'FABRIC_SUPPLY_AGENT_URL',
    #     keywords    = ['magazzino', 'stock', 'ordine', 'fornitore', 'inventario'],
    # ),
]


def get_agent_by_id(agent_id: str) -> Optional[AgentDefinition]:
    return next((a for a in AGENTS_REGISTRY if a.id == agent_id), None)


def get_available_agents() -> list[AgentDefinition]:
    """Restituisce solo gli agenti con URL configurato nel .env."""
    return [a for a in AGENTS_REGISTRY if a.available]
