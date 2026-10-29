/* Reports page: one measure for every system across reports and scale factors. */
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("site-data").textContent);
  var sel = document.getElementById("cmp-measure"), host = document.getElementById("cmp-chart");
  var table = document.getElementById("cmp-table");
  if (!sel || !host) return;
  var chart = echarts.init(host);

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function fmt(v) {
    if (v == null) return "";
    var a = Math.abs(v);
    return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  }
  function ink() { return getComputedStyle(document.body).color; }
  /* Columns: report x scale factor. Systems: every system seen, in the site's order. */
  function columns() {
    var cols = [];
    DATA.reports.slice().reverse().forEach(function (r) {
      Object.keys(r.summary).sort(function (a, b) { return a - b; }).forEach(function (sf) {
        cols.push({label: r.id + " SF" + sf, per: r.summary[sf], systems: r.systems});
      });
    });
    return cols;
  }
  function systemsOf(cols) {
    var seen = {}, out = [];
    cols.forEach(function (c) { c.systems.forEach(function (s) { if (!seen[s.name]) { seen[s.name] = 1; out.push(s); } }); });
    return out;
  }
  /* How many queries a total covers, shown beside it: totals over different sets differ in kind. */
  var NOTES = {
    common_total: "Each column sums the queries every integration answered correctly in that report; the count is shown as (n q).",
    own_total: "Each system sums only the queries it answered correctly, shown as (ok/total); a system that fails more queries has a smaller total.",
    overhead: "Geometric mean over the queries every integration answered correctly in that report."
  };
  function coverage(m, e) {
    if (!e || e[m] == null) return "";
    if (m === "own_total") return " (" + e.ok + "/" + e.queries + ")";
    if (m === "common_total" || m === "overhead") return " (" + e.n_common + " q)";
    return "";
  }
  function draw() {
    var m = sel.value, cols = columns(), systems = systemsOf(cols);
    var note = document.getElementById("cmp-note");
    if (note) note.textContent = NOTES[m] || "";
    var series = systems.map(function (s) {
      return {name: s.label, type: "bar", barMaxWidth: 24, itemStyle: {color: s.color},
        data: cols.map(function (c) {
          var e = c.per[s.name];
          return e && e[m] != null ? {value: e[m], cover: coverage(m, e)} : null;
        })};
    }).filter(function (s) { return s.data.some(function (v) { return v != null; }); });
    chart.setOption({
      animation: false, textStyle: {color: ink()}, tooltip: {trigger: "item", confine: true, formatter: function (p) {
        return "<b>" + esc(p.seriesName) + "</b><br>" + esc(p.name) + ": " + esc(fmt(p.value) + (p.data.cover || "")); }},
      legend: {bottom: 0, textStyle: {color: ink()}},
      grid: {left: 16, right: 24, top: 24, bottom: 80, containLabel: true},
      xAxis: {type: "category", data: cols.map(function (c) { return c.label; }), axisLabel: {color: ink()}},
      yAxis: {type: /_total$/.test(m) ? "log" : "value", axisLabel: {color: ink()}},
      series: series
    }, true);
    table.innerHTML = "<thead><tr><th>System</th>" + cols.map(function (c) { return "<th>" + esc(c.label) + "</th>"; }).join("") +
      "</tr></thead><tbody>" + systems.map(function (s) {
        return "<tr><th scope=\"row\">" + esc(s.label) + "</th>" + cols.map(function (c) {
          var e = c.per[s.name]; return '<td class="num">' + esc(e && e[m] != null ? fmt(e[m]) + coverage(m, e) : "") + "</td>";
        }).join("") + "</tr>";
      }).join("") + "</tbody>";
  }
  sel.onchange = draw;
  window.addEventListener("resize", function () { chart.resize(); });
  draw();
})();
