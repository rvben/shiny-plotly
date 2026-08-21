import json
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any, cast

import plotly
import plotly.graph_objects as go
import pytest
from shiny import App, Inputs, Outputs, Session, module, ui
from shiny.express._stub_session import ExpressStubSession
from shiny.render.renderer import Renderer
from shiny.session import session_context
from starlette.testclient import TestClient

from shiny_plotly import __version__, output_plotly, plotly_js, render_plotly
from shiny_plotly._render import TEMPLATE_MESSAGE

from helpers import run


def bar() -> go.Figure:
    return go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))


# --- decorator shape ------------------------------------------------------------------


def test_bare_decorator_keeps_the_function_name_as_output_id():
    @render_plotly
    def sales():
        return bar()

    assert isinstance(sales, Renderer)
    assert sales.output_id == "sales"


def test_parenthesised_decorator_keeps_the_output_id_and_records_options():
    @render_plotly(height="300px", figurewidget_margins=True, config={"displaylogo": False})
    def sales():
        return bar()

    assert sales.output_id == "sales"
    assert sales.height == "300px"
    assert sales.figurewidget_margins is True
    assert sales.config == {"displaylogo": False}


def test_express_auto_output_ui_is_the_plotly_output_with_the_output_id():
    @render_plotly
    def sales():
        return bar()

    tag = sales.auto_output_ui()

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-plotly-output")
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_is_a_fill_aware_output_bound_by_the_browser_helper():
    tag = output_plotly("sales")

    assert tag.attrs["id"] == "sales"
    assert tag.has_class("shiny-plotly-output"), "the class the output binding finds"
    assert not tag.has_class("shiny-html-output"), "not an output_ui: values are figures"
    assert tag.has_class("html-fill-item")
    assert tag.has_class("html-fill-container")


def test_output_plotly_carries_the_bundle_and_the_helper_so_no_page_level_call_is_needed():
    deps = output_plotly("sales").get_dependencies()

    assert [(d.name, str(d.version)) for d in deps if d.name != "htmltools-fill"] == [
        ("plotly", plotly.__version__),
        ("shiny-plotly", __version__),
    ]


@pytest.mark.parametrize("height", [None, "200px"])
def test_output_plotly_carries_the_fill_css_so_the_graph_fills_the_output_on_any_page(height):
    """Without it the graph inside a fixed-height output on a plain page keeps its 400px."""
    deps = output_plotly("sales", height=height).get_dependencies()

    assert "htmltools-fill" in [d.name for d in deps]


def test_events_reach_the_value_once_each_in_a_fixed_order():
    @render_plotly(events=("relayout", "click", "click"))
    def sales():
        return bar()

    assert sales.events == ("click", "relayout")


def test_a_single_event_name_is_accepted_as_a_string():
    @render_plotly(events="hover")
    def sales():
        return bar()

    assert sales.events == ("hover",)


def test_unknown_event_names_fail_at_decoration_time():
    expected = "click, doubleclick, hover, selected, relayout, legendclick, legenddoubleclick"
    with pytest.raises(ValueError, match=rf"plotly_clack.*{expected}"):

        @render_plotly(events=("click", "plotly_clack"))
        def sales():
            return bar()


def test_the_double_click_and_legend_events_are_accepted():
    @render_plotly(events=("legenddoubleclick", "doubleclick", "legendclick"))
    def sales():
        return bar()

    assert sales.events == ("doubleclick", "legendclick", "legenddoubleclick")


def test_max_event_points_defaults_to_ten_thousand_and_is_recorded():
    @render_plotly
    def sales():
        return bar()

    @render_plotly(max_event_points=None)
    def unlimited():
        return bar()

    assert sales.max_event_points == 10_000
    assert unlimited.max_event_points is None


@pytest.mark.parametrize("bad", [0, -5, 2.5, "100", True])
def test_max_event_points_must_be_a_positive_int_or_none(bad):
    with pytest.raises(ValueError, match="max_event_points"):

        @render_plotly(max_event_points=bad)
        def sales():
            return bar()


def test_theme_auto_is_the_plotly_light_dark_pair():
    @render_plotly(theme="auto")
    def sales():
        return bar()

    assert sales.theme == ("plotly", "plotly_dark")


