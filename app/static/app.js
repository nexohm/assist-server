// Script della dashboard. Sta in un file a parte (niente JavaScript dentro le pagine):
// così la Content-Security-Policy può vietare qualsiasi script inserito di nascosto.

// Moduli con conferma: <form data-confirm="Sei sicuro?">
document.addEventListener("submit", function (e) {
  const msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Pagina impianto: si aggiorna da sola quando cambia lo stato dell'impianto o dei comandi.
(function () {
  const el = document.getElementById("plant-live");
  if (!el) return;
  const url = el.dataset.url;
  let first;
  try { first = JSON.parse(el.dataset.state); } catch (e) { return; }
  async function poll() {
    try {
      const r = await fetch(url, {credentials: "same-origin"});
      if (r.ok) {
        const s = await r.json();
        const seen = document.getElementById("seen");
        if (seen) seen.textContent = s.last_seen;
        const cmds = s.commands.map(c => c.status);
        if (s.status !== first.status || s.last_diag_at !== first.diag || JSON.stringify(cmds) !== JSON.stringify(first.cmds)) {
          location.reload(); return;
        }
      }
    } catch (e) {}
    setTimeout(poll, 4000);
  }
  setTimeout(poll, 4000);
})();
