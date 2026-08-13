"""Chapter Tools: definitions beside their labels, and picking a member by typing.

All data here is invented - never real chapter services, credentials, or people.

Two changes, one module because they land on the same surfaces (the edit form and
the child form):

  1. Each definition now renders beside the FIELD it defines, not in a block at
     the foot of the form. The assertions are positional on purpose - "the text
     is somewhere on the page" was already true before this change, so it cannot
     be what proves it moved.
  2. Steward and holder-account are typed, not chosen from a <select> of every
     member. The tests that matter are the ones about the hidden value and the
     visible box never disagreeing, because that is the failure mode a control
     shaped like this has.
"""
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from tools import forms
from tools.chapterToolsHelp import getEntry
from tools.models import ChapterResource, ResourceHolder
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing
from tools.tests.test_chapter_tools import _makeResource, _restrictedPayload


def _positionOf(response, needle):
    """Byte offset of `needle` in the rendered page, asserting it is there."""
    body = response.content.decode()
    at = body.find(needle)
    assert at != -1, f"{needle!r} is not on the page at all"
    return at


class ExplainSlugMapTests(TestCase):
    """The form-side half: which fields carry a definition."""

    def test_every_mapped_slug_resolves(self):
        """A typo in a form's EXPLAIN_SLUGS would render nothing at all, because
        getEntry returns None rather than raising. This is where it gets caught -
        the sibling test in test_chapter_tools_vocabulary covers the same risk for
        slugs hardcoded in templates."""
        mapped = set()
        for formClass in (forms.ChapterResourceForm, forms.ResourceHolderForm,
                          forms.ResourceCredentialForm):
            mapped.update(formClass.EXPLAIN_SLUGS.values())
        self.assertTrue(mapped, "no form declares any definitions")
        for slug in sorted(mapped):
            self.assertIsNotNone(getEntry(slug), f"{slug!r} resolves to nothing")

    def test_every_mapped_field_exists_on_its_form(self):
        """A field renamed without updating the map would silently stop showing
        its definition. Checked against the Keys class rather than the live
        fields, because the restricted keys are deleted from some instances."""
        for formClass in (forms.ChapterResourceForm, forms.ResourceHolderForm,
                          forms.ResourceCredentialForm):
            declared = {
                value for name, value in vars(formClass.Keys).items()
                if not name.startswith("_")
            }
            for fieldName in formClass.EXPLAIN_SLUGS:
                self.assertIn(fieldName, declared,
                              f"{formClass.__name__} maps a definition to the unknown "
                              f"field {fieldName!r}")

    def test_the_filter_reads_the_map_off_the_bound_field(self):
        form = forms.ResourceHolderForm()
        rendered = Template(
            "{% load explain_tags %}"
            "[{{ form.accessLevel|explainSlugFor }}][{{ form.personName|explainSlugFor }}]"
        ).render(Context({"form": form}))
        self.assertEqual(rendered, "[access-level][]")

    def test_a_form_with_no_map_yields_no_slug(self):
        """Every other domain's forms go through the same formRow partial, so the
        filter has to be silent on a form that never heard of definitions."""
        form = forms.ResourceDependencyForm(resource=None, allowRestrictedKinds=False)
        rendered = Template(
            "{% load explain_tags %}[{{ form.kind|explainSlugFor }}]"
        ).render(Context({"form": form}))
        self.assertEqual(rendered, "[]")


class FormRowExplainTests(TestCase):
    """The template-side half. formRow.html renders every form in the app, so the
    no-slug path has to stay byte-for-byte what it was."""

    def _render(self, extra=""):
        form = forms.ResourceHolderForm()
        return Template(
            "{% load explain_tags %}"
            '{% include "tools/common/formRow.html" with field=form.accessLevel ' + extra + " %}"
        ).render(Context({"form": form}))

    def test_a_slug_puts_the_definition_in_a_label_row(self):
        rendered = self._render("explainSlug=form.accessLevel|explainSlugFor")
        self.assertIn('class="explain-row"', rendered)
        self.assertIn("<details", rendered)
        # The definition sits inside the label row, not after the widget. The
        # widget is what the field renders - accessLevel is a text box with a
        # datalist now, so anchor on the input, not on a <select> that would make
        # this assertion pass by finding nothing.
        widgetAt = rendered.find("<input")
        self.assertNotEqual(widgetAt, -1)
        self.assertLess(rendered.find("<details"), widgetAt)

    def test_no_slug_renders_no_label_row_at_all(self):
        rendered = self._render()
        self.assertNotIn("explain-row", rendered)
        self.assertNotIn("<details", rendered)
        self.assertIn("<label", rendered)

    def test_the_label_still_points_at_its_field(self):
        """The row wraps the label; it must not replace it. A label that lost its
        `for` stops focusing the field when clicked."""
        rendered = self._render("explainSlug=form.accessLevel|explainSlugFor")
        self.assertIn('for="id_accessLevel"', rendered)


