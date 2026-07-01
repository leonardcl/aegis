// Floating Hermes Agent chatbot — talks to /agent/chat
(function () {
  const fab = document.getElementById("hermes-fab");
  const panel = document.getElementById("hermes-panel");
  const closeBtn = document.getElementById("hermes-close");
  const form = document.getElementById("hermes-form");
  const input = document.getElementById("hermes-text");
  const messages = document.getElementById("hermes-messages");

  if (!fab || !panel) return;

  function togglePanel(show) {
    panel.hidden = show === undefined ? !panel.hidden : !show;
    if (!panel.hidden) input && input.focus();
  }

  fab.addEventListener("click", () => togglePanel());
  closeBtn && closeBtn.addEventListener("click", () => togglePanel(false));

  function addMessage(text, role) {
    const el = document.createElement("div");
    el.className = "hermes-msg " + role;
    el.textContent = text;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
  }

  // Small technical caption under an assistant reply: which engine answered
  // (real Hermes vs the deterministic local reasoner) and how long it took.
  function addMeta(engine, ms) {
    const el = document.createElement("div");
    el.className = "hermes-meta";
    const live = engine === "hermes" || engine === "live";
    const dot = document.createElement("span");
    dot.className = "live-dot " + (live ? "up" : "warn");
    el.appendChild(dot);
    const label = live ? "real Hermes" : (engine || "local");
    el.appendChild(document.createTextNode(label + (ms ? " · " + (ms / 1000).toFixed(1) + "s" : "")));
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
  }

  const sendBtn = form.querySelector('button[type="submit"]');
  let inFlight = false;

  function setBusy(busy) {
    inFlight = busy;
    if (sendBtn) sendBtn.disabled = busy;
    input.disabled = busy;
    if (!busy) input.focus();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (inFlight) return;                 // ignore double-submits while waiting
    const text = input.value.trim();
    if (!text) return;

    addMessage(text, "user");
    input.value = "";
    setBusy(true);

    const typing = addMessage("Hermes is thinking", "assistant typing");
    const dots = document.createElement("span");
    dots.className = "dots";
    typing.appendChild(dots);
    const t0 = (window.performance && performance.now) ? performance.now() : Date.now();

    // Abort the request if it outruns the server-side chat timeout, so the
    // panel never hangs when the model is busy with a council call.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);

    try {
      const res = await fetch(window.AEGIS.chatUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, context: window.AEGIS.page }),
        signal: controller.signal,
      });
      const data = await res.json();
      typing.remove();
      addMessage(data.reply || "No response.", "assistant");
      const elapsed = ((window.performance && performance.now) ? performance.now() : Date.now()) - t0;
      addMeta(data.engine, Math.round(elapsed));
    } catch (err) {
      typing.remove();
      addMessage(
        err && err.name === "AbortError"
          ? "That took too long (the agent may be running an audit). Please try again in a moment."
          : "Sorry, I couldn't reach the server.",
        "assistant"
      );
    } finally {
      clearTimeout(timer);
      setBusy(false);
    }
  });
})();