def test_theme_defaults_to_none():
    @render_plotly
    def sales():
        return bar()

    assert sales.theme is None


def test_a_bare_template_name_is_rejected_with_the_alternative_spelled_out():
    with pytest.raises(ValueError, match=r'"auto".*pair.*layout\.template'):

        @render_plotly(theme="plotly_dark")
        def sales():
            return bar()


def test_an_unknown_template_name_fails_at_decoration_time():
    with pytest.raises(ValueError, match=r"plotly_drak.*plotly_dark"):

        @render_plotly(theme=("plotly", "plotly_drak"))
        def sales():
            return bar()


def test_a_theme_pair_must_have_exactly_two_templates():
    three: Any = ("plotly", "plotly_dark", "seaborn")
    with pytest.raises(ValueError, match="pair"):

        @render_plotly(theme=three)
        def sales():
            return bar()


def test_a_theme_template_must_be_a_name_a_template_object_or_a_dict():
    with pytest.raises(ValueError, match="Template"):

        @render_plotly(theme=("plotly", 7))
        def sales():
            return bar()


def test_output_plotly_is_namespaced_inside_a_module():
    @module.ui
    def mod_ui():
        return output_plotly("fig")

    assert mod_ui("m").attrs["id"] == "m-fig"


def test_express_auto_output_ui_is_namespaced_inside_a_module():
    @render_plotly
    def fig():
        return bar()

    @module.ui
    def mod_ui():
        return fig.auto_output_ui()

    assert mod_ui("m").attrs["id"] == "m-fig"


def test_output_plotly_with_a_height_is_a_fixed_size_fill_container():
    tag = output_plotly("sales", height="220px", width="400px")

    assert tag.has_class("html-fill-container"), "the graph inside still fills the output"
    assert not tag.has_class("html-fill-item"), "a fill item would not keep its 220px"
    assert "height:220px" in str(tag.attrs["style"]).replace(" ", "")
    assert "width:400px" in str(tag.attrs["style"]).replace(" ", "")


# --- end to end through a real Shiny app ----------------------------------------------


def make_app() -> App:
    app_ui = ui.page_fluid(
        plotly_js(),
        output_plotly("sync_fig"),
        output_plotly("async_fig"),
        output_plotly("empty_fig"),
        output_plotly("parity_fig"),
        output_plotly("themed_fig"),
    )

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def sync_fig():
            return bar()

        @render_plotly
        async def async_fig():
            return bar().update_layout(title="async")

        @render_plotly
        def empty_fig():
            return None

        @render_plotly(
            figurewidget_margins=True,
            height="250px",
            width="300px",
            config={"displaylogo": False},
            post_script="console.log('{plot_id}')",
            events=["selected", "click"],
            max_event_points=None,
        )
        def parity_fig():
            return bar()

        @render_plotly(theme="auto")
        def themed_fig():
            return bar()

    return App(app_ui, server)


OUTPUT_IDS = ("sync_fig", "async_fig", "empty_fig", "parity_fig", "themed_fig")


