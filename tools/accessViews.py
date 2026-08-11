import datetime
import logging
import urllib.parse
import uuid

from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group, Permission
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse

import settings

from . import permissions
from .forms import GroupForm, ManageAccessForm, ReviewAccessRequestForm, SelfServiceAccessForm
from .models import AccessRequests, DelegatedEvents, EventOwners, User, revokeCommitteeMembership

logger = logging.getLogger(__name__)

# NOTE: unlike the event flows this uses Django's built-in mail module (the
# TODOs in eventViews suggest moving there anyway): it honors the configured
# EMAIL_BACKEND (console in dev - handy for grabbing the review link) and is
# assertable in tests. Send failures are logged and never fail the request -
# same convention as everywhere else in this app.


def _getApproversFor(accessRequest: AccessRequests):
    """Everyone who may act on this request: superusers, holders of
    approveAccessRequest (directly or via a group), for group requests existing
    members of the requested group, and for event-owner requests the owner's
    current authorizers. The requester is excluded. Mirrors
    AccessRequests.canBeReviewedBy - keep the two in sync."""
    approvePermission = Permission.objects.filter(
        codename=permissions.APPROVE_ACCESS_REQUEST.split(".")[1],
        content_type__app_label="tools",
    ).first()
    query = Q(is_superuser=True)
    if approvePermission is not None:
        query |= Q(user_permissions=approvePermission) | Q(groups__permissions=approvePermission)
    else:
        # Shouldn't happen once migrations have run; superusers still get notified
        logger.error("AccessRequests: approveAccessRequest permission row is missing")
    if accessRequest.group is not None:
        query |= Q(groups=accessRequest.group)
    if accessRequest.owner is not None:
        # related_name on EventOwners.authorizers is "eventAuthorizations"
        query |= Q(eventAuthorizations=accessRequest.owner)
    return (
        User.objects.filter(query, is_active=True)
        .exclude(id=accessRequest.requester_id)
        .distinct()
    )


def _sendBatchRequestEmails(request, createdRequests: list[AccessRequests]):
    """Notify approvers about one self-service submission's new rows (plan D7).

    Groups by approver rather than by row: build _getApproversFor per row, then
    invert to approver -> the items THAT approver may decide, and send each
    approver exactly ONE email listing only those items. That inversion is the
    leak-free construction - an approver who can only decide a permission-target
    row must never see the owner-target row a different requester's committee
    peer would review (REVIEW.md F5's concern, applied here to email rather
    than the review-list web surfaces this plan leaves alone).

    Each send is wrapped separately (D7's second half): the old single-row
    _sendNewRequestEmails sat the whole per-approver loop inside one try, so
    one bad address silently ate every later recipient's email too.
    """
    itemsByApprover: dict[User, list[AccessRequests]] = {}
    for accessRequest in createdRequests:
        for approver in _getApproversFor(accessRequest):
            itemsByApprover.setdefault(approver, []).append(accessRequest)

    requester = createdRequests[0].requester
    for approver, items in itemsByApprover.items():
        try:
            itemLines = "\n".join(
                f"- {item.getTargetDescription()} : {request.build_absolute_uri(item.getUrl())}"
                for item in items
            )
            subject = (
                f"Access requested: {items[0].getTargetDescription()}"
                if len(items) == 1
                else f"Access requested: {len(items)} item(s) from {requester.getUserNameString()}"
            )
            send_mail(
                subject=subject,
                message=(
                    f"{requester.getUserNameString()} has requested the following access on Echo (Austin DSA):\n"
                    f"{itemLines}\n\n"
                    f"Their reason: {items[0].justification}\n\n"
                    "Please visit the link(s) above to approve or deny each request."
                ),
                from_email=settings.EMAIL_HOST_USER,
                recipient_list=[approver.email],
            )
        except Exception as err:
            logger.error("RequestAccess: Failed to send batch notification email to %s", approver.email)
            logger.exception(err)

    try:
        totalPending = AccessRequests.objects.filter(
            requester=requester, status=AccessRequests.Status.REQUESTED
        ).count()
        send_mail(
            subject="Your access request was submitted",
            message=(
                f"Your request for {len(createdRequests)} item(s) has been sent to the approvers. "
                f"You have {totalPending} item(s) still pending review overall. "
                "You will receive an email once each is reviewed."
            ),
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[requester.email],
        )
    except Exception as err:
        logger.error("RequestAccess: Failed to send requester confirmation email")
        logger.exception(err)


def _sendDecisionEmail(accessRequest: AccessRequests):
    if accessRequest.requester is None:
        return
    try:
        decision = "approved" if accessRequest.status == AccessRequests.Status.APPROVED else "denied"
        send_mail(
            subject=f"Your access request was {decision}",
            message=f"""Your request for {accessRequest.getTargetDescription()} was {decision} by {accessRequest.getReviewerName()}.
            Reason: {accessRequest.reason}""",
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[accessRequest.requester.email],
        )
    except Exception as err:
        logger.error("ReviewAccessRequest: Failed to send decision email")
        logger.exception(err)


