"""Chapter Tools round 4: a typed role, a shorter credential form, a clearable date.

All data here is invented - never real chapter services, credentials, or people.

Three changes with one thing in common: each removes a control that was asking
the editor a question the registry should not have been asking.

  * The holder's role was a five-rung dropdown of generic words. No service names
    its tiers those words, so every entry was a translation and the translation
    lost what the editor actually knew. It is free text now, and the two facts the
    register has to READ ACROSS rows moved into their own booleans.
  * The credential form asked for a vault collection, a status and two dates even
    for an individual login or a 2FA token, which are one person's own and are
    never vaulted or rotated. Four permanently-unanswerable questions.
  * "Last reviewed" could be set and not un-set, because Chrome's date input has
    no clear affordance. An optional field that is one-way strands a claim about
    reality that somebody has since found to be wrong.

The assertions to keep honest are the negative ones - the fields that must NOT
appear, and the values that must NOT survive a kind change. "The field renders"
was true before every one of these changes.
"""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as djangoTimezone

from tools import forms
from tools.models import ChapterResource, ResourceCredential, ResourceHolder
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing
from tools.tests.test_chapter_tools import _makeResource, _restrictedPayload

CREDENTIAL_SENTINEL = "ExampleCredentialSentinel"


def _credentialPayload(**overrides):
    """A complete credential POST body, as the browser sends it."""
    payload = {
        "label": CREDENTIAL_SENTINEL,
        "kind": str(ResourceCredential.Kind.VAULT_SHARED_LOGIN),
        "vaultCollection": "example-collection",
        "status": str(ResourceCredential.Status.LIVE),
        "addedAt": "2024-01-05",
        "lastRotated": "2024-02-01",
        "note": "",
    }
    payload.update(overrides)
    return payload


