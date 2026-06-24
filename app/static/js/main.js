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
