"""The register-wide privileged-access page, and stylesheet cache-busting.

All data here is invented - never real chapter services, credentials, or people.

The page answers a question no single resource page can: where is the chapter one
person away from losing something. The tests below are mostly about the FINDINGS,
because the findings are the product - a page that lists holders correctly but
mislabels "nobody recorded" as healthy is worse than no page.
"""
import re

from django.test import TestCase
from django.urls import reverse

from tools.models import ChapterResource, ResourceHolder
from tools.templatetags.asset_tags import _versionCache, versionedStatic
from tools.tests.support import LoginClientMixin, UserFactory, fastHashing
from tools.tests.test_chapter_tools import _makeResource

HOLDER_SENTINEL = "Example Holder"


def _holder(resource, name, role="", canGrant=False, owns=False, confirmed=True):
    """One holder row.

    `role` is free text and is deliberately NOT what any assertion below turns
    on - the findings are computed from canGrant/owns. That separation is the
    thing worth testing: the page has to keep working when somebody types
    "Delegated user" instead of a word this file happens to know.
    """
    return ResourceHolder.objects.create(
        resource=resource, personName=name, accessLevel=role,
        canGrantAccess=canGrant, ownsAccount=owns, confirmed=confirmed,
    )


@fastHashing
class PrivilegedAccessGateTests(LoginClientMixin, TestCase):
    """Holder identity is exactly what this page shows, so it is the holder tier
    or audit - not a new one. 404 rather than 403 for anybody else, matching the
    restricted child routes: a 403 confirms the page exists."""

    def setUp(self):
        self.resource = _makeResource()
        _holder(self.resource, HOLDER_SENTINEL, role="Owner", canGrant=True, owns=True)
        self.url = reverse("chapter-tools-privileged")

    def test_a_plain_member_gets_a_404(self):
        self.loginAs(UserFactory.make("member"))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_holder_tier_can_read_it(self):
        self.loginAs(UserFactory.make("organizer", perms=("viewResourceHolders",)))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # Paired with the 404 above using the same literal, so a markup change
        # that stops the name rendering fails loudly here instead of making the
        # absence assertion pass for the wrong reason.
        self.assertContains(response, HOLDER_SENTINEL)

    def test_the_audit_tier_can_read_it_without_the_holder_permission(self):
        """_hasHolders is `audit or holders`: the audit ring is already trusted
        with strictly more than the roster, so a second grant buys nothing."""
        self.loginAs(UserFactory.make("committee", perms=("viewChapterToolAudit",)))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, HOLDER_SENTINEL)

    def test_the_nav_tile_is_visible_to_both_tiers(self):
        """NavTool.alsoVisibleWith exists for exactly this page. Without it the
        tile would be hidden from the audit ring while the URL still worked."""
        from tools.navigation import NAV_TOOLS

        tile = next(tool for tool in NAV_TOOLS if tool.routeName == "chapter-tools-privileged")
        self.assertTrue(tile.isVisibleTo(UserFactory.make("a", perms=("viewResourceHolders",))))
        self.assertTrue(tile.isVisibleTo(UserFactory.make("b", perms=("viewChapterToolAudit",))))
        self.assertFalse(tile.isVisibleTo(UserFactory.make("c")))