class Flush:
    """Every message the browser received, up to and including the one with the values."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages

    @property
    def values(self) -> dict:
        return self.messages[-1]["values"]

    @property
    def template_messages(self) -> list[dict[str, str]]:
        """What each shiny-plotly-template message carried, in arrival order."""
        return [
            msg["custom"][TEMPLATE_MESSAGE]["templates"]
            for msg in self.messages
            if TEMPLATE_MESSAGE in (msg.get("custom") or {})
        ]

    @property
    def templates(self) -> dict[str, str]:
        """Every template that arrived, by key."""
        arrived: dict[str, str] = {}
        for templates in self.template_messages:
            arrived.update(templates)
        return arrived


@contextmanager
def connected(client: TestClient, output_ids: Sequence[str], data: dict | None = None):
    """A websocket initialised the way the browser initialises one."""
    with client.websocket_connect("/websocket/") as ws:
        assert "config" in ws.receive_json()
        ws.send_json(
            {
                "method": "init",
                "data": {
                    **{f".clientdata_output_{oid}_hidden": False for oid in output_ids},
                    **(data or {}),
                },
            }
        )
        yield ws


def read_flush(ws, output_ids: Sequence[str]) -> Flush:
    """Read until every named output has a value, keeping what arrived on the way."""
    seen: list[dict] = []
    for _ in range(50):
        msg = ws.receive_json()
        seen.append(msg)
        if "values" in msg and all(oid in msg["values"] for oid in output_ids):
            assert msg.get("errors") in (None, {}), msg.get("errors")
            return Flush(seen)
    pytest.fail(f"no values for {', '.join(output_ids)} flushed after init")


def connect_and_flush(client: TestClient, *output_ids: str, data: dict | None = None) -> Flush:
    with connected(client, output_ids, data) as ws:
        return read_flush(ws, output_ids)


@pytest.fixture(scope="module")
def flushed() -> Flush:
    with TestClient(make_app()) as client:
        return connect_and_flush(client, *OUTPUT_IDS)


@pytest.fixture(scope="module")
def values(flushed: Flush) -> dict:
    return flushed.values


def test_sync_figure_is_sent_as_figure_json_for_the_browser_helper_to_draw(values):
    value = values["sync_fig"]

    figure = json.loads(value["figure"])
    assert figure["data"][0]["type"] == "bar"
    assert figure["data"][0]["x"] == ["a", "b"]
    assert "layout" in figure
    assert value["config"] == {"responsive": True}
    assert value["height"] is None
    assert value["width"] == "100%"
    assert value["post_script"] is None
    assert value["events"] == []
    assert value["max_event_points"] == 10_000


def test_figure_json_is_plotly_serialised_not_shiny_serialised():
    """numpy arrays only survive plotly's encoder; plotly 6 also packs them as compact bdata."""
    np = pytest.importorskip("numpy")

    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return go.Figure(go.Scatter(x=np.arange(3), y=np.array([1.5, 2.5, 3.5])))

    with TestClient(App(app_ui, server)) as client:
        value = flush_one(client, "fig")

    trace = json.loads(value["figure"])["data"][0]
    if int(plotly.__version__.split(".")[0]) >= 6:
        assert trace["x"] == {"dtype": "i1", "bdata": "AAEC"}
        assert trace["y"] == {"dtype": "f8", "bdata": "AAAAAAAA+D8AAAAAAAAEQAAAAAAAAAxA"}
    else:
        assert trace["x"] == [0, 1, 2]
        assert trace["y"] == [1.5, 2.5, 3.5]


def test_async_render_function_is_awaited(values):
    figure = json.loads(values["async_fig"]["figure"])

    assert figure["layout"]["title"]["text"] == "async"


def test_none_renders_as_an_empty_output(values):
    assert values["empty_fig"] is None


def test_renderer_options_reach_the_value(values):
    value = values["parity_fig"]

    assert json.loads(value["figure"])["layout"]["margin"] == {"l": 16, "t": 32, "r": 16, "b": 16}
    assert value["height"] == "250px"
    assert value["width"] == "300px"
    assert value["config"] == {"responsive": True, "displaylogo": False}
    assert value["post_script"] == "console.log('{plot_id}')"
    assert value["events"] == ["click", "selected"]
    assert value["max_event_points"] is None


def test_a_themed_value_names_its_templates_and_drops_the_figure_baked_in_one(flushed):
    value = flushed.values["themed_fig"]

    assert value["themes"] is None, "the templates travel over their own message"
    keys = value["theme_keys"]
    assert set(keys) == {"light", "dark"}
    templates = {mode: json.loads(flushed.templates[key]) for mode, key in keys.items()}
    assert templates["light"]["layout"]["font"]["color"] == "#2a3f5f", "the plotly template"
    assert templates["dark"]["layout"]["font"]["color"] == "#f2f5fa", "the plotly_dark template"
    for template in templates.values():
        assert template["layout"]["paper_bgcolor"] == "rgba(0,0,0,0)", "the page shows through"
        assert template["layout"]["plot_bgcolor"] == "rgba(0,0,0,0)"
    figure = json.loads(value["figure"])
    assert "template" not in figure["layout"], "the browser applies the mode's template"
    assert figure["data"][0]["type"] == "bar", "the figure itself is untouched"


