import { useEffect, useState } from "react";

type Theme = "dark" | "light";

/**
 * App-wide theme state, synchronized with next-themes.
 *
 * next-themes (see theme-provider.tsx) is the single source of truth: it toggles the
 * `dark` class on <html> and persists to localStorage under the `theme` key. This hook
 * mirrors that exact storage + class contract so the Lamp Monitor pages (Login, Setup,
 * Monitor, EditConfig) — which were written before next-themes and apply their own
 * `lm-light`/`lm-dark` class to `.lm-root` — switch in lockstep with the rest of the app.
 *
 * Default is DARK (matches defaultTheme="dark" in theme-provider.tsx).
 */
const STORAGE_KEY = "theme";

function read(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light") return "light";
  } catch {
    /* ignore */
  }
  return "dark";
}

function apply(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* ignore */
  }
}

/** Returns [theme, toggle, themeClass] — add themeClass to .lm-root */
export function useTheme(): [Theme, () => void, string] {
  const [theme, setTheme] = useState<Theme>(read);

  // Keep <html>.dark + storage in sync, and react to changes made elsewhere
  // (e.g. the shadcn ThemeToggle via next-themes, or another tab).
  useEffect(() => {
    apply(theme);
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === "dark" || e.newValue === "light")) {
        setTheme(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [theme]);

  const toggle = () => setTheme((prev) => (prev === "dark" ? "light" : "dark"));

  const cls = theme === "light" ? "lm-light" : "lm-dark";

  return [theme, toggle, cls];
}
