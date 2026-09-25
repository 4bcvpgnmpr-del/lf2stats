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
                        (el script recorre solo los grupos "Liga Regular A/B"
                        para equipos y rankings, y TODAS las fases y jornadas
                        para los resultados)
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
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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
# Descargas en paralelo: la FEB tiene cientos de paginas y de una en una tarda horas.
HILOS = int(os.environ.get("FEB_HILOS", "4"))
# Tiempo maximo (minutos) que se dedica a bajar estadisticas de partidos.
# Lo que no entre se bajara en la siguiente ejecucion.
MINUTOS_MAX_PARTIDOS = int(os.environ.get("FEB_MINUTOS_PARTIDOS", "45"))
INICIO_EJECUCION = time.time()


def _en_paralelo(items: list, funcion, hilos: int = None) -> list:
    """Aplica 'funcion' a cada elemento con varios hilos, conservando el orden.
    Si un elemento falla, devuelve None en su posicion."""
    if not items:
        return []
    hilos = max(1, hilos or HILOS)
    if hilos == 1:
        resultados = []
        for it in items:
            try:
                resultados.append(funcion(it))
            except Exception as exc:  # noqa: BLE001
                print(f"  Aviso: fallo procesando {it}: {exc}", file=sys.stderr)
                resultados.append(None)
        return resultados

    def _seguro(it):
        try:
            return funcion(it)
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: fallo procesando {it}: {exc}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        return list(pool.map(_seguro, items))

# ----------------------------- RED ----------------------------------------


def _request_con_reintentos(session, method, url, **kwargs):
    """Hace una peticion HTTP con hasta MAX_RETRIES intentos."""
    ultimo_error = None
    for intento in range(1, MAX_RETRIES + 1):
        try:
            cabeceras = dict(HEADERS)
            cabeceras.update(kwargs.pop("headers_extra", {}) or {})
            resp = session.request(method, url, headers=cabeceras, timeout=30, **kwargs)
            resp.raise_for_status()
            time.sleep(PAUSA_ENTRE_PETICIONES / max(1, HILOS))
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
    headers = (headers + [f"col_{i}" for i in range(len(headers), ncols)])[:ncols]
    df = pd.DataFrame(data, columns=headers).fillna("")

    cols_basura = [
        c for c in df.columns
        if re.match(r"^col_\d+$", str(c)) and (df[c].astype(str).str.strip() == "").all()
    ]
    if cols_basura:
        df = df.drop(columns=cols_basura)
    return df


# ----------------------------- PAGINACION ---------------------------------

PAGER_RE = re.compile(r"__doPostBack\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)")
LABELS_SIGUIENTE = {"...", "…", ">", ">>", "»", "siguiente"}


def _numero_pagina(label: str, argumento: str):
    if label.isdigit():
        return int(label)
    m = re.search(r"(\d+)", argumento or "")
    return int(m.group(1)) if m else None


def _enlaces_paginacion(soup: BeautifulSoup) -> list:
    """Busca los enlaces de paginacion dentro de la tabla de ranking."""
    tabla = _tabla_ranking(soup)
    if tabla is None:
        return []
    zonas = [tabla]
    if tabla.parent is not None:
        zonas.append(tabla.parent)

    enlaces, claves = [], set()
    for zona in zonas:
        for a in zona.find_all("a", href=True):
            label = a.get_text(strip=True)
            if not (label.isdigit() or label.lower() in LABELS_SIGUIENTE):
                continue
            href = a["href"]
            m = PAGER_RE.search(href)
            if m:
                target, arg = m.group(1), m.group(2)
                if "rankingsDropDownList" in target:
                    continue
                clave = f"{target}|{arg}"
                if clave in claves:
                    continue
                claves.add(clave)
                enlaces.append({"label": label, "tipo": "postback", "target": target,
                                "arg": arg, "clave": clave,
                                "pagina": _numero_pagina(label, arg)})
            elif label.isdigit() and re.search(r"(pag|page|p=)", href, re.I):
                if href in claves:
                    continue
                claves.add(href)
                enlaces.append({"label": label, "tipo": "get", "href": href,
                                "clave": href, "pagina": int(label)})
        if enlaces:
            break
    return enlaces


def _pagina_actual(soup: BeautifulSoup):
    """La pagina actual suele mostrarse como <span>N</span> sin enlace."""
    tabla = _tabla_ranking(soup)
    if tabla is None:
        return None
    for span in tabla.find_all("span"):
        t = span.get_text(strip=True)
        if t.isdigit() and span.find_parent("a") is None:
            if any(td.get("colspan") for td in span.find_parents("td")) or span.find_parent("table") is not tabla:
                return int(t)
    return None


def _clave_jugadora(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in ("Jugador", "Equipo") if c in df.columns] or list(df.columns)
    return df[cols].astype(str).agg("|".join, axis=1)


def _descargar_todas_las_paginas(session, url: str, html_inicial: str,
                                 formula_sep: str, etiqueta: str) -> pd.DataFrame:
    """Lee la pagina actual y recorre todas las siguientes del ranking."""
    frames = []
    vistos = set()
    visitadas = {1}
    claves_usadas = set()
    html = html_inicial
    paginas_leidas = 0

    for n in range(MAX_PAGINAS):
        df = parse_ranking_html(html, formula_sep)
        nuevas = 0
        if not df.empty:
            for k in _clave_jugadora(df):
                if k not in vistos:
                    vistos.add(k)
                    nuevas += 1
            frames.append(df)
        paginas_leidas += 1

        if n > 0 and nuevas == 0:
            print(f"  [paginacion] {etiqueta}: pagina sin jugadoras nuevas, fin.", file=sys.stderr)
            break

        soup = BeautifulSoup(html, "html.parser")
        actual = _pagina_actual(soup)
        if actual:
            visitadas.add(actual)

        enlaces = _enlaces_paginacion(soup)
        if n == 0:
            print(f"  [paginacion] {etiqueta}: {len(enlaces)} enlaces de pagina detectados "
                  f"({', '.join(e['label'] for e in enlaces) or 'ninguno'})", file=sys.stderr)

        con_numero = [e for e in enlaces if e["pagina"] is not None
                      and e["pagina"] not in visitadas and e["clave"] not in claves_usadas]
        sin_numero = [e for e in enlaces if e["pagina"] is None and e["clave"] not in claves_usadas]

        if con_numero:
            siguiente = min(con_numero, key=lambda e: e["pagina"])
        elif sin_numero:
            siguiente = sin_numero[-1]   # el "..." hacia delante suele ser el ultimo
        else:
            break

        try:
            if siguiente["tipo"] == "postback":
                datos = _extract_form_state(soup)
                datos["__EVENTTARGET"] = siguiente["target"]
                datos["__EVENTARGUMENT"] = siguiente["arg"]
                resp = _request_con_reintentos(session, "POST", url, data=datos)
            else:
                resp = _request_con_reintentos(session, "GET", urljoin(url, siguiente["href"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  [paginacion] {etiqueta}: fallo al pedir la pagina "
                  f"'{siguiente['label']}': {exc}. Me quedo con lo descargado.", file=sys.stderr)
            break

        claves_usadas.add(siguiente["clave"])
        if siguiente["pagina"] is not None:
            visitadas.add(siguiente["pagina"])
        html = resp.text

    if not frames:
        return pd.DataFrame()
    df_total = pd.concat(frames, ignore_index=True).fillna("")
    df_total = df_total.loc[~_clave_jugadora(df_total).duplicated()].reset_index(drop=True)
    print(f"  [paginacion] {etiqueta}: {paginas_leidas} pagina(s), {len(df_total)} jugadoras.", file=sys.stderr)
    return df_total


def fetch_ranking_with_photos(url: str, formula_sep: str = ";") -> pd.DataFrame:
    """Ranking de Puntos (categoria por defecto), todas las paginas,
    de TODOS los grupos de la liga regular."""
    return _ranking_por_grupos(url, formula_sep, "Puntos")


# ----------------------------- NOMBRES DE EQUIPO UNIFICADOS ----------------
# La FEB no escribe siempre igual el nombre del equipo: en una pagina pone
# "MIRALVALLE" y en otra "MIRALVALLE PLASENCIA". Eso hacia que el mismo equipo
# saliera duplicado y que sus jugadoras se quedaran sin grupo. Aqui se toma
# como nombre bueno el de la pestaña Equipos y se traduce todo lo demas.

PALABRAS_IGNORADAS = {"CB", "C.B.", "CB.", "BC", "CD", "CLUB", "BALONCESTO", "BASKET",
                      "FEMENINO", "FEM", "SAD", "S.A.D.", "DE", "DEL", "LA", "EL", "LAS", "LOS"}


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto))
                   if unicodedata.category(c) != "Mn")


def normalizar_equipo(nombre: str) -> str:
    """Nombre simplificado para poder comparar: sin tildes, sin puntuacion y
    sin palabras de relleno (CB, CLUB, BALONCESTO...)."""
    txt = _sin_tildes(nombre or "").upper()
    txt = re.sub(r"[^A-Z0-9 ]+", " ", txt)
    palabras = [p for p in txt.split() if p and p not in PALABRAS_IGNORADAS]
    return " ".join(palabras)


def construir_canonicos(equipos_df: pd.DataFrame) -> dict:
    """{nombre normalizado: nombre bueno} a partir de la pestaña Equipos."""
    if equipos_df.empty or "Equipo" not in equipos_df.columns:
        return {}
    canonicos = {}
    for nombre in equipos_df["Equipo"].astype(str):
        n = normalizar_equipo(nombre)
        if n:
            canonicos.setdefault(n, nombre.strip())
    return canonicos


def canonizar_equipo(nombre: str, canonicos: dict) -> str:
    """Traduce un nombre de equipo al nombre bueno. Si no esta claro, lo deja igual."""
    original = str(nombre or "").strip()
    n = normalizar_equipo(original)
    if not n or not canonicos:
        return original
    if n in canonicos:
        return canonicos[n]
    # uno contiene al otro ("MIRALVALLE" dentro de "MIRALVALLE PLASENCIA")
    candidatos = {c for nc, c in canonicos.items() if nc.startswith(n) or n.startswith(nc)}
    if len(candidatos) == 1:
        return candidatos.pop()
    candidatos = {c for nc, c in canonicos.items() if n in nc or nc in n}
    if len(candidatos) == 1:
        return candidatos.pop()
    # palabras en comun: solo si hay un unico ganador claro
    toks = set(n.split())
    puntuaciones = sorted(((len(toks & set(nc.split())), c) for nc, c in canonicos.items()), reverse=True)
    if puntuaciones and puntuaciones[0][0] > 0:
        if len(puntuaciones) == 1 or puntuaciones[0][0] > puntuaciones[1][0]:
            return puntuaciones[0][1]
    return original


def aplicar_canonicos(df: pd.DataFrame, canonicos: dict, columnas=("Equipo",)) -> tuple:
    """Cambia los nombres de equipo por los buenos. Devuelve (df, nombres no reconocidos)."""
    if df is None or df.empty or not canonicos:
        return df, set()
    sin_reconocer = set()
    for col in columnas:
        if col not in df.columns:
            continue
        nuevos = []
        for valor in df[col].astype(str):
            bueno = canonizar_equipo(valor, canonicos)
            if normalizar_equipo(bueno) not in canonicos and valor.strip():
                sin_reconocer.add(valor.strip())
            nuevos.append(bueno)
        df[col] = nuevos
    return df, sin_reconocer


# ----------------------------- FASES Y GRUPOS ------------------------------
# La LF2 se juega en dos grupos (Liga Regular "A" y "B") y despues hay
# eliminatorias. La web de la FEB muestra por defecto la ULTIMA fase (las
# finales), asi que solo salian los equipos que jugaron las finales.
# Estas funciones buscan el desplegable de fases y recorren cada grupo.

def _opciones(sel) -> list:
    return [(opt.get("value", ""), opt.get_text(strip=True)) for opt in sel.find_all("option")]


def _valor_seleccionado(sel) -> str:
    opt = sel.find("option", selected=True) or sel.find("option")
    return opt.get("value", "") if opt else ""