class CredentialKindSuppressionTests(TestCase):
    """An individual login and a 2FA token are one person's own.

    Everything here is really one assertion: the four suppressed fields must be
    unanswerable, not merely un-asked. A form that hides a control in the browser
    and still saves whatever was posted into it is the worse of the two states -
    the page and the database disagree, and nothing on screen says which won.
    """

    def test_the_two_kinds_are_the_ones_that_do_not_rotate(self):
        self.assertEqual(
            set(ResourceCredential.NO_ROTATION_KINDS),
            {ResourceCredential.Kind.INDIVIDUAL_LOGIN,
             ResourceCredential.Kind.TWO_FACTOR_TOKEN},
        )

    def test_a_vault_shared_login_still_asks_all_four(self):
        """The control case. Without it, a bug that suppressed every kind would
        pass every other test in this class."""
        form = forms.ResourceCredentialForm(data=_credentialPayload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["vaultCollection"], "example-collection")
        self.assertIsNotNone(form.cleaned_data["lastRotated"])

    def test_an_individual_login_saves_none_of_the_four(self):
        form = forms.ResourceCredentialForm(data=_credentialPayload(
            kind=str(ResourceCredential.Kind.INDIVIDUAL_LOGIN),
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["vaultCollection"], "")
        self.assertIsNone(form.cleaned_data["addedAt"])
        self.assertIsNone(form.cleaned_data["lastRotated"])
        # LIVE, not None: the column is NOT NULL and has a meaningful default, so
        # blanking it to None would fail at the database rather than at the form.
        self.assertEqual(form.cleaned_data["status"], ResourceCredential.Status.LIVE)

    def test_a_2fa_token_saves_none_of_the_four(self):
        form = forms.ResourceCredentialForm(data=_credentialPayload(
            kind=str(ResourceCredential.Kind.TWO_FACTOR_TOKEN),
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["vaultCollection"], "")
        self.assertIsNone(form.cleaned_data["addedAt"])
        self.assertIsNone(form.cleaned_data["lastRotated"])

    def test_a_bad_date_order_does_not_block_a_kind_that_ignores_dates(self):
        """The suppression runs BEFORE the date-order rule. Otherwise an editor
        switching a vault login to an individual login could be rejected over two
        dates the form is no longer asking for and they can no longer see."""
        form = forms.ResourceCredentialForm(data=_credentialPayload(
            kind=str(ResourceCredential.Kind.INDIVIDUAL_LOGIN),
            addedAt="2024-06-01", lastRotated="2024-01-01",
        ))
        self.assertTrue(form.is_valid(), form.errors)

    def test_an_unparseable_value_in_a_suppressed_field_is_discarded_not_raised(self):
        """A field this kind does not ask for must not be able to block the save.
        The per-field clean still runs and still errors, so clean() has to drop
        that error along with the value - otherwise a scriptless browser posting
        a half-typed date on a field the editor cannot see makes the form
        permanently unsaveable, with the error pointing at nothing on screen."""
        form = forms.ResourceCredentialForm(data=_credentialPayload(
            kind=str(ResourceCredential.Kind.TWO_FACTOR_TOKEN),
            addedAt="not-a-date",
        ))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["addedAt"])

    def test_the_same_unparseable_value_still_errors_on_a_kind_that_asks(self):
        form = forms.ResourceCredentialForm(data=_credentialPayload(addedAt="not-a-date"))
        self.assertFalse(form.is_valid())
        self.assertIn("addedAt", form.errors)

    def test_the_date_order_rule_still_applies_to_a_kind_that_uses_dates(self):
        form = forms.ResourceCredentialForm(data=_credentialPayload(
            addedAt="2024-06-01", lastRotated="2024-01-01",
        ))
        self.assertFalse(form.is_valid())
        self.assertIn("rotated before it existed", str(form.errors))

    def test_the_kind_select_tells_the_browser_the_same_rule_the_server_enforces(self):
        """The two data- attributes are rendered from the model constant and the
        form constant. If they were written into a template by hand, the browser
        could come to hold a different opinion than clean() about which kinds
        suppress which fields, and the disagreement would be invisible."""
        rendered = str(forms.ResourceCredentialForm()["kind"])
        for kind in ResourceCredential.NO_ROTATION_KINDS:
            self.assertIn(str(kind), rendered)
        for key in forms.ResourceCredentialForm.SUPPRESSED_BY_KIND_KEYS:
            self.assertIn(key, rendered)

    def test_a_new_form_preselects_no_kind(self):
        """Otherwise the select answers this question for the editor - with the
        first option, "Individual login", which is one of the two kinds that
        suppress four fields. Somebody opening the page to add a shared vault
        login would find those four already gone and no reason given."""
        form = forms.ResourceCredentialForm()
        self.assertIsNone(form["kind"].value())
        rendered = str(form["kind"])
        # The BLANK option is the selected one, which is the whole point - not
        # "nothing is selected", which a <select> cannot express.
        self.assertIn('<option value="" selected>', rendered)
        self.assertNotIn('value="0" selected', rendered)

    def test_submitting_without_choosing_a_kind_is_an_error_not_a_default(self):
        """Unreachable before this round: with no blank option a select always
        posts a real value, so the empty case could not occur and the message
        written for it was dead text."""
        form = forms.ResourceCredentialForm(data=_credentialPayload(kind=""))
        self.assertFalse(form.is_valid())
        self.assertIn("Pick what kind", str(form.errors))

    def test_nothing_is_suppressed_until_a_kind_is_chosen(self):
        """The browser reads the same rule: an unset select matches no suppressed
        value, so all four fields stay on the page until the editor decides."""
        rendered = str(forms.ResourceCredentialForm()["kind"])
        self.assertIn('data-suppress-when="0,4"', rendered)
        self.assertIn('value=""', rendered)

    def test_rotation_status_is_not_applicable_not_unknown(self):
        """"No dates recorded" on an individual login reports a gap that can
        never be closed, and a register full of permanent gaps trains people to
        ignore the real ones."""
        credential = ResourceCredential.objects.create(
            resource=_makeResource(), label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.INDIVIDUAL_LOGIN,
        )
        self.assertFalse(credential.tracksRotation())
        self.assertEqual(credential.getRotationStatus(), "not-applicable")
        self.assertFalse(credential.isRotationOverdue())

    def test_an_old_individual_login_is_not_reported_as_overdue(self):
        """Even with a date on the row - a legacy value, or one written before
        the kind was corrected - the kind is what decides."""
        credential = ResourceCredential.objects.create(
            resource=_makeResource(), label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.INDIVIDUAL_LOGIN,
            lastRotated=djangoTimezone.now().date() - datetime.timedelta(days=5000),
        )
        self.assertFalse(credential.isRotationOverdue())
        self.assertEqual(credential.getRotationStatus(), "not-applicable")


@fastHashing
class CredentialKindSuppressionSaveTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.editor = UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)
        self.resource = _makeResource()

    def test_changing_kind_drops_the_dates_that_no_longer_describe_anything(self):
        """The real failure this prevents: a vault shared login with a rotation
        history is switched to an individual login, and the register keeps
        reporting rotation dates for a secret the chapter does not hold."""
        credential = ResourceCredential.objects.create(
            resource=self.resource, label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.VAULT_SHARED_LOGIN,
            vaultCollection="example-collection",
            addedAt="2024-01-05", lastRotated="2024-02-01",
        )
        self.client.post(
            reverse("chapter-tool-child-edit", kwargs={
                "pk": self.resource.pk, "childKind": "credentials", "childId": credential.pk,
            }),
            _credentialPayload(kind=str(ResourceCredential.Kind.INDIVIDUAL_LOGIN)),
        )
        credential.refresh_from_db()
        self.assertEqual(credential.kind, ResourceCredential.Kind.INDIVIDUAL_LOGIN)
        self.assertEqual(credential.vaultCollection, "")
        self.assertIsNone(credential.addedAt)
        self.assertIsNone(credential.lastRotated)

    def test_the_detail_page_says_not_tracked_rather_than_not_recorded(self):
        ResourceCredential.objects.create(
            resource=self.resource, label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.INDIVIDUAL_LOGIN,
        )
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and this section does not render at all.
        body = self.client.get(f"{self.resource.getUrl()}?tab=credentials").content.decode()
        self.assertIn("not tracked", body)
        self.assertNotIn("No dates recorded", body)

    def test_the_detail_page_still_reports_a_real_gap_on_a_shared_login(self):
        """The negative test above is only worth anything alongside this one."""
        ResourceCredential.objects.create(
            resource=self.resource, label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.VAULT_SHARED_LOGIN,
        )
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and this section does not render at all.
        body = self.client.get(f"{self.resource.getUrl()}?tab=credentials").content.decode()
        self.assertIn("No dates recorded", body)