def _permissionSource(user, permission, directIds) -> str:
    """Where a HELD permission comes from: "Superuser", "Granted directly", or
    "Via <group>, <group>". Extracted from my_access (plan access-self-service-
    form step 2) so my_access and the self-service checklist below describe the
    same permission the same way - two independent computations of "where does
    this come from" is exactly how the two pages would drift.

    A permission held both directly and via a group is reported as "Granted
    directly" - group provenance is only surfaced when it's the sole source.
    Callers that need to know about a dual-source hold for a DIFFERENT reason
    (the self-service checklist's lock rule, D4) compute that separately; this
    function only answers "what does my_access print".

    ``directIds`` is passed in rather than recomputed, so a caller looping over
    many permissions computes it once.
    """
    if user.is_superuser:
        return "Superuser"
    if permission.id in directIds:
        return "Granted directly"
    viaGroups = user.groups.filter(permissions=permission)
    return "Via " + ", ".join(group.name for group in viaGroups) if viaGroups else "Granted directly"


def _ownerKey(ownerId) -> str:
    return f"{SelfServiceAccessForm.OWNER_PREFIX}:{ownerId}"


def _permissionKey(permissionId) -> str:
    return f"{SelfServiceAccessForm.PERMISSION_PREFIX}:{permissionId}"


def _nonSuperuserApproveHolderCount(excludeUserId) -> int:
    """How many OTHER active, non-superuser members hold approveAccessRequest
    (directly or via a group) - the D8 count behind "are you the last one".

    Superusers are deliberately excluded from the count: they can always
    review anything (AccessRequests.canBeReviewedBy), so they are the floor the
    warning already accounts for ("only site superusers will be able to review
    requests"), not a peer holder whose presence should suppress the warning.
    """
    approvePermission = Permission.objects.filter(
        codename=permissions.APPROVE_ACCESS_REQUEST.split(".")[1],
        content_type__app_label="tools",
    ).first()
    if approvePermission is None:
        return 0
    return (
        User.objects.filter(
            Q(user_permissions=approvePermission) | Q(groups__permissions=approvePermission),
            is_active=True, is_superuser=False,
        )
        .exclude(id=excludeUserId)
        .distinct()
        .count()
    )


def _reconcileHeldAndPending(user) -> None:
    """Auto-close any pending AccessRequests the member already effectively
    holds (plan D6). Reachable without any race: manage_event_owner's
    add-authorizer auto-close (ownerViews.py) only fires for an add made
    THROUGH that page, and no surface auto-closes a permission-target request
    that a later GROUP grant happens to satisfy (manage_group's auto-close
    matches group_id, never the permission ids that group carries). Run this
    every time the self-service page is visited so a stale pending row never
    sits there forever, and so the checklist below only ever has to render
    "held" XOR "pending" for a given item, never both at once.
    """
    pendingRequests = AccessRequests.objects.filter(
        requester=user, status=AccessRequests.Status.REQUESTED,
    ).filter(Q(owner__isnull=False) | Q(permission__isnull=False))
    for pendingRequest in pendingRequests:
        if pendingRequest.owner_id is not None:
            satisfied = pendingRequest.owner.authorizers.filter(id=user.id).exists()
        else:
            satisfied = user.has_perm("tools." + pendingRequest.permission.codename)
        if not satisfied:
            continue
        logger.info(
            "RequestAccess: closing pending request %d for %s - access already held",
            pendingRequest.id, user.getUserNameString(),
        )
        pendingRequest.status = AccessRequests.Status.APPROVED
        pendingRequest.dateReviewed = datetime.datetime.now(datetime.UTC)
        pendingRequest.reason = "Access already held"
        pendingRequest.save()
        _sendDecisionEmail(pendingRequest)


def _currentAccessState(user) -> dict:
    """Fresh read of everything the checklist's diff math needs. Always called
    again at POST-apply time rather than trusting anything computed at GET
    time - that is the whole point of the renderedChecked mechanism (plan
    D1, REVIEW.md F1/F2): removals/additions are judged against what is true
    RIGHT NOW, scoped down to the rendered-universe snapshot the form carries.
    """
    now = datetime.datetime.now(datetime.UTC)
    activeOwnerIds = set(
        EventOwners.objects.filter(Q(isPermanent=True) | Q(expiration__gt=now))
        .values_list("id", flat=True)
    )
    authorizedOwnerIds = set(user.eventAuthorizations.values_list("id", flat=True))
    pendingOwnerIds = set(
        AccessRequests.objects.filter(
            requester=user, status=AccessRequests.Status.REQUESTED, owner__isnull=False,
        ).values_list("owner_id", flat=True)
    )

    requestablePermissionIds = set(permissions.getRequestablePermissions().values_list("id", flat=True))
    directPermissionIds = set(
        user.user_permissions.filter(id__in=requestablePermissionIds).values_list("id", flat=True)
    )
    if user.is_superuser:
        # Nothing direct is meaningfully "unlocked" for a superuser - has_perm
        # is always True and there is no box that ever does anything (R3).
        groupGrantedPermissionIds = set()
        lockedPermissionIds = set(requestablePermissionIds)
        heldPermissionIds = set(requestablePermissionIds)
    else:
        groupGrantedPermissionIds = set(
            Permission.objects.filter(group__user=user, id__in=requestablePermissionIds)
            .values_list("id", flat=True)
        )
        lockedPermissionIds = set(groupGrantedPermissionIds)
        heldPermissionIds = directPermissionIds | groupGrantedPermissionIds
    pendingPermissionIds = set(
        AccessRequests.objects.filter(
            requester=user, status=AccessRequests.Status.REQUESTED, permission__isnull=False,
        ).values_list("permission_id", flat=True)
    )

    return {
        "activeOwnerIds": activeOwnerIds,
        "authorizedOwnerIds": authorizedOwnerIds,
        "pendingOwnerIds": pendingOwnerIds,
        "requestablePermissionIds": requestablePermissionIds,
        "directPermissionIds": directPermissionIds,
        "lockedPermissionIds": lockedPermissionIds,
        "heldPermissionIds": heldPermissionIds,
        "pendingPermissionIds": pendingPermissionIds,
    }


