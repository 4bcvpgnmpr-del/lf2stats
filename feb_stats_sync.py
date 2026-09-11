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
from bs4 import BeautifulSoup
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


def fetch_ranking_with_photos(url: str) -> pd.DataFrame:
    """Descarga la tabla de rankings de jugadoras conservando la foto de cada una.

    A diferencia de las demas tablas (leidas con pandas.read_html), esta se
    parsea a mano con BeautifulSoup porque necesitamos conservar la URL de
    la foto de cada jugadora, dato que pandas.read_html descarta al quedarse
    solo con el texto de las celdas.

    Las fotos se guardan como formulas =IMAGE(...) para que Google Sheets
    las muestre como miniaturas dentro de la propia celda.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    target_table = None
    for table in soup.find_all("table"):
        if "Jugador" in table.get_text(" ", strip=True) and len(table.find_all("tr")) > 1:
            target_table = table
            break

    if target_table is None:
        return pd.DataFrame()

    rows = target_table.find_all("tr")
    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    if headers and headers[0] == "":
        headers[0] = "Foto"

    data = []
    for tr in rows[1:]:
        cells = tr.find_all("td")
        # Filas como la de paginacion al final de la tabla ("1 2 3 4 5...")
        # tienen menos celdas que columnas (usan colspan) -> se descartan.
        if len(cells) < len(headers):
            continue
        row_values = []
        for cell in cells:
            img = cell.find("img")
            if img and img.get("src"):
                src = img["src"]
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = "https://imagenes.feb.es" + src
                row_values.append(f'=IMAGE("{src}", 4, 40, 40)')
            else:
                row_values.append(cell.get_text(strip=True))
        data.append(row_values)

    if not data:
        return pd.DataFrame()

    ncols = len(data[0])
    headers = (headers + [f"col_{i}" for i in range(len(headers), ncols)])[:ncols]
    df = pd.DataFrame(data, columns=headers)
    # Por si quedara algun NaN suelto (celdas vacias, filas irregulares):
    # rellenar con texto vacio para que sea JSON-serializable al escribir
    # en Google Sheets.
    df = df.fillna("")
    return df


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Limpieza basica: quita columnas 'Unnamed' vacias y NaN sueltos.

    Algunas tablas de la FEB tienen encabezados de dos niveles (p.ej. un
    grupo "Totales" con subcolumnas debajo), que pandas representa como un
    MultiIndex. Hay que aplanarlos a texto simple antes de poder filtrar
    las columnas "Unnamed".
    """
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join(str(level) for level in col if str(level) != "" and not str(level).startswith("Unnamed"))
            or f"col_{i}"
            for i, col in enumerate(df.columns.values)
        ]
    else:
        df.columns = df.columns.astype(str)

    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
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


HEADER_BG = {"red": 0.10, "green": 0.16, "blue": 0.33}   # azul oscuro
HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}       # texto blanco
BAND_COLOR = {"red": 0.93, "green": 0.95, "blue": 0.98}   # gris azulado muy claro


def _existing_banding_id(sh: gspread.Spreadsheet, sheet_id: int):
    """Busca si la pestaña ya tiene una banda de colores alternos aplicada."""
    meta = sh.fetch_sheet_metadata()
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == sheet_id:
            bandings = s.get("bandedRanges", [])
            if bandings:
                return bandings[0]["bandedRangeId"]
    return None


def style_worksheet(sh: gspread.Spreadsheet, ws: gspread.Worksheet, n_rows: int, has_photos: bool = False):
    """Aplica un estilo profesional: cabecera coloreada, fila superior fija
    y filas alternas en gris claro. Si has_photos=True, tambien agranda la
    primera columna y las filas para que las miniaturas de las fotos quepan
    bien."""
    sheet_id = ws.id

    requests_list = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_BG,
                        "textFormat": {"bold": True, "foregroundColor": HEADER_FG},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
    ]

    banding_range = {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": max(n_rows + 1, 2)}
    banding_props = {
        "range": banding_range,
        "rowProperties": {
            "headerColor": HEADER_BG,
            "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
            "secondBandColor": BAND_COLOR,
        },
    }
    existing_id = _existing_banding_id(sh, sheet_id)
    if existing_id:
        requests_list.append(
            {"updateBanding": {"bandedRange": {"bandedRangeId": existing_id, **banding_props}, "fields": "range,rowProperties"}}
        )
    else:
        requests_list.append({"addBanding": {"bandedRange": banding_props}})

    if has_photos:
        requests_list.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 50},
                    "fields": "pixelSize",
                }
            }
        )
        requests_list.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": max(n_rows + 1, 2)},
                    "properties": {"pixelSize": 45},
                    "fields": "pixelSize",
                }
            }
        )

    sh.batch_update({"requests": requests_list})


def write_dataframe(sh: gspread.Spreadsheet, tab_name: str, df: pd.DataFrame, has_photos: bool = False):
    """Escribe (sobrescribiendo) un DataFrame en una pestaña del Sheet, y le
    aplica un formato profesional (cabecera, colores alternos, fotos)."""
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=tab_name, rows=max(len(df) + 10, 20), cols=max(len(df.columns) + 2, 10)
        )

    values = [list(df.columns.astype(str))] + df.fillna("").astype(str).values.tolist()
    ws.update(values, value_input_option="USER_ENTERED")

    try:
        style_worksheet(sh, ws, n_rows=len(df), has_photos=has_photos)
    except Exception as exc:  # noqa: BLE001
        # El formato es "nice to have": si falla no debe tumbar la
        # actualizacion de datos, que es lo importante.
        print(f"Aviso: no se pudo aplicar formato a '{tab_name}': {exc}", file=sys.stderr)


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

    # 2) Rankings por jugadora (con foto de cada jugadora)
    ranking_df = fetch_ranking_with_photos(RANKINGS_URL)
    if not ranking_df.empty:
        write_dataframe(sh, "Jugadoras", ranking_df, has_photos=True)
        resumen.append(f"Jugadoras: {len(ranking_df)} filas (con foto)")
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
