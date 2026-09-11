"""
feb_stats_sync.py
------------------
Descarga las estadisticas (por equipo) y los rankings (por jugadora) de una
competicion de la FEB desde el portal "feb.es/competiciones" (que sirve HTML
normal, sin JavaScript, a diferencia de las webs feb.es/<competicion>/... que
cargan las tablas por AJAX) y las vuelca en un Google Sheet.

Pensado para ejecutarse solo, por ejemplo via GitHub Actions (ver
.github/workflows/actualizar_feb_stats.yml), pero tambien puedes lanzarlo a
mano en tu ordenador con: python feb_stats_sync.py

CONFIGURACION
-------------
Todo lo que hay que tocar esta en el bloque CONFIG de aqui abajo, o se puede
sobreescribir con variables de entorno (utiles para GitHub Actions):

    FEB_GROUP_ID     -> id numerico de la competicion (LF2 = 9)
    FEB_SEASON_START -> año de inicio de temporada, p.ej. 2025 para 2025/2026
    FEB_SLUG         -> "nm" que usa la URL (LF2 = "lf2")
    GOOGLE_SHEET_ID  -> ID de tu Google Sheet (esta en la URL de la hoja)
    GOOGLE_SERVICE_ACCOUNT_JSON -> contenido JSON de la service account de Google

Como conseguir esos ids/credenciales -> ver README.md
"""

import os
import sys
import json
import io
from datetime import datetime, timezone

import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# ----------------------------- CONFIG ------------------------------------

GROUP_ID = os.environ.get("FEB_GROUP_ID", "9")          # LF2 = 9
SEASON_START = os.environ.get("FEB_SEASON_START", "2025")  # temporada 2025/2026
SLUG = os.environ.get("FEB_SLUG", "lf2")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PON_AQUI_EL_ID_DE_TU_GOOGLE_SHEET")

BASE_URL = "https://www.feb.es/competiciones"
ESTADISTICAS_URL = f"{BASE_URL}/estadisticas.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"
RANKINGS_URL = f"{BASE_URL}/rankings.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"
RESULTADOS_URL = f"{BASE_URL}/resultados.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"

HEADERS = {
    # Un user-agent de navegador normal evita bloqueos basicos.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ----------------------------- SCRAPING -----------------------------------


def fetch_tables(url: str) -> list[pd.DataFrame]:
    """Descarga una URL y devuelve todas las tablas HTML como DataFrames."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    try:
        # OJO: hay que envolver el HTML en io.StringIO(); las versiones
        # recientes de pandas ya no aceptan un string HTML "a pelo" y lo
        # intentan interpretar como una ruta de archivo, lo que provoca
        # un FileNotFoundError con todo el HTML en el mensaje de error.
        tables = pd.read_html(io.StringIO(resp.text))
    except ValueError:
        # No se encontro ninguna tabla en la pagina (p.ej. temporada sin datos)
        tables = []
    return tables


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Limpieza basica: quita columnas 'Unnamed' vacias y NaN sueltos."""
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df = df.fillna("")
    return df


# ----------------------------- GOOGLE SHEETS -------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_gspread_client() -> gspread.Client:
    creds_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_raw:
        # Tambien se puede apuntar a un fichero local para pruebas en local.
        creds_path = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
        )
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    else:
        creds_dict = json.loads(creds_raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def write_dataframe(sh: gspread.Spreadsheet, tab_name: str, df: pd.DataFrame):
    """Escribe (sobrescribiendo) un DataFrame en una pestaña del Sheet."""
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=tab_name, rows=max(len(df) + 10, 20), cols=max(len(df.columns) + 2, 10)
        )

    values = [list(df.columns.astype(str))] + df.astype(str).values.tolist()
    ws.update(values, value_input_option="USER_ENTERED")


def write_log(sh: gspread.Spreadsheet, message: str):
    try:
        ws = sh.worksheet("Log")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Log", rows=1000, cols=2)
        ws.update([["Fecha (UTC)", "Evento"]])
    ws.append_row([datetime.now(timezone.utc).isoformat(timespec="seconds"), message])


# ----------------------------- MAIN ----------------------------------------


def main():
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)

    resumen = []

    # 1) Estadisticas por equipo
    equipo_tables = fetch_tables(ESTADISTICAS_URL)
    if equipo_tables:
        for i, df in enumerate(equipo_tables):
            df = clean_dataframe(df)
            tab = "Equipos" if i == 0 else f"Equipos_{i}"
            write_dataframe(sh, tab, df)
            resumen.append(f"{tab}: {len(df)} filas")
    else:
        resumen.append(
            "Equipos: sin tablas (probablemente la temporada aun no tiene partidos)"
        )

    # 2) Rankings por jugadora
    ranking_tables = fetch_tables(RANKINGS_URL)
    if ranking_tables:
        for i, df in enumerate(ranking_tables):
            df = clean_dataframe(df)
            tab = "Jugadoras" if i == 0 else f"Jugadoras_{i}"
            write_dataframe(sh, tab, df)
            resumen.append(f"{tab}: {len(df)} filas")
    else:
        resumen.append("Jugadoras: sin tablas todavia")

    # 3) Resultados (util para saber que jornada es la ultima procesada)
    resultado_tables = fetch_tables(RESULTADOS_URL)
    if resultado_tables:
        df = clean_dataframe(resultado_tables[0])
        write_dataframe(sh, "Resultados", df)
        resumen.append(f"Resultados: {len(df)} filas")

    write_log(sh, " | ".join(resumen))
    print("Actualizacion completada:")
    for r in resumen:
        print(" -", r)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