def _buildAccessChecklist(user):
    """Build the request-access page's render state: which committees and
    permissions to show, whether each is checked/locked, and the
    renderedChecked snapshot the form must carry to POST (plan D1/D2/D4/D6).

    Rendering rule (D6): never hide an item the member currently holds - hide
    only pending AND NOT held. A held-and-pending item renders checked (the
    reconciliation above already closed the stale pending row by the time this
    runs, so by construction nothing here is ever both).

    A locked permission (group-granted, or ANY permission for a superuser - R3)
    is never added to renderedChecked, regardless of whether it's checked -
    that is D4/F6's "no hidden mirror for locked rows": a locked item is
    out-of-universe on the server side, full stop, so a forged renderedChecked
    entry naming one can never make it a removal candidate later.
    """
    _reconcileHeldAndPending(user)
    state = _currentAccessState(user)

    renderedChecked = set()

    pendingDelegatedCounts = dict(
        DelegatedEvents.objects.filter(
            owner_id__in=state["authorizedOwnerIds"], status=DelegatedEvents.Status.REQUESTED,
        ).values("owner_id").annotate(count=Count("id")).values_list("owner_id", "count")
    )
    authorizerCounts = dict(
        EventOwners.objects.filter(id__in=state["activeOwnerIds"])
        .annotate(authorizerCount=Count("authorizers"))
        .values_list("id", "authorizerCount")
    )

    committeeRows = []
    for owner in EventOwners.objects.filter(id__in=state["activeOwnerIds"]).order_by("name"):
        held = owner.id in state["authorizedOwnerIds"]
        pending = owner.id in state["pendingOwnerIds"]
        if pending and not held:
            continue
        key = _ownerKey(owner.id)
        if held:
            renderedChecked.add(key)
        committeeRows.append({
            "owner": owner,
            "key": key,
            "checked": held,
            "authorizerCount": authorizerCounts.get(owner.id, 0) if held else 0,
            "pendingDelegatedEventCount": pendingDelegatedCounts.get(owner.id, 0) if held else 0,
        })

    byCategory = {}
    approveOtherHolderCount = _nonSuperuserApproveHolderCount(excludeUserId=user.id)
    approveAccessRequestCodename = permissions.APPROVE_ACCESS_REQUEST.split(".")[1]
    for permission in permissions.getRequestablePermissions():
        held = permission.id in state["heldPermissionIds"]
        pending = permission.id in state["pendingPermissionIds"]
        if pending and not held:
            continue
        locked = permission.id in state["lockedPermissionIds"]
        key = _permissionKey(permission.id)
        if held and not locked:
            renderedChecked.add(key)
        lockHint = None
        if locked:
            if user.is_superuser:
                lockHint = "Locked - superusers hold every permission implicitly."
            else:
                groupNames = ", ".join(
                    user.groups.filter(permissions=permission).order_by("name").values_list("name", flat=True)
                )
                if permission.id in state["directPermissionIds"]:
                    lockHint = (
                        f"Locked - also granted via {groupNames}, so the direct grant underneath it "
                        "can't be dropped here. An admin can remove the direct grant on Manage Member Access."
                    )
                else:
                    lockHint = f"Locked - granted via {groupNames}. Ask an admin to change your groups instead."
        category = permissions.getPermissionCategory(permission.codename)
        byCategory.setdefault(category, []).append({
            "permission": permission,
            "shortLabel": permissions.shortPermissionLabel(permission.name),
            "key": key,
            "checked": held,
            "locked": locked,
            "lockHint": lockHint,
            "isApproveAccessRequest": permission.codename == approveAccessRequestCodename,
            "otherApproveHolderCount": approveOtherHolderCount,
        })

    categoryOrder = [title for title, _ in permissions.PERMISSION_CATEGORIES] + ["Other"]
    permissionSections = [
        {"title": title, "rows": byCategory[title]}
        for title in categoryOrder
        if title in byCategory
    ]
    return committeeRows, permissionSections, renderedChecked


