// Live image-update badges on the container list.
(function () {
  "use strict";

  if (!document.querySelector("[data-container-update]")) return;

  function apply(data) {
    var count = document.getElementById("updates-available-count");
    if (count) count.textContent = data.available != null ? data.available : 0;
    var stat = document.getElementById("updates-available-stat");
    if (stat) stat.classList.toggle("warn", Boolean(data.available));
    var state = document.getElementById("updates-check-state");
    if (state) state.textContent = data.busy ? "checking..." : "checked";

    document.querySelectorAll("[data-container-update]").forEach(function (badge) {
      var result = data.states && data.states[badge.getAttribute("data-container-update")];
      if (!result) return;
      badge.textContent = result.label;
      badge.className = "tag " + (result.css || "");
      badge.title = result.detail || "";
    });
  }

  function load() {
    fetch("/containers/update-status", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (!response.ok) throw new Error("status request failed");
        return response.json();
      })
      .then(function (data) {
        if (data && !data.error) apply(data);
      })
      .catch(function () {
        var state = document.getElementById("updates-check-state");
        if (state) state.textContent = "check unavailable";
      });
  }

  load();
  setInterval(load, 5000);
})();
