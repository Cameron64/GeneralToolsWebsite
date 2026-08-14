import datetime

from django.test import TestCase
from django.urls import reverse

from tools.linkTreeViews import QR_CODES_PER_PAGE
from tools.models import LinkEvent, LinkTree, LinkTreeItem, QRCode

from tools.tests.support import (
    LoginClientMixin, UserFactory, fastHashing, permission, refetchForPerms,
)


@fastHashing
class ManageLinkTreeTests(LoginClientMixin, TestCase):
    def setUp(self):
        self.maintainer = UserFactory.make("maintainer", perms=("manageLinkTree",))
        self.member = UserFactory.make("member")
        self.tree = LinkTree.objects.create(
            slug="links", title="Austin DSA", visibility=LinkTree.Visibility.PUBLIC
        )
        self.itemA = LinkTreeItem.objects.create(
            tree=self.tree, order=0, kind=LinkTreeItem.Kind.MANUAL,
            label="Join", url="https://example.org/join",
        )
        self.itemB = LinkTreeItem.objects.create(
            tree=self.tree, order=1, kind=LinkTreeItem.Kind.MANUAL,
            label="Donate", url="https://example.org/donate",
        )

    # --- gating ------------------------------------------------------------

    def test_plain_user_cannot_open_management_pages(self):
        self.loginAs(self.member)
        for name, kwargs in (
            ("manage-link-tree-list", {}),
            ("manage-link-tree-edit", {"treeId": self.tree.id}),
            ("manage-qr-code-list", {}),
            ("manage-qr-code-new", {}),
        ):
            resp = self.client.get(reverse(name, kwargs=kwargs))
            self.assertEqual(resp.status_code, 302, name)  # bounced by permission_required

    def test_maintainer_without_is_staff_can_manage(self):
        self.assertFalse(self.maintainer.is_staff)
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-link-tree-list"))
        self.assertEqual(resp.status_code, 200)

    # --- list / create -----------------------------------------------------

    def test_list_shows_trees(self):
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-link-tree-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Austin DSA")
        self.assertContains(resp, "/t/links/")
        self.assertContains(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))

    def test_create_tree_with_duplicate_slug_fails(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-link-tree-new"), {
            "slug": "links", "title": "Dupe", "visibility": LinkTree.Visibility.PUBLIC, "isActive": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertEqual(LinkTree.objects.filter(slug="links").count(), 1)

    def test_edit_tree_slug_to_existing_slug_fails(self):
        other = LinkTree.objects.create(slug="other", title="Other")
        self.loginAs(self.maintainer)
        resp = self.client.post(
            reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}),
            {"slug": "other", "title": "Austin DSA", "visibility": LinkTree.Visibility.PUBLIC, "isActive": "on"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.tree.refresh_from_db()
        self.assertEqual(self.tree.slug, "links")  # unchanged

    def test_create_and_edit_tree_settings(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-link-tree-new"), {
            "slug": "newsletter", "title": "Newsletter",
            "visibility": LinkTree.Visibility.MEMBERS, "isActive": "on",
        })
        created = LinkTree.objects.get(slug="newsletter")
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": created.id}))
        self.assertEqual(created.visibility, LinkTree.Visibility.MEMBERS)
        self.assertTrue(created.isActive)

        # Edit it: flip visibility + deactivate.
        resp = self.client.post(
            reverse("manage-link-tree-edit", kwargs={"treeId": created.id}),
            {"slug": "newsletter", "title": "Newsletter",
             "visibility": LinkTree.Visibility.PUBLIC},  # isActive omitted -> False
        )
        self.assertEqual(resp.status_code, 200)
        created.refresh_from_db()
        self.assertEqual(created.visibility, LinkTree.Visibility.PUBLIC)
        self.assertFalse(created.isActive)

    # --- items -------------------------------------------------------------

    def test_add_item_manual_requires_url(self):
        self.loginAs(self.maintainer)
        url = reverse("manage-link-tree-item-new", kwargs={"treeId": self.tree.id})
        # Blank url -> non-field error, no item created.
        resp = self.client.post(url, {
            "kind": LinkTreeItem.Kind.MANUAL, "order": 5, "label": "Bad", "isActive": "on",
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "manual link needs a destination URL")
        self.assertFalse(LinkTreeItem.objects.filter(label="Bad").exists())

        # With a url -> created and destinationUrl set.
        resp = self.client.post(url, {
            "kind": LinkTreeItem.Kind.MANUAL, "order": 5, "label": "Good",
            "url": "https://example.org/good", "isActive": "on",
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH,
        })
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        item = LinkTreeItem.objects.get(label="Good")
        self.assertEqual(item.destinationUrl(), "https://example.org/good")

    def test_wiki_item_not_resolved_at_request_time(self):
        self.loginAs(self.maintainer)
        url = reverse("manage-link-tree-item-new", kwargs={"treeId": self.tree.id})
        resp = self.client.post(url, {
            "kind": LinkTreeItem.Kind.WIKI, "order": 9, "label": "Latest GBM agenda",
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH, "wikiQuery": "GBM Agenda",
            "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        item = LinkTreeItem.objects.get(label="Latest GBM agenda")
        self.assertEqual(item.kind, LinkTreeItem.Kind.WIKI)
        self.assertEqual(item.wikiQuery, "GBM Agenda")
        # The UI never resolves wiki items - the cache stays empty.
        self.assertEqual(item.resolvedUrl, "")
        self.assertIsNone(item.resolvedAt)

    def test_reorder_items(self):
        self.loginAs(self.maintainer)
        # Submit reversed order; also slip in a foreign item id to prove the
        # cross-tree tamper guard ignores it.
        otherTree = LinkTree.objects.create(slug="other", title="Other")
        foreign = LinkTreeItem.objects.create(
            tree=otherTree, order=0, kind=LinkTreeItem.Kind.MANUAL,
            label="Foreign", url="https://example.org/x",
        )
        resp = self.client.post(
            reverse("manage-link-tree-item-reorder", kwargs={"treeId": self.tree.id}),
            {"itemOrder": [self.itemB.id, self.itemA.id, foreign.id]},
        )
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.itemA.refresh_from_db()
        self.itemB.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.itemB.order, 0)
        self.assertEqual(self.itemA.order, 1)
        self.assertEqual(foreign.order, 0)  # untouched - belongs to another tree

    def test_reorder_renumbers_all_items_densely(self):
        # Non-consecutive starting orders; submit only 2 of 3 ids.
        self.itemA.order = 10
        self.itemA.save()
        self.itemB.order = 20
        self.itemB.save()
        itemC = LinkTreeItem.objects.create(
            tree=self.tree, order=30, kind=LinkTreeItem.Kind.MANUAL,
            label="Volunteer", url="https://example.org/vol",
        )
        self.loginAs(self.maintainer)
        self.client.post(
            reverse("manage-link-tree-item-reorder", kwargs={"treeId": self.tree.id}),
            {"itemOrder": [itemC.id, self.itemA.id]},  # itemB omitted
        )
        self.itemA.refresh_from_db()
        self.itemB.refresh_from_db()
        itemC.refresh_from_db()
        # Submitted ids first in submitted order, unlisted item last.
        self.assertEqual(itemC.order, 0)
        self.assertEqual(self.itemA.order, 1)
        self.assertEqual(self.itemB.order, 2)

    def test_reorder_empty_post_is_a_noop(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(
            reverse("manage-link-tree-item-reorder", kwargs={"treeId": self.tree.id}),
            {"itemOrder": []},
        )
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.itemA.refresh_from_db()
        self.itemB.refresh_from_db()
        self.assertEqual(self.itemA.order, 0)
        self.assertEqual(self.itemB.order, 1)

    def test_deactivate_item(self):
        self.loginAs(self.maintainer)
        url = reverse("manage-link-tree-item-edit", kwargs={"treeId": self.tree.id, "itemId": self.itemA.id})
        # isActive omitted from POST -> unchecked -> False.
        resp = self.client.post(url, {
            "kind": LinkTreeItem.Kind.MANUAL, "order": 0, "label": "Join",
            "url": "https://example.org/join",
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH,
        })
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.itemA.refresh_from_db()
        self.assertFalse(self.itemA.isActive)
        # Public page no longer shows it.
        public = self.client.get(reverse("link-tree", kwargs={"slug": "links"}))
        self.assertNotContains(public, "Join")

    def test_kind_switch_clears_stale_cross_kind_data(self):
        # MANUAL item with a url; edit it to WIKI without resending url. clean()
        # must clear the url.
        self.loginAs(self.maintainer)
        url = reverse("manage-link-tree-item-edit", kwargs={"treeId": self.tree.id, "itemId": self.itemA.id})
        resp = self.client.post(url, {
            "kind": LinkTreeItem.Kind.WIKI, "order": 0, "label": "Join",
            "url": "https://example.org/join",  # stale value still posted
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH, "wikiQuery": "Join Us",
            "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.itemA.refresh_from_db()
        self.assertEqual(self.itemA.kind, LinkTreeItem.Kind.WIKI)
        self.assertEqual(self.itemA.url, "")  # cleared by clean()
        self.assertEqual(self.itemA.wikiQuery, "Join Us")

    def test_visibleFrom_round_trips_as_utc(self):
        self.loginAs(self.maintainer)
        # Seed an item with a UTC-aware visibleFrom.
        when = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
        self.itemA.visibleFrom = when
        self.itemA.save()
        # GET the edit page: the initial value renders without a tz shift.
        editUrl = reverse("manage-link-tree-item-edit", kwargs={"treeId": self.tree.id, "itemId": self.itemA.id})
        resp = self.client.get(editUrl)
        self.assertContains(resp, "2026-06-01T12:00")
        # POST the same string back; the stored value is the original UTC value.
        resp = self.client.post(editUrl, {
            "kind": LinkTreeItem.Kind.MANUAL, "order": 0, "label": "Join",
            "url": "https://example.org/join", "visibleFrom": "2026-06-01T12:00",
            "wikiMode": LinkTreeItem.WikiMode.LATEST_MATCH, "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.itemA.refresh_from_db()
        self.assertEqual(self.itemA.visibleFrom, when)

    # --- QR codes ----------------------------------------------------------

    def test_create_qr_requires_exactly_one_target(self):
        self.loginAs(self.maintainer)
        url = reverse("manage-qr-code-new")
        # Zero targets.
        resp = self.client.post(url, {"code": "zero", "label": "Zero", "isActive": "on"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "exactly one target")
        self.assertFalse(QRCode.objects.filter(code="zero").exists())

        # Two targets.
        resp = self.client.post(url, {
            "code": "two", "label": "Two", "tree": self.tree.id, "rawUrl": "https://x.org",
            "isActive": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "exactly one target")
        self.assertFalse(QRCode.objects.filter(code="two").exists())

        # One target (item).
        resp = self.client.post(url, {
            "code": "one", "label": "One", "item": self.itemA.id, "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        qr = QRCode.objects.get(code="one")
        self.assertEqual(qr.createdBy, self.maintainer)
        self.assertEqual(qr.scanUrl(), reverse("qr-redirect", kwargs={"code": "one"}))
        self.assertEqual(qr.targetUrl(), "https://example.org/join")

    # The four traps a first-time member hit on this form. Each of these used to
    # be a dead end with either no message or Django's "Enter a valid 'slug'".

    def test_create_qr_accepts_a_web_address_with_no_scheme(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Canvass flyer", "targetKind": "url",
            "rawUrl": "austindsa.org/canvass", "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        qr = QRCode.objects.get(code="canvass-flyer")
        self.assertEqual(qr.rawUrl, "https://austindsa.org/canvass")  # https, not http

    def test_create_qr_slugifies_a_human_typed_code_instead_of_rejecting_it(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Anything", "code": "Summer Mutual Aid!", "targetKind": "url",
            "rawUrl": "https://example.org", "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        self.assertTrue(QRCode.objects.filter(code="summer-mutual-aid").exists())

    def test_create_qr_builds_the_code_from_the_label_when_left_blank(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Spring 2026 Tabling", "targetKind": "tree",
            "tree": self.tree.id, "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        self.assertTrue(QRCode.objects.filter(code="spring-2026-tabling").exists())

    def test_answering_the_target_question_ignores_the_other_target_fields(self):
        # A member who fills a field, changes their mind, and picks a different
        # target should not be told they chose two targets.
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Changed Mind", "targetKind": "url", "rawUrl": "example.org",
            "tree": self.tree.id, "item": self.itemA.id, "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        qr = QRCode.objects.get(code="changed-mind")
        self.assertEqual(qr.rawUrl, "https://example.org")
        self.assertIsNone(qr.tree)
        self.assertIsNone(qr.item)

    def test_answering_the_target_question_then_leaving_it_blank_errors_on_that_field(self):
        self.loginAs(self.maintainer)
        for kind, expected in (
            ("url", "Enter the web address this code should open."),
            ("tree", "Choose the link tree this code should open."),
        ):
            resp = self.client.post(reverse("manage-qr-code-new"), {
                "label": "Blank Target", "targetKind": kind, "isActive": "on",
            })
            self.assertEqual(resp.status_code, 200, kind)
            self.assertContains(resp, expected)
            self.assertFalse(QRCode.objects.filter(code="blank-target").exists(), kind)

    # --- the tree branch resolves to a tree OR one of its items -------------

    def test_tree_branch_with_no_link_chosen_targets_the_whole_page(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Whole Page", "targetKind": "tree",
            "tree": self.tree.id, "item": "", "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        qr = QRCode.objects.get(code="whole-page")
        self.assertEqual(qr.tree, self.tree)
        self.assertIsNone(qr.item)

    def test_tree_branch_with_a_link_chosen_targets_just_that_link(self):
        # Narrowing to one link replaces the tree as the target - QRCode allows
        # exactly one, and the item already knows which tree it belongs to.
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Donate Link", "targetKind": "tree",
            "tree": self.tree.id, "item": self.itemB.id, "isActive": "on",
        })
        self.assertRedirects(resp, reverse("manage-qr-code-list"))
        qr = QRCode.objects.get(code="donate-link")
        self.assertEqual(qr.item, self.itemB)
        self.assertIsNone(qr.tree)
        self.assertEqual(qr.targetUrl(), "https://example.org/donate")

    def test_tree_branch_rejects_a_link_from_a_different_tree(self):
        other = LinkTree.objects.create(
            slug="members", title="Members", visibility=LinkTree.Visibility.PUBLIC
        )
        strayItem = LinkTreeItem.objects.create(
            tree=other, order=0, kind=LinkTreeItem.Kind.MANUAL,
            label="Agenda", url="https://example.org/agenda",
        )
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "label": "Mismatch", "targetKind": "tree",
            "tree": self.tree.id, "item": strayItem.id, "isActive": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "is not in Austin DSA")
        self.assertFalse(QRCode.objects.filter(code="mismatch").exists())

    def test_edit_page_opens_an_item_target_on_its_parent_tree(self):
        qr = QRCode.objects.create(code="agenda-qr", label="Agenda QR", item=self.itemA)
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-edit", kwargs={"code": qr.code}))
        form = resp.context["form"]
        self.assertEqual(form.initial["targetKind"], "tree")
        self.assertEqual(form.initial["tree"], self.tree.id)
        self.assertEqual(form.initial["item"], self.itemA.id)

    def test_qr_form_tags_each_link_option_with_its_tree(self):
        # The two-step link picker narrows the item dropdown client-side using
        # these attributes, so losing them silently reverts it to one long list.
        other = LinkTree.objects.create(
            slug="members", title="Members", visibility=LinkTree.Visibility.PUBLIC
        )
        otherItem = LinkTreeItem.objects.create(
            tree=other, order=0, kind=LinkTreeItem.Kind.MANUAL,
            label="Agenda", url="https://example.org/agenda",
        )
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-new"))
        self.assertContains(resp, f'data-tree="{self.tree.id}"')
        self.assertContains(resp, f'data-tree="{other.id}"')
        self.assertContains(resp, f'value="{otherItem.id}"')

    def test_qr_form_suggests_campaign_tags_already_in_use(self):
        QRCode.objects.create(code="a", label="A", tree=self.tree, campaign="table-tent")
        QRCode.objects.create(code="b", label="B", tree=self.tree, campaign="flyer")
        QRCode.objects.create(code="c", label="C", tree=self.tree, campaign="flyer")
        QRCode.objects.create(code="d", label="D", tree=self.tree, campaign="")
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-new"))
        body = resp.content.decode()
        self.assertIn('<datalist id="campaignTagOptions">', body)
        self.assertEqual(body.count('<option value="flyer">'), 1)  # deduped
        self.assertIn('<option value="table-tent">', body)

    def test_create_qr_with_duplicate_code_fails(self):
        QRCode.objects.create(code="flyer", label="Flyer", tree=self.tree)
        self.loginAs(self.maintainer)
        resp = self.client.post(reverse("manage-qr-code-new"), {
            "code": "flyer", "label": "Dupe", "tree": self.tree.id, "isActive": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertEqual(QRCode.objects.filter(code="flyer").count(), 1)

    def test_edit_qr_code_to_existing_code_fails(self):
        a = QRCode.objects.create(code="aaa", label="A", tree=self.tree)
        QRCode.objects.create(code="bbb", label="B", tree=self.tree)
        self.loginAs(self.maintainer)
        resp = self.client.post(
            reverse("manage-qr-code-edit", kwargs={"code": "aaa"}),
            {"code": "bbb", "label": "A", "tree": self.tree.id, "isActive": "on"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        a.refresh_from_db()
        self.assertEqual(a.code, "aaa")  # unchanged

    def test_qr_list_renders_scan_url_and_image_links(self):
        qr = QRCode.objects.create(code="tabling", label="Tabling", tree=self.tree)
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("qr-image", kwargs={"code": qr.code}))
        self.assertContains(resp, reverse("qr-redirect", kwargs={"code": qr.code}))

    def test_qr_list_renders_placeholder_for_unresolved_wiki_item(self):
        wikiItem = LinkTreeItem.objects.create(
            tree=self.tree, order=2, kind=LinkTreeItem.Kind.WIKI,
            label="Agenda", wikiQuery="GBM Agenda",  # unresolved
        )
        QRCode.objects.create(code="agenda", label="Agenda QR", item=wikiItem)
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-list"))
        self.assertContains(resp, "Not resolved yet")

    def test_qr_list_shows_no_codes_message_when_empty(self):
        self.loginAs(self.maintainer)
        resp = self.client.get(reverse("manage-qr-code-list"))
        self.assertContains(resp, "No QR codes yet")
        self.assertNotContains(resp, "qr-card")

    def test_qr_list_splits_across_pages(self):
        for i in range(QR_CODES_PER_PAGE + 3):
            QRCode.objects.create(code=f"code-{i:02d}", label=f"Code {i:02d}", tree=self.tree)
        self.loginAs(self.maintainer)

        # class="qr-card" (with the closing quote) - not "qr-card-header" etc,
        # which also contain "qr-card" as a substring - so this counts cards,
        # not every class name that happens to be prefixed with it.
        firstPage = self.client.get(reverse("manage-qr-code-list"))
        self.assertContains(firstPage, 'class="qr-card"', count=QR_CODES_PER_PAGE)
        self.assertContains(firstPage, "Page 1 of 2")
        self.assertContains(firstPage, "?page=2")

        secondPage = self.client.get(reverse("manage-qr-code-list"), {"page": 2})
        self.assertContains(secondPage, 'class="qr-card"', count=3)
        self.assertContains(secondPage, "Page 2 of 2")
        self.assertNotContains(secondPage, "?page=3")  # no Next past the last page

    def test_qr_list_out_of_range_page_clamps_instead_of_erroring(self):
        QRCode.objects.create(code="only-one", label="Only One", tree=self.tree)
        self.loginAs(self.maintainer)

        tooHigh = self.client.get(reverse("manage-qr-code-list"), {"page": 999})
        self.assertEqual(tooHigh.status_code, 200)
        self.assertContains(tooHigh, "Only One")  # clamped to the last real page

        notANumber = self.client.get(reverse("manage-qr-code-list"), {"page": "banana"})
        self.assertEqual(notANumber.status_code, 200)
        self.assertContains(notANumber, "Only One")  # clamped to page 1

    # --- no open redirect --------------------------------------------------

    def test_no_open_redirect_in_reorder(self):
        self.loginAs(self.maintainer)
        resp = self.client.post(
            reverse("manage-link-tree-item-reorder", kwargs={"treeId": self.tree.id}) + "?next=https://evil.example.com",
            {"itemOrder": [self.itemB.id, self.itemA.id]},
        )
        self.assertRedirects(resp, reverse("manage-link-tree-edit", kwargs={"treeId": self.tree.id}))
        self.assertNotIn("evil.example.com", resp["Location"])
