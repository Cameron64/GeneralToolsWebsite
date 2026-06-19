"""Tests for the resolution submission + signature sign-on feature.

The AN MIG check is always exercised through a validator (the mock by default;
a fake client for the live-gate test), so nothing here touches the network.
"""
import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from tools.models import Resolution, ResolutionSignature, PostedEvents
from tools.resolutionText import normalizedTextHash, renderMarkdown
from tools.ActionNetworkAPI import ActionNetworkAPI
from tools.ActionNetworkAPI.migValidator import (
    MIGStatus, MockANMIGValidator, LiveANMIGValidator,
)
from tools.tests.support.factories import UserFactory


def _futureMeeting(daysAhead=40, title="July 2026 GBM"):
    now = datetime.datetime.now(datetime.UTC)
    start = now + datetime.timedelta(days=daysAhead)
    return PostedEvents.objects.create(
        title=title, start=start, end=start + datetime.timedelta(hours=2),
        timezone="America/Chicago", locationName="", streetAddress="", city="",
        state="", zip="", country="", description="", instructions="",
        dateCreated=now, datePublished=now, anManageLink="", anShareLink="",
        gCalLink="", zoomLink="", zoomAccount="", reason="",
    )


# --- safe markdown renderer ------------------------------------------------

class MarkdownRendererTests(TestCase):
    def test_basic_formatting(self):
        html = renderMarkdown("# Title\n\nSome **bold** and _italic_ text.\n\n- one\n- two")
        self.assertIn("<h2>Title</h2>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<li>one</li>", html)

    def test_escapes_raw_html_xss(self):
        html = renderMarkdown("<script>alert('x')</script>\n\nhi")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_drops_unsafe_link_scheme(self):
        html = renderMarkdown("[click](javascript:alert(1))")
        self.assertNotIn("javascript:", html)
        self.assertIn("click", html)

    def test_keeps_safe_link(self):
        html = renderMarkdown("[BRT](https://example.org/brt)")
        self.assertIn('href="https://example.org/brt"', html)

    def test_empty(self):
        self.assertEqual(renderMarkdown(""), "")


class NormalizedHashTests(TestCase):
    def test_crlf_and_whitespace_are_noops(self):
        a = normalizedTextHash("Whereas this\nResolved that\n")
        b = normalizedTextHash("  Whereas this\r\nResolved that  ")
        self.assertEqual(a, b)

    def test_real_change_differs(self):
        self.assertNotEqual(normalizedTextHash("one"), normalizedTextHash("two"))


# --- model -----------------------------------------------------------------

class ResolutionModelTests(TestCase):
    def setUp(self):
        self.member = UserFactory.make("proponent")

    def _make(self, kind=Resolution.Kind.PROJECT_COMMITTEE, text="Body"):
        return Resolution.objects.create(
            title="T", kind=kind, text=text, proponent=self.member,
        )

    def test_threshold_and_lead_days_by_kind(self):
        self.assertIsNone(self._make(Resolution.Kind.GENERAL).threshold)
        self.assertEqual(self._make(Resolution.Kind.PROJECT_COMMITTEE).threshold, 25)
        self.assertEqual(self._make(Resolution.Kind.BYLAWS_AMENDMENT).threshold, 35)
        self.assertEqual(self._make(Resolution.Kind.GENERAL).leadDays, 0)
        self.assertEqual(self._make(Resolution.Kind.PROJECT_COMMITTEE).leadDays, 10)
        self.assertEqual(self._make(Resolution.Kind.BYLAWS_AMENDMENT).leadDays, 21)

    def test_general_meets_threshold_with_no_signatures(self):
        self.assertTrue(self._make(Resolution.Kind.GENERAL).meetsThreshold)

    def test_only_verified_signatures_count(self):
        res = self._make()
        signer1 = UserFactory.make("s1")
        signer2 = UserFactory.make("s2")
        ResolutionSignature.objects.create(
            resolution=res, member=signer1, textHashAtSigning="h",
            verified=True, verificationStatus=MIGStatus.OK,
            checkedAt=datetime.datetime.now(datetime.UTC),
        )
        ResolutionSignature.objects.create(
            resolution=res, member=signer2, textHashAtSigning="h",
            verified=False, verificationStatus=MIGStatus.EXPIRED,
            checkedAt=datetime.datetime.now(datetime.UTC),
        )
        self.assertEqual(res.signatureCount, 1)

    def test_lock_text_is_idempotent(self):
        res = self._make(text="Final text")
        res.lockText()
        self.assertTrue(res.locked)
        self.assertEqual(res.lockedTextHash, normalizedTextHash("Final text"))
        firstLockedAt = res.lockedAt
        res.lockText()
        self.assertEqual(res.lockedAt, firstLockedAt)

    def test_replace_text_resets_signatures_on_real_change(self):
        res = self._make(text="Original")
        signer = UserFactory.make("s1")
        ResolutionSignature.objects.create(
            resolution=res, member=signer, textHashAtSigning="h",
            verified=True, verificationStatus=MIGStatus.OK,
            checkedAt=datetime.datetime.now(datetime.UTC),
        )
        res.lockText()
        didReset = res.replaceText("Completely different text")
        self.assertTrue(didReset)
        self.assertFalse(res.locked)
        self.assertEqual(res.signatures.count(), 0)

    def test_replace_text_noop_resave_preserves_signatures(self):
        res = self._make(text="Original body")
        signer = UserFactory.make("s1")
        ResolutionSignature.objects.create(
            resolution=res, member=signer, textHashAtSigning="h",
            verified=True, verificationStatus=MIGStatus.OK,
            checkedAt=datetime.datetime.now(datetime.UTC),
        )
        res.lockText()
        # Same text but Windows line endings + trailing whitespace.
        didReset = res.replaceText("  Original body\r\n")
        self.assertFalse(didReset)
        self.assertTrue(res.locked)
        self.assertEqual(res.signatures.count(), 1)

    def test_deadline_null_guard(self):
        res = self._make()  # no targetMeeting
        self.assertIsNone(res.deadline())
        self.assertIsNone(res.deadlineMet())

    def test_deadline_computed_from_meeting(self):
        meeting = _futureMeeting(daysAhead=40)
        res = Resolution.objects.create(
            title="T", kind=Resolution.Kind.BYLAWS_AMENDMENT, text="x",
            proponent=self.member, targetMeeting=meeting,
        )
        expected = meeting.start - datetime.timedelta(days=21)
        self.assertEqual(res.deadline(), expected)
        self.assertTrue(res.deadlineMet())  # filed now, 40 days out, 21-day lead


