import os

import plotly
from htmltools import HTMLDependency

from shiny_plotly import plotly_js


def test_plotly_js_is_an_html_dependency_on_the_installed_plotly_bundle():
    dep = plotly_js()

    assert isinstance(dep, HTMLDependency)
    assert dep.name == "plotly"
    assert str(dep.version) == plotly.__version__

    source_dir = dep.source_path_map()["source"]
    assert source_dir == os.path.join(os.path.dirname(plotly.__file__), "package_data")
    assert os.path.isfile(os.path.join(source_dir, "plotly.min.js"))

    assert [s["src"] for s in dep.script] == ["plotly.min.js"]


def test_plotly_js_returns_equal_dependencies_so_htmltools_dedups_them():
    assert plotly_js() == plotly_js()