def _computeSelfServiceDiff(user, form):
    """The diff math itself (plan D1, REVIEW.md F1/F2's fix):

        removals  = (renderedChecked - submittedChecked) & removalEligible
        additions = (submittedChecked - renderedChecked) - heldKeys - pendingKeys

    ``removalEligible`` is every currently-authorized committee plus every
    permission the member holds DIRECTLY and UNLOCKED right now - re-read fresh,
    never assumed from the stale render. That second condition is D4's teeth: a
    permission that became group-granted between GET and POST is excluded even
    if it's (genuinely or by forgery) present in renderedChecked, so the direct
    grant underneath a locked permission is never droppable via self-service.

    Anything the checklist did not render as an active checkbox - an expired
    owner's authorization, a locked permission - was never added to
    renderedChecked in the first place (see _buildAccessChecklist), so it can
    never appear in ``removals`` regardless of what the POST claims. That is the
    whole protection for REVIEW.md's Scenario B (an authorizer of an expired
    owner) and for the stale-tab race (F2) - no special-case code, just scope.

    Returns (removals, additions, state) as sets of opaque item keys plus the
    fresh state dict _currentAccessState produced, so the caller can reuse it.
    """
    state = _currentAccessState(user)

    renderedChecked = set(form.cleaned_data[SelfServiceAccessForm.Keys.RENDERED_CHECKED])
    submittedChecked = (
        {_ownerKey(owner.id) for owner in form.cleaned_data[SelfServiceAccessForm.Keys.COMMITTEES]}
        | {_permissionKey(permission.id) for permission in form.cleaned_data[SelfServiceAccessForm.Keys.PERMISSIONS]}
    )

    unlockedDirectPermissionKeys = {
        _permissionKey(permissionId)
        for permissionId in state["directPermissionIds"] - state["lockedPermissionIds"]
    }
    removalEligible = (
        {_ownerKey(ownerId) for ownerId in state["authorizedOwnerIds"]}
        | unlockedDirectPermissionKeys
    )
    removals = (renderedChecked - submittedChecked) & removalEligible

    heldKeys = (
        {_ownerKey(ownerId) for ownerId in state["authorizedOwnerIds"]}
        | {_permissionKey(permissionId) for permissionId in state["heldPermissionIds"]}
    )
    pendingKeys = (
        {_ownerKey(ownerId) for ownerId in state["pendingOwnerIds"]}
        | {_permissionKey(permissionId) for permissionId in state["pendingPermissionIds"]}
    )
    additions = (submittedChecked - renderedChecked) - heldKeys - pendingKeys

    return removals, additions, state


def _applySelfServiceDiff(request, user, removals, additions, justification) -> dict:
    """Apply the diff (plan D1/D3/D9): removals immediately with no approval,
    then one AccessRequests row per addition sharing a single batchId. One row
    per addition is not a style choice - AccessRequests.clean() enforces
    exactly one target per row, and the reviewer tiers differ per target type
    (a committee's authorizers may decide an owner row; a permission row is
    admin-only), so one row cannot carry both.

    A stale addition target (deleted between form validation and this loop -
    genuinely concurrent, not reproducible without a race) is dropped rather
    than failing the whole batch (D6's "process the rest" rule) - re-fetching
    with .get() here, instead of trusting the form's already-validated model
    instances, is what makes that race detectable at all.

    Returns a plain-dict, JSON-safe result (it round-trips through the session
    across the POST-redirect-GET in request_access - D10).
    """
    removedOwners = []
    removedPermissions = []
    for key in sorted(removals):
        kind, _, rawId = key.partition(":")
        if kind == SelfServiceAccessForm.OWNER_PREFIX:
            owner = EventOwners.objects.filter(id=rawId).first()
            if owner is None:
                continue
            groupDropped = revokeCommitteeMembership(user, owner)
            removedOwners.append({"name": owner.name, "groupDropped": groupDropped})
            logger.info(
                "RequestAccess: %s left '%s' (self-service, no approval needed, roleGroupDropped=%s)",
                user.getUserNameString(), owner.name, groupDropped,
            )
        else:
            permission = Permission.objects.filter(id=rawId).first()
            if permission is None:
                continue
            user.user_permissions.remove(permission)
            removedPermissions.append(permission.name)
            logger.info(
                "RequestAccess: %s dropped '%s' (self-service, no approval needed)",
                user.getUserNameString(), permission.name,
            )

    batchId = uuid.uuid4() if additions else None
    createdRequests = []
    droppedCount = 0
    for key in sorted(additions):
        kind, _, rawId = key.partition(":")
        try:
            if kind == SelfServiceAccessForm.OWNER_PREFIX:
                owner = EventOwners.objects.get(id=rawId)
                accessRequest = AccessRequests.objects.create(
                    requester=user, owner=owner, justification=justification,
                    status=AccessRequests.Status.REQUESTED, batchId=batchId,
                )
            else:
                permission = Permission.objects.get(id=rawId)
                accessRequest = AccessRequests.objects.create(
                    requester=user, permission=permission, justification=justification,
                    status=AccessRequests.Status.REQUESTED, batchId=batchId,
                )
        except (EventOwners.DoesNotExist, Permission.DoesNotExist) as err:
            logger.warning(
                "RequestAccess: dropped stale addition %s for %s (%s)",
                key, user.getUserNameString(), err,
            )
            droppedCount += 1
            continue
        logger.info(
            "RequestAccess: %s requested %s (batch %s)",
            user.getUserNameString(), accessRequest.getTargetDescription(), batchId,
        )
        createdRequests.append(accessRequest)

    if createdRequests:
        _sendBatchRequestEmails(request, createdRequests)

    totalPending = AccessRequests.objects.filter(
        requester=user, status=AccessRequests.Status.REQUESTED
    ).count()

    return {
        "removedOwners": removedOwners,
        "removedPermissions": removedPermissions,
        "createdDescriptions": [accessRequest.getTargetDescription() for accessRequest in createdRequests],
        "droppedCount": droppedCount,
        "totalPending": totalPending,
    }


