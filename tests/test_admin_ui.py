from html.parser import HTMLParser
from pathlib import Path


ADMIN_PATH = Path(__file__).resolve().parents[1] / "admin.html"
ADMIN_HTML = ADMIN_PATH.read_text(encoding="utf-8")


class AdminMarkupInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def inspect_admin_markup():
    parser = AdminMarkupInspector()
    parser.feed(ADMIN_HTML)
    return parser


def test_admin_navigation_groups_preserve_all_panels():
    parser = inspect_admin_markup()
    groups = [
        attrs.get("data-nav-group")
        for _, attrs in parser.elements
        if attrs.get("data-nav-group")
    ]
    panels = {
        attrs.get("id")
        for tag, attrs in parser.elements
        if tag == "section" and "panel" in attrs.get("class", "").split()
    }

    assert groups == ["catalog", "quality", "analytics"]
    assert any(attrs.get("id") == "adminSubtabs" for _, attrs in parser.elements)
    assert panels == {
        "scan",
        "icons",
        "cards",
        "temp",
        "counterTests",
        "screenshotTests",
        "users",
        "analytics",
    }
    assert "sessionStorage.setItem('adminTab:'+groupId,tabId)" in ADMIN_HTML
    assert "loadAdminPanel(tabId)" in ADMIN_HTML
    nav_config = ADMIN_HTML.split("const ADMIN_NAV_GROUPS={", 1)[1].split("};", 1)[0]
    for tab_id in panels:
        assert "{id:'" + tab_id + "'" in nav_config


def test_admin_confirmation_uses_one_accessible_div_overlay():
    parser = inspect_admin_markup()
    elements_by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in parser.elements
        if attrs.get("id")
    }
    overlay_tag, overlay_attrs = elements_by_id["confirmOverlay"]

    assert overlay_tag == "div"
    assert "hidden" in overlay_attrs
    assert elements_by_id["confirmTitle"][1].get("id") == "confirmTitle"
    assert elements_by_id["confirmOverlay"][1].get("class") == "confirm-overlay"
    assert elements_by_id["adminToast"][1].get("aria-live") == "polite"
    assert "function showConfirm(title,body,okText=" in ADMIN_HTML
    assert "event.key==='Escape'" in ADMIN_HTML
    assert "event.target===event.currentTarget" in ADMIN_HTML
    assert "confirm(" not in ADMIN_HTML


def test_admin_users_table_scrolls_without_collapsing_columns():
    parser = inspect_admin_markup()
    elements_by_id = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in parser.elements
        if attrs.get("id")
    }

    assert elements_by_id["usersTableBody"][0] == "tbody"
    assert 'class="users-table-scroll"' in ADMIN_HTML
    assert ".users-table{width:100%;min-width:780px;border-collapse:collapse}" in ADMIN_HTML
    assert ".admin-sticky{" in ADMIN_HTML
