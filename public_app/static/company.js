(() => {
  "use strict";

  function fallbackCopy(value) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("copy command failed");
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-button]");
    if (!button) return;
    const value = button.getAttribute("data-copy-value") || "";
    if (!value) return;
    const status = button.parentElement?.querySelector("[data-copy-status]");
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        fallbackCopy(value);
      }
      if (status) status.textContent = "Скопировано";
    } catch (_error) {
      if (status) status.textContent = "Не удалось скопировать";
    }
  });
})();
