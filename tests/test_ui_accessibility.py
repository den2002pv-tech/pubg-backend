"""Static regression checks for the public HTML interface and accessibility contracts."""

from html.parser import HTMLParser
from pathlib import Path
import re


HTML_PATH = Path(__file__).resolve().parents[1] / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")


class MarkupInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if attrs.get("id"):
            self.ids.append(attrs["id"])


def inspect_markup():
    parser = MarkupInspector()
    parser.feed(HTML)
    return parser


def test_viewport_does_not_disable_user_zoom():
    viewport = re.search(r'<meta\s+name=["\']viewport["\']\s+content=["\']([^"\']+)', HTML)
    assert viewport
    assert "width=device-width" in viewport.group(1)
    assert "user-scalable=no" not in viewport.group(1)
    assert "maximum-scale=1" not in viewport.group(1)


def test_tab_controls_are_associated_with_their_panels():
    parser = inspect_markup()
    by_id = {attrs.get("id"): (tag, attrs) for tag, attrs in parser.elements if attrs.get("id")}
    tabs = [(tag, attrs) for tag, attrs in parser.elements if attrs.get("role") == "tab"]
    assert len(tabs) == 2
    assert all(attrs.get("aria-selected") in {"true", "false"} for _, attrs in tabs)
    for _, attrs in tabs:
        panel_id = attrs.get("aria-controls")
        assert panel_id in by_id
        panel_attrs = by_id[panel_id][1]
        assert panel_attrs.get("role") == "tabpanel"
        assert panel_attrs.get("aria-labelledby") == attrs.get("id")


def test_static_html_ids_are_unique():
    parser = inspect_markup()
    assert len(parser.ids) == len(set(parser.ids)), "Duplicate static HTML id found"


def test_search_and_sort_controls_have_accessible_names():
    parser = inspect_markup()
    labels_for = {
        attrs.get("for")
        for _, attrs in parser.elements
        if attrs.get("for")
    }
    by_id = {attrs.get("id"): attrs for _, attrs in parser.elements if attrs.get("id")}
    assert "inventorySearch" in labels_for
    assert "inventorySort" in labels_for or by_id["inventorySort"].get("aria-label")
    assert by_id["searchClearBtn"].get("aria-label")


def test_status_and_retry_controls_exist_for_quantity_saves():
    parser = inspect_markup()
    by_id = {attrs.get("id"): attrs for _, attrs in parser.elements if attrs.get("id")}
    status = by_id["quantitySaveStatus"]
    assert status.get("role") == "status"
    assert status.get("aria-live") == "polite"
    assert "retryQuantitySaveBtn" in by_id
    assert "setQuantitySaveStatus('error'" in HTML
    assert "pendingQuantityUpdates.clear()" not in HTML
    assert "pendingQuantityUpdates.delete(id)" in HTML


def test_mobile_filters_wrap_and_search_toolbar_stacks():
    assert "flex-wrap:wrap" in HTML
    assert "@media(max-width:480px){.toolbar-wrap{grid-template-columns:minmax(0,1fr)}}" in HTML
    assert "min-height:44px" in HTML


def test_empty_states_offer_recovery_and_scanner_has_steps():
    assert "scannerInstructions" in HTML
    assert 'data-empty-action="clear-search"' in HTML
    assert 'data-empty-action="scanner"' in HTML
    assert "Показано ' + list.length + ' из ' + inventoryCards.length" in HTML
