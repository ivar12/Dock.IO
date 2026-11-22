// Client-side search + filters for the list pages.
(function () {
  "use strict";

  function matches(item, query, criteria) {
    var haystack = (item.getAttribute("data-search") || item.textContent || "").toLowerCase();
    if (query && haystack.indexOf(query) === -1) return false;
    return criteria.every(function (c) {
      return !c.value || item.getAttribute("data-" + c.key) === c.value;
    });
  }

  document.querySelectorAll("[data-filter-scope]").forEach(function (scope) {
    var items = Array.prototype.slice.call(scope.querySelectorAll("[data-filter-item]"));
    var search = scope.querySelector("[data-filter-search]");
    var selects = Array.prototype.slice.call(scope.querySelectorAll("select[data-filter-key]"));
    var counter = scope.querySelector("[data-filter-count]");
    var empty = scope.querySelector("[data-filter-empty]");
    var clear = scope.querySelector("[data-filter-clear]");

    function apply() {
      var query = (search ? search.value : "").trim().toLowerCase();
      var criteria = selects.map(function (select) {
        return { key: select.getAttribute("data-filter-key"), value: select.value };
      });
      var shown = 0;
      items.forEach(function (item) {
        var visible = matches(item, query, criteria);
        item.hidden = !visible;
        if (visible) shown += 1;
      });
      if (counter) counter.textContent = shown + " / " + items.length;
      if (empty) empty.hidden = shown !== 0;
    }

    if (search) search.addEventListener("input", apply);
    selects.forEach(function (select) { select.addEventListener("change", apply); });
    if (clear) {
      clear.addEventListener("click", function () {
        if (search) search.value = "";
        selects.forEach(function (select) { select.value = ""; });
        apply();
        if (search) search.focus();
      });
    }
    apply();
  });
})();
