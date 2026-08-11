from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from tools.models import EVENT_LEAD_ROLE_GROUP, AccessRequests

from tools.tests.support import (
    LoginClientMixin, MailAssertionsMixin, UserFactory, fastHashing, permission,
)


@fastHashing
class ManageAccessTests(MailAssertionsMixin, LoginClientMixin, TestCase):
    def setUp(self):
        # This suite's cast is a perm-admin + a member who is NOT in the group
        # (it grants membership in a test), so it builds directly rather than
        # via AccessFixtureMixin.buildCast() (which puts member in the group).
        self.group = Group.objects.create(name="Anti-ICE Campaign")
        self.admin = UserFactory.admin("admin")
        self.member = UserFactory.make("member")

    def test_plain_user_cannot_open_manage_pages(self):
        self.loginAs(self.member)
        resp = self.client.get(reverse("manage-access"))
        self.assertEqual(resp.status_code, 302)  # bounced by permission_required
        resp = self.client.post(
            reverse("manage-access-user", kwargs={"userId": self.member.id}),
            {"groups": [self.group.id], "permissions": []},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(self.group, self.member.groups.all())

    def test_admin_sees_member_list(self):
        self.loginAs(self.admin)
        resp = self.client.get(reverse("manage-access"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "member")
        self.assertContains(resp, reverse("manage-access-user", kwargs={"userId": self.member.id}))

    def test_admin_grants_and_revokes_group_and_permission(self):
        perm = permission("manageLinkTree")
        self.loginAs(self.admin)
        url = reverse("manage-access-user", kwargs={"userId": self.member.id})

        # Grant a group + a direct permission
        resp = self.client.post(url, {"groups": [self.group.id], "permissions": [perm.id]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.group, self.member.groups.all())
        self.assertIn(perm, self.member.user_permissions.all())

        # Revoke everything
        self.client.post(url, {"groups": [], "permissions": []})
        self.member.refresh_from_db()
        self.assertEqual(self.member.groups.count(), 0)
        self.assertNotIn(perm, self.member.user_permissions.all())

    def test_non_custom_direct_permissions_are_preserved(self):
        # A model permission granted outside this page must survive a save
        otherPermission = Permission.objects.get(codename="view_linktree")
        self.member.user_permissions.add(otherPermission)
        self.loginAs(self.admin)
        self.client.post(
            reverse("manage-access-user", kwargs={"userId": self.member.id}),
            {"groups": [], "permissions": []},
        )
        self.assertIn(otherPermission, self.member.user_permissions.all())

    def test_direct_grant_closes_matching_pending_request(self):
        pending = AccessRequests.objects.create(
            requester=self.member, group=self.group, justification="please"
        )
        unrelated = AccessRequests.objects.create(
            requester=self.member,
            permission=permission("publishEvent"),
            justification="separate ask",
        )
        self.loginAs(self.admin)
        self.client.post(
            reverse("manage-access-user", kwargs={"userId": self.member.id}),
            {"groups": [self.group.id], "permissions": []},
        )
        pending.refresh_from_db()
        self.assertEqual(pending.status, AccessRequests.Status.APPROVED)
        self.assertEqual(pending.reviewer, self.admin)
        self.assertEqual(pending.reason, "Access granted directly")
        self.assertIsNotNone(pending.dateReviewed)
        # requester is notified
        self.assertEmailedTo(self.member.email)
        # the unrelated pending request is untouched
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, AccessRequests.Status.REQUESTED)


@fastHashing
class ManageAccessTemplateCommentTests(LoginClientMixin, TestCase):
    """A rendered page must never show the notes we write for maintainers.

    This exists because it already happened: the Event Leads note below was
    first written with Django's short hash-brace comment form, which is
    SINGLE-LINE only. A multi-line one is not a comment at all - it renders as
    literal body text, and because it sat inside the group loop it repeated once
    per row. Every absence assertion here is paired with a presence assertion on
    the real hint, so a markup change that stops the section rendering fails
    loudly instead of letting the absence checks pass for the wrong reason.
    """

    def setUp(self):
        self.admin = UserFactory.admin("admin")
        self.member = UserFactory.make("member")
        # The derived group by its real name - that string is what the template
        # matches on, so the hint only renders when a group is named exactly this.
        Group.objects.create(name=EVENT_LEAD_ROLE_GROUP)
        Group.objects.create(name="Example Working Group")

    def _render(self) -> str:
        self.loginAs(self.admin)
        resp = self.client.get(
            reverse("manage-access-user", kwargs={"userId": self.member.id})
        )
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_maintainer_notes_do_not_render_as_page_text(self):
        content = self._render()
        for leak in ("{#", "#}", "{% comment", "endcomment",
                     "EVENT_LEAD_ROLE_GROUP", "revokeCommitteeMembership",
                     "tools/models.py", ".groups.set()"):
            self.assertNotIn(leak, content, f"template comment leaked: {leak}")

    def test_the_derived_group_hint_does_render(self):
        # The pairing that keeps the test above honest.
        content = self._render()
        self.assertIn("Derived", content)
        self.assertIn(EVENT_LEAD_ROLE_GROUP, content)

    def test_the_hint_appears_once_not_once_per_group_row(self):
        # The repeat-per-row half of the original bug.
        content = self._render()
        self.assertEqual(content.count("follows committee authorization"), 1)
