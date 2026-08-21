import copy

import plotly
import plotly.graph_objects as go
import pytest
from htmltools import TagList

from shiny_plotly import FIGUREWIDGET_MARGINS, __version__, fig_to_ui

from newplot import parse_newplot, render_html


def bar() -> go.Figure:
    return go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))


def test_none_renders_as_nothing():
    assert fig_to_ui(None) is None


def test_fragment_calls_newplot_inline_with_no_cdn_and_no_amd_loader():
    html = render_html(fig_to_ui(bar(), div_id="p"))

    call = parse_newplot(html)
    assert call.div_id == "p"
    assert call.data[0]["type"] == "bar"
    assert "cdn.plot.ly" not in html
    assert "require(" not in html
    assert "define(" not in html
    assert "<script" in html and "plotly.min.js" not in html, "the bundle rides as a dependency"


def test_fragment_carries_the_plotly_bundle_and_the_helper_script_as_dependencies():
    deps = TagList(fig_to_ui(bar())).get_dependencies()

    assert [(d.name, str(d.version)) for d in deps] == [
        ("plotly", plotly.__version__),
        ("shiny-plotly", __version__),
    ]


def test_fragment_hands_the_graph_to_the_helper_before_the_users_post_script():
    html = render_html(fig_to_ui(bar(), div_id="p", post_script="window.__mine = 1;"))

    track = html.index("shinyPlotly.track(document.getElementById('p'))")
    assert html.index("Plotly.newPlot(") < track < html.index("window.__mine = 1;")


def test_plot_is_responsive_by_default():
    call = parse_newplot(render_html(fig_to_ui(bar())))

    assert call.config == {"responsive": True}


def test_user_config_is_merged_over_the_default():
    call = parse_newplot(render_html(fig_to_ui(bar(), config={"displaylogo": False})))

    assert call.config == {"responsive": True, "displaylogo": False}

    call = parse_newplot(render_html(fig_to_ui(bar(), config={"responsive": False})))
    assert call.config == {"responsive": False}


def test_default_container_fills_its_parent_from_a_400px_basis():
    """Mirrors shinywidgets: flex 1 1 400px inside fill layouts, 400px tall elsewhere."""
    html = render_html(fig_to_ui(bar()))

    assert 'class="shiny-plotly html-fill-item"' in html
    assert "height:400px" in html.replace(" ", "")
    assert "width:100%" in html.replace(" ", "")


def test_explicit_height_fixes_the_size_and_opts_out_of_filling():
    html = render_html(fig_to_ui(bar(), div_id="p", height="300px", width="50%"))

    assert 'class="shiny-plotly"' in html, "a fill item would shrink or grow past the height"
    assert "height:300px" in html.replace(" ", "")
    assert "width:50%" in html.replace(" ", "")
    assert 'id="p"' in html


def test_div_id_is_unique_per_call_when_not_given():
    a = parse_newplot(render_html(fig_to_ui(bar()))).div_id
    b = parse_newplot(render_html(fig_to_ui(bar()))).div_id

    assert a and b and a != b


def test_figurewidget_margins_preset_fills_in_unset_sides_only():
    fig = bar().update_layout(margin={"t": 80})

    call = parse_newplot(render_html(fig_to_ui(fig, figurewidget_margins=True)))

    assert call.layout["margin"] == {"t": 80, "l": 16, "r": 16, "b": 16}
    assert FIGUREWIDGET_MARGINS == {"l": 16, "t": 32, "r": 16, "b": 16}


def test_figurewidget_margins_preset_does_not_mutate_the_callers_figure():
    fig = bar()
    before = copy.deepcopy(fig.to_dict())

    fig_to_ui(fig, figurewidget_margins=True)

    assert fig.to_dict() == before
    assert "margin" not in fig.to_dict()["layout"]


def test_margins_are_left_alone_without_the_preset():
    call = parse_newplot(render_html(fig_to_ui(bar())))

    assert "margin" not in call.layout


def test_post_script_runs_after_newplot_with_the_div_id_substituted():
    html = render_html(fig_to_ui(bar(), div_id="p", post_script="window.__done = '{plot_id}';"))

    assert html.index("Plotly.newPlot(") < html.index("window.__done = 'p';")


def test_accepts_a_prebuilt_figure_dict_without_revalidating_it():
    fig_dict = {"data": [{"type": "bar", "x": ["a"], "y": [1], "not_a_real_attr": 1}], "layout": {}}

    call = parse_newplot(render_html(fig_to_ui(fig_dict, div_id="p")))

    assert call.data[0]["not_a_real_attr"] == 1, "a dict goes to JSON as-is; validation would raise"


def test_a_figure_dict_with_no_layout_is_given_one():
    """Margins and themes reach into layout without checking first; this is why they can."""
    fig_dict = {"data": [{"type": "bar", "y": [1]}]}

    call = parse_newplot(render_html(fig_to_ui(fig_dict, div_id="p", figurewidget_margins=True)))

    assert call.layout["margin"] == FIGUREWIDGET_MARGINS
    assert fig_dict == {"data": [{"type": "bar", "y": [1]}]}, "the caller's dict is untouched"


def test_rejects_values_that_are_not_figures():
    with pytest.raises(TypeError, match=r"go\.Figure"):
        fig_to_ui("not a figure")  # type: ignore[arg-type]