@fastHashing
class ClearableDateTests(LoginClientMixin, TestCase):
    """"Last reviewed" has to be un-settable, not only settable.

    A review date is a claim that somebody checked this row. When that turns out
    to be wrong, the register has to be able to take the claim back - otherwise
    the only way out is to enter a different wrong date.
    """

    def setUp(self):
        self.editor = UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)

    def test_a_review_date_can_be_cleared(self):
        resource = _makeResource(lastReviewed="2026-01-05", reviewedBy="Example Reviewer")
        response = self.client.post(
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk}),
            _restrictedPayload(name=resource.name, lastReviewed=""),
        )
        self.assertEqual(response.status_code, 302)
        resource.refresh_from_db()
        self.assertIsNone(resource.lastReviewed)
        # Cleared means stale again, which is the honest reading: nobody has
        # checked this row.
        self.assertTrue(resource.isStale())

    def test_the_form_renders_a_clear_control_beside_the_date(self):
        resource = _makeResource(lastReviewed="2026-01-05")
        body = self.client.get(
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk})
        ).content.decode()
        row = body[body.find('data-field="lastReviewed"'):body.find('data-field="reviewedBy"')]
        self.assertIn("data-clearable-date", row)
        self.assertIn("data-clearable-date-clear", row)

    def test_the_clear_button_ships_hidden_so_a_scriptless_page_has_no_dead_control(self):
        resource = _makeResource(lastReviewed="2026-01-05")
        body = self.client.get(
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk})
        ).content.decode()
        button = body[body.find("data-clearable-date-clear"):]
        self.assertIn("hidden", button[:40])

    def test_the_page_loads_the_script_that_reveals_it(self):
        resource = _makeResource()
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": resource.pk}))
        self.assertContains(response, "js/clearableDate.js?v=")

    def test_the_credential_dates_are_clearable_too(self):
        """Same widget, same reason - a rotation date entered in error is a claim
        about a secret, and the register has to be able to withdraw it."""
        resource = _makeResource()
        credential = ResourceCredential.objects.create(
            resource=resource, label=CREDENTIAL_SENTINEL,
            kind=ResourceCredential.Kind.VAULT_SHARED_LOGIN,
            addedAt="2024-01-05", lastRotated="2024-02-01",
        )
        self.client.post(
            reverse("chapter-tool-child-edit", kwargs={
                "pk": resource.pk, "childKind": "credentials", "childId": credential.pk,
            }),
            _credentialPayload(addedAt="", lastRotated=""),
        )
        credential.refresh_from_db()
        self.assertIsNone(credential.addedAt)
        self.assertIsNone(credential.lastRotated)


