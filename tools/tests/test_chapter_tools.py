"""Chapter Tools (IT access registry, M1). All data here is invented - never
real chapter services, credentials, or people (this repo is public).

Covers: the requestable/steward clean() rule, the open/restricted visibility
split on the directory + detail pages, the questions workbench's permission
gate, the read-log write-exactly-on-restricted-render contract, and the
seed_chapter_tools management command's idempotency.
"""
import datetime
import io
import json
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.html import escape
from django.urls import reverse

from tools.models import (
    ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder, ResourceQuestion,
    ToolAuditReadLog,
)
from tools import forms as chapterForms
from tools.forms import ChapterResourceForm, ResourceDependencyForm
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing


# The rendered marker for a SIGN_IN edge, in one place because the RUNS_ON leak
# guard asserts its ABSENCE - and an absence assertion that stops matching the
# markup silently passes forever. That already happened once: the guard used to
# assert "You need <name> first", then the dependency name became a link
# ("You need <a href=...>Name</a> first"), which no longer contains that string.
# The test kept passing while checking nothing.
#
# So this phrase is deliberately chosen to be CONTIGUOUS TEXT with no markup
# inside it. If you restyle the edge, keep one unbroken run of text containing
# the resource name, or this guard quietly stops guarding.
EDGE_PHRASE = "How to get {name}"

# The holder name - the thing viewResourceHolders/viewChapterToolAudit gates.
# Every assertNotContains(HOLDER_SENTINEL) for a viewer without either
# permission is paired with an assertContains(HOLDER_SENTINEL) for one who has
# one, using this exact literal. That pairing is the mechanism, not decoration:
# it is what stops the guard from going stale the way the EDGE_PHRASE guard
# once did above - if a markup change ever stops the name from rendering at
# all, the PRESENCE assertion fails loudly instead of the absence assertion
# quietly passing for the wrong reason.
HOLDER_SENTINEL = "Example Holder"


def _makeResource(**overrides):
    defaults = {
        "name": "Example Wiki",
        "blurb": "The chapter's knowledge base.",
        "category": ChapterResource.Category.COMMUNICATION,
        "accessModel": ChapterResource.AccessModel.SHARED_VAULT,
        "payer": ChapterResource.Payer.CHAPTER,
        "howToGetAccess": "Ask in #it-committee.",
    }
    defaults.update(overrides)
    return ChapterResource.objects.create(**defaults)


class ChapterResourceCleanTests(TestCase):
    def test_requestable_without_steward_raises(self):
        resource = _makeResource(requestable=True)
        with self.assertRaises(ValidationError):
            resource.clean()

    def test_requestable_with_steward_passes(self):
        steward = UserFactory.make("steward")
        # A tier is needed as well as a steward: requestable is only allowed on
        # Green or Yellow, and _makeResource leaves the model default
        # (Unclassified), which is deliberately not requestable.
        resource = _makeResource(
            requestable=True, steward=steward,
            delegationTier=ChapterResource.DelegationTier.GREEN,
        )
        resource.clean()  # must not raise

    def test_not_requestable_without_steward_passes(self):
        resource = _makeResource(requestable=False)
        resource.clean()  # must not raise


@fastHashing
class ChapterToolsVisibilityTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.member = UserFactory.make("member")
        self.auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
        # Holds ONLY the new tier-1 permission - not audit - so it exercises
        # the "or" side of _hasHolders independently of _hasAudit.
        self.organizer = UserFactory.make("organizer", perms=("viewResourceHolders",))
        self.resource = _makeResource(
            delegationTier=ChapterResource.DelegationTier.RED,
            revocationNote="Rotate the shared vault login.",
            continuityNote="Backup admin: Example Backup.",
            lastReviewed=None,  # stale by default
        )
        ResourceHolder.objects.create(
            resource=self.resource, personName=HOLDER_SENTINEL, confirmed=False,
        )
        # Deliberately distinctive: this label is the sentinel for "a credential
        # row leaked to a plain member", so it must not collide with any word the
        # open layer legitimately prints (an earlier "Shared login" collided with
        # the access-model label and made the leak test pass for the wrong reason).
        ResourceCredential.objects.create(
            resource=self.resource, label="ExampleCredentialSentinel",
            kind=ResourceCredential.Kind.VAULT_SHARED_LOGIN,
        )

    def test_index_open_layer_visible_to_any_member(self):
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Example Wiki")
        self.assertContains(resp, "Ask in #it-committee.")
        # Holder identity is tier 1 now - inverted from the original assertion
        # here (see HOLDER_SENTINEL comment above for why this is only safe
        # paired with test_index_holder_sentinel_shown_to_organizer below).
        self.assertNotContains(resp, HOLDER_SENTINEL)

    def test_index_holder_sentinel_shown_to_organizer(self):
        """Pairs with test_index_open_layer_visible_to_any_member's absence
        assertion - identical literal, opposite viewer."""
        self.loginAs(self.organizer)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertContains(resp, HOLDER_SENTINEL)

    def test_index_hides_stale_flag_from_plain_member(self):
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertNotContains(resp, "Stale review")

    def test_index_shows_stale_flag_to_auditor(self):
        self.loginAs(self.auditor)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertContains(resp, "Stale review")

    def test_detail_open_section_visible_to_any_member(self):
        self.loginAs(self.member)
        resp = self.client.get(self.resource.getUrl())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Example Wiki")
        # Inverted from the original assertion here - see
        # test_detail_holder_sentinel_shown_to_organizer for the paired
        # presence assertion of the identical literal.
        self.assertNotContains(resp, HOLDER_SENTINEL)
        # The tier-0 fallback replaces the roster, not silence.
        self.assertContains(resp, "Access to this is stewarded by the IT Sub-Committee.")

    def test_detail_holder_sentinel_shown_to_organizer(self):
        """Pairs with test_detail_open_section_visible_to_any_member's
        absence assertion - identical literal, opposite viewer."""
        self.loginAs(self.organizer)
        resp = self.client.get(self.resource.getUrl())
        self.assertContains(resp, HOLDER_SENTINEL)

    def test_detail_hides_restricted_section_from_plain_member(self):
        self.loginAs(self.member)
        resp = self.client.get(self.resource.getUrl())
        self.assertNotContains(resp, "Committee only")
        self.assertNotContains(resp, "Rotate the shared vault login.")
        self.assertNotContains(resp, "ExampleCredentialSentinel")

    def test_detail_hides_restricted_section_from_organizer(self):
        """The organizer holds viewResourceHolders, not viewChapterToolAudit -
        it must unlock the holder tier without also unlocking the audit
        tier. Mirrors test_detail_hides_restricted_section_from_plain_member,
        but the organizer DOES see the holder roster (see
        test_detail_holder_sentinel_shown_to_organizer)."""
        self.loginAs(self.organizer)
        resp = self.client.get(self.resource.getUrl())
        self.assertNotContains(resp, "Committee only")
        self.assertNotContains(resp, "Rotate the shared vault login.")
        self.assertNotContains(resp, "ExampleCredentialSentinel")

    def test_detail_shows_restricted_section_to_auditor(self):
        self.loginAs(self.auditor)
        resp = self.client.get(self.resource.getUrl())
        self.assertContains(resp, "Committee only")
        self.assertContains(resp, "Rotate the shared vault login.")
        self.assertContains(resp, "ExampleCredentialSentinel")

    def test_detail_shows_holder_sentinel_to_auditor(self):
        """Audit implies holders (_hasHolders's `or`) - the auditor sees the
        roster too, not just the restricted committee-detail section."""
        self.loginAs(self.auditor)
        resp = self.client.get(self.resource.getUrl())
        self.assertContains(resp, HOLDER_SENTINEL)

    def test_detail_explains_the_access_model_in_plain_language(self):
        """A member who has never met the phrase "shared vault login" must not
        have to guess what it costs them - the label always ships with its
        explanation."""
        self.loginAs(self.member)
        resp = self.client.get(self.resource.getUrl())
        self.assertContains(resp, "you need a vault account")

    def test_index_legend_covers_every_access_model_shown(self):
        """Superseded by pagination (ChapterToolsDirectoryPagingTests): the
        access-model disclosure now moved onto each card and deliberately
        lists the COMPLETE ladder every time - see
        ChapterResource.getAccessModelLegend's docstring. A legend built from
        only the models on screen was the exact bug pagination would have
        reintroduced (a phrase defined on page one, undefined on page two),
        so this only checks presence now, not absence."""
        _makeResource(name="Example Bank", accessModel=ChapterResource.AccessModel.INDIVIDUAL)
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        # Sentinels avoid apostrophes on purpose - the template escapes them to
        # &#x27; and a raw "person's" would never match the rendered HTML.
        self.assertContains(resp, "you need a vault account")          # SHARED_VAULT, in use
        self.assertContains(resp, "does not affect anybody else")      # INDIVIDUAL, in use

    def test_index_does_not_lay_the_directory_out_as_a_table(self):
        """The directory renders as cards, not rows.

        It was a 6-column data-table and two of those columns held full
        sentences. .page-card sets overflow-x:auto, so table auto-layout gave
        the prose its max-content width and pushed the rest off the right edge
        - a member had to scroll sideways to reach "How to get access", the one
        thing they came for. Nothing about that is fixable by resizing columns,
        so this asserts the container itself, which is the part that regressed.
        """
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertNotContains(resp, "data-table")
        # ...and the answer still renders in full rather than being truncated
        # to fit a column.
        self.assertContains(resp, "Ask in #it-committee.")
        # The template explains the above in a {% comment %} block. Django's
        # {# #} form is single-line only, so writing that rationale as a
        # multi-line {# #} silently prints it as body text - it shipped that
        # way once. This sentinel is a word only that comment uses.
        self.assertNotContains(resp, "overflow-x:auto")

    def test_questions_view_redirects_plain_member(self):
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools-questions"))
        # permission_required (no raise_exception) redirects rather than 403s -
        # same convention as accessViews.manage_access.
        self.assertEqual(resp.status_code, 302)

    def test_questions_view_accessible_to_auditor(self):
        self.loginAs(self.auditor)
        resp = self.client.get(reverse("chapter-tools-questions"))
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_user_is_redirected_from_every_route(self):
        for url in (reverse("chapter-tools"), self.resource.getUrl(), reverse("chapter-tools-questions")):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 302)


@fastHashing
class ChapterToolsReadLogTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.member = UserFactory.make("member")
        self.auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
        self.organizer = UserFactory.make("organizer", perms=("viewResourceHolders",))
        self.superuser = UserFactory.superuser("root")
        self.resource = _makeResource()

    def test_open_only_index_render_writes_no_log(self):
        self.loginAs(self.member)
        self.client.get(reverse("chapter-tools"))
        self.assertEqual(ToolAuditReadLog.objects.count(), 0)

    def test_open_only_detail_render_writes_no_log(self):
        self.loginAs(self.member)
        self.client.get(self.resource.getUrl())
        self.assertEqual(ToolAuditReadLog.objects.count(), 0)

    def test_holder_tier_only_index_render_writes_no_log(self):
        """The read-log stays audit-only (PROPOSAL.md §9) - an organizer's
        holder-tier render must not write a row. This is exactly where a
        log-on-the-wrong-tier bug would slip in during the gate refactor."""
        self.loginAs(self.organizer)
        self.client.get(reverse("chapter-tools"))
        self.assertEqual(ToolAuditReadLog.objects.count(), 0)

    def test_holder_tier_only_detail_render_writes_no_log(self):
        self.loginAs(self.organizer)
        self.client.get(self.resource.getUrl())
        self.assertEqual(ToolAuditReadLog.objects.count(), 0)

    def test_restricted_index_render_writes_one_row(self):
        self.loginAs(self.auditor)
        self.client.get(reverse("chapter-tools"))
        self.assertEqual(ToolAuditReadLog.objects.count(), 1)
        row = ToolAuditReadLog.objects.get()
        self.assertEqual(row.user, self.auditor)
        self.assertEqual(row.target, "index")

    def test_restricted_detail_render_writes_one_row_targeting_the_resource(self):
        self.loginAs(self.auditor)
        self.client.get(self.resource.getUrl())
        self.assertEqual(ToolAuditReadLog.objects.count(), 1)
        row = ToolAuditReadLog.objects.get()
        self.assertEqual(row.target, self.resource.name)

    def test_questions_render_writes_one_row_and_post_writes_none(self):
        self.loginAs(self.auditor)
        self.client.get(reverse("chapter-tools-questions"))
        self.assertEqual(ToolAuditReadLog.objects.count(), 1)
        self.assertEqual(ToolAuditReadLog.objects.get().target, "questions")

        self.client.post(reverse("chapter-tools-questions"), {"action": "add", "question": "Test question?"})
        # The POST redirects and never renders the restricted section itself -
        # only the GET that follows (if any) would add another row.
        self.assertEqual(ToolAuditReadLog.objects.count(), 1)

    def test_superuser_read_is_logged_same_as_anyone_else(self):
        # "The log protects you too" - superuser bypass must not be a logging
        # bypass, or the flagship custody artifact would exempt its own admin.
        self.loginAs(self.superuser)
        self.client.get(self.resource.getUrl())
        self.assertEqual(ToolAuditReadLog.objects.count(), 1)
        self.assertEqual(ToolAuditReadLog.objects.get().user, self.superuser)