@fastHashing
class PrivilegedAccessFindingTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.loginAs(UserFactory.make("committee", perms=("viewChapterToolAudit",)))
        self.url = reverse("chapter-tools-privileged")

    def _findingFor(self, resource):
        rows = self.client.get(self.url).context["rows"]
        return next(row for row in rows if row["resource"].pk == resource.pk)

    def test_a_resource_with_no_holders_is_no_holders_not_no_owner(self):
        """Two different gaps. "Nobody is recorded at all" is a hole in the
        register; "people hold this but no owner is recorded" is a finding about
        the chapter. Collapsing them hides which one you are looking at."""
        resource = _makeResource(name="Example Empty Tool")
        self.assertEqual(self._findingFor(resource)["finding"], "no-holders")

    def test_holders_but_no_owner_is_no_owner(self):
        resource = _makeResource(name="Example Unowned Tool")
        _holder(resource, HOLDER_SENTINEL, role="Ordinary member")
        self.assertEqual(self._findingFor(resource)["finding"], "no-owner")

    def test_only_unchecked_holders_is_reported_separately(self):
        """The owner may already be on the list, unverified - which is the
        cheapest thing to go and find out, so it gets its own finding rather than
        being lumped in with "no owner".

        Driven by `confirmed`, which is now the single "has anybody looked" flag.
        It used to be asked twice - this flag and an UNCONFIRMED rung on the
        access ladder - and this page counted only the second one, so a row that
        was unconfirmed overall but had a level typed in was reported as checked.
        """
        resource = _makeResource(name="Example Unchecked Tool")
        _holder(resource, HOLDER_SENTINEL, confirmed=False)
        row = self._findingFor(resource)
        self.assertEqual(row["finding"], "unconfirmed-only")
        self.assertEqual(row["unconfirmedCount"], 1)

    def test_one_owner_is_single_owner(self):
        resource = _makeResource(name="Example Single Owner Tool")
        _holder(resource, HOLDER_SENTINEL, role="Primary owner", canGrant=True, owns=True)
        row = self._findingFor(resource)
        self.assertEqual(row["finding"], "single-owner")
        self.assertEqual(row["ownerCount"], 1)

    def test_two_owners_is_ok(self):
        resource = _makeResource(name="Example Two Owner Tool")
        _holder(resource, HOLDER_SENTINEL, role="Owner", canGrant=True, owns=True)
        _holder(resource, "Example Second Holder", role="Primary owner", canGrant=True, owns=True)
        row = self._findingFor(resource)
        self.assertEqual(row["finding"], "ok")
        self.assertEqual(row["ownerCount"], 2)

    def test_an_admin_is_privileged_but_does_not_count_as_an_owner(self):
        """An admin can change other people's access but usually cannot recover
        or delete the account, so they answer the privileged question and not the
        bus-factor one."""
        resource = _makeResource(name="Example Admin Only Tool")
        _holder(resource, HOLDER_SENTINEL, role="Admin", canGrant=True)
        row = self._findingFor(resource)
        self.assertEqual(row["ownerCount"], 0)
        # Asserted through the line the page actually renders rather than a
        # count: privileged holders are now grouped by power, so "privileged but
        # not an owner" IS "on the accessOnly line and not the owners one".
        self.assertEqual(row["owners"], "")
        self.assertEqual(row["accessOnly"], f"{HOLDER_SENTINEL} (Admin)")
        self.assertEqual(row["finding"], "no-owner")

    def test_gaps_sort_above_healthy_rows(self):
        """The page exists to surface what is missing; sorted by name the empty
        rows bury themselves among the healthy ones."""
        healthy = _makeResource(name="AAA Example Healthy Tool")
        _holder(healthy, "Example Owner One", role="Owner", canGrant=True, owns=True)
        _holder(healthy, "Example Owner Two", role="Owner", canGrant=True, owns=True)
        _makeResource(name="ZZZ Example Empty Tool")

        rows = self.client.get(self.url).context["rows"]
        findings = [row["finding"] for row in rows]
        self.assertEqual(findings[0], "no-holders")
        self.assertEqual(findings[-1], "ok")

    def test_the_audit_read_is_logged(self):
        from tools.models import ToolAuditReadLog

        _makeResource(name="Example Logged Tool")
        self.client.get(self.url)
        self.assertTrue(ToolAuditReadLog.objects.filter(target="privileged-access").exists())

    def test_the_holder_tier_read_is_not_logged(self):
        """The read log is the restricted layer auditing itself. The holder tier
        is not the restricted layer, and logging it would quietly turn this page
        into surveillance of organizers."""
        from tools.models import ToolAuditReadLog

        self.loginAs(UserFactory.make("organizer", perms=("viewResourceHolders",)))
        self.client.get(self.url)
        self.assertFalse(ToolAuditReadLog.objects.filter(target="privileged-access").exists())


