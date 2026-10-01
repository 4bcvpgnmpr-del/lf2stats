/* LF2 Stats - service worker
   Objetivo: que la app se pueda instalar y abrir rapido,
   pero SIN guardar nunca datos viejos de Google Sheets ni de la FEB.
   Estrategia: red primero, y si no hay internet, lo guardado. */

const CACHE = "lf2-stats-2026-10-18";

const BASICOS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icono-192.png",
  "./icono-512.png",
  "./icono-180.png"
];

// Dominios que NUNCA se guardan en cache (datos que deben estar al dia)
function esDatosEnVivo(url) {
  return url.includes("google.com") ||
         url.includes("googleusercontent.com") ||
         url.includes("gstatic.com/charts") ||
         url.includes("feb.es") ||
         url.includes("youtube.com") ||
         url.includes("vimeo.com");
}

self.addEventListener("install", (evento) => {
  evento.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(BASICOS).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((claves) => Promise.all(
        claves.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (evento) => {
  const peticion = evento.request;

  if (peticion.method !== "GET") return;
  if (esDatosEnVivo(peticion.url)) return;          // va directo a la red
  if (!peticion.url.startsWith("http")) return;

  evento.respondWith(
    fetch(peticion)
      .then((respuesta) => {
        if (respuesta && respuesta.status === 200 &&
            (respuesta.type === "basic" || respuesta.type === "cors")) {
          const copia = respuesta.clone();
          caches.open(CACHE).then((c) => c.put(peticion, copia)).catch(() => {});
        }
        return respuesta;
      })
      .catch(() =>
        caches.match(peticion).then((guardada) =>
          guardada || caches.match("./index.html")
        )
      )
  );
});