@fastHashing
class ChapterToolsQuestionsWorkflowTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
        self.resource = _makeResource()

    def test_add_assign_and_resolve_a_question(self):
        self.loginAs(self.auditor)
        url = reverse("chapter-tools-questions")

        self.client.post(url, {"action": "add", "resource": self.resource.id, "question": "Who has recovery codes?"})
        question = ResourceQuestion.objects.get()
        self.assertEqual(question.resource, self.resource)
        self.assertFalse(question.isResolved())

        self.client.post(url, {"action": "assign", "questionId": question.id, "assignedTo": "Example Assignee"})
        question.refresh_from_db()
        self.assertEqual(question.assignedTo, "Example Assignee")

        self.client.post(url, {"action": "resolve", "questionId": question.id, "resolution": "Rotated."})
        question.refresh_from_db()
        self.assertTrue(question.isResolved())
        self.assertEqual(question.resolution, "Rotated.")

    def test_add_chapter_wide_question_with_no_resource(self):
        self.loginAs(self.auditor)
        self.client.post(reverse("chapter-tools-questions"), {"action": "add", "question": "Chapter-wide question?"})
        question = ResourceQuestion.objects.get()
        self.assertIsNone(question.resource)

    def test_stale_repost_with_non_numeric_ids_redirects_instead_of_500(self):
        """A stale form / back-button repost can submit a non-numeric
        resource or questionId. That must redirect back to the questions
        page, not raise ValueError into a 500."""
        self.loginAs(self.auditor)
        url = reverse("chapter-tools-questions")

        addResponse = self.client.post(
            url, {"action": "add", "resource": "not-an-id", "question": "Stale repost?"},
        )
        self.assertRedirects(addResponse, url)
        question = ResourceQuestion.objects.get()
        self.assertIsNone(question.resource)

        assignResponse = self.client.post(
            url, {"action": "assign", "questionId": "not-an-id", "assignedTo": "Nobody"},
        )
        self.assertRedirects(assignResponse, url)

        resolveResponse = self.client.post(
            url, {"action": "resolve", "questionId": "not-an-id", "resolution": "N/A"},
        )
        self.assertRedirects(resolveResponse, url)
        question.refresh_from_db()
        self.assertFalse(question.isResolved())


