"""Chapter Tools (IT access registry, M1). All data here is invented - never
real chapter services, credentials, or people (this repo is public).

Covers: the requestable/steward clean() rule, the open/restricted visibility
split on the directory + detail pages, the questions workbench's permission
gate, the read-log write-exactly-on-restricted-render contract, and the
seed_chapter_tools management command's idempotency.
"""
import datetime
import json
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tools.models import (
    ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder, ResourceQuestion,
    ToolAuditReadLog,
)
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing


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
        self.resource = _makeResource(
            delegationTier=ChapterResource.DelegationTier.RED,
            revocationNote="Rotate the shared vault login.",
            continuityNote="Backup admin: Example Backup.",
            lastReviewed=None,  # stale by default
        )
        ResourceHolder.objects.create(
            resource=self.resource, personName="Example Holder", confirmed=False,
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
        self.assertContains(resp, "Example Holder")

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
        self.assertContains(resp, "Example Holder")

    def test_detail_hides_restricted_section_from_plain_member(self):
        self.loginAs(self.member)
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
    """The SIGN_IN / RUNS_ON edges: forward + reverse rendering across the
    open/restricted split, self-reference and duplicate rejection, and the
    index prefetch's query-count contract. See the plan
    (chapter-tools-dependencies) for why these are the two fixed kinds and
    why cycles are deliberately not prevented.
    """

    def setUp(self):
        self.member = UserFactory.make("member")
        self.auditor = UserFactory.make("auditor", perms=("viewChapterToolAudit",))
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
        self.assertNotContains(resp, "You need ExampleHostingSentinel first")  # the edge - not open

    # --- 3. SIGN_IN does reach a plain member, index and detail ---

    def test_sign_in_reaches_plain_member_on_index_and_detail(self):
        ResourceDependency.objects.create(
            resource=self.wiki, dependsOn=self.identityProvider,
            kind=ResourceDependency.Kind.SIGN_IN,
        )
        self.loginAs(self.member)

        indexResp = self.client.get(reverse("chapter-tools"))
        self.assertContains(indexResp, "You need Example SSO first")

        detailResp = self.client.get(self.wiki.getUrl())
        self.assertContains(detailResp, "You need Example SSO first")

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
