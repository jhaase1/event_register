"""A small in-memory stand-in for the booking site's redesigned member portal.

Models just enough of the real pages (captured Nov 2026) for website.py to run
against: the events list (one card per recurring series) and each series'
details page (one row per date, a header for the selected date, and a
Register button). Selectors are matched the way website.py writes them.

Behaviour the live site hasn't been seen to have is opt-in, so tests can't
pass by relying on it: clicking a date row doesn't change the header unless
Series.row_click_selects is set, and Finalize Registration stays on the
signup page unless Series.finalize_navigates is set.
"""

import re
import time

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
INELIGIBLE_MARK = "not eligible"


class El:
    def __init__(self, testid=None, text="", tag="div", children=(), on_click=None, role=None,
                 click_error=None, displayed=True):
        self.testid = testid
        self._text = text
        self.tag_name = tag
        self.children = list(children)
        self.on_click = on_click
        self.role = role
        self.click_error = click_error  # raised by a native click only
        self.displayed = displayed

    @property
    def text(self):
        own = [self._text] if self._text else []
        return "\n".join(own + [c.text for c in self.children if c.text])

    def get_attribute(self, name):
        if name == "data-testid":
            return self.testid
        if name == "role":
            return self.role
        return None

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return True

    def click(self):
        if self.click_error is not None:
            raise self.click_error
        self.js_click()

    def js_click(self):
        if self.on_click:
            self.on_click()

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def find_elements(self, by, selector):
        return _select([e for c in self.children for e in c.walk()], by, selector)

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]


def _matches(el, selector):
    for part in [p.strip() for p in selector.split(",")]:
        m = re.fullmatch(r"\[data-testid(=|\^=|~=)'([^']*)'\]", part)
        if m:
            op, value = m.groups()
            tid = el.testid or ""
            if (op == "=" and tid == value) or (op == "^=" and tid.startswith(value)) or (
                op == "~=" and value in tid.split()
            ):
                return True
            continue
        if part == "[role='dialog']" and el.role == "dialog":
            return True
        if part in ("button",) and el.tag_name == "button":
            return True
    return False


def _select(elements, by, selector):
    if by == By.CSS_SELECTOR:
        return [e for e in elements if _matches(e, selector)]
    if by == By.TAG_NAME:
        return [e for e in elements if e.tag_name == selector]
    if by == By.XPATH and "not eligible" in selector:
        # Like text(): an element's own text, never a script's or style's.
        return [
            e for e in elements
            if INELIGIBLE_MARK in (e._text or "").lower() and e.tag_name not in ("script", "style")
        ]
    raise AssertionError(f"fake doesn't support {by} {selector}")


class DateRow:
    def __init__(self, month, day, weekday, time_text, state="open", slots="16 of 28 spots remaining",
                 opens_in="Single date registration opens in 4 days and 23 h"):
        self.month, self.day, self.weekday, self.time_text = month, day, weekday, time_text
        self.state = state  # 'open' | 'full' | 'not_open'
        self.slots = slots
        self.opens_in = opens_in
        self.registered = False
        # Loads of the details page after which a not-open date opens.
        self.opens_after_loads = None


class Series:
    def __init__(self, card_id, rows, name="$3 Intermediate Pickleball", category="Intermediate Open Play",
                 cost="FREE", restriction=None, details_code="ABC123", description=None):
        self.card_id = str(card_id)
        self.rows = rows
        self.name, self.category, self.cost = name, category, cost
        self.restriction = restriction
        self.details_code = details_code
        self.description = description
        # The header shows default_row on every load of the details page.
        self.default_row = 0
        self.selected = 0
        self.row_click_selects = False
        self.finalize_navigates = False
        self.register_click_error = None  # raised by a native click on Register
        # Renders of the signup page before its date header shows up.
        self.signup_date_delay_renders = 0
        self.signup_renders = 0
        # Seconds Finalize shows a spinner (no button) before it registers;
        # leaving the page before then abandons the registration.
        self.finalize_spinner_seconds = None
        self.signup_script_text = None  # text inside a <script> on the signup page
        # How the signup step behaves: 'register' (Finalize registers), 'nothing'
        # (Finalize has no effect), 'ineligible' (signup page says not eligible),
        # 'wrong_date' (signup page is for a different date).
        self.on_register = "register"

    def row_label(self, row):
        if row.registered:
            return "Edit Registration"
        if row.state == "not_open":
            return "Details"
        if row.state == "full":
            return "Join Waitlist"
        return "Register"


