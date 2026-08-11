"""Chapter Tools: the vocabulary layer and the two facts it made expressible.

All data here is invented - never real chapter services, credentials, or people.

This covers the words the registry uses that look ordinary and are not
(delegation tier, steward, review, requestable, access level), plus the two
things the model previously could not say at all: how old a credential is, and
how much power a holder actually holds.

Separate module from test_chapter_tools.py rather than appended to it: that file
is already 1500 lines covering M1's visibility split and CRUD, and these are a
different concern. The payload helpers are imported from it on purpose - they are
underscore-private, but duplicating them is how a payload that gains a required
field starts failing in one module and passing in the other.
"""
import datetime

from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from tools.chapterToolsHelp import GLOSSARY, getEntry
from tools.models import ChapterResource, ResourceCredential, ResourceHolder
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing
from tools.tests.test_chapter_tools import (
    _makeResource, _resourcePayload, _restrictedPayload,
)


class DelegationTierRuleTests(TestCase):
    """The tier-to-requestable rule. Red means "do not hand this out on your
    own", and a self-serve request button is exactly that."""

    def test_every_tier_has_a_real_explanation(self):
        """A bare colour name explains nothing, which is the whole reason the
        dict exists - so a tier added later without prose fails here instead of
        rendering as an unexplained word on the detail page."""
        for value, label in ChapterResource.DELEGATION_TIER_CHOICES:
            explanation = ChapterResource.DELEGATION_TIER_EXPLANATIONS.get(value)
            self.assertTrue(explanation, f"tier {label} has no explanation")
            self.assertGreater(len(explanation), 40, f"the {label} explanation is a stub")

    def test_red_and_unclassified_are_not_requestable(self):
        """Unclassified is defined as "treat as Red", so it must not become a way
        to get a request button without anybody making the delegation decision."""
        self.assertNotIn(ChapterResource.DelegationTier.RED, ChapterResource.REQUESTABLE_TIERS)
        self.assertNotIn(
            ChapterResource.DelegationTier.UNCLASSIFIED, ChapterResource.REQUESTABLE_TIERS,
        )

    def test_red_tier_requestable_raises(self):
        steward = UserFactory.make("steward")
        resource = _makeResource(
            requestable=True, steward=steward,
            delegationTier=ChapterResource.DelegationTier.RED,
        )
        with self.assertRaises(ValidationError):
            resource.clean()

    def test_green_and_yellow_requestable_pass(self):
        steward = UserFactory.make("steward")
        for tier in (ChapterResource.DelegationTier.GREEN, ChapterResource.DelegationTier.YELLOW):
            resource = _makeResource(
                name=f"Example Tool {tier}", requestable=True, steward=steward,
                delegationTier=tier,
            )
            resource.clean()  # must not raise

    def test_the_legend_is_complete_and_flags_which_tiers_can_be_requested(self):
        legend = ChapterResource.getDelegationTierLegend()
        self.assertEqual(len(legend), len(ChapterResource.DELEGATION_TIER_CHOICES))
        self.assertEqual(
            {entry["label"] for entry in legend if entry["requestable"]},
            {"Green", "Yellow"},
        )


@fastHashing
class DelegationTierFormRuleTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.editor = UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.loginAs(self.editor)

    def test_the_form_rejects_a_red_requestable_tool(self):
        """Re-implemented in the form because the view never calls full_clean(),
        the same reason the steward rule is re-implemented there."""
        steward = UserFactory.make("steward")
        response = self.client.post(
            reverse("chapter-tool-new"),
            _restrictedPayload(
                name="Example Vault", requestable="on", steward=str(steward.pk),
                delegationTier=str(ChapterResource.DelegationTier.RED),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be opened for member requests")
        self.assertFalse(ChapterResource.objects.filter(name="Example Vault").exists())

    def test_an_open_layer_editor_is_not_blocked_by_a_tier_they_cannot_see(self):
        """Both fields in the tier rule are restricted, so an open-layer editor
        can neither create a violation nor fix one. Raising it at them would make
        a legacy row permanently unsaveable by the only person in front of it,
        and the message would name a tier they are not allowed to see."""
        steward = UserFactory.make("steward")
        resource = _makeResource(
            name="Example Vault", requestable=True, steward=steward,
            delegationTier=ChapterResource.DelegationTier.RED,
        )
        openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))
        self.loginAs(openEditor)
        response = self.client.post(
            reverse("chapter-tool-edit", kwargs={"pk": resource.pk}),
            _resourcePayload(
                name="Example Vault", steward=str(steward.pk),
                blurb="Edited by the open layer.",
            ),
        )
        self.assertEqual(response.status_code, 302)
        resource.refresh_from_db()
        self.assertEqual(resource.blurb, "Edited by the open layer.")
        # Left untouched - neither repaired nor blanked.
        self.assertEqual(resource.delegationTier, ChapterResource.DelegationTier.RED)
        self.assertTrue(resource.requestable)


