/** Only render a link for plain https CRTC URLs; everything else is shown as text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && (u.hostname === "crtc.gc.ca" || u.hostname.endsWith(".crtc.gc.ca"))
      ? u.toString()
      : null;
  } catch {
    return null;
  }
}
