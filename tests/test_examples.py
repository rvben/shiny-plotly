"""Every example in examples/ builds a runnable shiny App."""

import importlib.util
from pathlib import Path

import pytest
from shiny import App
from shiny.express import is_express_app, wrap_express_app

EXAMPLES = Path(__file__).parent.parent / "examples"

# Found rather than listed, so an example is covered from the moment it is written; Shiny's
# own detector decides which of the two ways each one is built.
ALL_EXAMPLES = sorted(
    path.relative_to(EXAMPLES) for path in EXAMPLES.rglob("*.py") if "__pycache__" not in path.parts
)
EXPRESS_EXAMPLES = [p for p in ALL_EXAMPLES if is_express_app(p.name, str(EXAMPLES / p.parent))]
CORE_EXAMPLES = [p for p in ALL_EXAMPLES if p not in EXPRESS_EXAMPLES]


def test_the_examples_were_found_and_told_apart():
    """A glob that matched nothing would leave every test below passing on no examples."""
    assert set(CORE_EXAMPLES) | set(EXPRESS_EXAMPLES) == set(ALL_EXAMPLES)
    assert Path("core_app.py") in CORE_EXAMPLES
    assert Path("shinylive/app.py") in CORE_EXAMPLES, "the search reaches into subdirectories"
    assert Path("express_app.py") in EXPRESS_EXAMPLES


@pytest.mark.parametrize("name", CORE_EXAMPLES, ids=str)
def test_core_example_builds_an_app(name: Path):
    path = EXAMPLES / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert isinstance(module.app, App)


@pytest.mark.parametrize("name", EXPRESS_EXAMPLES, ids=str)
def test_express_example_builds_an_app(name: Path):
    app = wrap_express_app(EXAMPLES / name)

    assert isinstance(app, App)


def test_the_shinylive_example_pins_past_the_version_that_crashed_under_pyodide():
    requirements = (EXAMPLES / "shinylive" / "requirements.txt").read_text()

    assert "shiny-plotly>0.3.0" in requirements
