import os
import uuid
import json
import re
from flask import Flask, redirect, request, session, jsonify
from flask_cors import CORS
import msal
from dotenv import load_dotenv
from fabric_agent import ask_fabric_agent


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

    result = ask_fabric_agent(token, data['prompt'].strip())
    if not result['success']:
        return jsonify({'error': result['error']}), 502

    parsed = parse_agent_response(result['response'])
    return jsonify({'success': True, **parsed})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, port=port)