@fastHashing
class ResourceDependencyTests(LoginClientMixin, TestCase):
    """The dependency edges (SIGN_IN, RUNS_ON, REACHED_THROUGH): forward +
    reverse rendering across the open/restricted split, self-reference and
    duplicate rejection, and the index prefetch's query-count contract. See the
    plan (chapter-tools-dependencies) for why the kinds are a fixed curated set
    rather than an open graph, and why cycles are deliberately not prevented.

    The set is fixed, not frozen: REACHED_THROUGH was added when a real
    resource (the chapter calendar) turned out to be one nobody is ever granted
    directly, which neither existing kind could say.
    """

    def setUp(self):
        self.member = UserFactory.make("member")
        self.auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
        self.organizer = UserFactory.make("organizer", perms=("viewResourceHolders",))
        self.wiki = _makeResource(name="Example Wiki")
        self.identityProvider = _makeResource(
            name="Example SSO", category=ChapterResource.Category.COMMUNICATION,
        )
        # Deliberately named per the plan's own sentinel choice - distinct from
        # ExampleCredentialSentinel above, which guards a different leak.
        self.hostingSentinel = _makeResource(
            name="ExampleHostingSentinel", category=ChapterResource.Category.INFRASTRUCTURE,
        )

    # --- 1. RUNS_ON never reaches a plain member on the DETAIL page ---

    def test_runs_on_hidden_from_plain_member_on_detail(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.member)
        resp = self.client.get(self.wiki.getUrl())
        self.assertNotContains(resp, "ExampleHostingSentinel")

    def test_runs_on_shown_to_auditor_on_detail(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.auditor)
        resp = self.client.get(self.wiki.getUrl())
        self.assertContains(resp, "ExampleHostingSentinel")

    # --- 2. RUNS_ON never reaches a plain member on the INDEX page ---
    # The single most important test in the change (the plan's own words).
    # The only thing keeping a restricted edge off the open directory is the
    # kind=SIGN_IN filter inside the index view's Prefetch. Assert the
    # rendered EDGE PHRASE, not the bare resource name - resource existence is
    # already fully open (the index lists every ChapterResource for any
    # logged-in member), so the sentinel legitimately appears on screen as its
    # own card and asserting its bare name would fail spuriously.

    def test_runs_on_never_leaks_the_edge_phrase_onto_the_index_for_a_plain_member(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        self.assertContains(resp, "ExampleHostingSentinel")  # its own card - legitimately open
        self.assertNotContains(resp, EDGE_PHRASE.format(name="ExampleHostingSentinel"))  # the edge - not open

    # --- 3. SIGN_IN does reach a plain member, index and detail ---

    def test_sign_in_reaches_plain_member_on_index_and_detail(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)

        indexResp = self.client.get(reverse("chapter-tools"))
        self.assertContains(indexResp, EDGE_PHRASE.format(name="Example SSO"))

        detailResp = self.client.get(self.wiki.getUrl())
        self.assertContains(detailResp, EDGE_PHRASE.format(name="Example SSO"))

    def test_sign_in_dependency_links_to_the_thing_it_names(self):
        """Naming a precondition without linking it hands the reader a second
        task and no route to it. Both surfaces must link."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)
        target = f'href="{self.identityProvider.getUrl()}"'

        self.assertContains(self.client.get(reverse("chapter-tools")), target)
        self.assertContains(self.client.get(self.wiki.getUrl()), target)

    # --- 4. Reverse SIGN_IN renders on the depended-upon resource's page ---

    def test_reverse_sign_in_renders_on_depended_upon_resource_for_plain_member(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)
        resp = self.client.get(self.identityProvider.getUrl())
        self.assertContains(resp, "What signs in through this")
        self.assertContains(resp, self.wiki.getUrl())

    # --- 5. Reverse RUNS_ON renders only for an auditor ---

    def test_reverse_runs_on_hidden_from_plain_member(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.member)
        resp = self.client.get(self.hostingSentinel.getUrl())
        self.assertNotContains(resp, "What runs on this")

    def test_reverse_runs_on_shown_to_auditor(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.auditor)
        resp = self.client.get(self.hostingSentinel.getUrl())
        self.assertContains(resp, "What runs on this")
        self.assertContains(resp, self.wiki.getUrl())

    # --- 6. Every kind must declare which layer it belongs to ---

    def test_every_kind_declares_its_layer(self):
        """The guard that makes adding a kind safe. OPEN_KINDS/RESTRICTED_KINDS
        drive every dependency query in the views, so a kind in neither renders
        nowhere (a silent feature) and a kind in both is a contradiction. Either
        way the author has not made the visibility decision, and this fails
        instead of picking one for them.

        This is the same rule as the `kind` field having no default: an
        undecided layer must fail loudly rather than fail open."""
        declared = set(ResourceDependency.OPEN_KINDS) | set(ResourceDependency.RESTRICTED_KINDS)
        allKinds = {value for value, _label in ResourceDependency.KIND_CHOICES}
        self.assertEqual(
            allKinds, declared,
            "A ResourceDependency.Kind is missing from OPEN_KINDS/RESTRICTED_KINDS. "
            "Add it to exactly one and decide whether members should see it.",
        )
        self.assertEqual(
            set(ResourceDependency.OPEN_KINDS) & set(ResourceDependency.RESTRICTED_KINDS), set(),
            "A kind cannot be both open and restricted.",
        )

    def test_index_never_prefetches_a_restricted_edge_for_a_plain_member(self):
        """Covers OPEN_KINDS directly, against the context rather than the HTML.

        The rendered leak test above cannot fail this one for us: the index
        template loops over each open kind by name, so an edge wrongly declared
        open renders nowhere and the page looks clean. That is a good
        fail-closed property and a bad test - it means the page passing proves
        nothing about the queryset. Assert the data the template is handed, so
        a mis-declared kind is caught here even while the HTML stays innocent.
        """
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.hostingSentinel,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        # An open edge as well, so the non-vacuity check below is about the
        # filter and not about there being nothing to filter. With only the
        # restricted edge, an empty prefetch is the CORRECT answer and the
        # guard would fail on a passing system.
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))

        prefetched = [
            dependency
            for row in resp.context["rows"]
            for dependency in row["resource"].openDependencies
        ]
        self.assertNotEqual(prefetched, [], "Nothing was prefetched - this test would pass vacuously.")
        self.assertNotIn(
            ResourceDependency.Kind.RUNS_ON, [dependency.kind for dependency in prefetched],
            "A restricted edge was loaded into the open directory's context.",
        )

    # --- 7. REACHED_THROUGH: open, both directions, and does not impersonate SIGN_IN ---

    def test_reached_through_reaches_plain_member_on_index_and_detail(self):
        """The kind exists so a resource nobody can be granted directly can say
        so. It is open for the same reason SIGN_IN is: it changes what the
        reader does next."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.REACHED_THROUGH,
        )
        self.loginAs(self.member)

        indexResp = self.client.get(reverse("chapter-tools"))
        self.assertContains(indexResp, EDGE_PHRASE.format(name="Example SSO"))
        self.assertContains(indexResp, "Most people reach this through Example SSO")

        detailResp = self.client.get(self.wiki.getUrl())
        self.assertContains(detailResp, EDGE_PHRASE.format(name="Example SSO"))
        self.assertContains(detailResp, "Most people reach this through Example SSO")

    def test_reached_through_does_not_claim_direct_access_is_impossible(self):
        """The headline must not assert there is no direct route, because for
        some resources there is one for a different audience - a Zoom meeting
        host gets the shared login, an Action Network organiser gets their own
        account - and the card prints that route in the very next paragraph.

        Pinned as a test rather than left to the wording, because the absolute
        phrasing was correct for the first resource this kind was used on and
        stayed on the page unchallenged when the second and third arrived."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.REACHED_THROUGH,
        )
        self.loginAs(self.member)
        for resp in (self.client.get(reverse("chapter-tools")), self.client.get(self.wiki.getUrl())):
            self.assertNotContains(resp, "You do not get access to this directly")

    def test_reached_through_does_not_render_as_a_sign_in_prerequisite(self):
        """The two must not collapse into one another. "You need X first" says
        get X as well; "most people reach this through X" says X is the usual
        route instead of, not as well as. Rendering a reached-through edge with the
        sign-in wording would send a member to request a login nobody issues -
        the exact defect this kind was added to fix."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.REACHED_THROUGH,
        )
        self.loginAs(self.member)
        for resp in (self.client.get(reverse("chapter-tools")), self.client.get(self.wiki.getUrl())):
            self.assertNotContains(resp, "signs you in through your")

    def test_reverse_reached_through_renders_on_the_front_door_for_plain_member(self):
        """The reverse is the more useful half: on Echo's page, "what people
        reach through this" is what tells a reader it is the front door for
        more than one system."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.REACHED_THROUGH,
        )
        self.loginAs(self.member)
        resp = self.client.get(self.identityProvider.getUrl())
        self.assertContains(resp, "What people reach through this")
        self.assertContains(resp, self.wiki.getUrl())

    # --- 6. Self-dependency is rejected ---

    def test_self_dependency_rejected_by_clean(self):
        dependency = ResourceDependency(
            resource=self.wiki, dependsOn=self.wiki, kind=ResourceDependency.Kind.SIGN_IN,
        )
        with self.assertRaises(ValidationError):
            dependency.clean()

    def test_self_dependency_rejected_by_db_constraint(self):
        # clean() is a readable-message duplicate, not the only protection - a
        # raw .create() that skips clean() must still fail on the DB
        # constraint. Wrapped in transaction.atomic(): an uncaught
        # IntegrityError inside a TestCase poisons the surrounding test
        # transaction and every later query in this test would error out.
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                ResourceDependency.objects.create(
                    resource=self.wiki, dependsOn=self.wiki, kind=ResourceDependency.Kind.SIGN_IN,
                )

    # --- 7. Duplicate (resource, dependsOn, kind) is rejected; a different kind is allowed ---

    def test_duplicate_edge_rejected(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                ResourceDependency.objects.create(
                    resource=self.wiki, dependsOn=self.identityProvider,
                    kind=ResourceDependency.Kind.SIGN_IN,
                )

    def test_same_pair_with_a_different_kind_is_allowed(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.assertEqual(
            ResourceDependency.objects.filter(resource=self.wiki, dependsOn=self.identityProvider).count(),
            2,
        )

    # --- 8. Index query count does not grow with resource count ---
    # assertNumQueries cannot express this - it takes a fixed expected count,
    # so there is no way to run it twice and compare. This catches a removed
    # or broken prefetch (a query per row); it does NOT catch a wrong filter -
    # case 2 above is the guard for that. The two are complements, not
    # substitutes.

    def test_index_query_count_does_not_scale_with_resource_count(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)

        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("chapter-tools"))

        for i in range(4):
            extra = _makeResource(name=f"Example Extra Resource {i}")
            ResourceDependency.objects.create(
                resource=extra, dependsOn=self.identityProvider,
                kind=ResourceDependency.Kind.SIGN_IN,
            )

        with CaptureQueriesContext(connection) as large:
            self.client.get(reverse("chapter-tools"))

        self.assertEqual(len(small.captured_queries), len(large.captured_queries))

    def test_index_query_count_does_not_scale_with_resource_count_for_organizer(self):
        """The plain-member variant above never exercises the conditional
        "holders" prefetch added for viewResourceHolders - that path needs its
        own no-N+1 contract, since it is the one that actually runs the extra
        query and could regress into one query per resource."""
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        ResourceHolder.objects.create(resource=self.wiki, personName="Example Organizer-Visible Holder")
        self.loginAs(self.organizer)

        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("chapter-tools"))

        for i in range(4):
            extra = _makeResource(name=f"Example Extra Organizer Resource {i}")
            ResourceDependency.objects.create(
                resource=extra, dependsOn=self.identityProvider,
                kind=ResourceDependency.Kind.SIGN_IN,
            )
            ResourceHolder.objects.create(resource=extra, personName=f"Example Extra Holder {i}")

        with CaptureQueriesContext(connection) as large:
            self.client.get(reverse("chapter-tools"))

        self.assertEqual(len(small.captured_queries), len(large.captured_queries))


class ResourceLinkTests(LoginClientMixin, TestCase):
    """siteUrl / accessRequestUrl. The directory's whole job is answering "how
    do I get into this", and an answer a member cannot click is not an answer -
    howToGetAccess renders as plain text, so a URL written into that prose is
    dead characters the reader has to retype."""

    def setUp(self):
        self.member = UserFactory.make("member")

    def test_site_host_is_the_bare_hostname(self):
        resource = _makeResource(siteUrl="https://example-wiki.invalid/home?x=1")
        self.assertEqual(resource.getSiteHost(), "example-wiki.invalid")

    def test_site_host_is_empty_when_unset(self):
        self.assertEqual(_makeResource().getSiteHost(), "")

    def test_index_and_detail_render_both_links_when_set(self):
        resource = _makeResource(
            siteUrl="https://example-wiki.invalid/",
            accessRequestUrl="https://example-form.invalid/request",
        )
        self.loginAs(self.member)
        for resp in (self.client.get(reverse("chapter-tools")), self.client.get(resource.getUrl())):
            self.assertContains(resp, "https://example-form.invalid/request")
            self.assertContains(resp, "https://example-wiki.invalid/")
            # The hostname is the visible link text, not a generic "Open site" -
            # the address is the part a member needs to remember tomorrow.
            self.assertContains(resp, "example-wiki.invalid")

    def test_no_link_markup_when_urls_are_unset(self):
        """An empty URLField must not render an <a href=""> that goes nowhere."""
        resource = _makeResource()
        self.loginAs(self.member)
        resp = self.client.get(resource.getUrl())
        self.assertNotContains(resp, 'href="" target="_blank"')
        self.assertNotContains(resp, "Request access")


class SeedChapterToolsCommandTests(TestCase):
    SEED = {
        "resources": [
            {
                "name": "Example Bank",
                "blurb": "Where the chapter's money lives.",
                "category": "FINANCE",
                "accessModel": "SHARED_VAULT",
                "payer": "CHAPTER",
                "annualCost": "0",
                "howToGetAccess": "Ask the treasurer.",
                "stewardName": "Example Treasurer",
                "requestable": False,
                "lastReviewed": "2026-01-01",
                "reviewedBy": "Example Treasurer",
                "delegationTier": "RED",
                "revocationNote": "Rotate the shared login and re-share via vault.",
                "continuityNote": "Backup: Example Co-Treasurer.",
                "credentials": [
                    {"label": "Shared login", "kind": "VAULT_SHARED_LOGIN", "vaultCollection": "finance", "status": "LIVE"},
                ],
                "holders": [
                    {"personName": "Example Treasurer", "how": "VAULT_COLLECTION", "confirmed": True},
                ],
                "questions": [
                    {"question": "Who else has the recovery codes?"},
                ],
            },
        ],
        "questions": [
            {"question": "Is there a chapter-wide break-glass doc?"},
        ],
    }

    def _writeSeedFile(self, data) -> str:
        tempDir = tempfile.mkdtemp()
        path = Path(tempDir) / "chapter-tools.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_loads_resources_holders_credentials_and_questions(self):
        path = self._writeSeedFile(self.SEED)
        call_command("seed_chapter_tools", file=path)

        resource = ChapterResource.objects.get(name="Example Bank")
        self.assertEqual(resource.category, ChapterResource.Category.FINANCE)
        self.assertEqual(resource.delegationTier, ChapterResource.DelegationTier.RED)
        self.assertEqual(resource.holders.count(), 1)
        self.assertEqual(resource.credentials.count(), 1)
        self.assertEqual(resource.questions.count(), 1)
        self.assertEqual(ResourceQuestion.objects.filter(resource__isnull=True).count(), 1)

    def test_second_run_is_idempotent(self):
        path = self._writeSeedFile(self.SEED)
        call_command("seed_chapter_tools", file=path)
        call_command("seed_chapter_tools", file=path)

        self.assertEqual(ChapterResource.objects.count(), 1)
        resource = ChapterResource.objects.get(name="Example Bank")
        self.assertEqual(resource.holders.count(), 1)
        self.assertEqual(resource.credentials.count(), 1)
        self.assertEqual(resource.questions.count(), 1)
        self.assertEqual(ResourceQuestion.objects.filter(resource__isnull=True).count(), 1)

    def test_second_run_preserves_question_progress_made_in_app(self):
        path = self._writeSeedFile(self.SEED)
        call_command("seed_chapter_tools", file=path)

        question = ResourceQuestion.objects.get(resource__name="Example Bank")
        question.assignedTo = "Example Assignee"
        question.resolvedAt = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
        question.resolution = "Confirmed via 1:1."
        question.save()

        call_command("seed_chapter_tools", file=path)

        question.refresh_from_db()
        self.assertEqual(question.assignedTo, "Example Assignee")
        self.assertEqual(question.resolution, "Confirmed via 1:1.")
        self.assertIsNotNone(question.resolvedAt)

    # --- dependencies: the real inventory must be able to express the same
    # edges the demo seed can, or the registry is only complete on the demo box.

    def _seedWithDependency(self, **edgeOverrides):
        """Deliberately declares the edge on the FIRST resource, pointing at one
        defined LATER in the file - the ordering a one-pass loader would fail on."""
        edge = {"dependsOn": "Example SSO", "kind": "SIGN_IN", "note": "OAuth"}
        edge.update(edgeOverrides)
        return {
            "resources": [
                {"name": "Example Wiki", "category": "COMMUNICATION", "dependencies": [edge]},
                {"name": "Example SSO", "category": "COMMUNICATION"},
            ],
        }

    def test_loads_dependencies_declared_before_their_target_is_defined(self):
        call_command("seed_chapter_tools", file=self._writeSeedFile(self._seedWithDependency()))

        dependency = ResourceDependency.objects.get()
        self.assertEqual(dependency.resource.name, "Example Wiki")
        self.assertEqual(dependency.dependsOn.name, "Example SSO")
        self.assertEqual(dependency.kind, ResourceDependency.Kind.SIGN_IN)
        self.assertEqual(dependency.note, "OAuth")

    def test_reseeding_does_not_duplicate_dependencies(self):
        path = self._writeSeedFile(self._seedWithDependency())
        call_command("seed_chapter_tools", file=path)
        call_command("seed_chapter_tools", file=path)
        self.assertEqual(ResourceDependency.objects.count(), 1)

    def test_dependency_on_an_unknown_resource_is_a_hard_error(self):
        """A dropped edge would leave the registry looking complete when it is
        not - the exact failure this registry exists to fix."""
        seed = self._seedWithDependency(dependsOn="Example Typo")
        with self.assertRaises(CommandError):
            call_command("seed_chapter_tools", file=self._writeSeedFile(seed))

    def test_unknown_dependency_kind_is_a_hard_error(self):
        seed = self._seedWithDependency(kind="DEPENDS_SOMEHOW")
        with self.assertRaises(CommandError):
            call_command("seed_chapter_tools", file=self._writeSeedFile(seed))

    # --- controllability: preview before writing, and write only one section.
    # The loader overwrites hand-entered registry fields by design, so "what is
    # this about to change" must be answerable without finding out afterwards.

    def _run(self, seed, **options):
        out = io.StringIO()
        call_command("seed_chapter_tools", file=self._writeSeedFile(seed), stdout=out, **options)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self._run(self.SEED, dry_run=True)
        self.assertIn("DRY RUN", output)
        self.assertEqual(ChapterResource.objects.count(), 0)
        self.assertEqual(ResourceCredential.objects.count(), 0)
        self.assertEqual(ResourceQuestion.objects.count(), 0)

    def test_dry_run_previews_the_same_changes_a_real_run_makes(self):
        """The preview must be the real code path rolled back, not a second
        implementation that can drift from it."""
        preview = self._run(self.SEED, dry_run=True)
        real = self._run(self.SEED)
        self.assertEqual(
            [line for line in preview.splitlines() if line.startswith("  ")],
            [line for line in real.splitlines() if line.startswith("  ")],
        )

    def test_report_names_the_field_it_overwrites(self):
        self._run(self.SEED)
        edited = dict(self.SEED)
        edited["resources"] = [dict(self.SEED["resources"][0], blurb="Edited in admin.")]

        output = self._run(edited, dry_run=True)
        self.assertIn("overwrote blurb", output)
        # Only the changed field is named - a report that lists every field on
        # every run is noise, and noise is how people learn to skip the preview.
        self.assertNotIn("category", output)

    def test_unchanged_input_reports_no_changes(self):
        self._run(self.SEED)
        self.assertIn("No changes", self._run(self.SEED))

    def test_only_limits_which_sections_are_written(self):
        self._run(self.SEED)
        ResourceCredential.objects.all().delete()
        ChapterResource.objects.update(blurb="Edited in admin.")

        self._run(self.SEED, only=["credentials"])

        self.assertEqual(ResourceCredential.objects.count(), 1)  # restored
        self.assertEqual(  # left alone
            ChapterResource.objects.get(name="Example Bank").blurb, "Edited in admin.",
        )

    def test_only_child_section_does_not_conjure_a_missing_resource(self):
        output = self._run(self.SEED, only=["credentials"])
        self.assertEqual(ChapterResource.objects.count(), 0)
        self.assertIn("skipped", output)

    def test_unresolvable_steward_username_is_an_error_not_a_silent_unassign(self):
        seed = {"resources": [{
            "name": "Example Bank", "category": "FINANCE", "stewardUsername": "nosuchperson",
        }]}
        with self.assertRaises(CommandError):
            self._run(seed)

    def test_loads_site_and_request_urls(self):
        seed = {"resources": [{
            "name": "Example Wiki", "category": "COMMUNICATION",
            "siteUrl": "https://example-wiki.invalid/",
            "accessRequestUrl": "https://example-form.invalid/request",
        }]}
        call_command("seed_chapter_tools", file=self._writeSeedFile(seed))

        resource = ChapterResource.objects.get(name="Example Wiki")
        self.assertEqual(resource.siteUrl, "https://example-wiki.invalid/")
        self.assertEqual(resource.accessRequestUrl, "https://example-form.invalid/request")
        self.assertEqual(resource.getSiteHost(), "example-wiki.invalid")


# --- CRUD (manageChapterTools) ----------------------------------------------
#
# The load-bearing property under test is NOT "can the form save" - it is that
# manageChapterTools does not become a read permission for the restricted layer.
# Every absence assertion below is paired with a presence assertion for an
# editor who DOES hold viewChapterToolAudit, using the same literal, because an
# unpaired assertNotContains passes forever the moment the markup changes. That
# has already happened once in this file (see the EDGE_PHRASE note at the top).

# Restricted-layer sentinels. Distinctive strings, so a match cannot come from
# some unrelated part of the page.
TIER_FIELD_SENTINEL = "delegationTier"
CONTINUITY_SENTINEL = "Break glass via ExampleBackupHolder"
CRED_SENTINEL = "ExampleCrudCredentialSentinel"
RUNS_ON_SENTINEL = "ExampleHostingSentinel"


# Every field's valid-and-blank default, keyed by ChapterResourceForm.Keys.
# _sectionPayload/_createPayload walk a row spec against this table rather
# than hand-listing one payload per facet, so a field added to a facet or to
# CREATE_ROWS is required here too instead of silently missing from every
# payload that happens to validate anyway.
_K = ChapterResourceForm.Keys
_FIELD_DEFAULTS = {
    _K.NAME: "Example Wiki",
    _K.BLURB: "",
    _K.CATEGORY: str(ChapterResource.Category.COMMUNICATION),
    _K.ACCESS_MODEL: str(ChapterResource.AccessModel.SHARED_VAULT),
    _K.HOW_TO_GET_ACCESS: "",
    _K.ACCESS_REQUEST_URL: "",
    _K.SITE_URL: "",
    _K.STEWARD: "",
    _K.STEWARD_NAME: "",
    _K.PAYER: str(ChapterResource.Payer.CHAPTER),
    _K.ANNUAL_COST: "",
    _K.COST_NOTE: "",
    _K.REQUESTABLE: "",
    _K.LAST_REVIEWED: "",
    _K.REVIEWED_BY: "",
    _K.DELEGATION_TIER: str(ChapterResource.DelegationTier.UNCLASSIFIED),
    _K.REVOCATION_NOTE: "",
    _K.CONTINUITY_NOTE: "",
}


def _sectionPayload(facetSlug, **overrides):
    """A complete POST body for one facet's section-edit page
    (chapter-tool-facet-edit), built by walking FACETS."""
    facet = chapterForms._FACET_BY_SLUG[facetSlug]
    payload = {key: _FIELD_DEFAULTS[key] for key in facet.keys}
    payload.update(overrides)
    return payload


def _createPayload(**overrides):
    """A complete POST body for the create page (chapterForms.CREATE_ROWS)."""
    payload = {
        key: _FIELD_DEFAULTS[key]
        for row in chapterForms.CREATE_ROWS for key in row
    }
    payload.update(overrides)
    return payload


class ChapterToolsFormsUnderTest:
    """Builds a ResourceDependencyForm whose dependsOn queryset has NOT had the
    resource excluded, which is the only way to reach the self-reference check in
    clean() - the real form excludes it in __init__, so that branch is otherwise
    unreachable. This exists to prove the backstop works, not to be used by any
    production code path."""

    @staticmethod
    def dependencyForm(data, resource):
        form = ResourceDependencyForm(data, resource=resource, allowRestrictedKinds=True)
        form.fields[ResourceDependencyForm.Keys.DEPENDS_ON].queryset = (
            ChapterResource.objects.all()
        )
        return form


class ChapterToolsCrudGateTests(LoginClientMixin, TestCase):
    """Who may reach the write surface at all."""

    def setUp(self):
        self.resource = _makeResource()
        self.member = UserFactory.make("member")
        self.editor = UserFactory.make("editor", perms=("manageChapterTools",))

    def test_a_plain_member_cannot_reach_any_write_page(self):
        self.loginAs(self.member)
        for url in (
            reverse("chapter-tool-new"),
            reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}),
            reverse("chapter-tool-delete", kwargs={"pk": self.resource.pk}),
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "holders"}),
            *(
                reverse("chapter-tool-facet-edit",
                        kwargs={"pk": self.resource.pk, "facetSlug": facet.slug})
                for facet in chapterForms.FACETS
            ),
        ):
            response = self.client.get(url)
            # permission_required without raise_exception redirects to login -
            # the convention the other admin-tier access pages follow.
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("login", response["Location"], url)

    def test_an_editor_can_reach_the_workbench(self):
        self.loginAs(self.editor)
        url = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The bare URL opens on Who has it (holders) now - there is no Details
        # tab any more, so the tab strip is what proves the workbench itself
        # rendered, and the roster heading is the marker for the default tab.
        self.assertContains(response, 'class="tab-nav"')
        self.assertContains(response, "Who has it now")
        # Paired, so this keeps meaning something if the sections were dropped
        # from the page rather than moved into tabs.
        self.assertContains(self.client.get(f"{url}?tab=dependencies"), "What this needs")

    def test_the_edit_link_is_hidden_from_a_member_and_shown_to_an_editor(self):
        detailUrl = self.resource.getUrl()
        editUrl = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        self.loginAs(self.member)
        self.assertNotContains(self.client.get(detailUrl), editUrl)
        self.assertNotContains(self.client.get(reverse("chapter-tools")), editUrl)
        # Paired presence - otherwise the two assertions above would keep
        # passing if the link were removed from both pages entirely.
        self.loginAs(self.editor)
        self.assertContains(self.client.get(detailUrl), editUrl)
        self.assertContains(self.client.get(reverse("chapter-tools")), editUrl)

    def test_an_unknown_child_kind_is_a_404(self):
        self.loginAs(self.editor)
        response = self.client.get(reverse(
            "chapter-tool-child-new", kwargs={"pk": self.resource.pk, "childKind": "grants"},
        ))
        self.assertEqual(response.status_code, 404)


class ChapterToolsCrudRestrictedLayerTests(LoginClientMixin, TestCase):
    """manageChapterTools must not become a read permission for the restricted
    layer. This is the class that matters."""

    def setUp(self):
        self.steward = UserFactory.make("steward")
        self.resource = _makeResource(
            delegationTier=ChapterResource.DelegationTier.RED,
            revocationNote="Rotate the shared vault login.",
            continuityNote=CONTINUITY_SENTINEL,
            steward=self.steward,
        )
        self.hosting = _makeResource(name=RUNS_ON_SENTINEL)
        ResourceDependency.objects.create(
            resource=self.resource, dependsOn=self.hosting,
            kind=ResourceDependency.Kind.RUNS_ON,
        )
        ResourceCredential.objects.create(
            resource=self.resource, label=CRED_SENTINEL,
            kind=ResourceCredential.Kind.VAULT_SHARED_LOGIN,
        )
        self.editor = UserFactory.make("editor", perms=("manageChapterTools",))
        self.auditEditor = UserFactory.make(
            "auditEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.editUrl = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        self.committeeUrl = reverse(
            "chapter-tool-facet-edit", kwargs={"pk": self.resource.pk, "facetSlug": "committee-only"},
        )
        self.costsUrl = reverse(
            "chapter-tool-facet-edit", kwargs={"pk": self.resource.pk, "facetSlug": "what-it-costs"},
        )

    def test_restricted_fields_are_absent_from_a_non_audit_editors_form(self):
        # 404, not the field simply missing from a 200 - the committee-only
        # section does not exist at all for this editor (_resolveChild's rule).
        self.loginAs(self.editor)
        sectionResponse = self.client.get(self.committeeUrl)
        self.assertEqual(sectionResponse.status_code, 404)
        # The stored VALUE must not leak from either surface this editor CAN
        # reach - the 404 page itself, and the hub, whose committee-only card
        # (the only place this value would render) is absent for them too.
        self.assertNotContains(sectionResponse, CONTINUITY_SENTINEL, status_code=404)
        self.assertNotContains(self.client.get(self.resource.getUrl()), CONTINUITY_SENTINEL)

    def test_restricted_fields_are_present_for_an_audit_editor(self):
        # The pairing that keeps the test above honest.
        self.loginAs(self.auditEditor)
        response = self.client.get(self.committeeUrl)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, TIER_FIELD_SENTINEL)
        # The stored VALUE, not just the field name - a bound form renders
        # current values, which is the actual leak.
        self.assertContains(response, CONTINUITY_SENTINEL)

    def test_credentials_are_hidden_from_a_non_audit_editor_and_shown_to_an_audit_one(self):
        # Asked for by tab, because that is the only place the section renders
        # now. The non-audit half is the stronger assertion of the two: asking
        # for ?tab=credentials WITHOUT the permission does not render them, it
        # falls back to details - see _activeTab.
        credentialsTab = f"{self.editUrl}?tab=credentials"
        self.loginAs(self.editor)
        self.assertNotContains(self.client.get(credentialsTab), CRED_SENTINEL)
        self.loginAs(self.auditEditor)
        self.assertContains(self.client.get(credentialsTab), CRED_SENTINEL)

    def test_a_non_audit_editor_cannot_reach_the_credential_routes(self):
        credential = self.resource.credentials.first()
        self.loginAs(self.editor)
        for url in (
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "credentials"}),
            reverse("chapter-tool-child-edit",
                    kwargs={"pk": self.resource.pk, "childKind": "credentials",
                            "childId": credential.pk}),
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        # 404 and not 403 on purpose: a 403 confirms the credentials surface
        # exists for this resource, which is itself part of what is withheld.
        self.loginAs(self.auditEditor)
        for url in (
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "credentials"}),
            reverse("chapter-tool-child-edit",
                    kwargs={"pk": self.resource.pk, "childKind": "credentials",
                            "childId": credential.pk}),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_runs_on_edge_is_hidden_from_a_non_audit_editor(self):
        dependenciesTab = f"{self.editUrl}?tab=dependencies"
        self.loginAs(self.editor)
        self.assertNotContains(self.client.get(dependenciesTab), RUNS_ON_SENTINEL)
        self.loginAs(self.auditEditor)
        self.assertContains(self.client.get(dependenciesTab), RUNS_ON_SENTINEL)

    def test_a_non_audit_editor_cannot_post_a_runs_on_edge(self):
        other = _makeResource(name="Example Bank")
        self.loginAs(self.editor)
        response = self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "dependencies"}),
            {"dependsOn": str(other.pk), "kind": str(ResourceDependency.Kind.RUNS_ON), "note": ""},
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with an error
        self.assertFalse(ResourceDependency.objects.filter(
            resource=self.resource, dependsOn=other,
        ).exists())

    def test_a_non_audit_editor_saving_does_not_wipe_the_restricted_fields(self):
        """The whole reason _applyCleanedData copies only the keys present -
        now the mechanism behind "only this section is saved" for EVERY
        section, not just the audit/non-audit split the old workbench had."""
        self.loginAs(self.editor)
        response = self.client.post(self.costsUrl, _sectionPayload("what-it-costs"))
        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.delegationTier, ChapterResource.DelegationTier.RED)
        self.assertEqual(self.resource.continuityNote, CONTINUITY_SENTINEL)
        self.assertEqual(self.resource.revocationNote, "Rotate the shared vault login.")

    def test_a_non_audit_editor_saving_a_different_section_does_not_wipe_the_steward(self):
        """Steward is not restricted - it lives on the open who's-answerable
        facet - but it is still a field this editor's what-it-costs form does
        not own, so a save there must leave it exactly as untouched as the
        audit-only fields above."""
        self.loginAs(self.editor)
        response = self.client.post(self.costsUrl, _sectionPayload("what-it-costs"))
        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.steward_id, self.steward.pk)


class ChapterToolsCrudWriteTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)

    def test_create_lands_on_the_hub_with_every_other_field_defaulted(self):
        response = self.client.post(
            reverse("chapter-tool-new"), _createPayload(name="Example Vault"),
        )
        resource = ChapterResource.objects.get(name="Example Vault")
        # The hub, not the workbench - the create form has no child rows to
        # fill in immediately any more, just the four fields it just posted.
        self.assertRedirects(response, resource.getUrl())
        self.assertEqual(resource.accessModel, ChapterResource.AccessModel.UNCONFIRMED)
        self.assertEqual(resource.payer, ChapterResource.Payer.UNCONFIRMED)
        self.assertEqual(resource.delegationTier, ChapterResource.DelegationTier.UNCLASSIFIED)
        self.assertFalse(resource.requestable)
        self.assertEqual(resource.howToGetAccess, "")
        self.assertEqual(resource.costNote, "")
        self.assertIsNone(resource.annualCost)
        self.assertIsNone(resource.steward)
        self.assertIsNone(resource.lastReviewed)

    def test_a_duplicate_name_is_a_field_error_not_a_500(self):
        _makeResource(name="Example Vault")
        response = self.client.post(
            reverse("chapter-tool-new"), _createPayload(name="example vault"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(ChapterResource.objects.filter(name__iexact="example vault").count(), 1)

    def test_a_duplicate_name_is_also_a_field_error_on_the_what_it_is_section(self):
        """clean_name() has no change per the plan, but it now runs against
        base_fields sliced to one facet - this is the same guard exercised
        through the edit path instead of create."""
        _makeResource(name="Example Vault")
        other = _makeResource(name="Example Chat")
        response = self.client.post(
            reverse("chapter-tool-facet-edit",
                    kwargs={"pk": other.pk, "facetSlug": "what-it-is"}),
            _sectionPayload("what-it-is", name="example vault"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        other.refresh_from_db()
        self.assertEqual(other.name, "Example Chat")

    def test_requestable_without_a_steward_is_rejected_by_the_form(self):
        """ChapterResource.clean()'s rule, re-implemented because the view
        never calls full_clean(). Posted on the committee-only section, which
        owns `requestable` but not `steward` - Rule A has to read the stored
        steward through _value() to catch this."""
        resource = _makeResource(name="Example Vault")
        response = self.client.post(
            reverse("chapter-tool-facet-edit",
                    kwargs={"pk": resource.pk, "facetSlug": "committee-only"}),
            _sectionPayload("committee-only", requestable="on"),
        )
        self.assertEqual(response.status_code, 200)
        # The committee-only-specific copy (forms.py's clean()) - pinned so a
        # future edit that reverts to the old steward-field error message
        # (unreachable here, since this form has no steward field to attach
        # it to) fails loudly instead of just changing quietly.
        self.assertContains(response, "would be a dead letter")
        resource.refresh_from_db()
        self.assertFalse(resource.requestable)

    def test_requestable_with_a_steward_and_a_green_tier_saves(self):
        steward = UserFactory.make("steward")
        resource = _makeResource(name="Example Vault", steward=steward)
        response = self.client.post(
            reverse("chapter-tool-facet-edit",
                    kwargs={"pk": resource.pk, "facetSlug": "committee-only"}),
            _sectionPayload(
                "committee-only", requestable="on",
                delegationTier=str(ChapterResource.DelegationTier.GREEN),
            ),
        )
        self.assertEqual(response.status_code, 302)
        resource.refresh_from_db()
        self.assertTrue(resource.requestable)

    def test_a_non_audit_editor_cannot_strip_the_steward_off_a_requestable_resource(self):
        """The rule from the other direction: `requestable` is not on this
        section's form, so they cannot turn it on - but they can clear the
        steward, which breaks the same invariant from the other side."""
        steward = UserFactory.make("steward")
        resource = _makeResource(name="Example Vault", requestable=True, steward=steward)
        openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))
        self.loginAs(openEditor)
        response = self.client.post(
            reverse("chapter-tool-facet-edit",
                    kwargs={"pk": resource.pk, "facetSlug": "whos-answerable"}),
            _sectionPayload("whos-answerable", steward="", stewardName=""),
        )
        self.assertEqual(response.status_code, 200)
        resource.refresh_from_db()
        self.assertEqual(resource.steward_id, steward.pk)

    def test_a_self_dependency_is_a_field_error(self):
        """ResourceDependency.clean() and its CheckConstraint both sit on paths
        this view does not take (no full_clean(), and SQLite/Postgres would raise
        an IntegrityError rather than a field error), so the form is the guard.

        Which of the form's TWO guards fires is pinned deliberately: the
        dependsOn queryset excludes self, so ModelChoiceField rejects the id
        before clean() runs. clean()'s identical check is the backstop for
        whoever removes that exclusion. Asserting the queryset one fires keeps
        the comments in ResourceDependencyForm honest."""
        resource = _makeResource()
        response = self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": resource.pk, "childKind": "dependencies"}),
            {"dependsOn": str(resource.pk), "kind": str(ResourceDependency.Kind.SIGN_IN), "note": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResourceDependency.objects.count(), 0)
        self.assertContains(response, "valid choice")

    def test_the_self_dependency_backstop_in_clean_also_works(self):
        """The other guard, exercised directly - a unit test is the only way to
        reach it while the queryset exclusion is in place."""
        resource = _makeResource()
        form = ChapterToolsFormsUnderTest.dependencyForm(
            {"dependsOn": str(resource.pk), "kind": str(ResourceDependency.Kind.SIGN_IN),
             "note": ""},
            resource=resource,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("cannot depend on itself", str(form.errors))

    def test_a_duplicate_dependency_is_a_field_error_not_an_integrity_error(self):
        resource = _makeResource()
        other = _makeResource(name="Example Chat")
        ResourceDependency.objects.create(
            resource=resource, dependsOn=other, kind=ResourceDependency.Kind.SIGN_IN,
        )
        response = self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": resource.pk, "childKind": "dependencies"}),
            {"dependsOn": str(other.pk), "kind": str(ResourceDependency.Kind.SIGN_IN), "note": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already recorded")
        self.assertEqual(ResourceDependency.objects.count(), 1)

    def test_a_holder_needs_a_name_or_an_account(self):
        resource = _makeResource()
        response = self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": resource.pk, "childKind": "holders"}),
            {"personName": "", "user": "", "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
             "accessLevel": "Ordinary member",
             "confirmed": "", "note": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResourceHolder.objects.count(), 0)

    def test_holder_add_edit_and_delete(self):
        resource = _makeResource()
        addUrl = reverse("chapter-tool-child-new",
                         kwargs={"pk": resource.pk, "childKind": "holders"})
        self.client.post(addUrl, {
            "personName": "Example Holder", "user": "",
            "how": str(ResourceHolder.How.VAULT_COLLECTION),
            "accessLevel": "Owner", "canGrantAccess": "on", "ownsAccount": "on",
            "confirmed": "on", "note": "",
        })
        holder = ResourceHolder.objects.get(resource=resource)
        self.assertTrue(holder.confirmed)
        self.assertEqual(holder.how, ResourceHolder.How.VAULT_COLLECTION)
        self.assertEqual(holder.accessLevel, "Owner")
        self.assertTrue(holder.isPrivileged())

        editUrl = reverse("chapter-tool-child-edit", kwargs={
            "pk": resource.pk, "childKind": "holders", "childId": holder.pk,
        })
        # An unchecked checkbox is simply absent from a real POST body.
        self.client.post(editUrl, {
            "personName": "Example Holder", "user": "",
            "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
            "accessLevel": "Ordinary member",
            "note": "Now unconfirmed.",
        })
        holder.refresh_from_db()
        self.assertFalse(holder.confirmed)
        self.assertEqual(holder.note, "Now unconfirmed.")
        self.assertFalse(holder.isPrivileged())

        self.client.post(reverse("chapter-tool-child-delete", kwargs={
            "pk": resource.pk, "childKind": "holders", "childId": holder.pk,
        }))
        self.assertEqual(ResourceHolder.objects.count(), 0)

    def test_a_credential_needs_a_kind(self):
        """`kind` has no model default on purpose, and TypedChoiceField coerces
        a blank to None rather than erroring, so clean_kind is what stops a None
        reaching a NOT NULL column."""
        resource = _makeResource()
        response = self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": resource.pk, "childKind": "credentials"}),
            {"label": CRED_SENTINEL, "kind": "", "vaultCollection": "",
             "status": str(ResourceCredential.Status.LIVE), "note": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ResourceCredential.objects.count(), 0)

    def test_a_child_of_another_resource_cannot_be_edited_by_guessing_its_id(self):
        mine = _makeResource(name="Example Wiki")
        theirs = _makeResource(name="Example Bank")
        holder = ResourceHolder.objects.create(resource=theirs, personName="Example Holder")
        response = self.client.get(reverse("chapter-tool-child-edit", kwargs={
            "pk": mine.pk, "childKind": "holders", "childId": holder.pk,
        }))
        self.assertEqual(response.status_code, 404)

    def test_child_delete_is_post_only(self):
        """A GET-deletable URL gets emptied by a link prefetcher or a crawler."""
        resource = _makeResource()
        holder = ResourceHolder.objects.create(resource=resource, personName="Example Holder")
        url = reverse("chapter-tool-child-delete", kwargs={
            "pk": resource.pk, "childKind": "holders", "childId": holder.pk,
        })
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ResourceHolder.objects.filter(pk=holder.pk).exists())


class ChapterToolsDeleteTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)
        self.resource = _makeResource(name="Example Wiki")
        self.other = _makeResource(name="Example Chat")
        ResourceHolder.objects.create(resource=self.resource, personName="Example Holder")
        ResourceCredential.objects.create(
            resource=self.resource, label=CRED_SENTINEL,
            kind=ResourceCredential.Kind.API_TOKEN,
        )
        ResourceDependency.objects.create(
            resource=self.resource, dependsOn=self.other,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        # The edge pointing AT the resource - the half of the cascade the
        # workbench shows as read-only, so the delete page has to name it.
        ResourceDependency.objects.create(
            resource=self.other, dependsOn=self.resource,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.question = ResourceQuestion.objects.create(
            resource=self.resource, question="Who owns this?",
        )
        self.url = reverse("chapter-tool-delete", kwargs={"pk": self.resource.pk})

    def test_the_confirm_page_counts_both_dependency_directions(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Dependency edges pointing out of it")
        self.assertContains(response, "Dependency edges pointing at it")
        self.assertContains(response, "Credential rows")

    def test_a_mismatched_typed_name_does_not_delete(self):
        response = self.client.post(self.url, {"confirmName": "Example Wik"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ChapterResource.objects.filter(pk=self.resource.pk).exists())

    def test_the_typed_name_check_is_enforced_server_side_not_just_by_the_button(self):
        # An empty POST is what a hand-built request or a JS-off browser sends.
        self.client.post(self.url, {})
        self.assertTrue(ChapterResource.objects.filter(pk=self.resource.pk).exists())

    def test_a_matching_name_deletes_and_cascades(self):
        response = self.client.post(self.url, {"confirmName": "Example Wiki"})
        self.assertRedirects(response, reverse("chapter-tools"))
        self.assertFalse(ChapterResource.objects.filter(pk=self.resource.pk).exists())
        self.assertEqual(ResourceHolder.objects.count(), 0)
        self.assertEqual(ResourceCredential.objects.count(), 0)
        # Both directions go, including the edge owned by the other resource.
        self.assertEqual(ResourceDependency.objects.count(), 0)
        # ...but the question survives as chapter-wide (SET_NULL), which is what
        # the confirm page promises.
        self.question.refresh_from_db()
        self.assertIsNone(self.question.resource_id)

    def test_a_non_audit_editor_is_not_told_the_credential_count(self):
        openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))
        self.loginAs(openEditor)
        self.assertNotContains(self.client.get(self.url), "Credential rows")


class ChapterToolsTemplateCommentTests(LoginClientMixin, TestCase):
    """Maintainer notes must not render as body text.

    Django's {# #} comment is SINGLE-LINE only, so a multi-line one renders to
    the page verbatim. That shipped once already, on Manage Member Access, and
    these pages carry long explanatory comments - so the guard is worth having
    on each one."""

    LEAKS = ("{#", "#}", "{% comment", "endcomment", "CHILD_SPECS",
             "_chapter-tools.css", "manageChapterTools", "RESTRICTED_KEYS")

    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)
        self.resource = _makeResource()

    def _assertClean(self, response, presenceMarker):
        content = response.content.decode()
        for leak in self.LEAKS:
            self.assertNotIn(leak, content, f"template comment leaked: {leak}")
        # Paired presence: without this, the loop above would pass for a page
        # that failed to render its content at all.
        self.assertIn(presenceMarker, content)

    def test_the_workbench_renders_no_comment_text(self):
        # Every tab, not just the default one. The sections are behind ?tab= now,
        # so checking the bare URL would leave panels' worth of comments -
        # and this template carries the longest comments in the feature - never
        # looked at. The marker is per tab for the same reason _assertClean pairs
        # one at all: an empty panel would otherwise pass the leak loop. No
        # "details" tab any more - the resource's own fields moved to the hub.
        url = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        for tab, marker in (
            ("holders", "Who has it now"),
            ("dependencies", "What this needs"),
            ("credentials", "Credentials"),
            ("delete", "Delete this chapter tool"),
        ):
            with self.subTest(tab=tab):
                self._assertClean(self.client.get(f"{url}?tab={tab}"), marker)

    def test_the_child_form_renders_no_comment_text(self):
        self._assertClean(
            self.client.get(reverse("chapter-tool-child-new",
                                    kwargs={"pk": self.resource.pk, "childKind": "holders"})),
            "Add a holder",
        )

    def test_the_delete_page_renders_no_comment_text(self):
        self._assertClean(
            self.client.get(reverse("chapter-tool-delete", kwargs={"pk": self.resource.pk})),
            "to confirm",
        )

    def test_the_questions_workbench_renders_no_comment_text(self):
        auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
        self.loginAs(auditor)
        self._assertClean(self.client.get(reverse("chapter-tools-questions")), "Add a question")

    def test_the_create_page_renders_no_comment_text(self):
        self._assertClean(
            self.client.get(reverse("chapter-tool-new")), "Create chapter tool",
        )

    def test_the_hub_renders_no_comment_text(self):
        self._assertClean(self.client.get(self.resource.getUrl()), "What it is")

    def test_the_section_page_renders_no_comment_text(self):
        self._assertClean(
            self.client.get(reverse("chapter-tool-facet-edit",
                                    kwargs={"pk": self.resource.pk, "facetSlug": "what-it-costs"})),
            "Save changes",
        )


class ChapterToolsReadabilityTests(LoginClientMixin, TestCase):
    """The layout half of the change: no page in this feature may go back to a
    wide data-table, because .page-card sets overflow-x:auto and a table with a
    prose column scrolls sideways instead of wrapping."""

    def setUp(self):
        self.auditor = UserFactory.make(
            "auditor", perms=("viewChapterToolAudit", "manageChapterTools"),
        )
        self.loginAs(self.auditor)
        self.resource = _makeResource()
        ResourceQuestion.objects.create(
            resource=self.resource,
            question="A question long enough that it would have taken the whole column width.",
        )

    def test_the_questions_workbench_is_a_record_list_not_a_table(self):
        response = self.client.get(reverse("chapter-tools-questions"))
        self.assertContains(response, "record-list")
        self.assertNotContains(response, "data-table")

    def test_the_workbench_child_sections_are_record_lists(self):
        url = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        for tab in ("holders", "dependencies", "credentials"):
            with self.subTest(tab=tab):
                response = self.client.get(f"{url}?tab={tab}")
                self.assertContains(response, "record-list")
                self.assertNotContains(response, "data-table")

    def test_the_record_and_detail_grid_styles_are_actually_compiled(self):
        """A class used in a template does nothing unless output.css was rebuilt,
        and a stale output.css fails silently - the class sits in the markup with
        no rule behind it. This asserts the rules are compiled AND that
        .detail-grid stacks at the same 48rem breakpoint .data-table collapses at.

        (The note that used to be here, that the Tailwind CLI could not run on
        this machine, is wrong: the standalone binary builds fine. Believing it
        was broken is what let this file go stale enough to lose four utilities
        the resolutions templates were using.)"""
        cssPath = Path(__file__).resolve().parent.parent / "static" / "css" / "output.css"
        css = cssPath.read_text(encoding="utf-8")
        for className in (".record-list", ".record-item", ".record-meta",
                          ".record-actions", ".record-action-field"):
            self.assertIn(className, css, f"{className} is used in a template but not compiled")
        self.assertIn("grid-template-columns: 1fr", css)
        self.assertIn("@media (width < 48rem)", css)


class ChapterToolsWorkbenchTabTests(LoginClientMixin, TestCase):
    """The workbench's sections are tabs in the URL rather than five stacked
    cards. The point of putting the tab in the URL is that the child views can
    redirect back into it - without that, adding a second holder still means
    landing on the details form and scrolling to find the button again, which was
    the whole complaint. So the redirect targets are the load-bearing assertions
    here, not the markup."""

    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)
        self.resource = _makeResource()
        self.editUrl = reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})
        # _makeResource creates no children. Both power flags are on so
        # getPowerSummary returns its LONGEST sentence - the one that was being
        # rendered into the uppercase label slot.
        self.holder = ResourceHolder.objects.create(
            resource=self.resource, personName="Example Workbench Holder",
            accessLevel="Owner", canGrantAccess=True, ownsAccount=True,
        )

    def test_the_default_tab_is_holders(self):
        """There is no details tab any more - the resource's own fields moved
        to the hub - so the bare URL opens on holders, the child kind the
        registry's biggest recorded gap lives on."""
        response = self.client.get(self.editUrl)
        self.assertContains(response, 'href="?tab=holders" aria-current="page"')
        self.assertContains(response, "Who has it now")

    def test_each_tab_renders_only_its_own_section(self):
        """Only one panel at a time - otherwise this is the old stacked page with
        a row of links bolted on top of it."""
        markers = {
            "holders": "Who has it now",
            "dependencies": "What this needs",
            # Not the bare word "Credentials" - that is also the tab-nav
            # label, which renders on every tab's page regardless of which is
            # active, and would make this cross-check pass vacuously.
            "credentials": "Add a credential",
            "delete": "Delete this chapter tool",
        }
        for tab, marker in markers.items():
            with self.subTest(tab=tab):
                content = self.client.get(f"{self.editUrl}?tab={tab}").content.decode()
                self.assertIn(marker, content)
                for otherTab, otherMarker in markers.items():
                    if otherTab != tab:
                        self.assertNotIn(otherMarker, content)

    def test_an_unknown_tab_falls_back_to_holders_rather_than_404ing(self):
        """A tab is a view preference. A stale bookmark should land on the page
        you asked for."""
        for bogus in ("", "nope", "holders/../credentials", "DETAILS"):
            with self.subTest(tab=bogus):
                response = self.client.get(f"{self.editUrl}?tab={bogus}")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'href="?tab=holders" aria-current="page"')

    def test_the_credentials_tab_is_absent_for_an_editor_without_the_audit_permission(self):
        plainEditor = UserFactory.make("plain", perms=("manageChapterTools",))
        self.loginAs(plainEditor)
        response = self.client.get(self.editUrl)
        self.assertNotContains(response, "?tab=credentials")
        # And forcing it lands on holders rather than an empty labelled tab that
        # implies there is something there to be shown.
        forced = self.client.get(f"{self.editUrl}?tab=credentials")
        self.assertContains(forced, 'href="?tab=holders" aria-current="page"')

    def test_the_delete_control_is_only_on_its_own_tab(self):
        """It used to sit at the foot of the old details tab, which was the form
        people opened most - so the one destructive control on the page was also
        the one hardest to avoid scrolling past."""
        deleteUrl = reverse("chapter-tool-delete", kwargs={"pk": self.resource.pk})
        for tab in ("holders", "dependencies", "credentials"):
            with self.subTest(tab=tab):
                self.assertNotContains(self.client.get(f"{self.editUrl}?tab={tab}"), deleteUrl)
        onTab = self.client.get(f"{self.editUrl}?tab=delete")
        self.assertContains(onTab, deleteUrl)
        self.assertContains(onTab, 'href="?tab=delete" aria-current="page"')

    def test_the_delete_tab_is_two_steps_from_a_deleted_row(self):
        """Opening the section deletes nothing, and the section itself only links
        onward to the typed-name confirmation. A tab labelled Delete would be a
        trap if either half of that were untrue."""
        response = self.client.get(f"{self.editUrl}?tab=delete")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ChapterResource.objects.filter(pk=self.resource.pk).exists())
        # A link, not a form that posts to the delete view from here.
        content = response.content.decode()
        deleteUrl = reverse("chapter-tool-delete", kwargs={"pk": self.resource.pk})
        self.assertIn(f'href="{deleteUrl}"', content)
        self.assertNotIn(f'action="{deleteUrl}"', content)

    def test_the_delete_tab_is_not_gated_on_the_audit_permission(self):
        """Unlike credentials. Deleting needs manageChapterTools, which is what
        this whole page needs, so hiding the tab from an editor who may delete
        would only hide the button from the person allowed to press it."""
        plainEditor = UserFactory.make("plain", perms=("manageChapterTools",))
        self.loginAs(plainEditor)
        self.assertContains(self.client.get(self.editUrl), "?tab=delete")
        self.assertContains(
            self.client.get(f"{self.editUrl}?tab=delete"), "Delete this chapter tool")

    def test_adding_a_child_returns_to_that_child_s_own_tab(self):
        """The reason the tab is in the URL at all."""
        other = _makeResource(name="Example Bank")
        cases = (
            ("holders", {"personName": "Example New Holder", "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
                         "accessLevel": "Member", "note": ""}),
            ("dependencies", {"dependsOn": str(other.pk),
                              "kind": str(ResourceDependency.Kind.SIGN_IN), "note": ""}),
        )
        for childKind, payload in cases:
            with self.subTest(childKind=childKind):
                response = self.client.post(
                    reverse("chapter-tool-child-new",
                            kwargs={"pk": self.resource.pk, "childKind": childKind}),
                    payload,
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"{self.editUrl}?tab={childKind}")

    def test_deleting_a_child_returns_to_that_child_s_own_tab(self):
        response = self.client.post(
            reverse("chapter-tool-child-delete",
                    kwargs={"pk": self.resource.pk, "childKind": "holders",
                            "childId": self.holder.pk}),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.editUrl}?tab=holders")

    def test_cancelling_out_of_a_child_form_returns_to_the_same_tab(self):
        """Cancel and save land in the same place. A Cancel that dropped you on
        the details form would reintroduce the scroll for anybody who changed
        their mind."""
        response = self.client.get(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "holders"}),
        )
        self.assertEqual(response.context["backUrl"], f"{self.editUrl}?tab=holders")
        self.assertContains(response, f'href="{self.editUrl}?tab=holders"')

    def test_the_tab_styles_are_actually_compiled(self):
        """Same rule as the record-list check above: a class in a template does
        nothing until output.css is rebuilt."""
        cssPath = Path(__file__).resolve().parent.parent / "static" / "css" / "output.css"
        css = cssPath.read_text(encoding="utf-8")
        for className in (".tab-nav", ".tab", ".tab-count"):
            self.assertIn(className, css, f"{className} is used in a template but not compiled")
        # The active marker is a variant, so its absence would leave every tab
        # looking identical while the markup still said which one was current.
        self.assertIn('.tab[aria-current="page"]', css)

    def test_the_power_summary_is_not_a_label_on_the_workbench_either(self):
        """The same defect that was fixed on the detail page: getPowerSummary
        returns a sentence, and .record-meta-label renders uppercase and
        letter-spaced, so a sentence in that slot shouts over the name it
        describes."""
        content = self.client.get(f"{self.editUrl}?tab=holders").content.decode()
        self.assertNotIn("What that means here", content)
        summary = self.holder.getPowerSummary()
        self.assertGreater(len(summary), 20, "guard: this only proves anything for a sentence")
        # escape() because the sentence contains an apostrophe and the template
        # renders it as &#x27; - comparing the raw string finds nothing and looks
        # exactly like the line being absent.
        self.assertIn(f'class="record-aside">{escape(summary)}', content)


