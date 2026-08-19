// The browser half of shiny-plotly: an output binding that draws render_plotly values, and
// a size tracker shared with fig_to_ui fragments.
//
// The binding keeps one plotly graph div per output. The first figure is drawn with
// Plotly.newPlot; every later one with Plotly.react, which diffs the figure into the graph
// that is already there instead of tearing it down. So the DOM node, its event handlers
// (post_script runs once) and plotly's per-graph state survive a re-render, and a figure
// that sets layout.uirevision keeps the zoom and pan the user dragged.
//
// Plotly re-measures a responsive graph only on window resize, and it registers one window
// listener per graph div. So a graph whose card changes size without a window resize (a
// sibling output rendering, a sidebar toggle) stays at its old size, and a graph removed
// from the page is kept alive by that listener forever. One ResizeObserver covers both: a
// size change re-lays the graph out, and the notification for a detached element (size 0x0,
// no longer in the document) purges it, which removes the listener and plotly's state.
(function () {
  "use strict";

  var FILL_BASIS = "400px"; // height of a filling plot when nothing constrains it
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

  function track(gd) {
    if (!gd) return;
    if (observer === null && typeof ResizeObserver === "function") {
      observer = new ResizeObserver(onResize);
    }
    if (observer !== null) observer.observe(gd);
  }

  function release(gd) {
    if (observer !== null) observer.unobserve(gd);
    if (window.Plotly && gd._fullLayout) window.Plotly.purge(gd);
  }

  // --- the output binding -------------------------------------------------------------

  function graphDiv(el) {
    return el.querySelector(":scope > .shiny-plotly > .plotly-graph-div");
  }

  function clear(el) {
    var gd = graphDiv(el);
    if (gd) release(gd);
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  // Builds the container and graph div for an output that has none yet; the container
  // carries the size options and the fill class, the graph div fills the container.
  function create(el, value) {
    var container = document.createElement("div");
    container.className = value.height ? "shiny-plotly" : "shiny-plotly html-fill-item";
    container.style.height = value.height || FILL_BASIS;
    container.style.width = value.width || "100%";
    var gd = document.createElement("div");
    gd.id = el.id + "-plotly";
    gd.className = "plotly-graph-div";
    gd.style.height = "100%";
    gd.style.width = "100%";
    container.appendChild(gd);
    el.appendChild(container);
    return gd;
  }

  function runPostScript(gd, script) {
    if (!script) return;
    /* eslint-disable-next-line no-new-func */
    new Function(script.replace(/\{plot_id\}/g, gd.id))();
  }

  function draw(el, value) {
    if (value === null || value === undefined) {
      clear(el);
      return Promise.resolve();
    }
    if (!window.Plotly) {
      throw new Error("plotly.js is not loaded; use output_plotly() for render_plotly outputs");
    }
    var figure = JSON.parse(value.figure);
    figure.config = value.config;
    var gd = graphDiv(el);
    if (gd) {
      return window.Plotly.react(gd, figure);
    }
    clear(el); // an error message may be showing
    gd = create(el, value);
    return window.Plotly.newPlot(gd, figure).then(function () {
      track(gd);
      runPostScript(gd, value.post_script);
    });
  }

  function register() {
    if (!window.Shiny || !window.Shiny.OutputBinding) return false;
    var binding = new window.Shiny.OutputBinding();
    binding.find = function (scope) {
      return window.jQuery(scope).find(".shiny-plotly-output");
    };
    binding.renderValue = function (el, value) {
      return draw(el, value);
    };
    // Shiny's default renderError writes the message over the output's children, which
    // would leave plotly's state behind; release the graph first. The classes match the
    // default so Shiny's error styling applies.
    binding.renderError = function (el, err) {
      this.clearError(el);
      clear(el);
      if (err.message === "") return;
      el.classList.add("shiny-output-error");
      if (err.type) {
        var types = Array.isArray(err.type) ? err.type : [err.type];
        for (var i = 0; i < types.length; i++) el.classList.add("shiny-output-error-" + types[i]);
      }
      el.textContent = err.message;
    };
    window.Shiny.outputBindings.register(binding, "shiny-plotly.output");
    return true;
  }

  window.shinyPlotly = {
    // Called from a fig_to_ui fragment right after Plotly.newPlot resolves.
    track: track
  };

  if (!register()) {
    document.addEventListener("DOMContentLoaded", register);
  }
})();
