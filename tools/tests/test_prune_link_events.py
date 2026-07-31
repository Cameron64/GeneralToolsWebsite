"""prune_link_events: retention for the Link Tree click/scan log.

LinkEvent is appended on every public click and QR scan (tracking.recordEvent)
and nothing else in the app ever deletes one, so without this command the table
grows without bound - the reachable-by-anyone availability problem, and the
"we kept visitor logs forever" data-minimization problem.

LinkEvent.occurredAt is auto_now_add, so tests age rows with a queryset
.update() after creation rather than passing occurredAt to create().
"""
import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from tools import tasks
from tools.models import LinkEvent, LinkTree, LinkTreeItem


def makeEvent(daysAgo=0, source=LinkEvent.Source.WEB):
    """A LinkEvent aged `daysAgo` days (occurredAt is auto_now_add)."""
    event = LinkEvent.objects.create(source=source, destinationUrl="https://example.org")
    if daysAgo:
        LinkEvent.objects.filter(id=event.id).update(
            occurredAt=timezone.now() - datetime.timedelta(days=daysAgo)
        )
    return event


class PruneLinkEventsCommandTests(TestCase):
    def runCommand(self, **kwargs):
        out = StringIO()
        call_command("prune_link_events", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_deletes_rows_older_than_the_window(self):
        stale = makeEvent(daysAgo=200)
        self.runCommand()
        self.assertFalse(LinkEvent.objects.filter(id=stale.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_keeps_rows_inside_the_window(self):
        fresh = makeEvent(daysAgo=10)
        recent = makeEvent(daysAgo=0)
        self.runCommand()
        self.assertTrue(LinkEvent.objects.filter(id=fresh.id).exists())
        self.assertTrue(LinkEvent.objects.filter(id=recent.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_boundary_row_is_kept(self):
        """The filter is occurredAt < cutoff, so a row exactly at the window
        edge is retained rather than deleted."""
        edge = makeEvent(daysAgo=179)
        self.runCommand()
        self.assertTrue(LinkEvent.objects.filter(id=edge.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_days_flag_overrides_the_setting(self):
        old = makeEvent(daysAgo=45)
        self.runCommand(days=30)
        self.assertFalse(LinkEvent.objects.filter(id=old.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_dry_run_deletes_nothing_and_reports_the_count(self):
        stale = makeEvent(daysAgo=200)
        makeEvent(daysAgo=201)
        output = self.runCommand(dry_run=True)
        self.assertTrue(LinkEvent.objects.filter(id=stale.id).exists())
        self.assertEqual(LinkEvent.objects.count(), 2)
        self.assertIn("DRY RUN", output)
        self.assertIn("2", output)

    @override_settings(LINK_EVENT_RETENTION_DAYS=0)
    def test_zero_retention_is_a_no_op_not_a_purge(self):
        """0 is the documented "keep everything" escape hatch.

        Treating it as a cutoff of "now" would silently delete the whole table,
        which is the opposite of what an operator disabling retention wants.
        """
        keep = makeEvent(daysAgo=5000)
        output = self.runCommand()
        self.assertTrue(LinkEvent.objects.filter(id=keep.id).exists())
        self.assertIn("disabled", output)

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_negative_days_is_rejected(self):
        keep = makeEvent(daysAgo=5000)
        with self.assertRaises(SystemExit):
            self.runCommand(days=-1)
        self.assertTrue(LinkEvent.objects.filter(id=keep.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_quiet_suppresses_the_summary(self):
        makeEvent(daysAgo=200)
        self.assertEqual(self.runCommand(quiet=True).strip(), "")

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_prunes_qr_scans_too(self):
        staleScan = makeEvent(daysAgo=200, source=LinkEvent.Source.QR)
        self.runCommand()
        self.assertFalse(LinkEvent.objects.filter(id=staleScan.id).exists())

    @override_settings(LINK_EVENT_RETENTION_DAYS=180)
    def test_does_not_touch_the_tree_or_item_rows(self):
        """Pruning the log must not cascade into the link tree itself."""
        tree = LinkTree.objects.create(slug="atx", title="Austin DSA")
        item = LinkTreeItem.objects.create(
            tree=tree, kind=LinkTreeItem.Kind.MANUAL, label="Join", url="https://example.org",
        )
        event = LinkEvent.objects.create(
            tree=tree, item=item, source=LinkEvent.Source.WEB,
            destinationUrl="https://example.org",
        )
        LinkEvent.objects.filter(id=event.id).update(
            occurredAt=timezone.now() - datetime.timedelta(days=200)
        )

        self.runCommand()

        self.assertFalse(LinkEvent.objects.filter(id=event.id).exists())
        self.assertTrue(LinkTree.objects.filter(id=tree.id).exists())
        self.assertTrue(LinkTreeItem.objects.filter(id=item.id).exists())


class PruneLinkEventsTaskTests(SimpleTestCase):
    """The Huey schedule around the command (see test_tasks.py for the idiom)."""

    def test_calls_command_quietly(self):
        with mock.patch("tools.tasks.call_command") as mockCall:
            tasks.pruneLinkEvents.call_local()
        mockCall.assert_called_once_with("prune_link_events", quiet=True)

    def test_error_exit_from_command_is_swallowed_and_logged(self):
        # A SystemExit inside the consumer must become a logged error, never an
        # attempt to exit the worker process.
        with mock.patch("tools.tasks.call_command", side_effect=SystemExit(1)):
            with self.assertLogs("tools.tasks", level="ERROR"):
                tasks.pruneLinkEvents.call_local()

    def test_clean_exit_from_command_is_silent(self):
        with mock.patch("tools.tasks.call_command", side_effect=SystemExit(0)):
            with self.assertNoLogs("tools.tasks", level="ERROR"):
                tasks.pruneLinkEvents.call_local()
