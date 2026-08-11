"""Render the Chapter Tools registry to Markdown (and optionally CSV) as an
out-of-band continuity copy.

Echo currently runs on a personal laptop, so a login-gated app page is not
enough of a break-glass artifact by itself - this command exists so the
registry stays readable even if the app is unreachable. See the plan's
"Continuity" section: the wiki page stays authoritative until this export
exists and has a home; only then does the wiki become a pointer to
/chapter-tools.

    python manage.py export_chapter_tools --out /abs/path/registry.md
    python manage.py export_chapter_tools --out registry.md --csv-out registry.csv
    python manage.py export_chapter_tools                      # Markdown to stdout

The Markdown is split into the open layer (what any logged-in member sees in
the app) and the restricted layer (delegation tier, revocation/continuity
notes, credential objects, review/staleness detail, open questions) - the
same split the app enforces behind viewChapterToolAudit. Anyone who can run
this management command already has shell access to the underlying database,
so both layers are always included here; there is no open-only mode.
"""
import csv

from django.core.management.base import BaseCommand
from django.utils import timezone as djangoTimezone

from tools.models import ChapterResource


class Command(BaseCommand):
    help = (
        "Export the Chapter Tools registry to Markdown (open + restricted layers), "
        "and optionally CSV, for an out-of-band continuity copy."
    )

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Write the Markdown export here instead of stdout.")
        parser.add_argument("--csv-out", help="Also write a flat one-row-per-resource CSV here.")

    def handle(self, *args, **options):
        resources = (
            ChapterResource.objects.prefetch_related("holders", "credentials", "questions")
            .order_by("category", "name")
        )
        markdown = self._renderMarkdown(resources)

        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as fileHandle:
                fileHandle.write(markdown)
            self.stdout.write(self.style.SUCCESS(f"Wrote {options['out']}"))
        else:
            self.stdout.write(markdown)

        if options["csv_out"]:
            self._writeCsv(resources, options["csv_out"])
            self.stdout.write(self.style.SUCCESS(f"Wrote {options['csv_out']}"))

    def _renderMarkdown(self, resources) -> str:
        lines = [
            "# Chapter Tools registry export",
            "",
            f"Generated {djangoTimezone.now().strftime('%Y-%m-%d %H:%M UTC')}. "
            "Out-of-band continuity copy - see /chapter-tools for the live version.",
            "",
            "## Open layer",
            "",
        ]
        for resource in resources:
            holderNames = ", ".join(
                f"{holder.getDisplayName()}{'' if holder.confirmed else ' (unconfirmed)'}"
                for holder in resource.holders.all()
            ) or "None recorded"
            lines += [
                f"### {resource.name}",
                f"- Category: {resource.get_category_display()}",
                f"- Blurb: {resource.blurb or '-'}",
                f"- Access model: {resource.get_accessModel_display()}",
                f"- Holders: {holderNames}",
                f"- Payer: {resource.get_payer_display()}"
                + (f" ({resource.costNote})" if resource.costNote else ""),
                f"- How to get access: {resource.howToGetAccess or '-'}",
                "",
            ]

        lines += ["## Restricted layer", ""]
        for resource in resources:
            staleNote = " **(STALE)**" if resource.isStale() else ""
            lines += [
                f"### {resource.name}",
                f"- Delegation tier: {resource.get_delegationTier_display()}",
                f"- Last reviewed: {resource.lastReviewed or 'Never'}{staleNote} "
                f"by {resource.reviewedBy or '-'}",
                f"- Revocation note: {resource.revocationNote or '-'}",
                f"- Continuity note: {resource.continuityNote or '-'}",
            ]
            credentials = list(resource.credentials.all())
            if credentials:
                lines.append("- Credentials:")
                for credential in credentials:
                    vaultSuffix = f", vault: {credential.vaultCollection}" if credential.vaultCollection else ""
                    lines.append(
                        f"  - {credential.label} ({credential.get_kind_display()}, "
                        f"{credential.get_status_display()}{vaultSuffix})"
                    )
            openQuestions = [question for question in resource.questions.all() if not question.isResolved()]
            if openQuestions:
                lines.append("- Open questions:")
                for question in openQuestions:
                    assigneeSuffix = f" (assigned: {question.assignedTo})" if question.assignedTo else ""
                    lines.append(f"  - {question.question}{assigneeSuffix}")
            lines.append("")

        return "\n".join(lines)

    def _writeCsv(self, resources, path):
        with open(path, "w", encoding="utf-8", newline="") as fileHandle:
            writer = csv.writer(fileHandle)
            writer.writerow([
                "name", "category", "blurb", "accessModel", "payer", "costNote",
                "howToGetAccess", "requestable", "delegationTier", "lastReviewed",
                "reviewedBy", "revocationNote", "continuityNote",
            ])
            for resource in resources:
                writer.writerow([
                    resource.name, resource.get_category_display(), resource.blurb,
                    resource.get_accessModel_display(), resource.get_payer_display(), resource.costNote,
                    resource.howToGetAccess, resource.requestable, resource.get_delegationTier_display(),
                    resource.lastReviewed or "", resource.reviewedBy, resource.revocationNote,
                    resource.continuityNote,
                ])