@login_required
def request_access(request):
    """The one-form request+revoke page (plan access-self-service-form).

    Any logged-in member may ask to join an event owner (committee) or for one
    of the custom tools.* permissions (needs approval, unchanged in spirit from
    the old dropdown), AND may drop access they already hold (new: immediate,
    no approval - D1). Checked means held; unchecking something the member
    holds is applied right away, but ONLY within the rendered universe the
    renderedChecked snapshot captured - see _computeSelfServiceDiff for why a
    plain absolute diff against DB state is unsafe here.

    POST-redirect-GET (D10): the result is stashed in the session and the
    response redirects back to this same URL, so a back-button resubmit
    replays nothing - the GET branch below either shows that one result once
    (session.pop) or renders a fresh checklist.
    """
    user = request.user
    if request.method == "POST":
        form = SelfServiceAccessForm(request.POST)
        missingJustification = False
        if form.is_valid():
            removals, additions, _state = _computeSelfServiceDiff(user, form)
            justification = form.cleaned_data[SelfServiceAccessForm.Keys.JUSTIFICATION].strip()
            if additions and not justification:
                # Removals never need a reason (D1) - only additions do, and
                # only when there are any - so this is checked here rather than
                # as a static field requirement.
                form.add_error(
                    SelfServiceAccessForm.Keys.JUSTIFICATION,
                    "Tell the approvers why you need this - only required when you're adding something.",
                )
                missingJustification = True
            else:
                result = _applySelfServiceDiff(request, user, removals, additions, justification)
                request.session["accessRequestResult"] = result
                return redirect("request-access")

        committeeRows, permissionSections, renderedChecked = _buildAccessChecklist(user)
        if missingJustification:
            # Only the justification textarea failed - re-render the member's
            # own submitted checkmarks rather than resetting to current DB
            # state, so fixing the one error doesn't also discard their picks.
            submittedOwnerIds = {
                owner.id for owner in form.cleaned_data[SelfServiceAccessForm.Keys.COMMITTEES]
            }
            submittedPermissionIds = {
                permission.id for permission in form.cleaned_data[SelfServiceAccessForm.Keys.PERMISSIONS]
            }
            for row in committeeRows:
                row["checked"] = row["owner"].id in submittedOwnerIds
            for section in permissionSections:
                for row in section["rows"]:
                    if not row["locked"]:
                        row["checked"] = row["permission"].id in submittedPermissionIds
        return render(request, "tools/access-requests/request.html", {
            "form": form,
            "committeeRows": committeeRows,
            "permissionSections": permissionSections,
            "renderedChecked": renderedChecked,
        })

    resultData = request.session.pop("accessRequestResult", None)
    if resultData is not None:
        return render(request, "tools/access-requests/created.html", {"result": resultData})

    committeeRows, permissionSections, renderedChecked = _buildAccessChecklist(user)
    return render(request, "tools/access-requests/request.html", {
        "form": SelfServiceAccessForm(),
        "committeeRows": committeeRows,
        "permissionSections": permissionSections,
        "renderedChecked": renderedChecked,
    })


@login_required
def access_request_list(request):
    myRequests = AccessRequests.objects.filter(requester=request.user).order_by("-dateCreated")
    pending = (
        AccessRequests.objects.filter(status=AccessRequests.Status.REQUESTED)
        .exclude(requester=request.user)
        .order_by("-dateCreated")
    )
    actionable = [r for r in pending if r.canBeReviewedBy(request.user)]
    return render(
        request,
        "tools/access-requests/list.html",
        {"myRequests": myRequests, "actionable": actionable},
    )