class DefinitionsSitWithTheirFieldTests(LoginClientMixin, TestCase):
    """The positional assertions. This is the change Cam asked for: the
    definitions used to be a block below the submit button."""

    @fastHashing
    def setUp(self):
        self.resource = _makeResource(name="Example Wiki")
        self.auditEditor = UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        )
        self.openEditor = UserFactory.make("openEditor", perms=("manageChapterTools",))

    def test_the_steward_definition_sits_between_its_own_field_and_the_next(self):
        self.loginAs(self.auditEditor)
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))
        stewardRow = _positionOf(response, 'data-field="steward"')
        definition = _positionOf(response, "The one person answerable for this tool")
        nextRow = _positionOf(response, 'data-field="stewardName"')
        self.assertLess(stewardRow, definition)
        self.assertLess(definition, nextRow)

    def test_no_definition_is_left_below_the_submit_button(self):
        """The regression this change exists to prevent: a definition that ends
        up after the button is read after the field is already filled in."""
        self.loginAs(self.auditEditor)
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))
        button = _positionOf(response, "Save changes")
        body = response.content.decode()
        # Everything after the submit button, up to the holders card.
        tail = body[button:body.find("Who has it now")]
        self.assertNotIn("<details", tail)

    def test_the_tier_definition_is_absent_for_an_open_layer_editor(self):
        """This replaces an explicit hasAudit branch in the template. The
        restricted FIELDS are already removed from a non-audit editor's form, so
        their definitions cannot render - and that is now the only rule, rather
        than a second list in the template that could disagree with the first."""
        self.loginAs(self.openEditor)
        response = self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))
        self.assertContains(response, "The one person answerable for this tool")
        self.assertNotContains(response, "Hand this out freely")
        self.assertNotContains(response, "What members can request through Echo")

    def test_the_holder_form_defines_access_level_beside_the_field(self):
        self.loginAs(self.auditEditor)
        response = self.client.get(reverse(
            "chapter-tool-child-new", kwargs={"pk": self.resource.pk, "childKind": "holders"},
        ))
        levelRow = _positionOf(response, 'data-field="accessLevel"')
        definition = _positionOf(response, "which door they came through")
        nextRow = _positionOf(response, 'data-field="confirmed"')
        self.assertLess(levelRow, definition)
        self.assertLess(definition, nextRow)

    def test_the_credential_form_defines_rotation_beside_the_date(self):
        self.loginAs(self.auditEditor)
        response = self.client.get(reverse(
            "chapter-tool-child-new",
            kwargs={"pk": self.resource.pk, "childKind": "credentials"},
        ))
        dateRow = _positionOf(response, 'data-field="lastRotated"')
        definition = _positionOf(response, "Age is counted from the day")
        self.assertLess(dateRow, definition)
        self.assertLess(definition, _positionOf(response, 'data-field="note"'))


