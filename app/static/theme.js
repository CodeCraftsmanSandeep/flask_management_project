"use strict";

/*
 * Theme switch. Loaded synchronously in <head> (before the page paints) so a saved light theme
 * never flashes dark first. Dark is the default. Pages that draw on canvas listen for the
 * "themechange" event to recolour themselves.
 */
(() => {
  const KEY = "gts-theme";
  const root = document.documentElement;

  function saved() {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "dark";
    } catch {
      return "dark"; // storage blocked: still works, just not remembered
    }
  }

  function label(theme) {
    return theme === "dark" ? "Light theme" : "Dark theme";
  }

  function apply(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = label(theme);
      button.setAttribute("aria-label", `Switch to ${label(theme).toLowerCase()}`);
    }
  }

  apply(saved());

  document.addEventListener("DOMContentLoaded", () => {
    apply(root.dataset.theme === "light" ? "light" : "dark");
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", () => {
      const next = root.dataset.theme === "light" ? "dark" : "light";
      try {
        localStorage.setItem(KEY, next);
      } catch {
        // not remembered; the switch still applies for this visit
      }
      apply(next);
      window.dispatchEvent(new CustomEvent("themechange", { detail: next }));
    });
  });
})();
