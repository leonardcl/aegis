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

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;

    addMessage(text, "user");
    input.value = "";

    const typing = addMessage("Hermes is thinking…", "assistant typing");

    try {
      const res = await fetch(window.AEGIS.chatUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, context: window.AEGIS.page }),
      });
      const data = await res.json();
      typing.remove();
      addMessage(data.reply || "No response.", "assistant");
    } catch (err) {
      typing.remove();
      addMessage("Sorry, I couldn't reach the server.", "assistant");
    }
  });
})();