def _target_postback(sel) -> str:
    """Nombre del control que la web usa para el postback de un desplegable."""
    m = re.search(r"__doPostBack\(\s*'([^']+)'", sel.get("onchange") or "")
    if m:
        return m.group(1)
    return (sel.get("name") or "").replace(":", "$")


def _url_formulario(soup: BeautifulSoup, url_base: str) -> str:
    """URL a la que hay que enviar el formulario (la de 'action')."""
    form = soup.find("form")
    action = form.get("action") if form else None
    return urljoin(url_base, action) if action else url_base


def _es_liga_regular(texto: str) -> bool:
    return bool(re.search(r"liga\s+regular", texto, re.I))


def _nombre_grupo(texto: str) -> str:
    """'Liga Regular "A"' -> 'A'. Si no es un grupo de liga regular -> ''."""
    m = re.search(r"liga\s+regular\W*([A-Za-z0-9]+)", texto, re.I)
    return m.group(1).upper() if m else ""


def _desplegable_fases(soup: BeautifulSoup):
    """El <select> de fases/grupos (o None si la pagina no tiene)."""
    candidatos = []
    for sel in soup.find_all("select"):
        nombre = (sel.get("name") or "").lower()
        if not nombre or any(p in nombre for p in ("temporada", "jornada", "ranking")):
            continue
        candidatos.append(sel)
    for sel in candidatos:
        if any(_es_liga_regular(t) for _, t in _opciones(sel)):
            return sel
    for sel in candidatos:
        if re.search(r"fase|grupo", sel.get("name") or "", re.I):
            return sel
    return None


def _desplegable_jornadas(soup: BeautifulSoup):
    for sel in soup.find_all("select"):
        nombre = (sel.get("name") or "").lower()
        textos = [t for _, t in _opciones(sel)]
        if "jornada" in nombre or (textos and all(t.lower().startswith("jornada") for t in textos)):
            return sel
    return None


def _cambiar_desplegable(session, post_url: str, soup: BeautifulSoup,
                         campo: str, target: str, valor: str) -> str:
    """Simula elegir 'valor' en un desplegable ASP.NET. Devuelve el HTML nuevo."""
    datos = _extract_form_state(soup)
    datos["__EVENTTARGET"] = target
    datos["__EVENTARGUMENT"] = ""
    datos[campo] = valor
    return _request_con_reintentos(session, "POST", post_url, data=datos).text


def _paginas_por_fase(url: str, solo_liga_regular: bool = True):
    """Recorre las fases de una pagina. Por cada una devuelve:
    (texto_fase, grupo, session, html, post_url).
    Si la pagina no tiene desplegable de fases, devuelve solo la pagina
    tal cual (fase y grupo vacios), igual que antes."""
    session = requests.Session()
    resp = _request_con_reintentos(session, "GET", url)
    soup = BeautifulSoup(resp.text, "html.parser")
    sel = _desplegable_fases(soup)
    if sel is None:
        print(f"  [fases] {url}: sin desplegable de fases, uso la pagina por defecto.", file=sys.stderr)
        yield "", "", session, resp.text, _url_formulario(soup, resp.url)
        return

    opciones = _opciones(sel)
    print(f"  [fases] {url}: fases detectadas -> {', '.join(t for _, t in opciones)}", file=sys.stderr)
    if solo_liga_regular:
        elegidas = [o for o in opciones if _es_liga_regular(o[1])]
        if not elegidas:
            print("  [fases] no hay 'Liga Regular' en el desplegable; uso la fase por defecto.", file=sys.stderr)
            yield "", "", session, resp.text, _url_formulario(soup, resp.url)
            return
    else:
        elegidas = list(reversed(opciones))   # de la primera fase a la ultima

    campo, target = sel.get("name"), _target_postback(sel)
    for valor, texto in elegidas:
        try:
            # Sesion nueva por fase: asi una fase no interfiere con otra
            s2 = requests.Session()
            r2 = _request_con_reintentos(s2, "GET", url)
            soup2 = BeautifulSoup(r2.text, "html.parser")
            post_url = _url_formulario(soup2, r2.url)
            sel2 = _desplegable_fases(soup2)
            if sel2 is not None and _valor_seleccionado(sel2) == valor:
                html = r2.text
            else:
                html = _cambiar_desplegable(s2, post_url, soup2, campo, target, valor)
            yield texto, _nombre_grupo(texto), s2, html, post_url
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: no se pudo abrir la fase '{texto}': {exc}", file=sys.stderr)
            continue


def quitar_jugadoras_repetidas(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por jugadora y equipo (tras unificar los nombres puede haber repetidas)."""
    if df is None or df.empty or "Jugador" not in df.columns:
        return df
    return df.loc[~_clave_jugadora(df).duplicated()].reset_index(drop=True)


def _reordenar_ranking(df: pd.DataFrame) -> pd.DataFrame:
    """Tras juntar los dos grupos: ordena por la media y renumera la posicion."""
    col_media = next((c for c in ("Media", "Med", "Medias") if c in df.columns), None)
    if not col_media:
        return df

    def _num(v):
        try:
            return float(str(v).replace(".", "").replace(",", "."))
        except ValueError:
            return float("-inf")

    df = df.assign(_orden=df[col_media].map(_num))
    df = df.sort_values("_orden", ascending=False, kind="stable").drop(columns="_orden")
    df = df.reset_index(drop=True)
    primera = df.columns[0]
    valores = df[primera].astype(str).str.strip()
    if primera not in ("Foto", "Jugador") and (valores != "").all() and valores.str.isdigit().all():
        df[primera] = [str(i) for i in range(1, len(df) + 1)]
    return df


def _ranking_por_grupos(url: str, formula_sep: str, etiqueta: str, cat_value=None) -> pd.DataFrame:
    """Ranking de una categoria juntando Grupo A + Grupo B (todas las paginas)."""
    frames = []
    for fase, grupo, session, html, post_url in _paginas_por_fase(url):
        nombre = f"{etiqueta} Grupo {grupo}" if grupo else etiqueta
        try:
            if cat_value is not None:
                soup = BeautifulSoup(html, "html.parser")
                html = _cambiar_desplegable(session, post_url, soup, RANKINGS_DROPDOWN_FIELD,
                                            RANKINGS_DROPDOWN_TARGET, str(cat_value))
            df = _descargar_todas_las_paginas(session, post_url, html, formula_sep, nombre)
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: {nombre}: {exc}", file=sys.stderr)
            continue
        if df.empty:
            continue
        if grupo:
            df["Grupo"] = grupo
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df_total = pd.concat(frames, ignore_index=True).fillna("")
    df_total = df_total.loc[~_clave_jugadora(df_total).duplicated()].reset_index(drop=True)
    if len(frames) > 1:
        df_total = _reordenar_ranking(df_total)
    return df_total


# ----------------------------- OTRAS CATEGORIAS DE RANKING -----------------
# El desplegable de categoria es un postback ASP.NET (no un parametro de URL).
# Indices confirmados capturando la peticion real del navegador:
RANKING_CATEGORIES = {
    "Rebotes_Totales": 1,
    "Asistencias": 4,
    "Robos": 5,
    "Tapones_Favor": 7,
    "Tapones_Contra": 8,
    "Mates": 9,
    "Faltas_Recibidas": 10,
    "Faltas_Cometidas": 11,
    "Valoracion": 12,
    "Minutos_Jugados": 13,
    "Pct_Tiros_2": 14,
    "Pct_Tiros_3": 15,
    "Pct_Tiros_Libres": 16,
}

RANKINGS_DROPDOWN_FIELD = "_ctl0:MainContentPlaceHolderMaster:rankingsDropDownList"
RANKINGS_DROPDOWN_TARGET = "_ctl0$MainContentPlaceHolderMaster$rankingsDropDownList"


def _extract_form_state(soup: BeautifulSoup) -> dict:
    """Campos de un formulario ASP.NET (inputs ocultos + selects)."""
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
    """Descarga el resto de categorias (todas sus paginas y todos los grupos).
    Si una categoria falla, se omite sin tumbar el resto."""
    nombres = list(RANKING_CATEGORIES.keys())
    dfs = _en_paralelo(nombres, lambda nombre: _ranking_por_grupos(
        url, formula_sep, nombre, RANKING_CATEGORIES[nombre]))
    results = {}
    for nombre, df in zip(nombres, dfs):
        if df is None:
            print(f"Aviso: no se pudo obtener el ranking de '{nombre}'", file=sys.stderr)
        elif df.empty:
            print(f"Aviso: la categoria '{nombre}' devolvio una tabla vacia", file=sys.stderr)
        else:
            results[nombre] = df
    return results


# ----------------------------- EQUIPOS -------------------------------------

STAT_TOTAL_MEDIA_COLS = {
    "MIN", "PT", "Rebotes_RO", "Rebotes_RD", "Rebotes_RT", "AS", "BR", "BP",
    "Tapones_TF", "Tapones_TC", "MT", "Faltas_FC", "Faltas_FR", "VA",
}

SHOT_STAT_COLS = {"T2", "T3", "TC", "TL"}


def fix_total_media_cell(text: str, part: int) -> str:
    """Recalcula 'Total Media' a partir del Total y los partidos jugados."""
    if not part:
        return text
    m = re.search(r"(-?\d+)", text)
    if not m:
        return text
    total = int(m.group(1))
    media = total / part
    media_str = str(int(media)) if media == int(media) else f"{media:.1f}".replace(".", ",")
    return f"{total} {media_str}"


def fix_equipo_medias(df: pd.DataFrame) -> pd.DataFrame:
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
    """Separa 'Total Media' y 'anotados/intentados pct%' en columnas sueltas."""
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


def _parse_resultados_html(html: str, fase: str = "", grupo: str = "",
                           jornada: str = "") -> tuple:
    """Lee UNA pagina de resultados: (filas_de_partidos, clasificacion, clasificacion_final)."""
    soup = BeautifulSoup(html, "html.parser")
    resultado_rows = []
    clasificacion_df = pd.DataFrame()
    final_df = pd.DataFrame()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]

        if "Equipos" in header_cells and "Resultado" in header_cells:
            if jornada:
                jornada_label = jornada
            else:
                heading = table.find_previous(["h1", "h2", "h3", "h4", "span", "div"])
                jornada_label = heading.get_text(strip=True) if heading else ""
            for tr in rows[1:]:
                celdas_tag = tr.find_all("td")
                cells = [_texto_celda(c) for c in celdas_tag]
                if len(cells) < 3:
                    continue
                enlace = tr.find("a", href=re.compile(r"Partido\.aspx\?p=\d+", re.I))
                m_id = re.search(r"p=(\d+)", enlace["href"]) if enlace else None
                partido_id = f"p{m_id.group(1)}" if m_id else ""
                equipos_txt, resultado_txt = cells[0], cells[1]
                fecha_txt = cells[2] if len(cells) > 2 else ""
                hora_txt = cells[3] if len(cells) > 3 else ""
                # Lo mas fiable: los dos enlaces a los equipos de esa fila
                equipos_enlaces = [a.get_text(" ", strip=True) for a in
                                   celdas_tag[0].find_all("a", href=re.compile(r"Equipo\.aspx", re.I))] \
                    if celdas_tag else []
                if len(equipos_enlaces) >= 2:
                    local, visitante = equipos_enlaces[0].strip(), equipos_enlaces[1].strip()
                elif " - " in equipos_txt:
                    local, visitante = [t.strip() for t in equipos_txt.split(" - ", 1)]
                else:
                    local, visitante = equipos_txt.strip(), ""
                jugado = "*" not in resultado_txt and "-" in resultado_txt
                pts_local, pts_visitante = "", ""
                if jugado:
                    partes = resultado_txt.split("-")
                    if len(partes) == 2:
                        pts_local, pts_visitante = partes[0].strip(), partes[1].strip()
                resultado_rows.append({
                    "Fase": fase,
                    "Grupo": grupo,
                    "Jornada": jornada_label,
                    "Local": local,
                    "Visitante": visitante,
                    "Resultado": resultado_txt,
                    "Pts_Local": pts_local,
                    "Pts_Visitante": pts_visitante,
                    "Jugado": "Si" if jugado else "No",
                    "Fecha": fecha_txt,
                    "Hora": hora_txt,
                    "PartidoID": partido_id,
                })

        elif "Equipo" in header_cells and "PJ" in header_cells and "PG" in header_cells:
            data = []
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all("td")]
                if len(cells) == len(header_cells):
                    data.append(cells)
            if data:
                clasificacion_df = pd.DataFrame(data, columns=header_cells)

        elif "Equipo" in header_cells and "Po" in header_cells and "PJ" not in header_cells:
            data = []
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all("td")]
                if len(cells) == len(header_cells):
                    data.append(cells)
            if data:
                final_df = pd.DataFrame(data, columns=header_cells)

    return resultado_rows, clasificacion_df, final_df


def _clave_fecha(fila: dict):
    try:
        f = datetime.strptime(f"{fila.get('Fecha', '')} {fila.get('Hora', '') or '00:00'}".strip(), "%d/%m/%Y %H:%M")
        return (0, f)
    except ValueError:
        return (1, datetime.max)


def fetch_resultados_y_clasificacion(url: str) -> tuple:
    """Resultados de TODAS las fases y jornadas + Clasificacion de cada grupo
    + Clasificacion final (si la FEB la publica)."""
    todas = []
    clasificaciones = []
    clasif_final = pd.DataFrame()

    for fase, grupo, session, html, post_url in _paginas_por_fase(url, solo_liga_regular=False):
        try:
            soup = BeautifulSoup(html, "html.parser")
            filas, clasif, final = _parse_resultados_html(html, fase, grupo)
            if not clasif.empty and (grupo or not fase):
                if grupo:
                    clasif.insert(0, "Grupo", grupo)
                clasificaciones.append(clasif)
            if len(final) > len(clasif_final):
                clasif_final = final

            sel_j = _desplegable_jornadas(soup)
            if sel_j is None:
                todas.extend(filas)
                continue
            actual = _valor_seleccionado(sel_j)
            campo, target = sel_j.get("name"), _target_postback(sel_j)

            def _una_jornada(opcion):
                valor, texto = opcion
                h = html if valor == actual else _cambiar_desplegable(
                    session, post_url, soup, campo, target, valor)
                return _parse_resultados_html(h, fase, grupo, texto)[0]

            for filas_jornada in _en_paralelo(_opciones(sel_j), _una_jornada):
                if filas_jornada:
                    todas.extend(filas_jornada)
            print(f"  [resultados] {fase or 'fase por defecto'}: {len(_opciones(sel_j))} jornadas leidas", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: no se pudieron leer los resultados de '{fase}': {exc}", file=sys.stderr)

    resultados_df = pd.DataFrame(todas)
    if not resultados_df.empty:
        resultados_df = resultados_df.drop_duplicates(
            subset=["Fase", "Jornada", "Local", "Visitante"]).reset_index(drop=True)
        # PartidoID: primera columna, para que la app pueda buscar por ella
        resultados_df = resultados_df[["PartidoID"] + [c for c in resultados_df.columns if c != "PartidoID"]]
        orden = sorted(range(len(resultados_df)),
                       key=lambda i: _clave_fecha(resultados_df.iloc[i].to_dict()))
        resultados_df = resultados_df.iloc[orden].reset_index(drop=True)
        for col in ("Fase", "Grupo"):
            if (resultados_df[col].astype(str).str.strip() == "").all():
                resultados_df = resultados_df.drop(columns=col)

    clasificaciones.sort(key=lambda d: str(d["Grupo"].iloc[0]) if "Grupo" in d.columns else "")
    clasificacion_df = (pd.concat(clasificaciones, ignore_index=True).fillna("")
                        if clasificaciones else pd.DataFrame())
    return resultados_df, clasificacion_df, clasif_final


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Aplana cabeceras dobles y quita columnas vacias/sin nombre."""
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

    cols_basura = [
        c for c in df.columns
        if re.match(r"^col_\d+$", str(c)) and (df[c].astype(str).str.strip() == "").all()
    ]
    if cols_basura:
        df = df.drop(columns=cols_basura)
    return df


# ----------------------------- GOOGLE SHEETS -------------------------------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_gspread_client() -> gspread.Client:
    creds_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_raw:
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    else:
        creds_dict = json.loads(creds_raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


HEADER_BG = {"red": 0.10, "green": 0.16, "blue": 0.33}
HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}
BAND_COLOR = {"red": 0.93, "green": 0.95, "blue": 0.98}


def _existing_banding_id(sh: gspread.Spreadsheet, sheet_id: int):
    meta = sh.fetch_sheet_metadata()
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == sheet_id:
            bandings = s.get("bandedRanges", [])
            if bandings:
                return bandings[0]["bandedRangeId"]
    return None


def style_worksheet(sh: gspread.Spreadsheet, ws: gspread.Worksheet, n_rows: int, has_photos: bool = False):
    """Cabecera coloreada, fila superior fija, filas alternas y tamaño para fotos."""
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
        requests_list.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 50},
                "fields": "pixelSize",
            }
        })
        requests_list.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": max(n_rows + 1, 2)},
                "properties": {"pixelSize": 45},
                "fields": "pixelSize",
            }
        })

    sh.batch_update({"requests": requests_list})