class ChapterFacetTests(LoginClientMixin, TestCase):
    """The eighteen fields are partitioned into facets (forms.FACETS), each its
    own card on the hub and its own section-edit page - replacing the flat
    details form's fieldsets (forms.py's old FIELD_GROUPS).

    That is the whole risk being covered. A field the form declares and no
    facet names would not error - it would simply never appear on any page,
    save its default forever, and look like a field nobody had got round to
    using. So the partition is asserted directly, and every section page's
    render is asserted to contain every field its facet names."""

    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)
        self.resource = _makeResource()

    def _sectionUrl(self, facetSlug):
        return reverse("chapter-tool-facet-edit",
                        kwargs={"pk": self.resource.pk, "facetSlug": facetSlug})

    def test_every_field_is_in_exactly_one_facet(self):
        keys = [key for facet in chapterForms.FACETS for key in facet.keys]
        declared = set(ChapterResourceForm.base_fields)
        self.assertEqual(
            set(keys), declared,
            "FACETS and the form's own fields have diverged",
        )
        self.assertEqual(
            len(keys), len(set(keys)),
            "a field appears in two facets, so it would render on two pages and "
            "only one save would stick",
        )

    def test_every_create_field_is_also_editable_later(self):
        createKeys = {key for row in chapterForms.CREATE_ROWS for key in row}
        self.assertTrue(createKeys.issubset(set(ChapterResourceForm.base_fields)))
        facetKeys = {key for facet in chapterForms.FACETS for key in facet.keys}
        for key in createKeys:
            with self.subTest(field=key):
                self.assertIn(key, facetKeys, f"{key} is on the create form but in no facet")

    def test_the_last_facet_is_exactly_the_restricted_fields(self):
        """Why it matters that they line up exactly: the whole facet 404s for
        a non-audit editor. A restricted field parked in an open facet would
        make it reachable without viewChapterToolAudit; an open field parked in
        the restricted facet would become uneditable by anyone without it,
        quietly."""
        facet = chapterForms.FACETS[-1]
        self.assertEqual(facet.legend, "Committee only")
        self.assertTrue(facet.requiresAudit)
        self.assertEqual(set(facet.keys), set(ChapterResourceForm.RESTRICTED_KEYS))

    def test_an_ungrouped_field_raises_instead_of_vanishing(self):
        """The guard moved from groupedFields() (called at render time) to a
        module-level check called from ChapterResourceForm.__init__ (called at
        construction time) - it still names the field, and it still cannot be
        silently skipped."""
        savedFacets = chapterForms.FACETS
        try:
            chapterForms.FACETS = savedFacets[:-1]  # drop "Committee only"
            with self.assertRaises(ImproperlyConfigured) as caught:
                ChapterResourceForm(rows=chapterForms.CREATE_ROWS)
            for key in ChapterResourceForm.RESTRICTED_KEYS:
                self.assertIn(key, str(caught.exception))
        finally:
            chapterForms.FACETS = savedFacets

    def test_the_committee_section_is_a_404_for_a_non_audit_editor(self):
        """Replaces the old "restricted group disappears rather than rendering
        empty" test - there is no single page with an empty heading to avoid
        any more, because the restricted facet is its own URL and that URL
        does not exist for this editor at all."""
        openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))
        self.loginAs(openEditor)
        self.assertEqual(self.client.get(self._sectionUrl("committee-only")).status_code, 404)
        # The open facets all survive - the guard removes only the one
        # section, never the others.
        for facetSlug in ("what-it-is", "getting-in", "whos-answerable", "what-it-costs"):
            with self.subTest(facetSlug=facetSlug):
                self.assertEqual(self.client.get(self._sectionUrl(facetSlug)).status_code, 200)

    def test_every_facet_legend_is_a_section_h1_and_a_hub_card_title(self):
        hubContent = self.client.get(self.resource.getUrl()).content.decode()
        for facet in chapterForms.FACETS:
            with self.subTest(legend=facet.legend):
                sectionContent = self.client.get(self._sectionUrl(facet.slug)).content.decode()
                self.assertIn(f'<h1 class="page-title">{escape(facet.legend)}</h1>', sectionContent)
                self.assertIn(f'<span class="record-title">{escape(facet.legend)}</span>', hubContent)

    def test_every_field_still_reaches_its_section_page(self):
        """The regression the partition could cause. formRow.html stamps
        data-field on every row, so this counts rendered inputs without
        depending on any label wording."""
        for facet in chapterForms.FACETS:
            content = self.client.get(self._sectionUrl(facet.slug)).content.decode()
            for key in facet.keys:
                with self.subTest(facet=facet.slug, field=key):
                    self.assertIn(f'data-field="{key}"', content)

    def test_every_create_field_reaches_the_create_page(self):
        content = self.client.get(reverse("chapter-tool-new")).content.decode()
        createKeys = [key for row in chapterForms.CREATE_ROWS for key in row]
        for key in createKeys:
            with self.subTest(field=key):
                self.assertIn(f'data-field="{key}"', content)
        # Exactly four - the create form's whole point is that it is NOT the
        # eighteen-field workbench form any more.
        self.assertEqual(content.count("data-field=\""), len(createKeys))

    def test_paired_fields_share_a_row_and_single_fields_do_not(self):
        stewardFacet = chapterForms._FACET_BY_SLUG["whos-answerable"]
        form = ChapterResourceForm(rows=stewardFacet.rows)
        rows = {tuple(field.name for field in fields): isPair for isPair, fields in form.rows()}
        self.assertIs(rows[("steward", "stewardName")], True)

        costFacet = chapterForms._FACET_BY_SLUG["what-it-costs"]
        costForm = ChapterResourceForm(rows=costFacet.rows)
        costRows = {tuple(field.name for field in fields): isPair for isPair, fields in costForm.rows()}
        self.assertIs(costRows[("payer", "annualCost")], True)
        self.assertIs(costRows[("costNote",)], False)

        content = self.client.get(self._sectionUrl("whos-answerable")).content.decode()
        self.assertIn('<div class="form-pair">', content)
        # A single-field row gets a bare <div>, not class="" - an empty class
        # attribute would mean the isPair branch had stopped working and every
        # row was being wrapped the same way.
        singleFieldContent = self.client.get(self._sectionUrl("what-it-is")).content.decode()
        self.assertNotIn('<div class="">', singleFieldContent)

    def test_the_facet_styles_are_actually_compiled(self):
        """.form-group/.form-group-legend were retired with the flat details
        form - create.html and section.html each render exactly one facet, so
        there is no longer a group of groups to fieldset. Only .form-pair
        survives."""
        cssPath = Path(__file__).resolve().parent.parent / "static" / "css" / "output.css"
        css = cssPath.read_text(encoding="utf-8")
        self.assertIn(".form-pair", css, ".form-pair is used in a template but not compiled")
        self.assertNotIn(".form-group", css, ".form-group should have been retired with FIELD_GROUPS")

    def test_a_definition_marker_stays_on_its_label_s_line(self):
        """Two facets carry a definition on the LEFT field of a paired row only
        (whos-answerable's steward, committee-only's lastReviewed). While the
        closed marker had a 12rem flex basis it could not fit beside a label in
        a ~18rem column, so it wrapped and pushed that field's input one line
        below its partner's - the pair stopped lining up on every row that had
        a definition.

        Asserted against the compiled CSS because that is the failure this
        had: the markup was already right and only the layout was wrong."""
        cssPath = Path(__file__).resolve().parent.parent / "static" / "css" / "output.css"
        css = cssPath.read_text(encoding="utf-8")
        self.assertIn("flex: 0 1 auto", css)
        # And the open panel still gets the full row - the body is a child of the
        # <details>, so a box sized to "What's this?" would render the whole
        # delegation-tier ladder in a 7rem column.
        self.assertIn(".explain-row > .explain[open]", css)
        # Both paired rows that carry a definition still render the wrapper the
        # rules above target.
        for facetSlug, key in (("whos-answerable", "steward"), ("committee-only", "lastReviewed")):
            with self.subTest(field=key):
                content = self.client.get(self._sectionUrl(facetSlug)).content.decode()
                start = content.index(f'data-field="{key}"')
                self.assertIn('class="explain-row"', content[start:start + 400])


