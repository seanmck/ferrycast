"""Timezone fingerprinting.

Deliberately built on synthetic zones rather than real ones. A test asserting that
America/Vancouver changes clocks on a particular date would pass or fail depending on which
tz database the machine happens to ship — which is the very problem this code exists to make
visible, and a poor thing to pin a test to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

from ferrycast.timeutil import UTC, _offset_text, timezone_rule


class SwitchingZone(tzinfo):
    """A zone that moves its clocks once, on a known date."""

    def __init__(self, before, after, switch):
        self._before, self._after, self._switch = before, after, switch

    def utcoffset(self, dt):
        if dt is None:
            return self._before
        naive = dt.replace(tzinfo=None)
        return self._after if naive >= self._switch else self._before

    def tzname(self, dt):
        return "AFT" if self.utcoffset(dt) == self._after else "BEF"

    def dst(self, dt):
        return timedelta(0)

    def __str__(self):
        return "Test/Switching"


def test_offset_is_rendered_the_way_a_human_compares_two_machines():
    assert _offset_text(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-7)))) == "UTC-07:00"
    assert _offset_text(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == (
        "UTC+05:30"
    )
    assert _offset_text(datetime(2026, 1, 1, tzinfo=UTC)) == "UTC+00:00"


def test_a_zone_that_never_changes_reports_no_transition():
    zone = timezone(timedelta(hours=-7), "PDT")
    rule = timezone_rule(zone, at=datetime(2026, 8, 12, tzinfo=UTC))
    assert rule.next_transition is None
    assert rule.offset_now == "UTC-07:00"
    assert rule.offset_in_a_year == "UTC-07:00"
    assert "none within 2 years" in rule.summary()


def test_a_zone_that_changes_reports_when():
    zone = SwitchingZone(timedelta(hours=-7), timedelta(hours=-8), datetime(2026, 11, 1))
    rule = timezone_rule(zone, at=datetime(2026, 8, 12, tzinfo=UTC))
    assert rule.next_transition == "2026-11-01"
    assert rule.offset_now == "UTC-07:00"
    assert rule.offset_in_a_year == "UTC-08:00"


def test_the_summary_carries_everything_needed_to_spot_a_mismatch():
    """The failure this guards against is two machines disagreeing, so the line has to hold
    enough to compare: what the offset is now, whether a change is coming, and where it ends
    up. An hour's disagreement about "11:45 local" moves every sailing window."""
    zone = SwitchingZone(timedelta(hours=-7), timedelta(hours=-8), datetime(2026, 11, 1))
    summary = timezone_rule(zone, at=datetime(2026, 8, 12, tzinfo=UTC)).summary()
    assert "Test/Switching" in summary
    assert "UTC-07:00" in summary and "UTC-08:00" in summary
    assert "2026-11-01" in summary
