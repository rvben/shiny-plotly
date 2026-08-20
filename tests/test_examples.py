"""Every example in examples/ builds a runnable shiny App."""

import importlib.util
from pathlib import Path

import pytest
from shiny import App
from shiny.express import wrap_express_app

EXAMPLES = Path(__file__).parent.parent / "examples"

CORE_EXAMPLES = (
    "core_app.py",
    "events_app.py",
    "dark_app.py",
    "streaming_app.py",
    "shinylive/app.py",
)


@pytest.mark.parametrize("name", CORE_EXAMPLES)
def test_core_example_builds_an_app(name: str):
    path = EXAMPLES / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert isinstance(module.app, App)


def test_express_example_builds_an_app():
    app = wrap_express_app(EXAMPLES / "express_app.py")

    assert isinstance(app, App)


def test_the_shinylive_example_pins_past_the_version_that_crashed_under_pyodide():
    requirements = (EXAMPLES / "shinylive" / "requirements.txt").read_text()

    assert "shiny-plotly>0.3.0" in requirements
