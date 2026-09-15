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


def get_formula_separator(sh: gspread.Spreadsheet) -> str:
    """Google Sheets usa ',' como separador de argumentos en formulas si el
    idioma de la hoja es ingles, pero ';' si es español (y la mayoria de
    idiomas europeos). Sin esto, =IMAGE(url, 4, 40, 40) da #ERROR! en una
    hoja configurada en español."""
    try:
        meta = sh.fetch_sheet_metadata()
        locale = meta.get("properties", {}).get("locale", "")
    except Exception:  # noqa: BLE001
        locale = ""
    return ";" if not locale.startswith("en") else ","


def fetch_ranking_with_photos(url: str, formula_sep: str = ";") -> pd.DataFrame:
    """Descarga la tabla de rankings de jugadoras (categoria "Puntos", la que
    sale por defecto) conservando la foto de cada una.

    A diferencia de las demas tablas (leidas con pandas.read_html), esta se
    parsea a mano con BeautifulSoup porque necesitamos conservar la URL de
    la foto de cada jugadora, dato que pandas.read_html descarta al quedarse
    solo con el texto de las celdas.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_ranking_html(resp.text, formula_sep)


def parse_ranking_html(html_text: str, formula_sep: str = ";") -> pd.DataFrame:
    """Parsea el HTML de una pagina de rankings de la FEB (la tabla de
    jugadoras con foto), sea cual sea la categoria mostrada (Puntos,
    Rebotes, Asistencias...). Las fotos se guardan como formulas =IMAGE(...)
    para que Google Sheets las muestre como miniaturas."""
    soup = BeautifulSoup(html_text, "html.parser")

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
    headers.append("FotoURL")  # columna extra en texto plano (uso de apps externas)

    data = []
    for tr in rows[1:]:
        cells = tr.find_all("td")
        # Filas como la de paginacion al final de la tabla ("1 2 3 4 5...")
        # tienen menos celdas que columnas (usan colspan) -> se descartan.
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
        row_values.append(foto_url)  # FotoURL en texto plano al final de la fila
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


# ----------------------------- OTRAS CATEGORIAS DE RANKING -----------------
#
# La web de la FEB usa un desplegable "Puntos / Rebotes Totales / Rebotes
# Ofensivos / ... / Valoracion" para elegir la categoria del ranking, pero
# NO es un simple parametro en la URL: es un control ASP.NET clasico que
# envia un "postback" (una peticion POST con unos tokens de sesion largos:
# __VIEWSTATE, __EVENTVALIDATION...). Para descargar varias categorias hay
# que simular ese postback con requests.Session().
#
# Indices reales del desplegable, confirmados capturando la peticion real
# del navegador (Chrome DevTools -> Network -> Payload) el 13/09/2026:
RANKING_CATEGORIES = {
    "Rebotes_Totales": 1,
    "Asistencias": 4,
    "Robos": 5,       # "Balones recuperados" en la FEB
    "Tapones_Favor": 7,
    "Valoracion": 12,
}

RANKINGS_DROPDOWN_FIELD = "_ctl0:MainContentPlaceHolderMaster:rankingsDropDownList"


def _extract_form_state(soup: BeautifulSoup) -> dict:
    """Extrae todos los campos de un formulario ASP.NET (inputs ocultos +
    selects con su opcion seleccionada), necesarios para poder enviar un
    postback valido sin perder el resto del estado de la pagina."""
    state = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("hidden", "text", "submit"):
            state[name] = inp.get("value", "")
    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        chosen = sel.find("option", selected=True) or sel.find("option")
        state[name] = chosen.get("value", "") if chosen else ""
    return state


def fetch_all_ranking_categories(url: str, formula_sep: str = ";") -> dict:
    """Descarga el resto de categorias de ranking (Rebotes, Asistencias,
    Robos, Tapones, Valoracion) simulando el postback del desplegable de
    la FEB. Devuelve un dict {nombre_categoria: DataFrame}.

    Es intencionadamente tolerante a fallos: si la web cambia y el postback
    deja de funcionar, se registra un aviso y se omite esa categoria en vez
    de tumbar el resto de la sincronizacion (Equipos, Puntos, Resultados
    siguen funcionando igual)."""
    results = {}
    try:
        session = requests.Session()
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        state = _extract_form_state(soup)
    except Exception as exc:  # noqa: BLE001
        print(f"Aviso: no se pudo iniciar la sesion para otras categorias de ranking: {exc}", file=sys.stderr)
        return results

    for cat_name, cat_value in RANKING_CATEGORIES.items():
        try:
            form_data = dict(state)
            form_data["__EVENTTARGET"] = "_ctl0$MainContentPlaceHolderMaster$rankingsDropDownList"
            form_data["__EVENTARGUMENT"] = ""
            form_data[RANKINGS_DROPDOWN_FIELD] = str(cat_value)

            post_resp = session.post(url, data=form_data, headers=HEADERS, timeout=30)
            post_resp.raise_for_status()

            df = parse_ranking_html(post_resp.text, formula_sep)
            if not df.empty:
                results[cat_name] = df
            else:
                print(f"Aviso: la categoria '{cat_name}' devolvio una tabla vacia (temporada sin datos?)", file=sys.stderr)

            # El __VIEWSTATE cambia en cada postback: hay que refrescar el
            # estado con la respuesta actual antes de pedir la siguiente
            # categoria, o el siguiente postback sera invalido.
            soup = BeautifulSoup(post_resp.text, "html.parser")
            state = _extract_form_state(soup)
        except Exception as exc:  # noqa: BLE001
            print(f"Aviso: no se pudo obtener el ranking de '{cat_name}': {exc}", file=sys.stderr)
            continue

    return results


import re

# Columnas de "Total y Media" de la tabla de Equipos: la FEB muestra ambos
# numeros en la misma celda (con un botón para alternar cual se ve), pero
# nuestro scraper a veces arrastra basura del valor oculto. Como el Total
# siempre se lee bien, recalculamos la Media nosotros mismos.
STAT_TOTAL_MEDIA_COLS = {
    "MIN", "PT", "Rebotes_RO", "Rebotes_RD", "Rebotes_RT", "AS", "BR", "BP",
    "Tapones_TF", "Tapones_TC", "MT", "Faltas_FC", "Faltas_FR", "VA",
}

# Columnas de tiro: "aciertos/intentos porcentaje%" (ej. "42/89 47,2%")
SHOT_STAT_COLS = {"T2", "T3", "TC", "TL"}


def fix_total_media_cell(text: str, part: int) -> str:
    """Recalcula 'Total Media' de una celda a partir del Total (fiable) y
    los partidos jugados, ignorando el texto de Media que trae la pagina
    (que a veces sale corrompido, ej. '25 12,2005' en vez de '25 12,5').

    Usamos re.search (no re.match) por si hay algun caracter invisible o
    icono delante del numero que impida el match anclado al principio.
    """
    if not part:
        return text
    m = re.search(r"(-?\d+)", text)
    if not m:
        return text
    total = int(m.group(1))
    media = total / part
    media_str = str(int(media)) if media == int(media) else f"{media:.1f}".replace(".", ",")
    fixed = f"{total} {media_str}"
    if fixed != text.strip():
        print(f"  [fix_equipo_medias] '{text!r}' -> '{fixed}' (repr original para depurar: {text.encode('unicode_escape')})", file=sys.stderr)
    return fixed


def fix_equipo_medias(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica fix_total_media_cell a todas las columnas de Total/Media de
    la tabla de Equipos, usando la columna 'Part' (partidos jugados) de
    cada fila."""
    if "Part" not in df.columns:
        return df
    cols_to_fix = [c for c in df.columns if c in STAT_TOTAL_MEDIA_COLS]
    if not cols_to_fix:
        return df
    for idx, row in df.iterrows():
        try:
            part = int(re.match(r"^\s*(-?\d+)", str(row["Part"])).group(1))
        except (AttributeError, ValueError):
            continue
        for c in cols_to_fix:
            df.at[idx, c] = fix_total_media_cell(str(row[c]), part)
    return df


