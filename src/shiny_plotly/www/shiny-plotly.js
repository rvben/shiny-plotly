// The browser half of shiny-plotly: an output binding that draws render_plotly values and
// forwards plotly events to Shiny inputs, a handler that applies the in-place updates sent
// by extend_traces, restyle and relayout, and a size tracker shared with fig_to_ui fragments.
//
// The binding keeps one plotly graph div per output. The first figure is drawn with
// Plotly.newPlot; every later one with Plotly.react, which diffs the figure into the graph
// that is already there instead of tearing it down. So the DOM node, its event handlers
// (post_script and the events option attach once) and plotly's per-graph state survive a
// re-render, and a figure that sets layout.uirevision keeps the zoom and pan the user dragged.
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
  var HOVER_DELAY_MS = 100; // quiet time before a hover (or the pointer leaving) is sent
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
    if (gd._shinyPlotlyHover) clearTimeout(gd._shinyPlotlyHover.timer);
    if (window.Plotly && gd._fullLayout) window.Plotly.purge(gd);
  }

  // --- events to inputs ---------------------------------------------------------------
  // What plotly hands an event handler holds the full trace and axis objects, circular and
  // large; what travels is each point's scalar fields plus the few objects worth having
  // (bbox, pointNumbers, customdata), the same cut Dash makes. Typed arrays, which is how
  // plotly holds decoded bdata, become plain arrays so they serialize as lists.

  function plain(value) {
    if (ArrayBuffer.isView(value)) return Array.prototype.slice.call(value);
    if (Array.isArray(value)) return value.map(plain);
    return value;
  }

  function pointData(gd, point) {
    var out = {};
    for (var key in point) {
      var v = point[key];
      if (v === null || typeof v !== "object") out[key] = v;
    }
    if (point.bbox) out.bbox = point.bbox;
    if (point.pointNumbers) out.pointNumbers = point.pointNumbers;
    var trace = gd._fullData && gd._fullData[point.curveNumber];
    if (trace && trace.customdata != null && point.pointNumber !== undefined) {
      out.customdata = plain(trace.customdata[point.pointNumber]);
    }
    return out;
  }

  // Above maxPoints (null: no cap) the points stay here and the value carries their count
  // and the selection's geometry, so the server knows what was selected and can recompute
  // membership from its own data; a 100-byte point times a dense trace would otherwise
  // build a message of many megabytes, or one that closes the websocket.
  function pointsData(gd, ev, maxPoints) {
    if (!ev || !ev.points) return null; // a selection cleared by clicking empty space
    var out = {};
    if (maxPoints !== null && maxPoints !== undefined && ev.points.length > maxPoints) {
      out.points = null;
      out.point_count = ev.points.length;
    } else {
      out.points = ev.points.map(function (p) { return pointData(gd, p); });
    }
    if (ev.range) out.range = ev.range;
    if (ev.lassoPoints) out.lassoPoints = ev.lassoPoints;
    return out;
  }

  function relayoutData(ev) {
    var out = {};
    for (var key in ev) out[key] = plain(ev[key]);
    return out;
  }

  // Wires the requested plotly events of one graph div to inputs named <output id>_<event>.
  // Called once per graph div, right after its first draw; Plotly.react keeps the handlers.
  function attachEvents(gd, outputId, names, maxPoints) {
    var hover = { timer: null };
    gd._shinyPlotlyHover = hover;
    function send(name, value, options) {
      window.Shiny.setInputValue(outputId + "_" + name, value, options);
    }
    function hoverLater(value) {
      clearTimeout(hover.timer);
      hover.timer = setTimeout(function () { send("hover", value); }, HOVER_DELAY_MS);
    }
    function points(ev) {
      return pointsData(gd, ev, maxPoints);
    }
    names.forEach(function (name) {
      if (name === "click") {
        // Every click counts, a repeated one too; the same rule as Shiny's plot clicks.
        gd.on("plotly_click", function (ev) { send("click", points(ev), { priority: "event" }); });
      } else if (name === "hover") {
        gd.on("plotly_hover", function (ev) { hoverLater(points(ev)); });
        gd.on("plotly_unhover", function () { hoverLater(null); });
      } else if (name === "selected") {
        gd.on("plotly_selected", function (ev) { send("selected", points(ev)); });
        gd.on("plotly_deselect", function () { send("selected", null); });
      } else if (name === "relayout") {
        gd.on("plotly_relayout", function (ev) { send("relayout", relayoutData(ev)); });
      }
    });
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
      if (value.events && value.events.length) {
        attachEvents(gd, el.id, value.events, value.max_event_points);
      }
      runPostScript(gd, value.post_script);
      gd._shinyPlotlyDrawn = true;
      return applyPending(el, gd);
    });
  }

  // --- in-place updates -----------------------------------------------------------------
  // extend_traces, restyle and relayout send {id, method, args}: the output id, the name of
  // the Plotly function, and its arguments after the graph div as plotly JSON. An update for
  // an output that has no figure drawn right now (a slow first render, an output in a hidden
  // tab, an output showing an error or emptied by a None) is held on the output element and
  // applied, in order, right after its next draw. One for an id with no output on the page
  // is dropped with a warning.

  function applyUpdate(gd, update) {
    var args = JSON.parse(update.args);
    if (update.method === "extendTraces") {
      // Plotly.extendTraces wants an array of indices and no maxPoints rather than null.
      if (args[1] === null) args[1] = gd.data.map(function (_, i) { return i; });
      if (args[2] === null) args.length = 2;
    }
    return window.Plotly[update.method].apply(window.Plotly, [gd].concat(args));
  }

  function applyPending(el, gd) {
    var queue = el._shinyPlotlyPending || [];
    el._shinyPlotlyPending = null;
    return queue.reduce(function (chain, update) {
      return chain.then(function () { return applyUpdate(gd, update); });
    }, Promise.resolve());
  }

  function onUpdate(update) {
    var el = document.getElementById(update.id);
    if (!el) {
      console.warn("shiny-plotly: " + update.method + " for '" + update.id + "' dropped: no such output on the page");
      return;
    }
    var gd = graphDiv(el);
    if (gd && gd._shinyPlotlyDrawn) {
      applyUpdate(gd, update);
      return;
    }
    (el._shinyPlotlyPending || (el._shinyPlotlyPending = [])).push(update);
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
    window.Shiny.addCustomMessageHandler("shiny-plotly", onUpdate);
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
