// The browser half of shiny-plotly: an output binding that draws render_plotly values and
// forwards plotly events to Shiny inputs, a handler that applies the in-place updates sent
// by extend_traces, restyle and relayout, a theme switcher that keeps a figure rendered
// with theme= on the page's color mode, and a size tracker shared with fig_to_ui fragments.
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
        forget(gd);
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
    forget(gd);
    if (gd._shinyPlotlyHover) clearTimeout(gd._shinyPlotlyHover.timer);
    if (window.Plotly && gd._fullLayout) window.Plotly.purge(gd);
  }

  // --- theme: follow the page's color mode ----------------------------------------------
  // A value rendered with theme= names two templates and carries no baked-in one; the
  // mode's template goes into the figure before it is drawn, and a mode flip relayouts it
  // into every themed graph on the page, with no server round-trip. The mode is the
  // data-bs-theme of the graph's nearest ancestor that sets one (ui.input_dark_mode() sets
  // it on <html>), the OS preference otherwise.
  //
  // The templates themselves arrive over their own message and are held here by key, so a
  // page of charts sharing a theme is sent one copy of it rather than one per render. They
  // are held as text and parsed per draw: plotly writes into the layout it is handed, so
  // two graphs must never share one template object.

  var templates = {}; // template JSON text, by key
  var themed = []; // graph divs carrying templates for both modes
  var modeWatcher = null;

  function onTemplates(message) {
    var sent = message.templates;
    for (var key in sent) {
      if (Object.prototype.hasOwnProperty.call(sent, key)) templates[key] = sent[key];
    }
  }

  function themesFor(value) {
    // Inline, for a render with no session to hold the cache (a stub session under Express
    // before it connects, or a value produced outside a session at all).
    if (value.themes) return JSON.parse(value.themes);
    var keys = value.theme_keys;
    if (!keys) return null;
    if (templates[keys.light] === undefined || templates[keys.dark] === undefined) {
      // Only reachable if the message carrying them was lost, which would also have closed
      // the websocket. Drawing untemplated beats not drawing at all.
      console.warn("shiny-plotly: theme templates for this output never arrived; drawing it with plotly's default template");
      return null;
    }
    return {
      light: JSON.parse(templates[keys.light]),
      dark: JSON.parse(templates[keys.dark])
    };
  }

  function colorSchemeQuery() {
    return window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  }

  function pageMode(el) {
    // Bootstrap honours data-bs-theme on any element, so a card or a sidebar can set a mode
    // for part of a page; the nearest ancestor that names one wins, el itself included.
    for (var node = el; node; node = node.parentElement) {
      var attr = node.getAttribute && node.getAttribute("data-bs-theme");
      if (attr === "dark" || attr === "light") return attr;
    }
    var query = colorSchemeQuery();
    return query && query.matches ? "dark" : "light";
  }

  function retheme(gd) {
    // Not drawn yet: relayout would throw; the retheme chained after the draw catches up.
    if (!gd._shinyPlotlyThemes || !gd._shinyPlotlyDrawn) return Promise.resolve();
    var mode = pageMode(gd);
    if (gd._shinyPlotlyMode === mode) return Promise.resolve();
    gd._shinyPlotlyMode = mode;
    return window.Plotly.relayout(gd, { template: gd._shinyPlotlyThemes[mode] });
  }

  function onModeChange() {
    themed = themed.filter(function (gd) { return gd.isConnected; });
    themed.forEach(function (gd) { retheme(gd); });
  }

  function watchMode(gd) {
    if (themed.indexOf(gd) === -1) themed.push(gd);
    if (modeWatcher !== null) return;
    modeWatcher = new MutationObserver(onModeChange);
    // The whole tree, not just <html>: a container deeper in the page can set the mode for
    // the graphs under it. Every themed graph re-reads its own nearest ancestor, and one
    // whose mode did not change is left alone.
    modeWatcher.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-bs-theme"],
      subtree: true
    });
    var query = colorSchemeQuery();
    if (query && query.addEventListener) query.addEventListener("change", onModeChange);
  }

  function forget(gd) {
    var at = themed.indexOf(gd);
    if (at !== -1) themed.splice(at, 1);
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

  // A legend event names a trace, not points: the trace's index and, because the event
  // fires before the default toggle, the visibility the click is about to change. For a
  // trace type whose legend items are labels (pie, funnelarea) plotly adds the label.
  function legendData(gd, ev) {
    var out = { curve_number: ev.curveNumber, expanded_index: ev.expandedIndex };
    var trace = gd._fullData && gd._fullData[ev.curveNumber];
    if (trace) {
      out.name = trace.name;
      out.visible = trace.visible;
    }
    if (ev.label !== undefined) out.label = ev.label;
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
      } else if (name === "doubleclick") {
        // The event hands its handler nothing, so the value is a count kept on the graph
        // div: it changes on every double-click, which is what invalidates the input.
        gd.on("plotly_doubleclick", function () {
          gd._shinyPlotlyDblclicks = (gd._shinyPlotlyDblclicks || 0) + 1;
          send("doubleclick", gd._shinyPlotlyDblclicks);
        });
      } else if (name === "legendclick" || name === "legenddoubleclick") {
        // priority event, like click: a repeated click on the same item counts again.
        gd.on("plotly_" + name, function (ev) {
          send(name, legendData(gd, ev), { priority: "event" });
        });
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
    var themes = themesFor(value);
    var gd = graphDiv(el);
    var redraw = gd !== null;
    if (!redraw) {
      clear(el); // an error message may be showing
      gd = create(el, value);
    }
    // The graph div exists before the mode is read: the mode comes from where the graph
    // sits on the page, so there has to be a graph there to ask about.
    gd._shinyPlotlyThemes = themes;
    if (themes) {
      gd._shinyPlotlyMode = pageMode(gd);
      figure.layout = figure.layout || {};
      figure.layout.template = themes[gd._shinyPlotlyMode];
      watchMode(gd);
    }
    if (redraw) {
      // The retheme covers a mode that flipped while the draw was in flight.
      return window.Plotly.react(gd, figure).then(function () { return retheme(gd); });
    }
    return window.Plotly.newPlot(gd, figure).then(function () {
      track(gd);
      if (value.events && value.events.length) {
        attachEvents(gd, el.id, value.events, value.max_event_points);
      }
      runPostScript(gd, value.post_script);
      gd._shinyPlotlyDrawn = true;
      return applyPending(el, gd).then(function () { return retheme(gd); });
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
    if (update.method === "extendTraces" || update.method === "prependTraces") {
      // Both want an array of indices and no maxPoints rather than null.
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
    window.Shiny.addCustomMessageHandler("shiny-plotly-template", onTemplates);
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
