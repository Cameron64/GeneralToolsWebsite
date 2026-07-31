"""publishEventJob re-checks authorization at EXECUTION time.

The enqueueing view's authorization check is a snapshot. In production Huey is
asynchronous (HUEY_IMMEDIATE=False), so an arbitrary amount of time passes
between the POST and the publish, and every fact the view checked is mutable in
that window: the permission can be revoked, the member can be dropped from
EventOwners.authorizers, the owner's expiration can pass, and a delegated
request can be denied by a different authorizer.

Publishing is not recallable - Action Network has no delete API - so
tasks._revalidateJobAuthorization fails CLOSED. Every refusal case here must
make NO external call, leave the job FAILED, and create no PostedEvents row.

Same conventions as test_publish_task.py: publishEvent is patched at its
tasks-module binding and task bodies run via .call_local().
"""
import datetime
from unittest import mock

from django.test import TestCase

from tools import tasks
from tools.eventViews import _buildEventPayload
from tools.models import DelegatedEvents, EventOwners, PostedEvents, PublishJob
from tools.tests.support import UserFactory, fastHashing
from tools.tests.test_publish_task import FUTURE, makeEventInfo, publishedResult

PAST = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)


@fastHashing
class RevalidateDirectPublishTests(TestCase):
    def setUp(self):
        self.creator = UserFactory.make("publisher", perms=("publishEvent",))
        self.owner = EventOwners.objects.create(
            name="Education Committee", isPermanent=True, expiration=FUTURE,
        )
        self.owner.authorizers.add(self.creator)

    def makeJob(self):
        return PublishJob.objects.create(
            kind=PublishJob.Kind.DIRECT,
            payload=_buildEventPayload(makeEventInfo(), False),
            creator=self.creator,
            owner=self.owner,
        )

    def runJob(self, job, result=None):
        with mock.patch("tools.tasks.EventAutomationDriver.publishEvent",
                        return_value=result) as publishEvent, \
             mock.patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount"):
            tasks.publishEventJob.call_local(job.id)
        job.refresh_from_db()
        return publishEvent

    def assertRefusedWithoutPublishing(self, job, publishEvent):
        publishEvent.assert_not_called()
        self.assertEqual(job.status, PublishJob.Status.FAILED)
        self.assertIsNone(job.postedEvent)
        self.assertEqual(PostedEvents.objects.count(), 0)
        self.assertIsNotNone(job.finishedAt)
        # The reason reaches the member through PublishJob.errorMessage (rendered
        # by new-event/unknown.html), so it must read as an explanation rather
        # than as a crash dump.
        self.assertTrue(job.errorMessage)
        self.assertNotIn("Traceback", job.errorMessage)

    def test_publishes_when_authorization_still_holds(self):
        """Control: the guard must not block a legitimately authorized job."""
        job = self.makeJob()
        publishEvent = self.runJob(job, result=publishedResult())
        publishEvent.assert_called_once()
        self.assertEqual(job.status, PublishJob.Status.PUBLISHED)

    def test_refuses_when_creator_dropped_from_authorizers(self):
        job = self.makeJob()
        self.owner.authorizers.remove(self.creator)
        publishEvent = self.runJob(job)
        self.assertRefusedWithoutPublishing(job, publishEvent)
        self.assertIn("no longer an authorizer", job.errorMessage)

    def test_refuses_when_publish_permission_revoked(self):
        job = self.makeJob()
        self.creator.user_permissions.clear()
        publishEvent = self.runJob(job)
        self.assertRefusedWithoutPublishing(job, publishEvent)
        self.assertIn("permission to publish events was removed", job.errorMessage)

    def test_refuses_when_owner_expired_while_queued(self):
        job = self.makeJob()
        self.owner.isPermanent = False
        self.owner.expiration = PAST
        self.owner.save()
        publishEvent = self.runJob(job)
        self.assertRefusedWithoutPublishing(job, publishEvent)
        self.assertIn("no longer active", job.errorMessage)

    def test_refuses_when_creator_deleted_while_queued(self):
        # PublishJob.creator is SET_NULL, so a deleted account reads as None
        # rather than raising. That must count as revocation, not as "nothing to
        # check".
        job = self.makeJob()
        self.creator.delete()
        publishEvent = self.runJob(job)
        self.assertRefusedWithoutPublishing(job, publishEvent)

    def test_refuses_when_owner_deleted_while_queued(self):
        job = self.makeJob()
        self.owner.delete()
        publishEvent = self.runJob(job)
        self.assertRefusedWithoutPublishing(job, publishEvent)