# --- validators ------------------------------------------------------------

class _FakeClient:
    """Stands in for ActionNetworkAPI; returns a canned (status, person)."""
    def __init__(self, status, person):
        self._status = status
        self._person = person

    def getPersonForVoteValidation(self, email):
        return (self._status, self._person)


def _person(memberStatus=True, chapter="austin", expired=False):
    today = datetime.date.today()
    return ActionNetworkAPI.PersonInfoForVoteValidation(
        chapter=chapter, memberStatus=memberStatus,
        expireDate=today - datetime.timedelta(days=1) if expired else today + datetime.timedelta(days=300),
        joinDate=today - datetime.timedelta(days=400),
    )


class LiveValidatorGateTests(TestCase):
    def _verify(self, status, person):
        validator = LiveANMIGValidator("tok")
        validator._client = _FakeClient(status, person)  # bypass network init
        return validator.verify("a@b.org")

    def test_mig_passes(self):
        result = self._verify(ActionNetworkAPI.GetPersonAPIReturnStatus.SUCCESS, _person(True))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, MIGStatus.OK)

    def test_wrong_chapter(self):
        result = self._verify(ActionNetworkAPI.GetPersonAPIReturnStatus.SUCCESS, _person(False, chapter="dallas"))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, MIGStatus.WRONG_CHAPTER)

    def test_expired(self):
        result = self._verify(ActionNetworkAPI.GetPersonAPIReturnStatus.SUCCESS, _person(False, expired=True))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, MIGStatus.EXPIRED)

    def test_not_found(self):
        result = self._verify(ActionNetworkAPI.GetPersonAPIReturnStatus.NOT_FOUND, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, MIGStatus.NOT_FOUND)

    def test_client_exception_fails_closed(self):
        validator = LiveANMIGValidator("tok")

        class Boom:
            def getPersonForVoteValidation(self, email):
                raise RuntimeError("network down")
        validator._client = Boom()
        result = validator.verify("a@b.org")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, MIGStatus.API_ERROR)


# --- views -----------------------------------------------------------------

class _ViewBase(TestCase):
    def setUp(self):
        self.member = UserFactory.make("member")
        self.secretary = UserFactory.make("sec", perms=("administerResolutions",))

    def _make(self, kind=Resolution.Kind.PROJECT_COMMITTEE, text="Body text", proponent=None):
        return Resolution.objects.create(
            title="A Resolution", kind=kind, text=text,
            proponent=proponent or self.member,
        )


class AuthGateTests(_ViewBase):
    def test_anonymous_redirected(self):
        res = self._make()
        for url in [
            reverse("submit-resolution"),
            reverse("sign-resolution"),
            reverse("resolution-status"),
            reverse("resolution-detail", kwargs={"pk": res.pk}),
            reverse("resolution-edit", kwargs={"pk": res.pk}),
        ]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("/accounts/login", resp.url, url)