class MemberSearchEndpointTests(LoginClientMixin, TestCase):
    @fastHashing
    def setUp(self):
        self.url = reverse("chapter-tool-member-search")
        self.editor = UserFactory.make("fullEditor", perms=("manageChapterTools",))
        UserFactory.make("rmartinez", email="rosa@example.org",
                         first_name="Rosa", last_name="Martinez")
        UserFactory.make("dchen", email="dchen@example.org",
                         first_name="Dana", last_name="Chen")

    def _search(self, query):
        return self.client.get(self.url, {"q": query}).json()["results"]

    def test_a_plain_member_cannot_search(self):
        """It is a member-directory search, so it is gated on the same permission
        as the forms that use it - not merely on being logged in."""
        self.loginAs(UserFactory.make("member"))
        response = self.client.get(self.url, {"q": "ro"})
        self.assertNotEqual(response.status_code, 200)

    def test_one_character_returns_nothing(self):
        """Not an error: a one-letter query matches most of the chapter, so it
        would be a slow way to say nothing."""
        self.loginAs(self.editor)
        self.assertEqual(self._search("r"), [])
        self.assertEqual(self._search(""), [])

    def test_it_matches_first_name_last_name_username_and_email(self):
        self.loginAs(self.editor)
        for query in ("Rosa", "Martinez", "rmartinez", "rosa@example"):
            names = [row["label"] for row in self._search(query)]
            self.assertTrue(any("Rosa Martinez" in name for name in names),
                            f"{query!r} did not find Rosa")

    def test_the_label_is_the_name_and_email(self):
        """getUserNameString(), the same string the <select> showed - it is what
        tells two members with the same first name apart."""
        self.loginAs(self.editor)
        row = self._search("rmartinez")[0]
        self.assertEqual(row["label"], "Rosa Martinez - rosa@example.org")
        self.assertEqual(row["username"], "rmartinez")

    def test_an_inactive_member_is_not_offered(self):
        self.loginAs(self.editor)
        gone = UserFactory.make("former", first_name="Former", last_name="Member")
        gone.is_active = False
        gone.save()
        self.assertEqual(self._search("Former"), [])

    def test_results_are_capped(self):
        """The cap is the point of the endpoint - an uncapped one is the <select>
        again, just delivered as JSON."""
        self.loginAs(self.editor)
        for index in range(15):
            UserFactory.make(f"padding{index}", first_name="Padding", last_name=f"P{index}")
        self.assertEqual(len(self._search("Padding")), 10)


