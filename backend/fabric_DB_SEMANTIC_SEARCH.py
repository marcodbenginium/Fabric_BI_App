"""Utility per connettersi al database Azure SQL / Fabric usando Azure AD Interactive.

Questo modulo fornisce funzioni helper per ottenere una connessione ODBC
autenticata tramite Azure AD (InteractiveBrowserCredential) e per
eseguire query semplici restituyendo righe come dizionari.

Configurazione consigliata (senza mettere credenziali nel codice):
- impostare le variabili d'ambiente `SEMANTIC_DB_SERVER` e `SEMANTIC_DB_DATABASE`
- opzionale: `ODBC_DRIVER` (default: "ODBC Driver 18 for SQL Server")

Se non vengono fornite variabili, i valori di default sono quelli indicati
nel requisito della richiesta (server + database).
"""

from __future__ import annotations

import os
import struct
from typing import Any, Dict, List, Optional

import pyodbc
import argparse
import base64
import json
from azure.identity import InteractiveBrowserCredential, ClientSecretCredential

# Default settings — preferire l'uso di variabili d'ambiente
DEFAULT_DRIVER = os.getenv("ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
DEFAULT_SERVER = os.getenv(
	"SEMANTIC_DB_SERVER",
	"n5dl6zgbqstevk7fmxwf7mnde4-iqrfi2wvop2edhhr6vy2q2ba54.database.fabric.microsoft.com,1433",
)
DEFAULT_DATABASE = os.getenv(
	"SEMANTIC_DB_DATABASE", "TEST_CONNECTION_DB-2b1e2d2a-7d39-4071-b638-35134f39cfbc"
)
_AZURE_SCOPE = os.getenv("AZURE_SCOPE", "https://database.windows.net/.default")


def _build_conn_str(driver: str, server: str, database: str, include_auth: bool = True) -> str:
	"""Build a typical ODBC connection string.

	If `include_auth` is True, append `Authentication=ActiveDirectoryInteractive`
	so the ODBC driver can perform browser-based interactive sign-in.
	"""
	conn = (
		f"DRIVER={{{driver}}};"
		f"SERVER={server};"
		f"DATABASE={database};"
		"Encrypt=yes;TrustServerCertificate=no;"
	)
	if include_auth:
		conn += "Authentication=ActiveDirectoryInteractive;"
	return conn


def _pack_access_token(access_token: str) -> bytes:
	"""Convert the access token string to the binary format expected by ODBC.

	The ODBC SQL_COPT_SS_ACCESS_TOKEN option (1256) expects a 4-byte length
	followed by the token bytes (little-endian length).
	"""
	token_bytes = access_token.encode("utf-16-le")
	# Use little-endian unsigned 32-bit length followed by UTF-16-LE token bytes
	return struct.pack("<I", len(token_bytes)) + token_bytes


def get_connection(
	server: Optional[str] = None,
	database: Optional[str] = None,
	driver: Optional[str] = None,
	credential: Optional[InteractiveBrowserCredential] = None,
	use_driver_auth: Optional[bool] = None,
) -> pyodbc.Connection:
	"""Return a pyodbc.Connection.

	By default the function will use the ODBC driver's interactive authentication
	flow (Authentication=ActiveDirectoryInteractive). Set `use_driver_auth` to
	False to obtain an access token via `InteractiveBrowserCredential` and
	pass it to the driver via `attrs_before` instead.
	"""
	server = server or DEFAULT_SERVER
	database = database or DEFAULT_DATABASE
	driver = driver or DEFAULT_DRIVER

	if use_driver_auth is None:
		use_driver_auth = os.getenv("USE_DRIVER_AUTH", "1").lower() in ("1", "true", "yes")

	# Driver-side interactive auth (opens browser via ODBC driver)
	if use_driver_auth:
		conn_str = _build_conn_str(driver, server, database, include_auth=True)
		return pyodbc.connect(conn_str)

	# Token-based approach (obtain token via azure.identity and attach to ODBC)
	credential = credential or InteractiveBrowserCredential()
	access_token = credential.get_token(_AZURE_SCOPE).token
	attrs_before = {1256: _pack_access_token(access_token)}

	conn_str = _build_conn_str(driver, server, database, include_auth=False)
	return pyodbc.connect(conn_str, attrs_before=attrs_before)


def execute_query(sql: str, params: Optional[List[Any]] = None, conn: Optional[pyodbc.Connection] = None) -> List[Dict[str, Any]]:
	"""Execute a query and return results as list of dicts.

	If `conn` is omitted, a new interactive connection will be opened.
	"""
	close_conn = False
	if conn is None:
		conn = get_connection()
		close_conn = True

	cur = conn.cursor()
	cur.execute(sql, params or [])

	columns = [col[0] for col in cur.description] if cur.description else []
	rows = [dict(zip(columns, row)) for row in cur.fetchall()]

	cur.close()
	if close_conn:
		conn.close()

	return rows


def list_tables(conn: Optional[pyodbc.Connection] = None) -> List[Dict[str, str]]:
	"""List base tables in the current database (schema + table name)."""
	sql = "SELECT TABLE_SCHEMA AS schema, TABLE_NAME AS table_name FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
	return execute_query(sql, conn=conn)


def get_credential() -> Any:
	"""Return a credential: ClientSecretCredential if AZURE_* env vars set, else InteractiveBrowserCredential."""
	client_id = os.getenv("AZURE_CLIENT_ID")
	tenant_id = os.getenv("AZURE_TENANT_ID")
	client_secret = os.getenv("AZURE_CLIENT_SECRET")
	if client_id and tenant_id and client_secret:
		return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
	return InteractiveBrowserCredential()


def _decode_jwt(token: str) -> Dict[str, Any]:
	"""Decode JWT payload without verification (for diagnostics only)."""
	try:
		parts = token.split('.')
		if len(parts) < 2:
			return {}
		payload = parts[1]
		padding = '=' * (-len(payload) % 4)
		payload += padding
		data = base64.urlsafe_b64decode(payload.encode('utf-8'))
		return json.loads(data)
	except Exception:
		return {}


def get_token_claims(credential: Optional[Any] = None) -> Dict[str, Any]:
	"""Acquire an access token and return useful claims for diagnosis."""
	cred = credential or get_credential()
	token = cred.get_token(_AZURE_SCOPE).token
	claims = _decode_jwt(token)
	info = {
		"upn": claims.get("upn") or claims.get("preferred_username"),
		"preferred_username": claims.get("preferred_username"),
		"oid": claims.get("oid"),
		"tid": claims.get("tid"),
		"exp": claims.get("exp"),
	}
	# Only include raw token if explicitly requested via env var
	if os.getenv("DEBUG_SHOW_TOKEN"):
		info["raw_token"] = token
	return info


def check_sql_identity(server: Optional[str] = None, database: Optional[str] = None, driver: Optional[str] = None, credential: Optional[Any] = None, use_driver_auth: Optional[bool] = None) -> None:
	"""Try to connect and run small identity/debug queries (prints results)."""
	cred = credential or get_credential()
	print("Getting token claims...")
	claims = get_token_claims(cred)
	print(json.dumps({k: claims.get(k) for k in ("upn","preferred_username","oid","tid","exp")}, indent=2))
	conn = None
	try:
		print("Attempting ODBC connection (this may raise a login/permission error)...")
		conn = get_connection(server=server, database=database, driver=driver, credential=cred, use_driver_auth=use_driver_auth)
		print("Connected. Running identity checks...")
		# Minimal query to confirm the connection can execute SQL without
		# relying on functions that might not be supported on every endpoint.
		rows = execute_query(
			"SELECT 1 AS connected",
			conn=conn,
		)
		for r in rows:
			print(json.dumps(r, indent=2))
	except Exception as e:
		print("Connection / query error:", e)
	finally:
		if conn:
			conn.close()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Utility per diagnostica Azure AD -> Azure SQL / Fabric")
	parser.add_argument("--diagnose", action="store_true", help="Mostra claims token e verifica identità SQL")
	parser.add_argument("--show-token", action="store_true", help="Mostra il token raw (solo debugging, attenzione)")
	args = parser.parse_args()

	if args.show_token:
		os.environ["DEBUG_SHOW_TOKEN"] = "1"

	if args.diagnose:
		print("Diagnostica identità: ottengo token e provo la connessione (potrebbe aprire il browser)...")
		cred = get_credential()
		claims = get_token_claims(cred)
		print("Token claims:")
		print(json.dumps({k: claims.get(k) for k in ("upn","preferred_username","oid","tid","exp")}, indent=2))
		check_sql_identity(credential=cred)
	else:
		# Comportamento preesistente: lista tabelle
		print("Listing tables (interactive auth may open a browser)...")
		try:
			for t in list_tables():
				print(f"{t['schema']}.{t['table_name']}")
		except Exception as e:
			print("Errore durante la connessione o la query:", e)