RE_TIEMPO = re.compile(r"^\d{1,4}:[0-5]\d(:[0-5]\d)?$")


def _protege_minutos(celda):
    """Evita que Google interprete los minutos ('32:35') como una hora."""
    return "'" + celda if isinstance(celda, str) and RE_TIEMPO.match(celda.strip()) else celda


def write_dataframe(sh: gspread.Spreadsheet, tab_name: str, df: pd.DataFrame, has_photos: bool = False):
    """Escribe (sobrescribiendo) un DataFrame en una pestaña y le da formato.
    RAW salvo en pestañas con formulas =IMAGE(...)."""
    n_filas = len(df) + 1
    n_cols = max(len(df.columns), 1)
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
        # Ahora hay muchas mas jugadoras: ampliar la pestaña si se queda corta
        if ws.row_count < n_filas + 5 or ws.col_count < n_cols:
            ws.resize(rows=max(ws.row_count, n_filas + 5), cols=max(ws.col_count, n_cols))
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=max(n_filas + 10, 20), cols=max(n_cols + 2, 10))

    values = [list(df.columns.astype(str))] + df.fillna("").astype(str).values.tolist()
    value_input_option = "USER_ENTERED" if has_photos else "RAW"
    if value_input_option == "USER_ENTERED":
        # Google convierte "32:35" (minutos) en una hora y devuelve datos falsos
        # ("2:35" o "31:52:00"). Con un apostrofo delante se queda como texto.
        values = [values[0]] + [[_protege_minutos(c) for c in fila] for fila in values[1:]]
    ws.update(values, value_input_option=value_input_option)

    try:
        style_worksheet(sh, ws, n_rows=len(df), has_photos=has_photos)
    except Exception as exc:  # noqa: BLE001
        print(f"Aviso: no se pudo aplicar formato a '{tab_name}': {exc}", file=sys.stderr)


def write_log(sh: gspread.Spreadsheet, message: str):
    try:
        ws = sh.worksheet("Log")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Log", rows=1000, cols=2)
        ws.update([["Fecha (UTC)", "Evento"]])
    ws.append_row([datetime.now(timezone.utc).isoformat(timespec="seconds"), message])


# ----------------------------- ESCUDOS DE EQUIPO ---------------------------

def _team_ids_de_html(html: str) -> dict:
    """{nombre_equipo: id_equipo} a partir de los enlaces 'Equipo.aspx?i=ID'."""
    soup = BeautifulSoup(html, "html.parser")
    mapping = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"Equipo\.aspx\?i=(\d+)", a["href"])
        if m:
            name = a.get_text(strip=True)
            if name:
                mapping[name] = m.group(1)
    return mapping


def fetch_team_ids(url: str) -> dict:
    try:
        resp = _request_con_reintentos(requests.Session(), "GET", url)
    except Exception as exc:  # noqa: BLE001
        print(f"Aviso: no se pudieron obtener los IDs de equipo: {exc}", file=sys.stderr)
        return {}
    return _team_ids_de_html(resp.text)


def fetch_equipos_por_grupo(url: str) -> tuple:
    """Estadisticas de equipo de TODOS los grupos de la liga regular.
    Devuelve (df_equipos, ids_equipo, tablas_extra)."""
    frames, team_ids, extras = [], {}, []
    for fase, grupo, session, html, post_url in _paginas_por_fase(url):
        team_ids.update(_team_ids_de_html(html))
        try:
            tablas = pd.read_html(io.StringIO(html))
        except ValueError:
            tablas = []
        if not tablas:
            continue
        df = clean_dataframe(tablas[0])
        df = fix_equipo_medias(df)
        df = split_stat_columns(df)
        if grupo:
            df["Grupo"] = grupo
        frames.append(df)
        if not grupo:  # sin grupos: se conservan las tablas extra como antes
            extras = [clean_dataframe(t) for t in tablas[1:]]
    if not frames:
        return pd.DataFrame(), team_ids, extras
    return pd.concat(frames, ignore_index=True).fillna(""), team_ids, extras


def add_escudos(df: pd.DataFrame, team_ids: dict, formula_sep: str = ";") -> pd.DataFrame:
    """Columnas 'Escudo' (IMAGE) y 'EscudoURL'. Sin ID -> celda vacia."""
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


# ----------------------------- ESTADISTICAS DE CADA PARTIDO ----------------
# Cada partido jugado tiene su pagina "Partido.aspx?p=ID" con una tabla por
# equipo (una fila por jugadora + fila de totales). Para no tardar horas,
# solo se descargan los partidos que todavia NO estan en la pestaña.

PARTIDO_URL = f"{BASE_URL}/Partido.aspx?p={{id}}"
TAB_PARTIDOS = "Partidos_Estadisticas"
MAX_PARTIDOS_POR_EJECUCION = int(os.environ.get("FEB_MAX_PARTIDOS", "500"))

# Nombres claros para las columnas (la FEB repite "TC": tiros de campo y tapones en contra)
_RENOMBRAR_BOX = {"I": "Titular", "D": "Dorsal", "TF": "TAP_F", "FC": "FAL_C", "FR": "FAL_R",
                  "RO": "REB_O", "RD": "REB_D", "RT": "REB_T", "+/-": "MAS_MENOS"}


COLUMNAS_TIRO = ("T2", "T3", "TC", "TL")


def _texto_celda(celda) -> str:
    """Texto de una celda separando los trozos con espacio.
    Sin esto, la FEB pega los tiros y el porcentaje: '4/757,1%' en vez de '4/7 57,1%'."""
    return re.sub(r"\s+", " ", celda.get_text(" ", strip=True)).strip()


