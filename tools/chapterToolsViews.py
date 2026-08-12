"""Chapter Tools (IT access registry) - M1: the directory, a resource detail
page, and the restricted questions workbench. See tools/models.py for the
two-visibility-layer design and tools/navigation.py for how these routes are
registered under the existing "access" domain.

CRUD for ChapterResource and its child rows (ResourceHolder,
ResourceCredential, ResourceDependency) lives here too, gated on
manageChapterTools - see the CRUD block at the bottom of this module. It used
to be admin-only; tools/admin.py remains registered as the fallback for the
fields no in-app form exposes (ResourceGrant, ToolAuditReadLog).

The one rule to keep in mind when editing that block: manageChapterTools is
NOT the audit permission. An editor without viewChapterToolAudit gets a form
with the restricted fields removed, no credentials, and no RUNS_ON edges -
because a bound form renders current values, so leaving a restricted field on
the page would leak exactly what chapter_tool_detail is careful to withhold.

ResourceQuestion has its own add/assign/resolve workbench (below), which
predates the CRUD block: it was the 08-20 meeting agenda item (split the open
questions among the room, live).
"""
import dataclasses
import logging

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone as djangoTimezone

from . import forms, permissions
from .models import (ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder,
                     ResourceQuestion, ToolAuditReadLog, User)

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
        "canManage": request.user.has_perm(permissions.MANAGE_CHAPTER_TOOLS),
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
        "canManage": request.user.has_perm(permissions.MANAGE_CHAPTER_TOOLS),
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
            # `how` is which door they come through, `accessLevel` is what they
            # can do once inside - two facts the registry used to conflate, so
            # "who has Slack" and "who could delete Slack" were one list.
            # accessLevel is the service's own word for the role and is free
            # text; powerSummary is the chapter's reading of the two booleans,
            # and is what the badge colour comes from.
            "accessLevel": holder.getAccessLevelDisplay(),
            "powerSummary": holder.getPowerSummary(),
            "privileged": holder.isPrivileged(),
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
def chapter_tools_privileged(request):
    """Who holds owner or admin rights across the whole register, and - the
    point of the page - which resources have nobody recorded as owner at all.

    Why this is a page and not a column on the directory. The useful question is
    not "who has access to this one tool", which the detail page already answers;
    it is "where is the chapter one person away from losing something", and that
    only reads across resources. A registry that can only be read one row at a
    time cannot answer it.

    Gated on the holder tier OR audit, matching _hasHolders - holder identity is
    exactly what this page shows, so it is the same tier as the roster on the
    detail page and not a new one. Http404 rather than 403 for somebody without
    it, following the same rule as the restricted child routes: a 403 confirms
    the page exists.
    """
    if not _hasHolders(request.user):
        raise Http404

    hasAudit = _hasAudit(request.user)
    if hasAudit:
        _logRestrictedRead(request.user, "privileged-access")

    # Read from the two structured booleans, never from the role name. The role
    # name is free text in whatever words the service uses, so no page can ask a
    # question of it - which is exactly why ResourceHolder carries ownsAccount
    # and canGrantAccess alongside it.
    rows = []
    for resource in ChapterResource.objects.prefetch_related("holders").order_by("category", "name"):
        holders = list(resource.holders.all())
        owners = [holder for holder in holders if holder.ownsAccount]
        privileged = [holder for holder in holders if holder.isPrivileged()]
        # `confirmed` is now the single "has anybody actually checked this row"
        # signal. It used to be asked twice - this flag, and an UNCONFIRMED rung
        # on the access-level ladder that said it again about one field - so a row
        # could be confirmed with an unconfirmed level and this page counted the
        # second one only.
        unconfirmed = [holder for holder in holders if not holder.confirmed]

        # Three distinct findings, deliberately not collapsed into one "risk"
        # score. "Nobody is recorded" is a gap in the register; "one person is
        # recorded" is a fact about the chapter. They prompt different work -
        # one is a question to answer, the other a decision to make - and a
        # single number would hide which of the two you are looking at.
        if not holders:
            finding = "no-holders"
        elif not owners and unconfirmed:
            finding = "unconfirmed-only"
        elif not owners:
            finding = "no-owner"
        elif len(owners) == 1:
            finding = "single-owner"
        else:
            finding = "ok"

        rows.append({
            "resource": resource,
            "privileged": privileged,
            "unconfirmedCount": len(unconfirmed),
            "holderCount": len(holders),
            "ownerCount": len(owners),
            "finding": finding,
        })

    # Gaps first. The page exists to surface what is missing, and a register
    # sorted by name buries the empty rows among the healthy ones.
    ranking = {"no-holders": 0, "unconfirmed-only": 1, "no-owner": 2, "single-owner": 3, "ok": 4}
    rows.sort(key=lambda row: (ranking[row["finding"]], row["resource"].name))

    return render(request, "tools/chapter-tools/privileged.html", {
        "rows": rows,
        "hasAudit": hasAudit,
        "canManage": request.user.has_perm(permissions.MANAGE_CHAPTER_TOOLS),
        "needsAttention": sum(
            1 for row in rows if row["finding"] in ("no-holders", "unconfirmed-only", "no-owner")
        ),
        "singleOwnerCount": sum(1 for row in rows if row["finding"] == "single-owner"),
    })


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


