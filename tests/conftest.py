"""Keep ordinary test runs independent of the developer's persistent cache."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_compression_cache():
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SHINY_PLOTLY_NO_CACHE", "1")
        yield