class SubmitTests(_ViewBase):
    def test_submit_persists_and_redirects(self):
        self.client.force_login(self.member)
        resp = self.client.post(reverse("submit-resolution"), {
            "title": "Endorse the BRT plan",
            "kind": Resolution.Kind.GENERAL,
            "text": "Whereas...\n\nResolved...",
        })
        self.assertEqual(resp.status_code, 302)
        res = Resolution.objects.get(title="Endorse the BRT plan")
        self.assertEqual(res.proponent, self.member)
        self.assertEqual(res.kind, Resolution.Kind.GENERAL)
        self.assertEqual(resp.url, reverse("resolution-detail", kwargs={"pk": res.pk}))

    def test_submit_invalid_kind_rerenders(self):
        self.client.force_login(self.member)
        resp = self.client.post(reverse("submit-resolution"), {
            "title": "X", "kind": "NONSENSE", "text": "body",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Resolution.objects.filter(title="X").exists())


class SignOnTests(_ViewBase):
    def _signWith(self, validator, res, user):
        with mock.patch("tools.resolutionViews.getMIGValidator", return_value=validator):
            return self.client.post(reverse("resolution-detail", kwargs={"pk": res.pk}), {"affirm": "on"})

    def test_sign_on_happy_path_records_and_locks(self):
        res = self._make(text="Lock me")
        signer = UserFactory.make("signer1")
        self.client.force_login(signer)
        resp = self._signWith(MockANMIGValidator(), res, signer)
        self.assertEqual(resp.status_code, 200)
        res.refresh_from_db()
        self.assertEqual(res.signatureCount, 1)
        self.assertTrue(res.locked)
        sig = ResolutionSignature.objects.get(resolution=res, member=signer)
        self.assertTrue(sig.verified)
        self.assertEqual(sig.verificationStatus, MIGStatus.OK)
        self.assertEqual(sig.textHashAtSigning, normalizedTextHash("Lock me"))

    def test_failed_check_does_not_record(self):
        res = self._make()
        signer = UserFactory.make("signer2")
        self.client.force_login(signer)
        validator = MockANMIGValidator(defaultStatus=MIGStatus.EXPIRED)
        resp = self._signWith(validator, res, signer)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ResolutionSignature.objects.filter(resolution=res, member=signer).exists())
        res.refresh_from_db()
        self.assertFalse(res.locked)

    def test_api_error_fails_closed(self):
        res = self._make()
        signer = UserFactory.make("signer3")
        self.client.force_login(signer)
        validator = MockANMIGValidator(defaultStatus=MIGStatus.API_ERROR)
        self._signWith(validator, res, signer)
        self.assertFalse(ResolutionSignature.objects.filter(resolution=res, member=signer).exists())

    def test_duplicate_sign_on_blocked(self):
        res = self._make()
        signer = UserFactory.make("signer4")
        self.client.force_login(signer)
        self._signWith(MockANMIGValidator(), res, signer)
        self._signWith(MockANMIGValidator(), res, signer)
        self.assertEqual(
            ResolutionSignature.objects.filter(resolution=res, member=signer).count(), 1
        )


class EditResetTests(_ViewBase):
    def test_edit_after_lock_requires_confirm_then_resets(self):
        res = self._make(text="Original")
        signer = UserFactory.make("signer5")
        ResolutionSignature.objects.create(
            resolution=res, member=signer, textHashAtSigning=normalizedTextHash("Original"),
            verified=True, verificationStatus=MIGStatus.OK,
            checkedAt=datetime.datetime.now(datetime.UTC),
        )
        res.lockText()
        self.client.force_login(self.member)  # the proponent
        url = reverse("resolution-edit", kwargs={"pk": res.pk})

        # Without confirm: re-renders, no change applied.
        resp = self.client.post(url, {"text": "New wording entirely"})
        self.assertEqual(resp.status_code, 200)
        res.refresh_from_db()
        self.assertEqual(res.signatureCount, 1)
        self.assertTrue(res.locked)

        # With confirm: applies + resets sign-ons.
        resp = self.client.post(url, {"text": "New wording entirely", "confirmReset": "on"})
        self.assertEqual(resp.status_code, 302)
        res.refresh_from_db()
        self.assertEqual(res.signatureCount, 0)
        self.assertFalse(res.locked)

    def test_non_proponent_cannot_edit(self):
        res = self._make(proponent=self.member)
        other = UserFactory.make("interloper")
        self.client.force_login(other)
        resp = self.client.get(reverse("resolution-edit", kwargs={"pk": res.pk}))
        self.assertEqual(resp.status_code, 403)


class DashboardTests(_ViewBase):
    def test_dashboard_requires_permission(self):
        self._make()
        self.client.force_login(self.member)  # no administerResolutions
        resp = self.client.get(reverse("resolution-status"))
        self.assertEqual(resp.status_code, 302)  # permission_required redirects

    def test_dashboard_visible_to_secretary(self):
        self._make(kind=Resolution.Kind.GENERAL)
        self.client.force_login(self.secretary)
        resp = self.client.get(reverse("resolution-status"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "In flight")
        self.assertContains(resp, "n/a")  # general resolution shows n/a for sign-ons
