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
from django.test import TestCase
from django.urls import reverse

from tools.models import (
    ChapterResource, ResourceCredential, ResourceHolder, ResourceQuestion, ToolAuditReadLog,
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