# --- CRUD (manageChapterTools) ---------------------------------------------
#
# Closes the v1 gap named in this module's docstring: the registry used to be
# editable only through /admin/.
#
# The write surface is gated by TWO permissions, not one. manageChapterTools
# authorizes writing; the restricted layer keeps its own gate, so the fields,
# credentials, and RUNS_ON edges that chapter_tool_detail hides from a
# non-audit reader are also absent from the forms it serves them. Otherwise
# "can edit" would quietly become "can read everything", which is the exact
# leak the two-layer design exists to prevent.


def _canEditRestricted(user) -> bool:
    """Whether this editor may see and write the restricted layer.

    Same shape as _hasAudit, kept as its own name because the two answer
    different questions and only one of them is about writing - a future change
    that makes writing stricter than reading should have somewhere to land."""
    return _hasAudit(user)


@dataclasses.dataclass(frozen=True)
class _ChildSpec:
    """One child model hanging off a ChapterResource.

    Three near-identical CRUD triples (holders, credentials, dependencies) are
    driven from this table rather than written out nine times, for the same
    reason _edges() exists above: the copies drift. The RUNS_ON layer bug this
    module's docstring warns about came from exactly that kind of duplication.
    """
    label: str                  # singular, for headings and log lines
    model: type
    formClass: type
    relatedName: str            # ChapterResource.<relatedName>
    requiresAudit: bool         # the whole child kind is restricted
    # No explainSlug here any more: which words a form needs defined now lives on
    # the FORM, as its EXPLAIN_SLUGS map, so the definition renders beside the
    # field it defines instead of once at the foot of the page. A per-kind slug
    # on this spec could only ever place one definition, and only there.


CHILD_SPECS = {
    "holders": _ChildSpec(
        label="holder", model=ResourceHolder, formClass=forms.ResourceHolderForm,
        relatedName="holders", requiresAudit=False,
    ),
    # Credentials have no open-layer half at all - the model's own docstring
    # calls it restricted - so the kind is gated, not just some of its fields.
    "credentials": _ChildSpec(
        label="credential", model=ResourceCredential, formClass=forms.ResourceCredentialForm,
        relatedName="credentials", requiresAudit=True,
    ),
    # NOT audit-gated as a whole: SIGN_IN and REACHED_THROUGH are open kinds and
    # are the two a member actually needs. The restricted kind (RUNS_ON) is
    # filtered inside the form instead - see ResourceDependencyForm.
    "dependencies": _ChildSpec(
        label="dependency", model=ResourceDependency, formClass=forms.ResourceDependencyForm,
        relatedName="dependencies", requiresAudit=False,
    ),
}


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_member_search(request):
    """Typeahead backing for the steward and holder-account pickers.

    Why an endpoint at all: both fields used to render a <select> holding every
    active member, which is a control that stops working at exactly the chapter
    size this registry is built for. See forms.UserTypeaheadWidget.

    Gated on manageChapterTools, the same permission as the forms that use it -
    and it discloses nothing those forms did not already: the <select> it
    replaces rendered every active member's name and email into the page for the
    same audience. The gate matters anyway, because an ungated version would be a
    member-directory search for anybody with an account.

    Two characters minimum, ten results maximum. Both are for the same reason -
    a one-letter query matches most of the chapter, so it would be a slow way to
    say nothing.
    """
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    matches = (
        User.objects.filter(is_active=True)
        .filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
        .order_by("first_name", "last_name", "username")[:10]
    )
    # `label` is getUserNameString() - "First Last - email" - because that is
    # what the <select> showed, and it is the form that tells two members with
    # the same first name apart. The client renders it verbatim.
    return JsonResponse({"results": [
        {"id": member.id, "label": member.getUserNameString(), "username": member.username}
        for member in matches
    ]})


