from __future__ import annotations

from importlib.metadata import version

import plotly
from htmltools import HTMLDependency

from ._bundle import selected_bundle

__all__ = ("__version__", "plotly_js", "shiny_plotly_js")

__version__ = version("shiny-plotly")


def plotly_js() -> HTMLDependency:
    """
    The process-wide plotly.js dependency, served locally by Shiny.

    Shiny serves HTML dependencies under ``/lib/<name>-<version>/``, so the URL is keyed
    by the installed plotly version and caches correctly across deploys. Nothing is
    copied or written for the default bundle: it points at ``plotly/package_data/plotly.min.js``,
    the exact bundle ``plotly.offline.get_plotlyjs()`` would inline.

    Every :func:`~shiny_plotly.output_plotly` and every :func:`~shiny_plotly.fig_to_ui`
    fragment carries it, so nothing needs to be added to the page for it; htmltools
    de-duplicates. Add it to the page UI yourself only when the first figure is inserted
    later (``ui.insert_ui``, a ``@render.ui`` that starts empty) and the bundle should load
    with the page instead.

    After :func:`~shiny_plotly.use_plotly_bundle` (or SHINY_PLOTLY_BUNDLE), this
    carries a private snapshot instead. Its version includes the variant and content
    digest, so changing its bytes changes its immutable URL. The first call fixes the
    choice for the process. The bundle is served compressed and immutable from the
    first request; see :mod:`shiny_plotly._serve`.
    """
    selected = selected_bundle()
    if selected is not None:
        bundle, directory = selected
        return HTMLDependency(
            name="plotly",
            version=f"{plotly.__version__}+{bundle.local_version}",
            source={"subdir": str(directory)},
            script={"src": "plotly.min.js"},
        )
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
        stylesheet={"href": "shiny-plotly.css"},
    )