@login_required
def my_access(request):
    """What the logged-in member can currently do, and where each piece of
    access comes from."""
    groupsInfo = []
    for group in request.user.groups.prefetch_related("permissions__content_type").order_by("name"):
        groupsInfo.append({
            "name": group.name,
            "permissionNames": [
                permission.name
                for permission in group.permissions.all()
                if permission.content_type.model == "permissionrights"
            ],
        })

    directIds = set(request.user.user_permissions.values_list("id", flat=True))
    heldPermissions = []
    for permission in permissions.getRequestablePermissions():
        if not request.user.has_perm("tools." + permission.codename):
            continue
        heldPermissions.append({
            "name": permission.name,
            "source": _permissionSource(request.user, permission, directIds),
        })

    return render(request, "tools/access/my-access.html", {
        "groupsInfo": groupsInfo,
        "heldPermissions": heldPermissions,
    })


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_access(request):
    users = (
        User.objects.filter(is_active=True)
        .prefetch_related("groups")
        .order_by("username")
    )
    customIds = set(permissions.getRequestablePermissions().values_list("id", flat=True))
    rows = []
    for member in users:
        rows.append({
            "user": member,
            "groups": list(member.groups.all()),
            "directPermissionCount": member.user_permissions.filter(id__in=customIds).count(),
        })
    return render(request, "tools/access/manage-list.html", {"rows": rows})


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_access_user(request, userId):
    try:
        target = User.objects.get(id=userId)
    except User.DoesNotExist:
        return redirect("manage-access")

    customIds = set(permissions.getRequestablePermissions().values_list("id", flat=True))
    saved = False
    if request.method == "POST":
        form = ManageAccessForm(request.POST)
        if form.is_valid():
            target.groups.set(form.cleaned_data[ManageAccessForm.Keys.GROUPS])
            # Only manage the custom tools.* permissions here - leave any other
            # directly-assigned permissions (e.g. model perms for staff) alone
            keepOthers = list(target.user_permissions.exclude(id__in=customIds))
            target.user_permissions.set(
                keepOthers + list(form.cleaned_data[ManageAccessForm.Keys.PERMISSIONS])
            )
            logger.info(
                "ManageAccess: %s set %s groups=%s directPerms=%s",
                request.user.getUserNameString(),
                target.getUserNameString(),
                [group.name for group in form.cleaned_data[ManageAccessForm.Keys.GROUPS]],
                [permission.codename for permission in form.cleaned_data[ManageAccessForm.Keys.PERMISSIONS]],
            )
            # A direct grant satisfies any pending request for the same thing -
            # close those out so they stop cluttering approver queues
            selectedGroupIds = {group.id for group in form.cleaned_data[ManageAccessForm.Keys.GROUPS]}
            selectedPermissionIds = {
                permission.id for permission in form.cleaned_data[ManageAccessForm.Keys.PERMISSIONS]
            }
            pendingRequests = AccessRequests.objects.filter(
                requester=target, status=AccessRequests.Status.REQUESTED
            )
            for pending in pendingRequests:
                satisfied = (
                    (pending.group_id is not None and pending.group_id in selectedGroupIds)
                    or (pending.permission_id is not None and pending.permission_id in selectedPermissionIds)
                )
                if not satisfied:
                    continue
                logger.info(
                    "ManageAccess: closing pending request %d - access granted directly",
                    pending.id,
                )
                pending.status = AccessRequests.Status.APPROVED
                pending.reviewer = request.user
                pending.dateReviewed = datetime.datetime.now(datetime.UTC)
                pending.reason = "Access granted directly"
                pending.save()
                _sendDecisionEmail(pending)
            saved = True
    else:
        form = ManageAccessForm(initial={
            ManageAccessForm.Keys.GROUPS: target.groups.all(),
            ManageAccessForm.Keys.PERMISSIONS: target.user_permissions.filter(id__in=customIds),
        })

    # Hand the template self-describing rows (rendered as plain checkboxes
    # named groups/permissions, which is exactly what ManageAccessForm parses
    # back on POST). Built after the save so a successful POST shows the
    # member's new state.
    groups = list(Group.objects.prefetch_related("permissions").order_by("name"))
    targetGroupIds = set(target.groups.values_list("id", flat=True))
    targetDirectIds = set(
        target.user_permissions.filter(id__in=customIds).values_list("id", flat=True)
    )

    groupRows = []
    for group in groups:
        customPermissions = [p for p in group.permissions.all() if p.id in customIds]
        groupRows.append({
            "group": group,
            "permissionNames": [permissions.shortPermissionLabel(p.name) for p in customPermissions],
            "checked": group.id in targetGroupIds,
        })

    allPermissions = list(permissions.getRequestablePermissions())
    grantedBy = {permission.id: [] for permission in allPermissions}
    for group in groups:
        for groupPermission in group.permissions.all():
            if groupPermission.id in grantedBy:
                grantedBy[groupPermission.id].append(group.id)

    byCategory = {}
    for permission in allPermissions:
        category = permissions.getPermissionCategory(permission.codename)
        byCategory.setdefault(category, []).append(permission)

    categoryOrder = [title for title, _ in permissions.PERMISSION_CATEGORIES] + ["Other"]
    permissionSections = []
    for title in categoryOrder:
        if title not in byCategory:
            continue
        permissionSections.append({
            "title": title,
            "rows": [{
                "permission": permission,
                "shortLabel": permissions.shortPermissionLabel(permission.name),
                "viaGroupIds": ",".join(str(groupId) for groupId in grantedBy[permission.id]),
                "checked": permission.id in targetDirectIds,
            } for permission in byCategory[title]],
        })

    return render(request, "tools/access/manage-user.html", {
        "target": target,
        "form": form,
        "saved": saved,
        "groupRows": groupRows,
        "permissionSections": permissionSections,
    })


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_groups(request):
    createForm = GroupForm()
    if request.method == "POST":
        createForm = GroupForm(request.POST)
        if createForm.is_valid():
            group = Group.objects.create(name=createForm.cleaned_data[GroupForm.Keys.NAME])
            logger.info(
                "ManageGroups: %s created group '%s'",
                request.user.getUserNameString(), group.name,
            )
            return redirect("manage-group", groupId=group.id)

    customIds = set(permissions.getRequestablePermissions().values_list("id", flat=True))
    rows = []
    for group in Group.objects.prefetch_related("permissions").order_by("name"):
        customPermissions = [p for p in group.permissions.all() if p.id in customIds]
        rows.append({
            "group": group,
            "memberCount": group.user_set.filter(is_active=True).count(),
            "permissionNames": [permissions.shortPermissionLabel(p.name) for p in customPermissions],
        })
    return render(request, "tools/access/manage-groups.html", {
        "rows": rows,
        "createForm": createForm,
        "deletedName": request.GET.get("deleted", ""),
    })


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_group(request, groupId):
    try:
        group = Group.objects.get(id=groupId)
    except Group.DoesNotExist:
        return redirect("manage-groups")

    customIds = set(permissions.getRequestablePermissions().values_list("id", flat=True))
    saved = False
    if request.method == "POST":
        form = GroupForm(request.POST, group=group)
        if form.is_valid():
            previousMemberIds = set(group.user_set.values_list("id", flat=True))
            group.name = form.cleaned_data[GroupForm.Keys.NAME]
            group.save()
            # Only manage the custom tools.* permissions here - leave any model
            # permissions attached in /admin/ alone (mirrors manage_access_user)
            keepOtherPermissions = list(group.permissions.exclude(id__in=customIds))
            group.permissions.set(
                keepOtherPermissions + list(form.cleaned_data[GroupForm.Keys.PERMISSIONS])
            )
            # Membership changes arrive as deltas (see GroupForm), so members
            # not named in the request are never touched
            addedMembers = [
                member for member in form.cleaned_data[GroupForm.Keys.ADD_MEMBERS]
                if member.id not in previousMemberIds
            ]
            group.user_set.add(*addedMembers)
            group.user_set.remove(*form.cleaned_data[GroupForm.Keys.REMOVE_MEMBERS])
            logger.info(
                "ManageGroups: %s set group '%s' permissions=%s added=%s removed=%s",
                request.user.getUserNameString(),
                group.name,
                [p.codename for p in form.cleaned_data[GroupForm.Keys.PERMISSIONS]],
                [member.username for member in addedMembers],
                [member.username for member in form.cleaned_data[GroupForm.Keys.REMOVE_MEMBERS]],
            )
            # Adding someone here satisfies their pending request for this group -
            # close those out, same as a direct grant on the member page
            newMemberIds = {member.id for member in addedMembers}
            if newMemberIds:
                pendingRequests = AccessRequests.objects.filter(
                    group=group,
                    status=AccessRequests.Status.REQUESTED,
                    requester_id__in=newMemberIds,
                )
                for pending in pendingRequests:
                    logger.info(
                        "ManageGroups: closing pending request %d - access granted directly",
                        pending.id,
                    )
                    pending.status = AccessRequests.Status.APPROVED
                    pending.reviewer = request.user
                    pending.dateReviewed = datetime.datetime.now(datetime.UTC)
                    pending.reason = "Access granted directly"
                    pending.save()
                    _sendDecisionEmail(pending)
            saved = True
    else:
        form = GroupForm(group=group, initial={GroupForm.Keys.NAME: group.name})

    # Same self-describing-row pattern as manage_access_user; built after the
    # save so a successful POST shows the group's new state
    groupPermissionIds = set(group.permissions.values_list("id", flat=True))
    allPermissions = list(permissions.getRequestablePermissions())
    byCategory = {}
    for permission in allPermissions:
        category = permissions.getPermissionCategory(permission.codename)
        byCategory.setdefault(category, []).append(permission)

    categoryOrder = [title for title, _ in permissions.PERMISSION_CATEGORIES] + ["Other"]
    permissionSections = []
    for title in categoryOrder:
        if title not in byCategory:
            continue
        permissionSections.append({
            "title": title,
            "rows": [{
                "permission": permission,
                "shortLabel": permissions.shortPermissionLabel(permission.name),
                "checked": permission.id in groupPermissionIds,
            } for permission in byCategory[title]],
        })

    # Only the current roster renders; additions come through the search
    # endpoint below, so the page stays light no matter how big the org gets
    memberRows = [
        {"user": member}
        for member in group.user_set.filter(is_active=True).order_by("username")
    ]
    activeMemberCount = len(memberRows)

    return render(request, "tools/access/manage-group.html", {
        "group": group,
        "form": form,
        "saved": saved,
        "permissionSections": permissionSections,
        "memberRows": memberRows,
        "activeMemberCount": activeMemberCount,
    })


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_group_member_search(request, groupId):
    """Typeahead backing for the group page's add-member box: the top matches
    among active users who aren't already in the group."""
    try:
        group = Group.objects.get(id=groupId)
    except Group.DoesNotExist:
        return JsonResponse({"results": []})

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
        .exclude(groups=group)
        .order_by("username")[:10]
    )
    return JsonResponse({"results": [{
        "id": member.id,
        "username": member.username,
        "fullName": f"{member.first_name} {member.last_name}".strip(),
        "email": member.email,
    } for member in matches]})


