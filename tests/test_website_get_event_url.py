import website


class FakeElement:
    def __init__(self, text="", href=None):
        self.text = text
        self._href = href

    def get_attribute(self, name):
        assert name == "href"
        return self._href


class FakeEvent:
    def __init__(self, register_btn):
        self._register_btn = register_btn

    def find_element(self, by, selector):
        assert selector == website.REGISTER_BTN
        return self._register_btn


def _make_site(event):
    site = website.Website.__new__(website.Website)
    site.display_all_events = lambda: None
    site._find_event = lambda *_args, **_kwargs: event
    return site


def test_get_event_url_returns_register_link_href():
    register_btn = FakeElement(text="Register", href="https://events.example.com/Online/Events/Details/19992/ABC123")
    site = _make_site(FakeEvent(register_btn))

    event_url = site.get_event_url("MON, MAY 5", "9:00am - 10:00am")

    assert event_url == "https://events.example.com/Online/Events/Details/19992/ABC123"


def test_get_event_url_returns_none_when_no_event_found():
    site = _make_site(None)
    site._find_event = lambda *_args, **_kwargs: None

    event_url = site.get_event_url("MON, MAY 5", "9:00am - 10:00am")

    assert event_url is None


def test_get_event_url_returns_none_when_href_missing():
    register_btn = FakeElement(text="Details", href=None)
    site = _make_site(FakeEvent(register_btn))

    event_url = site.get_event_url("MON, MAY 5", "9:00am - 10:00am")

    assert event_url is None