class FacetConfirmedDerivationTests(TestCase):
    """The confirmed-derivation table's edge cases (plan §3) - values only, no
    model field and no migration. Each case is chosen because a naive
    implementation gets it backwards."""

    def test_free_payer_with_no_annual_cost_is_confirmed(self):
        """annualCost is deliberately excluded from the rule - FREE and
        NATIONAL tools legitimately have none."""
        resource = _makeResource(payer=ChapterResource.Payer.FREE, annualCost=None)
        self.assertTrue(chapterForms._FACET_BY_SLUG["what-it-costs"].isConfirmed(resource))

    def test_unconfirmed_payer_is_not_confirmed(self):
        resource = _makeResource(payer=ChapterResource.Payer.UNCONFIRMED)
        self.assertFalse(chapterForms._FACET_BY_SLUG["what-it-costs"].isConfirmed(resource))

    def test_a_name_only_steward_alone_is_confirmed(self):
        """A name-only steward is a real answer (models.py's stewardName help
        text) - requiring the Echo-account half too would make this
        permanently unconfirmable for most stewards."""
        resource = _makeResource(stewardName="Jo")
        self.assertTrue(chapterForms._FACET_BY_SLUG["whos-answerable"].isConfirmed(resource))

    def test_a_blank_blurb_is_not_confirmed(self):
        """category always has a creator-chosen value and name is required at
        create, so blurb is the only honest signal left for what-it-is."""
        resource = _makeResource(blurb="")
        self.assertFalse(chapterForms._FACET_BY_SLUG["what-it-is"].isConfirmed(resource))


