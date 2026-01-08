import os
import json
import logging
import requests
import pyodbc
import azure.functions as func

app = func.FunctionApp()

def _get_conn() -> pyodbc.Connection:
    conn_str = os.environ["SQL_CONNECTION_STRING"]

    # pyodbc expects a Driver=...; part. If your portal string doesn't have it, add it.
    if "Driver=" not in conn_str and "DRIVER=" not in conn_str:
        conn_str = "Driver={ODBC Driver 18 for SQL Server};" + conn_str

    return pyodbc.connect(conn_str)

@app.route(route="db-ping", auth_level=func.AuthLevel.ANONYMOUS)
def db_ping(req: func.HttpRequest) -> func.HttpResponse:
    conn_str = os.getenv("SQL_CONNECTION_STRING")
    if not conn_str:
        return func.HttpResponse("SQL_CONNECTION_STRING not set", status_code=500)

    try:
        with pyodbc.connect(conn_str) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
        return func.HttpResponse(f"DB OK ✅ result={row[0]}")
    except Exception as e:
        return func.HttpResponse(f"DB FAIL ❌ {type(e).__name__}: {e}", status_code=500)
