// Live view of tracked container updates and Dock.IO release status.
(function () {
  "use strict";

  var tbody = document.getElementById("update-rows");
  if (!tbody) return;

  var counter = document.getElementById("update-active-count");
  var refresh = document.getElementById("update-refresh");
  var intervalMs = 2000;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function shortTime(value) {
    return String(value || "").replace("T", " ").slice(0, 19);
  }

  function row(job) {
    var state = job.finished_at ? (job.ok ? "done" : "failed") : "active";
    var percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
    var badgeClass = state === "done" ? "yes" : state === "failed" ? "no" : "";
    return (
      '<tr class="update-job ' + state + '">' +
      "<td><strong>" + esc(job.container) + "</strong></td>" +
      '<td><span class="tag ' + badgeClass + '">' + esc(job.label || job.phase) + "</span></td>" +
      '<td><div class="progress"><span style="width:' + percent + '%"></span></div>' +
      '<span class="muted small-text">' + percent + "%</span></td>" +
      '<td class="mono wrap">' + esc(job.detail || "-") + "</td>" +
      '<td class="mono small-text">' + esc(job.started_display || shortTime(job.started_at)) + "</td>" +
      '<td class="mono small-text">' + esc(job.finished_display || "-") + "</td>" +
      '<td class="small-text">' + esc(job.actor) + "</td>" +
      "</tr>"
    );
  }

  function render(data) {
    if (counter) {
      counter.textContent = (data.active ? data.active.length : 0) + " active";
    }
    var jobs = data.jobs || [];
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">No recent updates.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(row).join("");
  }

  function load() {
    fetch("/updates/status", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (!response.ok) throw new Error("status request failed");
        return response.json();
      })
      .then(function (data) {
        if (data) render(data);
      })
      .catch(function () {
        if (counter) counter.textContent = "connection lost";
      });
  }

  if (refresh) refresh.addEventListener("click", load);
  load();
  setInterval(load, intervalMs);
})();
