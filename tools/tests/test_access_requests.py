import datetime
from unittest import mock

from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from tools import permissions
from tools.models import AccessRequests, EventOwners
from tools.models import EVENT_LEAD_ROLE_GROUP, revokeCommitteeMembership

from tools.tests.support import (
    AccessFixtureMixin, LoginClientMixin, MailAssertionsMixin,
    UserFactory, fastHashing, permission, refetchForPerms,
)

# isActive() short-circuits on isPermanent, but expiration is a required column.
FAR_FUTURE = datetime.datetime(2099, 12, 31, tzinfo=datetime.UTC)


@fastHashing
class SelfServiceAccessFormTests(AccessFixtureMixin, MailAssertionsMixin, LoginClientMixin, TestCase):
    """Replaces AccessRequestCreateTests and AccessRequestPermissionDropdownTests
    (both retired outright - see below) now that request_access is the checklist
    diff-and-apply form from the access-self-service-form plan, not the old
    single-target `target=` dropdown those classes pinned.

    Contracts ported forward from the retired classes: creating a row + who
    gets emailed, duplicate-pending rejection, and email-failure-is-never-fatal.
    "Groups are no longer self-requestable" is dropped outright rather than
    ported - the checklist has no group option at all now (SelfServiceAccessForm
    shadows `groups` to None), so there is nothing left for a test like that to
    pin. AccessRequestPermissionDropdownTests is retired in full: it existed
    purely to pin <optgroup>/dropdown-value rendering, and there is no dropdown
    left to render.
    """

    def setUp(self):
        cast = self.buildCast()
        self.admin, self.requester = cast["admin"], cast["requester"]
        # buildCast's admin holds approveAccessRequest; the original suite's
        # admin was a superuser - keep both so the cast matches the old fixture.
        self.admin.is_superuser = True
        self.admin.save()
        self.approver = UserFactory.make("approver", perms=("approveAccessRequest",))
        # Members self-request to JOIN an event owner (committee); approval adds
        # them to its authorizers, and the owner's current authorizers are the
        # peer reviewers.
        self.owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        self.ownerMember = UserFactory.make("ownermember")
        self.owner.authorizers.add(self.ownerMember)

    def _post(self, committees=(), permissionsList=(), justification="I work on this campaign.",
              renderedChecked=(), follow=True):
        return self.client.post(
            reverse("request-access"),
            {
                "justification": justification,
                "committees": list(committees),
                "permissions": list(permissionsList),
                "renderedChecked": list(renderedChecked),
            },
            follow=follow,
        )

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(reverse("request-access"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_owner_request_creates_row(self):
        self.loginAs(self.requester)
        resp = self._post(committees=[self.owner.id])
        self.assertEqual(resp.status_code, 200)  # followed the D10 redirect
        request = AccessRequests.objects.get()
        self.assertEqual(request.status, AccessRequests.Status.REQUESTED)
        self.assertEqual(request.requester, self.requester)
        self.assertEqual(request.owner, self.owner)
        self.assertIsNone(request.group)
        self.assertIsNone(request.permission)
        self.assertEqual(request.justification, "I work on this campaign.")
        self.assertIsNotNone(request.dateCreated)
        self.assertIsNone(request.dateReviewed)
        self.assertIsNotNone(request.batchId)

    def test_owner_request_emails_admins_approvers_and_authorizers_once_each(self):
        # admin is also an authorizer - must still get exactly one email
        self.owner.authorizers.add(self.admin)
        self.loginAs(self.requester)
        self._post(committees=[self.owner.id])

        self.assertEmailedTo(self.admin.email, times=1)
        self.assertEmailedTo(self.ownerMember.email, times=1)
        self.assertEmailedTo(self.approver.email, times=1)
        # requester gets a confirmation
        self.assertEmailedTo(self.requester.email, times=1)

        request = AccessRequests.objects.get()
        reviewPath = reverse("review-access-request", kwargs={"id": request.id})
        approverMails = [m for m in mail.outbox if self.ownerMember.email in m.to]
        self.assertIn(reviewPath, approverMails[0].body)

    def test_permission_request_does_not_email_owner_authorizers(self):
        self.loginAs(self.requester)
        perm = permission("manageLinkTree")
        self._post(permissionsList=[perm.id])

        self.assertNotEmailedTo(self.ownerMember.email)
        self.assertEmailedTo(self.admin.email)
        self.assertEmailedTo(self.approver.email)

        request = AccessRequests.objects.get()
        self.assertEqual(request.permission, perm)
        self.assertIsNone(request.owner)
        self.assertIsNone(request.group)

    def test_existing_authorizer_checking_their_own_owner_creates_no_request(self):
        # Ported from the retired test_existing_authorizer_cannot_request_their_owner:
        # an authorizer's own committee is rendered checked (held), and resubmitting
        # it checked must never create a duplicate join request.
        self.loginAs(self.ownerMember)
        resp = self._post(committees=[self.owner.id], renderedChecked=[f"o:{self.owner.id}"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccessRequests.objects.count(), 0)
        self.assertIn(self.ownerMember, self.owner.authorizers.all())

    def test_duplicate_pending_request_is_rejected(self):
        self.loginAs(self.requester)
        self._post(committees=[self.owner.id])
        self.assertEqual(AccessRequests.objects.count(), 1)
        # A forged resubmission naming the same (now pending, hence hidden from
        # a real render) committee must not create a second row.
        self._post(committees=[self.owner.id])
        self.assertEqual(AccessRequests.objects.count(), 1)

    def test_email_failure_does_not_fail_request_creation(self):
        self.loginAs(self.requester)
        with mock.patch("tools.accessViews.send_mail", side_effect=Exception("smtp down")):
            resp = self._post(committees=[self.owner.id])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccessRequests.objects.count(), 1)


@fastHashing
class AccessRequestReviewTests(AccessFixtureMixin, MailAssertionsMixin, LoginClientMixin, TestCase):
    def setUp(self):
        cast = self.buildCast()
        self.group, self.member = cast["group"], cast["member"]
        self.admin, self.requester = cast["admin"], cast["requester"]
        # buildCast's admin holds approveAccessRequest; the original suite's
        # admin was a superuser - keep both so the cast matches the old fixture.
        self.admin.is_superuser = True
        self.admin.save()
        # extras this suite needs on top of the standard cast
        self.otherGroup = Group.objects.create(name="Other Campaign")
        self.otherMember = UserFactory.make("othermember", groups=[self.otherGroup])
        self.approver = UserFactory.make("approver", perms=("approveAccessRequest",))
        self.groupRequest = AccessRequests.objects.create(
            requester=self.requester,
            group=self.group,
            justification="please",
            status=AccessRequests.Status.REQUESTED,
        )

    def _reviewUrl(self, request=None):
        return reverse(
            "review-access-request", kwargs={"id": (request or self.groupRequest).id}
        )

    def _approve(self, reason="welcome aboard"):
        return self.client.post(self._reviewUrl(), {"approve": "YES", "reason": reason})

    def _deny(self, reason="not yet"):
        return self.client.post(self._reviewUrl(), {"approve": "NO", "reason": reason})

    def test_random_user_cannot_review(self):
        self.loginAs(UserFactory.make("random"))
        self._approve()
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.REQUESTED)
        self.assertNotIn(self.group, self.requester.groups.all())

    def test_nonexistent_request_is_indistinguishable_from_unauthorized(self):
        # Brittle by design: this asserts the anti-enumeration CONTRACT - a
        # missing id and a forbidden id must render the SAME template so ids
        # can't be probed. It is not coupled to template internals; keep it.
        # Probing ids must not reveal which requests exist (no enumeration
        # oracle) and must never leak exception text.
        self.loginAs(UserFactory.make("prober"))
        missing = self.client.get(reverse("review-access-request", kwargs={"id": 9999}))
        forbidden = self.client.get(self._reviewUrl())
        self.assertEqual(missing.status_code, 200)
        self.assertNotContains(missing, "DoesNotExist")
        self.assertEqual(
            missing.templates[0].name if missing.templates else None,
            forbidden.templates[0].name if forbidden.templates else None,
        )

    def test_member_of_other_group_cannot_review(self):
        self.loginAs(self.otherMember)
        self._approve()
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.REQUESTED)

    def test_requester_cannot_review_own_request_even_with_permission(self):
        self.requester.user_permissions.add(permission("approveAccessRequest"))
        self.loginAs(self.requester)
        self._approve()
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.REQUESTED)

    def test_group_member_can_approve_group_request(self):
        self.loginAs(self.member)
        self._approve()
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.APPROVED)
        self.assertEqual(self.groupRequest.reviewer, self.member)
        self.assertEqual(self.groupRequest.reason, "welcome aboard")
        self.assertIsNotNone(self.groupRequest.dateReviewed)
        self.assertIn(self.group, self.requester.groups.all())
        # requester is notified
        self.assertEmailedTo(self.requester.email)

    def test_owner_authorizer_can_approve_owner_request(self):
        owner = EventOwners.objects.create(
            name="Political Education", isPermanent=True, expiration=FAR_FUTURE,
        )
        authorizer = UserFactory.make("authorizer")
        owner.authorizers.add(authorizer)
        ownerRequest = AccessRequests.objects.create(
            requester=self.requester, owner=owner,
            justification="want in", status=AccessRequests.Status.REQUESTED,
        )
        self.loginAs(authorizer)
        self.client.post(self._reviewUrl(ownerRequest), {"approve": "YES", "reason": "welcome"})
        ownerRequest.refresh_from_db()
        self.assertEqual(ownerRequest.status, AccessRequests.Status.APPROVED)
        # Approval adds the requester to the owner's authorizers...
        self.assertIn(self.requester, owner.authorizers.all())
        # ...and adds them to the Event Leads role group, which carries the full
        # event-lead capability - publish AND approve delegated events - so the
        # join is actually useful (authorizer membership alone is inert without
        # the page-level permissions).
        self.assertTrue(self.requester.groups.filter(name="Event Leads").exists())
        leadRequester = refetchForPerms(self.requester)
        self.assertTrue(leadRequester.has_perm(permissions.PUBLISH_EVENT))
        self.assertTrue(leadRequester.has_perm(permissions.APPROVE_DELEGATED_EVENT))
        self.assertEmailedTo(self.requester.email)

    def test_non_authorizer_cannot_approve_owner_request(self):
        owner = EventOwners.objects.create(
            name="Political Education", isPermanent=True, expiration=FAR_FUTURE,
        )
        owner.authorizers.add(UserFactory.make("someauthorizer"))
        ownerRequest = AccessRequests.objects.create(
            requester=self.requester, owner=owner,
            justification="want in", status=AccessRequests.Status.REQUESTED,
        )
        # self.member is a group member but NOT an authorizer of this owner
        self.loginAs(self.member)
        self.client.post(self._reviewUrl(ownerRequest), {"approve": "YES", "reason": "ok"})
        ownerRequest.refresh_from_db()
        self.assertEqual(ownerRequest.status, AccessRequests.Status.REQUESTED)
        self.assertNotIn(self.requester, owner.authorizers.all())

    def test_permission_holder_can_approve_permission_request(self):
        perm = permission("manageLinkTree")
        permRequest = AccessRequests.objects.create(
            requester=self.requester,
            permission=perm,
            justification="link duty",
            status=AccessRequests.Status.REQUESTED,
        )
        self.loginAs(self.approver)
        self.client.post(self._reviewUrl(permRequest), {"approve": "YES", "reason": "ok"})
        permRequest.refresh_from_db()
        self.assertEqual(permRequest.status, AccessRequests.Status.APPROVED)
        self.assertIn(perm, self.requester.user_permissions.all())
        # Fresh instance so the permission cache is clean
        freshRequester = refetchForPerms(self.requester)
        self.assertTrue(freshRequester.has_perm(permissions.MANAGE_LINK_TREE))

    def test_group_member_cannot_approve_permission_request(self):
        permRequest = AccessRequests.objects.create(
            requester=self.requester,
            permission=permission("manageLinkTree"),
            justification="link duty",
            status=AccessRequests.Status.REQUESTED,
        )
        self.loginAs(self.member)
        self.client.post(self._reviewUrl(permRequest), {"approve": "YES", "reason": "ok"})
        permRequest.refresh_from_db()
        self.assertEqual(permRequest.status, AccessRequests.Status.REQUESTED)

    def test_superuser_can_approve_without_reason(self):
        # reason is optional - an approval with no note must go through
        self.loginAs(self.admin)
        self.client.post(self._reviewUrl(), {"approve": "YES", "reason": ""})
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.APPROVED)
        self.assertEqual(self.groupRequest.reason, "")

    def test_deny_grants_nothing_and_stamps_reason(self):
        self.loginAs(self.member)
        self._deny()
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.DENIED)
        self.assertEqual(self.groupRequest.reason, "not yet")
        self.assertNotIn(self.group, self.requester.groups.all())
        self.assertEmailedTo(self.requester.email)

    def test_already_reviewed_request_cannot_be_rereviewed(self):
        self.loginAs(self.member)
        self._approve()
        self.groupRequest.refresh_from_db()
        firstReviewDate = self.groupRequest.dateReviewed

        self.loginAs(self.admin)
        self._deny(reason="changed my mind")
        self.groupRequest.refresh_from_db()
        self.assertEqual(self.groupRequest.status, AccessRequests.Status.APPROVED)
        self.assertEqual(self.groupRequest.reviewer, self.member)
        self.assertEqual(self.groupRequest.dateReviewed, firstReviewDate)
        self.assertIn(self.group, self.requester.groups.all())