@fastHashing
class RevalidateDelegatedPublishTests(TestCase):
    """The delegated approve flow re-validates the APPROVER - who is the job's
    creator - plus the request's current review status."""

    def setUp(self):
        self.requester = UserFactory.make("requester", perms=("requestDelegatedEvent",))
        self.approver = UserFactory.make("approver", perms=("approveDelegatedEvent",))
        self.owner = EventOwners.objects.create(
            name="Education Committee", isPermanent=True, expiration=FUTURE,
        )
        self.owner.authorizers.add(self.approver)
        self.event = DelegatedEvents.objects.create(
            title="Tabling at the farmers market",
            start=datetime.datetime(2030, 7, 1, 23, 0, tzinfo=datetime.UTC),
            end=datetime.datetime(2030, 7, 2, 0, 0, tzinfo=datetime.UTC),
            timezone="America/Chicago",
            locationName="Mueller Lake Park", streetAddress="4550 Mueller Blvd",
            city="Austin", state="TX", zip="78723", country="US",
            description="A table, some flyers", instructions="Look for the red banner",
            dateCreated=datetime.datetime.now(datetime.UTC),
            creator=self.requester, owner=self.owner,
            status=DelegatedEvents.Status.REQUESTED,
        )

    def makeJob(self):
        payload = _buildEventPayload(self.event.getEventInfo(), ignoreResolveableConflicts=True)
        payload["reason"] = "Looks good"
        payload["approverId"] = self.approver.id
        return PublishJob.objects.create(
            kind=PublishJob.Kind.DELEGATED, payload=payload,
            creator=self.approver, owner=self.owner, delegatedEvent=self.event,
        )

    def runJob(self, job, result=None):
        with mock.patch("tools.tasks.EventAutomationDriver.publishEvent",
                        return_value=result) as publishEvent, \
             mock.patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount"):
            tasks.publishEventJob.call_local(job.id)
        job.refresh_from_db()
        return publishEvent

    def test_publishes_and_approves_when_authorization_still_holds(self):
        job = self.makeJob()
        publishEvent = self.runJob(job, result=publishedResult())
        publishEvent.assert_called_once()
        self.assertEqual(job.status, PublishJob.Status.PUBLISHED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, DelegatedEvents.Status.APPROVED)

    def test_refuses_when_request_denied_while_queued(self):
        """Before this guard, a denial landing in the queue window was published
        anyway and _finishDelegatedPublish then overwrote the row to APPROVED."""
        job = self.makeJob()
        self.event.status = DelegatedEvents.Status.DENIED
        self.event.save()

        publishEvent = self.runJob(job)

        publishEvent.assert_not_called()
        self.assertEqual(job.status, PublishJob.Status.FAILED)
        self.assertEqual(PostedEvents.objects.count(), 0)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, DelegatedEvents.Status.DENIED)
        self.assertIn("already denied", job.errorMessage)

    def test_refuses_when_request_already_approved_by_a_competing_job(self):
        job = self.makeJob()
        self.event.status = DelegatedEvents.Status.APPROVED
        self.event.save()
        publishEvent = self.runJob(job)
        publishEvent.assert_not_called()
        self.assertEqual(job.status, PublishJob.Status.FAILED)
        self.assertEqual(PostedEvents.objects.count(), 0)

    def test_refuses_when_approver_dropped_from_authorizers(self):
        job = self.makeJob()
        self.owner.authorizers.remove(self.approver)
        publishEvent = self.runJob(job)
        publishEvent.assert_not_called()
        self.assertEqual(job.status, PublishJob.Status.FAILED)
        # The request stays reviewable by whoever is still an authorizer.
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, DelegatedEvents.Status.REQUESTED)

    def test_refuses_when_approve_permission_revoked(self):
        job = self.makeJob()
        self.approver.user_permissions.clear()
        publishEvent = self.runJob(job)
        publishEvent.assert_not_called()
        self.assertEqual(job.status, PublishJob.Status.FAILED)
        self.assertIn("approve delegated events", job.errorMessage)

    def test_denial_landing_mid_publish_does_not_overwrite_the_decision(self):
        """The residual race the pre-publish guard cannot close.

        The real publish takes 15-30s. A denial landing inside that window
        arrives after the external event already exists and cannot be recalled,
        so the PostedEvents row is still written as the record of what is out
        there - but the human's DENIED decision must survive.
        """
        job = self.makeJob()

        def denyMidPublish(*args, **kwargs):
            # .update() so the in-memory self.event is untouched, mimicking a
            # denial committed by a different request mid-publish.
            DelegatedEvents.objects.filter(id=self.event.id).update(
                status=DelegatedEvents.Status.DENIED
            )
            return publishedResult()

        with mock.patch("tools.tasks.EventAutomationDriver.publishEvent",
                        side_effect=denyMidPublish), \
             mock.patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount"):
            tasks.publishEventJob.call_local(job.id)
        job.refresh_from_db()

        # The external event exists, so it is recorded.
        self.assertEqual(job.status, PublishJob.Status.PUBLISHED)
        self.assertEqual(PostedEvents.objects.count(), 1)
        # ...but the denial is not silently converted into an approval.
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, DelegatedEvents.Status.DENIED)
