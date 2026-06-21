"""Close out resolutions whose filing deadline passed while still gathering.

A resolution gathers sign-ons until the Secretary schedules it onto a meeting
agenda. If its filing deadline (the target meeting's start minus the kind's lead
days) passes while it is still GATHERING, it can never make that agenda and
accepts no further sign-ons - a zombie. This sweep moves those to the terminal
LAPSED status so they drop off the active "sign onto a resolution" and On Deck
lists. A proponent who wants more time should re-target the resolution to a later
meeting before the deadline (see the resolution edit page); that resets the
deadline and keeps it gathering.

Resolutions with no target meeting have no deadline and are never lapsed.

Scheduled daily by Huey (tools/tasks.py lapseExpiredResolutions, run by the
`worker` service). This command remains the imperative core for manual runs and
--dry-run (and is the only way to run it in dev, where Huey's immediate mode
does not fire periodic schedules).

Run from the repo root:
    python manage.py lapse_expired_resolutions [--dry-run] [--quiet]
"""

import logging

from django.core.management.base import BaseCommand

from tools.models import Resolution

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Move GATHERING resolutions past their filing deadline to LAPSED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would lapse without changing anything.",
        )
        parser.add_argument(
            "--quiet", action="store_true",
            help="Suppress per-resolution output (still logs the summary).",
        )

    def handle(self, *args, **options):
        dryRun = options["dry_run"]
        quiet = options["quiet"]

        # The deadline is computed from the meeting (not a column), so evaluate
        # it in Python. The gathering set is small.
        expired = [
            r for r in (
                Resolution.objects
                .filter(status=Resolution.Status.GATHERING)
                .select_related("targetMeeting")
            )
            if r.signOnDeadlinePassed()
        ]

        for r in expired:
            if not quiet:
                verb = "Would lapse" if dryRun else "Lapsing"
                self.stdout.write(f"{verb} #{r.pk} {r.title!r} (deadline {r.getDeadlineStr()})")
            if not dryRun:
                r.markLapsed(actor=None, note="Filing deadline passed before reaching an agenda.")

        summary = f"{'Would lapse' if dryRun else 'Lapsed'} {len(expired)} resolution(s)."
        if not quiet:
            self.stdout.write(self.style.SUCCESS(summary))
        logger.info("lapse_expired_resolutions: %s (dry_run=%s)", summary, dryRun)
