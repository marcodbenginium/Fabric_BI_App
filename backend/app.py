import os
import uuid
import json
import re
import io
import base64
import requests
from pathlib import Path
from datetime import datetime
from flask import Flask, redirect, request, session, jsonify, send_file
from flask_cors import CORS
import msal
from dotenv import load_dotenv
from fpdf import FPDF
from fabric_agent import ask_fabric_agent
from agents_registry import get_available_agents
from router import route, check_ollama_status


def parse_agent_response(raw: str) -> dict:
    """
    Analizza la risposta dell'agente Fabric.
    Se contiene un JSON con 'chart': true, restituisce anche chart_data.
    """
    text = raw.strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            parsed = json.loads(match.group())
            if parsed.get('chart'):
                return {
                    'type': 'chart',
                    'chart_data': parsed,
                    'response': parsed.get('title', 'Grafico'),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    return {'type': 'text', 'response': text}

load_dotenv()

app = Flask(__name__, static_folder='../frontend', static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(32).hex())
CORS(app, supports_credentials=True)

# DEV_MODE=true → usa Azure CLI (az login), nessuna App Registration necessaria
DEV_MODE = os.getenv('DEV_MODE', 'false').lower() == 'true'

DASHBOARD_FILE = Path(__file__).parent / 'dashboard_widgets.json'

def _load_dashboard():
    if DASHBOARD_FILE.exists():
        try:
            return json.loads(DASHBOARD_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return []

def _save_dashboard(widgets):
    DASHBOARD_FILE.write_text(json.dumps(widgets, ensure_ascii=False, indent=2), encoding='utf-8')

CLIENT_ID     = os.getenv('AZURE_CLIENT_ID')
CLIENT_SECRET = os.getenv('AZURE_CLIENT_SECRET')
TENANT_ID     = os.getenv('AZURE_TENANT_ID')
AUTHORITY     = f'https://login.microsoftonline.com/{TENANT_ID}' if TENANT_ID else None
REDIRECT_URI  = os.getenv('REDIRECT_URI', 'http://localhost:5000/auth/callback')
SCOPES        = ['https://api.fabric.microsoft.com/.default']
FABRIC_SCOPE  = 'https://api.fabric.microsoft.com/.default'


def _get_cli_token() -> str:
    """Ottieni token da Azure CLI (dev mode)."""
    from azure.identity import AzureCliCredential
    return AzureCliCredential().get_token(FABRIC_SCOPE).token


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.send_static_file('index.html')


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login')
def login():
    if DEV_MODE:
        return redirect('/')   # nessun login necessario in dev mode
    state = str(uuid.uuid4())
    session['oauth_state'] = state
    auth_url = _msal_app().get_authorization_request_url(
        SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
    )
    return redirect(auth_url)


@app.route('/auth/callback')
def auth_callback():
    if DEV_MODE:
        return redirect('/')
    # Validazione CSRF state
    if request.args.get('state') != session.pop('oauth_state', None):
        return jsonify({'error': 'State mismatch — possibile attacco CSRF'}), 400

    code = request.args.get('code')
    if not code:
        error_desc = request.args.get('error_description', 'Autorizzazione negata')
        return jsonify({'error': error_desc}), 400

    result = _msal_app().acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    if 'error' in result:
        return jsonify({'error': result.get('error_description', result['error'])}), 400

    session['access_token'] = result['access_token']
    session['user'] = result.get('id_token_claims', {})
    return redirect('/')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


# ── API ────────────────────────────────────────────────────────────────────────

@app.route('/api/me')
def me():
    if DEV_MODE:
        return jsonify({'authenticated': True, 'name': 'Dev (Azure CLI)'})
    user = session.get('user')
    if not user:
        return jsonify({'authenticated': False}), 401
    return jsonify({
        'authenticated': True,
        'name': user.get('name') or user.get('preferred_username', 'Utente'),
    })


@app.route('/api/router/status')
def router_status():
    """Stato del router: agenti configurati e disponibilità Ollama/Phi."""
    agents = get_available_agents()
    ollama = check_ollama_status()
    return jsonify({
        'agents': [{'id': a.id, 'name': a.name} for a in agents],
        'ollama': ollama,
    })


@app.route('/api/chat', methods=['POST'])
def chat():
    if DEV_MODE:
        try:
            token = _get_cli_token()
        except Exception as e:
            return jsonify({'error': f'Azure CLI non disponibile: {e}. Esegui az login.'}), 401
    else:
        token = session.get('access_token')
        if not token:
            return jsonify({'error': 'Non autenticato'}), 401

    data = request.get_json(silent=True)
    if not data or not data.get('prompt', '').strip():
        return jsonify({'error': 'Parametro "prompt" mancante o vuoto'}), 400

    prompt = data['prompt'].strip()

    # Scegli l'agente più adatto con il router (Phi-3.5 Mini o keyword fallback)
    try:
        agent = route(prompt)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 503

    # Recupera thread/assistant persistenti per questo agente nella sessione
    fabric_threads = session.get('fabric_threads', {})
    agent_thread   = fabric_threads.get(agent.id, {})

    # Chiama il Fabric Data Agent selezionato (riutilizza thread se disponibile)
    result = ask_fabric_agent(
        token,
        prompt,
        agent_url    = agent.url,
        thread_id    = agent_thread.get('thread_id'),
        assistant_id = agent_thread.get('assistant_id'),
    )

    if not result['success']:
        return jsonify({'error': result['error'], 'agent_id': agent.id}), 502

    # Conserva thread_id e assistant_id nella sessione per i prossimi messaggi
    fabric_threads[agent.id] = {
        'thread_id':    result.get('thread_id'),
        'assistant_id': result.get('assistant_id'),
    }
    session['fabric_threads'] = fabric_threads
    session.modified = True

    # Salva Q&A nella history locale (per summary e PDF)
    history = session.get('chat_history', [])
    history.append({
        'role':       'user',
        'text':       prompt,
        'agent_id':   agent.id,
        'agent_name': agent.name,
        'ts':         datetime.utcnow().isoformat(),
    })
    parsed = parse_agent_response(result['response'])
    agent_entry = {
        'role':       'agent',
        'text':       parsed.get('response', ''),
        'entry_type': parsed.get('type', 'text'),
        'ts':         datetime.utcnow().isoformat(),
    }
    if parsed.get('type') == 'chart' and parsed.get('chart_data'):
        agent_entry['chart_data'] = parsed['chart_data']
    history.append(agent_entry)
    session['chat_history'] = history
    session.modified = True

    return jsonify({'success': True, 'agent_id': agent.id, 'agent_name': agent.name, **parsed})


@app.route('/api/chat/reset', methods=['POST'])
def chat_reset():
    """Azzera thread Fabric e history della sessione corrente."""
    session.pop('fabric_threads', None)
    session.pop('chat_history', None)
    return jsonify({'success': True})


@app.route('/api/chat/summary', methods=['POST'])
def chat_summary():
    """Genera un riassunto della conversazione usando Phi-3.5 Mini (locale, zero capacity Fabric)."""
    history = session.get('chat_history', [])
    if not history:
        return jsonify({'success': True, 'summary': 'Nessuna conversazione da riassumere.'})

    # Costruisci la trascrizione per Phi
    transcript_lines = []
    for entry in history:
        if entry['role'] == 'user':
            transcript_lines.append(f"Utente: {entry['text']}")
        else:
            if entry.get('entry_type') == 'chart' and entry.get('chart_data'):
                cd = entry['chart_data']
                rows_text = ', '.join(
                    ' / '.join(f"{k}: {v}" for k, v in row.items())
                    for row in (cd.get('data') or [])
                )
                transcript_lines.append(
                    f"Agente (grafico '{cd.get('title', '')}'): {rows_text}"
                )
            else:
                transcript_lines.append(f"Agente: {entry['text']}")
    transcript = '\n'.join(transcript_lines)

    prompt = (
        'Di seguito è riportata una conversazione tra un utente e un assistente dati. '
        'Scrivi un riassunto conciso in italiano, evidenziando le domande principali '
        'e i dati chiave emersi dalle risposte.\n\n'
        f'{transcript}\n\n'
        'Riassunto:'
    )

    try:
        resp = requests.post(
            'http://localhost:11434/api/chat',
            json={
                'model': 'phi3.5',
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
                'options': {'temperature': 0.3},
            },
            timeout=60,
        )
        resp.raise_for_status()
        summary = resp.json().get('message', {}).get('content', '').strip()
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Ollama non disponibile. Assicurati che sia in esecuzione.'}), 503
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({'error': f'Errore Phi-3.5: {e}'}), 500

    return jsonify({'success': True, 'summary': summary})


@app.route('/api/chat/export-pdf', methods=['POST'])
def export_pdf():
    """Genera e scarica un PDF con la history della conversazione e il riassunto."""
    data    = request.get_json(silent=True) or {}
    summary = data.get('summary', '')
    charts  = data.get('charts', [])  # lista di dataURL base64 dai canvas Chart.js
    history = session.get('chat_history', [])

    if not history and not summary:
        return jsonify({'error': 'Nessuna conversazione da esportare'}), 400

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Titolo
    pdf.set_font('Helvetica', 'B', 16)
    pdf.cell(0, 10, 'Report Conversazione - Fabric Data Agent', ln=True, align='C')
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(0, 6, f'Generato il {datetime.utcnow().strftime("%d/%m/%Y %H:%M")} UTC', ln=True, align='C')
    pdf.ln(6)

    # Riassunto
    if summary:
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(0, 8, 'Riassunto', ln=True)
        pdf.set_draw_color(0, 120, 212)
        pdf.set_line_width(0.5)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)
        pdf.set_font('Helvetica', '', 10)
        pdf.multi_cell(0, 6, summary)
        pdf.ln(8)

    # Conversazione
    if history:
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(0, 8, 'Conversazione', ln=True)
        pdf.set_draw_color(0, 120, 212)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)

        chart_index = 0
        for entry in history:
            if entry['role'] == 'user':
                pdf.set_font('Helvetica', 'B', 10)
                agent_label = entry.get('agent_name', '')
                label = f'Tu  ->  {agent_label}:' if agent_label else 'Tu:'
                pdf.set_text_color(0, 120, 212)
                pdf.cell(0, 7, label, ln=True)
                pdf.set_text_color(0, 0, 0)
                pdf.set_font('Helvetica', '', 10)
                pdf.multi_cell(0, 6, entry['text'])
            else:
                pdf.set_font('Helvetica', 'B', 10)
                pdf.set_text_color(30, 130, 80)
                if entry.get('entry_type') == 'chart':
                    pdf.cell(0, 7, f'Agente (Grafico): {entry["text"]}', ln=True)
                    pdf.set_text_color(0, 0, 0)
                    if chart_index < len(charts):
                        raw = charts[chart_index]
                        if ',' in raw:
                            raw = raw.split(',', 1)[1]
                        img_bytes = base64.b64decode(raw)
                        pdf.image(io.BytesIO(img_bytes), w=180)
                    chart_index += 1
                else:
                    pdf.cell(0, 7, 'Agente:', ln=True)
                    pdf.set_text_color(0, 0, 0)
                    pdf.set_font('Helvetica', '', 10)
                    pdf.multi_cell(0, 6, entry['text'])
            pdf.ln(3)

    pdf_bytes = pdf.output()
    buf = io.BytesIO(bytes(pdf_bytes))
    filename = f'conversazione_{datetime.utcnow().strftime("%Y%m%d_%H%M")}.pdf'
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


# ── Dashboard widgets ──────────────────────────────────────────────────────
@app.route('/api/dashboard', methods=['GET'])
def dashboard_get():
    return jsonify(_load_dashboard())


@app.route('/api/dashboard', methods=['POST'])
def dashboard_add():
    data = request.get_json(silent=True) or {}
    chart_data = data.get('chart_data')
    if not chart_data:
        return jsonify({'error': 'chart_data mancante'}), 400
    widgets = _load_dashboard()
    widget = {
        'id':         str(uuid.uuid4()),
        'title':      chart_data.get('title', 'Grafico'),
        'chart_data': chart_data,
        'created_at': datetime.utcnow().isoformat(),
    }
    widgets.append(widget)
    _save_dashboard(widgets)
    return jsonify({'success': True, 'widget': widget}), 201


@app.route('/api/dashboard/<widget_id>', methods=['DELETE'])
def dashboard_remove(widget_id):
    widgets = _load_dashboard()
    widgets = [w for w in widgets if w['id'] != widget_id]
    _save_dashboard(widgets)
    return jsonify({'success': True})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, port=port)
