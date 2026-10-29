/* Report switcher: open the same page in the chosen report. */
(function () {
  "use strict";
  var sel = document.getElementById("report-switch");
  if (!sel) return;
  sel.onchange = function () {
    var page = sel.getAttribute("data-page") + ".html";
    window.location.href = sel.getAttribute("data-root") + sel.value + page + window.location.hash;
  };
})();