def split_stat_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Separa las columnas combinadas de la FEB en columnas independientes
    con un unico numero cada una:
      - Columnas de 'Total Media' (ej. '25 12,5') -> dos columnas:
        '<Stat>_Total' y '<Stat>_Media'.
      - Columnas de tiro 'aciertos/intentos pct%' (ej. '42/89 47,2%') ->
        tres columnas: '<Stat>_Anotados', '<Stat>_Intentados', '<Stat>_Pct'.
    """
    new_cols = {}
    for col in df.columns:
        if col in STAT_TOTAL_MEDIA_COLS:
            totals, medias = [], []
            for val in df[col].astype(str):
                m = re.match(r"^\s*(-?\d+)\s+(-?[\d,]+)\s*$", val)
                if m:
                    totals.append(m.group(1))
                    medias.append(m.group(2))
                else:
                    totals.append(val)
                    medias.append("")
            new_cols[f"{col}_Total"] = totals
            new_cols[f"{col}_Media"] = medias
        elif col in SHOT_STAT_COLS:
            anotados, intentados, pct = [], [], []
            for val in df[col].astype(str):
                m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s+([\d,]+)\s*%\s*$", val)
                if m:
                    anotados.append(m.group(1))
                    intentados.append(m.group(2))
                    pct.append(m.group(3))
                else:
                    anotados.append("")
                    intentados.append("")
                    pct.append(val)
            new_cols[f"{col}_Anotados"] = anotados
            new_cols[f"{col}_Intentados"] = intentados
            new_cols[f"{col}_Pct"] = pct
        else:
            new_cols[col] = df[col].astype(str).tolist()
    return pd.DataFrame(new_cols)


def fetch_resultados_y_clasificacion(url: str) -> tuple:
    """Descarga la pagina de Resultados de la FEB, que en realidad contiene
    VARIAS tablas: una por cada jornada (con los partidos y su marcador) y,
    si la temporada ya tiene partidos jugados, una tabla de Clasificacion
    real (PJ, PG, PP, puntos a favor/en contra, % y racha actual).

    Devuelve (resultados_df, clasificacion_df). Cualquiera de las dos puede
    venir vacia si esa parte no esta disponible todavia.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    resultado_rows = []
    clasificacion_df = pd.DataFrame()

    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True)
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]

        # Tabla de partidos: cabecera "Equipos, Resultado, Fecha, Hora"
        if "Equipos" in header_cells and "Resultado" in header_cells:
            # La jornada suele estar en un titulo justo antes de la tabla
            heading = table.find_previous(["h1", "h2", "h3", "h4", "span", "div"])
            jornada_label = heading.get_text(strip=True) if heading else ""
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all("td")]
                if len(cells) < 3:
                    continue
                equipos_txt, resultado_txt = cells[0], cells[1]
                fecha_txt = cells[2] if len(cells) > 2 else ""
                hora_txt = cells[3] if len(cells) > 3 else ""
                if " - " in equipos_txt:
                    local, visitante = [t.strip() for t in equipos_txt.split(" - ", 1)]
                else:
                    local, visitante = equipos_txt, ""
                jugado = "*" not in resultado_txt and "-" in resultado_txt
                pts_local, pts_visitante = "", ""
                if jugado:
                    partes = resultado_txt.split("-")
                    if len(partes) == 2:
                        pts_local, pts_visitante = partes[0].strip(), partes[1].strip()
                resultado_rows.append({
                    "Jornada": jornada_label,
                    "Local": local,
                    "Visitante": visitante,
                    "Resultado": resultado_txt,
                    "Pts_Local": pts_local,
                    "Pts_Visitante": pts_visitante,
                    "Jugado": "Si" if jugado else "No",
                    "Fecha": fecha_txt,
                    "Hora": hora_txt,
                })

        # Tabla de clasificacion real: cabecera "Po, Equipo, PJ, PG, PP..."
        elif "Equipo" in header_cells and "PJ" in header_cells and "PG" in header_cells:
            data = []
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all("td")]
                if len(cells) == len(header_cells):
                    data.append(cells)
            if data:
                clasificacion_df = pd.DataFrame(data, columns=header_cells)

    resultados_df = pd.DataFrame(resultado_rows)
    return resultados_df, clasificacion_df


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
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "AUTOMATIC"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
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
    aplica un formato profesional (cabecera, colores alternos, fotos).

    Usamos RAW salvo que la pestaña tenga formulas =IMAGE(...) (has_photos),
    porque con USER_ENTERED Sheets intenta reinterpretar el texto como si
    lo hubiera tecleado una persona, y puede arrastrar un formato numerico
    residual de una celda que antes tuvo otro tipo de dato."""
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=tab_name, rows=max(len(df) + 10, 20), cols=max(len(df.columns) + 2, 10)
        )

    values = [list(df.columns.astype(str))] + df.fillna("").astype(str).values.tolist()
    value_input_option = "USER_ENTERED" if has_photos else "RAW"
    ws.update(values, value_input_option=value_input_option)

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


# ----------------------------- ESCUDOS DE EQUIPO ---------------------------

def fetch_team_ids(url: str) -> dict:
    """Devuelve un dict {nombre_equipo: id_equipo} leyendo los enlaces
    "Equipo.aspx?i=ID" que aparecen en la pagina de estadisticas. Ese ID es
    el que permite construir la URL del escudo del equipo:
    https://imagenes.feb.es/Imagen.aspx?i=ID&ti=1
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"Aviso: no se pudieron obtener los IDs de equipo para los escudos: {exc}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    mapping = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"Equipo\.aspx\?i=(\d+)", a["href"])
        if m:
            name = a.get_text(strip=True)
            if name:
                mapping[name] = m.group(1)
    return mapping