def test_the_templates_arrive_before_the_value_that_names_them(flushed):
    """Otherwise the first draw of the session has nothing to look its keys up in."""

    def kind(msg: dict) -> str:
        if TEMPLATE_MESSAGE in (msg.get("custom") or {}):
            # Shiny runs a custom handler after the output values of the same message, so
            # sharing one with them would be as late as arriving after them.
            assert "values" not in msg, "the templates share a message with the values"
            return "templates"
        return "values" if "values" in msg else "other"

    kinds = [kind(msg) for msg in flushed.messages]

    assert "templates" in kinds, "no shiny-plotly-template message arrived"
    assert kinds.index("templates") < kinds.index("values")


def test_without_a_theme_the_value_names_no_templates_and_the_figure_keeps_its_own(values):
    value = values["sync_fig"]

    assert value["themes"] is None
    assert value["theme_keys"] is None
    assert "template" in json.loads(value["figure"])["layout"]


def themed_app() -> App:
    """Two charts sharing one theme, one of them redrawn by an input."""
    app_ui = ui.page_fluid(output_plotly("one"), output_plotly("two"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme="auto")
        def one():
            return bar().update_layout(title=f"n={input.n()}")

        @render_plotly(theme="auto")
        def two():
            return bar()

    return App(app_ui, server)


def test_two_charts_on_one_theme_are_sent_one_copy_of_it():
    with TestClient(themed_app()) as client:
        flushed = connect_and_flush(client, "one", "two", data={"n": 1})

    assert [sorted(sent) for sent in flushed.template_messages] == [sorted(flushed.templates)]
    assert len(flushed.templates) == 2, "one light and one dark, for both charts"
    assert flushed.values["one"]["theme_keys"] == flushed.values["two"]["theme_keys"]


def test_a_re_render_names_the_templates_the_session_already_has():
    with TestClient(themed_app()) as client, connected(client, ("one", "two"), {"n": 1}) as ws:
        first = read_flush(ws, ("one", "two"))
        ws.send_json({"method": "update", "data": {"n": 2}})
        again = read_flush(ws, ("one",))

    assert json.loads(again.values["one"]["figure"])["layout"]["title"]["text"] == "n=2"
    assert again.template_messages == [], "already in the browser's cache"
    assert again.values["one"]["theme_keys"] == first.values["one"]["theme_keys"]


def test_a_second_session_is_sent_the_templates_again():
    """A browser that reconnects has an empty cache, and it gets a new session."""
    with TestClient(themed_app()) as client:
        first = connect_and_flush(client, "one", "two", data={"n": 1})
        second = connect_and_flush(client, "one", "two", data={"n": 1})

    assert second.template_messages == first.template_messages != []
    assert second.values["one"]["theme_keys"] == first.values["one"]["theme_keys"]


def test_a_module_shares_the_session_cache_with_the_page_around_it():
    """A module has its own session scope, but one browser holds one set of templates."""

    @module.ui
    def mod_ui():
        return output_plotly("fig")

    @module.server
    def mod_server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme="auto")
        def fig():
            return bar()

    app_ui = ui.page_fluid(output_plotly("page"), mod_ui("m"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme="auto")
        def page():
            return bar()

        mod_server("m")

    with TestClient(App(app_ui, server)) as client:
        flushed = connect_and_flush(client, "page", "m-fig")

    assert len(flushed.template_messages) == 1, "one message, for both scopes"
    assert flushed.values["m-fig"]["theme_keys"] == flushed.values["page"]["theme_keys"]


def test_a_custom_theme_pair_travels_resolved_with_transparent_backgrounds():
    """A registered Template object and a plain dict, the two non-name spellings."""
    import plotly.io as pio

    app_ui = ui.page_fluid(output_plotly("fig"))

    dark = {"layout": {"font": {"color": "rgb(1, 2, 3)"}}}

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme=(pio.templates["seaborn"], dark))
        def fig():
            return bar()

    with TestClient(App(app_ui, server)) as client:
        flushed = connect_and_flush(client, "fig")

    keys = flushed.values["fig"]["theme_keys"]
    themes = {mode: json.loads(flushed.templates[key]) for mode, key in keys.items()}
    assert themes["light"]["layout"]["font"]["color"] == "rgb(36,36,36)", "seaborn's text color"
    assert themes["dark"]["layout"]["font"]["color"] == "rgb(1, 2, 3)"
    for template in themes.values():
        assert template["layout"]["paper_bgcolor"] == "rgba(0,0,0,0)"
        assert template["layout"]["plot_bgcolor"] == "rgba(0,0,0,0)"