@login_required
@permission_required(permissions.APPROVE_ACCESS_REQUEST)
def manage_group_delete(request, groupId):
    if request.method != "POST":
        return redirect("manage-groups")
    try:
        group = Group.objects.get(id=groupId)
    except Group.DoesNotExist:
        return redirect("manage-groups")

    # The page's JS keeps the delete button disabled until the typed name
    # matches; this is the server-side backstop.
    if request.POST.get("confirmName", "").strip() != group.name:
        logger.warning(
            "ManageGroups: %s sent a delete for '%s' with a mismatched confirmation",
            request.user.getUserNameString(), group.name,
        )
        return redirect("manage-group", groupId=group.id)

    # A pending request for a deleted group can never be granted - deny it
    # with an explanation rather than leaving it stranded in approver queues.
    # (AccessRequests.group is SET_NULL, so reviewed history survives.)
    pendingRequests = AccessRequests.objects.filter(
        group=group, status=AccessRequests.Status.REQUESTED
    )
    for pending in pendingRequests:
        pending.status = AccessRequests.Status.DENIED
        pending.reviewer = request.user
        pending.dateReviewed = datetime.datetime.now(datetime.UTC)
        pending.reason = f"The group '{group.name}' was deleted"
        pending.save()
        _sendDecisionEmail(pending)

    deletedName = group.name
    memberCount = group.user_set.count()
    group.delete()
    logger.info(
        "ManageGroups: %s deleted group '%s' (%d members at deletion)",
        request.user.getUserNameString(), deletedName, memberCount,
    )
    return redirect(
        reverse("manage-groups") + "?" + urllib.parse.urlencode({"deleted": deletedName})
    )