@fastHashing
class PrivilegedCardLayoutTests(LoginClientMixin, TestCase):
    """How the card READS, which is a product question and not a cosmetic one.

    The first version listed one line per privileged person, labelled with the
    sentence getPowerSummary() returns. Labels in this card vocabulary render
    uppercase and letter-spaced, so a tool with two owners printed the same
    43-character run of capitals twice and the names - the only part that varied,
    and the only part anybody opened the page for - were the quietest thing on
    it. People are grouped by power now: one short label, several names.

    These assertions are about structure rather than pixels, because that is the
    part that can regress silently: a repeated label, a name on the wrong line, a
    sentence back in the label slot.
    """

    def setUp(self):
        self.loginAs(UserFactory.make("committee", perms=("viewChapterToolAudit",)))
        self.url = reverse("chapter-tools-privileged")

    def _rowFor(self, resource):
        return next(
            row for row in self.client.get(self.url).context["rows"]
            if row["resource"].id == resource.id
        )

    def test_two_owners_share_one_label(self):
        """The actual regression. Two owners used to mean the same long phrase
        twice; it must now appear once with both names after it."""
        resource = _makeResource(name="Example Two Owner Tool")
        _holder(resource, "Example Owner One", role="Owner", canGrant=True, owns=True)
        _holder(resource, "Example Owner Two", role="Owner", canGrant=True, owns=True)

        page = self.client.get(self.url).content.decode()
        self.assertEqual(page.count(">Owns it<"), 1)
        row = self._rowFor(resource)
        self.assertIn("Example Owner One", row["owners"])
        self.assertIn("Example Owner Two", row["owners"])

    def test_the_power_sentence_is_not_a_label_on_this_page(self):
        """getPowerSummary() is the sentence for ONE row on one tool's own page.
        Here it would be shouted once per person, so the page uses short labels
        and the sentence must not have crept back in."""
        resource = _makeResource(name="Example Sentence Free Tool")
        holder = _holder(resource, HOLDER_SENTINEL, role="Owner", canGrant=True, owns=True)

        page = self.client.get(self.url).content.decode()
        # Guard: the sentence has to be a real one, or this asserts nothing.
        self.assertGreater(len(holder.getPowerSummary()), 20)
        self.assertNotIn(holder.getPowerSummary(), page)
        self.assertContains(self.client.get(self.url), HOLDER_SENTINEL)

    def test_the_two_groups_partition_the_privileged_holders(self):
        """isPrivileged() is owns OR canGrant, so anybody privileged who is not
        an owner belongs to the second group. Nobody may appear twice - a name on
        both lines reads as two different people with the same name - and nobody
        may be dropped, which would understate the finding."""
        resource = _makeResource(name="Example Mixed Power Tool")
        _holder(resource, "Example Both Powers", role="Owner", canGrant=True, owns=True)
        _holder(resource, "Example Grant Only", role="Administrator", canGrant=True)
        _holder(resource, "Example Owner Only", role="Billing contact", owns=True)
        _holder(resource, "Example No Power", role="Member")

        row = self._rowFor(resource)
        self.assertIn("Example Both Powers", row["owners"])
        self.assertIn("Example Owner Only", row["owners"])
        self.assertNotIn("Example Both Powers", row["accessOnly"])
        self.assertEqual(row["accessOnly"], "Example Grant Only (Administrator)")
        # Somebody with neither power is not on the page's lines at all.
        self.assertNotIn("Example No Power", row["owners"])
        self.assertNotIn("Example No Power", row["accessOnly"])

    def test_a_role_and_the_unconfirmed_flag_share_one_bracket(self):
        """One bracket per person. A name trailed by a separate parenthetical and
        a separate dash is three fragments to reassemble into one fact."""
        resource = _makeResource(name="Example Unconfirmed Owner Tool")
        _holder(resource, "Example Unchecked Owner", role="Owner",
                canGrant=True, owns=True, confirmed=False)

        self.assertEqual(
            self._rowFor(resource)["owners"],
            "Example Unchecked Owner (Owner, unconfirmed)",
        )

    def test_a_blank_role_gets_no_empty_bracket(self):
        """Blank is a true answer for a free-text role - nobody wrote one down -
        but "(Role not recorded)" beside every name on a page about owners is
        noise, so the bracket is simply absent."""
        resource = _makeResource(name="Example Unnamed Role Tool")
        _holder(resource, "Example Roleless Owner", canGrant=True, owns=True)

        self.assertEqual(self._rowFor(resource)["owners"], "Example Roleless Owner")

    def test_the_people_lines_stack_instead_of_flowing_inline(self):
        """The inline strip assumes SHORT values. These are lists of names, and
        flowing one beside another puts the start of the second group somewhere in
        the middle of the first."""
        resource = _makeResource(name="Example Stacking Tool")
        _holder(resource, "Example Owner", role="Owner", canGrant=True, owns=True)
        _holder(resource, "Example Admin", role="Administrator", canGrant=True)

        self.assertContains(
            self.client.get(self.url), 'class="record-meta record-meta-stack"')

    def test_short_labels_stay_short(self):
        """Every label in this strip is followed by its value, so a label that is
        itself a sentence renders as "NOBODY HAS CHECKED 1" with the number
        reading as a stray."""
        resource = _makeResource(name="Example Label Length Tool")
        _holder(resource, "Example Unchecked", role="Owner",
                canGrant=True, owns=True, confirmed=False)

        page = self.client.get(self.url).content.decode()
        labels = re.findall(r'class="record-meta-label">([^<]+)<', page)
        self.assertIn("Unchecked", labels)
        self.assertNotIn("Nobody has checked", labels)
        for label in labels:
            self.assertLessEqual(
                len(label), 20, f"label is too long for the uppercase slot: {label!r}")


