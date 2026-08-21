// copyText writes `text` to the clipboard and returns true on success.
//
// The device web UI is served over plain HTTP on LAN (http://<pi-ip>/...), NOT
// HTTPS or localhost, so `navigator.clipboard` is undefined per the Async
// Clipboard API's secure-context requirement. Silently falls back to the
// hidden-textarea + `document.execCommand("copy")` path, which still works on
// http:// origins, so copy buttons actually copy instead of no-oping.
//
// Callers should await the returned boolean and toast success/failure — an
// unavailable fallback (very old browser) is the only case where this returns
// false; both modern paths succeed on our supported browsers.
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Rare permission / focus errors — fall through to the legacy path.
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep off-screen so a copy in the middle of a scrolled page doesn't yank
    // focus or scroll position around.
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.opacity = "0";
    ta.setAttribute("readonly", "");
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
