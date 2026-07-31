"""The delegated-request conflict page must not leak the chapter's calendars.

new_delegated_event is gated on requestDelegatedEvent alone - no owner-authorizer
membership - and it runs a full publishEvent dry run synchronously, before the
request row exists and before any approval. That dry run queries the central Zoom
and Google Calendar under service credentials, so it can see every event on them.

Rendering those conflicts verbatim handed the requester the titles of unrelated
meetings plus the host's Zoom account, for any time window they chose to submit.
The times stay (that is what makes a conflict actionable); the titles and host
accounts are redacted for anyone who is not an authorizer for the owner.

External services never run here: publishEvent is patched at its eventViews
binding, so the "conflict" is entirely synthetic.
"""
import datetime
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from tools.eventViews import REDACTED_CONFLICT_TITLE
from tools.EventAutomation import EventAutomationDriver
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing
from tools.tests.test_event_owners import eventFormData, makeOwner
from tools.timezones import DateTimeWithAcceptedTimeZone

SECRET_GCAL_TITLE = "Steering Committee: staff discipline case"
SECRET_ZOOM_TITLE = "Legal strategy call with counsel"
SECRET_ZOOM_HOST = "chair@austindsa.org"


def gCalConflict():
    return EventAutomationDriver.Conflict(
        type=EventAutomationDriver.Conflict.ConflictType.GCAL,
        title=SECRET_GCAL_TITLE,
        start=DateTimeWithAcceptedTimeZone(
            wallTime=datetime.datetime(2030, 7, 1, 18, 30), zoneName="America/Chicago"),
        end=DateTimeWithAcceptedTimeZone(
            wallTime=datetime.datetime(2030, 7, 1, 19, 30), zoneName="America/Chicago"),
        zoomUser=None,
    )


def zoomConflict():
    return EventAutomationDriver.Conflict(
        type=EventAutomationDriver.Conflict.ConflictType.ZOOM,
        title=SECRET_ZOOM_TITLE,
        start=DateTimeWithAcceptedTimeZone(
            wallTime=datetime.datetime(2030, 7, 1, 18, 30), zoneName="America/Chicago"),
        end=DateTimeWithAcceptedTimeZone(
            wallTime=datetime.datetime(2030, 7, 1, 19, 30), zoneName="America/Chicago"),
        zoomUser=SECRET_ZOOM_HOST,
    )


def conflictResult(resultType, conflicts):
    return EventAutomationDriver.Result(type=resultType, conflicts=conflicts)


@fastHashing
class DelegatedRequestConflictRedactionTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.requester = UserFactory.make("requester", perms=("requestDelegatedEvent",))
        self.authorizer = UserFactory.make("authorizer", perms=("requestDelegatedEvent",))
        # A healthy owner the requester is NOT an authorizer for.
        self.owner = makeOwner(
            "Education Committee", isPermanent=True, authorizers=[self.authorizer],
        )

    def postRequest(self, resultType, conflicts):
        with patch("tools.eventViews.EventAutomationDriver.publishEvent",
                   return_value=conflictResult(resultType, conflicts)), \
             patch("tools.eventViews.EmailApi.sendEmailFromWebsiteAccount"):
            return self.client.post(reverse("new-delegated-event"), eventFormData(self.owner.name))

    def test_unresolveable_conflict_hides_zoom_title_and_host(self):
        self.loginAs(self.requester)
        resp = self.postRequest(
            EventAutomationDriver.Result.ResultType.UNRESOLVEABLE_CONFLICT, [zoomConflict()],
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(SECRET_ZOOM_TITLE, body)
        self.assertNotIn(SECRET_ZOOM_HOST, body)
        self.assertIn(REDACTED_CONFLICT_TITLE, body)

    def test_resolveable_conflict_hides_calendar_title(self):
        self.loginAs(self.requester)
        resp = self.postRequest(
            EventAutomationDriver.Result.ResultType.CONFLICT, [gCalConflict()],
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(SECRET_GCAL_TITLE, body)
        self.assertIn(REDACTED_CONFLICT_TITLE, body)

    def test_conflict_times_are_still_shown(self):
        """Redaction must not make the conflict useless.

        The requester still needs to know which slot is taken so they can pick
        another - only the identity of the other event is withheld.
        """
        self.loginAs(self.requester)
        resp = self.postRequest(
            EventAutomationDriver.Result.ResultType.CONFLICT, [gCalConflict()],
        )
        body = resp.content.decode()
        # conflictList.html renders wall time + zone, e.g.
        # "2030-07-01 18:30 (America/Chicago) to 2030-07-01 19:30 (America/Chicago)"
        self.assertIn("2030-07-01 18:30", body)
        self.assertIn("2030-07-01 19:30", body)

    def test_authorizer_still_sees_full_detail(self):
        """An authorizer for the owner reaches the same detail via the approve
        flow, so withholding it from them would remove information they already
        hold rather than protecting anything."""
        self.loginAs(self.authorizer)
        resp = self.postRequest(
            EventAutomationDriver.Result.ResultType.UNRESOLVEABLE_CONFLICT, [zoomConflict()],
        )
        body = resp.content.decode()
        self.assertIn(SECRET_ZOOM_TITLE, body)
        self.assertIn(SECRET_ZOOM_HOST, body)
        self.assertNotIn(REDACTED_CONFLICT_TITLE, body)

    def test_multiple_conflicts_are_all_redacted(self):
        """Guard against redacting only the first entry."""
        self.loginAs(self.requester)
        resp = self.postRequest(
            EventAutomationDriver.Result.ResultType.UNRESOLVEABLE_CONFLICT,
            [zoomConflict(), gCalConflict()],
        )
        body = resp.content.decode()
        self.assertNotIn(SECRET_ZOOM_TITLE, body)
        self.assertNotIn(SECRET_ZOOM_HOST, body)
        self.assertNotIn(SECRET_GCAL_TITLE, body)
        self.assertEqual(body.count(REDACTED_CONFLICT_TITLE), 2)