def add_escudos(df: pd.DataFrame, team_ids: dict, formula_sep: str = ";") -> pd.DataFrame:
    """Anade al DataFrame de Equipos una columna 'Escudo' (miniatura via
    IMAGE()) y 'EscudoURL' (texto plano, para apps externas), usando el
    mapeo nombre de equipo -> id. Si no se encuentra el id de un equipo,
    esas celdas quedan vacias (nunca se inventa una URL)."""
    if "Equipo" not in df.columns or not team_ids:
        return df

    escudo_formula, escudo_url = [], []
    for name in df["Equipo"]:
        team_id = team_ids.get(str(name).strip())
        if team_id:
            url = f"https://imagenes.feb.es/Imagen.aspx?i={team_id}&ti=1"
            escudo_formula.append(f'=IMAGE("{url}"{formula_sep} 4{formula_sep} 40{formula_sep} 40)')
            escudo_url.append(url)
        else:
            escudo_formula.append("")
            escudo_url.append("")

    df.insert(1, "Escudo", escudo_formula)
    df["EscudoURL"] = escudo_url
    return df


# ----------------------------- MAIN ----------------------------------------


def main():
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)
    formula_sep = get_formula_separator(sh)

    resumen = []

    # 1) Estadisticas por equipo (+ escudo de cada equipo)
    equipo_tables = fetch_tables(ESTADISTICAS_URL)
    team_ids = fetch_team_ids(ESTADISTICAS_URL)
    if equipo_tables:
        for i, df in enumerate(equipo_tables):
            df = clean_dataframe(df)
            if i == 0:
                df = fix_equipo_medias(df)
                df = split_stat_columns(df)
                df = add_escudos(df, team_ids, formula_sep=formula_sep)
            tab = "Equipos" if i == 0 else f"Equipos_{i}"
            write_dataframe(sh, tab, df, has_photos=(i == 0))
            resumen.append(f"{tab}: {len(df)} filas")
    else:
        resumen.append(
            "Equipos: sin tablas (probablemente la temporada aun no tiene partidos)"
        )

    # 2) Rankings por jugadora (con foto de cada jugadora)
    ranking_df = fetch_ranking_with_photos(RANKINGS_URL, formula_sep=formula_sep)
    if not ranking_df.empty:
        write_dataframe(sh, "Jugadoras", ranking_df, has_photos=True)
        resumen.append(f"Jugadoras: {len(ranking_df)} filas (con foto)")
    else:
        resumen.append("Jugadoras: sin tablas todavia")

    # 2b) Resto de categorias de ranking (rebotes, asistencias, robos,
    # tapones, valoracion), cada una en su propia pestana "Jugadoras_<Cat>"
    otras_categorias = fetch_all_ranking_categories(RANKINGS_URL, formula_sep=formula_sep)
    for cat_name, df in otras_categorias.items():
        tab = f"Jugadoras_{cat_name}"
        write_dataframe(sh, tab, df, has_photos=True)
        resumen.append(f"{tab}: {len(df)} filas (con foto)")
    categorias_faltantes = set(RANKING_CATEGORIES) - set(otras_categorias)
    if categorias_faltantes:
        resumen.append(f"Categorias no disponibles esta vez: {', '.join(sorted(categorias_faltantes))}")

    # 3) Resultados por jornada + Clasificacion real (viven en la misma pagina)
    resultados_df, clasificacion_df = fetch_resultados_y_clasificacion(RESULTADOS_URL)
    if not resultados_df.empty:
        write_dataframe(sh, "Resultados", resultados_df)
        resumen.append(f"Resultados: {len(resultados_df)} filas")
    else:
        resumen.append("Resultados: sin partidos todavia")

    if not clasificacion_df.empty:
        write_dataframe(sh, "Clasificacion", clasificacion_df)
        resumen.append(f"Clasificacion: {len(clasificacion_df)} filas")
    else:
        resumen.append("Clasificacion: no disponible todavia (necesita al menos una jornada jugada)")

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
