"""Chapter Tools (IT access registry) - M1: the directory, a resource detail
page, and the restricted questions workbench. See tools/models.py for the
two-visibility-layer design and tools/navigation.py for how these routes are
registered under the existing "access" domain.

The detail page (chapter_tool_detail) is the FACET HUB as of the
chapter-tools-facet-hub change: it renders one card per facet (forms.FACETS -
What it is / Getting in / Who's answerable / What it costs / Committee only),
each with its own focused edit page (chapter_tool_facet_edit). The workbench
(chapter_tool_edit) is CHILD ROWS ONLY now - holders, dependencies,
credentials, delete - the resource's own eighteen fields no longer have a
combined form anywhere; chapter_tool_create and chapter_tool_facet_edit are
the only two views that ever build a ChapterResourceForm.

CRUD for ChapterResource and its child rows (ResourceHolder,
ResourceCredential, ResourceDependency) lives here too, gated on
manageChapterTools - see the CRUD block at the bottom of this module. It used
to be admin-only; tools/admin.py remains registered as the fallback for the
fields no in-app form exposes (ResourceGrant, ToolAuditReadLog).

The one rule to keep in mind when editing that block: manageChapterTools is
NOT the audit permission. An editor without viewChapterToolAudit gets a 404 for
the committee-only facet (chapter_tool_facet_edit) and for the credentials
child kind, and no RUNS_ON edges - because a bound form renders current
values, so leaving a restricted field reachable would leak exactly what
chapter_tool_detail is careful to withhold from the same editor's own hub view.

ResourceQuestion has its own add/assign/resolve workbench (below), which
predates the CRUD block: it was the 08-20 meeting agenda item (split the open
questions among the room, live).
"""
import dataclasses
import logging

from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone as djangoTimezone

from . import forms, permissions
from .models import (ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder,
                     ResourceQuestion, ToolAuditReadLog, User)

logger = logging.getLogger(__name__)

# The directory pages at 25. The register is expected to grow well past that -
# it currently omits more of the chapter's systems than it holds - and the cards
# each carry two paragraphs of prose, so an unpaginated list is a page that gets
# slower and less readable with every row somebody adds.
#
# Only the directory is paginated. The privileged-access page deliberately is
# not: its whole job is to be read across the entire register at once, and a
# gap-first sort that only sorted the current page would put the worst findings
# on whichever page they fell on.
RESOURCES_PER_PAGE = 25


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


