/* ScoutFlow - service worker
   Objetivo: que la app se abra SIEMPRE, también sin internet.
   - Las páginas de la app y sus librerías se guardan al instalar.
   - Con internet: se pide la versión nueva (máx. 4 s) y se guarda; sin internet o si tarda, se usa lo guardado.
   - Los datos en vivo (Supabase, FEB, Google Sheets, vídeos) nunca se guardan aquí: la app ya guarda su propia copia. */

const CACHE = "scoutflow-2026-11-18b";
const IMG = "scoutflow-img";   /* fotos y escudos: se guardan aparte y no se borran al actualizar */
const IMG_MAX = 1500;

const BASICOS = [
  "./",
  "./index.html",
  "./entrenador.html",
  "./pizarra.html",
  "./jugadoras.html",
  "./panel.html",
  "./neutro.js",
  "./manifest.json",
  "./sf-192.png",
  "./sf-512.png",
  "./sf-180.png"
];
/* librerías de fuera (versión fija: se pueden guardar para siempre) */
const LIBRERIAS = [
  "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.0/dist/umd/supabase.js",
  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
];

// Lo que NUNCA se guarda (datos que deben estar al día o archivos enormes)
function noGuardar(url) {
  return url.includes("supabase.co") ||
         url.includes("workers.dev") ||
         url.includes("docs.google.com") ||
         url.includes("sheets.googleapis.com") ||
         url.includes("googleusercontent.com") ||
         url.includes("gstatic.com/charts") ||
         url.includes("feb.es") ||
         url.includes("youtube.com") ||
         url.includes("vimeo.com");
}
// Librerías y tipos de letra: no cambian, se sirven de lo guardado (más rápido y sin red)
function esFijo(url) {
  return url.includes("cdn.jsdelivr.net") || url.includes("cdnjs.cloudflare.com") ||
         url.includes("fonts.googleapis.com") || url.includes("fonts.gstatic.com") || url.includes("unpkg.com");
}

self.addEventListener("install", (evento) => {
  evento.waitUntil((async () => {
    const c = await caches.open(CACHE);
    // de uno en uno: si falta un archivo, el resto se guarda igual
    await Promise.all(BASICOS.map((u) => c.add(new Request(u, { cache: "reload" })).catch(() => {})));
    await Promise.all(LIBRERIAS.map((u) => c.add(u).catch(() => c.add(new Request(u, { mode: "no-cors" })).catch(() => {}))));
  })());
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((claves) => Promise.all(claves.filter((k) => k !== CACHE && k !== IMG).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const guardar = (peticion, respuesta) => {
  if (respuesta && (respuesta.ok || respuesta.type === "opaque")) {
    const copia = respuesta.clone();
    caches.open(CACHE).then((c) => c.put(peticion, copia)).catch(() => {});
  }
  return respuesta;
};
async function deCache(peticion) {
  return (await caches.match(peticion)) || (await caches.match(peticion, { ignoreSearch: true }));
}
async function paginaGuardada(url) {
  const u = new URL(url), nombre = u.pathname.split("/").pop() || "index.html";
  return (await caches.match("./" + nombre)) || (await caches.match(u.pathname, { ignoreSearch: true })) ||
         (nombre.includes("entrenador") ? null : await caches.match("./index.html")) || (await caches.match("./entrenador.html"));
}

/* ---- fotos y escudos ---- */
const esImagen = (p) => p.destination === "image" || /\.(png|jpe?g|gif|webp|svg|avif)(\?|$)/i.test(p.url);
async function recortarImg() { try { const c = await caches.open(IMG), k = await c.keys(); for (let i = 0; i < k.length - IMG_MAX; i++) await c.delete(k[i]); } catch (e) { /* nada */ } }
async function bajarImg(url) {
  // primero con CORS (ocupa poco); si el servidor no lo permite, copia "opaca" (se ve igual en la app)
  let r = null; try { r = await fetch(url, { mode: "cors", credentials: "omit" }); if (!r.ok) r = null; } catch (e) { r = null; }
  if (!r) { try { r = await fetch(url, { mode: "no-cors", credentials: "omit" }); } catch (e) { r = null; } }
  if (r && (r.ok || r.type === "opaque")) { const c = await caches.open(IMG); await c.put(url, r.clone()); }
  return r;
}
async function servirImg(peticion) {
  const c = await caches.open(IMG), g = await c.match(peticion.url);
  if (g) { if (navigator.onLine) bajarImg(peticion.url).catch(() => {}); return g; }   // al instante y se actualiza por detrás
  try { const r = await fetch(peticion); if (r && (r.ok || r.type === "opaque")) { c.put(peticion.url, r.clone()).catch(() => {}); recortarImg(); } return r; }
  catch (e) { return new Response("", { status: 504 }); }
}
/* la app manda la lista de fotos y escudos que quiere tener guardados para usar sin conexión */
self.addEventListener("message", (ev) => {
  const l = ev.data && ev.data.guardarImagenes; if (!Array.isArray(l)) return;
  ev.waitUntil((async () => { const c = await caches.open(IMG); let n = 0;
    for (const u of [...new Set(l)].slice(0, IMG_MAX)) { if (!/^https?:/.test(u)) continue; if (await c.match(u)) continue; await bajarImg(u).catch(() => {}); n++; }
    await recortarImg(); if (ev.source) ev.source.postMessage({ imagenesGuardadas: n });
  })());
});

self.addEventListener("fetch", (evento) => {
  const peticion = evento.request, url = peticion.url;
  if (peticion.method !== "GET" || !url.startsWith("http")) return;
  if (esImagen(peticion) && !url.includes("workers.dev") && !peticion.headers.get("range")) { evento.respondWith(servirImg(peticion)); return; }
  if (noGuardar(url)) return;                       // va directo a la red
  if (peticion.headers.get("range")) return;        // trozos de vídeo: directo

  if (esFijo(url)) {
    evento.respondWith(deCache(peticion).then((g) => g || fetch(peticion).then((r) => guardar(peticion, r))));
    return;
  }

  // páginas y archivos propios: red primero (máx. 4 s), si no, lo guardado
  evento.respondWith((async () => {
    const red = fetch(peticion, { cache: "no-cache" }).then((r) => guardar(peticion, r));
    try {
      return await Promise.race([red, new Promise((_, no) => setTimeout(() => no(new Error("lento")), 4000))]);
    } catch (e) {
      const g = await deCache(peticion);
      if (g) return g;
      if (peticion.mode === "navigate") { const p = await paginaGuardada(url); if (p) return p; }
      try { return await red; } catch (e2) { /* sin red y sin copia */ }
      return new Response("Sin conexión y sin copia guardada de esta página. Ábrela una vez con internet.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }
  })());
});
