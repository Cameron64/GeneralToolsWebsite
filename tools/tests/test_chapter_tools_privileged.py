"""The register-wide privileged-access page, and stylesheet cache-busting.

All data here is invented - never real chapter services, credentials, or people.

The page answers a question no single resource page can: where is the chapter one
person away from losing something. The tests below are mostly about the FINDINGS,
because the findings are the product - a page that lists holders correctly but
mislabels "nobody recorded" as healthy is worse than no page.
"""
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
        self.assertEqual(len(row["privileged"]), 1)
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
