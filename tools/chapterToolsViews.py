"""Chapter Tools (IT access registry) - M1: the directory, a resource detail
page, and the restricted questions workbench. See tools/models.py for the
two-visibility-layer design and tools/navigation.py for how these routes are
registered under the existing "access" domain.

No in-app CRUD for ChapterResource/ResourceCredential/ResourceHolder in v1 -
that stays admin-only (see tools/admin.py). ResourceQuestion is the one
exception: it gets a minimal in-app add/assign/resolve workflow here, since
that's the actual 08-20 meeting agenda item (split the open questions among
the room, live).
"""
import logging

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone as djangoTimezone

from . import permissions
from .models import ChapterResource, ResourceDependency, ResourceQuestion, ToolAuditReadLog

logger = logging.getLogger(__name__)


def _hasAudit(user) -> bool:
    """Whether the restricted layer renders for this user. has_perm already
    returns True for superusers (Django's ModelBackend), so this is just the
    one call the plan describes as 'the viewChapterToolAudit permission or
    superuser' - the check simply implies the other."""
    return user.has_perm(permissions.VIEW_CHAPTER_TOOL_AUDIT)


def _hasHolders(user) -> bool:
    """Whether the holder-identity tier (names, how, confirmed, note) renders
    for this user. Audit implies holders: the restricted ring is already
    trusted with every resource's weaknesses, which is strictly more
    sensitive than who holds it, so a second explicit grant would buy
    nothing (see REVIEW.md finding F4, which resolves PROPOSAL.md's own
    self-contradiction in favor of this `or`)."""
    return _hasAudit(user) or user.has_perm(permissions.VIEW_RESOURCE_HOLDERS)


def _logRestrictedRead(user, target: str) -> None:
    ToolAuditReadLog.objects.create(user=user, target=target)


def _edges(manager, kind: int, otherEnd: str) -> list:
    """One direction of one kind of dependency edge, with the far end joined.

    `otherEnd` is which side to select_related: "dependsOn" reading forward off
    resource.dependencies, "resource" reading backward off resource.dependents.
    Six near-identical querysets were previously spelled out inline, which is
    how the restricted RUNS_ON pair drifted into a different shape from the
    open ones and became easy to copy into the wrong block."""
    return list(manager.filter(kind=kind).select_related(otherEnd))


def _parseId(rawValue) -> int | None:
    """Defensively parse a POSTed pk. A stale form / back-button repost can
    submit a non-numeric or missing id; treat that as "no such row" rather
    than letting the ValueError bubble into a 500."""
    if not rawValue:
        return None
    try:
        return int(rawValue)
    except (TypeError, ValueError):
        return None


@login_required
def chapter_tools_index(request):
    """The directory: open layer for everyone, plus a per-row stale flag for
    viewChapterToolAudit holders (restricted-layer detail, per the plan - the
    open directory itself stays clean), plus the holder roster for
    viewResourceHolders holders (or audit - see _hasHolders)."""
    hasAudit = _hasAudit(request.user)
    hasHolders = _hasHolders(request.user)
    if hasAudit:
        _logRestrictedRead(request.user, "index")

    # Holder identity is tier 1 (PROPOSAL.md, REVIEW.md F4/F9) - the "holders"
    # prefetch is added to the queryset only when the viewer is permitted to
    # see it, following the same build-only-if-permitted rule as
    # chapter_tool_detail's runsOnDependencies. Passing an un-prefetched
    # accessor to the template would still be safe (no rows), but it would run
    # a query per resource - and more importantly a future edit could start
    # reading resource.holders.all() unconditionally, so the rows must never be
    # loaded into memory in the first place for someone without the permission.
    prefetches = [
        # OPEN_KINDS, not a named kind: the directory shows every edge a member
        # needs to act on, and the model decides which those are. Naming kinds
        # here is how a new open kind gets silently dropped from the directory.
        Prefetch(
            "dependencies",
            queryset=ResourceDependency.objects.filter(
                kind__in=ResourceDependency.OPEN_KINDS,
            ).select_related("dependsOn"),
            to_attr="openDependencies",
        ),
    ]
    if hasHolders:
        prefetches.append("holders")

    resources = ChapterResource.objects.prefetch_related(*prefetches).order_by("category", "name")
    rows = []
    for resource in resources:
        row = {
            "resource": resource,
            "signInDependencies": [
                dependency for dependency in resource.openDependencies
                if dependency.kind == ResourceDependency.Kind.SIGN_IN
            ],
            # Split out rather than rendered from one list, because the two
            # read as different sentences: "you need Slack first" is a
            # prerequisite, while "you never get this directly" redirects the
            # reader somewhere else entirely.
            "reachedThroughDependencies": [
                dependency for dependency in resource.openDependencies
                if dependency.kind == ResourceDependency.Kind.REACHED_THROUGH
            ],
        }
        if hasHolders:
            row["holderRows"] = [
                {"name": holder.getDisplayName(), "confirmed": holder.confirmed}
                for holder in resource.holders.all()
            ]
        rows.append(row)

    # Legend of only the access models actually on screen. Glossing every row
    # inline would repeat four lines of prose five times; defining each term
    # once under the table keeps the table scannable and still leaves no
    # unexplained phrase on the page.
    modelsInUse = {resource.accessModel for resource in resources}
    accessModelLegend = [
        {"label": label, "explanation": ChapterResource.ACCESS_MODEL_EXPLANATIONS.get(value, "")}
        for value, label in ChapterResource.ACCESS_MODEL_CHOICES
        if value in modelsInUse
    ]

    return render(request, "tools/chapter-tools/index.html", {
        "rows": rows,
        "accessModelLegend": accessModelLegend,
        "hasAudit": hasAudit,
        "hasHolders": hasHolders,
    })


