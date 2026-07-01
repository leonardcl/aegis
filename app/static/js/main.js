// Misc UI behaviour
(function () {
  // Live-update priority slider value badges on the procurement form
  document.querySelectorAll(".priority-range").forEach((range) => {
    const target = document.getElementById(range.dataset.target);
    if (!target) return;
    range.addEventListener("input", () => {
      target.textContent = range.value;
    });
  });
})();

// Full-screen loading overlay for slow synchronous form posts (e.g. Hermes parse).
// Opt in by adding `data-loading` to a <form>. Optional copy via data attributes:
//   data-loading-title, data-loading-sub, data-loading-sub-hermes
// The overlay is cleared automatically when the server's redirect loads the next page.
(function () {
  const overlay = document.getElementById("aegis-overlay");
  if (!overlay) return;
  const titleEl = document.getElementById("aegis-overlay-title");
  const subEl = document.getElementById("aegis-overlay-sub");

  function showOverlay(title, sub) {
    if (title) titleEl.textContent = title;
    if (sub) subEl.textContent = sub;
    overlay.hidden = false;
  }
  window.AEGIS = window.AEGIS || {};
  window.AEGIS.showOverlay = showOverlay;

  document.querySelectorAll("form[data-loading]").forEach((form) => {
    form.addEventListener("submit", () => {
      // HTML5 validation runs before this fires, so a submit here means the form is valid.
      const title = form.dataset.loadingTitle || "Working…";
      let sub = form.dataset.loadingSub || "Hermes is processing your request.";
      const hermes = form.querySelector('input[name="use_hermes"]');
      if (hermes && hermes.checked && form.dataset.loadingSubHermes) {
        sub = form.dataset.loadingSubHermes;
      }
      showOverlay(title, sub);
    });
  });

  // Hide the overlay if the page is restored from the back/forward cache,
  // so a spinner from a previous navigation never gets stuck on screen.
  window.addEventListener("pageshow", () => { overlay.hidden = true; });
})();

// Live platform telemetry — polls /healthz and reflects real system state in the
// topbar chips and sidebar footer. Honest: "live" means the model server actually
// answered a reachability probe, not just that a URL is configured.
(function () {
  var url = (window.AEGIS && window.AEGIS.healthUrl);
  if (!url) return;

  function setDot(el, state) {            // state: "up" | "down" | "warn"
    if (!el) return;
    el.classList.remove("up", "down", "warn");
    el.classList.add(state);
  }
  function setChip(chip, ok) {
    if (!chip) return;
    chip.classList.toggle("is-unknown", ok === null);
  }
  function txt(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; }

  function paint(h) {
    var hermesUp = !!(h && h.hermes_live);
    var hermesConf = !!(h && h.hermes_configured);
    var dbUp = !!(h && h.db);

    // Hermes: live (reachable) > configured-but-unreachable (warn) > off
    var hState = hermesUp ? "up" : (hermesConf ? "warn" : "down");
    var hLabel = hermesUp ? "live" : (hermesConf ? "unreachable" : "local");
    setDot(document.getElementById("chip-hermes-dot"), hState);
    setChip(document.getElementById("chip-hermes"), hState !== "down" ? true : false);
    txt("chip-hermes-v", hLabel);
    setDot(document.getElementById("sf-hermes-dot"), hState);
    txt("sf-hermes-val", hLabel);

    setDot(document.getElementById("chip-db-dot"), dbUp ? "up" : "down");
    setChip(document.getElementById("chip-db"), dbUp ? true : false);
    txt("chip-db-v", dbUp ? "ok" : "down");
    setDot(document.getElementById("sf-db-dot"), dbUp ? "up" : "down");
    txt("sf-db-val", dbUp ? "connected" : "down");
  }

  function poll() {
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () {
        setDot(document.getElementById("chip-hermes-dot"), "down");
        setDot(document.getElementById("chip-db-dot"), "down");
      });
  }
  poll();
  setInterval(poll, 15000);
})();
