# Automatización de estadísticas LF2 (FEB) → Google Sheets

Esto descarga automáticamente las estadísticas por equipo, los rankings por
jugadora y los resultados de la Liga Femenina 2 (LF2) desde la web de la FEB,
y las escribe en un Google Sheet tuyo. Se ejecuta solo 3 veces por semana
(gratis, con GitHub Actions) sin que tengas que tener nada encendido.

## Cómo funciona (resumen técnico)

La web principal de la FEB carga las tablas con JavaScript, pero existe un
portal alternativo — `feb.es/competiciones/...` — que sirve las mismas tablas
como HTML normal. El script usa esas URLs, así que no hace falta un navegador
simulado, solo peticiones HTTP normales.

## Paso 1 — Crear el Google Sheet

1. Crea una hoja de cálculo nueva en Google Sheets (o usa una que ya tengas).
2. Copia el ID de la hoja: es el texto largo que aparece en la URL entre
   `/d/` y `/edit`:
   `https://docs.google.com/spreadsheets/d/ESTE_ES_EL_ID/edit`

## Paso 2 — Crear una cuenta de servicio de Google (para que el script pueda escribir)

1. Ve a [Google Cloud Console](https://console.cloud.google.com/) y crea un
   proyecto nuevo (o usa uno existente).
2. Activa la **Google Sheets API**: menú "APIs y servicios" → "Habilitar
   APIs y servicios" → busca "Google Sheets API" → Habilitar.
3. Ve a "APIs y servicios" → "Credenciales" → "Crear credenciales" →
   "Cuenta de servicio". Dale cualquier nombre, por ejemplo `feb-stats-bot`.
4. Una vez creada, entra en la cuenta de servicio → pestaña "Claves" →
   "Agregar clave" → "Crear clave nueva" → JSON. Se descargará un archivo
   `.json`. **Guárdalo, lo necesitas en el paso 4.**
5. Copia el "email" de la cuenta de servicio (algo como
   `feb-stats-bot@tu-proyecto.iam.gserviceaccount.com`).
6. Vuelve a tu Google Sheet, pulsa "Compartir" y comparte la hoja con ese
   email, dándole permiso de **Editor**.

## Paso 3 — Subir este proyecto a GitHub

1. Crea un repositorio nuevo en GitHub (puede ser privado).
2. Sube todos estos archivos (`feb_stats_sync.py`, `requirements.txt`, la
   carpeta `.github/`, este `README.md`).

## Paso 4 — Configurar los "Secrets" en GitHub

En tu repositorio: Settings → Secrets and variables → Actions → "New
repository secret". Crea estos dos:

- `GOOGLE_SHEET_ID` → el ID que copiaste en el Paso 1.
- `GOOGLE_SERVICE_ACCOUNT_JSON` → abre el archivo `.json` que descargaste en
  el Paso 2 con un editor de texto y pega **todo su contenido** tal cual.

## Paso 5 — Probarlo

En tu repositorio, ve a la pestaña "Actions" → selecciona el workflow
"Actualizar estadisticas LF2" → botón "Run workflow" (esto lo lanza a mano,
sin esperar al horario programado). En 1-2 minutos deberías ver pestañas
nuevas en tu Google Sheet: `Equipos`, `Jugadoras`, `Resultados` y `Log`.

A partir de ahí, se ejecutará solo los lunes, miércoles y viernes a las
07:00 UTC. Puedes cambiar ese horario editando la línea `cron` en
`.github/workflows/actualizar_feb_stats.yml` (usa
[crontab.guru](https://crontab.guru/) para construir la expresión).

## Ajustar a otra temporada o competición

Al principio de `feb_stats_sync.py` (o como variables de entorno en el
workflow) puedes cambiar:

- `FEB_GROUP_ID`: id de la competición. LF2 = `9`. Otros: LF Endesa = `4`,
  Primera FEB = `1`, Segunda FEB = `2`, LF Challenge = `67`, Tercera FEB = `3`.
- `FEB_SEASON_START`: año de inicio de temporada (2025 para la 2025/2026).
- `FEB_SLUG`: el "nm" que usa la URL (`lf2`, `lfendesa`, `primerafeb`,
  `segundafeb`, `lfchallenge`, `tercerafeb`).

## Nota importante

La temporada 2026/2027 de LF2 empieza en octubre — hasta que arranque, las
tablas de la FEB estarán vacías y el script simplemente lo anotará en la
pestaña `Log` sin fallar. En cuanto haya partidos jugados, empezará a
rellenar `Equipos` y `Jugadoras` automáticamente.

## Ejecutarlo en tu ordenador (opcional, para probar sin GitHub)

```bash
pip install -r requirements.txt
export GOOGLE_SHEET_ID="tu_id_de_sheet"
export GOOGLE_SERVICE_ACCOUNT_FILE="ruta/a/tu/archivo.json"
python feb_stats_sync.py
```