@fastHashing
class AccessRequestListTests(AccessFixtureMixin, LoginClientMixin, TestCase):
    def setUp(self):
        cast = self.buildCast()
        self.group, self.member = cast["group"], cast["member"]
        self.requester = cast["requester"]
        self.request = AccessRequests.objects.create(
            requester=self.requester,
            group=self.group,
            justification="please",
            status=AccessRequests.Status.REQUESTED,
        )

    def test_requester_sees_own_request(self):
        self.loginAs(self.requester)
        resp = self.client.get(reverse("access-request-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Anti-ICE Campaign")

    def test_approver_sees_actionable_request(self):
        self.loginAs(self.member)
        resp = self.client.get(reverse("access-request-list"))
        self.assertContains(resp, "Anti-ICE Campaign")
        self.assertContains(
            resp, reverse("review-access-request", kwargs={"id": self.request.id})
        )

    def test_uninvolved_user_sees_empty_state(self):
        self.loginAs(UserFactory.make("bystander"))
        resp = self.client.get(reverse("access-request-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Anti-ICE Campaign")

    def test_dates_render_in_browser_timezone(self):
        # Brittle (tz/middleware): depends on TimezoneMiddleware activating the
        # zone from the django_timezone cookie (set by base.html), so timestamps
        # show local time instead of UTC. Asserting CST/CDT literals. Keep as-is.
        # TimezoneMiddleware activates the tz from the django_timezone cookie
        # (set by base.html), so timestamps show local time instead of UTC.
        self.client.cookies["django_timezone"] = "America/Chicago"
        self.loginAs(self.member)
        resp = self.client.get(reverse("access-request-list"))
        self.assertTrue(
            b"CST" in resp.content or b"CDT" in resp.content,
            "expected Central-time timestamp on the page",
        )
        self.assertNotContains(resp, "UTC")


@fastHashing
class SelfServiceRevokeTests(LoginClientMixin, TestCase):
    """The revoke half of the self-service form (plan §6 tests 1 and 6): a
    directly-granted permission is removed the moment it's unchecked, with no
    approval and no AccessRequests row, and the removal is logged naming the
    actor and the target (D9)."""

    def _post(self, committees=(), permissionsList=(), renderedChecked=(), justification=""):
        return self.client.post(
            reverse("request-access"),
            {
                "justification": justification,
                "committees": list(committees),
                "permissions": list(permissionsList),
                "renderedChecked": list(renderedChecked),
            },
            follow=True,
        )

    def test_unchecking_directly_granted_permission_removes_it_immediately(self):
        member = UserFactory.make("directholder", perms=("manageLinkTree",))
        perm = permission("manageLinkTree")
        self.loginAs(member)

        resp = self._post(renderedChecked=[f"p:{perm.id}"])

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(member.user_permissions.filter(id=perm.id).exists())
        self.assertEqual(AccessRequests.objects.count(), 0)

    def test_leaving_a_committee_via_the_form_removes_the_authorization(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("committeeleaver")
        owner.authorizers.add(member)
        self.loginAs(member)

        resp = self._post(renderedChecked=[f"o:{owner.id}"])

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(member, owner.authorizers.all())
        self.assertEqual(AccessRequests.objects.count(), 0)

    def test_revoke_logs_actor_and_target(self):
        member = UserFactory.make(
            "logrevoke", perms=("manageLinkTree",), email="logrevoke@example.com",
        )
        perm = permission("manageLinkTree")
        self.loginAs(member)

        with self.assertLogs("tools.accessViews", level="INFO") as logs:
            resp = self._post(renderedChecked=[f"p:{perm.id}"])

        self.assertEqual(resp.status_code, 200)
        joinedLogs = "\n".join(logs.output)
        self.assertIn(member.email, joinedLogs)
        self.assertIn(perm.name, joinedLogs)


@fastHashing
class SelfServiceLockedPermissionTests(LoginClientMixin, TestCase):
    """Plan §6 test 2 - the ONE test that actually exercises D4's lock rule.

    A group-only permission is deliberately NOT this scenario: it survives any
    implementation, including one with no protection at all, because
    user_permissions.remove() is a no-op on something that was never a direct
    grant. The only state that proves the lock rule is doing something is a
    permission held BOTH directly and via a group - that's the only case where
    an unprotected implementation COULD remove real state (the direct row) and
    doesn't."""

    def test_dual_source_permission_survives_forged_omission_and_is_not_reported_removed(self):
        perm = permission("manageLinkTree")
        group = Group.objects.create(name="Example Committee Tools")
        group.permissions.add(perm)
        member = UserFactory.make("dualsource", perms=("manageLinkTree",), groups=[group])
        self.loginAs(member)

        getResp = self.client.get(reverse("request-access"))
        rows = {
            row["permission"].id: row
            for section in getResp.context["permissionSections"]
            for row in section["rows"]
        }
        self.assertTrue(rows[perm.id]["locked"])
        self.assertTrue(rows[perm.id]["checked"])
        # The row must render an actually-disabled input - a browser never
        # submits a disabled checkbox, which is the first line of defense.
        self.assertContains(getResp, f'value="{perm.id}"')

        # Forged POST: omit the permission (as if unchecked) AND forge
        # renderedChecked into naming it - even that must not be enough,
        # because the view treats a locked item as out-of-universe server-side
        # (D4/F6), never trusting a hidden echo of it.
        resp = self.client.post(
            reverse("request-access"),
            {
                "justification": "",
                "committees": [],
                "permissions": [],
                "renderedChecked": [f"p:{perm.id}"],
            },
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(member.user_permissions.filter(id=perm.id).exists())
        self.assertEqual(AccessRequests.objects.count(), 0)
        self.assertEqual(resp.context["result"]["removedPermissions"], [])


@fastHashing
class SelfServicePhantomRevokeGuardTests(LoginClientMixin, TestCase):
    """Plan §6 tests 4, 5, and 6 - the phantom-revoke guards. Each of these is
    constructed so it would FAIL against a naive absolute-state diff (anything
    currently held that isn't in the POST gets revoked):

    - test 4 constructs "held and pending" directly in the DB (bypassing any
      one application code path that happens to auto-close it), and asserts
      the reconciliation still resolves it AND that the render doesn't drop the
      committee from the page at all - a naive D6 that hides ANY pending item
      (regardless of held status) would omit it from committeeRows entirely,
      failing the very first assertion.
    - test 5 relies on an expired owner never entering the rendered universe -
      a naive diff that revokes "anything held but not submitted" would strip
      it the moment the member submits anything else.
    - test 6 submits exactly what a stale tab would have submitted (the
      permission was neither checked nor in renderedChecked, because it wasn't
      held yet when that tab loaded) - a naive diff has no renderedChecked
      concept and revokes on "held now but not in this POST" alone.
    """

    def _post(self, committees=(), permissionsList=(), renderedChecked=(), justification="unrelated change"):
        return self.client.post(
            reverse("request-access"),
            {
                "justification": justification,
                "committees": list(committees),
                "permissions": list(permissionsList),
                "renderedChecked": list(renderedChecked),
            },
            follow=True,
        )

    def test_held_and_pending_owner_renders_checked_and_survives_unrelated_submit(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        otherOwner = EventOwners.objects.create(
            name="Sample Working Group", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("heldandpending")
        owner.authorizers.add(member)  # held
        pending = AccessRequests.objects.create(
            requester=member, owner=owner, justification="already asked once",
            status=AccessRequests.Status.REQUESTED,
        )

        self.loginAs(member)
        getResp = self.client.get(reverse("request-access"))
        rows = {row["owner"].id: row for row in getResp.context["committeeRows"]}
        self.assertIn(owner.id, rows)  # a naive "hide any pending" rule would drop it entirely
        self.assertTrue(rows[owner.id]["checked"])
        pending.refresh_from_db()
        self.assertEqual(pending.status, AccessRequests.Status.APPROVED)  # D6 reconciliation

        # Submit an unrelated change (asking to join otherOwner) while owner
        # stays checked, exactly as a real browser would resubmit it.
        resp = self._post(
            committees=[owner.id, otherOwner.id],
            renderedChecked=[f"o:{owner.id}"],
            justification="joining another committee too",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(member, owner.authorizers.all())
        self.assertTrue(
            AccessRequests.objects.filter(
                requester=member, owner=otherOwner, status=AccessRequests.Status.REQUESTED,
            ).exists()
        )

    def test_expired_owner_authorization_untouched_by_unrelated_submit(self):
        expiredOwner = EventOwners.objects.create(
            name="Example Committee", isPermanent=False,
            expiration=datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
        )
        member = UserFactory.make("expiredauthorizer")
        expiredOwner.authorizers.add(member)
        self.loginAs(member)

        getResp = self.client.get(reverse("request-access"))
        ownerIds = {row["owner"].id for row in getResp.context["committeeRows"]}
        self.assertNotIn(expiredOwner.id, ownerIds)  # never rendered - expired, out of universe

        perm = permission("manageLinkTree")
        resp = self._post(permissionsList=[perm.id], justification="need it for link duty")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(member, expiredOwner.authorizers.all())

    def test_grant_between_get_and_post_not_revoked_by_stale_submit(self):
        member = UserFactory.make("staletab")
        perm = permission("manageLinkTree")
        self.loginAs(member)

        getResp = self.client.get(reverse("request-access"))
        rows = {
            row["permission"].id: row
            for section in getResp.context["permissionSections"]
            for row in section["rows"]
        }
        self.assertFalse(rows[perm.id]["checked"])  # not held yet at render time

        # Something grants it between GET and POST (an admin, or a separate
        # approval landing in another tab).
        member.user_permissions.add(perm)

        # The stale tab submits exactly what IT rendered: perm was neither
        # checked nor part of that render's renderedChecked snapshot.
        resp = self._post(permissionsList=[], renderedChecked=[])

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(member.user_permissions.filter(id=perm.id).exists())


@fastHashing
class SelfServiceForgedAdditionTests(LoginClientMixin, TestCase):
    """Plan §6 test 7: checking a box for something already held (whether
    genuinely re-rendered that way, or forged) must never create a duplicate
    AccessRequests row - the additions formula subtracts everything currently
    held (via has_perm, so group-held counts too), not just what's new."""

    def test_forged_post_checking_already_held_permission_creates_no_request(self):
        member = UserFactory.make("alreadyheld", perms=("manageLinkTree",))
        perm = permission("manageLinkTree")
        self.loginAs(member)

        resp = self.client.post(
            reverse("request-access"),
            {
                "justification": "",
                "committees": [],
                "permissions": [perm.id],
                "renderedChecked": [],  # forged: pretend it was never rendered checked
            },
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccessRequests.objects.count(), 0)


@fastHashing
class SelfServiceBatchGrantTests(LoginClientMixin, TestCase):
    """The grant half's batching (plan §6 tests 8, 9, and 10): additions fan
    out to one AccessRequests row each sharing a single batchId (D3, forced by
    AccessRequests.clean()'s one-target-per-row rule and the per-target
    reviewer tiers), and a race that makes one addition stale doesn't block
    the rest of the batch (D6)."""

    def test_three_additions_share_one_batch_id_with_the_same_justification(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        owner.authorizers.add(UserFactory.make("existingauth1"))
        perm1 = permission("manageLinkTree")
        perm2 = permission("viewLinkMetrics")
        member = UserFactory.make("batchjoiner")
        self.loginAs(member)

        resp = self.client.post(
            reverse("request-access"),
            {
                "justification": "need all three for committee work",
                "committees": [owner.id],
                "permissions": [perm1.id, perm2.id],
                "renderedChecked": [],
            },
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        rows = list(AccessRequests.objects.filter(requester=member))
        self.assertEqual(len(rows), 3)
        batchIds = {row.batchId for row in rows}
        self.assertEqual(len(batchIds), 1)
        self.assertIsNotNone(next(iter(batchIds)))
        for row in rows:
            self.assertEqual(row.justification, "need all three for committee work")

    def test_owner_row_peer_reviewable_permission_row_admin_only_in_same_batch(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        authorizer = UserFactory.make("peerauthorizer")
        owner.authorizers.add(authorizer)
        perm = permission("manageLinkTree")
        member = UserFactory.make("batchsubmitter")
        self.loginAs(member)

        self.client.post(
            reverse("request-access"),
            {
                "justification": "x",
                "committees": [owner.id],
                "permissions": [perm.id],
                "renderedChecked": [],
            },
            follow=True,
        )

        ownerRow = AccessRequests.objects.get(requester=member, owner=owner)
        permRow = AccessRequests.objects.get(requester=member, permission=perm)
        self.assertEqual(ownerRow.batchId, permRow.batchId)
        self.assertTrue(ownerRow.canBeReviewedBy(authorizer))
        self.assertFalse(permRow.canBeReviewedBy(authorizer))

    def test_one_stale_addition_target_does_not_block_the_rest(self):
        staleOwner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        staleOwner.authorizers.add(UserFactory.make("staleexisting"))
        perm = permission("manageLinkTree")
        member = UserFactory.make("stalesubmitter")
        self.loginAs(member)

        staleOwnerId = staleOwner.id
        realGet = EventOwners.objects.get

        def flakyGet(*args, **kwargs):
            if str(kwargs.get("id")) == str(staleOwnerId):
                raise EventOwners.DoesNotExist("simulated race - deleted mid-submission")
            return realGet(*args, **kwargs)

        with mock.patch.object(EventOwners.objects, "get", side_effect=flakyGet):
            resp = self.client.post(
                reverse("request-access"),
                {
                    "justification": "need both",
                    "committees": [staleOwner.id],
                    "permissions": [perm.id],
                    "renderedChecked": [],
                },
                follow=True,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccessRequests.objects.filter(requester=member).count(), 1)
        self.assertTrue(AccessRequests.objects.filter(requester=member, permission=perm).exists())
        self.assertFalse(AccessRequests.objects.filter(requester=member, owner=staleOwner).exists())
        self.assertEqual(resp.context["result"]["droppedCount"], 1)


@fastHashing
class SelfServiceBatchEmailTests(LoginClientMixin, TestCase):
    """Plan §6 test 11 - D7's grouped, per-approver, per-recipient-wrapped send.
    An owner-authorizer who cannot review a permission-target row must never
    see it in their email (the leak-free inversion), and a bad address on one
    approver's row must not suppress a different approver's email."""

    def test_each_approver_gets_one_email_with_only_their_items_bad_address_does_not_suppress_others(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        ownerAuthorizer = UserFactory.make("emailauthorizer", email="authorizer@example.com")
        owner.authorizers.add(ownerAuthorizer)
        UserFactory.make("emailadmin", perms=("approveAccessRequest",), email="bad-address-example")
        perm = permission("manageLinkTree")
        member = UserFactory.make("emailmember", email="member@example.com")
        self.loginAs(member)

        from django.core.mail import send_mail as realSendMail

        def flakySendMail(*args, **kwargs):
            if "bad-address-example" in kwargs.get("recipient_list", []):
                raise Exception("smtp rejected bad address")
            return realSendMail(*args, **kwargs)

        with mock.patch("tools.accessViews.send_mail", side_effect=flakySendMail):
            resp = self.client.post(
                reverse("request-access"),
                {
                    "justification": "need both",
                    "committees": [owner.id],
                    "permissions": [perm.id],
                    "renderedChecked": [],
                },
                follow=True,
            )

        self.assertEqual(resp.status_code, 200)
        # The admin's send failed - nothing for that address landed in the outbox.
        self.assertFalse(any("bad-address-example" in m.to for m in mail.outbox))
        # The owner-authorizer's send must still have gone through exactly once,
        # naming only the item they can decide.
        authorizerMails = [m for m in mail.outbox if "authorizer@example.com" in m.to]
        self.assertEqual(len(authorizerMails), 1)
        self.assertIn(owner.name, authorizerMails[0].body)
        self.assertNotIn(perm.name, authorizerMails[0].body)
        # The requester's own confirmation still goes out too.
        self.assertTrue(any("member@example.com" in m.to for m in mail.outbox))


@fastHashing
class RevokeCommitteeMembershipTests(MailAssertionsMixin, LoginClientMixin, TestCase):
    """revokeCommitteeMembership (tools/models.py) and its two callers - the
    plan's D5: 'Event Leads' is a MANAGED/DERIVED group as of the 2026-08-11
    decision (PLAN.md section 9). Membership means "authorizes at least one
    EventOwner", full stop, regardless of how the membership was acquired.

    Uses invented committee/campaign names ("Example Committee", "Sample
    Working Group") rather than the real Austin DSA formation names some
    pre-existing fixtures elsewhere in this file use - that precedent predates
    this test class and is not one to follow.
    """

    def _grantEventLeadRole(self, user):
        """Add a user to Event Leads the way an admin hand-grant would (Manage
        Member Access / Manage Groups / admin), i.e. NOT through grantTo and
        with no backing AccessRequests row. Used to build scenarios whose
        point is that revokeCommitteeMembership doesn't care how the group was
        acquired."""
        group, _ = Group.objects.get_or_create(name=EVENT_LEAD_ROLE_GROUP)
        user.groups.add(group)
        return group

    # --- the reconciler itself (called directly) ----------------------------

    def test_leaving_only_committee_removes_authorizer_and_event_leads_group(self):
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("onlycommittee")
        owner.authorizers.add(member)
        self._grantEventLeadRole(member)

        groupRemoved = revokeCommitteeMembership(member, owner)

        self.assertTrue(groupRemoved)
        self.assertNotIn(member, owner.authorizers.all())
        self.assertFalse(member.groups.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    def test_leaving_one_of_two_committees_keeps_event_leads(self):
        ownerA = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        ownerB = EventOwners.objects.create(
            name="Sample Working Group", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("twocommittees")
        ownerA.authorizers.add(member)
        ownerB.authorizers.add(member)
        self._grantEventLeadRole(member)

        groupRemoved = revokeCommitteeMembership(member, ownerA)

        self.assertFalse(groupRemoved)
        self.assertNotIn(member, ownerA.authorizers.all())
        self.assertIn(member, ownerB.authorizers.all())
        self.assertTrue(member.groups.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    def test_revoke_is_noop_when_event_leads_group_does_not_exist(self):
        # A fresh install (or one where nobody has ever joined a committee)
        # has no "Event Leads" row - get_or_create in _grantEventLeadRole /
        # grantTo is what would normally create it, and neither ran here.
        self.assertFalse(Group.objects.filter(name=EVENT_LEAD_ROLE_GROUP).exists())
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("noroleyet")
        owner.authorizers.add(member)

        groupRemoved = revokeCommitteeMembership(member, owner)  # must not raise

        self.assertFalse(groupRemoved)
        self.assertNotIn(member, owner.authorizers.all())
        self.assertFalse(Group.objects.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    def test_hand_granted_event_leads_with_no_request_provenance_loses_group_on_next_leave(self):
        """This is the D5/Option-1 tradeoff, decided deliberately - not a bug.

        Event Leads is fully derived from eventAuthorizations, with NO check on
        how the group was acquired. A member can be added to it directly today
        (Manage Member Access's target.groups.set(), Manage Groups'
        group.user_set.add(), or /admin/'s group member widget) without ever
        going through grantTo or creating an AccessRequests row. The very next
        time that member leaves a committee, revokeCommitteeMembership
        reconciles the group away even though nothing here ever granted it via
        an approved request.

        Cam chose this (2026-08-11, PLAN.md section 9, "Option 1") over the
        rejected alternative of gating removal on AccessRequests provenance
        (only drop the group when an APPROVED owner-target AccessRequests row
        exists for the user). Do NOT "fix" this test by adding a provenance
        check to revokeCommitteeMembership - that reintroduces the rejected
        design and the two competing meanings for one group it was rejected
        for causing.
        """
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("handgranted")
        owner.authorizers.add(member)  # a committee membership grantTo never touched
        self._grantEventLeadRole(member)  # a group grant grantTo never touched either
        self.assertFalse(
            AccessRequests.objects.filter(
                requester=member, owner__isnull=False,
                status=AccessRequests.Status.APPROVED,
            ).exists(),
            "setup check: this member must have no approved owner-target request",
        )

        groupRemoved = revokeCommitteeMembership(member, owner)

        self.assertTrue(groupRemoved)
        self.assertFalse(member.groups.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    # --- the same rule enforced through the admin owner-edit page -----------

    def test_admin_owner_edit_leaving_only_committee_removes_group_too(self):
        admin = UserFactory.make("ownermgr1", perms=("manageEventOwners",))
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("adminonlycommittee")
        owner.authorizers.add(member)
        self._grantEventLeadRole(member)

        self.loginAs(admin)
        resp = self.client.post(
            reverse("manage-event-owner", kwargs={"ownerId": owner.id}),
            {
                "ownerName": owner.name,
                "ownerIsPermanent": "on",
                "removeAuthorizers": [member.id],
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(member, owner.authorizers.all())
        self.assertFalse(member.groups.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    def test_admin_owner_edit_leaving_one_of_two_keeps_group(self):
        admin = UserFactory.make("ownermgr2", perms=("manageEventOwners",))
        ownerA = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        ownerB = EventOwners.objects.create(
            name="Sample Working Group", isPermanent=True, expiration=FAR_FUTURE,
        )
        member = UserFactory.make("admintwocommittees")
        ownerA.authorizers.add(member)
        ownerB.authorizers.add(member)
        self._grantEventLeadRole(member)

        self.loginAs(admin)
        resp = self.client.post(
            reverse("manage-event-owner", kwargs={"ownerId": ownerA.id}),
            {
                "ownerName": ownerA.name,
                "ownerIsPermanent": "on",
                "removeAuthorizers": [member.id],
            },
        )

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(member, ownerA.authorizers.all())
        self.assertIn(member, ownerB.authorizers.all())
        self.assertTrue(member.groups.filter(name=EVENT_LEAD_ROLE_GROUP).exists())

    # --- the owner-target auto-close root cause (D6's last paragraph) -------

    def test_admin_adding_authorizer_auto_closes_pending_owner_request(self):
        admin = UserFactory.make("ownermgr3", perms=("manageEventOwners",))
        owner = EventOwners.objects.create(
            name="Example Committee", isPermanent=True, expiration=FAR_FUTURE,
        )
        existingAuthorizer = UserFactory.make("existingauthorizer")
        owner.authorizers.add(existingAuthorizer)
        requester = UserFactory.make("pendingjoiner")
        pending = AccessRequests.objects.create(
            requester=requester, owner=owner,
            justification="I want to help organize this committee.",
            status=AccessRequests.Status.REQUESTED,
        )

        self.loginAs(admin)
        resp = self.client.post(
            reverse("manage-event-owner", kwargs={"ownerId": owner.id}),
            {
                "ownerName": owner.name,
                "ownerIsPermanent": "on",
                "addAuthorizers": [requester.id],
            },
        )

        self.assertEqual(resp.status_code, 200)
        pending.refresh_from_db()
        self.assertEqual(pending.status, AccessRequests.Status.APPROVED)
        self.assertEqual(pending.reviewer, admin)
        self.assertEqual(pending.reason, "Access granted directly")
        self.assertIsNotNone(pending.dateReviewed)
        self.assertIn(requester, owner.authorizers.all())
        self.assertEmailedTo(requester.email)