class AccessLevelTests(TestCase):
    def test_every_level_has_an_explanation(self):
        for value, label in ResourceHolder.ACCESS_LEVEL_CHOICES:
            self.assertTrue(
                ResourceHolder.ACCESS_LEVEL_EXPLANATIONS.get(value),
                f"access level {label} has no explanation",
            )

    def test_a_new_holder_defaults_to_unconfirmed_not_ordinary(self):
        """A guess recorded as a fact is worse than a recorded gap - the rule
        `confirmed` already follows. Every row predating this field is genuinely
        unchecked, and the default says so rather than calling them all
        ordinary."""
        holder = ResourceHolder.objects.create(
            resource=_makeResource(), personName="Example Holder",
        )
        self.assertEqual(holder.accessLevel, ResourceHolder.AccessLevel.UNCONFIRMED)
        self.assertFalse(holder.isPrivileged())

    def test_unconfirmed_and_ordinary_are_not_privileged(self):
        """UNCONFIRMED is the absence of a claim, not a claim that somebody IS
        privileged, so it must not inflate the privileged list."""
        self.assertNotIn(ResourceHolder.AccessLevel.UNCONFIRMED, ResourceHolder.PRIVILEGED_LEVELS)
        self.assertNotIn(ResourceHolder.AccessLevel.ORDINARY, ResourceHolder.PRIVILEGED_LEVELS)

    def test_admin_owner_and_primary_owner_are_privileged(self):
        resource = _makeResource()
        for level in (ResourceHolder.AccessLevel.ADMIN, ResourceHolder.AccessLevel.OWNER,
                      ResourceHolder.AccessLevel.PRIMARY_OWNER):
            holder = ResourceHolder.objects.create(
                resource=resource, personName=f"Example Holder {level}", accessLevel=level,
            )
            self.assertTrue(holder.isPrivileged(), f"level {level} should be privileged")


class CredentialAgeTests(TestCase):
    def _credential(self, **overrides):
        defaults = {
            "resource": _makeResource(),
            "label": "ExampleCredentialSentinel",
            "kind": ResourceCredential.Kind.VAULT_SHARED_LOGIN,
        }
        defaults.update(overrides)
        return ResourceCredential.objects.create(**defaults)

    def test_no_dates_reads_as_unknown_not_as_new(self):
        """Not knowing and knowing it is old prompt different work, so they must
        not collapse into one answer - and an undated credential must never be
        reported as freshly rotated."""
        credential = self._credential()
        self.assertIsNone(credential.getAgeDays())
        self.assertFalse(credential.isRotationOverdue())
        self.assertEqual(credential.getRotationStatus(), "unknown")

    def test_age_zero_is_a_real_answer_not_a_missing_one(self):
        """A credential rotated today has an age of 0, which is falsy - so any
        caller testing truthiness reports it as unknown. That exact bug was
        written into this feature's template once."""
        credential = self._credential(lastRotated=datetime.date.today())
        self.assertEqual(credential.getAgeDays(), 0)
        self.assertEqual(credential.getRotationStatus(), "ok")

    def test_age_falls_back_to_addedAt_when_never_rotated(self):
        credential = self._credential(
            addedAt=datetime.date.today() - datetime.timedelta(days=10),
        )
        self.assertEqual(credential.getAgeDays(), 10)
        self.assertEqual(credential.getRotationStatus(), "never-rotated")

    def test_overdue_past_the_threshold(self):
        credential = self._credential(
            lastRotated=datetime.date.today() - datetime.timedelta(
                days=ResourceCredential.ROTATE_AFTER_DAYS + 1,
            ),
        )
        self.assertTrue(credential.isRotationOverdue())
        self.assertEqual(credential.getRotationStatus(), "overdue")

    def test_a_retired_credential_is_never_overdue(self):
        credential = self._credential(
            lastRotated=datetime.date.today() - datetime.timedelta(days=5000),
            status=ResourceCredential.Status.RETIRED,
        )
        self.assertFalse(credential.isRotationOverdue())
        self.assertEqual(credential.getRotationStatus(), "retired")


