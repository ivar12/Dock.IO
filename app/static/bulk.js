// Selection controls for the bulk-action bars on list pages.
(function () {
  "use strict";

  document.querySelectorAll("[data-bulk-form]").forEach(function (bar) {
    var form = document.getElementById(bar.getAttribute("data-bulk-form"));
    if (!form) return;

    var scope = bar.closest("[data-filter-scope]") || document;
    var boxes = Array.prototype.slice
      .call(scope.querySelectorAll("input[data-bulk-item]"))
      .filter(function (box) {
        return box.getAttribute("form") === form.id;
      });
    var selectAll = bar.querySelector("[data-select-all]");
    var action = bar.querySelector("[data-bulk-action]");
    var submit = bar.querySelector("[data-bulk-submit]");
    var count = bar.querySelector("[data-bulk-count]");

    function selected() {
      return boxes.filter(function (box) { return box.checked; });
    }

    function visible(box) {
      var row = box.closest("tr");
      return !row || !row.hidden;
    }

    function update() {
      var chosen = selected();
      var shown = boxes.filter(visible);
      if (count) count.textContent = chosen.length + " selected";
      if (submit) submit.disabled = chosen.length === 0;
      if (selectAll) {
        selectAll.checked = shown.length > 0 && shown.every(function (box) { return box.checked; });
        selectAll.indeterminate =
          shown.some(function (box) { return box.checked; }) && !selectAll.checked;
      }
    }

    boxes.forEach(function (box) { box.addEventListener("change", update); });
    if (selectAll) {
      selectAll.addEventListener("change", function () {
        boxes.forEach(function (box) {
          if (visible(box)) box.checked = selectAll.checked;
        });
        update();
      });
    }
    if (submit) {
      submit.addEventListener("click", function (event) {
        var amount = selected().length;
        if (!amount) {
          event.preventDefault();
          return;
        }
        var label = action && action.options[action.selectedIndex]
          ? action.options[action.selectedIndex].text
          : "Apply";
        if (!window.confirm(label + " " + amount + " selected item" + (amount === 1 ? "?" : "s?"))) {
          event.preventDefault();
        }
      });
    }
    scope.addEventListener("filter:applied", update);
    update();
  });
})();