def _applyCleanedData(instance, form) -> None:
    """Copy a validated form onto a model instance, field by field.

    Every form Key in this module is named exactly after its model field, which
    is what makes this safe - and is why a dropped field (a restricted one
    removed for a non-audit editor) simply leaves the stored value untouched
    instead of blanking it. The alternative, listing assignments per field,
    is where a "quietly wiped the continuity note" bug would come from.

    Deliberately no full_clean(): the module convention is that the form is the
    sole validation point (see the comment block above the Chapter Tools forms
    in forms.py), and the two model rules it drops are re-implemented there."""
    for key, value in form.cleaned_data.items():
        setattr(instance, key, value)


def _buildResourceForm(request, resource, includeRestricted):
    data = request.POST if request.method == "POST" else None
    initial = None
    if resource is not None and request.method != "POST":
        initial = {key: getattr(resource, key) for key in _formKeys(forms.ChapterResourceForm)}
    return forms.ChapterResourceForm(
        data, resource=resource, includeRestricted=includeRestricted, initial=initial,
    )


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_create(request):
    """Create a resource, then land on its workbench so the child rows (holders,
    dependencies) can be filled in immediately - a resource with no holders and
    no access story is the half-finished state this registry keeps ending up in."""
    includeRestricted = _canEditRestricted(request.user)
    form = _buildResourceForm(request, None, includeRestricted)
    if request.method == "POST" and form.is_valid():
        resource = ChapterResource()
        _applyCleanedData(resource, form)
        resource.save()
        logger.info(
            "ChapterTools: %s created resource '%s'",
            request.user.getUserNameString(), resource.name,
        )
        return redirect("chapter-tool-edit", pk=resource.pk)

    return render(request, "tools/chapter-tools/edit.html", {
        "form": form,
        "resource": None,
        "hasAudit": includeRestricted,
        "isCreate": True,
    })


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_edit(request, pk):
    """The workbench: this resource's own fields, plus its child rows.

    One page rather than one per child kind, because the alternative is a
    round trip per row on a phone. The child rows are links out to a single
    focused form each; only the resource's own fields post from here."""
    resource = get_object_or_404(ChapterResource, pk=pk)
    includeRestricted = _canEditRestricted(request.user)
    form = _buildResourceForm(request, resource, includeRestricted)
    if request.method == "POST" and form.is_valid():
        _applyCleanedData(resource, form)
        resource.save()
        logger.info(
            "ChapterTools: %s edited resource '%s'",
            request.user.getUserNameString(), resource.name,
        )
        # POST-redirect-GET, so a refresh does not resubmit and the rebuilt form
        # shows the STORED values rather than what was typed - a save that
        # normalizes (a stripped name, a coerced decimal) would otherwise leave
        # the un-normalized text on screen to be posted straight back in.
        return redirect(f"{reverse('chapter-tool-edit', kwargs={'pk': resource.pk})}?saved=1")

    # Both directions, because an edge added from the far end is invisible here
    # otherwise and just gets added a second time.
    dependencyQuery = resource.dependencies.select_related("dependsOn")
    dependentQuery = resource.dependents.select_related("resource")
    if not includeRestricted:
        # Filtered in the QUERY rather than after loading, so a restricted edge
        # never enters memory - the same build-only-if-permitted rule
        # chapter_tool_detail follows for runsOnDependencies.
        dependencyQuery = dependencyQuery.filter(kind__in=ResourceDependency.OPEN_KINDS)
        dependentQuery = dependentQuery.filter(kind__in=ResourceDependency.OPEN_KINDS)

    context = {
        "form": form,
        "resource": resource,
        "hasAudit": includeRestricted,
        "isCreate": False,
        "saved": request.GET.get("saved") == "1",
        "holders": list(resource.holders.all()),
        "dependencies": list(dependencyQuery),
        "dependents": list(dependentQuery),
    }
    if includeRestricted:
        context["credentials"] = list(resource.credentials.all())
    return render(request, "tools/chapter-tools/edit.html", context)


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_delete(request, pk):
    """Delete a resource behind a typed-name confirmation, following
    manage_group_delete. The GET shows what else goes with it: the delete
    cascades to credentials, holders, and dependency edges in BOTH directions,
    which is not obvious from a page that lists only the forward ones."""
    resource = get_object_or_404(ChapterResource, pk=pk)

    if request.method == "POST":
        # Server-side backstop for the typed name, same as manage_group_delete -
        # the page's JS only disables the button.
        if request.POST.get("confirmName", "").strip() != resource.name:
            logger.warning(
                "ChapterTools: %s sent a delete for '%s' with a mismatched confirmation",
                request.user.getUserNameString(), resource.name,
            )
            return redirect("chapter-tool-delete", pk=resource.pk)
        name = resource.name
        resource.delete()
        logger.info("ChapterTools: %s deleted resource '%s'", request.user.getUserNameString(), name)
        return redirect("chapter-tools")

    counts = [
        ("Holder rows", resource.holders.count()),
        ("Dependency edges pointing out of it", resource.dependencies.count()),
        ("Dependency edges pointing at it", resource.dependents.count()),
        ("Grant-ledger rows", resource.grants.count()),
    ]
    if _canEditRestricted(request.user):
        counts.insert(1, ("Credential rows", resource.credentials.count()))
    return render(request, "tools/chapter-tools/delete.html", {
        "resource": resource,
        "counts": counts,
        # Questions are SET_NULL, so they survive as chapter-wide rather than
        # being destroyed - worth saying, since it is the one child that does not
        # disappear and a reader would otherwise assume it does.
        "questionCount": resource.questions.count(),
    })