def test_different_themes_get_different_keys_and_both_travel():
    app_ui = ui.page_fluid(output_plotly("light_fig"), output_plotly("dark_fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly(theme=("plotly", "plotly_dark"))
        def light_fig():
            return bar()

        @render_plotly(theme=("seaborn", "plotly_dark"))
        def dark_fig():
            return bar()

    with TestClient(App(app_ui, server)) as client:
        flushed = connect_and_flush(client, "light_fig", "dark_fig")

    light = flushed.values["light_fig"]["theme_keys"]
    dark = flushed.values["dark_fig"]["theme_keys"]
    assert light["light"] != dark["light"], "different templates, different keys"
    assert light["dark"] == dark["dark"], "the same template, one key and one copy"
    assert sorted(flushed.templates) == sorted({*light.values(), *dark.values()})
    assert len(flushed.templates) == 3


@pytest.mark.parametrize(
    "session",
    [
        pytest.param(None, id="no session at all"),
        pytest.param(ExpressStubSession(), id="express before the browser connects"),
    ],
)
def test_a_value_with_no_session_to_cache_against_carries_its_templates_inline(session):
    """Naming keys would name them in a cache nothing is keeping, so the pair travels whole."""

    @render_plotly(theme="auto")
    def sales():
        return bar()

    with session_context(session):
        value = cast(dict[str, Any], run(sales.transform(bar())))

    assert value["theme_keys"] is None, "there is nothing to look a key up in"
    themes = json.loads(value["themes"])
    assert themes["light"]["layout"]["font"]["color"] == "#2a3f5f", "the plotly template"
    assert themes["dark"]["layout"]["font"]["color"] == "#f2f5fa", "the plotly_dark template"
    assert "template" not in json.loads(value["figure"])["layout"]


def test_a_themed_figure_given_as_a_dict_with_no_layout_renders():
    """Dropping the baked-in template reaches into layout; a dict need not have brought one."""

    @render_plotly(theme="auto")
    def sales():
        return {"data": [{"type": "bar", "y": [1, 2]}]}

    value = cast(dict[str, Any], run(sales.transform({"data": [{"type": "bar", "y": [1, 2]}]})))

    assert json.loads(value["figure"])["layout"] == {}
    assert json.loads(value["themes"])["light"]["layout"]["font"]["color"] == "#2a3f5f"


def test_a_theme_dict_template_is_not_mutated_by_the_transparency_fill():
    custom = {"layout": {"font": {"color": "white"}}}

    @render_plotly(theme=("plotly", custom))
    def sales():
        return bar()

    assert custom == {"layout": {"font": {"color": "white"}}}


def flush_one(client: TestClient, output_id: str) -> dict:
    return connect_and_flush(client, output_id).values[output_id]


def test_plotly_bundle_is_served_from_the_page_level_dependency():
    # The route this reaches is about the path, not the encoding, so ask for the file as it is
    # on disk; what it serves compressed has tests of its own in test_compressed_js.py.
    with TestClient(make_app()) as client:
        resp = client.get(
            f"/lib/plotly-{plotly.__version__}/plotly.min.js",
            headers={"Accept-Encoding": "identity"},
        )

    assert resp.status_code == 200
    assert b"Plotly" in resp.content[:200_000]
    assert len(resp.content) > 1_000_000


def test_bundle_and_helper_are_served_from_the_output_tag_without_a_page_level_call():
    app_ui = ui.page_fluid(output_plotly("fig"))

    def server(input: Inputs, output: Outputs, session: Session):
        @render_plotly
        def fig():
            return bar()

    with TestClient(App(app_ui, server)) as client:
        raw = {"Accept-Encoding": "identity"}
        bundle = client.get(f"/lib/plotly-{plotly.__version__}/plotly.min.js", headers=raw)
        helper = client.get(f"/lib/shiny-plotly-{__version__}/shiny-plotly.js", headers=raw)

    assert bundle.status_code == 200
    assert len(bundle.content) > 1_000_000
    assert helper.status_code == 200
    assert b"ResizeObserver" in helper.content
    assert b"Plotly.react(" in helper.content
