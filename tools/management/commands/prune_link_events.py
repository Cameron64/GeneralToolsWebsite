"""Delete LinkEvent rows older than the retention window.

tools/LinkTree/tracking.py recordEvent() appends a LinkEvent on every public
click and QR scan, unconditionally and with no upper bound. Nothing else in the
app ever deletes one, so the table only grows. Two problems with that:

  * Availability. LinkEvent lives in the shared Django database, and the
    identifiers that reach the write path are public by construction - printed
    on QR codes and listed on public tree pages. Sustained requests to a valid
    /go or /qr URL grow the DB until writes start failing for the rest of the
    app. recordEvent swallowing its own exception protects the redirect, not
    everything else sharing that storage.
  * Data minimization. Even though the rows are deliberately privacy-first (a
    salted, daily-rotating visitorHash and no raw IP - see LinkEvent's
    docstring), keeping them forever is hard to defend. National's Membership
    List Guide asks that member-derived files be deleted regularly, and
    "indefinitely, because appending was cheap" is not a retention policy.

The window comes from settings.LINK_EVENT_RETENTION_DAYS (default 180, which
keeps season-over-season comparison). Deliberately a plain management command
with the aggregate metrics untouched: pruning raw events does not rewrite any
already-reported total, it only stops the log growing without bound.

Scheduled daily by Huey (tools/tasks.py pruneLinkEvents, run by the `worker`
service). This command remains the imperative core for manual runs and
--dry-run, and is the only way to run it in dev, where Huey's immediate mode
does not fire periodic schedules.

Run from the repo root:
    python manage.py prune_link_events [--days N] [--dry-run] [--quiet]
"""

import datetime
import logging

# django.conf.settings, not the bare `import settings` used in
# tools/EventAutomation and tools/tasks.py: this is the LinkTree feature's
# convention (tracking.recordEvent does the same), and it is the only form
# override_settings can reach, so the retention window stays testable.
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from tools.models import LinkEvent

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete Link Tree click/scan events older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help=(
                "Retention window in days; rows strictly older than this are "
                "deleted. Defaults to settings.LINK_EVENT_RETENTION_DAYS."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress the summary line (errors still print).",
        )

    def handle(self, *args, **options):
        dryRun = options["dry_run"]
        quiet = options["quiet"]
        days = options["days"]
        if days is None:
            days = getattr(settings, "LINK_EVENT_RETENTION_DAYS", 0)

        if days < 0:
            self.stderr.write(self.style.ERROR("FATAL: --days cannot be negative."))
            raise SystemExit(1)

        # 0 is the documented "keep everything" escape hatch. Treat it as a
        # no-op rather than as a cutoff of "now", which would delete the table.
        if days == 0:
            if not quiet:
                self.stdout.write(self.style.WARNING(
                    "LinkEvent retention is disabled (0 days) - nothing pruned. "
                    "Set LINK_EVENT_RETENTION_DAYS to enable pruning."
                ))
            return

        cutoff = timezone.now() - datetime.timedelta(days=days)
        stale = LinkEvent.objects.filter(occurredAt__lt=cutoff)

        if dryRun:
            count = stale.count()
            if not quiet:
                self.stdout.write(self.style.WARNING(
                    f"DRY RUN - would delete {count} LinkEvent row(s) older than "
                    f"{days} days (before {cutoff.isoformat()})."
                ))
            return

        # .delete() on the queryset issues the bulk DELETE and returns the total.
        # LinkEvent has no cascading children, so the total is the row count.
        deleted, _ = stale.delete()
        logger.info(
            "PruneLinkEvents: deleted %s LinkEvent row(s) older than %s days (before %s)",
            deleted, days, cutoff.isoformat(),
        )
        if not quiet:
            self.stdout.write(self.style.SUCCESS(
                f"Deleted {deleted} LinkEvent row(s) older than {days} days "
                f"(before {cutoff.isoformat()})."
            ))
