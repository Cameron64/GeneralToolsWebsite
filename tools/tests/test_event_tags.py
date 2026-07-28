"""Audience tags: the composition rules, and the tag keys' journey from the form
to the PostedEvents row through both creation flows.

The emoji live in the event TITLE because the chapter's public Google Calendar
feed carries no per-event colour or category (see tools/eventTags.py). That makes
composeTitle the load-bearing piece: it runs on every publish, tagged or not, so
the no-tags path has to be a provable no-op.
"""
import datetime
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from tools import eventTags
from tools.eventViews import _buildEventPayload
from tools.forms import NewEventForm
from tools.models import DelegatedEvents, EventOwners, PostedEvents, PublishJob
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing

FUTURE = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)

ROSE = eventTags.TAGS_BY_KEY["newcomer"].emoji
FAMILY = eventTags.TAGS_BY_KEY["family"].emoji
BEER = eventTags.TAGS_BY_KEY["social"].emoji


def makeOwner(name, authorizers=()):
    owner = EventOwners.objects.create(name=name, isPermanent=True, expiration=FUTURE)
    for authorizer in authorizers:
        owner.authorizers.add(authorizer)
    return owner


def eventFormData(ownerName, **overrides):
    """A valid NewEventForm POST (virtual, so the location fields don't apply)."""
    data = {
        "owner": ownerName,
        "title": "Reading Group",
        "description": "Chapter reading group",
        "eventType": "1",  # VIRTUAL
        "timezone": "US/Central",
        "startTime": "2030-07-01T18:00",
        "endTime": "2030-07-01T19:00",
        "instructions": "Zoom link to follow",
        "city": "Austin",
        "state": "TX",
        "country": "US",
    }
    data.update(overrides)
    return data


def tagField(key):
    return eventTags.formFieldName(key)


class ComposeTitleTests(TestCase):
    def test_no_tags_is_a_pure_no_op(self):
        # The case almost every event takes. Must be byte-identical.
        self.assertEqual(eventTags.composeTitle("Reading Group", []), "Reading Group")
        self.assertEqual(eventTags.composeTitle("Reading Group", ()), "Reading Group")

    def test_no_tags_leaves_a_hand_typed_emoji_alone(self):
        # A publisher who typed their own emoji and ticked nothing keeps it -
        # stripping only ever happens for tags they actually selected.
        self.assertEqual(
            eventTags.composeTitle(f"{ROSE} Orientation", []),
            f"{ROSE} Orientation",
        )

    def test_single_tag_prefixes_the_emoji(self):
        self.assertEqual(
            eventTags.composeTitle("Orientation", ["newcomer"]),
            f"{ROSE} Orientation",
        )

    def test_emoji_order_follows_the_vocabulary_not_the_submission(self):
        # Selection order must not change the rendered sequence, or the same
        # pair of tags would look different on two events.
        forward = eventTags.composeTitle("Picnic", ["newcomer", "family"])
        backward = eventTags.composeTitle("Picnic", ["family", "newcomer"])
        self.assertEqual(forward, backward)
        self.assertEqual(forward, f"{ROSE}{FAMILY} Picnic")

    def test_does_not_double_prefix_a_title_the_user_already_marked(self):
        self.assertEqual(
            eventTags.composeTitle(f"{ROSE} Orientation", ["newcomer"]),
            f"{ROSE} Orientation",
        )

    def test_strips_a_whole_leading_run_of_selected_emoji(self):
        self.assertEqual(
            eventTags.composeTitle(f"{ROSE} {FAMILY} Picnic", ["newcomer", "family"]),
            f"{ROSE}{FAMILY} Picnic",
        )

    def test_preserves_a_decorative_emoji_belonging_to_an_unselected_tag(self):
        # "Rose Sale" is about roses. Tagging it family must not delete the
        # publisher's glyph just because it collides with the newcomer tag.
        self.assertEqual(
            eventTags.composeTitle(f"{ROSE} Rose Sale", ["family"]),
            f"{FAMILY} {ROSE} Rose Sale",
        )

    def test_title_that_is_only_the_selected_emoji_gains_no_trailing_space(self):
        # The strip consumes the whole title, so the naive f-string would leave
        # a dangling space on every external surface.
        self.assertEqual(eventTags.composeTitle(ROSE, ["newcomer"]), ROSE)
        self.assertEqual(eventTags.composeTitle(f"  {ROSE}  ", ["newcomer"]), ROSE)

    def test_variation_selector_is_not_left_orphaned_mid_title(self):
        # Apple keyboards emit some emoji followed by U+FE0F. startswith matches
        # the base code point, so without handling it the selector would survive
        # as an invisible character after our prefix.
        composed = eventTags.composeTitle(
            f"{ROSE}{eventTags.VARIATION_SELECTOR} Orientation", ["newcomer"]
        )
        self.assertEqual(composed, f"{ROSE} Orientation")

    def test_unknown_keys_are_ignored(self):
        # A retired tag key stored on an old row must not raise or render blank.
        self.assertEqual(eventTags.composeTitle("Picnic", ["nope"]), "Picnic")
        self.assertEqual(
            eventTags.composeTitle("Picnic", ["nope", "social"]),
            f"{BEER} Picnic",
        )