@login_required
def chapter_tool_detail(request, pk):
    """One resource: open section for everyone; the holder roster (names,
    how, confirmed, note, steward) for viewResourceHolders holders (or audit
    - see _hasHolders); the restricted section (delegation tier,
    revocation/continuity notes, credentials, review detail, open questions)
    only for viewChapterToolAudit holders - and only that render writes a
    read-log row."""
    resource = get_object_or_404(ChapterResource, pk=pk)
    hasAudit = _hasAudit(request.user)
    hasHolders = _hasHolders(request.user)

    # Open kinds stay open in BOTH directions - each is the same public fact
    # read backwards, and the reverse is often the more useful half:
    # "3 tools sign in through Slack" on Slack's page, and "2 things reach the
    # calendar through Echo" on Echo's, which is what tells a reader that Echo
    # is the front door for more than one system.
    context = {
        "resource": resource,
        "hasAudit": hasAudit,
        "hasHolders": hasHolders,
        "signInDependencies": _edges(resource.dependencies, ResourceDependency.Kind.SIGN_IN, "dependsOn"),
        "signInDependents": _edges(resource.dependents, ResourceDependency.Kind.SIGN_IN, "resource"),
        "reachedThroughDependencies": _edges(
            resource.dependencies, ResourceDependency.Kind.REACHED_THROUGH, "dependsOn",
        ),
        "reachedThroughDependents": _edges(
            resource.dependents, ResourceDependency.Kind.REACHED_THROUGH, "resource",
        ),
    }
    if hasHolders:
        # Holder identity is tier 1 - built only here, same build-only-if-
        # permitted rule as runsOnDependencies below. A template-only guard
        # would still pull every holder row (names, how, note) into context on
        # every request, which is exactly the aggregate the permission exists
        # to gate.
        context["holderRows"] = [{
            "name": holder.getDisplayName(),
            "how": holder.get_how_display(),
            "confirmed": holder.confirmed,
            "note": holder.note,
        } for holder in resource.holders.all()]
    if hasAudit:
        _logRestrictedRead(request.user, resource.name)
        context["credentials"] = list(resource.credentials.all())
        context["questions"] = list(resource.questions.all())
        # RUNS_ON is restricted - built only here, alongside credentials and
        # questions, so it follows the same build-only-if-permitted rule as
        # every other restricted field on this page. A template-only guard is
        # one refactor away from leaking.
        context["runsOnDependencies"] = _edges(
            resource.dependencies, ResourceDependency.Kind.RUNS_ON, "dependsOn",
        )
        context["runsOnDependents"] = _edges(
            resource.dependents, ResourceDependency.Kind.RUNS_ON, "resource",
        )

    return render(request, "tools/chapter-tools/detail.html", context)


@login_required
@permission_required(permissions.VIEW_CHAPTER_TOOL_AUDIT)
def chapter_tools_questions(request):
    """The restricted ResourceQuestion workbench - meeting-friendly add /
    assign / resolve. permission_required (no raise_exception) redirects
    anyone without the permission to login, same convention as the other
    admin-tier access pages (see accessViews.manage_access)."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            question = request.POST.get("question", "").strip()
            if question:
                resourceId = _parseId(request.POST.get("resource"))
                resource = ChapterResource.objects.filter(id=resourceId).first() if resourceId else None
                ResourceQuestion.objects.create(resource=resource, question=question)
                logger.info(
                    "ChapterToolsQuestions: %s added a question%s",
                    request.user.getUserNameString(),
                    f" for {resource.name}" if resource else " (chapter-wide)",
                )
        elif action == "assign":
            assignedTo = request.POST.get("assignedTo", "").strip()
            questionId = _parseId(request.POST.get("questionId"))
            if questionId is not None:
                ResourceQuestion.objects.filter(id=questionId).update(assignedTo=assignedTo)
        elif action == "resolve":
            resolution = request.POST.get("resolution", "").strip()
            questionId = _parseId(request.POST.get("questionId"))
            if questionId is not None:
                ResourceQuestion.objects.filter(
                    id=questionId, resolvedAt__isnull=True,
                ).update(resolvedAt=djangoTimezone.now(), resolution=resolution)
        return redirect("chapter-tools-questions")

    _logRestrictedRead(request.user, "questions")

    openQuestions = (
        ResourceQuestion.objects.filter(resolvedAt__isnull=True)
        .select_related("resource").order_by("-raisedAt")
    )
    resolvedQuestions = (
        ResourceQuestion.objects.filter(resolvedAt__isnull=False)
        .select_related("resource").order_by("-resolvedAt")
    )
    resources = ChapterResource.objects.order_by("name")

    return render(request, "tools/chapter-tools/questions.html", {
        "openQuestions": openQuestions,
        "resolvedQuestions": resolvedQuestions,
        "resources": resources,
    })