@fastHashing
class HolderRosterLayoutTests(LoginClientMixin, TestCase):
    """The same sentence on the tool's OWN page, where it is one row about one
    person and so is worth showing in full - just not inside the label/value
    strip, which is for short facts laid out several to a line."""

    def setUp(self):
        self.loginAs(UserFactory.make("committee", perms=("viewChapterToolAudit",)))
        self.resource = _makeResource(name="Example Roster Layout Tool")
        _holder(self.resource, HOLDER_SENTINEL, role="Delegated user", canGrant=True)

    def test_the_power_summary_gets_its_own_line(self):
        response = self.client.get(self.resource.getUrl())
        self.assertContains(response, 'class="record-aside">Can change other people')

    def test_it_is_no_longer_labelled(self):
        """The badge in the card head already names the role and this line says
        what that role amounts to, so a label would be a third heading for one
        fact."""
        self.assertNotContains(
            self.client.get(self.resource.getUrl()), "What that means here")

    def test_the_role_the_service_uses_still_renders_beside_it(self):
        """The two are different answers and both belong on this page: the badge
        is the service's word, the aside is the chapter's reading of it."""
        response = self.client.get(self.resource.getUrl())
        self.assertContains(response, "Delegated user")


class StylesheetCacheBustingTests(TestCase):
    """The demo box serves static through runserver, which sends no
    Cache-Control and no ETag - so an unversioned URL lets a browser pair new
    HTML with a pre-deploy stylesheet. That is not hypothetical: it is what made
    a correctly deployed page look unstyled."""

    def setUp(self):
        _versionCache.clear()

    def tearDown(self):
        _versionCache.clear()

    def test_the_url_carries_a_content_version(self):
        url = versionedStatic("css/output.css")
        self.assertIn("output.css?v=", url)

    def test_the_version_is_stable_across_calls(self):
        self.assertEqual(versionedStatic("css/output.css"), versionedStatic("css/output.css"))

    def test_a_missing_file_degrades_to_a_plain_url(self):
        """An unversioned URL is merely today's behaviour; a 500 in the base
        template would take every page down."""
        url = versionedStatic("css/no-such-file-example.css")
        self.assertNotIn("?v=", url)
        self.assertIn("no-such-file-example.css", url)

    def test_the_rendered_page_links_the_versioned_stylesheet(self):
        """The tag is only useful if base.html actually uses it."""
        user = UserFactory.make("member")
        self.client.force_login(user)
        response = self.client.get(reverse("chapter-tools"))
        self.assertContains(response, "css/output.css?v=")
