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

    const typing = addMessage("Hermes is thinking…", "assistant typing");

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