def _resolveChild(request, pk, childKind, childId):
    """(resource, spec, instance) for a child route, or None if the caller may
    not be here. An unknown childKind, or a restricted kind requested by a
    non-audit editor, 404s rather than 403s: a 403 would confirm that the
    credentials tab exists."""
    spec = CHILD_SPECS.get(childKind)
    if spec is None:
        return None
    if spec.requiresAudit and not _canEditRestricted(request.user):
        return None
    resource = get_object_or_404(ChapterResource, pk=pk)
    instance = None
    if childId is not None:
        # Scoped through the parent, so a childId belonging to another resource
        # cannot be edited by guessing its number.
        instance = get_object_or_404(getattr(resource, spec.relatedName), pk=childId)
    return resource, spec, instance


def _buildChildForm(request, resource, spec, instance):
    data = request.POST if request.method == "POST" else None
    initial = None
    if instance is not None and request.method != "POST":
        initial = {
            key: getattr(instance, key)
            for key in _formKeys(spec.formClass)
        }
    kwargs = {}
    if spec.formClass is forms.ResourceDependencyForm:
        kwargs = {
            "resource": resource,
            "dependency": instance,
            "allowRestrictedKinds": _canEditRestricted(request.user),
        }
    return spec.formClass(data, initial=initial, **kwargs)


def _formKeys(formClass) -> list:
    """The field names a form's Keys class declares, in declaration order."""
    return [value for name, value in vars(formClass.Keys).items() if not name.startswith("_")]


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_child_edit(request, pk, childKind, childId=None):
    """Add or edit one child row (holder, credential, dependency).

    One view for all three kinds, driven by CHILD_SPECS - see the note there on
    why these are not written out nine times."""
    resolved = _resolveChild(request, pk, childKind, childId)
    if resolved is None:
        raise Http404("No such child kind for this resource.")
    resource, spec, instance = resolved

    form = _buildChildForm(request, resource, spec, instance)
    if request.method == "POST" and form.is_valid():
        if instance is None:
            instance = spec.model(resource=resource)
        _applyCleanedData(instance, form)
        instance.save()
        logger.info(
            "ChapterTools: %s %s a %s on '%s'",
            request.user.getUserNameString(),
            "added" if childId is None else "edited",
            spec.label, resource.name,
        )
        return redirect("chapter-tool-edit", pk=resource.pk)

    return render(request, "tools/chapter-tools/child.html", {
        "form": form,
        "resource": resource,
        "childLabel": spec.label,
        "childKind": childKind,
        "isCreate": childId is None,
    })


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_child_delete(request, pk, childKind, childId):
    """Delete one child row. POST only - a GET-deletable URL gets emptied by a
    link prefetcher or a crawler, and no typed confirmation would save it."""
    if request.method != "POST":
        return redirect("chapter-tool-edit", pk=pk)
    resolved = _resolveChild(request, pk, childKind, childId)
    if resolved is None:
        raise Http404("No such child kind for this resource.")
    resource, spec, instance = resolved
    describe = str(instance)
    instance.delete()
    logger.info(
        "ChapterTools: %s deleted a %s (%s) from '%s'",
        request.user.getUserNameString(), spec.label, describe, resource.name,
    )
    return redirect("chapter-tool-edit", pk=resource.pk)
