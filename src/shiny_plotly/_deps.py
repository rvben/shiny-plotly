from __future__ import annotations

from importlib.metadata import version

import plotly
from htmltools import HTMLDependency

__all__ = ("__version__", "plotly_js", "shiny_plotly_js")

__version__ = version("shiny-plotly")


def plotly_js() -> HTMLDependency:
    """
    The plotly.js bundle, served by Shiny straight from the installed ``plotly`` wheel.

    Shiny serves HTML dependencies under ``/lib/<name>-<version>/``, so the URL is keyed
    by the installed plotly version and caches correctly across deploys. Nothing is
    copied or written: the dependency points at ``plotly/package_data/plotly.min.js``,
    the exact bundle ``plotly.offline.get_plotlyjs()`` would inline.

    Every :func:`~shiny_plotly.output_plotly` and every :func:`~shiny_plotly.fig_to_ui`
    fragment carries it, so nothing needs to be added to the page for it; htmltools
    de-duplicates. Add it to the page UI yourself only when the first figure is inserted
    later (``ui.insert_ui``, a ``@render.ui`` that starts empty) and the bundle should load
    with the page instead.

    Once a session has rendered a figure, the bundle is served pre-compressed with an
    immutable cache lifetime; see :mod:`shiny_plotly._serve`.
    """
    return HTMLDependency(
        name="plotly",
        version=plotly.__version__,
        source={"package": "plotly", "subdir": "package_data"},
        script={"src": "plotly.min.js"},
    )


def shiny_plotly_js() -> HTMLDependency:
    """
    The small browser helper every output and fragment depends on.

    It holds the output binding for :func:`~shiny_plotly.output_plotly` (``Plotly.newPlot``
    once, ``Plotly.react`` on every re-render), keeps each graph sized to its container
    (plotly alone only reacts to window resizes) and purges a graph once it leaves the
    document, so nothing accumulates plotly state. It rides along with every output and
    fragment; there is no need to add it to the page yourself.
    """
    return HTMLDependency(
        name="shiny-plotly",
        version=__version__,
        source={"package": "shiny_plotly", "subdir": "www"},
        script={"src": "shiny-plotly.js"},
    )
