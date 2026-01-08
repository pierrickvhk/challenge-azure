import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
import pyodbc
import requests

app = func.FunctionApp()


def _get_conn_str() -> str:
    # Preferred: single connection string
    cs = os.getenv("SQL_CONNECTION_STRING")
    if cs:
        if "Driver=" not in cs and "DRIVER=" not in cs:
            cs = "Driver={ODBC Driver 18 for SQL Server};" + cs
        return cs

    # Fallback: build from parts (works with your current portal-style settings)
    server = os.getenv("SQL_SERVER")
    db = os.getenv("SQL_DB")
    user = os.getenv("SQL_USER")
    pwd = os.getenv("SQL_PASSWORD")
    if not all([server, db, user, pwd]):
        raise KeyError("Missing SQL_CONNECTION_STRING or SQL_SERVER/SQL_DB/SQL_USER/SQL_PASSWORD")

    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;"
        f"Database={db};"
        f"Uid={user};"
        f"Pwd={pwd};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


def _get_conn() -> pyodbc.Connection:
    return pyodbc.connect(_get_conn_str())


def _fetch_one(sql: str, params: tuple = ()) -> dict:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return {}
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


@app.route(route="db-ping", auth_level=func.AuthLevel.FUNCTION)
def db_ping(req: func.HttpRequest) -> func.HttpResponse:
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
        return func.HttpResponse(f"DB OK result={row[0]}")
    except Exception as e:
        logging.exception("DB ping failed")
        return func.HttpResponse(f"DB FAIL {type(e).__name__}: {e}", status_code=500)
    
@app.route(route="health-stats", auth_level=func.AuthLevel.FUNCTION)
def health_stats(req: func.HttpRequest) -> func.HttpResponse:
    try:
        ping = _fetch_one("SELECT 1 AS result;")
        if ping.get("result") != 1:
            raise RuntimeError("DB ping failed")

        stats = _fetch_one(
            """
            SELECT
                COUNT(*) AS total_24h,
                SUM(CASE WHEN cancelled = 1 THEN 1 ELSE 0 END) AS cancelled_24h,
                SUM(CASE WHEN delay_seconds > 0 THEN 1 ELSE 0 END) AS delayed_24h,
                CAST(AVG(CAST(delay_seconds AS float)) AS float) AS avg_delay_seconds_24h,
                MAX(ingest_utc) AS last_ingest_utc
            FROM dbo.departure_fact
            WHERE ingest_utc >= DATEADD(HOUR, -24, SYSUTCDATETIME());
            """
        )

        payload = {
            "ok": True,
            "utc_now": datetime.now(timezone.utc).isoformat(),
            "station_default": os.getenv("IRAIL_STATION"),
            "db": {"ping": "ok"},
            "stats": stats,
        }
        return func.HttpResponse(json.dumps(payload, default=str), mimetype="application/json", status_code=200)

    except Exception as e:
        logging.exception("health-stats failed")
        return func.HttpResponse(
            json.dumps({"ok": False, "error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )



@app.route(route="ingest", auth_level=func.AuthLevel.FUNCTION)
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    station = req.params.get("station") or os.getenv("IRAIL_STATION", "Gent-Sint-Pieters")
    lang = os.getenv("IRAIL_LANG", "en")

    try:
        # 1) Fetch iRail liveboard
        r = requests.get(
            "https://api.irail.be/liveboard/",
            params={"station": station, "format": "json", "lang": lang, "alerts": "false"},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()

        departures = payload.get("departures", {}).get("departure", [])
        if isinstance(departures, dict):
            departures = [departures]

        inserted = 0

        # 2) Insert into SQL
        with _get_conn() as conn:
            cur = conn.cursor()

            for d in departures:
                vehicle_id = d.get("vehicle")
                headsign = (d.get("direction") or {}).get("name") if isinstance(d.get("direction"), dict) else d.get("direction")
                platform = str(d.get("platform")) if d.get("platform") is not None else None
                if platform == "?":
                  platform = None
                delay_seconds = int(d.get("delay", 0)) if d.get("delay") is not None else None
                cancelled = 1 if str(d.get("canceled", "0")) == "1" else 0

                planned_utc = None
                t = d.get("time")
                if t:
                    planned_utc = datetime.fromtimestamp(int(t), tz=timezone.utc).replace(tzinfo=None)

                raw_json = json.dumps(d, ensure_ascii=False)
                try:
                    cur.execute(
                    """
                    INSERT INTO dbo.departure_fact
                      (station_name, vehicle_id, headsign, platform, planned_departure_utc, delay_seconds, cancelled, raw_json)
                    VALUES
                      (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (station, vehicle_id, headsign, platform, planned_utc, delay_seconds, cancelled, raw_json),
                    )
                    inserted += 1
                except pyodbc.IntegrityError:
                    pass

            conn.commit()

        return func.HttpResponse(
            json.dumps({"station": station, "fetched": len(departures), "inserted": inserted}),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.exception("Ingest failed")
        return func.HttpResponse(f"INGEST FAIL {type(e).__name__}: {e}", status_code=500)