def _namePeople(holders) -> str:
    """Several people on one line, each with the role they were recorded under.

    Built here rather than in the template because the privileged-access page
    GROUPS people by what they can do - all the owners on one line, everyone who
    can only change access on another - and Django templates cannot join a list
    of composed items without a loop that has to test forloop.last, which is how
    that page ended up rendering one line per person in the first place.

    Grouping is the whole point. The alternative, and what this replaced, was a
    line per person labelled with the sentence getPowerSummary() returns. In the
    label slot that sentence renders uppercase and letter-spaced, so a tool with
    two owners printed the same 43-character run of capitals twice and the names
    - the only part that varied, and the only part anybody came to read - were
    the quietest thing on the card. A short label with several names after it is
    the same information the way the register is actually read: down the label
    column first, then across.

    The role and the unconfirmed flag share one parenthetical so a name carries
    at most one bracket. "Devon K. (Owner, unconfirmed)" is one fact about one
    person; a name trailed by a separate bracket and a separate dash is three
    things to reassemble."""
    described = []
    for holder in holders:
        qualifiers = []
        # The service's own word for the role, when somebody wrote one down.
        # Blank is common and is a true answer - see getAccessLevelDisplay - but
        # here it just means no parenthetical, because "(Role not recorded)"
        # beside every name would be noise on a page about owners.
        if holder.accessLevel.strip():
            qualifiers.append(holder.accessLevel.strip())
        if not holder.confirmed:
            qualifiers.append("unconfirmed")
        suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
        described.append(f"{holder.getDisplayName()}{suffix}")
    return ", ".join(described)


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

    # Paginated, and the page is taken BEFORE the rows are built so a request
    # only ever composes the 25 cards it is going to render. Ordering is
    # explicit and total (category, then name) because a Paginator over an
    # unordered queryset gives no guarantee a row appears on exactly one page.
    #
    # Note what pagination broke, which is why the access-model legend is gone
    # from this view: it used to be computed from the models present in
    # `resources`, so a paginated register would define a phrase on the page
    # where it happened to occur and leave it undefined on the next one. The
    # definition now opens from beside the phrase on every card and lists the
    # complete set - see ChapterResource.getAccessModelLegend.
    resources = ChapterResource.objects.prefetch_related(*prefetches).order_by("category", "name")
    page = Paginator(resources, RESOURCES_PER_PAGE).get_page(request.GET.get("page"))
    rows = []
    for resource in page.object_list:
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

    return render(request, "tools/chapter-tools/index.html", {
        "rows": rows,
        "page": page,
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
    canManage = request.user.has_perm(permissions.MANAGE_CHAPTER_TOOLS)

    # Open kinds stay open in BOTH directions - each is the same public fact
    # read backwards, and the reverse is often the more useful half:
    # "3 tools sign in through Slack" on Slack's page, and "2 things reach the
    # calendar through Echo" on Echo's, which is what tells a reader that Echo
    # is the front door for more than one system.
    facetCards, unconfirmedCount = _buildFacetCards(
        resource, hasAudit=hasAudit, hasHolders=hasHolders, canManage=canManage,
    )
    context = {
        "resource": resource,
        "hasAudit": hasAudit,
        "hasHolders": hasHolders,
        "canManage": canManage,
        "facetCards": facetCards,
        "unconfirmedCount": unconfirmedCount,
        "savedSlug": request.GET.get("saved", ""),
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
            # Grouped by power, not listed per person - see _namePeople. Two
            # lines at most, and each carries a short label the eye can find
            # again on the next card down.
            #
            # `owners` and `accessOnly` partition `privileged` exactly:
            # isPrivileged() is ownsAccount OR canGrantAccess, so anybody in
            # privileged who is not an owner is in the second group by
            # definition. Nobody can appear on both lines and nobody is dropped.
            "owners": _namePeople(owners),
            "accessOnly": _namePeople([
                holder for holder in privileged if not holder.ownsAccount
            ]),
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


def _buildResourceForm(request, resource, rows):
    data = request.POST if request.method == "POST" else None
    initial = None
    if resource is not None and request.method != "POST":
        keys = [key for row in rows for key in row]
        initial = {key: getattr(resource, key) for key in keys}
    return forms.ChapterResourceForm(data, resource=resource, rows=rows, initial=initial)


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_create(request):
    """Create a resource with its four required fields (CREATE_ROWS), then land
    on its hub - the detail page. Every other facet renders there as an
    unconfirmed card with a prompt, so whoever actually knows the answer can
    fill it in from the hub instead of a half-finished workbench form."""
    form = _buildResourceForm(request, None, forms.CREATE_ROWS)
    if request.method == "POST" and form.is_valid():
        resource = ChapterResource()
        _applyCleanedData(resource, form)
        resource.save()
        logger.info(
            "ChapterTools: %s created resource '%s'",
            request.user.getUserNameString(), resource.name,
        )
        return redirect(resource.getUrl())

    return render(request, "tools/chapter-tools/create.html", {"form": form})


# Resource fields rendered as page CHROME above the facet cards (name/title,
# blurb, the how-to-get-access line, the access-model callout, and the
# request-access/site buttons) - never repeated inside a card, or the hub
# would say the same fact twice on one page. ACCESS_MODEL/SITE_URL/
# ACCESS_REQUEST_URL joined NAME/BLURB/HOW_TO_GET_ACCESS here after the first
# cut duplicated all three: detail.html's kept-verbatim lead (lines ~38-51)
# already renders the access-model callout and the Request access / site
# buttons from these same fields, so the Getting-in card was repeating them.
HUB_CHROME_KEYS = (
    forms.ChapterResourceForm.Keys.NAME,
    forms.ChapterResourceForm.Keys.BLURB,
    forms.ChapterResourceForm.Keys.HOW_TO_GET_ACCESS,
    forms.ChapterResourceForm.Keys.ACCESS_MODEL,
    forms.ChapterResourceForm.Keys.SITE_URL,
    forms.ChapterResourceForm.Keys.ACCESS_REQUEST_URL,
)

# Excluded from the generic dt/dd fact list, for two different reasons:
# - DELEGATION_TIER gets bespoke markup inside its own card instead - the
#   delegation-tier callout (with its ladder disclosure) is kept intact rather
#   than flattened into a row, per
#   test_the_tier_ladder_opens_from_inside_the_tier_callout.
# - STEWARD_NAME is folded into the STEWARD fact instead of getting its own
#   row: getStewardDisplayName() already prefers the Echo account and falls
#   back to stewardName, so when only stewardName is set (most stewards at
#   launch have no account) the STEWARD fact's value IS stewardName - a
#   second row showing the identical value under a second label would read
#   as a duplicate, not a second fact.
_BESPOKE_CARD_KEYS = (
    forms.ChapterResourceForm.Keys.DELEGATION_TIER,
    forms.ChapterResourceForm.Keys.STEWARD_NAME,
)


def _factValue(resource, key):
    """One field's value, read the way a reader wants to see it rather than
    the raw Python value: a choice field through its get_<key>_display(),
    steward through the HOLDER-tier display name (never getStewardName, which
    carries an email - see ChapterResource.getStewardDisplayName), a boolean
    as Yes/No, and lastReviewed as itself or "Never" rather than blank.

    Returns "" for anything blank/None so the caller can drop the row - a card
    with an empty dd for every unanswered field would bury the one prompt that
    actually says something is missing."""
    Keys = forms.ChapterResourceForm.Keys
    if key == Keys.STEWARD:
        return resource.getStewardDisplayName()
    if key == Keys.LAST_REVIEWED:
        return resource.lastReviewed if resource.lastReviewed is not None else "Never"
    displayGetter = getattr(resource, f"get_{key}_display", None)
    if displayGetter is not None:
        return displayGetter()
    value = getattr(resource, key)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return value if value is not None else ""


def _buildFacetCards(resource, *, hasAudit, hasHolders, canManage):
    """The hub's card list, and the unconfirmed count over only the cards
    actually returned - a facet the viewer cannot see must not count against
    them (the plan's "excludes any facet the viewer cannot see").

    Built here, not in the template, following the same build-only-if-
    permitted rule the read views already use for runsOnDependencies /
    holderRows: the who's-answerable card reads getStewardDisplayName(),
    which is deliberately holder-tier (REVIEW.md F3), so that card is normally
    holder-tier too.

    EXCEPT for a manageChapterTools editor without holder/audit visibility:
    before this hub existed, that editor could already see and edit the
    steward on the flat workbench form (steward was never in
    RESTRICTED_KEYS), so gating the CARD on hasHolders alone would remove a
    UI path they used to have while section/whos-answerable still answers 200
    for them underneath - a hub that can no longer even link to a page it
    still serves. `canManage` reopens the card for that editor without
    widening who gets it as a plain reader (a member with neither permission
    nor manageChapterTools still never sees it)."""
    cards = []
    for facet in forms.FACETS:
        if facet.requiresAudit and not hasAudit:
            continue
        if facet.slug == "whos-answerable" and not (hasHolders or canManage):
            continue
        facts = []
        for key in facet.keys:
            if key in HUB_CHROME_KEYS or key in _BESPOKE_CARD_KEYS:
                continue
            value = _factValue(resource, key)
            if value in ("", None):
                continue
            # base_fields holds the FORM's declared (unbound) field
            # instances - reading .label off it is the same string
            # ChapterResourceForm(rows=facet.rows) would render, without
            # instantiating a form (and, for whos-answerable, evaluating
            # _activeUsers() - an extra member queryset) per card per render.
            facts.append({
                "key": key,
                "label": forms.ChapterResourceForm.base_fields[key].label,
                "value": value,
            })
        confirmed = facet.isConfirmed(resource)
        cards.append({
            "facet": facet,
            "confirmed": confirmed,
            "facts": facts,
            # Precomputed rather than left as `facet.requiresAudit or
            # confirmed` in the template - Django's {% if %} has no grouping
            # parentheses, and this exact combination is what decides the
            # head shows Edit instead of nothing.
            "showEditButton": facet.requiresAudit or confirmed,
        })
    unconfirmedCount = sum(
        1 for card in cards if not card["facet"].requiresAudit and not card["confirmed"]
    )
    return cards, unconfirmedCount


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_facet_edit(request, pk, facetSlug):
    """Edit one facet's fields on one resource - the section page behind a hub
    card. The form is sliced to exactly this facet's keys (ChapterResourceForm
    rows=facet.rows), so saving here touches only those fields - the rest of
    the record is never read into the form and cannot be overwritten by it.

    404, not 403, for an unknown slug or for the committee-only facet
    requested by an editor without viewChapterToolAudit - same rule
    _resolveChild follows for the restricted child kinds: a 403 would itself
    confirm the section exists."""
    facet = forms._FACET_BY_SLUG.get(facetSlug)
    if facet is None or (facet.requiresAudit and not _canEditRestricted(request.user)):
        raise Http404("No such facet.")

    resource = get_object_or_404(ChapterResource, pk=pk)
    form = _buildResourceForm(request, resource, facet.rows)
    if request.method == "POST" and form.is_valid():
        _applyCleanedData(resource, form)
        resource.save()
        logger.info(
            "ChapterTools: %s edited the %s section of '%s'",
            request.user.getUserNameString(), facet.slug, resource.name,
        )
        return redirect(f"{resource.getUrl()}?saved={facet.slug}#{facet.slug}")

    return render(request, "tools/chapter-tools/section.html", {
        "form": form,
        "resource": resource,
        "facet": facet,
        # Hub + anchor. Cancel and Save land identically - same pattern as
        # child.html's backUrl (_backToSection).
        "backUrl": f"{resource.getUrl()}#{facet.slug}",
        "breadcrumbParentCrumbs": [
            {"label": "Chapter Tools", "url": reverse("chapter-tools")},
            {"label": resource.name, "url": resource.getUrl()},
        ],
    })


# The workbench's sections, as tab keys. Three of them are deliberately the SAME
# strings as CHILD_SPECS' keys, so a child view can send the editor back to the
# section it came from with `?tab={childKind}` and no lookup table. A mapping
# between two near-identical vocabularies is a thing that drifts; sharing one
# spelling means it cannot.
#
# No "details" tab any more - the resource's own fields moved to the hub's
# per-facet section pages (chapter_tool_facet_edit); this page is child rows
# only now.
#
# `delete` is a section of this page and not a child kind - it holds one button,
# which links out to the typed-name confirmation at chapter_tool_delete. It gets
# its own tab so that the only destructive control on the workbench is
# somewhere you have to go, rather than the permanent bottom of whichever
# section people edit most. It needs no permission of its own:
# chapter_tool_delete requires exactly the MANAGE_CHAPTER_TOOLS this whole view
# already requires, so an editor who can reach the tab can use it.
EDIT_TABS = (*CHILD_SPECS, "delete")


def _activeTab(request, hasAudit: bool) -> str:
    """Which section the workbench should render, from ?tab=.

    Default (and fallback for anything unrecognised) is `holders`, not
    `details` - there is no details tab any more, and holders is the child
    kind the registry's biggest recorded gap lives on, so it is the one worth
    landing on first. Anything unrecognised falls back rather than 404ing: the
    tab is a view preference, and a stale bookmark or a hand-typed URL should
    land you on a real page, not an error.

    `credentials` also falls back for an editor without the audit permission, so
    the tab strip and the panel agree. This is the tab STRIP's guard only - it
    decides which tab is highlighted, not what may be read. The panel is guarded
    separately and the view never puts credentials in the context at all without
    the permission, so a forced ?tab=credentials renders the holders panel
    rather than anything restricted."""
    requested = request.GET.get("tab", "")
    if requested == "credentials" and not hasAudit:
        return "holders"
    return requested if requested in EDIT_TABS else "holders"


def _backToSection(pk: int, childKind: str) -> str:
    """The workbench URL, opened on the section a child row belongs to.

    Every add, edit and delete of a child row ends here. Returning to the bare
    URL would drop the editor on the details form, so adding three holders meant
    three trips back to the top of the page and three scrolls down to find the
    button again. `childKind` is already one of the tab keys - see EDIT_TABS - so
    there is nothing to translate."""
    return f"{reverse('chapter-tool-edit', kwargs={'pk': pk})}?tab={childKind}"


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_edit(request, pk):
    """The workbench: this resource's CHILD rows only now - holders,
    dependencies, credentials, delete. The resource's own fields moved to the
    hub's per-facet section pages (chapter_tool_facet_edit), so this view no
    longer builds, binds, or posts a ChapterResourceForm at all.

    One page rather than one per child kind, because the alternative is a
    round trip per row on a phone. The child rows are links out to a single
    focused form each.

    The sections are tabs, addressed by ?tab= so the child views can redirect
    back into the one they came from (see _activeTab, EDIT_TABS,
    _backToSection) - adding a second holder must not land the editor back at
    the top of the page and make them scroll down past a list that just grew
    by one."""
    resource = get_object_or_404(ChapterResource, pk=pk)
    includeRestricted = _canEditRestricted(request.user)

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
        "resource": resource,
        "hasAudit": includeRestricted,
        "tab": _activeTab(request, includeRestricted),
        "holders": list(resource.holders.all()),
        "dependencies": list(dependencyQuery),
        "dependents": list(dependentQuery),
    }
    # The tab count sums BOTH directions, because the panel behind it renders
    # both. It used to count only the forward edges while the panel also listed
    # "What needs this", so Cloudflare - which needs nothing and has two things
    # running on it - read "What it needs (0)" above a list of two. A count that
    # disagrees with the list underneath it teaches a reader to stop trusting
    # every other count on the strip.
    #
    # Sum exactly what the panel will render, never the whole relation. For a
    # non-audit editor both querysets above are already filtered to
    # OPEN_KINDS, so this number stays equal to what they can actually see
    # rather than hinting at RUNS_ON edges by being larger than the list.
    context["connectionCount"] = len(context["dependencies"]) + len(context["dependents"])
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
        return redirect(_backToSection(resource.pk, childKind))

    return render(request, "tools/chapter-tools/child.html", {
        "form": form,
        "resource": resource,
        "childLabel": spec.label,
        "childKind": childKind,
        "isCreate": childId is None,
        # The same URL the successful POST above redirects to, so cancelling and
        # saving land in the same place. Every way out of this page uses it.
        "backUrl": _backToSection(resource.pk, childKind),
    })


@login_required
@permission_required(permissions.MANAGE_CHAPTER_TOOLS)
def chapter_tool_child_delete(request, pk, childKind, childId):
    """Delete one child row. POST only - a GET-deletable URL gets emptied by a
    link prefetcher or a crawler, and no typed confirmation would save it."""
    if request.method != "POST":
        return redirect(_backToSection(pk, childKind))
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
    return redirect(_backToSection(resource.pk, childKind))
