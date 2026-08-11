"""Idempotently load a Chapter Tools (IT access registry) inventory JSON file.

The real chapter inventory must never enter this repo - it is public on
GitHub. The seed file lives entirely outside the tree and is passed in by
absolute path:

    python manage.py seed_chapter_tools --file "/abs/path/to/chapter-tools.json"

JSON schema (top-level keys - "resources" is the only required one):

{
  "resources": [
    {
      "name": "Example Wiki",                  // required, unique - the upsert key
      "blurb": "Chapter knowledge base.",
      "category": "COMMUNICATION",             // ChapterResource.Category member name
      "accessModel": "SHARED_VAULT",           // ChapterResource.AccessModel member name
      "payer": "CHAPTER",                      // ChapterResource.Payer member name
      "annualCost": "60.00",                   // string or number, optional
      "costNote": "",
      "howToGetAccess": "Ask in #it-committee",
      "siteUrl": "https://wiki.example.org",   // where the thing lives
      "accessRequestUrl": "https://form.example.org",  // the link that starts a request today
      "stewardUsername": "",                   // optional - resolves to an existing Echo User
      "stewardName": "Jordan Steward",         // text fallback when there's no Echo account
      "requestable": false,
      "lastReviewed": "2026-05-01",            // ISO date, optional
      "reviewedBy": "Jordan Steward",
      "delegationTier": "YELLOW",              // ChapterResource.DelegationTier member name
      "revocationNote": "",
      "continuityNote": "",
      "credentials": [
        {
          "label": "Shared login",
          "kind": "VAULT_SHARED_LOGIN",         // ResourceCredential.Kind member name
          "vaultCollection": "wiki-admins",
          "status": "LIVE",                     // ResourceCredential.Status member name
          "note": ""
        }
      ],
      "holders": [
        {
          "personName": "Jordan Steward",
          "userUsername": "",                   // optional - resolves to an existing Echo User
          "how": "VAULT_COLLECTION",             // ResourceHolder.How member name
          "confirmed": true,
          "note": ""
        }
      ],
      "dependencies": [
        {
          "dependsOn": "Slack",                 // another resource's name, in this same file
          "kind": "SIGN_IN",                    // ResourceDependency.Kind member name: SIGN_IN | RUNS_ON
          "note": ""
        }
      ],
      "questions": [
        {"question": "Who else has recovery codes?", "assignedTo": "", "resolution": ""}
      ]
    }
  ],
  "questions": [
    // Chapter-wide questions (no resource) - same shape as a resource's questions.
    {"question": "Is there a break-glass doc?", "assignedTo": "", "resolution": ""}
  ]
}

Upsert rule: ChapterResource/ResourceCredential/ResourceHolder rows are
upserted (update_or_create) keyed on their natural identity (resource name;
resource+label; resource+personName) - the JSON is treated as the current
source of truth for hand-curated inventory facts, so re-running refreshes
them. ResourceQuestion rows are create-only, keyed on (resource, question
text): the questions workbench is a live meeting tool where assign/resolve
happen in the app, so re-seeding must never clobber progress already made
there.

Dependencies are wired in a SECOND PASS, after every resource in the file
exists, because an edge names its target by name and a one-pass loader would
fail on any file that mentions a target before defining it - i.e. it would
depend on key order, which JSON does not promise and a human editing the file
should not have to think about. An edge naming a resource that is not in the
file is a hard error, not a skip: silently dropping it would leave a registry
that looks complete and is not, which is the exact failure this whole thing
exists to fix.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tools.models import (
    ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder, ResourceQuestion, User,
)

CATEGORY_BY_NAME = {
    "COMMUNICATION": ChapterResource.Category.COMMUNICATION,
    "INFRASTRUCTURE": ChapterResource.Category.INFRASTRUCTURE,
    "FINANCE": ChapterResource.Category.FINANCE,
    "SOCIAL": ChapterResource.Category.SOCIAL,
    "ORGANIZING": ChapterResource.Category.ORGANIZING,
}
ACCESS_MODEL_BY_NAME = {
    "INDIVIDUAL": ChapterResource.AccessModel.INDIVIDUAL,
    "SHARED_VAULT": ChapterResource.AccessModel.SHARED_VAULT,
    "SERVICE_ACCOUNT": ChapterResource.AccessModel.SERVICE_ACCOUNT,
    "MIXED": ChapterResource.AccessModel.MIXED,
    "UNCONFIRMED": ChapterResource.AccessModel.UNCONFIRMED,
}
PAYER_BY_NAME = {
    "CHAPTER": ChapterResource.Payer.CHAPTER,
    "NATIONAL": ChapterResource.Payer.NATIONAL,
    "MIXED": ChapterResource.Payer.MIXED,
    "FREE": ChapterResource.Payer.FREE,
    "UNCONFIRMED": ChapterResource.Payer.UNCONFIRMED,
}
DELEGATION_TIER_BY_NAME = {
    "GREEN": ChapterResource.DelegationTier.GREEN,
    "YELLOW": ChapterResource.DelegationTier.YELLOW,
    "RED": ChapterResource.DelegationTier.RED,
    "UNCLASSIFIED": ChapterResource.DelegationTier.UNCLASSIFIED,
}
CREDENTIAL_KIND_BY_NAME = {
    "INDIVIDUAL_LOGIN": ResourceCredential.Kind.INDIVIDUAL_LOGIN,
    "VAULT_SHARED_LOGIN": ResourceCredential.Kind.VAULT_SHARED_LOGIN,
    "SERVICE_ACCOUNT_KEY": ResourceCredential.Kind.SERVICE_ACCOUNT_KEY,
    "API_TOKEN": ResourceCredential.Kind.API_TOKEN,
    "TWO_FACTOR_TOKEN": ResourceCredential.Kind.TWO_FACTOR_TOKEN,
}
CREDENTIAL_STATUS_BY_NAME = {
    "LIVE": ResourceCredential.Status.LIVE,
    "RETIRE_CANDIDATE": ResourceCredential.Status.RETIRE_CANDIDATE,
    "RETIRED": ResourceCredential.Status.RETIRED,
}
HOLDER_HOW_BY_NAME = {
    "INDIVIDUAL_LOGIN": ResourceHolder.How.INDIVIDUAL_LOGIN,
    "VAULT_COLLECTION": ResourceHolder.How.VAULT_COLLECTION,
    "SERVICE_ACCOUNT": ResourceHolder.How.SERVICE_ACCOUNT,
}
# No default kind, matching the model: a forgotten "kind" must fail loudly
# rather than quietly pick one. Guessing SIGN_IN would publish a restricted
# RUNS_ON edge to the open directory; guessing RUNS_ON would hide a sign-in
# precondition members need. Both silent failures are worse than a KeyError.
DEPENDENCY_KIND_BY_NAME = {
    "SIGN_IN": ResourceDependency.Kind.SIGN_IN,
    "RUNS_ON": ResourceDependency.Kind.RUNS_ON,
}


class Command(BaseCommand):
    help = (
        "Idempotently load a Chapter Tools registry JSON file (absolute path only - "
        "never commit real chapter data to this repo)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Absolute path to the registry JSON file.")

    @transaction.atomic
    def handle(self, *args, **options):
        filePath = options["file"]
        try:
            with open(filePath, "r", encoding="utf-8") as fileHandle:
                data = json.load(fileHandle)
        except OSError as err:
            raise CommandError(f"Could not read {filePath}: {err}")
        except json.JSONDecodeError as err:
            raise CommandError(f"Invalid JSON in {filePath}: {err}")

        resourceSpecs = data.get("resources", [])
        for resourceSpec in resourceSpecs:
            self._upsertResource(resourceSpec)
        resourceCount = len(resourceSpecs)

        # Second pass - see the module docstring for why edges cannot be wired
        # inline with their resource.
        dependencyCount = 0
        for resourceSpec in resourceSpecs:
            dependencyCount += self._wireDependencies(resourceSpec)

        newChapterWideQuestions = 0
        for questionSpec in data.get("questions", []):
            _, created = self._getOrCreateQuestion(None, questionSpec)
            if created:
                newChapterWideQuestions += 1

        self.stdout.write(self.style.SUCCESS(
            f"Loaded {resourceCount} resource(s); wired {dependencyCount} dependency edge(s); "
            f"added {newChapterWideQuestions} new chapter-wide question(s)."
        ))

    def _upsertResource(self, spec):
        steward = None
        stewardUsername = spec.get("stewardUsername") or ""
        if stewardUsername:
            steward = User.objects.filter(username=stewardUsername).first()

        # spec.get("annualCost") or None would collapse a real 0 annual cost
        # to None (0 is falsy) - only missing/blank should map to None.
        annualCost = spec.get("annualCost")
        if annualCost == "":
            annualCost = None

        resource, _ = ChapterResource.objects.update_or_create(
            name=spec["name"],
            defaults={
                "blurb": spec.get("blurb", ""),
                "category": CATEGORY_BY_NAME[spec.get("category", "ORGANIZING")],
                "accessModel": ACCESS_MODEL_BY_NAME[spec.get("accessModel", "UNCONFIRMED")],
                "payer": PAYER_BY_NAME[spec.get("payer", "UNCONFIRMED")],
                "annualCost": annualCost,
                "costNote": spec.get("costNote", ""),
                "howToGetAccess": spec.get("howToGetAccess", ""),
                "siteUrl": spec.get("siteUrl", ""),
                "accessRequestUrl": spec.get("accessRequestUrl", ""),
                "steward": steward,
                "stewardName": spec.get("stewardName", ""),
                "requestable": spec.get("requestable", False),
                "lastReviewed": spec.get("lastReviewed") or None,
                "reviewedBy": spec.get("reviewedBy", ""),
                "delegationTier": DELEGATION_TIER_BY_NAME[spec.get("delegationTier", "UNCLASSIFIED")],
                "revocationNote": spec.get("revocationNote", ""),
                "continuityNote": spec.get("continuityNote", ""),
            },
        )

        for credentialSpec in spec.get("credentials", []):
            ResourceCredential.objects.update_or_create(
                resource=resource,
                label=credentialSpec["label"],
                defaults={
                    "kind": CREDENTIAL_KIND_BY_NAME[credentialSpec["kind"]],
                    "vaultCollection": credentialSpec.get("vaultCollection", ""),
                    "status": CREDENTIAL_STATUS_BY_NAME[credentialSpec.get("status", "LIVE")],
                    "note": credentialSpec.get("note", ""),
                },
            )

        for holderSpec in spec.get("holders", []):
            holderUser = None
            holderUsername = holderSpec.get("userUsername") or ""
            if holderUsername:
                holderUser = User.objects.filter(username=holderUsername).first()
            ResourceHolder.objects.update_or_create(
                resource=resource,
                personName=holderSpec["personName"],
                defaults={
                    "user": holderUser,
                    "how": HOLDER_HOW_BY_NAME[holderSpec.get("how", "INDIVIDUAL_LOGIN")],
                    "confirmed": holderSpec.get("confirmed", False),
                    "note": holderSpec.get("note", ""),
                },
            )

        for questionSpec in spec.get("questions", []):
            self._getOrCreateQuestion(resource, questionSpec)

    def _wireDependencies(self, spec) -> int:
        dependencySpecs = spec.get("dependencies", [])
        if not dependencySpecs:
            return 0

        resource = ChapterResource.objects.get(name=spec["name"])
        wired = 0
        for dependencySpec in dependencySpecs:
            targetName = dependencySpec["dependsOn"]
            target = ChapterResource.objects.filter(name=targetName).first()
            if target is None:
                raise CommandError(
                    f'"{spec["name"]}" declares a dependency on "{targetName}", which is not a '
                    "resource in this file. Add it, or fix the name - an edge to nothing would "
                    "leave the registry looking complete while a real dependency goes unrecorded."
                )
            try:
                kind = DEPENDENCY_KIND_BY_NAME[dependencySpec["kind"]]
            except KeyError:
                raise CommandError(
                    f'"{spec["name"]}" -> "{targetName}" has kind '
                    f'{dependencySpec.get("kind")!r}; expected one of '
                    f"{sorted(DEPENDENCY_KIND_BY_NAME)}."
                )
            if target.id == resource.id:
                raise CommandError(f'"{spec["name"]}" cannot depend on itself.')

            ResourceDependency.objects.update_or_create(
                resource=resource, dependsOn=target, kind=kind,
                defaults={"note": dependencySpec.get("note", "")},
            )
            wired += 1
        return wired

    def _getOrCreateQuestion(self, resource, spec):
        """Create-only: never overwrite assignedTo/resolvedAt/resolution the
        in-app questions workbench may have already set for a question that
        was seeded on an earlier run."""
        existing = ResourceQuestion.objects.filter(resource=resource, question=spec["question"]).first()
        if existing is not None:
            return existing, False
        created = ResourceQuestion.objects.create(
            resource=resource,
            question=spec["question"],
            assignedTo=spec.get("assignedTo", ""),
            resolution=spec.get("resolution", ""),
        )
        return created, True