class ChapterFacetHubTests(LoginClientMixin, TestCase):
    """The hub (chapter_tool_detail): per-facet cards, the steward/audit
    build-only-if-permitted gates, the saved-alert query param, and the
    unconfirmed count. See forms.FACETS and
    chapterToolsViews._buildFacetCards."""

    def setUp(self):
        self.manager = UserFactory.make(
            "manager", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.member = UserFactory.make("member")

    def _sectionUrl(self, resource, facetSlug):
        return reverse("chapter-tool-facet-edit",
                        kwargs={"pk": resource.pk, "facetSlug": facetSlug})

    def test_fresh_resource_is_unconfirmed_with_a_prompt_and_a_manager_sees_a_button(self):
        resource = _makeResource(name="Example Fresh")
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        self.assertIn('<span class="badge badge-unresolved">Unconfirmed</span>', content)
        self.assertIn("Nobody is recorded as answerable for this tool.", content)
        self.assertIn(
            f'href="{self._sectionUrl(resource, "whos-answerable")}">Confirm the steward</a>',
            content,
        )

    def test_a_plain_member_sees_the_prompt_but_no_confirm_button(self):
        resource = _makeResource(
            name="Example Fresh Member View", payer=ChapterResource.Payer.UNCONFIRMED,
        )
        self.loginAs(self.member)
        content = self.client.get(resource.getUrl()).content.decode()
        self.assertIn("Who pays for this, and what does it cost a year?", content)
        self.assertNotIn("Confirm the cost", content)

    def test_a_fully_filled_resource_is_confirmed_with_an_edit_button(self):
        steward = UserFactory.make("steward")
        resource = _makeResource(name="Example Filled", steward=steward)
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        # what-it-is, getting-in, whos-answerable, what-it-costs - all four
        # open facets confirmed by _makeResource's non-blank defaults; the
        # fifth badge on the page (committee-only) is Privileged, not this one.
        self.assertEqual(content.count('<span class="badge badge-active">Confirmed</span>'), 4)
        self.assertIn(f'href="{self._sectionUrl(resource, "what-it-costs")}">Edit</a>', content)

    def test_the_committee_card_shows_privileged_and_is_absent_for_a_non_audit_manager(self):
        resource = _makeResource(name="Example Committee Card")
        openManager = UserFactory.make("openManager", perms=("manageChapterTools",))
        self.loginAs(openManager)
        self.assertNotContains(self.client.get(resource.getUrl()), "Committee only")

        self.loginAs(self.manager)
        response = self.client.get(resource.getUrl())
        self.assertContains(response, '<span class="record-title">Committee only</span>')
        self.assertContains(response, '<span class="badge badge-denied">Privileged</span>')

    def test_the_whos_answerable_card_is_absent_for_a_plain_member_and_present_for_a_manager_or_holder_viewer(self):
        """The steward leak guard - getStewardDisplayName is holder-tier, so
        the card built from it must not exist at all for a plain member (no
        holder tier, no manageChapterTools).

        DECIDED POLICY: a manageChapterTools-ONLY editor (no
        viewResourceHolders/viewChapterToolAudit) DOES get the card - before
        this hub existed, that editor could already see and edit the steward
        on the flat workbench's Details tab (steward was never in
        RESTRICTED_KEYS), so gating the card on hasHolders alone would remove
        a UI path they used to have while section/whos-answerable kept
        answering 200 underneath them - a hub that can no longer even link to
        a page it still serves."""
        resource = _makeResource(name="Example Steward Gate")
        plainMember = UserFactory.make("plainMember")
        self.loginAs(plainMember)
        self.assertNotContains(self.client.get(resource.getUrl()), "Who&#x27;s answerable")

        managerOnly = UserFactory.make("managerOnly", perms=("manageChapterTools",))
        self.loginAs(managerOnly)
        self.assertContains(self.client.get(resource.getUrl()), "Who&#x27;s answerable")

        holderManager = UserFactory.make(
            "holderManager", perms=("manageChapterTools", "viewResourceHolders"),
        )
        self.loginAs(holderManager)
        self.assertContains(self.client.get(resource.getUrl()), "Who&#x27;s answerable")

    def test_a_manage_only_editor_sees_the_whos_answerable_cards_edit_or_confirm_affordance(self):
        """Not just the card title - the button that makes the card usable.
        Fresh (unconfirmed) gets the Confirm button; a resource with a
        recorded steward gets Edit - both link to the same section page this
        editor's manageChapterTools already lets them POST to."""
        managerOnly = UserFactory.make("managerOnly", perms=("manageChapterTools",))
        self.loginAs(managerOnly)

        fresh = _makeResource(name="Example Fresh Manager Only")
        freshContent = self.client.get(fresh.getUrl()).content.decode()
        self.assertIn("Who&#x27;s answerable", freshContent)
        self.assertIn(
            f'href="{self._sectionUrl(fresh, "whos-answerable")}">Confirm the steward</a>',
            freshContent,
        )

        confirmed = _makeResource(name="Example Confirmed Manager Only", stewardName="Jo")
        confirmedContent = self.client.get(confirmed.getUrl()).content.decode()
        self.assertIn(
            f'href="{self._sectionUrl(confirmed, "whos-answerable")}">Edit</a>',
            confirmedContent,
        )

    def test_card_id_matches_its_facet_slug(self):
        resource = _makeResource(name="Example Ids")
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        for facet in chapterForms.FACETS:
            with self.subTest(slug=facet.slug):
                self.assertIn(f'id="{facet.slug}"', content)

    def test_saved_query_param_renders_the_saved_alert(self):
        resource = _makeResource(name="Example Saved")
        self.loginAs(self.manager)
        self.assertContains(
            self.client.get(f"{resource.getUrl()}?saved=what-it-costs"), "Saved.",
        )
        self.assertNotContains(self.client.get(resource.getUrl()), "Saved.")

    def test_the_audit_read_notice_shows_for_an_auditor_and_not_for_anyone_else(self):
        """The ToolAuditReadLog write (chapterToolsViews._logRestrictedRead)
        still fires for hasAudit renders of this page - this notice is the
        on-page half of that contract, telling the auditor their read was
        logged. It sits where the audit-only content begins, immediately
        before the committee-only card."""
        resource = _makeResource(name="Example Audit Notice")
        notice = "You're viewing the restricted layer - this read was logged."

        self.loginAs(self.manager)  # has viewChapterToolAudit
        self.assertContains(self.client.get(resource.getUrl()), notice)

        openManager = UserFactory.make("openManager", perms=("manageChapterTools",))
        self.loginAs(openManager)
        self.assertNotContains(self.client.get(resource.getUrl()), notice)

        self.loginAs(self.member)
        self.assertNotContains(self.client.get(resource.getUrl()), notice)

    def test_every_hub_chrome_key_is_actually_rendered_by_the_hub_chrome(self):
        """The justification for excluding a key from every facet card
        (HUB_CHROME_KEYS): it has to actually appear somewhere ELSE on the
        page - the kept-verbatim lead (detail.html's title/blurb/how-to-get-
        access line, the access-model callout, and the site/request-access
        buttons) - or it would just vanish off the page entirely."""
        resource = _makeResource(
            name="Example Chrome",
            blurb="A distinctive blurb for this hub-chrome check.",
            howToGetAccess="A distinctive how-to-get-access line.",
            accessModel=ChapterResource.AccessModel.SHARED_VAULT,
            siteUrl="https://example-chrome.invalid/",
            accessRequestUrl="https://example-chrome-request.invalid/",
        )
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        self.assertIn("Example Chrome", content)  # NAME (the h1)
        self.assertIn("A distinctive blurb for this hub-chrome check.", content)  # BLURB
        self.assertIn("A distinctive how-to-get-access line.", content)  # HOW_TO_GET_ACCESS
        self.assertIn(resource.get_accessModel_display(), content)  # ACCESS_MODEL
        self.assertIn("example-chrome.invalid", content)  # SITE_URL
        self.assertIn("https://example-chrome-request.invalid/", content)  # ACCESS_REQUEST_URL

    def test_unconfirmed_count_matches_the_visible_cards(self):
        # _makeResource's defaults confirm what-it-is/getting-in/what-it-costs
        # (non-blank blurb, a real access model and payer) - only
        # who's-answerable (no steward) is unconfirmed, and committee-only is
        # excluded from the count regardless.
        resource = _makeResource(name="Example Count")
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        self.assertIn("1 section still needs confirming.", content)

    def test_hub_fact_labels_match_the_section_forms_own_field_labels(self):
        resource = _makeResource(name="Example Labels", costNote="Five seats.")
        self.loginAs(self.manager)
        content = self.client.get(resource.getUrl()).content.decode()
        costForm = ChapterResourceForm(rows=chapterForms._FACET_BY_SLUG["what-it-costs"].rows)
        self.assertIn(f"<dt>{costForm.fields['costNote'].label}</dt>", content)

    def test_each_section_save_touches_only_its_own_facet(self):
        """Snapshot every column, POST a payload for that facet with values
        deliberately DIFFERENT from the resource's current stored state (so a
        cross-facet write of an unchanged value could not slip past this
        check vacuously), and assert every column OUTSIDE the facet is
        bit-for-bit identical afterward.

        Every POST also carries FORGED keys for fields the current facet does
        not own (requestable, delegationTier, steward, name) - a body an
        attacker or a stale cached form could send regardless of which fields
        the server actually renders. Django only reads a form's DECLARED
        fields out of cleaned_data (self.fields was already sliced to `rows`
        in __init__), so these must have zero effect no matter which facet
        they ride along with - that is what the "any facet, any body" style
        of this test is actually checking, not just "the default payload
        happens not to touch anything."""
        steward = UserFactory.make("steward")
        otherSteward = UserFactory.make("otherSteward")
        resource = _makeResource(
            name="Example Isolation", steward=steward,
            delegationTier=ChapterResource.DelegationTier.GREEN,
        )
        allKeys = list(_FIELD_DEFAULTS)
        self.loginAs(self.manager)

        # Each facet's own payload, changed away from the stored state above
        # in every field it owns.
        changedPayloads = {
            "what-it-is": _sectionPayload(
                "what-it-is", name="Example Isolation Renamed", blurb="Changed blurb.",
                category=str(ChapterResource.Category.INFRASTRUCTURE),
            ),
            "getting-in": _sectionPayload(
                "getting-in", accessModel=str(ChapterResource.AccessModel.INDIVIDUAL),
                howToGetAccess="Changed access instructions.",
                accessRequestUrl="https://example-form.invalid/changed",
                siteUrl="https://example-site.invalid/changed",
            ),
            "whos-answerable": _sectionPayload("whos-answerable", steward=str(otherSteward.pk)),
            "what-it-costs": _sectionPayload(
                "what-it-costs", payer=str(ChapterResource.Payer.FREE),
                annualCost="12.34", costNote="Changed cost note.",
            ),
            "committee-only": _sectionPayload(
                "committee-only", delegationTier=str(ChapterResource.DelegationTier.RED),
                lastReviewed="2026-02-02", reviewedBy="Changed Reviewer",
                revocationNote="Changed revocation note.", continuityNote="Changed continuity note.",
            ),
        }
        # Forged out-of-facet keys, present on EVERY post regardless of which
        # facet owns them. Where a facet's own payload also names one of
        # these keys (committee-only owns requestable/delegationTier,
        # whos-answerable owns steward, what-it-is owns name), the facet's
        # own intended value below overwrites the forged one - the forgery
        # only matters for the facets that do NOT own the key.
        forgedKeys = {
            "requestable": "on",
            "delegationTier": str(ChapterResource.DelegationTier.RED),
            "steward": str(otherSteward.pk),
            "name": "Forged Name",
        }

        for facet in chapterForms.FACETS:
            with self.subTest(facet=facet.slug):
                before = {key: getattr(resource, key) for key in allKeys}
                payload = dict(forgedKeys)
                payload.update(changedPayloads[facet.slug])
                response = self.client.post(self._sectionUrl(resource, facet.slug), payload)
                self.assertEqual(response.status_code, 302, response.content)
                resource.refresh_from_db()
                after = {key: getattr(resource, key) for key in allKeys}
                for key in allKeys:
                    if key in facet.keys:
                        continue
                    with self.subTest(facet=facet.slug, field=key):
                        self.assertEqual(before[key], after[key],
                                         f"{key} changed by a {facet.slug} save")

    def test_the_word_facet_never_reaches_a_page(self):
        resource = _makeResource(
            name="Example No Jargon", steward=UserFactory.make("steward"),
        )
        self.loginAs(self.manager)
        urls = [
            resource.getUrl(),
            reverse("chapter-tool-new"),
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk}),
            *(self._sectionUrl(resource, facet.slug) for facet in chapterForms.FACETS),
        ]
        for url in urls:
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertNotIn("facet", content.lower())


