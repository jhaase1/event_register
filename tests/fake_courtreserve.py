"""A small in-memory stand-in for CourtReserve's redesigned member portal.

Models just enough of the real pages (captured Nov 2026) for website.py to run
against: the events list (one card per recurring series) and each series'
details page (one row per date, a header for the selected date, and a
Register button). Selectors are matched the way website.py writes them.
"""

import re

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
INELIGIBLE_MARK = "not eligible"


class El:
    def __init__(self, testid=None, text="", tag="div", children=(), on_click=None, role=None):
        self.testid = testid
        self._text = text
        self.tag_name = tag
        self.children = list(children)
        self.on_click = on_click
        self.role = role

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
        return True

    def is_enabled(self):
        return True

    def click(self):
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
        return [e for e in elements if INELIGIBLE_MARK in (e._text or "").lower()]
    raise AssertionError(f"fake doesn't support {by} {selector}")


class DateRow:
    def __init__(self, month, day, weekday, time_text, state="open", slots="16 of 28 spots remaining",
                 opens_in="Single date registration opens in 4 days and 23 h"):
        self.month, self.day, self.weekday, self.time_text = month, day, weekday, time_text
        self.state = state  # 'open' | 'full' | 'not_open'
        self.slots = slots
        self.opens_in = opens_in
        self.registered = False


class Series:
    def __init__(self, card_id, rows, name="$3 Intermediate Pickleball", category="Intermediate Open Play",
                 cost="FREE", restriction=None, details_code="ABC123"):
        self.card_id = str(card_id)
        self.rows = rows
        self.name, self.category, self.cost = name, category, cost
        self.restriction = restriction
        self.details_code = details_code
        self.selected = 0
        # What the details-page Register click does: 'register', 'dialog', 'nothing', 'ineligible'.
        self.on_register = "register"

    def row_label(self, row):
        if row.registered:
            return "Edit Registration"
        if row.state == "not_open":
            return "Details"
        if row.state == "full":
            return "Join Waitlist"
        return "Register"


class FakeCourtReserve:
    """Acts as the Selenium driver."""

    def __init__(self, series, events_url="https://x/Online/Events/List/1"):
        self.series = {s.card_id: s for s in series}
        self.events_url = events_url
        self.page = ("list", None)
        self.current_url = events_url
        self.title = "Events"
        self.dialog = None
        self.ineligible = False
        self.render_list = True  # False: the list never finishes loading
        self.gets = []
        self.screenshots = []

    # --- driver API -------------------------------------------------------
    def get(self, url):
        self.gets.append(url)
        self.dialog = None
        self.current_url = url
        if url == self.events_url:
            self.page = ("list", None)
            return
        for s in self.series.values():
            if s.details_code in url:
                self.page = ("details", s.card_id)
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
            args[0].click()

    def find_elements(self, by, selector):
        return _select(list(self._root().walk()), by, selector)

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def quit(self):
        pass

    # --- page models ------------------------------------------------------
    def _root(self):
        kind, card_id = self.page
        if kind == "list":
            children = self._list_page() if self.render_list else [El(text="Loading...")]
        elif kind == "details":
            children = self._details_page(self.series[card_id])
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
        for s in self.series.values():
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
        header = El(testid="events-list-results-header", text=f"{len(cards)} events found")
        return [header, El(testid="events-list-grid", children=cards)]

    def _details_page(self, s):
        selected = s.rows[s.selected]
        rows = []
        for i, row in enumerate(s.rows):
            def select(series=s, index=i):
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

        def register(series=s, row=selected):
            if series.on_register == "register":
                row.registered = True
            elif series.on_register == "dialog":
                def confirm():
                    row.registered = True
                    self.dialog = None
                self.dialog = El(role="dialog", children=[
                    El(text="Confirm your registration"),
                    El(text="Cancel", tag="button"),
                    El(text="Confirm", tag="button", on_click=confirm),
                ])
            elif series.on_register == "ineligible":
                self.ineligible = True

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
            ))
        children.append(El(testid="events-details-dates-list", children=rows))
        if s.restriction:
            children.append(El(testid="events-details-side-restriction-0",
                               children=[El(text="Skill Level Restriction"), El(text=s.restriction)]))
        return children
