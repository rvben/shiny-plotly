.PHONY: sync lock-check browsers lint fmt typecheck test test-browser test-all build check-wheel \
	check-floor bench bench-events check version check-version release-notes publish clean \
	site site-check site-verify release-patch release-minor release-major

# Every CI step is one of these targets; the workflows only call make.

sync:
	uv sync --all-groups

lock-check:
	uv lock --check

# The Chromium the browser suite drives. Separate from sync so an install without
# playwright's browser download stays possible. CI passes PLAYWRIGHT_ARGS=--with-deps
# for the system libraries a bare runner lacks.
browsers:
	uv run playwright install $(PLAYWRIGHT_ARGS) chromium

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run pyright

# Unit tests and the in-process Shiny end-to-end tests; no browser needed.
test:
	uv run pytest -q -m "not browser"

# Drives the package in headless Chromium: sizing, resize tracking, purge on re-render,
# post_script event wiring. Needs `make browsers` once.
test-browser:
	uv run pytest -q -m browser

test-all: test test-browser

build:
	rm -rf dist
	uv build

# Installs the freshly built wheel into a throwaway venv and runs the non-browser suite
# against it, so what ships (the JS helper and py.typed included) is what was tested, not
# the editable checkout.
check-wheel: build
	rm -rf .wheel-venv
	uv venv --quiet .wheel-venv
	uv pip install --quiet --python .wheel-venv/bin/python dist/*.whl pytest httpx2
	.wheel-venv/bin/python -c "import shiny_plotly; print('shiny_plotly', shiny_plotly.__version__)"
	cd tests && ../.wheel-venv/bin/python -m pytest -q -p no:cacheprovider --ignore=browser \
		--rootdir=.. -c ../pyproject.toml .
	rm -rf .wheel-venv

# Installs the package with every direct dependency at the oldest version pyproject.toml
# allows (uv's lowest-direct resolution: plotly, shiny, htmltools at their floor, the rest
# as resolved from there) into a throwaway venv and runs the whole suite, browser included,
# so the declared bounds are tested rather than hoped. The test tools are installed in a
# second step at their current versions; none of them depends on the three. Needs
# Chromium for that venv's playwright, which the target installs.
check-floor:
	rm -rf .floor-venv
	uv venv --quiet .floor-venv
	uv pip install --quiet --python .floor-venv/bin/python --resolution lowest-direct .
	uv pip install --quiet --python .floor-venv/bin/python pytest httpx2 \
		pytest-playwright numpy
	.floor-venv/bin/python -c "import plotly, shiny, htmltools; \
		print('plotly', plotly.__version__, 'shiny', shiny.__version__, 'htmltools', htmltools.__version__)"
	.floor-venv/bin/python -m playwright install $(PLAYWRIGHT_ARGS) chromium
	cd tests && ../.floor-venv/bin/python -m pytest -q -p no:cacheprovider \
		--rootdir=.. -c ../pyproject.toml .
	rm -rf .floor-venv

check: lock-check lint typecheck test test-browser check-wheel check-floor

# Measures shinywidgets against this package on identical apps (bench/run.py) and
# prints a Markdown table; raw numbers land in bench/results.json. Needs the bench
# dependency group (make sync) and Chromium (make browsers).
bench:
	uv run python -m bench.run $(BENCH_ARGS)

# Measures what a selected event costs on a dense trace, capped by max_event_points and
# uncapped (bench/events.py); the numbers behind that option's default.
bench-events:
	uv run python -m bench.events $(BENCH_ARGS)

# Pinned: the demo build runs only in the Pages workflow, so a shinylive release
# could break the deploy with no CI run ever having seen it. Bump deliberately.
SHINYLIVE_VERSION := 0.8.11
SITE_PORT := 8008
SITE_URL ?= http://127.0.0.1:$(SITE_PORT)

# Exports the shinylive demo app with the wheel built from this checkout, so the
# deployed demo tracks main, not PyPI. The committed examples/shinylive stays a
# user-exportable example (its requirements.txt installs from PyPI); this target
# rewrites a copy to install the local wheel by absolute URL, because micropip
# resolves only http(s) URLs or index names, never bare filenames.
site: build
	rm -rf site tmp/site-src
	mkdir -p tmp/site-src
	cp examples/shinylive/app.py tmp/site-src/app.py
	printf '%s\n' "$(SITE_URL)/wheels/$$(basename dist/*.whl)" plotly > tmp/site-src/requirements.txt
	uvx shinylive==$(SHINYLIVE_VERSION) export tmp/site-src site
	mkdir -p site/wheels
	cp dist/*.whl site/wheels/

# Proves the export in two passes. shinylive-check judges the boot generically
# (console and page errors, output errors, a screenshot artifact on failure; it must
# serve on SITE_PORT because the export references its wheel by that absolute URL).
# tools/site_check.py then proves the app-specific behavior a boot check cannot:
# the stream ticks and the Explore tab draws. The Pages workflow runs this before
# rebuilding with the public URL and deploying.
site-check: site
	mkdir -p tmp
	uv run shinylive-check site --port $(SITE_PORT) --screenshot tmp/boot-failure.png
	uv run python tools/site_check.py --site site --url $(SITE_URL)

# Probes the deployed demo after the Pages deploy. The public-URL rebuild that
# actually ships can only be proven live (its wheel URL does not exist before the
# deploy), so this is the first check that sees what visitors get.
PAGES_URL ?= https://rvben.github.io/shiny-plotly/
site-verify:
	uv run shinylive-check $(PAGES_URL)

# The version in pyproject.toml, the single source for the package version.
version:
	@sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml

# Fails unless pyproject.toml carries VERSION; the release workflow runs it with the tag.
check-version:
	@test -n "$(VERSION)" || { echo "usage: make check-version VERSION=X.Y.Z" >&2; exit 2; }
	@v="$$($(MAKE) -s version)"; test "$$v" = "$(VERSION)" \
		|| { echo "pyproject.toml is $$v, expected $(VERSION)" >&2; exit 1; }

# Writes the CHANGELOG.md section for VERSION to tmp/release-notes.md; that text becomes
# the GitHub release body. Fails when the section is missing or empty.
release-notes:
	@test -n "$(VERSION)" || { echo "usage: make release-notes VERSION=X.Y.Z" >&2; exit 2; }
	@mkdir -p tmp
	@awk -v v="$(VERSION)" '\
		/^## \[/ { on = index($$0, "## [" v "]") == 1; next } \
		on && /^\[/ && /\]: / { next } \
		on { print }' CHANGELOG.md > tmp/release-notes.md
	@grep -q '[^[:space:]]' tmp/release-notes.md \
		|| { echo "CHANGELOG.md has no section for $(VERSION)" >&2; exit 1; }

# Uploads dist/* to PyPI through trusted publishing (GitHub Actions OIDC; no token).
# --check-url skips files the index already has, so a re-run after a partial failure
# does not fail on what already landed.
publish:
	uv publish --trusted-publishing always --check-url https://pypi.org/simple/shiny-plotly/ dist/*

clean:
	rm -rf dist tmp site .wheel-venv .floor-venv .pytest_cache .ruff_cache

release-patch:
	vership bump patch

release-minor:
	vership bump minor

release-major:
	vership bump major
