from html.parser import HTMLParser
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SHOWCASE = ROOT / "docs" / "showcase"


class ShowcaseParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.local_assets = []
        self.anchors = []
        self.module_sources = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            assert values["id"] not in self.ids
            self.ids.add(values["id"])
        if tag == "a" and values.get("href", "").startswith("#"):
            self.anchors.append(values["href"][1:])
        for attribute in ("src", "href"):
            value = values.get(attribute, "")
            if value and not value.startswith(("#", "http:", "https:", "mailto:")):
                self.local_assets.append(value.partition("?")[0])
        if tag == "script" and values.get("type") == "module":
            self.module_sources.append(values.get("src", "").partition("?")[0])


def test_showcase_html_ids_anchors_and_local_assets():
    parser = ShowcaseParser()
    parser.feed((SHOWCASE / "index.html").read_text(encoding="utf-8"))
    assert parser.ids
    assert parser.module_sources == ["app.mjs"]
    assert all(anchor in parser.ids for anchor in parser.anchors)
    assert parser.local_assets
    for asset in parser.local_assets:
        assert (SHOWCASE / asset).is_file(), asset


def test_feature_carousel_has_diagrams_and_accessible_controls():
    html = (SHOWCASE / "index.html").read_text(encoding="utf-8")
    css = (SHOWCASE / "styles.css").read_text(encoding="utf-8")
    assert html.count('data-feature="') == 3
    assert html.count('class="feature-svg"') == 3
    assert html.count('viewBox="0 0 360 200"') == 0
    assert html.count('viewBox="0 0 760 340"') == 2
    assert html.count('viewBox="0 0 760 300"') == 1
    assert html.count('href="#token-cell"') == 44
    assert html.count('href="#evidence-marker"') == 4
    assert html.count('href="#selected-') == 18
    assert html.count('preserveAspectRatio="xMidYMid meet"') == 3
    assert html.count('class="diagram-labels"') == 3
    assert html.count('data-feature-target=') == 3
    assert 'id="feature-page"' in html
    assert 'aria-roledescription="carousel"' in html
    assert 'aria-label="Feature cards. Use left and right arrow keys to choose a feature."' in html
    assert html.count('aria-controls="feature-viewport"') == 5
    assert 'aria-live="polite"' in html
    assert 'aria-atomic="true"' in html
    for paint in (
        ".diagram-box { fill: #202634; stroke: #7d889c;",
        ".diagram-line { fill: none; stroke: #a2a8b7;",
        ".diagram-label { fill: #f4f3ed;",
        ".diagram-text { fill: #c5ccd8;",
    ):
        assert paint in css


def test_showcase_javascript_syntax_and_simulation():
    subprocess.run(["node", "--check", str(SHOWCASE / "app.mjs")], check=True)
    subprocess.run(["node", "--check", str(SHOWCASE / "simulation.mjs")], check=True)
    subprocess.run(
        ["node", "--test", str(ROOT / "tests/control_plane/showcase_simulation.test.mjs")],
        check=True,
    )


def test_showcase_uses_current_rust_quickstart_and_static_boundaries():
    html = (SHOWCASE / "index.html").read_text(encoding="utf-8")
    app = (SHOWCASE / "app.mjs").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")

    assert "cargo build --release --locked" in html
    assert '"$CORE" client install --client pi' in html
    assert "Scripted local data · no model calls" in html
    assert "without connecting to a runtime" in html
    assert "rust/assets/teacher/sop-guide.md" in app
    assert "path: docs/showcase" in workflow
    for stale in ("Python 3.11+", "pip install -e .", "node_runtime", "blob/main/sop-guide.md"):
        assert stale not in html + app