@fastHashing
class CredentialDateFormTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.loginAs(UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        ))

    def _post(self, resource, **overrides):
        payload = {
            "label": "ExampleCredentialSentinel",
            "kind": str(ResourceCredential.Kind.VAULT_SHARED_LOGIN),
            "vaultCollection": "",
            "status": str(ResourceCredential.Status.LIVE),
            "addedAt": "", "lastRotated": "", "note": "",
        }
        payload.update(overrides)
        return self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": resource.pk, "childKind": "credentials"}),
            payload,
        )

    def test_rotated_before_it_existed_is_rejected(self):
        """getAgeDays() prefers lastRotated, so a rotation date before the added
        date would report a NEGATIVE age and read as fresher than new."""
        resource = _makeResource()
        response = self._post(resource, addedAt="2026-06-01", lastRotated="2026-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot have been rotated before it existed")
        self.assertEqual(ResourceCredential.objects.count(), 0)

    def test_the_dates_round_trip(self):
        resource = _makeResource()
        self._post(resource, addedAt="2026-01-01", lastRotated="2026-06-01")
        credential = ResourceCredential.objects.get(resource=resource)
        self.assertEqual(credential.addedAt, datetime.date(2026, 1, 1))
        self.assertEqual(credential.lastRotated, datetime.date(2026, 6, 1))


class GlossaryTests(TestCase):
    """The definitions themselves. These are prose, so the assertions are about
    structure and reachability - a stub entry, a dead slug, or a ladder that
    stopped being attached."""

    def test_every_entry_has_a_title_and_a_real_body(self):
        for slug, entry in GLOSSARY.items():
            self.assertTrue(entry.get("title"), f"{slug} has no title")
            self.assertGreater(len(entry.get("body", "")), 60, f"{slug}'s body is a stub")

    def test_unknown_slug_returns_none_rather_than_raising(self):
        """A template typo must not 500 a page whose real content is fine."""
        self.assertIsNone(getEntry("no-such-entry"))

    def test_the_two_ladders_are_attached_from_the_models(self):
        """The rung text has exactly one home - the model dicts - so the
        glossary cannot drift from what the detail page renders."""
        tier = getEntry("delegation-tier")
        self.assertEqual(len(tier["items"]), len(ChapterResource.DELEGATION_TIER_CHOICES))
        self.assertIn(
            ChapterResource.DELEGATION_TIER_EXPLANATIONS[ChapterResource.DelegationTier.RED],
            [item["explanation"] for item in tier["items"]],
        )

        level = getEntry("access-level")
        self.assertEqual(len(level["items"]), len(ResourceHolder.ACCESS_LEVEL_CHOICES))

    def test_getEntry_does_not_mutate_the_glossary(self):
        """It pops the `ladder` key off a copy. Popping off the original would
        work once and then silently stop attaching the ladder for the rest of the
        process - which is the kind of bug that only shows up on the second
        page view."""
        getEntry("delegation-tier")
        self.assertIn("ladder", GLOSSARY["delegation-tier"])
        self.assertIsNotNone(getEntry("delegation-tier").get("items"))

    def test_the_explain_tag_renders_the_definition(self):
        rendered = Template(
            '{% load explain_tags %}{% explain "steward" %}'
        ).render(Context({}))
        self.assertIn("What a steward is", rendered)
        self.assertIn("<details", rendered)

    def test_the_explain_tag_renders_nothing_for_an_unknown_slug(self):
        rendered = Template(
            '{% load explain_tags %}{% explain "no-such-entry" %}'
        ).render(Context({}))
        self.assertNotIn("<details", rendered)
        self.assertEqual(rendered.strip(), "")

    def test_every_slug_used_in_a_template_resolves(self):
        """getEntry returns None rather than raising for an unknown slug, so a
        typo would silently render nothing. This is where that gets caught."""
        import re
        from pathlib import Path

        templateDir = Path(__file__).resolve().parent.parent / "templates" / "tools"
        used = set()
        for path in templateDir.rglob("*.html"):
            for match in re.finditer(r'{%\s*explain\s+"([^"]+)"', path.read_text(encoding="utf-8")):
                used.add(match.group(1))
        self.assertTrue(used, "no explain tags found - has the tag been renamed?")
        for slug in sorted(used):
            self.assertIsNotNone(getEntry(slug), f'{{% explain "{slug}" %}} resolves to nothing')
