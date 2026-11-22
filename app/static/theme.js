// Light/dark theme switcher; the initial theme is set inline in base.html.
(function () {
  "use strict";

  var root = document.documentElement;
  var button = document.getElementById("theme-toggle");
  if (!button) return;

  function refreshLabel() {
    var light = root.getAttribute("data-theme") === "light";
    button.textContent = light ? "☀️ Light" : "🌙 Dark";
    button.title = "Switch to " + (light ? "dark" : "light") + " mode";
    button.setAttribute("aria-label", button.title);
  }

  button.addEventListener("click", function () {
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try {
      localStorage.setItem("dockio-theme", next);
    } catch (error) {
      // Storage can be disabled; the theme still applies for this page view.
    }
    refreshLabel();
  });

  refreshLabel();
})();