class TagsForTests(TestCase):
    def test_returns_vocabulary_order(self):
        tags = eventTags.tagsFor(["social", "newcomer"])
        self.assertEqual([tag.key for tag in tags], ["newcomer", "social"])

    def test_drops_unknown_keys_without_raising(self):
        self.assertEqual([tag.key for tag in eventTags.tagsFor(["gone", "family"])], ["family"])

    def test_empty_input(self):
        self.assertEqual(eventTags.tagsFor([]), [])
        self.assertEqual(eventTags.tagsFor(None), [])


class ComposeDescriptionTests(TestCase):
    def test_no_tags_is_a_pure_no_op(self):
        self.assertEqual(eventTags.composeDescription("Come along", []), "Come along")

    def test_appends_a_legend_explaining_each_emoji(self):
        composed = eventTags.composeDescription("Come along", ["newcomer", "family"])
        self.assertTrue(composed.startswith("Come along\n\n"))
        self.assertIn(f"{ROSE} Good for new members", composed)
        self.assertIn(f"{FAMILY} Kid and family friendly", composed)


@fastHashing
class NewEventFormTagTests(TestCase):
    """The form is the single choke point where composition happens, so these
    pin the boundary between "user typed" and "we composed"."""

    def setUp(self):
        self.publisher = UserFactory.make("publisher", perms=("publishEvent",))
        makeOwner("Education Committee", authorizers=[self.publisher])

    def test_untagged_submission_leaves_title_and_description_untouched(self):
        form = NewEventForm(eventFormData("Education Committee"))
        eventInfo = form.convertToEventInfo()
        self.assertEqual(form.getSelectedTagKeys(), [])
        self.assertEqual(eventInfo.title, "Reading Group")
        self.assertEqual(eventInfo.description, "Chapter reading group")

    def test_ticked_boxes_compose_title_and_description(self):
        form = NewEventForm(
            eventFormData(
                "Education Committee",
                **{tagField("newcomer"): "on", tagField("social"): "on"},
            )
        )
        eventInfo = form.convertToEventInfo()
        self.assertEqual(form.getSelectedTagKeys(), ["newcomer", "social"])
        self.assertEqual(eventInfo.title, f"{ROSE}{BEER} Reading Group")
        self.assertIn(f"{ROSE} Good for new members", eventInfo.description)
        self.assertTrue(eventInfo.description.startswith("Chapter reading group"))

    def test_tag_checkboxes_are_optional(self):
        form = NewEventForm(eventFormData("Education Committee"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_tag_fields_render_before_the_event_type_field(self):
        # order_fields hoists what it is given to the front, so a partial list
        # would push the checkboxes above the owner dropdown. Pin the layout.
        names = list(NewEventForm().fields)
        self.assertEqual(
            names[:4],
            ["owner", "title", "description", "tagsExplainer"],
        )
        self.assertLess(names.index(tagField("newcomer")), names.index("eventType"))
        self.assertLess(names.index("tagsExplainer"), names.index(tagField("newcomer")))


class PayloadTests(TestCase):
    def test_payload_version_is_unchanged_by_this_feature(self):
        # Bumping it would make pre-deploy CONFLICT jobs unpublishable, because
        # publish_anyway clones a stored payload verbatim including its version.
        self.assertEqual(PublishJob.PAYLOAD_VERSION, 2)

    def test_tags_default_to_empty_when_not_passed(self):
        from tools.tests.test_publish_jobs import makeEventInfo

        payload = _buildEventPayload(makeEventInfo(), False)
        self.assertEqual(payload["tags"], [])

    def test_tags_are_carried(self):
        from tools.tests.test_publish_jobs import makeEventInfo

        payload = _buildEventPayload(makeEventInfo(), False, ["newcomer"])
        self.assertEqual(payload["tags"], ["newcomer"])


def publishedResult():
    from tools.EventAutomation import EventAutomationDriver

    return EventAutomationDriver.Result(
        type=EventAutomationDriver.Result.ResultType.PUBLISHED,
        zoomAccount="events@austindsa.org",
        zoomLink="https://zoom.example/j/123",
        anManageLink="https://an.example/manage",
        anShareLink="https://an.example/share",
        gCalLink="https://gcal.example/event",
    )


@fastHashing
class DirectPublishTagTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.publisher = UserFactory.make("publisher", perms=("publishEvent",))
        self.owner = makeOwner("Education Committee", authorizers=[self.publisher])

    @patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.tasks.EventAutomationDriver.publishEvent")
    def test_tags_reach_the_posted_event_row(self, publishEvent, sendEmail):
        publishEvent.return_value = publishedResult()
        self.loginAs(self.publisher)
        self.client.post(
            reverse("new-event"),
            eventFormData(
                "Education Committee",
                **{tagField("newcomer"): "on", tagField("family"): "on"},
            ),
        )
        job = PublishJob.objects.get()
        self.assertEqual(job.payload["tags"], ["newcomer", "family"])
        self.assertEqual(job.payload["title"], f"{ROSE}{FAMILY} Reading Group")
        event = PostedEvents.objects.get()
        self.assertEqual(event.tags, ["newcomer", "family"])
        self.assertEqual(event.title, f"{ROSE}{FAMILY} Reading Group")
        self.assertEqual([tag.key for tag in event.getTags()], ["newcomer", "family"])

    @patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.tasks.EventAutomationDriver.publishEvent")
    def test_untagged_publish_stores_an_empty_list(self, publishEvent, sendEmail):
        publishEvent.return_value = publishedResult()
        self.loginAs(self.publisher)
        self.client.post(reverse("new-event"), eventFormData("Education Committee"))
        event = PostedEvents.objects.get()
        self.assertEqual(event.tags, [])
        self.assertEqual(event.title, "Reading Group")
        self.assertEqual(event.getTags(), [])

    @patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.tasks.EventAutomationDriver.publishEvent")
    def test_worker_tolerates_a_payload_written_before_tags_existed(self, publishEvent, sendEmail):
        # The version was deliberately not bumped, so the worker must accept a
        # payload with no "tags" key at all.
        from tools import tasks

        publishEvent.return_value = publishedResult()
        job = PublishJob.objects.create(
            kind=PublishJob.Kind.DIRECT,
            payload=_legacyPayload(),
            creator=self.publisher,
            owner=self.owner,
        )
        tasks.publishEventJob.call_local(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, PublishJob.Status.PUBLISHED)
        self.assertEqual(PostedEvents.objects.get().tags, [])


@fastHashing
class PublishAnywayTagTests(LoginClientMixin, TestCase):
    """Force-publishing past a gCal conflict clones the stored payload, so tags
    survive without any code of their own. Pinned here because a future refactor
    that rebuilt the clone field by field would silently drop them."""

    def setUp(self):
        self.creator = UserFactory.make("creator")
        self.owner = makeOwner("Education Committee", authorizers=[self.creator])

    @patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.tasks.EventAutomationDriver.publishEvent")
    def test_forced_publish_keeps_the_tags(self, publishEvent, sendEmail):
        from tools.tests.test_publish_jobs import makeEventInfo

        publishEvent.return_value = publishedResult()
        conflicted = PublishJob.objects.create(
            kind=PublishJob.Kind.DIRECT,
            status=PublishJob.Status.CONFLICT,
            payload=_buildEventPayload(
                makeEventInfo(title=f"{ROSE} Reading Group"), False, ["newcomer"]
            ),
            creator=self.creator,
            owner=self.owner,
        )
        self.loginAs(self.creator)
        self.client.post(reverse("publish-publish-anyway", kwargs={"jobId": conflicted.id}))
        clone = PublishJob.objects.exclude(id=conflicted.id).get()
        self.assertTrue(clone.payload["ignoreResolveableConflicts"])
        self.assertEqual(clone.payload["tags"], ["newcomer"])
        self.assertEqual(clone.payload["title"], f"{ROSE} Reading Group")
        event = PostedEvents.objects.get()
        self.assertEqual(event.tags, ["newcomer"])
        self.assertEqual(event.title, f"{ROSE} Reading Group")


def _legacyPayload():
    """A v2 payload as written before audience tags existed - no "tags" key."""
    from tools.tests.test_publish_jobs import makeEventInfo

    payload = _buildEventPayload(makeEventInfo(), False)
    del payload["tags"]
    return payload


@fastHashing
class DelegatedFlowTagTests(LoginClientMixin, TestCase):
    """Request -> approve -> publish. The tags are composed once at request time
    and must survive the round trip onto the published row."""

    def setUp(self):
        self.requester = UserFactory.make("requester", perms=("requestDelegatedEvent",))
        self.approver = UserFactory.make("approver", perms=("approveDelegatedEvent",))
        self.owner = makeOwner("Education Committee", authorizers=[self.approver])

    @patch("tools.eventViews.SecretManager")
    @patch("tools.eventViews.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.eventViews.EventAutomationDriver.publishEvent")
    def test_request_stores_composed_title_and_tag_keys(self, publishEvent, sendEmail, secrets):
        from tools.EventAutomation import EventAutomationDriver

        publishEvent.return_value = EventAutomationDriver.Result(
            type=EventAutomationDriver.Result.ResultType.NO_CONFLICTS
        )
        self.loginAs(self.requester)
        self.client.post(
            reverse("new-delegated-event"),
            eventFormData("Education Committee", **{tagField("family"): "on"}),
        )
        request = DelegatedEvents.objects.get()
        self.assertEqual(request.tags, ["family"])
        self.assertEqual(request.title, f"{FAMILY} Reading Group")
        self.assertEqual([tag.key for tag in request.getTags()], ["family"])

    @patch("tools.tasks.EmailApi.sendEmailFromWebsiteAccount")
    @patch("tools.tasks.EventAutomationDriver.publishEvent")
    def test_approve_copies_tags_onto_the_posted_event(self, publishEvent, sendEmail):
        publishEvent.return_value = publishedResult()
        moment = datetime.datetime(2030, 7, 1, 23, 0, tzinfo=datetime.UTC)
        request = DelegatedEvents.objects.create(
            title=f"{FAMILY} Tabling at the farmers market",
            start=moment,
            end=moment,
            timezone="America/Chicago",
            locationName="", streetAddress="", city="Austin", state="TX", zip="",
            country="US",
            description="Come say hi",
            instructions="Look for the banner",
            tags=["family"],
            dateCreated=moment,
            creator=self.requester,
            owner=self.owner,
            status=DelegatedEvents.Status.REQUESTED,
        )
        self.loginAs(self.approver)
        self.client.post(
            reverse("approve-delegated-event", kwargs={"id": request.id}),
            {"approve": "YES", "reason": "looks good"},
        )
        request.refresh_from_db()
        self.assertEqual(request.status, DelegatedEvents.Status.APPROVED)
        event = PostedEvents.objects.get()
        self.assertEqual(event.tags, ["family"])
        # Composed once, at request time - the approve path must not re-compose.
        self.assertEqual(event.title, f"{FAMILY} Tabling at the farmers market")