def _normaliza_tiro(texto: str) -> str:
    """'4/7 57,1%' -> '4/7'. El porcentaje lo calcula la app a partir de esos dos numeros."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", texto or "")
    return f"{m.group(1)}/{m.group(2)}" if m else (texto or "")


def _cabecera_box(tabla) -> list:
    """Cabecera de la tabla de un equipo (la fila que contiene 'Jugador')."""
    for tr in tabla.find_all("tr"):
        textos = [_texto_celda(c) for c in tr.find_all(["th", "td"])]
        if "Jugador" in textos and "PT" in textos:
            nombres, vistos_tc = [], 0
            for t in textos:
                if t == "TC":
                    vistos_tc += 1
                    nombres.append("TC" if vistos_tc == 1 else "TAP_C")
                else:
                    nombres.append(_RENOMBRAR_BOX.get(t, t))
            return nombres
    return []


def _nombre_equipo_de_tabla(tabla) -> str:
    h = tabla.find_previous(["h1", "h2", "h3", "h4"])
    return h.get_text(strip=True) if h else ""


_ETIQUETAS_PARTIDO = ("Fecha", "Árbitros", "Arbitros", "Pista")


def _dato_partido(soup: BeautifulSoup, etiqueta: str) -> str:
    """Texto que sigue a 'Fecha', 'Árbitros' o 'Pista' en la ficha del partido
    (hasta la siguiente etiqueta). Si no se encuentra, cadena vacia."""
    lineas = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    for i, linea in enumerate(lineas[:200]):
        if linea == etiqueta or linea.startswith(etiqueta + " "):
            partes = [linea[len(etiqueta):].strip()] if linea != etiqueta else []
            for sig in lineas[i + 1:i + 4]:
                if any(sig.startswith(e) for e in _ETIQUETAS_PARTIDO) or sig in ("%",) or len(sig) > 80:
                    break
                partes.append(sig)
                if etiqueta == "Pista" and len(partes) >= 2:
                    break
            return " ".join(p for p in partes if p)[:120]
    return ""


def parse_partido_html(html: str, partido_id: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    filas = []
    arbitros = _dato_partido(soup, "Árbitros")
    pista = _dato_partido(soup, "Pista")
    orden = 0
    for tabla in soup.find_all("table"):
        cab = _cabecera_box(tabla)
        if not cab:
            continue
        orden += 1
        equipo = _nombre_equipo_de_tabla(tabla)
        lado = "Local" if orden == 1 else ("Visitante" if orden == 2 else "")
        empezar = False
        for tr in tabla.find_all("tr"):
            celdas = [_texto_celda(c) for c in tr.find_all(["td", "th"])]
            if "Jugador" in celdas and "PT" in celdas:
                empezar = True
                continue
            if not empezar or len(celdas) != len(cab):
                continue
            fila = dict(zip(cab, celdas))
            if not fila.get("Jugador"):
                if not fila.get("PT"):
                    continue
                fila["Jugador"] = "TOTAL"
            fila["Titular"] = "Si" if "*" in fila.get("Titular", "") else ""
            for col in COLUMNAS_TIRO:
                if col in fila:
                    fila[col] = _normaliza_tiro(fila[col])
            filas.append({"PartidoID": partido_id, "Lado": lado, "Equipo": equipo, **fila,
                          "Arbitros": arbitros, "Pista": pista})
    return pd.DataFrame(filas)


def _ids_ya_guardados(sh) -> tuple:
    """(ids ya descargados, DataFrame con lo que ya habia en la pestaña)."""
    try:
        ws = sh.worksheet(TAB_PARTIDOS)
        valores = ws.get_all_values()
    except gspread.WorksheetNotFound:
        return set(), pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        print(f"Aviso: no se pudo leer {TAB_PARTIDOS}: {exc}", file=sys.stderr)
        return set(), pd.DataFrame()
    if len(valores) < 2:
        return set(), pd.DataFrame()
    df = pd.DataFrame(valores[1:], columns=valores[0])
    if "PartidoID" not in df.columns:
        return set(), pd.DataFrame()
    # Si lo guardado usa el formato antiguo (tiros con el % pegado), se rehace todo
    if "T2" in df.columns and df["T2"].astype(str).str.contains("%").any():
        print("  [partidos] datos antiguos con porcentajes mal leidos: se vuelven a descargar",
              file=sys.stderr)
        return set(), pd.DataFrame()
    return set(df["PartidoID"]), df


def fetch_estadisticas_partidos(sh, resultados_df: pd.DataFrame) -> tuple:
    """Devuelve (DataFrame completo, n_partidos_nuevos)."""
    ya, df_viejo = _ids_ya_guardados(sh)
    if resultados_df.empty or "PartidoID" not in resultados_df.columns:
        return df_viejo, 0
    jugados = resultados_df[(resultados_df["Jugado"] == "Si") & (resultados_df["PartidoID"] != "")]
    pendientes = [pid for pid in dict.fromkeys(jugados["PartidoID"]) if pid not in ya]
    pendientes = pendientes[:MAX_PARTIDOS_POR_EJECUCION]
    print(f"  [partidos] {len(ya)} ya guardados, {len(pendientes)} por descargar", file=sys.stderr)

    session = requests.Session()
    limite = INICIO_EJECUCION + MINUTOS_MAX_PARTIDOS * 60
    hechos = {"n": 0, "sin_tiempo": 0}

    def _un_partido(pid):
        if time.time() > limite:
            hechos["sin_tiempo"] += 1
            return None
        resp = _request_con_reintentos(session, "GET", PARTIDO_URL.format(id=pid.lstrip("p")))
        df = parse_partido_html(resp.text, pid)
        hechos["n"] += 1
        if hechos["n"] % 50 == 0:
            print(f"  [partidos] {hechos['n']}/{len(pendientes)}", file=sys.stderr)
        return df

    resultados_partidos = _en_paralelo(pendientes, _un_partido)
    nuevos = [df for df in resultados_partidos if df is not None and not df.empty]
    vacios = sum(1 for df in resultados_partidos if df is not None and df.empty)
    if vacios:
        print(f"  Aviso: {vacios} partidos sin tabla de estadisticas en la FEB", file=sys.stderr)
    if hechos["sin_tiempo"]:
        print(f"  [partidos] se alcanzo el limite de {MINUTOS_MAX_PARTIDOS} min: "
              f"quedan {hechos['sin_tiempo']} partidos para la proxima ejecucion", file=sys.stderr)

    if not nuevos:
        return df_viejo, 0
    total = pd.concat([df_viejo] + nuevos, ignore_index=True).fillna("")
    return total, len(nuevos)


# ----------------------------- MODO DETECTIVE (play by play) ---------------
# La pagina del partido carga el "Directo" (jugada a jugada) desde otra
# direccion, con JavaScript. Esta parte NO descarga datos nuevos: solo mira el
# HTML y los .js de la pagina y apunta en el registro las direcciones y claves
# que encuentra, para poder programar despues la descarga de verdad.

INVESTIGAR_PBP = os.environ.get("FEB_DETECTIVE", "0") != "0"   # ya no hace falta: se activa con FEB_DETECTIVE=1
PALABRAS_PBP = ("intrafeb", "livestats", "jugada", "playbyplay", "play_by_play",
                "pbp", "token", "api/", ".json", "directo")


def _fragmentos(texto: str, palabra: str, ancho: int = 120, maximo: int = 3) -> list:
    salidas = []
    for m in re.finditer(re.escape(palabra), texto, re.I):
        ini = max(0, m.start() - ancho // 2)
        salidas.append(re.sub(r"\s+", " ", texto[ini:ini + ancho]))
        if len(salidas) >= maximo:
            break
    return salidas


def investigar_play_by_play(partido_id: str) -> list:
    """Busca de donde saca la web el jugada a jugada. Devuelve lineas de informe."""
    informe = [f"=== DETECTIVE play-by-play (partido {partido_id}) ==="]
    session = requests.Session()
    try:
        resp = _request_con_reintentos(session, "GET", PARTIDO_URL.format(id=str(partido_id).lstrip("p")))
    except Exception as exc:  # noqa: BLE001
        return informe + [f"No se pudo abrir la pagina del partido: {exc}"]

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    urls = set(re.findall(r"https?://[^\s\"'<>()]+", html))
    interesantes = sorted(u for u in urls if any(p in u.lower() for p in ("intrafeb", "livestats", "api", ".json")))
    informe.append(f"Direcciones interesantes en el HTML: {len(interesantes)}")
    informe += [f"  URL: {u[:200]}" for u in interesantes[:15]]

    for palabra in PALABRAS_PBP:
        for frag in _fragmentos(html, palabra):
            informe.append(f"  HTML[{palabra}]: {frag[:200]}")

    scripts = []
    for sc in soup.find_all("script", src=True):
        scripts.append(urljoin(resp.url, sc["src"]))
    informe.append(f"Ficheros .js de la pagina: {len(scripts)}")

    revisados = 0
    for src in scripts:
        if revisados >= 10:
            break
        if not any(d in src for d in ("feb.es", "/js/", "/Scripts/", "/scripts/")):
            continue
        try:
            js = _request_con_reintentos(session, "GET", src).text
        except Exception as exc:  # noqa: BLE001
            informe.append(f"  JS no accesible {src[:120]}: {exc}")
            continue
        revisados += 1
        encontrado = False
        for palabra in PALABRAS_PBP:
            for frag in _fragmentos(js, palabra, maximo=2):
                informe.append(f"  JS {src.rsplit('/', 1)[-1][:40]} [{palabra}]: {frag[:200]}")
                encontrado = True
        for u in sorted(set(re.findall(r"https?://[^\s\"'<>()]+", js)))[:10]:
            if any(p in u.lower() for p in ("intrafeb", "livestats", "api", ".json")):
                informe.append(f"  JS URL: {u[:200]}")
                encontrado = True
        if not encontrado:
            informe.append(f"  JS {src.rsplit('/', 1)[-1][:40]}: sin pistas")

    informe.append("=== fin DETECTIVE ===")
    for linea in informe:
        print(linea, file=sys.stderr)
    return informe


# ----------------------------- API LIVESTATS DE LA FEB ---------------------
# Descubierto con el modo detective: la web pide los datos en vivo a
#   https://intrafeb.feb.es/LiveStats.API/api/v1/<SERVICIO>/<idPartido>
# con una clave que viene en la propia pagina del partido
#   <input id="_ctl0_token" value="...">  ->  cabecera Authorization: Bearer ...

LIVESTATS_BASE = os.environ.get("FEB_LIVESTATS_BASE", "https://intrafeb.feb.es/LiveStats.API/api/v1")
# Servicios vistos en el JavaScript de la FEB + otros nombres probables para el
# jugada a jugada. Los que no existan devolveran 404 y se descartan solos.
LIVESTATS_SERVICIOS = ["BoxScore", "KeyFacts", "TeamStats", "Ranking", "ShotChart",
                       "PlayByPlay", "PlayByPlays", "Jugadas", "Events", "Actions"]


def token_livestats(html: str) -> str:
    """Clave 'Bearer' que la web guarda en un campo oculto de la pagina del partido."""
    soup = BeautifulSoup(html, "html.parser")
    campo = (soup.find("input", id="_ctl0_token")
             or soup.find("input", attrs={"name": "_ctl0:token"})
             or soup.select_one("#contentToken > input"))
    return campo.get("value", "") if campo else ""


def _mapa_json(obj, prefijo="", profundidad=0, lineas=None, max_lineas=120) -> list:
    """Describe la forma de un JSON: claves, tipos y tamaños (sin volcarlo entero)."""
    if lineas is None:
        lineas = []
    if len(lineas) >= max_lineas or profundidad > 3:
        return lineas
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:40]:
            ruta = f"{prefijo}.{k}" if prefijo else k
            if isinstance(v, (dict, list)):
                tam = f"[{len(v)}]" if isinstance(v, list) else "{...}"
                lineas.append(f"    {ruta} {tam}")
                _mapa_json(v, ruta, profundidad + 1, lineas, max_lineas)
            else:
                lineas.append(f"    {ruta} = {str(v)[:60]}")
            if len(lineas) >= max_lineas:
                break
    elif isinstance(obj, list) and obj:
        _mapa_json(obj[0], f"{prefijo}[0]", profundidad + 1, lineas, max_lineas)
    return lineas


def investigar_livestats(partido_id: str) -> list:
    """Pregunta a la API de la FEB por un partido y describe lo que devuelve."""
    pid = str(partido_id).lstrip("p")
    informe = [f"=== DETECTIVE 2: API LiveStats (partido {pid}) ==="]
    session = requests.Session()
    try:
        html = _request_con_reintentos(session, "GET", PARTIDO_URL.format(id=pid)).text
    except Exception as exc:  # noqa: BLE001
        informe.append(f"No se pudo abrir la pagina del partido: {exc}")
        for l in informe:
            print(l, file=sys.stderr)
        return informe

    token = token_livestats(html)
    informe.append(f"Token encontrado: {'si' if token else 'NO'} (longitud {len(token)})")
    if not token:
        informe.append("Sin token no se puede preguntar a la API.")
        for l in informe:
            print(l, file=sys.stderr)
        return informe

    cabeceras = dict(HEADERS)
    cabeceras["Authorization"] = "Bearer " + token
    cabeceras["Accept"] = "application/json"

    for servicio in LIVESTATS_SERVICIOS:
        url = f"{LIVESTATS_BASE}/{servicio}/{pid}"
        try:
            resp = session.get(url, headers=cabeceras, timeout=30)
        except Exception as exc:  # noqa: BLE001
            informe.append(f"  {servicio}: error de conexion ({exc})")
            continue
        time.sleep(PAUSA_ENTRE_PETICIONES)
        informe.append(f"  {servicio}: HTTP {resp.status_code} ({len(resp.content)} bytes)")
        if resp.status_code != 200 or not resp.content:
            continue
        try:
            datos = resp.json()
        except ValueError:
            informe.append(f"    (no es JSON) {resp.text[:200]}")
            continue
        informe += _mapa_json(datos)
        # ¿hay sustituciones? es lo que hace falta para los quintetos
        texto = resp.text
        for palabra in ("ustitu", "ubstitu", "Entra a pista", "Sale de pista"):
            frags = _fragmentos(texto, palabra, ancho=260, maximo=2)
            for f in frags:
                informe.append(f"    [{palabra}] {f[:260]}")

    informe.append("=== fin DETECTIVE 2 ===")
    for l in informe:
        print(l, file=sys.stderr)
    return informe


# ----------------------------- RESUMEN DE TIROS POR JUGADORA ---------------
# La FEB solo publica sus rankings de porcentaje a quien llega a un minimo de
# intentos. Aqui se suman los tiros de TODAS las jugadoras a partir del cuadro
# de cada partido, en una pestaña pequeña que la app puede leer rapido.

TAB_TIROS = "Jugadoras_Tiros"


def _partes_tiro(txt: str) -> tuple:
    m = re.search(r"(\d+)\s*/\s*(\d+)", str(txt or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


SUMAS_JUGADORA = [("PT", "Puntos"), ("AS", "Asistencias"), ("BP", "Perdidas"), ("BR", "Robos"),
                  ("TAP_F", "Tapones"), ("REB_O", "RebOf"), ("REB_D", "RebDef"), ("REB_T", "RebTot"),
                  ("FAL_C", "FaltasCom"), ("FAL_R", "FaltasRec"), ("VA", "Valoracion")]


def _minutos_a_segundos(txt: str) -> int:
    m = re.match(r"\s*(\d+):([0-5]\d)", str(txt or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def resumen_tiros_jugadoras(partidos_df: pd.DataFrame) -> pd.DataFrame:
    if partidos_df.empty or "Jugador" not in partidos_df.columns:
        return pd.DataFrame()
    filas = {}
    for _, f in partidos_df.iterrows():
        jugadora = str(f.get("Jugador", "")).strip()
        equipo = str(f.get("Equipo", "")).strip()
        if not jugadora or jugadora == "TOTAL":
            continue
        clave = (equipo, jugadora)
        d = filas.setdefault(clave, {"Equipo": equipo, "Jugador": jugadora, "Partidos": 0,
                                     "T2A": 0, "T2I": 0, "T3A": 0, "T3I": 0, "TLA": 0, "TLI": 0,
                                     "Segundos": 0, "Dorsales": {},
                                     **{etiqueta: 0 for _, etiqueta in SUMAS_JUGADORA}})
        d["Partidos"] += 1
        d["Segundos"] += _minutos_a_segundos(f.get("MIN", ""))
        for col, etiqueta in SUMAS_JUGADORA:
            try:
                d[etiqueta] += int(float(str(f.get(col, "") or 0).replace(",", ".")))
            except ValueError:
                pass
        for col, pref in (("T2", "T2"), ("T3", "T3"), ("TL", "TL")):
            a, i = _partes_tiro(f.get(col, ""))
            d[pref + "A"] += a
            d[pref + "I"] += i
        dorsal = str(f.get("Dorsal", "")).strip()
        if dorsal:
            d["Dorsales"][dorsal] = d["Dorsales"].get(dorsal, 0) + 1

    salida = []
    for d in filas.values():
        dorsal = max(d["Dorsales"].items(), key=lambda x: x[1])[0] if d["Dorsales"] else ""
        pct = lambda a, i: f"{round(a / i * 100, 1):.1f}".replace(".", ",") if i else ""
        fila = {"Equipo": d["Equipo"], "Jugador": d["Jugador"], "Dorsal": dorsal,
                "Partidos": d["Partidos"], "Minutos": round(d["Segundos"] / 60, 1),
                "T2A": d["T2A"], "T2I": d["T2I"], "T2Pct": pct(d["T2A"], d["T2I"]),
                "T3A": d["T3A"], "T3I": d["T3I"], "T3Pct": pct(d["T3A"], d["T3I"]),
                "TLA": d["TLA"], "TLI": d["TLI"], "TLPct": pct(d["TLA"], d["TLI"])}
        for _, etiqueta in SUMAS_JUGADORA:
            fila[etiqueta] = d[etiqueta]
        salida.append(fila)
    return pd.DataFrame(salida).sort_values(["Equipo", "Jugador"]).reset_index(drop=True)


# ----------------------------- QUINTETOS (jugada a jugada) -----------------
# La API LiveStats de la FEB devuelve en "KeyFacts" el jugada a jugada
# (PLAYBYPLAY.LINES) con las sustituciones. Con eso se reconstruye que cinco
# jugadoras estaban en pista en cada momento y cuantos puntos se hicieron.

KEYFACTS_URL = LIVESTATS_BASE + "/KeyFacts/{id}"
TAB_QUINTETOS_PARTIDO = "Quintetos_Partido"
TAB_QUINTETOS = "Quintetos"
MINUTOS_MAX_QUINTETOS = int(os.environ.get("FEB_MINUTOS_QUINTETOS", "40"))
SEGUNDOS_CUARTO = 600        # 10 minutos
SEGUNDOS_PRORROGA = 300      # 5 minutos

RE_ENTRA = re.compile(r"entra\s+a\s+pista", re.I)
RE_SALE = re.compile(r"sale\s+de\s+pista", re.I)
RE_ANOTA = re.compile(r"tiro\s+de\s*(\d)\s*anotad|canasta\s+de\s*(\d)", re.I)
RE_EQUIPO_JUGADORA = re.compile(r"^\((?P<equipo>[^)]+)\)\s*(?P<jugadora>[^:]+):", re.S)
# Para contar posesiones (formula habitual): tiros de campo intentados
# - rebotes ofensivos + perdidas + 0,44 x tiros libres intentados
RE_TIRO = re.compile(r"tiro\s+de\s*(\d)\s*(anotad|fallad|encestad|errad)", re.I)
RE_REB_OF = re.compile(r"rebote\s+ofensivo", re.I)
RE_PERDIDA = re.compile(r"p[eé]rdida|balon\s+perdido|perdida", re.I)
RE_ASISTENCIA = re.compile(r"asistencia", re.I)
RE_REB_DEF = re.compile(r"rebote\s+defensivo", re.I)
RE_TAPON = re.compile(r"tap[oó]n", re.I)


def _segundos_restantes(txt: str) -> int:
    m = re.match(r"\s*(\d+):(\d+)", str(txt or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def _segundos_absolutos(cuarto: int, restantes: int) -> int:
    largo = SEGUNDOS_CUARTO if cuarto <= 4 else SEGUNDOS_PRORROGA
    previos = min(cuarto - 1, 4) * SEGUNDOS_CUARTO + max(0, cuarto - 5) * SEGUNDOS_PRORROGA
    return previos + max(0, largo - restantes)


def eventos_de_keyfacts(datos: dict) -> tuple:
    """(nombres de los dos equipos, lista de eventos ordenados)."""
    cabecera = datos.get("HEADER") or {}
    equipos = [str(t.get("name", "")).strip() for t in (cabecera.get("TEAM") or [])]
    lineas = ((datos.get("PLAYBYPLAY") or {}).get("LINES")) or []

    eventos = []
    for linea in lineas:
        if linea.get("deleted"):
            continue
        texto = str(linea.get("text") or "")
        try:
            cuarto = int(str(linea.get("quarter") or "1"))
            num = int(str(linea.get("num") or "0"))
        except ValueError:
            continue
        m = RE_EQUIPO_JUGADORA.match(texto)
        equipo = m.group("equipo").strip() if m else ""
        jugadora = re.sub(r"\s+", " ", m.group("jugadora")).strip() if m else ""
        if equipo and equipos:
            # normaliza al nombre tal cual lo da la cabecera
            for e in equipos:
                if e and (e.upper() == equipo.upper() or e.upper().startswith(equipo.upper()[:12])):
                    equipo = e
                    break
        anota = RE_ANOTA.search(texto)
        puntos = int(anota.group(1) or anota.group(2)) if anota else 0
        tiro = RE_TIRO.search(texto)
        tiro_campo = 1 if (tiro and tiro.group(1) in ("2", "3")) else 0
        tiro_libre = 1 if (tiro and tiro.group(1) == "1") else 0
        de_tres = 1 if (tiro and tiro.group(1) == "3") else 0
        reb_of = 1 if RE_REB_OF.search(texto) else 0
        asistencia = 1 if RE_ASISTENCIA.search(texto) else 0
        reb_def = 1 if RE_REB_DEF.search(texto) else 0
        tapon = 1 if RE_TAPON.search(texto) else 0
        perdida = 1 if RE_PERDIDA.search(texto) else 0
        if RE_ENTRA.search(texto):
            tipo = "entra"
        elif RE_SALE.search(texto):
            tipo = "sale"
        elif puntos:
            tipo = "anota"
        elif jugadora:
            tipo = "accion"
        else:
            tipo = "otro"
        eventos.append({"num": num, "cuarto": cuarto, "tipo": tipo, "equipo": equipo,
                        "jugadora": jugadora, "puntos": puntos,
                        "tc": tiro_campo, "tl": tiro_libre, "de3": de_tres,
                        "ro": reb_of, "rd": reb_def, "bp": perdida, "as": asistencia, "tap": tapon,
                        "seg": _segundos_absolutos(cuarto, _segundos_restantes(linea.get("time")))})
    eventos.sort(key=lambda e: (e["cuarto"], e["num"]))
    return equipos, eventos


def _quintetos_iniciales(eventos: list, equipo: str) -> set:
    """Quien estaba en pista al empezar el cuarto: quien actua o sale sin haber entrado."""
    inicial, actual = set(), set()
    for ev in eventos:
        if ev["equipo"] != equipo or not ev["jugadora"]:
            continue
        j = ev["jugadora"]
        if ev["tipo"] == "entra":
            actual.add(j)
            continue
        if j not in actual:
            inicial.add(j)
            actual.add(j)
        if ev["tipo"] == "sale":
            actual.discard(j)
    return inicial


def quintetos_de_partido(datos: dict, partido_id: str) -> list:
    """Minutos y puntos de cada quinteto en un partido."""
    equipos, eventos = eventos_de_keyfacts(datos)
    if len(equipos) != 2 or not eventos:
        return []

    acumulado = {}   # (equipo, quinteto) -> {segundos, pf, pc, posesiones...}
    cuartos = sorted({ev["cuarto"] for ev in eventos})
    ultimo_quinteto = {}        # con que cinco acabo cada equipo el cuarto anterior
    saltados = 0
    for cuarto in cuartos:
        evs = [e for e in eventos if e["cuarto"] == cuarto]
        if not evs:
            continue
        largo = SEGUNDOS_CUARTO if cuarto <= 4 else SEGUNDOS_PRORROGA
        inicio = _segundos_absolutos(cuarto, largo)
        fin = inicio + largo

        pista = {}
        for e in equipos:
            deducido = _quintetos_iniciales(evs, e)
            if len(deducido) != 5 and len(ultimo_quinteto.get(e, set())) == 5:
                # La FEB no siempre registra quien sale a cada cuarto:
                # si no cuadra, se sigue con el cinco que acabo el cuarto anterior
                deducido = set(ultimo_quinteto[e])
            pista[e] = deducido
        if any(len(pista[e]) != 5 for e in equipos):
            saltados += 1
            continue   # cuarto mal registrado: se descarta antes que inventar minutos

        t_ini = inicio
        vacio = lambda: {e: {"pts": 0, "tc": 0, "tl": 0, "ro": 0, "bp": 0} for e in equipos}
        tramo = vacio()

        def posesiones(d):
            return d["tc"] - d["ro"] + d["bp"] + 0.44 * d["tl"]

        def cerrar(t_fin):
            nonlocal tramo
            dur = max(0, min(t_fin, fin) - t_ini)
            if dur <= 0:
                return
            for e in equipos:
                if len(pista[e]) != 5:
                    continue
                rival = equipos[1] if e == equipos[0] else equipos[0]
                clave = (e, " · ".join(sorted(pista[e])))
                fila = acumulado.setdefault(clave, {"Segundos": 0, "PF": 0, "PC": 0,
                                                    "POS": 0.0, "POS_Rival": 0.0})
                fila["Segundos"] += dur
                fila["PF"] += tramo[e]["pts"]
                fila["PC"] += tramo[rival]["pts"]
                fila["POS"] += posesiones(tramo[e])
                fila["POS_Rival"] += posesiones(tramo[rival])

        for ev in evs:
            if ev["tipo"] in ("entra", "sale"):
                cerrar(ev["seg"])
                t_ini = min(ev["seg"], fin)
                tramo = vacio()
                if ev["equipo"] in pista and ev["jugadora"]:
                    if ev["tipo"] == "entra":
                        pista[ev["equipo"]].add(ev["jugadora"])
                    else:
                        pista[ev["equipo"]].discard(ev["jugadora"])
            elif ev["equipo"] in tramo:
                d = tramo[ev["equipo"]]
                d["pts"] += ev["puntos"]
                d["tc"] += ev.get("tc", 0)
                d["tl"] += ev.get("tl", 0)
                d["ro"] += ev.get("ro", 0)
                d["bp"] += ev.get("bp", 0)
        cerrar(fin)
        for e in equipos:
            if len(pista[e]) == 5:
                ultimo_quinteto[e] = set(pista[e])

    if saltados:
        print(f"  [quintetos] {partido_id}: {saltados} cuarto(s) descartados por sustituciones incompletas",
              file=sys.stderr)

    filas = []
    for (equipo, quinteto), d in acumulado.items():
        if d["Segundos"] <= 0:
            continue
        filas.append({"PartidoID": partido_id, "Equipo": equipo, "Quinteto": quinteto,
                      "Segundos": d["Segundos"], "PF": d["PF"], "PC": d["PC"],
                      "POS": round(d["POS"], 2), "POS_Rival": round(d["POS_Rival"], 2)})
    return filas


TAB_CUARTOS = "Partidos_Cuartos"
TAB_TIROS_MAPA = "Tiros"
SHOTCHART_URL = LIVESTATS_BASE + "/ShotChart/{id}"


def tiros_de_partido(datos: dict, partido_id: str) -> tuple:
    """Cada tiro del partido: quien, cuando, donde y si entro.
    Devuelve (filas, informe) — el informe solo sirve para el registro."""
    cabecera = datos.get("HEADER") or {}
    equipos_cab = [str(t.get("name", "")).strip() for t in (cabecera.get("TEAM") or [])]
    chart = datos.get("SHOTCHART") or {}
    equipos = chart.get("TEAM") or []
    tiros = chart.get("SHOTS") or []
    if not tiros:
        return [], []

    # Quien es cada jugadora: la lista PLAYER de su equipo.
    # El tiro apunta a ella con su dorsal o con su id, y a veces con ceros
    # delante ("08"), asi que se guardan todas las formas posibles.
    def _variantes(valor):
        txt = str(valor).strip()
        formas = {txt, txt.lstrip("0") or txt}
        if txt.isdigit():
            formas.add(str(int(txt)))
        return formas

    jugadoras = {}
    for i, eq in enumerate(equipos):
        nombre_eq = str(eq.get("name") or (equipos_cab[i] if i < len(equipos_cab) else "")).strip()
        for j, jug in enumerate(eq.get("PLAYER") or []):
            for clave in ("no", "dorsal", "number", "id", "idPlayer", "idLicense", "licencia"):
                if jug.get(clave) not in (None, ""):
                    for forma in _variantes(jug.get(clave)):
                        jugadoras.setdefault((i, forma), (nombre_eq, jug))
            for forma in _variantes(j):
                jugadoras.setdefault((i, forma), (nombre_eq, jug))

    def nombre_de(jug):
        for clave in ("name", "nombre", "playerName", "shortName"):
            if jug.get(clave):
                return str(jug[clave]).strip()
        return ""

    filas = []
    sin_nombre = 0
    for t in tiros:
        try:
            idx_eq = int(str(t.get("team", "0") or 0))
        except ValueError:
            idx_eq = 0
        ref = str(t.get("player", "")).strip()
        nombre_eq, jug = (equipos_cab[idx_eq] if idx_eq < len(equipos_cab) else "", {})
        for forma in _variantes(ref):
            if (idx_eq, forma) in jugadoras:
                nombre_eq, jug = jugadoras[(idx_eq, forma)]
                break
        if not jug:
            sin_nombre += 1
        filas.append({
            "PartidoID": partido_id,
            "Equipo": nombre_eq,
            "Jugadora": nombre_de(jug),
            "Dorsal": str(jug.get("no") or jug.get("dorsal") or ref),
            "Cuarto": str(t.get("quarter", "")),
            "Tiempo": str(t.get("t", "")),
            "Anotado": "Si" if str(t.get("m", "")) in ("1", "True", "true") else "No",
            "X": t.get("x", ""),
            "Y": t.get("y", ""),
        })

    # Informe para el registro: rangos y un par de ejemplos, para saber como numeran la pista
    xs = [float(f["X"]) for f in filas if str(f["X"]).replace(".", "", 1).replace("-", "", 1).isdigit()]
    ys = [float(f["Y"]) for f in filas if str(f["Y"]).replace(".", "", 1).replace("-", "", 1).isdigit()]
    informe = [f"  [tiros] {len(filas)} tiros en {partido_id} ({sin_nombre} sin jugadora identificada)"]
    if sin_nombre:
        informe.append(f"  [tiros] referencias que no cuadran: "
                       f"{sorted({str(t.get('player')) for t in tiros})[:8]}")
        if equipos and (equipos[0].get('PLAYER') or []):
            ejemplo = (equipos[0]['PLAYER'])[0]
            informe.append(f"  [tiros] ejemplo de jugadora: no={ejemplo.get('no')} id={ejemplo.get('id')} "
                           f"name={ejemplo.get('name')}")
    if xs and ys:
        informe.append(f"  [tiros] X de {min(xs):.1f} a {max(xs):.1f} · Y de {min(ys):.1f} a {max(ys):.1f}")
    if equipos:
        informe.append(f"  [tiros] claves de una jugadora: {list((equipos[0].get('PLAYER') or [{}])[0].keys())}")
    informe.append(f"  [tiros] ejemplos: {filas[:3]}")
    return filas, informe


def cuartos_de_partido(datos: dict, partido_id: str) -> list:
    """Puntos, tiros y posesiones de cada equipo en cada cuarto."""
    equipos, eventos = eventos_de_keyfacts(datos)
    if len(equipos) != 2 or not eventos:
        return []

    acumulado = {}
    for ev in eventos:
        if ev["equipo"] not in equipos:
            continue
        clave = (ev["equipo"], ev["cuarto"])
        d = acumulado.setdefault(clave, {"pts": 0, "t2a": 0, "t2i": 0, "t3a": 0, "t3i": 0,
                                         "tla": 0, "tli": 0, "ro": 0, "bp": 0})
        d["pts"] += ev["puntos"]
        d["ro"] += ev.get("ro", 0)
        d["bp"] += ev.get("bp", 0)
        if ev.get("tl"):
            d["tli"] += 1
            if ev["puntos"] == 1:
                d["tla"] += 1
        elif ev.get("tc"):
            if ev["puntos"] == 3:
                d["t3i"] += 1
                d["t3a"] += 1
            elif ev["puntos"] == 2:
                d["t2i"] += 1
                d["t2a"] += 1
            else:
                # tiro fallado: se mira si era de 3 por el texto del evento
                if ev.get("de3"):
                    d["t3i"] += 1
                else:
                    d["t2i"] += 1

    filas = []
    for (equipo, cuarto), d in sorted(acumulado.items(), key=lambda x: (x[0][0], x[0][1])):
        rival = equipos[1] if equipo == equipos[0] else equipos[0]
        pos = d["t2i"] + d["t3i"] - d["ro"] + d["bp"] + 0.44 * d["tli"]
        tc_a, tc_i = d["t2a"] + d["t3a"], d["t2i"] + d["t3i"]
        filas.append({
            "PartidoID": partido_id, "Equipo": equipo, "Rival": rival, "Cuarto": cuarto,
            "PT": d["pts"], "T2A": d["t2a"], "T2I": d["t2i"], "T3A": d["t3a"], "T3I": d["t3i"],
            "TLA": d["tla"], "TLI": d["tli"], "REB_O": d["ro"], "BP": d["bp"],
            "POS": round(pos, 2),
            "OER": round(100 * d["pts"] / pos, 1) if pos > 0 else "",
            "eFG": round(100 * (tc_a + 0.5 * d["t3a"]) / tc_i, 1) if tc_i else "",
        })
    return filas


TAB_REBOTES = "Rebotes"


def rebotes_por_tipo(datos: dict, partido_id: str) -> list:
    """De cada tiro fallado, quien cogio el rebote. Asi se sabe que se rebotea
    mejor: los triples fallados, los tiros de 2 o los tiros libres."""
    equipos, eventos = eventos_de_keyfacts(datos)
    if len(equipos) != 2 or not eventos:
        return []

    datos_eq = {}

    def ficha(equipo, tipo):
        return datos_eq.setdefault((equipo, tipo), {
            "PartidoID": partido_id, "Equipo": equipo, "Tipo": tipo,
            "Fallados": 0, "RebOf": 0, "RebDef": 0, "SinRebote": 0,
        })

    ultimo = None   # (equipo, tipo, indice del evento)
    for i, ev in enumerate(eventos):
        if ev["equipo"] not in equipos:
            continue

        # Un tiro fallado deja rebote en el aire
        if (ev.get("tc") or ev.get("tl")) and ev["puntos"] == 0:
            if ultimo:
                ficha(ultimo[0], ultimo[1])["SinRebote"] += 1
            tipo = "Tiro libre" if ev.get("tl") else ("Triple" if ev.get("de3") else "Tiro de 2")
            ficha(ev["equipo"], tipo)["Fallados"] += 1
            ultimo = (ev["equipo"], tipo, i)
            continue

        if not ultimo:
            continue

        if ev.get("ro") or ev.get("rd"):
            equipo_tirador, tipo, idx = ultimo
            # Solo cuenta si el rebote llega justo despues del fallo
            if i - idx <= 4:
                f = ficha(equipo_tirador, tipo)
                if ev["equipo"] == equipo_tirador:
                    f["RebOf"] += 1
                else:
                    f["RebDef"] += 1
            else:
                ficha(equipo_tirador, tipo)["SinRebote"] += 1
            ultimo = None
        elif ev["puntos"] or ev.get("bp"):
            # La jugada siguio sin rebote registrado
            ficha(ultimo[0], ultimo[1])["SinRebote"] += 1
            ultimo = None

    if ultimo:
        ficha(ultimo[0], ultimo[1])["SinRebote"] += 1

    return [f for f in datos_eq.values() if f["Fallados"]]


TAB_CLUTCH = "Jugadoras_Clutch"
SEGUNDOS_CLUTCH = 300      # ultimos 5 minutos
MARGEN_CLUTCH = 5          # con 5 puntos o menos de diferencia


def analisis_pbp_jugadoras(datos: dict, partido_id: str) -> list:
    """Dos cosas por jugadora, sacadas del jugada a jugada:
    1) sus numeros en los momentos decisivos (clutch)
    2) cuantas de sus canastas llegaron de una asistencia."""
    equipos, eventos = eventos_de_keyfacts(datos)
    if len(equipos) != 2 or not eventos:
        return []

    marcador = {e: 0 for e in equipos}
    filas = {}

    def ficha(equipo, jugadora):
        clave = (equipo, jugadora)
        return filas.setdefault(clave, {
            "PartidoID": partido_id, "Equipo": equipo, "Jugadora": jugadora,
            "ClutchPT": 0, "ClutchT2A": 0, "ClutchT2I": 0, "ClutchT3A": 0, "ClutchT3I": 0,
            "ClutchTLA": 0, "ClutchTLI": 0, "ClutchTiros": 0,
            "C2_Asistidas": 0, "C2_Creadas": 0, "C3_Asistidas": 0, "C3_Creadas": 0,
            "Asistencias_dadas": 0,
        })

    # ¿la canasta llego de una asistencia? se mira si hay una justo al lado
    for i, ev in enumerate(eventos):
        if not ev["jugadora"] or ev["equipo"] not in equipos:
            continue
        f = ficha(ev["equipo"], ev["jugadora"])

        if ev.get("as"):
            f["Asistencias_dadas"] += 1

        # Canasta anotada de 2 o de 3: se busca una asistencia de su equipo pegada
        if ev["puntos"] in (2, 3) and ev.get("tc"):
            asistida = False
            for j in range(max(0, i - 2), min(len(eventos), i + 3)):
                otro = eventos[j]
                if j == i or otro["equipo"] != ev["equipo"]:
                    continue
                if otro.get("as") and otro["cuarto"] == ev["cuarto"] and abs(otro["seg"] - ev["seg"]) <= 3:
                    asistida = True
                    break
            clave = "C3" if ev["puntos"] == 3 else "C2"
            f[clave + ("_Asistidas" if asistida else "_Creadas")] += 1

        # Momentos decisivos
        largo = SEGUNDOS_CUARTO if ev["cuarto"] <= 4 else SEGUNDOS_PRORROGA
        previos = min(ev["cuarto"] - 1, 4) * SEGUNDOS_CUARTO + max(0, ev["cuarto"] - 5) * SEGUNDOS_PRORROGA
        restantes = previos + largo - ev["seg"]
        rival = equipos[1] if ev["equipo"] == equipos[0] else equipos[0]
        diferencia = abs(marcador[ev["equipo"]] - marcador[rival])
        es_clutch = ev["cuarto"] >= 4 and restantes <= SEGUNDOS_CLUTCH and diferencia <= MARGEN_CLUTCH

        if es_clutch:
            f["ClutchPT"] += ev["puntos"]
            if ev.get("tl"):
                f["ClutchTLI"] += 1
                if ev["puntos"] == 1:
                    f["ClutchTLA"] += 1
            elif ev.get("tc"):
                f["ClutchTiros"] += 1
                if ev.get("de3"):
                    f["ClutchT3I"] += 1
                    if ev["puntos"] == 3:
                        f["ClutchT3A"] += 1
                else:
                    f["ClutchT2I"] += 1
                    if ev["puntos"] == 2:
                        f["ClutchT2A"] += 1

        marcador[ev["equipo"]] += ev["puntos"]

    # Solo se guardan las jugadoras con algo que contar
    return [f for f in filas.values()
            if any(f[c] for c in ("ClutchPT", "ClutchTiros", "ClutchTLI",
                                  "C2_Asistidas", "C2_Creadas", "C3_Asistidas", "C3_Creadas",
                                  "Asistencias_dadas"))]


def resumir_clutch(filas: list, ya_guardado: pd.DataFrame) -> pd.DataFrame:
    """Una fila por jugadora, sumando todos sus partidos."""
    base = pd.DataFrame(filas) if filas else pd.DataFrame()
    if not ya_guardado.empty and not base.empty:
        ids = set(base["PartidoID"])
        ya_guardado = ya_guardado[~ya_guardado["PartidoID"].isin(ids)]
    junto = pd.concat([ya_guardado, base], ignore_index=True) if not base.empty else ya_guardado
    if junto.empty:
        return pd.DataFrame(), pd.DataFrame()

    columnas = [c for c in junto.columns if c not in ("PartidoID", "Equipo", "Jugadora")]
    for c in columnas:
        junto[c] = pd.to_numeric(junto[c], errors="coerce").fillna(0)

    resumen = junto.groupby(["Equipo", "Jugadora"], as_index=False).agg(
        {**{c: "sum" for c in columnas}, "PartidoID": "nunique"})
    resumen = resumen.rename(columns={"PartidoID": "Partidos"})
    clutch_partidos = (junto[junto["ClutchTiros"] + junto["ClutchTLI"] + junto["ClutchPT"] > 0]
                       .groupby(["Equipo", "Jugadora"])["PartidoID"].nunique().rename("PartidosClutch"))
    resumen = resumen.merge(clutch_partidos, on=["Equipo", "Jugadora"], how="left").fillna({"PartidosClutch": 0})
    return junto, resumen


def obtener_token_livestats(session, partido_id: str) -> str:
    html = _request_con_reintentos(session, "GET", PARTIDO_URL.format(id=str(partido_id).lstrip("p"))).text
    return token_livestats(html)


def fetch_quintetos(sh, resultados_df: pd.DataFrame) -> tuple:
    """Devuelve (detalle por partido, resumen por equipo, nuevos)."""
    if resultados_df.empty or "PartidoID" not in resultados_df.columns:
        return pd.DataFrame(), pd.DataFrame(), 0

    try:
        ws = sh.worksheet(TAB_QUINTETOS_PARTIDO)
        valores = ws.get_all_values()
        df_viejo = pd.DataFrame(valores[1:], columns=valores[0]) if len(valores) > 1 else pd.DataFrame()
    except Exception:  # noqa: BLE001
        df_viejo = pd.DataFrame()
    if not df_viejo.empty and "POS" not in df_viejo.columns:
        print("  [quintetos] datos antiguos sin posesiones: se vuelven a descargar", file=sys.stderr)
        df_viejo = pd.DataFrame()
    ya = set(df_viejo["PartidoID"]) if "PartidoID" in df_viejo.columns else set()

    def _ids_de_pestana(nombre_tab):
        try:
            ws = sh.worksheet(nombre_tab)
            valores = ws.get_all_values()
            if len(valores) < 2:
                return set(), pd.DataFrame()
            df = pd.DataFrame(valores[1:], columns=valores[0])
            return (set(df["PartidoID"]) if "PartidoID" in df.columns else set()), df
        except Exception:  # noqa: BLE001
            return set(), pd.DataFrame()

    ya_cuartos, cuartos_viejos = _ids_de_pestana(TAB_CUARTOS)
    ya_tiros, tiros_viejos = _ids_de_pestana(TAB_TIROS_MAPA)
    _, clutch_viejo = _ids_de_pestana(TAB_CLUTCH + "_Partido")
    _, rebotes_viejos = _ids_de_pestana(TAB_REBOTES)

    jugados = resultados_df[(resultados_df["Jugado"] == "Si") & (resultados_df["PartidoID"] != "")]
    completos = ya & ya_cuartos & ya_tiros
    pendientes = [p for p in dict.fromkeys(jugados["PartidoID"]) if p not in completos]
    print(f"  [quintetos] quintetos {len(ya)} · cuartos {len(ya_cuartos)} · tiros {len(ya_tiros)} "
          f"-> {len(pendientes)} partidos pendientes", file=sys.stderr)

    # Datos del partido para poder filtrar luego (jornada, fecha, rival, victoria)
    meta = {}
    for _, fila in jugados.iterrows():
        pid = fila["PartidoID"]
        try:
            pl, pv = int(fila.get("Pts_Local") or 0), int(fila.get("Pts_Visitante") or 0)
        except ValueError:
            pl = pv = 0
        meta[pid] = {"Fase": fila.get("Fase", ""), "Jornada": fila.get("Jornada", ""),
                     "Fecha": fila.get("Fecha", ""), "Local": fila.get("Local", ""),
                     "Visitante": fila.get("Visitante", ""), "PtsL": pl, "PtsV": pv}

    def _con_meta(fila):
        m = meta.get(fila["PartidoID"])
        if not m:
            return fila
        es_local = normalizar_equipo(fila["Equipo"]) == normalizar_equipo(m["Local"])
        rival = m["Visitante"] if es_local else m["Local"]
        propios, contra = (m["PtsL"], m["PtsV"]) if es_local else (m["PtsV"], m["PtsL"])
        fila.update({"Fase": m["Fase"], "Jornada": m["Jornada"], "Fecha": m["Fecha"],
                     "Rival": rival, "Casa": "Si" if es_local else "No",
                     "Gano": "Si" if propios > contra else ("No" if propios < contra else "")})
        return fila

    nuevos = []
    nuevos_cuartos = []
    nuevos_tiros = []
    nuevos_clutch = []
    nuevos_rebotes = []
    if pendientes:
        session = requests.Session()
        try:
            token = obtener_token_livestats(session, pendientes[0])
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: no se pudo obtener el token de LiveStats: {exc}", file=sys.stderr)
            token = ""
        if token:
            cabeceras = dict(HEADERS)
            cabeceras["Authorization"] = "Bearer " + token
            cabeceras["Accept"] = "application/json"
            limite = INICIO_EJECUCION + (MINUTOS_MAX_PARTIDOS + MINUTOS_MAX_QUINTETOS) * 60
            hechos = {"n": 0, "sin_tiempo": 0, "vacios": 0}

            def _uno(pid):
                if time.time() > limite:
                    hechos["sin_tiempo"] += 1
                    return None
                resp = _request_con_reintentos(session, "GET", KEYFACTS_URL.format(id=pid.lstrip("p")),
                                               headers_extra=cabeceras)
                datos = resp.json()
                filas = quintetos_de_partido(datos, pid)
                try:
                    resp_t = _request_con_reintentos(session, "GET", SHOTCHART_URL.format(id=pid.lstrip("p")),
                                                     headers_extra=cabeceras)
                    tiros, informe_t = tiros_de_partido(resp_t.json(), pid)
                    if informe_t and not hechos.get("informe_tiros"):
                        hechos["informe_tiros"] = True
                        for l in informe_t:
                            print(l, file=sys.stderr)
                except Exception as exc:  # noqa: BLE001
                    tiros = []
                    if not hechos.get("aviso_tiros"):
                        hechos["aviso_tiros"] = True
                        print(f"  Aviso: no se pudo leer el ShotChart: {exc}", file=sys.stderr)
                hechos["n"] += 1
                if hechos["n"] % 50 == 0:
                    print(f"  [quintetos] {hechos['n']}/{len(pendientes)}", file=sys.stderr)
                if not filas:
                    hechos["vacios"] += 1
                return {"quintetos": filas, "cuartos": cuartos_de_partido(datos, pid), "tiros": tiros,
                        "clutch": analisis_pbp_jugadoras(datos, pid),
                        "rebotes": rebotes_por_tipo(datos, pid)}

            for resultado in _en_paralelo(pendientes, _uno):
                if not resultado:
                    continue
                if resultado.get("quintetos"):
                    nuevos.extend(_con_meta(f) for f in resultado["quintetos"])
                if resultado.get("cuartos"):
                    nuevos_cuartos.extend(resultado["cuartos"])
                if resultado.get("tiros"):
                    nuevos_tiros.extend(resultado["tiros"])
                if resultado.get("clutch"):
                    nuevos_clutch.extend(resultado["clutch"])
                if resultado.get("rebotes"):
                    nuevos_rebotes.extend(resultado["rebotes"])
            if hechos["vacios"]:
                print(f"  Aviso: {hechos['vacios']} partidos sin jugada a jugada utilizable", file=sys.stderr)
            if hechos["sin_tiempo"]:
                print(f"  [quintetos] limite de tiempo: quedan {hechos['sin_tiempo']} para la proxima vez",
                      file=sys.stderr)

    if not df_viejo.empty and nuevos:
        ids_rehechos = {f["PartidoID"] for f in nuevos}
        df_viejo = df_viejo[~df_viejo["PartidoID"].isin(ids_rehechos)]
    detalle = pd.concat([df_viejo, pd.DataFrame(nuevos)], ignore_index=True).fillna("") \
        if nuevos else df_viejo
    if detalle.empty:
        return detalle, pd.DataFrame(), 0

    # Rellena jornada, fecha, rival y victoria en todas las filas (nuevas y viejas)
    extra = [_con_meta({"PartidoID": pid, "Equipo": eq}) for pid, eq in
             zip(detalle["PartidoID"], detalle["Equipo"])]
    for col in ("Fase", "Jornada", "Fecha", "Rival", "Casa", "Gano"):
        detalle[col] = [e.get(col, "") for e in extra]

    for col in ("Segundos", "PF", "PC", "POS", "POS_Rival"):
        if col in detalle.columns:
            detalle[col] = pd.to_numeric(detalle[col], errors="coerce").fillna(0)
        else:
            detalle[col] = 0
    resumen = (detalle.groupby(["Equipo", "Quinteto"], as_index=False)
               .agg(Segundos=("Segundos", "sum"), PF=("PF", "sum"), PC=("PC", "sum"),
                    POS=("POS", "sum"), POS_Rival=("POS_Rival", "sum"),
                    Partidos=("PartidoID", "nunique")))
    resumen["Minutos"] = (resumen["Segundos"] / 60).round(1)
    resumen["Dif"] = resumen["PF"] - resumen["PC"]
    resumen["Dif40"] = (resumen["Dif"] / (resumen["Segundos"] / 2400)).round(1)
    pos = pd.to_numeric(resumen["POS"], errors="coerce").astype(float)
    posr = pd.to_numeric(resumen["POS_Rival"], errors="coerce").astype(float)
    resumen["ORtg"] = (100 * resumen["PF"] / pos.where(pos > 0)).astype(float).round(1)
    resumen["DRtg"] = (100 * resumen["PC"] / posr.where(posr > 0)).astype(float).round(1)
    resumen["NET"] = (resumen["ORtg"] - resumen["DRtg"]).astype(float).round(1)
    resumen = resumen[resumen["Segundos"] > 0].sort_values(
        ["Equipo", "Segundos"], ascending=[True, False]).reset_index(drop=True)
    resumen = resumen[["Equipo", "Quinteto", "Partidos", "Minutos", "PF", "PC", "Dif", "Dif40",
                       "ORtg", "DRtg", "NET", "POS", "POS_Rival", "Segundos"]].fillna("")

    # Cuartos: se junta lo nuevo con lo que ya hubiera guardado
    if not cuartos_viejos.empty and nuevos_cuartos:
        ids_nuevos = {f["PartidoID"] for f in nuevos_cuartos}
        cuartos_viejos = cuartos_viejos[~cuartos_viejos["PartidoID"].isin(ids_nuevos)]
    cuartos = pd.concat([cuartos_viejos, pd.DataFrame(nuevos_cuartos)], ignore_index=True).fillna("") \
        if nuevos_cuartos else cuartos_viejos
    if not tiros_viejos.empty and nuevos_tiros:
        ids_t = {f["PartidoID"] for f in nuevos_tiros}
        tiros_viejos = tiros_viejos[~tiros_viejos["PartidoID"].isin(ids_t)]
    tiros_mapa = pd.concat([tiros_viejos, pd.DataFrame(nuevos_tiros)], ignore_index=True).fillna("") \
        if nuevos_tiros else tiros_viejos

    clutch_detalle, clutch_resumen = resumir_clutch(nuevos_clutch, clutch_viejo)

    if not rebotes_viejos.empty and nuevos_rebotes:
        ids_r = {f["PartidoID"] for f in nuevos_rebotes}
        rebotes_viejos = rebotes_viejos[~rebotes_viejos["PartidoID"].isin(ids_r)]
    rebotes = pd.concat([rebotes_viejos, pd.DataFrame(nuevos_rebotes)], ignore_index=True).fillna("") \
        if nuevos_rebotes else rebotes_viejos

    return (detalle, resumen, cuartos, tiros_mapa, clutch_detalle, clutch_resumen, rebotes,
            len(set(f["PartidoID"] for f in nuevos)) if nuevos else 0)


# ----------------------------- MAIN ----------------------------------------


def main():
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)
    formula_sep = get_formula_separator(sh)

    resumen = []

    # 1) Estadisticas por equipo (+ escudos), todos los grupos
    equipos_df, team_ids, extras = fetch_equipos_por_grupo(ESTADISTICAS_URL)
    if not equipos_df.empty:
        equipos_df = add_escudos(equipos_df, team_ids, formula_sep=formula_sep)
        write_dataframe(sh, "Equipos", equipos_df, has_photos=True)
        grupos = ""
        if "Grupo" in equipos_df.columns:
            grupos = " (" + ", ".join(f"Grupo {g}: {n}" for g, n in
                                      equipos_df["Grupo"].value_counts().sort_index().items()) + ")"
        resumen.append(f"Equipos: {len(equipos_df)} filas{grupos}")
        for i, df in enumerate(extras, start=1):
            write_dataframe(sh, f"Equipos_{i}", df)
            resumen.append(f"Equipos_{i}: {len(df)} filas")
    else:
        resumen.append("Equipos: sin tablas (probablemente la temporada aun no tiene partidos)")

    # Nombres buenos de equipo (los de la pestaña Equipos)
    canonicos = construir_canonicos(equipos_df)
    nombres_raros = set()
    print(f"  [nombres] {len(canonicos)} equipos de referencia", file=sys.stderr)

    # 2) Ranking de Puntos, TODAS las paginas (con foto)
    ranking_df = fetch_ranking_with_photos(RANKINGS_URL, formula_sep=formula_sep)
    ranking_df, raros = aplicar_canonicos(ranking_df, canonicos)
    nombres_raros |= raros
    ranking_df = quitar_jugadoras_repetidas(ranking_df)
    if not ranking_df.empty:
        write_dataframe(sh, "Jugadoras", ranking_df, has_photos=True)
        resumen.append(f"Jugadoras: {len(ranking_df)} filas (con foto)")
    else:
        resumen.append("Jugadoras: sin tablas todavia")

    # 2b) Resto de categorias, TODAS las paginas
    otras_categorias = fetch_all_ranking_categories(RANKINGS_URL, formula_sep=formula_sep)
    for cat_name, df in otras_categorias.items():
        df, raros = aplicar_canonicos(df, canonicos)
        nombres_raros |= raros
        df = quitar_jugadoras_repetidas(df)
        tab = f"Jugadoras_{cat_name}"
        write_dataframe(sh, tab, df, has_photos=True)
        resumen.append(f"{tab}: {len(df)} filas (con foto)")
    categorias_faltantes = set(RANKING_CATEGORIES) - set(otras_categorias)
    if categorias_faltantes:
        resumen.append(f"Categorias no disponibles esta vez: {', '.join(sorted(categorias_faltantes))}")

    # 3) Resultados + Clasificacion
    resultados_df, clasificacion_df, clasif_final_df = fetch_resultados_y_clasificacion(RESULTADOS_URL)
    resultados_df, raros = aplicar_canonicos(resultados_df, canonicos, ("Local", "Visitante"))
    nombres_raros |= raros
    clasificacion_df, raros = aplicar_canonicos(clasificacion_df, canonicos)
    nombres_raros |= raros
    clasif_final_df, raros = aplicar_canonicos(clasif_final_df, canonicos)
    nombres_raros |= raros
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

    if not clasif_final_df.empty:
        write_dataframe(sh, "Clasificacion_Final", clasif_final_df)
        resumen.append(f"Clasificacion_Final: {len(clasif_final_df)} filas")

    # 4) Estadisticas de cada partido (solo los nuevos)
    try:
        partidos_df, n_nuevos = fetch_estadisticas_partidos(sh, resultados_df)
        partidos_df, raros = aplicar_canonicos(partidos_df, canonicos)
        nombres_raros |= raros
        if n_nuevos:
            write_dataframe(sh, TAB_PARTIDOS, partidos_df)
        n_part = partidos_df["PartidoID"].nunique() if not partidos_df.empty else 0
        resumen.append(f"{TAB_PARTIDOS}: {n_part} partidos ({n_nuevos} nuevos)")

        tiros_df = resumen_tiros_jugadoras(partidos_df)
        tiros_df, _ = aplicar_canonicos(tiros_df, canonicos)
        if not tiros_df.empty:
            write_dataframe(sh, TAB_TIROS, tiros_df)
            resumen.append(f"{TAB_TIROS}: {len(tiros_df)} jugadoras")
    except Exception as exc:  # noqa: BLE001
        resumen.append(f"{TAB_PARTIDOS}: error ({exc})")

    # 5) Quintetos reales (jugada a jugada de la API LiveStats)
    try:
        (detalle_q, resumen_q, cuartos_q, tiros_mapa,
         clutch_detalle, clutch_resumen, rebotes_q, nuevos_q) = fetch_quintetos(sh, resultados_df)
        detalle_q, _ = aplicar_canonicos(detalle_q, canonicos)
        resumen_q, raros = aplicar_canonicos(resumen_q, canonicos)
        nombres_raros |= raros
        if nuevos_q:
            write_dataframe(sh, TAB_QUINTETOS_PARTIDO, detalle_q)
        if not resumen_q.empty:
            write_dataframe(sh, TAB_QUINTETOS, resumen_q)
        if not cuartos_q.empty:
            cuartos_q, _ = aplicar_canonicos(cuartos_q, canonicos, ("Equipo", "Rival"))
            write_dataframe(sh, TAB_CUARTOS, cuartos_q)
            resumen.append(f"{TAB_CUARTOS}: {cuartos_q['PartidoID'].nunique()} partidos")
        if not tiros_mapa.empty:
            tiros_mapa, _ = aplicar_canonicos(tiros_mapa, canonicos)
            write_dataframe(sh, TAB_TIROS_MAPA, tiros_mapa)
            resumen.append(f"{TAB_TIROS_MAPA}: {len(tiros_mapa)} tiros de {tiros_mapa['PartidoID'].nunique()} partidos")
        if not clutch_detalle.empty:
            clutch_detalle, _ = aplicar_canonicos(clutch_detalle, canonicos)
            write_dataframe(sh, TAB_CLUTCH + "_Partido", clutch_detalle)
        if not clutch_resumen.empty:
            clutch_resumen, _ = aplicar_canonicos(clutch_resumen, canonicos)
            write_dataframe(sh, TAB_CLUTCH, clutch_resumen)
            resumen.append(f"{TAB_CLUTCH}: {len(clutch_resumen)} jugadoras")
        if not rebotes_q.empty:
            rebotes_q, _ = aplicar_canonicos(rebotes_q, canonicos)
            write_dataframe(sh, TAB_REBOTES, rebotes_q)
            resumen.append(f"{TAB_REBOTES}: {rebotes_q['PartidoID'].nunique()} partidos")
        n_part_q = detalle_q["PartidoID"].nunique() if not detalle_q.empty else 0
        resumen.append(f"{TAB_QUINTETOS}: {len(resumen_q)} quintetos de {n_part_q} partidos ({nuevos_q} nuevos)")
    except Exception as exc:  # noqa: BLE001
        resumen.append(f"{TAB_QUINTETOS}: error ({exc})")

    # 6) Detective del jugada a jugada (solo mira, no descarga estadisticas)
    if INVESTIGAR_PBP and not resultados_df.empty and "PartidoID" in resultados_df.columns:
        jugados_ids = [p for p in resultados_df.loc[resultados_df["Jugado"] == "Si", "PartidoID"] if p]
        if jugados_ids:
            try:
                lineas = investigar_livestats(jugados_ids[-1])
                ok = [l for l in lineas if "HTTP 200" in l]
                resumen.append(f"Detective LiveStats: {len(ok)} servicios responden (ver registro)")
            except Exception as exc:  # noqa: BLE001
                resumen.append(f"Detective PBP: error ({exc})")

    if nombres_raros:
        print(f"  [nombres] equipos NO reconocidos ({len(nombres_raros)}): "
              f"{', '.join(sorted(nombres_raros)[:20])}", file=sys.stderr)
        resumen.append(f"Nombres de equipo sin reconocer: {len(nombres_raros)} (ver registro)")

    resumen.append(f"Duracion: {int((time.time() - INICIO_EJECUCION) / 60)} min")
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