@login_required
def review_access_request(request, id):
    try:
        accessRequest = AccessRequests.objects.get(id=id)
    except AccessRequests.DoesNotExist:
        # Render the same page as the not-authorized case so probing ids
        # doesn't reveal which requests exist
        logger.info("ReviewAccessRequest: Request %s does not exist", id)
        return render(request, "tools/access-requests/unauthorized.html")

    # Only the requester and would-be reviewers may see the request at all
    isRequester = accessRequest.requester_id == request.user.id
    if not isRequester and not accessRequest.canBeReviewedBy(request.user):
        logger.error(
            "ReviewAccessRequest: User %s is not allowed to review request %s",
            request.user.email,
            str(accessRequest.id),
        )
        return render(request, "tools/access-requests/unauthorized.html")

    canAct = (
        accessRequest.status == AccessRequests.Status.REQUESTED
        and accessRequest.canBeReviewedBy(request.user)
    )

    if request.method == "POST" and canAct:
        form = ReviewAccessRequestForm(request.POST)
        if not form.is_valid():
            logger.error("ReviewAccessRequest: Submitted form is not valid")
            return render(
                request,
                "tools/access-requests/review.html",
                {"accessRequest": accessRequest, "form": form, "canAct": True},
            )
        formData = form.cleaned_data
        # Serialize concurrent reviewers: re-read the row under a lock and
        # re-check the status so two simultaneous approvals can't both win
        # (and the audit fields reflect whoever actually decided first).
        with transaction.atomic():
            accessRequest = AccessRequests.objects.select_for_update().get(id=id)
            if accessRequest.status != AccessRequests.Status.REQUESTED:
                logger.info(
                    "ReviewAccessRequest: Request %d was already reviewed, ignoring decision from %s",
                    accessRequest.id,
                    request.user.getUserNameString(),
                )
                return render(
                    request,
                    "tools/access-requests/review.html",
                    {"accessRequest": accessRequest, "canAct": False},
                )
            if formData[ReviewAccessRequestForm.Keys.APPROVE] == "YES":
                logger.info(
                    "ReviewAccessRequest: %s approved request %d",
                    request.user.getUserNameString(),
                    accessRequest.id,
                )
                if accessRequest.requester is not None:
                    accessRequest.grantTo(accessRequest.requester)
                accessRequest.status = AccessRequests.Status.APPROVED
            else:
                logger.info(
                    "ReviewAccessRequest: %s denied request %d",
                    request.user.getUserNameString(),
                    accessRequest.id,
                )
                accessRequest.status = AccessRequests.Status.DENIED
            accessRequest.reviewer = request.user
            accessRequest.dateReviewed = datetime.datetime.now(datetime.UTC)
            accessRequest.reason = formData[ReviewAccessRequestForm.Keys.REASON]
            accessRequest.save()
        # The grant is already committed - a failed email never rolls it back
        _sendDecisionEmail(accessRequest)
        return render(
            request,
            "tools/access-requests/review.html",
            {"accessRequest": accessRequest, "canAct": False},
        )

    context = {"accessRequest": accessRequest, "canAct": canAct}
    if canAct:
        context["form"] = ReviewAccessRequestForm()
    return render(request, "tools/access-requests/review.html", context)
