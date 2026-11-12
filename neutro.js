/* ScoutFlow: textos neutros (jugadores/as) para equipos masculinos y femeninos. Solo cambia lo que se ve; no toca nombres de archivos ni datos. */
(function () {
  if (window.__neutroOn) return; window.__neutroOn = true;
  var R = [
    [/\b(T|t)odas las jugadoras\b/g, function (m, t) { return t + "odos los/las jugadores/as"; }],
    [/\b(L|l)as jugadoras\b/g, function (m, l) { return l + "os/las jugadores/as"; }],
    [/\b(L|l)a jugadora\b/g, function (m, l) { return l === "L" ? "El/la jugador/a" : "el/la jugador/a"; }],
    [/\b(U|u)na jugadora\b/g, function (m, u) { return u + "n/a jugador/a"; }],
    [/\b(E|e)sta jugadora\b/g, function (m, e) { return e + "ste/a jugador/a"; }],
    [/\b(E|e)stas jugadoras\b/g, function (m, e) { return e + "stos/as jugadores/as"; }],
    [/\b(O|o)tras jugadoras\b/g, function (m, o) { return o + "tros/as jugadores/as"; }],
    [/\bJugadoras\b/g, "Jugadores/as"],
    [/\bjugadoras\b/g, "jugadores/as"],
    [/\bJUGADORAS\b/g, "JUGADORES/AS"],
    [/\bJugadora\b/g, "Jugador/a"],
    [/\bjugadora\b/g, "jugador/a"],
    [/\bJUGADORA\b/g, "JUGADOR/A"]
  ];
  function tr(s) { if (!s || !/ugadora/i.test(s)) return s; for (var i = 0; i < R.length; i++) s = s.replace(R[i][0], R[i][1]); return s; }
  var SKIP = /^(SCRIPT|STYLE|TEXTAREA|INPUT|CODE|PRE|NOSCRIPT)$/;
  function ok(n) { for (var p = n.parentNode; p && p.nodeType === 1; p = p.parentNode) { if (SKIP.test(p.tagName) || p.isContentEditable || (p.getAttribute && p.getAttribute("data-neutro") === "no")) return false; } return true; }
  function nodo(n) {
    if (n.nodeType === 3) { var v = n.nodeValue, w = tr(v); if (w !== v && ok(n)) n.nodeValue = w; return; }
    if (n.nodeType !== 1) return;
    ["placeholder", "title", "aria-label", "alt"].forEach(function (a) { var v = n.getAttribute(a); if (v) { var w = tr(v); if (w !== v) n.setAttribute(a, w); } });
    if (SKIP.test(n.tagName)) return;
    for (var c = n.firstChild; c; c = c.nextSibling) nodo(c);
  }
  function todo() { try { document.title = tr(document.title); nodo(document.body); } catch (e) {} }
  function ini() {
    todo();
    var t = 0;
    new MutationObserver(function (ms) {
      clearTimeout(t); t = setTimeout(function () { ms.forEach(function (m) { if (m.type === "characterData") nodo(m.target); m.addedNodes && m.addedNodes.forEach(nodo); if (m.type === "attributes") nodo(m.target); }); }, 30);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  if (document.body) ini(); else document.addEventListener("DOMContentLoaded", ini);
})();
