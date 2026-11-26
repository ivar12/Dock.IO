// Live container logs with a user-controlled auto-refresh toggle.
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) return;
  var name = script.getAttribute("data-container") || "";

  var toggle = document.getElementById("logs-toggle");
  var refresh = document.getElementById("logs-refresh");
  var auto = document.getElementById("logs-auto");
  var autoLabel = document.getElementById("logs-auto-label");
  var output = document.getElementById("logs-output");
  var hint = document.getElementById("logs-hint");
  if (!toggle || !refresh || !auto || !autoLabel || !output) return;

  var timer = null;
  var intervalMs = 2000;

  function stopAuto() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function startAuto() {
    if (timer === null && auto.checked) {
      timer = window.setInterval(load, intervalMs);
    }
  }

  function load() {
    fetch("/container/" + encodeURIComponent(name) + "/logs?tail=200", {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (!response.ok) throw new Error("log request failed");
        return response.json();
      })
      .then(function (data) {
        if (!data) return;
        if (data.error) {
          output.textContent = "Could not load logs: " + data.error;
          return;
        }
        var lines = data.lines || [];
        output.textContent = lines.length ? lines.join("\n") : "No log lines yet.";
        output.scrollTop = output.scrollHeight;
      })
      .catch(function (error) {
        output.textContent = "Could not load logs: " + error.message;
      });
  }

  toggle.addEventListener("click", function () {
    var opening = output.hidden;
    output.hidden = !opening;
    refresh.hidden = !opening;
    autoLabel.hidden = !opening;
    toggle.textContent = opening ? "Hide logs" : "View live logs";
    toggle.setAttribute("aria-expanded", opening ? "true" : "false");
    if (opening) {
      if (hint) hint.hidden = true;
      load();
      startAuto();
    } else {
      stopAuto();
      if (hint) hint.hidden = false;
    }
  });

  auto.addEventListener("change", function () {
    stopAuto();
    if (auto.checked) {
      load();
      startAuto();
    }
  });

  refresh.addEventListener("click", load);
})();
