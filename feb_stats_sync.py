"""
feb_stats_sync.py
------------------
Descarga las estadisticas (por equipo) y los rankings (por jugadora) de una
competicion de la FEB desde el portal "feb.es/competiciones" y las vuelca en
un Google Sheet.

Pensado para ejecutarse solo via GitHub Actions, pero tambien puedes lanzarlo
a mano con: python feb_stats_sync.py

CONFIGURACION (variables de entorno):
    FEB_GROUP_ID     -> id numerico de la competicion (LF2 = 9)
    FEB_SEASON_START -> año de inicio de temporada, p.ej. 2025 para 2025/2026
    FEB_SLUG         -> "nm" que usa la URL (LF2 = "lf2")
    GOOGLE_SHEET_ID  -> ID de tu Google Sheet
    GOOGLE_SERVICE_ACCOUNT_JSON -> contenido JSON de la service account
"""

import os
import sys
import json
import io
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
import pandas as pd
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

# ----------------------------- CONFIG ------------------------------------

GROUP_ID = os.environ.get("FEB_GROUP_ID", "9")
SEASON_START = os.environ.get("FEB_SEASON_START", "2025")
SLUG = os.environ.get("FEB_SLUG", "lf2")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PON_AQUI_EL_ID_DE_TU_GOOGLE_SHEET")

BASE_URL = "https://www.feb.es/competiciones"
ESTADISTICAS_URL = f"{BASE_URL}/estadisticas.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"
RANKINGS_URL = f"{BASE_URL}/rankings.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"
RESULTADOS_URL = f"{BASE_URL}/resultados.aspx?g={GROUP_ID}&t={SEASON_START}&nm={SLUG}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Red: reintentos y pausas (la web de la FEB a veces tarda en responder)
MAX_RETRIES = 3
PAUSA_ENTRE_PETICIONES = 0.7   # segundos
MAX_PAGINAS = 40               # tope de seguridad por categoria

# ----------------------------- RED ----------------------------------------


def _request_con_reintentos(session, method, url, **kwargs):
    """Hace una peticion HTTP con hasta MAX_RETRIES intentos."""
    ultimo_error = None
    for intento in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, headers=HEADERS, timeout=30, **kwargs)
            resp.raise_for_status()
            time.sleep(PAUSA_ENTRE_PETICIONES)
            return resp
        except requests.RequestException as exc:
            ultimo_error = exc
            print(f"  Aviso: intento {intento}/{MAX_RETRIES} fallido ({method} {url}): {exc}", file=sys.stderr)
            if intento < MAX_RETRIES:
                time.sleep(3 * intento)
    raise ultimo_error


# ----------------------------- SCRAPING -----------------------------------


def fetch_tables(url: str) -> list[pd.DataFrame]:
    """Descarga una URL y devuelve todas las tablas HTML como DataFrames."""
    resp = _request_con_reintentos(requests.Session(), "GET", url)
    try:
        # io.StringIO: las versiones recientes de pandas no aceptan HTML "a pelo"
        tables = pd.read_html(io.StringIO(resp.text))
    except ValueError:
        tables = []
    return tables


def get_formula_separator(sh: gspread.Spreadsheet) -> str:
    """';' para hojas en español, ',' para hojas en ingles."""
    try:
        meta = sh.fetch_sheet_metadata()
        locale = meta.get("properties", {}).get("locale", "")
    except Exception:  # noqa: BLE001
        locale = ""
    return ";" if not locale.startswith("en") else ","


def _tabla_ranking(soup: BeautifulSoup):
    """Devuelve la tabla de jugadoras de una pagina de rankings (o None)."""
    for table in soup.find_all("table"):
        if "Jugador" in table.get_text(" ", strip=True) and len(table.find_all("tr")) > 1:
            return table
    return None


def _es_fila_paginacion(tr) -> bool:
    """True si la fila es la de paginacion ("1 2 3 ...") y no una jugadora."""
    for td in tr.find_all("td"):
        if td.get("colspan"):
            return True
    textos = [c.get_text(strip=True) for c in tr.find_all("td")]
    textos = [t for t in textos if t]
    if textos and all(t.isdigit() or t in ("...", "…", ">", ">>", "<", "<<", "»", "«") for t in textos):
        return True
    return False


def parse_ranking_html(html_text: str, formula_sep: str = ";") -> pd.DataFrame:
    """Parsea UNA pagina de rankings de la FEB (cualquier categoria),
    conservando la foto de cada jugadora como formula =IMAGE(...)."""
    soup = BeautifulSoup(html_text, "html.parser")
    target_table = _tabla_ranking(soup)
    if target_table is None:
        return pd.DataFrame()

    # Solo filas propias de la tabla (no las de tablas anidadas, como un
    # paginador construido con otra <table> dentro).
    rows = [tr for tr in target_table.find_all("tr") if tr.find_parent("table") is target_table]
    if len(rows) < 2:
        return pd.DataFrame()

    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    if headers and headers[0] == "":
        headers[0] = "Foto"
    headers.append("FotoURL")

    data = []
    for tr in rows[1:]:
        if _es_fila_paginacion(tr):
            continue
        cells = [td for td in tr.find_all("td") if td.find_parent("tr") is tr]
        if len(cells) < len(headers) - 1:
            continue
        row_values = []
        foto_url = ""
        for cell in cells:
            img = cell.find("img")
            if img and img.get("src"):
                src = img["src"]
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = "https://imagenes.feb.es" + src
                foto_url = src
                row_values.append(f'=IMAGE("{src}"{formula_sep} 4{formula_sep} 40{formula_sep} 40)')
            else:
                row_values.append(cell.get_text(strip=True))
        row_values.append(foto_url)
        data.append(row_values)

    if not data:
        return pd.DataFrame()

    ncols = max(len(r) for r in data)
    data = [r + [""] * (ncols - len(r)) for r in data]
