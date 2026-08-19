// Keeps every shiny-plotly graph sized to its container, and releases a graph once Shiny
// has replaced the output that held it.
//
// Plotly re-measures a responsive graph only on window resize, and it registers one window
// listener per graph div. So a graph whose card changes size without a window resize (a
// sibling output rendering, a sidebar toggle) stays at its old size, and a graph Shiny has
// re-rendered is kept alive by that listener forever. One ResizeObserver covers both: a
// size change re-lays the graph out, and the notification for a detached element (size 0x0,
// no longer in the document) purges it, which removes the listener and plotly's state.
(function () {
  "use strict";

  var observer = null;

  function differs(actual, laidOut) {
    // Same tolerance plotly's own autosize applies before it redraws.
    return Math.abs(actual - laidOut) > 1;
  }

  function onResize(entries) {
    for (var i = 0; i < entries.length; i++) {
      var gd = entries[i].target;
      if (!gd.isConnected) {
        observer.unobserve(gd);
        if (window.Plotly) window.Plotly.purge(gd);
        continue;
      }
      var rect = entries[i].contentRect;
      var layout = gd._fullLayout;
      if (!layout || (rect.width === 0 && rect.height === 0)) continue;
      if (differs(rect.width, layout.width) || differs(rect.height, layout.height)) {
        window.Plotly.Plots.resize(gd);
      }
    }
  }

  window.shinyPlotly = {
    // Called from the fragment right after Plotly.newPlot resolves, with the graph div.
    track: function (gd) {
      if (!gd) return;
      if (observer === null && typeof ResizeObserver === "function") {
        observer = new ResizeObserver(onResize);
      }
      if (observer !== null) observer.observe(gd);
    }
  };
})();