class ChapterToolsDirectoryPagingTests(LoginClientMixin, TestCase):
    """The directory pages, and the access-model definition moved out of the
    card that used to sit under the whole list.

    These two changes are one change. The old footer legend was built from the
    access models present in the rows on screen, so paginating the register
    without moving it would have defined a phrase on the page where a matching
    row happened to fall and left it undefined on the next one."""

    def setUp(self):
        self.member = UserFactory.make("paging.member")
        self.holderViewer = UserFactory.make("paging.holders", perms=("viewResourceHolders",))
        self.url = reverse("chapter-tools")

    def _makeResources(self, count):
        # Zero-padded names so lexical order matches creation order. The view
        # orders by (category, name), so an unpadded 10 would sort before 2 and
        # the page-boundary assertions below would be testing something other
        # than what they say.
        return [
            _makeResource(name=f"Example Paged Tool {index:03d}")
            for index in range(count)
        ]

    def test_the_directory_pages_at_twenty_five(self):
        self._makeResources(26)
        self.loginAs(self.member)

        firstPage = self.client.get(self.url)
        self.assertEqual(len(firstPage.context["rows"]), 25)
        self.assertEqual(firstPage.context["page"].paginator.num_pages, 2)

        secondPage = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(secondPage.context["rows"]), 1)

    def test_a_short_register_renders_no_pager_at_all(self):
        """The partial hides itself below two pages. Asserted because a register
        that fits on one page is the normal case, and a lone disabled "1" would
        be furniture."""
        self._makeResources(3)
        self.loginAs(self.member)
        response = self.client.get(self.url)
        self.assertEqual(response.context["page"].paginator.num_pages, 1)
        self.assertNotContains(response, 'aria-label="Pagination"')

    def test_every_resource_appears_on_exactly_one_page(self):
        """The real risk with a Paginator is a row that repeats or vanishes at a
        page boundary, which happens when the ordering is not total. Reads the
        pages rather than trusting the order_by."""
        self._makeResources(26)
        self.loginAs(self.member)

        seen = []
        for pageNumber in (1, 2):
            response = self.client.get(self.url, {"page": pageNumber})
            seen.extend(row["resource"].pk for row in response.context["rows"])

        self.assertEqual(len(seen), 26)
        self.assertEqual(len(set(seen)), 26, "a resource appeared on two pages")

    def test_an_out_of_range_page_lands_on_a_real_one(self):
        """get_page clamps rather than 404ing - a stale bookmark should still
        show the register."""
        self._makeResources(26)
        self.loginAs(self.member)
        for bogus in ("99", "0", "-1", "nope", ""):
            with self.subTest(page=bogus):
                response = self.client.get(self.url, {"page": bogus})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["rows"])

    def test_the_footer_legend_card_is_gone(self):
        """It defined "How access works" as far from the phrase as the page
        allowed. Pinned as an absence so it cannot come back alongside the
        inline definition and print the same prose twice."""
        self._makeResources(2)
        self.loginAs(self.member)
        response = self.client.get(self.url)
        self.assertNotContains(response, "line means")
        self.assertNotIn("accessModelLegend", response.context)

    def test_the_access_model_definition_opens_from_the_card(self):
        """One disclosure per card, beside the value it defines."""
        self._makeResources(2)
        self.loginAs(self.member)
        content = self.client.get(self.url).content.decode()
        self.assertIn("How access works", content)
        self.assertIn("What this means", content)
        # The wrapper the CSS targets, so an opened panel gets the full row
        # rather than the width of the label.
        self.assertIn('class="explain-row"', content)

    def test_the_access_model_definition_lists_every_model_not_only_those_on_screen(self):
        """The whole reason the legend had to move. One resource on the page, and
        the reader is still told what all five models mean - otherwise the
        definition would depend on which page they happened to open."""
        _makeResource(name="Example Only Row", accessModel=ChapterResource.AccessModel.INDIVIDUAL)
        self.loginAs(self.member)
        content = self.client.get(self.url).content.decode()
        for _, label in ChapterResource.ACCESS_MODEL_CHOICES:
            with self.subTest(model=label):
                self.assertIn(escape(label), content)

    def test_the_holder_badge_note_survived_the_legend_being_removed(self):
        """The footer card carried a second thing: what "unconfirmed" beside a
        name means. Deleting the card without rehoming that note would have
        dropped it silently, so it is asserted rather than assumed."""
        resource = _makeResource(name="Example Noted Tool")
        ResourceHolder.objects.create(
            resource=resource, personName="Example Paging Holder", confirmed=False,
        )
        self.loginAs(self.holderViewer)
        content = self.client.get(self.url).content.decode()
        self.assertIn("How to read this", content)
        self.assertIn("nobody has verified", content)

    def test_a_plain_member_gets_no_holder_note_because_they_get_no_roster(self):
        """The note explains a badge they never see. Same tier as the roster."""
        resource = _makeResource(name="Example Unseen Tool")
        ResourceHolder.objects.create(resource=resource, personName="Example Hidden Holder")
        self.loginAs(self.member)
        content = self.client.get(self.url).content.decode()
        self.assertNotIn("How to read this", content)
        self.assertNotIn("Example Hidden Holder", content)


