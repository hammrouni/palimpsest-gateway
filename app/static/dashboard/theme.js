/* Colour theme: the only file that touches localStorage, and it stores nothing
   but "light" or "dark" (app.js keeps the operator key out of persistent
   storage; a test checks it never names localStorage).

   Loaded without defer from <head> so a saved choice applies before the first
   paint and dark never flashes light first. With no saved choice the system
   theme applies. app.js draws the toggle and calls window.saveTheme. */
(function () {
  const KEY = 'pgw-theme';
  const root = document.documentElement;

  try {
    const saved = window.localStorage.getItem(KEY);
    if (saved === 'light' || saved === 'dark') {
      root.setAttribute('data-theme', saved);
    }
  } catch (err) {
    /* Storage blocked: follow the system theme. */
  }

  window.saveTheme = function (theme) {
    if (theme !== 'light' && theme !== 'dark') {
      return;
    }
    root.setAttribute('data-theme', theme);
    try {
      window.localStorage.setItem(KEY, theme);
    } catch (err) {
      /* Storage blocked: the choice lasts for this page only. */
    }
  };
})();