class FakePortal:
    """Acts as the Selenium driver."""

    def __init__(self, series, events_url="https://x/Online/Events/List/1", lazy_cards=None):
        self.series = {s.card_id: s for s in series}
        self.events_url = events_url
        self.page = ("list", None)
        self.current_url = events_url
        self.title = "Events"
        self.dialog = None
        self.ineligible = False
        self.render_list = True  # False: the list never finishes loading
        # Cards rendered by a fresh list load before it's scrolled (None: all).
        self.lazy_cards = lazy_cards
        self.scrolled = False
        self.gets = []
        self.screenshots = []
        self.pending = None  # (series, row, done_at) while a Finalize spinner runs

    # --- driver API -------------------------------------------------------
    def get(self, url):
        if self.pending and time.monotonic() < self.pending[2]:
            self.pending = None  # left before the request finished
        self._settle_pending()
        self.gets.append(url)
        self.dialog = None
        self.current_url = url
        self.scrolled = False
        if "SignUpToEvent" in url:
            # The signup URL also carries the reservation number, so check it first.
            for s in self.series.values():
                if s.details_code in url:
                    self.page = ("signup", s.card_id)
                    return
        if url == self.events_url:
            self.page = ("list", None)
            return
        for s in self.series.values():
            if s.details_code in url:
                self.page = ("details", s.card_id)
                s.selected = s.default_row
                for row in s.rows:
                    if row.opens_after_loads is not None:
                        row.opens_after_loads -= 1
                        if row.opens_after_loads <= 0:
                            row.state, row.opens_after_loads = "open", None
                return
        self.page = ("other", None)

    @property
    def page_source(self):
        return f"<html><!-- {self.page} --><body>{self._root().text}</body></html>"

    def save_screenshot(self, path):
        self.screenshots.append(path)
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    def execute_script(self, script, *args):
        if "click()" in script and args:
            args[0].js_click()
        if "scrollTo" in script:
            self.scrolled = True

    def execute(self, command, params=None):
        # ActionChains (the wheel scroll) sends W3C actions through here.
        return {"value": None}

    def find_elements(self, by, selector):
        return _select(list(self._root().walk()), by, selector)

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def quit(self):
        pass

    def _settle_pending(self):
        if not self.pending or time.monotonic() < self.pending[2]:
            return
        series, target, _done_at = self.pending
        self.pending = None
        target.registered = True
        if series.finalize_navigates:
            self.get(f"https://x/Online/Events/Details/1/{series.details_code}?resId=1")

    # --- page models ------------------------------------------------------
    def _root(self):
        self._settle_pending()
        kind, card_id = self.page
        if kind == "list":
            children = self._list_page() if self.render_list else [El(text="Loading...")]
        elif kind == "details":
            children = self._details_page(self.series[card_id])
        elif kind == "signup":
            children = self._signup_page(self.series[card_id])
        else:
            children = []
        if self.dialog is not None:
            children.append(self.dialog)
        if self.ineligible:
            children.append(El(text="You are not eligible to register for this event."))
        return El(tag="body", children=children)

    @staticmethod
    def _date_text(row):
        suffix = "th" if 11 <= row.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(row.day % 10, "th")
        return f"{row.weekday}, {MONTH_ABBR[row.month]} {row.day}{suffix}"

    def _list_page(self):
        cards = []
        series = list(self.series.values())
        if self.lazy_cards is not None and not self.scrolled:
            series = series[: self.lazy_cards]
        for s in series:
            first = s.rows[0]
            cid = s.card_id

            def open_details(series=s):
                self.get(f"https://x/Online/Events/Details/1/{series.details_code}?resId=1&tab=dates")

            cards.append(El(testid=f"events-list-card-{cid}", children=[
                El(testid=f"events-list-card-category-tag-{cid}", text=s.category),
                El(testid=f"events-list-card-name-{cid}", text=s.name),
                El(testid=f"events-list-card-date-{cid}", text=f"{self._date_text(first)}, {first.time_text}",
                   children=[El(testid=f"events-list-card-dates-count-{cid}", text=f"({len(s.rows)} dates)")]),
                El(testid=f"events-list-card-cost-{cid}", text=s.cost),
                El(testid=f"events-list-slots-{cid}", text=first.slots if first.state != "not_open" else ""),
                El(testid=f"events-list-register-btn-{cid}", text=s.row_label(first), tag="button"),
                El(testid=f"events-list-view-upcoming-dates-btn-{cid}", text="VIEW UPCOMING DATES", tag="button",
                   on_click=open_details),
            ]))
        header = El(testid="events-list-results-header", text=f"{len(self.series)} events found")
        return [header, El(testid="events-list-grid", children=cards)]

    def _signup_page(self, s):
        row = s.rows[s.selected]
        shown = s.rows[(s.selected + 1) % len(s.rows)] if s.on_register == "wrong_date" else row

        def finalize(series=s, target=row):
            if series.on_register == "register" and series.finalize_spinner_seconds:
                self.pending = (series, target, time.monotonic() + series.finalize_spinner_seconds)
            elif series.on_register == "register":
                target.registered = True
                if series.finalize_navigates:
                    self.get(f"https://x/Online/Events/Details/1/{series.details_code}?resId=1")

        s.signup_renders += 1
        header = []
        if s.signup_renders > s.signup_date_delay_renders:
            header.append(El(testid="event-signup-header-date", text=f"{self._date_text(shown).replace('th', '').replace('st', '').replace('nd', '').replace('rd', '')}, 2026"))
        children = [El(testid="event-signup-page", children=header + [
            El(testid="event-signup-header-time", text=row.time_text),
            El(testid="event-signup-total-due", text="$0.00"),
        ])]
        if s.signup_script_text:
            children.append(El(tag="script", text=s.signup_script_text))
        if self.pending and self.pending[0] is s:
            children.append(El(text="Processing..."))
        elif s.on_register == "ineligible":
            children.append(El(text="You are not eligible to register for this event."))
        else:
            children.append(El(testid="event-signup-submit-btn", text="Finalize Registration", tag="button",
                               on_click=finalize))
        return children

    def _details_page(self, s):
        selected = s.rows[s.selected]
        rows = []
        for i, row in enumerate(s.rows):
            def select(series=s, index=i):
                if series.row_click_selects:
                    series.selected = index

            status = (
                El(testid=f"events-details-date-not-open-{i}", text=row.opens_in)
                if row.state == "not_open"
                else El(testid=f"events-details-date-row-slots-{i}", text="FULL" if row.state == "full" else row.slots)
            )
            rows.append(El(testid=f"events-details-date-row-{i}", on_click=select, children=[
                El(testid=f"events-details-date-row-date-{i}",
                   text=f"{row.weekday}\n{row.day}\n{MONTH_ABBR[row.month]}"),
                El(testid=f"events-details-date-row-time-{i}", text=row.time_text),
                status,
            ]))

        def register(series=s):
            # Like the live site: Register only opens the signup page.
            self.get(f"https://x/Online/Events/SignUpToEvent/1?eventId={series.card_id}"
                     f"&reservationNumber={series.details_code}")

        children = [
            El(testid="events-details-page", children=[
                El(testid="events-details-date", text=self._date_text(selected)),
                El(testid="events-details-time", text=selected.time_text),
                El(testid="events-details-slots", text=selected.slots),
            ]),
        ]
        if selected.state == "not_open":
            children.append(El(testid="events-details-signup-message",
                               text=selected.opens_in.replace("Single date registration", "Registration")))
        else:
            children.append(El(
                testid="events-details-register-btn events-details-register-single-btn register-btn",
                text=s.row_label(selected), tag="button", on_click=register,
                click_error=s.register_click_error,
            ))
        if s.description:
            children.append(El(testid="events-details-description", text=s.description))
        children.append(El(testid="events-details-dates-list", children=rows))
        if s.restriction:
            children.append(El(testid="events-details-side-restriction-0",
                               children=[El(text="Skill Level Restriction"), El(text=s.restriction)]))
        return children