class ChapterToolsWorkbenchConnectionCountTests(LoginClientMixin, TestCase):
    """The workbench tab count must equal what its panel lists.

    The bug: the count read only the forward edges while the panel rendered
    both directions, so a resource that needs nothing and has two things
    running on it displayed "(0)" directly above a list of two."""

    def setUp(self):
        self.editor = UserFactory.make(
            "count.editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.openEditor = UserFactory.make("count.open.editor", perms=("manageChapterTools",))
        self.host = _makeResource(name="Example Count Host")
        self.riderOne = _makeResource(name="Example Count Rider One")
        self.riderTwo = _makeResource(name="Example Count Rider Two")
        self.editUrl = reverse("chapter-tool-edit", kwargs={"pk": self.host.pk})

    def test_the_count_includes_reverse_edges(self):
        """The exact reported shape: no forward edges, two reverse ones."""
        for rider in (self.riderOne, self.riderTwo):
            ResourceDependency.objects.create(
                resource=rider, dependsOn=self.host, kind=ResourceDependency.Kind.RUNS_ON,
            )
        self.loginAs(self.editor)
        response = self.client.get(self.editUrl)
        self.assertEqual(response.context["connectionCount"], 2)
        self.assertContains(response, ">(2)<")

    def test_the_count_matches_the_panel_for_every_mix_of_directions(self):
        """Read from the rendered context rather than recomputed, so this stays
        a statement about agreement rather than a second copy of the sum."""
        ResourceDependency.objects.create(
            resource=self.host, dependsOn=self.riderOne, kind=ResourceDependency.Kind.SIGN_IN,
        )
        ResourceDependency.objects.create(
            resource=self.riderTwo, dependsOn=self.host, kind=ResourceDependency.Kind.RUNS_ON,
        )
        self.loginAs(self.editor)
        response = self.client.get(f"{self.editUrl}?tab=dependencies")
        panelTotal = len(response.context["dependencies"]) + len(response.context["dependents"])
        self.assertEqual(response.context["connectionCount"], panelTotal)
        self.assertEqual(panelTotal, 2)

    def test_the_count_excludes_restricted_edges_for_a_non_audit_editor(self):
        """A count larger than the list would announce the existence of RUNS_ON
        edges to an editor the view deliberately hides them from - leaking the
        fact through arithmetic instead of through markup."""
        ResourceDependency.objects.create(
            resource=self.riderOne, dependsOn=self.host, kind=ResourceDependency.Kind.RUNS_ON,
        )
        ResourceDependency.objects.create(
            resource=self.riderTwo, dependsOn=self.host, kind=ResourceDependency.Kind.REACHED_THROUGH,
        )
        self.loginAs(self.openEditor)
        response = self.client.get(self.editUrl)
        panelTotal = len(response.context["dependencies"]) + len(response.context["dependents"])
        self.assertEqual(response.context["connectionCount"], panelTotal)
        self.assertEqual(response.context["connectionCount"], 1, "a restricted edge was counted")

    def test_the_tab_label_no_longer_names_only_one_direction(self):
        """"What it needs" could not honestly carry a count of both directions."""
        self.loginAs(self.editor)
        response = self.client.get(self.editUrl)
        self.assertContains(response, "Connections")
        self.assertNotContains(response, "What it needs")

    def test_the_tab_key_is_still_dependencies(self):
        """The label changed, the key must not: the child add/edit/delete views
        redirect back into ?tab=dependencies by name."""
        self.loginAs(self.editor)
        self.assertContains(self.client.get(self.editUrl), 'href="?tab=dependencies"')