class StewardPickerTests(LoginClientMixin, TestCase):
    @fastHashing
    def setUp(self):
        self.steward = UserFactory.make("jvega", email="jvega@example.org",
                                        first_name="Jo", last_name="Vega")
        self.resource = _makeResource(name="Example Wiki")
        self.loginAs(UserFactory.make(
            "fullEditor", perms=("manageChapterTools", "viewChapterToolAudit"),
        ))

    def _editPage(self):
        return self.client.get(reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}))

    def test_the_steward_field_is_not_a_select_of_every_member(self):
        """The reason this changed: one <option> per member is a control whose
        cost grows with the chapter."""
        response = self._editPage()
        body = response.content.decode()
        stewardRow = body[body.find('data-field="steward"'):body.find('data-field="stewardName"')]
        self.assertNotIn("<select", stewardRow)
        self.assertIn("data-typeahead", stewardRow)

    def test_only_the_hidden_input_carries_the_field_name(self):
        """The visible box has no name, so it is never submitted and can never be
        mistaken for the value."""
        response = self._editPage()
        body = response.content.decode()
        stewardRow = body[body.find('data-field="steward"'):body.find('data-field="stewardName"')]
        self.assertIn('type="hidden" name="steward"', stewardRow)
        self.assertIn("data-typeahead-input", stewardRow)
        self.assertNotIn('name="steward" type="search"', stewardRow)
        # One named input in the row, not two.
        self.assertEqual(stewardRow.count('name="steward"'), 1)

    def test_an_empty_field_shows_the_search_box_and_no_chip(self):
        response = self._editPage()
        body = response.content.decode()
        stewardRow = body[body.find('data-field="steward"'):body.find('data-field="stewardName"')]
        self.assertIn("data-typeahead-chosen hidden", stewardRow.replace('="" ', " "))

    def test_a_set_steward_renders_as_a_chip_naming_them(self):
        self.resource.steward = self.steward
        self.resource.save()
        response = self._editPage()
        self.assertContains(response, "Jo Vega - jvega@example.org")
        body = response.content.decode()
        stewardRow = body[body.find('data-field="steward"'):body.find('data-field="stewardName"')]
        # The chip is shown and the search box is hidden - never both.
        self.assertNotIn("data-typeahead-chosen hidden", stewardRow.replace('="" ', " "))
        self.assertIn("data-typeahead-input", stewardRow)
        self.assertIn("hidden", stewardRow[stewardRow.find("data-typeahead-input"):])

    def test_posting_a_pk_still_saves_the_steward(self):
        """The widget swap must not change what the form accepts - the submitted
        value is still a pk, which is what ModelChoiceField already expects."""
        response = self.client.post(
            reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}),
            # The restricted payload, not the open one: this editor holds the
            # audit permission, so their form carries all six restricted fields
            # and a short payload fails for reasons unrelated to the picker.
            _restrictedPayload(name="Example Wiki", steward=str(self.steward.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.steward, self.steward)

    def test_an_unknown_pk_is_still_refused(self):
        response = self.client.post(
            reverse("chapter-tool-edit", kwargs={"pk": self.resource.pk}),
            _restrictedPayload(name="Example Wiki", steward="999999"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid choice")
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.steward)

    def test_a_steward_who_went_inactive_is_named_as_a_problem(self):
        """Not rendered as an empty picker: an empty picker saves as "no steward"
        on the next submit, silently dropping the fact that somebody was named."""
        self.resource.steward = self.steward
        self.resource.save()
        self.steward.is_active = False
        self.steward.save()
        response = self._editPage()
        self.assertContains(response, "no longer active")

    def test_the_holder_account_field_is_a_typeahead_too(self):
        response = self.client.get(reverse(
            "chapter-tool-child-new", kwargs={"pk": self.resource.pk, "childKind": "holders"},
        ))
        body = response.content.decode()
        userRow = body[body.find('data-field="user"'):body.find('data-field="how"')]
        self.assertIn("data-typeahead", userRow)
        self.assertNotIn("<select", userRow)

    def test_a_holder_can_still_be_linked_to_an_account(self):
        self.client.post(
            reverse("chapter-tool-child-new",
                    kwargs={"pk": self.resource.pk, "childKind": "holders"}),
            {
                "personName": "", "user": str(self.steward.pk),
                "how": str(ResourceHolder.How.INDIVIDUAL_LOGIN),
                "accessLevel": "Owner", "canGrantAccess": "on", "ownsAccount": "on",
                "confirmed": "on", "note": "",
            },
        )
        holder = ResourceHolder.objects.get(resource=self.resource)
        self.assertEqual(holder.user, self.steward)

    def test_the_page_loads_the_script_the_picker_needs(self):
        """A typeahead with no script is a dead text box. Versioned, because a
        behaviour script cached against new markup fails more confusingly than a
        stale stylesheet."""
        response = self._editPage()
        self.assertContains(response, "js/memberTypeahead.js?v=")


class DetailPageDefinitionsTests(LoginClientMixin, TestCase):
    """The reader-facing page. Same rule as the form: beside the label, not at
    the foot of the section."""

    @fastHashing
    def setUp(self):
        self.steward = UserFactory.make("jvega", first_name="Jo", last_name="Vega")
        self.resource = _makeResource(
            name="Example Wiki", steward=self.steward,
            delegationTier=ChapterResource.DelegationTier.YELLOW,
        )
        self.loginAs(UserFactory.make(
            "auditor", perms=("viewChapterToolAudit", "viewResourceHolders"),
        ))

    def test_the_steward_definition_follows_the_steward_value(self):
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and none of the fields below render at all.
        response = self.client.get(f"{self.resource.getUrl()}?tab=holders")
        value = _positionOf(response, "Jo Vega")
        definition = _positionOf(response, "The one person answerable for this tool")
        self.assertLess(value, definition)
        # Inside the same dd, so the panel gets the wide column - see the comment
        # in detail.html about the dt column being max-content. The row OPENS
        # before the value, so this looks backwards from it, not forwards.
        body = response.content.decode()
        self.assertIn("explain-row", body[max(0, value - 200):definition])

    def test_the_tier_ladder_opens_from_inside_the_tier_callout(self):
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and none of the fields below render at all.
        response = self.client.get(f"{self.resource.getUrl()}?tab=committee")
        callout = _positionOf(response, "Delegation tier:")
        ladder = _positionOf(response, "What the other tiers mean")
        body = response.content.decode()
        # Both inside the same alert block, so the tier and its scale are one
        # thing on the page rather than a fact and a footnote.
        alertEnd = body.find("</div>", callout)
        self.assertLess(ladder, alertEnd)

    def test_the_review_definition_sits_with_the_review_date(self):
        # ?tab= is explicit: the detail page is tabbed, so a bare fetch lands
        # on the access panel and none of the fields below render at all.
        response = self.client.get(f"{self.resource.getUrl()}?tab=committee")
        label = _positionOf(response, "Last reviewed")
        definition = _positionOf(response, "A review is a check, not an edit")
        nextLabel = _positionOf(response, "Reviewed by")
        self.assertLess(label, definition)
        self.assertLess(definition, nextLabel)
