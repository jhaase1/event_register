from types import SimpleNamespace

from user_intent import extract_user_intent


def _email(subject, body):
    return SimpleNamespace(subject=subject, body=body)


def test_reply_without_explicit_command_is_ignored():
    email = _email(
        "Re: Event Registration Confirmation: TUE, MARCH 17 1:30 - 4:00pm",
        "I don't think so. I checked my mail but didn't find it.\n\n"
        "On Sun, Mar 22, 2026 at 5:26 PM John Haase <x@y.com> wrote:\n"
        "> THU, APRIL 2 10:00am - 12:00pm",
    )

    action, details = extract_user_intent(email)

    assert action is None
    assert details is None


def test_reply_with_report_command_still_reports():
    email = _email(
        "Re: Event Registration Confirmation",
        "please send a report\n\n"
        "On Sun, Mar 22, 2026 at 5:26 PM John Haase <x@y.com> wrote:\n"
        "> old quoted content",
    )

    action, details = extract_user_intent(email)

    assert action == "report"
    assert details is None


def test_reply_with_remove_command_and_event_still_removes():
    email = _email(
        "Re: can you remove this",
        "Please remove THU, APRIL 2 10:00am - 12:00pm",
    )

    action, details = extract_user_intent(email)

    assert action == "remove"
    assert details == ("THU, APRIL 2", "10:00am - 12:00pm")
