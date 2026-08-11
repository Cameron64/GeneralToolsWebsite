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

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tools.models import (
    ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder, ResourceQuestion,
    ToolAuditReadLog,
)
from tools.forms import ResourceDependencyForm
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
        resource = _makeResource(requestable=True, steward=steward)
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
        self.assertNotContains(resp, "Committee detail")
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
        self.assertNotContains(resp, "Committee detail")
        self.assertNotContains(resp, "Rotate the shared vault login.")
        self.assertNotContains(resp, "ExampleCredentialSentinel")

    def test_detail_shows_restricted_section_to_auditor(self):
        self.loginAs(self.auditor)
        resp = self.client.get(self.resource.getUrl())
        self.assertContains(resp, "Committee detail")
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

    def test_index_legend_covers_every_access_model_shown_and_nothing_else(self):
        """The legend is built from the rows actually on screen, so a member
        never reads a definition for a model the page doesn't use."""
        _makeResource(name="Example Bank", accessModel=ChapterResource.AccessModel.INDIVIDUAL)
        self.loginAs(self.member)
        resp = self.client.get(reverse("chapter-tools"))
        # Sentinels avoid apostrophes on purpose - the template escapes them to
        # &#x27; and a raw "person's" would never match the rendered HTML.
        self.assertContains(resp, "you need a vault account")          # SHARED_VAULT, in use
        self.assertContains(resp, "does not affect anybody else")      # INDIVIDUAL, in use
        self.assertNotContains(resp, "An automated account does the work")  # SERVICE_ACCOUNT, unused

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
        self.assertContains(indexResp, "You do not get access to this directly")

        detailResp = self.client.get(self.wiki.getUrl())
        self.assertContains(detailResp, EDGE_PHRASE.format(name="Example SSO"))
        self.assertContains(detailResp, "You do not get access to this directly")

    def test_reached_through_does_not_render_as_a_sign_in_prerequisite(self):
        """The two must not collapse into one another. "You need X first" says
        get X as well; "you do not get access to this directly" says X is
        instead of, not as well as. Rendering a reached-through edge with the
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


def _resourcePayload(**overrides):
    """A complete OPEN-layer POST body. The four choice fields are required
    (TypedChoiceField is required by default), so a payload missing one fails
    validation for a reason that has nothing to do with what is being tested."""
    payload = {
        "name": "Example Wiki",
        "category": str(ChapterResource.Category.COMMUNICATION),
        "accessModel": str(ChapterResource.AccessModel.SHARED_VAULT),
        "payer": str(ChapterResource.Payer.CHAPTER),
        "blurb": "",
        "annualCost": "",
        "costNote": "",
        "howToGetAccess": "",
        "siteUrl": "",
        "accessRequestUrl": "",
        "steward": "",
        "stewardName": "",
    }
    payload.update(overrides)
    return payload


def _restrictedPayload(**overrides):
    """The open payload plus the six restricted fields an audit editor also
    submits. Mirrors ChapterResourceForm.RESTRICTED_KEYS."""
    payload = _resourcePayload()
    payload.update({
        "requestable": "",
        "lastReviewed": "",
        "reviewedBy": "",
        "delegationTier": str(ChapterResource.DelegationTier.UNCLASSIFIED),
        "revocationNote": "",
        "continuityNote": "",
    })
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
        ):
            response = self.client.get(url)
            # permission_required without raise_exception redirects to login -
            # the convention the other admin-tier access pages follow.
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("login", response["Location"], url)

    def test_an_editor_can_reach_the_workbench(self):
        self.loginAs(self.editor)
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Who has it now")

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
        self.resource = _makeResource(
            delegationTier=ChapterResource.DelegationTier.RED,
            revocationNote="Rotate the shared vault login.",
            continuityNote=CONTINUITY_SENTINEL,
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

    def test_restricted_fields_are_absent_from_a_non_audit_editors_form(self):
        self.loginAs(self.editor)
        response = self.client.get(self.editUrl)
        self.assertNotContains(response, TIER_FIELD_SENTINEL)
        # The stored VALUE, not just the field name - a bound form renders
        # current values, which is the actual leak.
        self.assertNotContains(response, CONTINUITY_SENTINEL)

    def test_restricted_fields_are_present_for_an_audit_editor(self):
        # The pairing that keeps the test above honest.
        self.loginAs(self.auditEditor)
        response = self.client.get(self.editUrl)
        self.assertContains(response, TIER_FIELD_SENTINEL)
        self.assertContains(response, CONTINUITY_SENTINEL)

    def test_credentials_are_hidden_from_a_non_audit_editor_and_shown_to_an_audit_one(self):
        self.loginAs(self.editor)
        self.assertNotContains(self.client.get(self.editUrl), CRED_SENTINEL)
        self.loginAs(self.auditEditor)
        self.assertContains(self.client.get(self.editUrl), CRED_SENTINEL)

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
        self.loginAs(self.editor)
        self.assertNotContains(self.client.get(self.editUrl), RUNS_ON_SENTINEL)
        self.loginAs(self.auditEditor)
        self.assertContains(self.client.get(self.editUrl), RUNS_ON_SENTINEL)

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
        """The whole reason _applyCleanedData copies only the keys present."""
        self.loginAs(self.editor)
        response = self.client.post(self.editUrl, _resourcePayload(name=self.resource.name))
        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.delegationTier, ChapterResource.DelegationTier.RED)
        self.assertEqual(self.resource.continuityNote, CONTINUITY_SENTINEL)
        self.assertEqual(self.resource.revocationNote, "Rotate the shared vault login.")


class ChapterToolsCrudWriteTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.editor = UserFactory.make(
            "editor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)

    def test_create_makes_a_resource_and_lands_on_its_workbench(self):
        response = self.client.post(
            reverse("chapter-tool-new"), _restrictedPayload(name="Example Vault"),
        )
        resource = ChapterResource.objects.get(name="Example Vault")
        self.assertRedirects(
            response, reverse("chapter-tool-edit", kwargs={"pk": resource.pk}),
        )

    def test_a_duplicate_name_is_a_field_error_not_a_500(self):
        _makeResource(name="Example Vault")
        response = self.client.post(
            reverse("chapter-tool-new"), _restrictedPayload(name="example vault"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(ChapterResource.objects.filter(name__iexact="example vault").count(), 1)

    def test_requestable_without_a_steward_is_rejected_by_the_form(self):
        """ChapterResource.clean()'s rule. The view never calls full_clean(), so
        without the form re-implementing it this would save happily."""
        response = self.client.post(
            reverse("chapter-tool-new"),
            _restrictedPayload(name="Example Vault", requestable="on", steward=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChapterResource.objects.filter(name="Example Vault").exists())

    def test_requestable_with_a_steward_saves(self):
        steward = UserFactory.make("steward")
        response = self.client.post(
            reverse("chapter-tool-new"),
            _restrictedPayload(name="Example Vault", requestable="on", steward=str(steward.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ChapterResource.objects.get(name="Example Vault").requestable)

    def test_a_non_audit_editor_cannot_strip_the_steward_off_a_requestable_resource(self):
        """The rule from the other direction: `requestable` is not on their form,
        so they cannot turn it on - but they can clear the steward, which breaks
        the same invariant."""
        steward = UserFactory.make("steward")
        resource = _makeResource(name="Example Vault", requestable=True, steward=steward)
        openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))
        self.loginAs(openEditor)
        response = self.client.post(
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk}),
            _resourcePayload(name="Example Vault", steward=""),
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
            "how": str(ResourceHolder.How.VAULT_COLLECTION), "confirmed": "on", "note": "",
        })
        holder = ResourceHolder.objects.get(resource=resource)
        self.assertTrue(holder.confirmed)
        self.assertEqual(holder.how, ResourceHolder.How.VAULT_COLLECTION)

        editUrl = reverse("chapter-tool-child-edit", kwargs={
            "pk": resource.pk, "childKind": "holders", "childId": holder.pk,
        })
        # An unchecked checkbox is simply absent from a real POST body.
        self.client.post(editUrl, {
            "personName": "Example Holder", "user": "",
            "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN), "note": "Now unconfirmed.",
        })
        holder.refresh_from_db()
        self.assertFalse(holder.confirmed)
        self.assertEqual(holder.note, "Now unconfirmed.")

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
        self._assertClean(
            self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk})),
            "Who has it now",
        )

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
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))
        self.assertContains(response, "record-list")
        self.assertNotContains(response, "data-table")

    def test_the_record_and_detail_grid_styles_are_actually_compiled(self):
        """output.css cannot be regenerated on this machine (the Tailwind CLI
        hits a lightningcss DLL failure), so a class used in a template is only
        real if it is already in the compiled file. This asserts the hand-mirrored
        rules are present AND that .detail-grid stacks at the same 48rem
        breakpoint .data-table collapses at."""
        cssPath = Path(__file__).resolve().parent.parent / "static" / "css" / "output.css"
        css = cssPath.read_text(encoding="utf-8")
        for className in (".record-list", ".record-item", ".record-meta",
                          ".record-actions", ".record-action-field"):
            self.assertIn(className, css, f"{className} is used in a template but not compiled")
        self.assertIn("grid-template-columns: 1fr", css)
        self.assertIn("@media (width < 48rem)", css)