@fastHashing
class HolderRolePickerTests(LoginClientMixin, TestCase):
    """The role is typed, with examples offered - not chosen from a fixed list."""

    def setUp(self):
        self.editor = UserFactory.make("editor", perms=("manageChapterTools",))
        self.loginAs(self.editor)
        self.resource = _makeResource()

    def _formPage(self):
        return self.client.get(reverse("chapter-tool-child-new", kwargs={
            "pk": self.resource.pk, "childKind": "holders",
        })).content.decode()

    def _row(self, body, field, nextField):
        return body[body.find(f'data-field="{field}"'):body.find(f'data-field="{nextField}"')]

    def test_the_role_is_a_text_box_not_a_dropdown(self):
        row = self._row(self._formPage(), "accessLevel", "canGrantAccess")
        self.assertNotIn("<select", row)
        self.assertIn("<input", row)

    def test_the_role_offers_examples_without_constraining_the_answer(self):
        row = self._row(self._formPage(), "accessLevel", "canGrantAccess")
        self.assertIn("<datalist", row)
        for example in ResourceHolder.ACCESS_LEVEL_EXAMPLES:
            self.assertIn(example, row)

    def test_the_datalist_is_wired_to_its_own_input(self):
        """A datalist nothing points at is invisible markup. The list id is
        derived from the field's id so two suggesting inputs cannot share one."""
        row = self._row(self._formPage(), "accessLevel", "canGrantAccess")
        self.assertIn('list="id_accessLevel-examples"', row)
        self.assertIn('id="id_accessLevel-examples"', row)

    def test_a_role_nobody_anticipated_saves(self):
        self.client.post(
            reverse("chapter-tool-child-new", kwargs={
                "pk": self.resource.pk, "childKind": "holders",
            }),
            {"personName": "Example Holder", "user": "",
             "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
             "accessLevel": "Regional coordinator (invented)",
             "confirmed": "on", "note": ""},
        )
        holder = ResourceHolder.objects.get(resource=self.resource)
        self.assertEqual(holder.accessLevel, "Regional coordinator (invented)")
        self.assertFalse(holder.isPrivileged())

    def test_the_two_power_questions_are_asked_separately_from_the_role(self):
        body = self._formPage()
        self.assertIn('data-field="canGrantAccess"', body)
        self.assertIn('data-field="ownsAccount"', body)

    def test_ticking_a_box_is_what_puts_somebody_on_the_privileged_page(self):
        self.client.post(
            reverse("chapter-tool-child-new", kwargs={
                "pk": self.resource.pk, "childKind": "holders",
            }),
            {"personName": "Example Holder", "user": "",
             "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
             "accessLevel": "Delegated user", "ownsAccount": "on",
             "confirmed": "on", "note": ""},
        )
        holder = ResourceHolder.objects.get(resource=self.resource)
        self.assertTrue(holder.ownsAccount)
        self.assertTrue(holder.isPrivileged())

    def test_the_detail_page_shows_the_typed_word_and_the_chapters_reading(self):
        """Both, because they answer different questions. The service's word is
        what an editor can check against the live account; the chapter's reading
        is what compares across tools."""
        organizer = UserFactory.make("organizer", perms=("viewResourceHolders",))
        ResourceHolder.objects.create(
            resource=self.resource, personName="Example Holder",
            accessLevel="Delegated user", ownsAccount=True, confirmed=True,
        )
        self.loginAs(organizer)
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and this section does not render at all.
        body = self.client.get(f"{self.resource.getUrl()}?tab=holders").content.decode()
        self.assertIn("Delegated user", body)
        self.assertIn("Owns it", body)


class HolderRoleIsNeverParsedTests(TestCase):
    """The one rule the whole design rests on: no surface may infer power from
    the words somebody typed."""

    def test_the_privileged_page_reads_the_booleans_not_the_word(self):
        resource = _makeResource(name="Example Trap Tool")
        # Says "Owner" and owns nothing; says nothing and owns it. A page that
        # matched on the word would get both of these backwards.
        ResourceHolder.objects.create(
            resource=resource, personName="Example Says Owner",
            accessLevel="Owner", confirmed=True,
        )
        ResourceHolder.objects.create(
            resource=resource, personName="Example Actually Owner",
            accessLevel="", ownsAccount=True, canGrantAccess=True, confirmed=True,
        )
        owners = [holder for holder in resource.holders.all() if holder.ownsAccount]
        self.assertEqual([holder.personName for holder in owners], ["Example Actually Owner"])

    def test_the_source_no_longer_has_a_ladder_to_be_tempted_by(self):
        """A leftover enum is how the old rungs come back as a lookup table
        somewhere. There must be nothing left to look up."""
        self.assertFalse(hasattr(ResourceHolder, "AccessLevel"))
        self.assertFalse(hasattr(ResourceHolder, "ACCESS_LEVEL_CHOICES"))
        self.assertFalse(hasattr(ResourceHolder, "PRIVILEGED_LEVELS"))
