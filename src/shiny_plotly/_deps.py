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

    Add it once to the page UI so the bundle loads with the page. Every figure rendered by
    :func:`~shiny_plotly.render_plotly` or :func:`~shiny_plotly.fig_to_ui` also carries it,
    so an output still works without the page-level call; htmltools de-duplicates.
    """
    return HTMLDependency(
        name="plotly",
        version=plotly.__version__,
        source={"package": "plotly", "subdir": "package_data"},
        script={"src": "plotly.min.js"},
    )


def shiny_plotly_js() -> HTMLDependency:
    """
    The small browser helper every rendered figure depends on.

    It keeps each graph sized to its container (plotly alone only reacts to window
    resizes) and purges a graph once Shiny has replaced the output holding it, so
    re-rendering outputs do not accumulate plotly state. It rides along with every
    fragment; there is no need to add it to the page yourself.
    """
    return HTMLDependency(
        name="shiny-plotly",
        version=__version__,
        source={"package": "shiny_plotly", "subdir": "www"},
        script={"src": "shiny-plotly.js"},
    )
