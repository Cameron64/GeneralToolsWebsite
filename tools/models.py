from urllib.parse import urlparse

import logging

from django.db import models
from django.contrib.auth.models import AbstractUser, Group, Permission
from django.utils import timezone as djangoTimezone
from .EventAutomation.EventAutomationDriver import EventInfo, ActionNetworkAutomation
import datetime
import pytz
from django.urls import reverse
from . import permissions
from . import utils
from .resolutionText import normalizedTextHash
from .ActionNetworkAPI.migValidator import MIGStatus
from .timezones import DateTimeWithAcceptedTimeZone

logger = logging.getLogger(__name__)

# Approving a committee (EventOwner) join grants the full event-lead capability
# through this managed role group. The EventOwner's authorizer list scopes which
# owner the member may act for; this group carries the page-level event
# permissions that the scoping is otherwise inert without. The lead bundle is
# publish + approve delegated events (plus the two list views needed to use them).
#
# Garrigan's call (2026-06): bundle committee membership with the lead capability
# for now, but keep the grant structured as two separable steps (see grantTo) so
# we can split them later - e.g. if enough non-lead members start joining that
# membership should stop implying publish/approve rights. To separate, stop
# granting EVENT_LEAD_ROLE_GROUP on join and grant the narrower publish-only
# "Event Publishers" group (and "Event Approvers" for approvers) instead.
#
# Decided 2026-08-11 (Cam, plan v3 D5): this group is MANAGED/DERIVED, not a
# free-standing role - membership means "authorizes at least one EventOwner",
# full stop. revokeCommitteeMembership() below is the reconciler: it strips the
# group the moment eventAuthorizations goes empty, regardless of how membership
# was acquired. That includes hand-grants made outside grantTo - Manage Member
# Access (target.groups.set), Manage Groups (group.user_set.add/remove), and
# /admin/'s group member widget all let an admin add someone here directly, and
# under this rule that grant is NOT durable: it gets reconciled away on the
# member's next committee-leave. If an admin wants to hand someone view-only
# event access without committee membership, put them in a narrower group
# instead (the seed's "Event Publishers" / "Event Approvers" pattern, scoped to
# VIEW_PUBLISHED_EVENTS / VIEW_DELEGATED_EVENTS) - Event Leads is not a place to
# park view-only grants, because they will not stick.
EVENT_LEAD_ROLE_GROUP = "Event Leads"
EVENT_LEAD_PERMISSIONS = (
    permissions.PUBLISH_EVENT,
    permissions.VIEW_PUBLISHED_EVENTS,
    permissions.VIEW_DELEGATED_EVENTS,
    permissions.APPROVE_DELEGATED_EVENT,
)


class User(AbstractUser):
    def getUserNameString(self) -> str:
        return f"{self.first_name} {self.last_name} - {self.email}"

    def getDisplayName(self) -> str:
        """Compact name for list tables (detail pages keep name + email)."""
        fullName = f"{self.first_name} {self.last_name}".strip()
        return fullName or self.username

class EventOwners(models.Model):
    name = models.CharField(max_length=100, unique=True)
    authorizers = models.ManyToManyField(User, related_name="eventAuthorizations")
    expiration = models.DateTimeField()
    isPermanent = models.BooleanField(default=False)
    def isActive(self):
        if self.isPermanent or datetime.datetime.now(datetime.UTC) < self.expiration:
            return True
        return False

    def __str__(self):
        return self.name

# List of all previously created events
# All date-times are in UTC
class PostedEvents(models.Model):
    title = models.CharField(max_length=500)
    start = models.DateTimeField()
    end = models.DateTimeField()
    timezone = models.CharField(max_length=50)

    locationName = models.CharField(max_length=500)
    streetAddress = models.CharField(max_length=500)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    zip = models.CharField(max_length=10)
    country = models.CharField(max_length=100)

    description = models.TextField()
    instructions = models.TextField()

    dateCreated = models.DateTimeField()
    datePublished = models.DateTimeField()

    # Not sure how big the links can get so using text fields
    anManageLink = models.TextField()
    anShareLink = models.TextField()
    gCalLink = models.TextField()
    zoomLink = models.TextField()
    zoomAccount = models.CharField(max_length=100)
    zoomRequired = models.BooleanField(default=True)

    creator = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name="postedEventCreator")
    authorizer = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name="postedEventAuthorizer")
    reason = models.TextField()

    owner = models.ForeignKey(EventOwners, on_delete=models.SET_NULL, blank=True, null=True)

    def __str__(self) -> str:
        return f"{self.title} ({self.getStartLocalizedStr()})"

    def getCreatorName(self) -> str:
        if self.creator is None:
            return ""
        return self.creator.getUserNameString()
    
    def getApproverName(self) -> str:
        if self.authorizer is None:
            return ""
        return self.authorizer.getUserNameString()
    
    def getOwnerName(self) -> str:
        if self.owner is None:
            return ""
        return self.owner.name
    
    def getUrl(self) -> str:
        return reverse("event-detail", kwargs={"pk" : self.id})
    
    def getStartLocalizedStr(self) -> str:
        return self.getStartLocalized().strftime(utils.DATE_TIME_FORMAT)
    
    def getEndLocalizedStr(self) -> str:
        return self.getEndLocalized().strftime(utils.DATE_TIME_FORMAT)

    def getStartLocalized(self) -> datetime.datetime:
        utcTime = self.start
        # If naiive add in the UTC info
        if utcTime.tzinfo is None or utcTime.tzinfo.utcoffset(utcTime) is None:
            utcTimezone = pytz.utc
            utcTime = utcTimezone.localize(utcTime)
        timezone = pytz.timezone(self.timezone)
        localTime = utcTime.astimezone(timezone)
        return localTime
    
    def getEndLocalized(self) -> datetime.datetime:
        utcTime = self.end
        # If naiive add in the UTC info
        if utcTime.tzinfo is None or utcTime.tzinfo.utcoffset(utcTime) is None:
            utcTimezone = pytz.utc
            utcTime = utcTimezone.localize(utcTime)
        timezone = pytz.timezone(self.timezone)
        localTime = utcTime.astimezone(timezone)
        return localTime
    
    def getEventInfo(self) -> EventInfo:
        return EventInfo(title=self.title,
                         start=DateTimeWithAcceptedTimeZone(wallTime=self.getStartLocalized().replace(tzinfo=None), zoneName=self.timezone),
                         end=DateTimeWithAcceptedTimeZone(wallTime=self.getEndLocalized().replace(tzinfo=None), zoneName=self.timezone),
                         locationName=self.locationName,
                         streetAddress=self.streetAddress,
                         city=self.city,
                         state=self.state,
                         zip=self.zip,
                         description=self.description,
                         instructions=self.instructions,
                         country=self.country,
                         zoomRequired=self.zoomRequired)


 # List of events that have been created to be delegated to an authorizer
 # There will be duplication with approved events here and the events in PostedEvents, PostedEvents should be the truth of all published events
class DelegatedEvents(models.Model):
    class Status:
        REQUESTED = 0
        DENIED = 1
        APPROVED = 2

    title = models.CharField(max_length=500)
    start = models.DateTimeField()
    end = models.DateTimeField()
    timezone = models.CharField(max_length=50)

    locationName = models.CharField(max_length=500)
    streetAddress = models.CharField(max_length=500)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    zip = models.CharField(max_length=10)
    country = models.CharField(max_length=100)

    description = models.TextField()
    instructions = models.TextField()

    dateCreated = models.DateTimeField()
    dateReviewed = models.DateTimeField(null=True, blank=True, default=None)

    creator = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name="delegatedEventCreator")
    owner = models.ForeignKey(EventOwners, on_delete=models.SET_NULL, blank=True, null=True)
    approver = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name="delegatedEventApprover")

    status = models.IntegerField()
    reason = models.TextField(blank=True)

    zoomRequired = models.BooleanField(default=True)

    eventType = models.IntegerField(default=ActionNetworkAutomation.ANTypes.HYBRID)

    def getStatusAsString(self) -> str:
        if self.status == DelegatedEvents.Status.REQUESTED:
            return "Requested"
        elif self.status == DelegatedEvents.Status.DENIED:
            return "Denied"
        elif self.status == DelegatedEvents.Status.APPROVED:
            return "Approved"
        else:
            return f"Unkown {self.status}"
    
    def getCreatorName(self) -> str:
        if self.creator is None:
            return ""
        return self.creator.getUserNameString()
    
    def getApproverName(self) -> str:
        if self.approver is None:
            return ""
        return self.approver.getUserNameString()
    
    def getOwnerName(self) -> str:
        if self.owner is None:
            return ""
        return self.owner.name
    
    def canBeApprovedBy(self, user) -> bool:
        # Mirrors the two gates on approve_delegated_event: the Django
        # permission plus membership in the owner's authorizers.
        if self.status != DelegatedEvents.Status.REQUESTED or self.owner is None:
            return False
        return (user.has_perm(permissions.APPROVE_DELEGATED_EVENT)
                and self.owner.authorizers.filter(id=user.id).exists())

    def getUrlFor(self, user) -> str:
        # Send the viewer to the approve page only if they can actually act on
        # the request; everyone else gets the read-only detail page (the
        # approve view rejects them anyway).
        if self.canBeApprovedBy(user):
            return reverse("approve-delegated-event", kwargs={ "id" :self.id})
        return reverse("delegated-event-detail", kwargs={"pk" : self.id})

    def getStartLocalizedStr(self) -> str:
        return self.getStartLocalized().strftime(utils.DATE_TIME_FORMAT)

    def getEndLocalizedStr(self) -> str:
        return self.getEndLocalized().strftime(utils.DATE_TIME_FORMAT)

    def getStartLocalized(self) -> datetime.datetime:
        utcTime = self.start
        # If naiive add in the UTC info
        if utcTime.tzinfo is None or utcTime.tzinfo.utcoffset(utcTime) is None:
            utcTimezone = pytz.utc
            utcTime = utcTimezone.localize(utcTime)
        timezone = pytz.timezone(self.timezone)
        localTime = utcTime.astimezone(timezone)
        return localTime

    def getEndLocalized(self) -> datetime.datetime:
        utcTime = self.end
        # If naiive add in the UTC info
        if utcTime.tzinfo is None or utcTime.tzinfo.utcoffset(utcTime) is None:
            utcTimezone = pytz.utc
            utcTime = utcTimezone.localize(utcTime)
        timezone = pytz.timezone(self.timezone)
        localTime = utcTime.astimezone(timezone)
        return localTime

    def getEventInfo(self) -> EventInfo:
        return EventInfo(title=self.title,
                         start=DateTimeWithAcceptedTimeZone(wallTime=self.getStartLocalized().replace(tzinfo=None), zoneName=self.timezone),
                         end=DateTimeWithAcceptedTimeZone(wallTime=self.getEndLocalized().replace(tzinfo=None), zoneName=self.timezone),
                         locationName=self.locationName,
                         streetAddress=self.streetAddress,
                         city=self.city,
                         state=self.state,
                         zip=self.zip,
                         description=self.description,
                         instructions=self.instructions,
                         country=self.country,
                         eventType=self.eventType,
                         zoomRequired=self.zoomRequired)


# One background publish run (tools/tasks.py publishEventJob). The row carries
# the serialized form input out to the Huey worker and the outcome back to the
# polling status page - it is the ONLY source of truth for the run (Huey's
# result store is off). PostedEvents stays the truth of what was published;
# this is the truth of what was attempted.
class PublishJob(models.Model):
    class Status:
        PENDING = 0
        RUNNING = 1
        PUBLISHED = 2
        # A gCal conflict the user may force past (direct flow only)
        CONFLICT = 3
        # A Zoom conflict - no free account, nothing to force
        UNRESOLVEABLE = 4
        FAILED = 5

    TERMINAL_STATUSES = (Status.PUBLISHED, Status.CONFLICT, Status.UNRESOLVEABLE, Status.FAILED)

    class Kind:
        DIRECT = 0
        DELEGATED = 1

    # Schema version stamped into every payload; publishEventJob refuses any
    # other version so a future schema change fails loudly instead of
    # publishing garbage from a stale queued job.
    # v2 (issue #26): startIso/endIso changed from tz-aware local ISO to the
    # literal local WALL time (naive ISO), with the zone carried in "timezone"
    # and reapplied on rehydrate. Any v1 job still queued is rejected rather
    # than misread.
    PAYLOAD_VERSION = 2

    kind = models.IntegerField()
    status = models.IntegerField(default=Status.PENDING)
    # The serialized EventInfo + flags (see eventViews._buildEventPayload)
    payload = models.JSONField()
    # Conflict dicts written by the task (see tasks._serializeConflicts);
    # datetimes stored as NAIVE ISO strings already localized to the payload
    # timezone, mirroring the localize-then-strip the views used to do inline.
    conflicts = models.JSONField(default=list, blank=True)
    errorMessage = models.TextField(blank=True)

    creator = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name="publishJobsCreated")
    owner = models.ForeignKey(EventOwners, on_delete=models.SET_NULL, blank=True, null=True, related_name="publishJobs")
    postedEvent = models.ForeignKey(PostedEvents, on_delete=models.SET_NULL, blank=True, null=True, related_name="publishJobs")
    delegatedEvent = models.ForeignKey(DelegatedEvents, on_delete=models.SET_NULL, blank=True, null=True, related_name="publishJobs")

    createdAt = models.DateTimeField(auto_now_add=True)
    startedAt = models.DateTimeField(null=True, blank=True, default=None)
    finishedAt = models.DateTimeField(null=True, blank=True, default=None)

    def getStatusAsString(self) -> str:
        if self.status == PublishJob.Status.PENDING:
            return "Pending"
        elif self.status == PublishJob.Status.RUNNING:
            return "Running"
        elif self.status == PublishJob.Status.PUBLISHED:
            return "Published"
        elif self.status == PublishJob.Status.CONFLICT:
            return "Conflict"
        elif self.status == PublishJob.Status.UNRESOLVEABLE:
            return "Unresolveable Conflict"
        elif self.status == PublishJob.Status.FAILED:
            return "Failed"
        else:
            return f"Unknown {self.status}"

    def getKindAsString(self) -> str:
        if self.kind == PublishJob.Kind.DIRECT:
            return "Direct"
        elif self.kind == PublishJob.Kind.DELEGATED:
            return "Delegated"
        else:
            return f"Unknown {self.kind}"

    def isTerminal(self) -> bool:
        return self.status in PublishJob.TERMINAL_STATUSES

    def getStatusUrl(self) -> str:
        return reverse("publish-status", kwargs={"jobId": self.id})

    def getResultContext(self) -> dict:
        """The template context for this job's terminal status - a thin,
        single-purpose status->context map (the QRCode.resolveTarget rationale:
        one source of truth the view and tests both consume; split it if it
        grows). The view picks the template per (status x kind); this only
        shapes the data those existing templates already expect."""
        if self.status == PublishJob.Status.PUBLISHED:
            event = self.postedEvent
            return {
                "anShareLink": event.anShareLink if event else "",
                "anManageLink": event.anManageLink if event else "",
                "gCalLink": event.gCalLink if event else "",
                "zoomLink": event.zoomLink if event else "",
                "zoomAccount": event.zoomAccount if event else "",
            }
        if self.status in (PublishJob.Status.CONFLICT, PublishJob.Status.UNRESOLVEABLE):
            return {"conflicts": [
                {
                    "type": conflict["type"],
                    "title": conflict["title"],
                    "zoomUser": conflict["zoomUser"],
                    "start": DateTimeWithAcceptedTimeZone.fromDict(conflict["start"]),
                    "end": DateTimeWithAcceptedTimeZone.fromDict(conflict["end"]),
                }
                for conflict in self.conflicts
            ]}
        if self.status == PublishJob.Status.FAILED:
            return {"errorStr": self.errorMessage}
        return {}


# A member's request to join an event owner (committee), be added to a group,
# or be granted one of the custom tools.* permissions. Mirrors the
# DelegatedEvents request/approve pattern: the row is the request/audit record,
# approvers are reached by an emailed review link, and the actual grant happens
# on approval.
class AccessRequests(models.Model):
    class Status:
        REQUESTED = 0
        DENIED = 1
        APPROVED = 2

    requester = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="accessRequestsCreated",
    )

    # Exactly one of group / permission / owner is set - see clean().
    group = models.ForeignKey(
        Group, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="accessRequests",
    )
    permission = models.ForeignKey(
        Permission, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="accessRequests",
    )
    # Applying to join an event owner (committee): approval adds the requester
    # to owner.authorizers, and the owner's current authorizers are the peer
    # reviewers (see canBeReviewedBy). SET_NULL so an owner deleted in /admin/
    # leaves reviewed history intact, exactly like the group/permission targets.
    owner = models.ForeignKey(
        EventOwners, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="accessRequests",
    )

    justification = models.TextField(
        help_text="Why the requester needs this access, e.g. 'I run events for the Anti-ICE campaign.'",
    )

    status = models.IntegerField(default=Status.REQUESTED)
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="accessRequestsReviewed",
    )
    reason = models.TextField(blank=True)

    dateCreated = models.DateTimeField(auto_now_add=True)
    dateReviewed = models.DateTimeField(null=True, blank=True, default=None)

    # Joins the rows produced by one multi-item self-service submission (plan
    # access-self-service-form D3). Forced by canBeReviewedBy/clean(): a single
    # checklist submit can name both an owner and a permission, which have
    # different rightful approvers and clean() forbids on one row, so a batch
    # is N rows sharing this id instead of one row with N targets. NULL for
    # every row created before this field existed and for any single-item
    # request going forward - both are legacy/ordinary singleton "batches" of
    # one, not an error state. Population and grouping are the second agent's
    # forms.py/accessViews.py work; this migration only adds the column.
    batchId = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = "Access Request"

    def __str__(self) -> str:
        return f"{self.getRequesterName()} -> {self.getTargetDescription()} ({self.getStatusAsString()})"

    def clean(self):
        from django.core.exceptions import ValidationError

        targets = [
            self.group_id is not None,
            self.permission_id is not None,
            self.owner_id is not None,
        ]
        if sum(1 for t in targets if t) != 1:
            raise ValidationError(
                "An access request must target exactly one thing: a group, a permission, or an event owner."
            )

    def getStatusAsString(self) -> str:
        if self.status == AccessRequests.Status.REQUESTED:
            return "Requested"
        elif self.status == AccessRequests.Status.DENIED:
            return "Denied"
        elif self.status == AccessRequests.Status.APPROVED:
            return "Approved"
        else:
            return f"Unkown {self.status}"

    def getRequesterName(self) -> str:
        if self.requester is None:
            return ""
        return self.requester.getUserNameString()

    def getReviewerName(self) -> str:
        if self.reviewer is None:
            return ""
        return self.reviewer.getUserNameString()

    def getTargetDescription(self) -> str:
        if self.group is not None:
            return f"Group: {self.group.name}"
        if self.permission is not None:
            return f"Permission: {self.permission.name}"
        if self.owner is not None:
            return f"Event Owner: {self.owner.name}"
        return "Unknown target"

    def getUrl(self) -> str:
        return reverse("review-access-request", kwargs={"id": self.id})

    def getDateCreatedStr(self) -> str:
        """Rendered in the request's active timezone (the browser's, via
        TimezoneMiddleware) rather than raw UTC."""
        if not self.dateCreated:
            return ""
        return djangoTimezone.localtime(self.dateCreated).strftime(utils.DATE_TIME_FORMAT)

    def canBeReviewedBy(self, user) -> bool:
        """Approver rules: admins (superuser or approveAccessRequest holders)
        can review anything; existing members of the requested group - or
        existing authorizers of the requested event owner - can review requests
        for that group/owner; nobody reviews their own request."""
        if not user.is_authenticated or not user.is_active:
            return False
        if self.requester is not None and user.id == self.requester_id:
            return False
        if user.is_superuser or user.has_perm(permissions.APPROVE_ACCESS_REQUEST):
            return True
        if self.group_id is not None:
            return user.groups.filter(id=self.group_id).exists()
        if self.owner_id is not None:
            return self.owner.authorizers.filter(id=user.id).exists()
        return False

    def grantTo(self, requester) -> None:
        """Apply the requested access to the requester."""
        if self.group is not None:
            requester.groups.add(self.group)
        elif self.permission is not None:
            requester.user_permissions.add(self.permission)
        elif self.owner is not None:
            # Two deliberately separate steps (Garrigan's bundle-now-separable-later
            # call): (1) committee membership scopes which owner the member may act
            # for; (2) the event-lead role group confers the actual page-level
            # capability. Authorizer membership alone is inert without (2). To stop
            # bundling, drop step (2) or swap in a narrower role group.
            self.owner.authorizers.add(requester)        # (1) committee membership
            self._grantEventLeadRole(requester)          # (2) event-lead capability

    def _grantEventLeadRole(self, requester) -> None:
        """Add the requester to the managed event-lead role group, creating it
        with its permission bundle on first use. Kept distinct from the authorizer
        grant in grantTo so the two can be separated later (see EVENT_LEAD_ROLE_GROUP)."""
        roleGroup, created = Group.objects.get_or_create(name=EVENT_LEAD_ROLE_GROUP)
        if created:
            roleGroup.permissions.add(*Permission.objects.filter(
                codename__in=[name.split(".")[1] for name in EVENT_LEAD_PERMISSIONS],
                content_type__app_label="tools",
            ))
        requester.groups.add(roleGroup)


def revokeCommitteeMembership(user, owner) -> bool:
    """Remove a member from an owner's authorizers, and drop the derived
    Event Leads role group when they no longer authorize any owner.

    Returns whether the role group was removed, so callers can report it.

    This is grantTo's owner branch, run backwards - the counterpart the app
    never had (see EVENT_LEAD_ROLE_GROUP's comment for the 2026-08-11 decision
    that made the group's membership fully derived from eventAuthorizations).
    Every removal path in the app - the self-service revoke this exists for,
    and the admin owner-edit page (ownerViews.py) - must call this rather than
    calling owner.authorizers.remove() directly, or the two surfaces drift.
    """
    owner.authorizers.remove(user)
    groupRemoved = False
    if not user.eventAuthorizations.exists():
        # filter().first(), never get() - a fresh install (or a DB where nobody
        # has ever joined a committee) has no "Event Leads" row yet, and that
        # is a normal state to reconcile through, not an error.
        roleGroup = Group.objects.filter(name=EVENT_LEAD_ROLE_GROUP).first()
        if roleGroup is not None:
            user.groups.remove(roleGroup)
            groupRemoved = True
    logger.info(
        "revokeCommitteeMembership: removed %s as an authorizer of '%s' (roleGroupRemoved=%s)",
        user.getUserNameString(), owner.name, groupRemoved,
    )
    return groupRemoved


# ---------------------------------------------------------------------------
# Link Tree
#
# A self-hosted replacement for the chapter's third-party "linktree" pages. A
# LinkTree is a public (or members-only) page of LinkTreeItems. Every outbound
# click and every QR scan is routed through the site (see linkTreeViews.go /
# qr_redirect) so usage is tracked and a printed QR code stays repointable.
#
# Items can be plain MANUAL links or WIKI links that surface Outline content
# (e.g. "the latest GBM agenda"). WIKI items are resolved out-of-band by the
# `sync_link_tree_wiki` management command, which writes the resolved url/label
# onto the item; the public page only ever reads that cache, so it never depends
# on Outline being reachable at request time.
# ---------------------------------------------------------------------------


class LinkTree(models.Model):
    class Visibility:
        PUBLIC = 0   # anyone, no login
        MEMBERS = 1  # login required

    VISIBILITY_CHOICES = (
        (Visibility.PUBLIC, "Public - anyone with the link"),
        (Visibility.MEMBERS, "Members only - requires login"),
    )

    slug = models.SlugField(
        max_length=80, unique=True,
        help_text="Used in the public URL, e.g. 'links' → /t/links/. Lowercase, no spaces.",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(
        blank=True, help_text="Optional blurb shown under the title on the public page.",
    )
    visibility = models.IntegerField(choices=VISIBILITY_CHOICES, default=Visibility.PUBLIC)
    isActive = models.BooleanField(
        default=True, help_text="Uncheck to take the whole tree offline (returns 404).",
    )
    # Optional per-committee scoping, mirroring the EventOwners.authorizers
    # pattern used for events. Not enforced in v1 (the manageLinkTree permission
    # gates editing globally); reserved for future per-owner edit scoping.
    owner = models.ForeignKey(
        EventOwners, on_delete=models.SET_NULL, blank=True, null=True, related_name="linkTrees",
    )

    dateCreated = models.DateTimeField(auto_now_add=True)
    dateModified = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Link Tree"

    def __str__(self) -> str:
        return f"{self.title} (/t/{self.slug}/)"

    def getPublicUrl(self) -> str:
        return reverse("link-tree", kwargs={"slug": self.slug})

    def isMembersOnly(self) -> bool:
        return self.visibility == LinkTree.Visibility.MEMBERS

    def activeItems(self):
        """Active items in display order, honoring any visibility window.

        Filters the (prefetched) related set in Python so a single prefetch on
        the public view covers ordering and the time-window logic together.
        """
        now = datetime.datetime.now(datetime.UTC)
        visible = []
        for item in self.items.all():
            if not item.isActive:
                continue
            if item.visibleFrom is not None and now < item.visibleFrom:
                continue
            if item.visibleUntil is not None and now > item.visibleUntil:
                continue
            visible.append(item)
        return visible


class LinkTreeItem(models.Model):
    class Kind:
        MANUAL = 0          # a fixed url the maintainer types in
        WIKI = 1            # resolved from the Outline wiki by sync_link_tree_wiki
        SECTION_HEADER = 2  # a non-clickable heading that groups the items below it

    KIND_CHOICES = (
        (Kind.MANUAL, "Manual link"),
        (Kind.WIKI, "Wiki link (auto-surfaced from Outline)"),
        (Kind.SECTION_HEADER, "Section header (not a link)"),
    )

    class WikiMode:
        LATEST_MATCH = 0  # newest published doc whose title matches wikiQuery
        PINNED = 1        # one specific document, by id

    WIKI_MODE_CHOICES = (
        (WikiMode.LATEST_MATCH, "Latest matching document"),
        (WikiMode.PINNED, "Pinned document"),
    )

    tree = models.ForeignKey(LinkTree, on_delete=models.CASCADE, related_name="items")
    order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers appear first.",
    )
    kind = models.IntegerField(
        choices=KIND_CHOICES, default=Kind.MANUAL,
        help_text="Manual link (you type the URL), wiki link (auto-pulled from Outline), "
        "or a section header (a non-clickable heading that groups the items below it).",
    )

    label = models.CharField(
        max_length=200, blank=True,
        help_text="Button text - or the heading text for a section header. For wiki "
        "links, leave blank to use the document's own title.",
    )
    subtitle = models.CharField(
        max_length=300, blank=True,
        help_text="Optional smaller line shown under the label.",
    )
    icon = models.CharField(
        max_length=8, blank=True,
        help_text="Optional emoji shown before the label, e.g. 📅 or 🗳️.",
    )
    isActive = models.BooleanField(
        default=True,
        help_text="Uncheck to hide this item from the page without deleting it.",
    )

    # Optional show/hide window (great for event-specific links). UTC in DB.
    visibleFrom = models.DateTimeField(
        null=True, blank=True,
        help_text="Optional: don't show the item before this time (UTC).",
    )
    visibleUntil = models.DateTimeField(
        null=True, blank=True,
        help_text="Optional: stop showing the item after this time (UTC).",
    )

    # --- MANUAL ---
    url = models.URLField(
        max_length=2000, blank=True,
        help_text="Destination URL. Used for manual links (ignored for wiki links and headers).",
    )

    # --- WIKI ---
    wikiMode = models.IntegerField(
        choices=WIKI_MODE_CHOICES, default=WikiMode.LATEST_MATCH,
        help_text="For wiki links: surface the newest document matching the query, "
        "or always link one specific pinned document.",
    )
    wikiQuery = models.CharField(
        max_length=200, blank=True,
        help_text="For 'latest matching': title text to search, e.g. 'GBM Agenda'.",
    )
    wikiCollectionId = models.CharField(
        max_length=100, blank=True,
        help_text="Optional Outline collection id to scope the search.",
    )
    pinnedWikiDocId = models.CharField(
        max_length=100, blank=True, help_text="For 'pinned': the Outline document id.",
    )

    # Cache written by sync_link_tree_wiki; read by the public page.
    resolvedUrl = models.TextField(
        blank=True,
        help_text="Auto-filled for wiki links by the sync command - the resolved document URL.",
    )
    resolvedLabel = models.CharField(
        max_length=300, blank=True,
        help_text="Auto-filled for wiki links by the sync command - the resolved document title.",
    )
    resolvedAt = models.DateTimeField(
        null=True, blank=True,
        help_text="When the wiki link was last resolved by the sync command.",
    )

    class Meta:
        verbose_name = "Link Tree Item"
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.displayLabel() or f"Item {self.pk}"

    def isWiki(self) -> bool:
        return self.kind == LinkTreeItem.Kind.WIKI

    def isHeader(self) -> bool:
        return self.kind == LinkTreeItem.Kind.SECTION_HEADER

    def shouldDisplay(self) -> bool:
        """A header always shows; a link shows only once it has a destination."""
        return self.isHeader() or self.isResolved()

    def displayLabel(self) -> str:
        """What to show on the button - explicit label wins, else resolved title."""
        return self.label or (self.resolvedLabel if self.isWiki() else "")

    def destinationUrl(self) -> str | None:
        """The real URL to redirect to. None if a wiki item hasn't resolved yet."""
        if self.isWiki():
            return self.resolvedUrl or None
        return self.url or None

    def isResolved(self) -> bool:
        return self.destinationUrl() is not None

    def trackedUrl(self) -> str | None:
        """Site URL that logs a click then redirects (what the page links to).

        None for a section header - a header is not a link, so it has no tracked
        destination. Callers (and the template) gate on isHeader() before using
        this, and this makes that contract honest rather than relying on the
        template alone.
        """
        if self.isHeader():
            return None
        return reverse("link-go", kwargs={"item_id": self.pk})


class QRCode(models.Model):
    """A repointable, tracked QR code.

    The generated image encodes the site's /qr/<code>/ URL - NOT the destination.
    Scans hit qr_redirect, which logs the scan and 302s to the current target, so
    a printed code can be repointed in admin without reprinting and every scan is
    still counted. Exactly one of tree / item / rawUrl is the target.
    """

    code = models.SlugField(
        max_length=40, unique=True,
        help_text="Short token in the QR URL, e.g. 'spring-tabling' → /qr/spring-tabling/.",
    )
    label = models.CharField(
        max_length=200, help_text="Human label, e.g. 'Spring 2026 tabling flyer'.",
    )
    campaign = models.CharField(
        max_length=100, blank=True,
        help_text="Optional medium/source tag to break down scans, e.g. 'flyer' or 'table-tent'.",
    )

    tree = models.ForeignKey(
        LinkTree, on_delete=models.SET_NULL, blank=True, null=True, related_name="qrCodes",
    )
    item = models.ForeignKey(
        LinkTreeItem, on_delete=models.SET_NULL, blank=True, null=True, related_name="qrCodes",
    )
    rawUrl = models.URLField(
        max_length=2000, blank=True, help_text="Target an arbitrary URL instead of a tree/item.",
    )

    isActive = models.BooleanField(default=True)
    createdBy = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="qrCodesCreated",
    )
    dateCreated = models.DateTimeField(auto_now_add=True)
    dateModified = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "QR Code"

    def __str__(self) -> str:
        return f"{self.label} (/qr/{self.code}/)"

    def clean(self):
        # Enforce exactly one target so a scan is never ambiguous. Runs in admin
        # (via ModelForm validation) and on any full_clean() call.
        from django.core.exceptions import ValidationError

        targets = [self.tree_id is not None, self.item_id is not None, bool(self.rawUrl)]
        chosen = sum(1 for t in targets if t)
        if chosen != 1:
            raise ValidationError(
                "A QR code must point at exactly one target: a link tree, a link tree item, or a raw URL."
            )

    def scanUrl(self) -> str:
        """Site URL the QR image encodes; logs a scan then redirects."""
        return reverse("qr-redirect", kwargs={"code": self.code})

    def resolveTarget(self):
        """The single source of truth for a QR code's target taxonomy.

        Returns ``(destinationUrl, tree, item)`` where destinationUrl is where a
        scan should 302 (or None if not yet resolvable), and tree/item are the
        objects to attribute the scan to in analytics. Centralizing this here
        means a new target type is added in exactly one place - the view and any
        other caller just consume the tuple.
        """
        if self.tree is not None:
            return self.tree.getPublicUrl(), self.tree, None
        if self.item is not None:
            return self.item.destinationUrl(), self.item.tree, self.item
        return (self.rawUrl or None), None, None

    def targetUrl(self) -> str | None:
        """Just the redirect destination (tree page > item dest > raw url)."""
        return self.resolveTarget()[0]


class LinkEvent(models.Model):
    """Append-only, privacy-first click/scan log.

    Deliberately stores NO raw IP: visitorHash is a salted, daily-rotating digest
    of IP+user-agent, good only for rough same-day unique counts and useless as a
    cross-day identifier. destinationUrl is snapshotted so analytics survive later
    edits to the item/QR target.
    """

    class Source:
        WEB = 0  # a click on the public tree page
        QR = 1   # a QR scan

    SOURCE_CHOICES = (
        (Source.WEB, "Web click"),
        (Source.QR, "QR scan"),
    )

    tree = models.ForeignKey(
        LinkTree, on_delete=models.SET_NULL, blank=True, null=True, related_name="events",
    )
    item = models.ForeignKey(
        LinkTreeItem, on_delete=models.SET_NULL, blank=True, null=True, related_name="events",
    )
    qr = models.ForeignKey(
        QRCode, on_delete=models.SET_NULL, blank=True, null=True, related_name="events",
    )

    source = models.IntegerField(choices=SOURCE_CHOICES)
    occurredAt = models.DateTimeField(auto_now_add=True, db_index=True)

    destinationUrl = models.TextField(blank=True)
    visitorHash = models.CharField(max_length=16, blank=True)
    uaFamily = models.CharField(max_length=40, blank=True)
    referrerHost = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Link Event"
        indexes = [
            models.Index(fields=["tree", "occurredAt"]),
            models.Index(fields=["item", "occurredAt"]),
            models.Index(fields=["qr", "occurredAt"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_source_display()} @ {self.occurredAt:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Resolutions
#
# A member-submitted resolution gathering signature "sign-ons" to make a meeting
# agenda. Echo owns the canonical text and enforces the integrity guarantee: the
# text locks when the first member signs on, and any later edit resets the
# sign-ons (see replaceText). Each signer is validated live as a Member in Good
# Standing against Action Network at sign-on time (see ActionNetworkAPI/).
#
# The bylaws set the rules by kind: a general resolution needs no sign-ons (the
# Leadership Committee sets the agenda); a project committee needs 25 sign-ons
# filed 10 days out (Section 7.1.5); a bylaws amendment needs proponent + 35
# filed 21 days out (Section 10.1). Kind is the single source of that truth.
# ---------------------------------------------------------------------------


class Resolution(models.Model):
    class Kind:
        GENERAL = "GENERAL"
        PROJECT_COMMITTEE = "PROJECT_COMMITTEE"
        BYLAWS_AMENDMENT = "BYLAWS_AMENDMENT"
        CANDIDATE_ENDORSEMENT = "CANDIDATE_ENDORSEMENT"

        CHOICES = (
            (GENERAL, "General resolution"),
            (PROJECT_COMMITTEE, "Project committee or campaign"),
            (BYLAWS_AMENDMENT, "Bylaws amendment"),
            (CANDIDATE_ENDORSEMENT, "Candidate endorsement"),
        )

        # The single source of threshold / lead-day truth. None threshold means
        # no sign-on requirement (the Leadership Committee or a direct vote sets
        # the agenda). Lead days are how far ahead of the meeting it must be filed.
        THRESHOLDS = {GENERAL: None, PROJECT_COMMITTEE: 25, BYLAWS_AMENDMENT: 35, CANDIDATE_ENDORSEMENT: None}
        LEAD_DAYS = {GENERAL: 0, PROJECT_COMMITTEE: 10, BYLAWS_AMENDMENT: 21, CANDIDATE_ENDORSEMENT: 0}

        # The vote it takes to adopt at the meeting. Bylaws amendments and
        # candidate endorsements need two-thirds; everything else a simple
        # majority of those voting (abstentions never count). Section 9.2 / 10.1.
        VOTE_MAJORITY = "MAJORITY"
        VOTE_TWO_THIRDS = "TWO_THIRDS"
        VOTE_THRESHOLDS = {
            GENERAL: VOTE_MAJORITY,
            PROJECT_COMMITTEE: VOTE_MAJORITY,
            BYLAWS_AMENDMENT: VOTE_TWO_THIRDS,
            CANDIDATE_ENDORSEMENT: VOTE_TWO_THIRDS,
        }

        # Self-documenting metadata for the submit-form type cards.
        COVERAGE = {
            GENERAL: "An endorsement, a political position, or a statement of the chapter.",
            PROJECT_COMMITTEE: "Create or dissolve a campaign, working group, or other project committee.",
            BYLAWS_AMENDMENT: "Change the text of the bylaws themselves.",
            CANDIDATE_ENDORSEMENT: "Endorse a candidate for elected office (a two-thirds vote).",
        }
        SECTION = {GENERAL: "", PROJECT_COMMITTEE: "Section 7.1", BYLAWS_AMENDMENT: "Section 10.1", CANDIDATE_ENDORSEMENT: "Section 9.2"}

    class Status:
        GATHERING = "GATHERING"
        SCHEDULED = "SCHEDULED"
        ADOPTED = "ADOPTED"
        REJECTED = "REJECTED"
        WITHDRAWN = "WITHDRAWN"
        SUPERSEDED = "SUPERSEDED"
        # Terminal: was still gathering when its filing deadline passed, so it
        # can never make that agenda. Distinct from a deliberate WITHDRAWN.
        LAPSED = "LAPSED"

        CHOICES = (
            (GATHERING, "Gathering sign-ons"),
            (SCHEDULED, "On the agenda"),
            (ADOPTED, "Adopted"),
            (REJECTED, "Did not pass"),
            (WITHDRAWN, "Withdrawn"),
            (SUPERSEDED, "Superseded"),
            (LAPSED, "Lapsed"),
        )

        # The two "in flight" states the Secretary actively manages; the rest
        # are terminal outcomes.
        IN_FLIGHT = (GATHERING, SCHEDULED)

    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=32, choices=Kind.CHOICES)
    # The canonical markdown copy. Echo is the source of truth while gathering.
    text = models.TextField()
    proponent = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="resolutionsProposed",
    )
    # The meeting it is filed for, read from the events domain. Nullable so a
    # draft can exist before a meeting is chosen; SET_NULL mirrors the event
    # owner FKs - any code reading targetMeeting must null-guard.
    targetMeeting = models.ForeignKey(
        PostedEvents, on_delete=models.SET_NULL, blank=True, null=True,
        related_name="resolutions",
    )
    status = models.CharField(max_length=16, choices=Status.CHOICES, default=Status.GATHERING)

    # Integrity lock: set on the first verified sign-on; cleared by replaceText
    # when the text changes.
    locked = models.BooleanField(default=False)
    lockedTextHash = models.CharField(max_length=64, blank=True)
    lockedAt = models.DateTimeField(null=True, blank=True)

    # --- lifecycle outcome (set by the Secretary transitions below) ------
    # A stable archive slug, set on adoption; also the <year>/<slug>.md repo
    # filename for the export handoff.
    slug = models.SlugField(max_length=120, blank=True)
    # When the membership voted, and the recorded Yes / No / Abstain tally.
    decidedAt = models.DateTimeField(null=True, blank=True)
    votesYes = models.PositiveIntegerField(null=True, blank=True)
    votesNo = models.PositiveIntegerField(null=True, blank=True)
    votesAbstain = models.PositiveIntegerField(null=True, blank=True)
    # The date an adopted resolution takes effect (immediately per the bylaws,
    # unless the resolution itself specifies otherwise).
    effectiveDate = models.DateField(null=True, blank=True)
    # Set when a later adopted resolution repeals or replaces this one.
    supersededBy = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supersedes",
    )
    # When the repo-format markdown was last exported (the Echo -> repo handoff).
    exportedAt = models.DateTimeField(null=True, blank=True)

    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-createdAt"]

    def __str__(self) -> str:
        return self.title

    # --- kind-derived rules (read from Kind, never duplicated) ----------

    @property
    def threshold(self) -> int | None:
        return Resolution.Kind.THRESHOLDS.get(self.kind)

    @property
    def leadDays(self) -> int:
        return Resolution.Kind.LEAD_DAYS.get(self.kind, 0)

    def getKindDisplay(self) -> str:
        return dict(Resolution.Kind.CHOICES).get(self.kind, self.kind)

    def getBylawsSection(self) -> str:
        return Resolution.Kind.SECTION.get(self.kind, "")

    def getStatusDisplay(self) -> str:
        return dict(Resolution.Status.CHOICES).get(self.status, self.status)

    def getStageDisplay(self) -> str:
        """The Stage label shown to members. Identical to getStatusDisplay except
        that a no-threshold resolution (general resolution, candidate endorsement)
        has nothing to gather while in GATHERING - it is simply awaiting the
        Leadership Committee's agenda decision - so it reads "Awaiting agenda"
        rather than the misleading "Gathering sign-ons". The status itself stays
        GATHERING so the Secretary scheduling workflow is unchanged."""
        if self.status == Resolution.Status.GATHERING and self.threshold is None:
            return "Awaiting agenda"
        return self.getStatusDisplay()

    @property
    def voteThreshold(self) -> str:
        return Resolution.Kind.VOTE_THRESHOLDS.get(self.kind, Resolution.Kind.VOTE_MAJORITY)

    def getVoteThresholdDisplay(self) -> str:
        if self.voteThreshold == Resolution.Kind.VOTE_TWO_THIRDS:
            return "two-thirds"
        return "simple majority"

    # --- lifecycle state helpers (read-only) -----------------------------

    @property
    def isInFlight(self) -> bool:
        return self.status in Resolution.Status.IN_FLIGHT

    @property
    def isInEffect(self) -> bool:
        """Adopted and not since superseded, i.e. currently governing."""
        return self.status == Resolution.Status.ADOPTED and self.supersededBy_id is None

    @property
    def isDecided(self) -> bool:
        return self.status in (Resolution.Status.ADOPTED, Resolution.Status.REJECTED)

    def voteTallyStr(self) -> str:
        """The recorded tally as 'Yes-No-Abstain', or '' when unrecorded."""
        if self.votesYes is None:
            return ""
        return f"{self.votesYes}-{self.votesNo}-{self.votesAbstain}"

    def votePasses(self, yes: int, no: int) -> bool:
        """Whether a Yes / No tally clears this resolution's vote threshold.
        Abstentions never count toward the threshold."""
        total = yes + no
        if total <= 0:
            return False
        if self.voteThreshold == Resolution.Kind.VOTE_TWO_THIRDS:
            return 3 * yes >= 2 * total  # yes >= two-thirds of (yes + no)
        return yes > no                  # simple majority of those voting

    # --- sign-on counting ------------------------------------------------

    @property
    def signatureCount(self) -> int:
        return self.signatures.filter(verified=True).count()

    @property
    def meetsThreshold(self) -> bool:
        return self.threshold is None or self.signatureCount >= self.threshold

    # --- deadline (null-guards a missing/orphaned meeting) ---------------

    def deadline(self) -> datetime.datetime | None:
        if self.targetMeeting is None:
            return None
        return self.targetMeeting.start - datetime.timedelta(days=self.leadDays)

    def deadlineMet(self) -> bool | None:
        """Whether it was filed in time (createdAt at or before the deadline).
        None when there is no meeting to measure against."""
        deadline = self.deadline()
        if deadline is None:
            return None
        createdAt = self.createdAt
        if createdAt is None:
            return None
        if createdAt.tzinfo is None:
            createdAt = pytz.utc.localize(createdAt)
        cutoff = deadline
        if cutoff.tzinfo is None:
            cutoff = pytz.utc.localize(cutoff)
        return createdAt <= cutoff

    def localTimeZone(self):
        """The timezone chapter-facing dates render in: the target meeting's own
        zone when there is one, else the chapter default. Datetimes are stored UTC
        (settings.TIME_ZONE = "UTC"), so a naive strftime would mislabel them by
        the UTC offset - mirrors PostedEvents.getStartLocalized."""
        if self.targetMeeting is not None and self.targetMeeting.timezone:
            return pytz.timezone(self.targetMeeting.timezone)
        return pytz.timezone(utils.CHAPTER_TIME_ZONE)

    def decidedDateLocal(self) -> datetime.date | None:
        """The decision date in chapter-local time. ``decidedAt`` is stored UTC,
        so a late-evening Central adoption would otherwise roll onto the next UTC
        day - wrong for the effective date and the permanent archive record."""
        if self.decidedAt is None:
            return None
        decided = self.decidedAt
        if decided.tzinfo is None:
            decided = pytz.utc.localize(decided)
        return decided.astimezone(self.localTimeZone()).date()

    def getDeadlineStr(self) -> str:
        deadline = self.deadline()
        if deadline is None:
            return "no meeting selected"
        if deadline.tzinfo is None:
            deadline = pytz.utc.localize(deadline)
        return deadline.astimezone(self.localTimeZone()).strftime(utils.DATE_TIME_FORMAT)

    def signOnDeadlinePassed(self) -> bool:
        """True only when a filing deadline exists and is now in the past, so a
        late sign-on can no longer help the resolution make the agenda."""
        deadline = self.deadline()
        if deadline is None:
            return False
        if deadline.tzinfo is None:
            deadline = pytz.utc.localize(deadline)
        return datetime.datetime.now(datetime.UTC) > deadline

    # --- integrity lock --------------------------------------------------

    def isOpenForSignOn(self) -> bool:
        return self.status == Resolution.Status.GATHERING

    def lockText(self) -> None:
        """Lock the text on the first sign-on. Idempotent."""
        if not self.locked:
            self.locked = True
            self.lockedTextHash = normalizedTextHash(self.text)
            self.lockedAt = datetime.datetime.now(datetime.UTC)
            self.save()

    def replaceText(self, newText: str) -> bool:
        """Replace the resolution text. Returns True if this reset sign-ons.

        A cosmetically-identical resave (same normalized hash) is a no-op and
        preserves sign-ons. A real change to a locked resolution deletes every
        sign-on and re-opens gathering - the integrity guarantee. The reset
        logic lives only here so the edit view and any future admin path share
        it."""
        if self.locked and normalizedTextHash(newText) == self.lockedTextHash:
            if newText != self.text:
                self.text = newText
                self.save()
            return False

        wasLocked = self.locked
        self.text = newText
        if wasLocked:
            self.signatures.all().delete()
            self.locked = False
            self.lockedTextHash = ""
            self.lockedAt = None
        self.save()
        return wasLocked

    # --- lifecycle transitions (Secretary-driven) ------------------------
    # Each transition validates it is legal from the current status (defense in
    # depth; the templates also hide illegal actions) and records a
    # ResolutionEvent for the audit trail. ``actor`` is the acting user, or None
    # for a system / seed action.

    def _recordEvent(self, actor, fromStatus: str, note: str = "") -> None:
        ResolutionEvent.objects.create(
            resolution=self, actor=actor,
            fromStatus=fromStatus, toStatus=self.status, note=note,
        )

    def _generateSlug(self) -> str:
        from django.utils.text import slugify
        return slugify(self.title)[:110] or f"resolution-{self.pk}"

    def schedule(self, meeting=None, actor=None, note: str = "") -> None:
        """Place a gathering resolution on a meeting agenda."""
        if self.status != Resolution.Status.GATHERING:
            raise ValueError(f"Cannot schedule a {self.status} resolution")
        fromStatus = self.status
        if meeting is not None:
            self.targetMeeting = meeting
        self.status = Resolution.Status.SCHEDULED
        self.save()
        self._recordEvent(actor, fromStatus, note)

    def sendBackToGathering(self, actor=None, note: str = "") -> None:
        """Undo a schedule, returning the resolution to gathering sign-ons."""
        if self.status != Resolution.Status.SCHEDULED:
            raise ValueError(f"Cannot send a {self.status} resolution back to gathering")
        fromStatus = self.status
        self.status = Resolution.Status.GATHERING
        self.save()
        self._recordEvent(actor, fromStatus, note)

    def recordVote(self, yes: int, no: int, abstain: int, actor=None, note: str = "") -> None:
        """Record the membership vote and decide the outcome. Adopts when the
        tally clears the kind's vote threshold, otherwise marks it rejected.
        Adoption freezes the text and stamps the effective date and archive slug."""
        if self.status not in Resolution.Status.IN_FLIGHT:
            raise ValueError(f"Cannot record a vote on a {self.status} resolution")
        fromStatus = self.status
        self.votesYes, self.votesNo, self.votesAbstain = yes, no, abstain
        self.decidedAt = datetime.datetime.now(datetime.UTC)
        if self.votePasses(yes, no):
            self.status = Resolution.Status.ADOPTED
            self.effectiveDate = self.decidedDateLocal()
            if not self.slug:
                self.slug = self._generateSlug()
            self.save()
            self.lockText()  # freeze the adopted text (idempotent)
        else:
            self.status = Resolution.Status.REJECTED
            self.save()
        self._recordEvent(actor, fromStatus, note)

    def withdraw(self, actor=None, note: str = "") -> None:
        """Pull an in-flight resolution before it is voted on."""
        if self.status not in Resolution.Status.IN_FLIGHT:
            raise ValueError(f"Cannot withdraw a {self.status} resolution")
        fromStatus = self.status
        self.status = Resolution.Status.WITHDRAWN
        self.save()
        self._recordEvent(actor, fromStatus, note)

    def markLapsed(self, actor=None, note: str = "") -> None:
        """Close out a gathering resolution whose filing deadline passed before it
        reached a meeting agenda. Terminal, and only legal from GATHERING - a
        scheduled resolution is already on an agenda, and the other statuses are
        themselves terminal. The daily sweep (lapse_expired_resolutions) is the
        usual caller; the Secretary can also trigger it by hand once the deadline
        has passed. To keep it open instead, re-target it to a later meeting
        (which resets the deadline) before it lapses."""
        if self.status != Resolution.Status.GATHERING:
            raise ValueError(f"Cannot lapse a {self.status} resolution")
        fromStatus = self.status
        self.status = Resolution.Status.LAPSED
        self.save()
        self._recordEvent(actor, fromStatus, note)

    def supersede(self, actor=None, replacement=None, note: str = "") -> None:
        """Mark an adopted resolution as repealed or replaced by a later one."""
        if self.status != Resolution.Status.ADOPTED:
            raise ValueError(f"Cannot supersede a {self.status} resolution")
        fromStatus = self.status
        self.status = Resolution.Status.SUPERSEDED
        if replacement is not None:
            self.supersededBy = replacement
        self.save()
        self._recordEvent(actor, fromStatus, note)

    def getUrl(self) -> str:
        return reverse("resolution-detail", kwargs={"pk": self.id})


class ResolutionSignature(models.Model):
    # Verification outcome codes live in ActionNetworkAPI.migValidator (the
    # validator and this model share them, so they never drift).
    VerificationStatus = MIGStatus

    resolution = models.ForeignKey(
        Resolution, on_delete=models.CASCADE, related_name="signatures",
    )
    member = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="resolutionSignatures",
    )
    signedAt = models.DateTimeField(auto_now_add=True)
    # The normalized hash of the text the member actually signed (defense in
    # depth: a counted signature can be proven to match the locked text).
    textHashAtSigning = models.CharField(max_length=64)
    verified = models.BooleanField(default=False)
    verificationStatus = models.CharField(max_length=20, choices=MIGStatus.CHOICES)
    checkedAt = models.DateTimeField()

    class Meta:
        unique_together = (("resolution", "member"),)

    def __str__(self) -> str:
        return f"{self.member.getDisplayName()} -> {self.resolution.title}"

    def getStatusDisplay(self) -> str:
        return dict(MIGStatus.CHOICES).get(self.verificationStatus, self.verificationStatus)


class ResolutionEvent(models.Model):
    """An audit-trail entry: one row per lifecycle transition, so the Secretary
    (and the bylaws record) can see who moved a resolution and when."""

    resolution = models.ForeignKey(
        Resolution, on_delete=models.CASCADE, related_name="events",
    )
    # The acting user, or null for a system / seed action.
    actor = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name="resolutionEvents",
    )
    fromStatus = models.CharField(max_length=16, blank=True)
    toStatus = models.CharField(max_length=16)
    note = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.resolution_id}: {self.fromStatus} -> {self.toStatus}"

    def getFromDisplay(self) -> str:
        return dict(Resolution.Status.CHOICES).get(self.fromStatus, self.fromStatus or "created")

    def getToDisplay(self) -> str:
        return dict(Resolution.Status.CHOICES).get(self.toStatus, self.toStatus)
# Chapter Tools (IT access registry) - M1
#
# The chapter-wide inventory of what systems Austin DSA runs, how access to
# each one works, and who currently holds it. Two visibility layers share one
# table: the open layer (name/blurb/category/access model/holders/payer/
# how-to-get-access) is visible to any logged-in member; the restricted layer
# (delegation tier, revocation/continuity notes, credential objects, review/
# staleness detail, open questions) requires the viewChapterToolAudit
# permission and is read-logged via ToolAuditReadLog on every render - see
# chapterToolsViews.py.
#
# M1 is directory + detail + the questions workbench only; all other CRUD is
# admin-only (no in-app edit views). Request-access integration
# (AccessRequests gaining a `resource` target) is M2 and explicitly out of
# scope here - ResourceGrant exists now purely as a hook for it so a later
# migration isn't needed just to add the ledger table.
# ---------------------------------------------------------------------------


class ChapterResource(models.Model):
    class Category:
        COMMUNICATION = 0
        INFRASTRUCTURE = 1
        FINANCE = 2
        SOCIAL = 3
        ORGANIZING = 4

    CATEGORY_CHOICES = (
        (Category.COMMUNICATION, "Communication"),
        (Category.INFRASTRUCTURE, "Infrastructure"),
        (Category.FINANCE, "Finance"),
        (Category.SOCIAL, "Social"),
        (Category.ORGANIZING, "Organizing"),
    )

    class AccessModel:
        INDIVIDUAL = 0
        SHARED_VAULT = 1
        SERVICE_ACCOUNT = 2
        MIXED = 3
        UNCONFIRMED = 4

    # Labels are written for a member who just wants to get into something, not
    # for the committee - "Individual logins" told a first-time reader nothing.
    ACCESS_MODEL_CHOICES = (
        (AccessModel.INDIVIDUAL, "Your own login"),
        (AccessModel.SHARED_VAULT, "Shared login, handed out from the vault"),
        (AccessModel.SERVICE_ACCOUNT, "Automated service account"),
        (AccessModel.MIXED, "Mixed - some of both"),
        (AccessModel.UNCONFIRMED, "Not confirmed yet"),
    )

    # The label alone still can't carry what the model *costs* the reader (do I
    # need a vault account first? does removing me affect anyone else?), so every
    # surface that shows a label shows this alongside it. One source of truth:
    # the directory builds its legend from this dict, the detail page reads it
    # through getAccessModelExplanation().
    ACCESS_MODEL_EXPLANATIONS = {
        AccessModel.INDIVIDUAL: (
            "You get your own login in your own name. Nobody shares a password, "
            "and removing one person's access does not affect anybody else."
        ),
        AccessModel.SHARED_VAULT: (
            "Everybody signs in with the same login. The password is handed out "
            "through the chapter password vault, so you need a vault account "
            "before you can get in. Because the password is shared, taking access "
            "away later means changing it for everyone on it."
        ),
        AccessModel.SERVICE_ACCOUNT: (
            "No person signs in here. An automated account does the work on the "
            "chapter's behalf, so getting access means being able to change how "
            "that automation is set up."
        ),
        AccessModel.MIXED: (
            "Some parts of this use your own login and some parts use a shared "
            "one. Read the How to get access note for which applies to you."
        ),
        AccessModel.UNCONFIRMED: (
            "Nobody has checked yet how access to this actually works. Treat it "
            "as an open question rather than a settled answer, and expect to ask "
            "a person."
        ),
    }

    class Payer:
        CHAPTER = 0
        NATIONAL = 1
        MIXED = 2
        FREE = 3
        UNCONFIRMED = 4

    PAYER_CHOICES = (
        (Payer.CHAPTER, "Chapter"),
        (Payer.NATIONAL, "National"),
        (Payer.MIXED, "Mixed"),
        (Payer.FREE, "Free"),
        (Payer.UNCONFIRMED, "Unconfirmed"),
    )

    class DelegationTier:
        GREEN = 0
        YELLOW = 1
        RED = 2
        UNCLASSIFIED = 3

    DELEGATION_TIER_CHOICES = (
        (DelegationTier.GREEN, "Green"),
        (DelegationTier.YELLOW, "Yellow"),
        (DelegationTier.RED, "Red"),
        (DelegationTier.UNCLASSIFIED, "Unclassified"),
    )

    # A colour name is not a definition. "Red" told a reader nothing about what
    # they may or may not do, so the ladder is spelled out here in the same
    # shape as ACCESS_MODEL_EXPLANATIONS above: one dict, read by every surface
    # that shows a tier, so the wording cannot drift between the detail page,
    # the edit form and the legend.
    #
    # The ladder answers exactly one question - how freely may this be handed
    # to somebody else - and the tiers are written as permissions to act, not as
    # severity adjectives.
    DELEGATION_TIER_EXPLANATIONS = {
        DelegationTier.GREEN: (
            "Hand this out freely. Anybody who needs it for chapter work can be "
            "given it without asking the committee first. If it is misused or "
            "lost, undoing that is quick and affects only the one person."
        ),
        DelegationTier.YELLOW: (
            "Hand this out deliberately, to a named person, for a stated reason. "
            "Nothing here is irreversible, but taking it back costs real work - "
            "changing a shared password, or undoing what somebody posted - and "
            "that work lands on other people."
        ),
        DelegationTier.RED: (
            "Do not hand this out on your own. Holding it means you can lock "
            "other people out, spend chapter money, or read member data. Some of "
            "what can go wrong here cannot be undone, so the committee decides "
            "together and the decision gets written down."
        ),
        DelegationTier.UNCLASSIFIED: (
            "Nobody has decided yet how freely this may be handed out. Treat it "
            "as Red until somebody does. An unclassified tool is an open job, "
            "not a tool that is safe by default."
        ),
    }

    # Which tiers may carry a self-serve request button. Green and Yellow only:
    # the Red wording above is "do not hand this out on your own", and a button
    # a member can press by themselves is precisely that. Unclassified is
    # excluded because it is defined as "treat as Red" - which also makes this a
    # forcing function, since opening a tool to requests now requires somebody
    # to classify it first.
    REQUESTABLE_TIERS = (DelegationTier.GREEN, DelegationTier.YELLOW)

    # A row with no lastReviewed, or one older than this, shows a stale flag -
    # restricted-layer detail (see Visibility in the plan), not open-layer.
    STALE_AFTER_DAYS = 90

    name = models.CharField(max_length=200, unique=True)
    blurb = models.TextField(
        blank=True,
        help_text="One or two sentences: what this is and what the chapter uses it for. "
                   "Shown to every logged-in member.",
    )
    category = models.IntegerField(choices=CATEGORY_CHOICES, default=Category.ORGANIZING)

    accessModel = models.IntegerField(
        choices=ACCESS_MODEL_CHOICES, default=AccessModel.UNCONFIRMED,
        help_text="A summary only - the real access story lives on this resource's credential rows.",
    )
    payer = models.IntegerField(choices=PAYER_CHOICES, default=Payer.UNCONFIRMED)
    annualCost = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    costNote = models.CharField(
        max_length=300, blank=True,
        help_text="Seat caps, per-seat pricing, or other budget notes. "
                   "Shown to every logged-in member. "
                   "Example: 'Five seats on the paid plan; a sixth is $8/month more.'",
    )

    howToGetAccess = models.TextField(
        blank=True,
        help_text="Open-layer instructions, e.g. 'Request below' or 'Ask in #it-committee'. "
                   "Shown to every logged-in member.",
    )

    # Two URLs, not one, because a member asks two different questions and a
    # single "link" field can only answer one of them. Prose in howToGetAccess
    # cannot substitute: it renders as plain text, so a URL written in there is
    # not clickable and the reader has to retype it.
    siteUrl = models.URLField(
        blank=True,
        help_text="Where this thing actually lives, for somebody who already has access.",
    )
    accessRequestUrl = models.URLField(
        blank=True,
        help_text=(
            "The link that starts an access request TODAY - an external form, a signup page. "
            "This is the M1 stopgap and is unrelated to the 'requestable' flag below, which is "
            "the M2 in-app front door. Most resources will have this and not that for a while."
        ),
    )

    steward = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="stewardedResources",
        help_text=(
            "The one person answerable for this tool: they keep this row true, they "
            "are who to ask when access here is unclear, and they review requests "
            "for it. A steward is not the only person with access and does not have "
            "to be on the committee - the point is that exactly one name is on the "
            "hook, so the question never falls to 'somebody'."
        ),
    )
    stewardName = models.CharField(
        max_length=200, blank=True,
        help_text="Text fallback when the steward has no Echo account yet - most stewards won't at launch.",
    )

    requestable = models.BooleanField(
        default=False,
        help_text=(
            "Whether members can ask for this through Echo itself (M2 front door). "
            "Tick it only when all three are true: there is a steward to review the "
            "request, the delegation tier is Green or Yellow, and holding it gives "
            "somebody no power over other members. Leave it off when access is a "
            "shared password that is expensive to change, when the holder could "
            "spend money or read member data, or when nobody is ever given this "
            "directly - a calendar that Echo writes to is reached through Echo, so "
            "the thing to request is Echo."
        ),
    )

    lastReviewed = models.DateField(
        null=True, blank=True,
        help_text=(
            "The day somebody last checked this row against reality - that the "
            "holder list is still right, that the people on it can still get in, "
            "and that the cost and steward are current. It records a check, not an "
            f"edit. Rows go stale after {STALE_AFTER_DAYS} days."
        ),
    )
    reviewedBy = models.CharField(
        max_length=200, blank=True,
        help_text="Who did that check, so the claim has a name attached to it.",
    )

    # --- restricted layer (viewChapterToolAudit) ---
    delegationTier = models.IntegerField(choices=DELEGATION_TIER_CHOICES, default=DelegationTier.UNCLASSIFIED)
    revocationNote = models.TextField(
        blank=True,
        help_text=(
            "What taking access away actually costs here, in the order somebody "
            "would have to do it. Write the cost, not the intention. "
            "Example: 'One shared password, so removing one person means changing "
            "it and telling the other four. Budget 30 minutes.'"
        ),
    )
    continuityNote = models.TextField(
        blank=True,
        help_text=(
            "How somebody gets in if the steward is unreachable - the break-glass "
            "path. Name a second person or a second route, not a hope. "
            "Example: 'Recovery codes are in the vault collection; two other "
            "committee members can read it.'"
        ),
    )

    class Meta:
        verbose_name = "Chapter Resource"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.requestable and self.steward_id is None:
            raise ValidationError(
                "A requestable resource must have a steward - a request button with no "
                "resolvable reviewer would be a dead letter."
            )
        # The tier ladder's own wording for Red is "do not hand this out on your
        # own", and a self-serve request button is exactly that. Unclassified is
        # blocked for the same reason, because it is defined as "treat as Red" -
        # so opening a tool to requests forces somebody to classify it first.
        if self.requestable and self.delegationTier not in self.REQUESTABLE_TIERS:
            raise ValidationError(
                f"A {self.get_delegationTier_display()}-tier resource cannot be opened "
                "for member requests. Classify it Green or Yellow first if that is "
                "genuinely what it is, or leave requests closed and keep handing this "
                "out deliberately."
            )

    def getStewardName(self) -> str:
        """Name AND email ("First Last - email"). Currently has no caller: the
        one template that used it now renders getStewardDisplayName instead,
        because the steward line moved a permission tier wider.

        Kept, not deleted, because the audit tier may legitimately want the
        contact address - but do not reach for this just because it is the
        shorter name. Anything rendered outside a viewChapterToolAudit block
        wants getStewardDisplayName below."""
        if self.steward is not None:
            return self.steward.getUserNameString()
        return self.stewardName

    def getStewardDisplayName(self) -> str:
        """Name only, no email - for the holder tier (viewResourceHolders).
        getStewardName's getUserNameString() form ("First Last - email") is
        fine for the audit ring, which already sees every credential on this
        resource, but rendering it a permission tier lower would hand the
        wider organizer ring a contact address nobody consented to publish
        (REVIEW.md F3)."""
        if self.steward is not None:
            return self.steward.getDisplayName()
        return self.stewardName

    def getAccessModelExplanation(self) -> str:
        return self.ACCESS_MODEL_EXPLANATIONS.get(self.accessModel, "")

    @classmethod
    def getAccessModelLegend(cls) -> list:
        """Every access model, in order, for the help popover - the same shape
        getDelegationTierLegend returns and read by the same ladder template.

        Always complete, deliberately. The directory used to build a legend of
        only the models actually on screen, which was defensible while the whole
        register fitted on one page and became a bug the moment it was
        paginated: the same term would be defined on page one and undefined on
        page two, purely because of where the row happened to fall. A reader
        being told what a phrase means wants the set it belongs to, not the
        subset that shares their pagination."""
        return [
            {
                "value": value,
                "label": label,
                "explanation": cls.ACCESS_MODEL_EXPLANATIONS.get(value, ""),
            }
            for value, label in cls.ACCESS_MODEL_CHOICES
        ]

    def getDelegationTierExplanation(self) -> str:
        return self.DELEGATION_TIER_EXPLANATIONS.get(self.delegationTier, "")

    @classmethod
    def getDelegationTierLegend(cls) -> list:
        """The whole ladder, in order, for the help popover. Unlike the access-
        model legend on the directory (which lists only the models actually on
        screen) this one is always complete: a reader is being told what the
        tiers *mean*, and a ladder with a rung missing does not explain the rung
        above it."""
        return [
            {
                "value": value,
                "label": label,
                "explanation": cls.DELEGATION_TIER_EXPLANATIONS.get(value, ""),
                "requestable": value in cls.REQUESTABLE_TIERS,
            }
            for value, label in cls.DELEGATION_TIER_CHOICES
        ]

    def isStale(self) -> bool:
        if self.lastReviewed is None:
            return True
        return (djangoTimezone.now().date() - self.lastReviewed).days > self.STALE_AFTER_DAYS

    def getUrl(self) -> str:
        return reverse("chapter-tool-detail", kwargs={"pk": self.id})

    def getSiteHost(self) -> str:
        """The bare hostname of siteUrl, e.g. 'wiki.austindsa.org'. Used as the
        visible link text: a button reading "Open the wiki" teaches the reader
        nothing they can use tomorrow, whereas the address itself is the thing
        they actually need to remember. Returns "" when siteUrl is unset so the
        template can simply fall through."""
        if not self.siteUrl:
            return ""
        return urlparse(self.siteUrl).netloc


class ResourceCredential(models.Model):
    """Restricted layer: one row per credential *object*, not per resource - a
    single resource may be reachable through several distinct credentials
    (multiple shared logins, an API token, a 2FA token), so this is
    deliberately finer-grained than ChapterResource. Also the hook a later
    read-only Vaultwarden drift-sync compares against (vaultCollection)."""

    class Kind:
        INDIVIDUAL_LOGIN = 0
        VAULT_SHARED_LOGIN = 1
        SERVICE_ACCOUNT_KEY = 2
        API_TOKEN = 3
        TWO_FACTOR_TOKEN = 4

    KIND_CHOICES = (
        (Kind.INDIVIDUAL_LOGIN, "Individual login"),
        (Kind.VAULT_SHARED_LOGIN, "Vault shared login"),
        (Kind.SERVICE_ACCOUNT_KEY, "Service-account key"),
        (Kind.API_TOKEN, "API token"),
        (Kind.TWO_FACTOR_TOKEN, "2FA token"),
    )

    class Status:
        LIVE = 0
        RETIRE_CANDIDATE = 1
        RETIRED = 2

    STATUS_CHOICES = (
        (Status.LIVE, "Live"),
        (Status.RETIRE_CANDIDATE, "Retire candidate"),
        (Status.RETIRED, "Retired"),
    )

    # A shared secret that has never been changed is the registry's most common
    # real risk, and it was previously unrepresentable: there was no date on this
    # model at all, so "how old is this password" had no answer. One year is the
    # threshold rather than the resource-level 90 days because rotating a secret
    # is disruptive work, not a read-through - flagging it quarterly would train
    # people to ignore the flag.
    ROTATE_AFTER_DAYS = 365

    # The kinds the chapter does not hold, vault, or rotate - so the vault
    # collection, the lifecycle status, and both dates have no answer for them
    # and are not asked for.
    #
    # An individual login is one person's own password on their own account. The
    # chapter never sees it, cannot rotate it, and would be recording a fiction
    # by dating it. A 2FA token is bound to one person's device or key; it is
    # replaced when that person is replaced, not on a rotation schedule.
    #
    # This is the one place the rule lives. The form drops the fields, the
    # detail page drops the badges, and getRotationStatus() returns
    # "not-applicable" - all three read from here rather than repeating the
    # membership test, which is how the layer bugs elsewhere in this module got
    # started.
    NO_ROTATION_KINDS = (Kind.INDIVIDUAL_LOGIN, Kind.TWO_FACTOR_TOKEN)

    resource = models.ForeignKey(ChapterResource, on_delete=models.CASCADE, related_name="credentials")
    label = models.CharField(max_length=200, help_text="e.g. 'Example Org shared login #2'.")
    kind = models.IntegerField(choices=KIND_CHOICES)
    vaultCollection = models.CharField(
        max_length=200, blank=True,
        help_text="Vault collection name - the hook for a future read-only drift sync.",
    )
    status = models.IntegerField(choices=STATUS_CHOICES, default=Status.LIVE)
    note = models.TextField(
        blank=True,
        help_text=(
            "Anything a person needs to know before touching this credential. "
            "Example: 'Rotating this signs everybody out, so do it after a "
            "meeting, not during one.'"
        ),
    )

    # Deliberately nullable with no auto_now_add. auto_now_add would backfill
    # every existing row with the migration timestamp, i.e. invent an age for a
    # credential nobody has dated - and this registry's whole discipline is that
    # an unconfirmed fact renders as open work rather than as a confident answer.
    # A null here reads as "not recorded", which is true.
    addedAt = models.DateField(
        null=True, blank=True, default=None,
        help_text="When this credential first existed, as far as anybody knows. "
                   "Leave it empty rather than guessing - empty reads as 'not recorded'.",
    )
    lastRotated = models.DateField(
        null=True, blank=True, default=None,
        help_text=(
            "The day this secret was last actually changed. Not the day somebody "
            "looked at it, and not the day a holder was added or removed - only a "
            "new secret counts."
        ),
    )

    class Meta:
        verbose_name = "Resource Credential"
        ordering = ["resource__name", "label"]

    def __str__(self) -> str:
        return f"{self.resource.name}: {self.label}"

    def tracksRotation(self) -> bool:
        """Whether rotation is a question worth asking of this credential at all.
        See NO_ROTATION_KINDS."""
        return self.kind not in self.NO_ROTATION_KINDS

    def getAgeDays(self) -> int | None:
        """Days since this secret was last changed, falling back to when it was
        added. None when neither date is recorded - which is a distinct answer
        from zero and must not be rendered as one."""
        reference = self.lastRotated or self.addedAt
        if reference is None:
            return None
        return (djangoTimezone.now().date() - reference).days

    def isRotationOverdue(self) -> bool:
        """True only when there is a date to judge. An undated credential is NOT
        reported as overdue: it is reported as undated (see getRotationStatus),
        because "we do not know" and "we know it is old" prompt different work
        and collapsing them loses the distinction the registry exists to keep."""
        if not self.tracksRotation():
            return False
        if self.status == self.Status.RETIRED:
            return False
        age = self.getAgeDays()
        return age is not None and age > self.ROTATE_AFTER_DAYS

    def getRotationStatus(self) -> str:
        """One of "not-applicable", "retired", "unknown", "overdue",
        "never-rotated" or "ok" - the template branches on this rather than
        re-deriving the combination, so the states stay the same states on every
        surface that shows them.

        "not-applicable" is checked first and beats every other answer: an
        individual login with no dates is not an undated credential, it is a
        credential the chapter was never going to date, and reporting it as
        "no dates recorded" would fill the register with gaps that can never be
        closed."""
        if not self.tracksRotation():
            return "not-applicable"
        if self.status == self.Status.RETIRED:
            return "retired"
        if self.getAgeDays() is None:
            return "unknown"
        if self.isRotationOverdue():
            return "overdue"
        if self.lastRotated is None:
            return "never-rotated"
        return "ok"


class ResourceHolder(models.Model):
    """Open layer: the 'who holds it' map - the wiki draft's own biggest
    named gap. Pre-existing access is state (this model); a grant made
    through the app is an event (ResourceGrant, below) - the two are
    deliberately not merged."""

    class How:
        INDIVIDUAL_LOGIN = 0
        VAULT_COLLECTION = 1
        SERVICE_ACCOUNT = 2

    HOW_CHOICES = (
        (How.INDIVIDUAL_LOGIN, "Their own login"),
        (How.VAULT_COLLECTION, "Shared login from the vault"),
        (How.SERVICE_ACCOUNT, "Through the service account"),
    )

    # `how` and `accessLevel` answer two different questions and neither implies
    # the other: `how` is which door somebody comes through, `accessLevel` is what
    # they can do once inside. Most tools have both an ordinary tier and an
    # administrative one - a workspace has members and it has owners - and the
    # registry could not previously tell them apart, so "who has access to Slack"
    # and "who could delete the Slack workspace" were the same list.
    #
    # accessLevel is FREE TEXT, and that is the second version of this field. It
    # was a five-value enum of deliberately generic rungs (Ordinary / Admin /
    # Owner / Primary owner / Not confirmed) chosen so one ladder could fit every
    # row in the register. In use that turned out to be the wrong trade: no tool
    # calls its tiers those words, so every entry was a translation, and the
    # translation lost the only thing an editor actually knew - what the service
    # itself calls the role. "Delegated user", "Billing contact", "Workspace
    # member", "Repo maintainer" are all real answers that had no rung.
    #
    # Free text cannot be reported on, which is why the two booleans below exist
    # instead of being derived from it. See canGrantAccess/ownsAccount.
    ACCESS_LEVEL_EXAMPLES = (
        "Admin",
        "Owner",
        "Primary owner",
        "Delegated user",
        "Ordinary member",
        "Billing contact",
        "Read only",
    )

    resource = models.ForeignKey(ChapterResource, on_delete=models.CASCADE, related_name="holders")
    personName = models.CharField(
        max_length=200, help_text="Primary - most holders have no Echo account.",
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="resourceHolderRows",
    )
    how = models.IntegerField(choices=HOW_CHOICES, default=How.INDIVIDUAL_LOGIN)
    accessLevel = models.CharField(
        max_length=100, blank=True,
        help_text=(
            "What the service itself calls this person's role, in its own words. "
            "Blank reads as not recorded, which is a true answer and better than a "
            "guessed one."
        ),
    )
    # The two facts the register has to be able to READ ACROSS rows, which is why
    # they are structured while the role name is not. chapter_tools_privileged
    # answers "where is the chapter one person away from losing something", and
    # that question cannot be asked of free text - "Delegated user" does not say
    # whether it can remove somebody, and no keyword list would reliably guess.
    #
    # They are deliberately two facts rather than one rung on a ladder, because
    # they come apart in the wild: a billing contact owns the account and cannot
    # touch membership, and a workspace admin removes people but cannot delete the
    # workspace. Collapsing them is what made the old enum need four rungs to say
    # two things.
    canGrantAccess = models.BooleanField(
        default=False,
        help_text="They can change what other people here are allowed to do, including removing somebody.",
    )
    ownsAccount = models.BooleanField(
        default=False,
        help_text="They hold it at the top level: billing, deletion, and who the other owners are.",
    )
    # The single "has anybody actually checked this row" signal. It used to be
    # duplicated: `confirmed` said it, and accessLevel had an UNCONFIRMED rung
    # that said it again about one field, so a row could be confirmed with an
    # unconfirmed level and neither reading was wrong. One flag now.
    confirmed = models.BooleanField(
        default=False, help_text="Unconfirmed holders render as open work, not silence.",
    )
    note = models.TextField(
        blank=True,
        help_text=(
            "Shown to every holder of the viewResourceHolders permission "
            "(organizers), not just the IT committee - so write it for that "
            "audience. Example: 'Added for the newsletter, only needs it until "
            "the drive ends.'"
        ),
    )

    class Meta:
        verbose_name = "Resource Holder"
        ordering = ["resource__name", "personName"]

    def __str__(self) -> str:
        return f"{self.resource.name}: {self.personName}"

    def getDisplayName(self) -> str:
        if self.user is not None:
            return self.user.getDisplayName()
        return self.personName

    def isPrivileged(self) -> bool:
        """Whether this person's departure or absence is a chapter problem.

        Read from the two booleans, never from the role name: a registry that
        guessed privilege by matching words would call a "Read only admin
        console" holder an admin and miss a "Delegated user" who can remove
        people, and both mistakes are silent."""
        return self.canGrantAccess or self.ownsAccount

    def getAccessLevelDisplay(self) -> str:
        """The role name for reading. Blank is a real state - nobody wrote one
        down - and must not render as an empty badge."""
        return self.accessLevel.strip() or "Role not recorded"

    def getPowerSummary(self) -> str:
        """One short phrase naming what the two booleans amount to, so a reader
        does not have to hold two checkboxes in their head. Deliberately not the
        role name: the role name is the service's word, this is the chapter's."""
        if self.ownsAccount and self.canGrantAccess:
            return "Owns it, and can change other people's access"
        if self.ownsAccount:
            return "Owns it"
        if self.canGrantAccess:
            return "Can change other people's access"
        return "No power over other people's access"


class ResourceDependency(models.Model):
    """One resource needs another. A small fixed set of kinds, deliberately not
    a general graph - see the plan's Scope note on why an open-ended
    relationship type is the failure mode here. "Fixed set" is the constraint,
    not the number two: a new kind is a considered addition with a layer
    decision attached, which is exactly what OPEN_KINDS/RESTRICTED_KINDS force.

    The layer split follows one test - does a member need this to take their
    first action?

    SIGN_IN is open because the wiki signs you in through Slack, so "ask in the
    IT channel" is the wrong first step for somebody not in Slack yet.

    REACHED_THROUGH is open for the same reason and answers a sharper question:
    some resources are ones a member never gets access to at all. Nobody is
    given a Google Calendar login; things land on the chapter calendar because
    Echo puts them there, so the member's real task is getting event access in
    Echo. Without this kind the registry has to invent an access story for such
    a resource - and it did exactly that, sending members to ask for calendar
    rights that nobody grants.

    RUNS_ON is restricted because nobody requesting access cares what a thing is
    hosted on, while the committee needs it to answer "what breaks if we lose
    this?".

    Cycles are not prevented, deliberately. Blocking self-reference (below)
    covers the nonsense case. A true cycle check needs a graph walk on every
    save for a situation that has not occurred - this is a decision, not an
    oversight."""

    class Kind:
        SIGN_IN = 0
        RUNS_ON = 1
        REACHED_THROUGH = 2

    KIND_CHOICES = (
        (Kind.SIGN_IN, "Signs you in through"),
        (Kind.RUNS_ON, "Runs on"),
        (Kind.REACHED_THROUGH, "You reach through"),
    )

    # Which layer each kind belongs to, in one place. Every filter in the views
    # reads these instead of naming kinds inline, because the old scattered
    # `kind=SIGN_IN` filters encoded the open/restricted split implicitly in
    # four separate queries - so adding a kind silently omitted it from the open
    # layer (harmless) or, with one careless `exclude`, published a restricted
    # one (not harmless).
    #
    # A kind listed in neither is a bug, and test_every_kind_declares_its_layer
    # fails on it rather than letting it default to either. That mirrors the
    # `kind` field's no-default rule below: an undecided layer must fail loudly,
    # never fail open.
    OPEN_KINDS = (Kind.SIGN_IN, Kind.REACHED_THROUGH)
    RESTRICTED_KINDS = (Kind.RUNS_ON,)

    # Read from both ends. Forward: "the wiki signs you in through Slack".
    # Reverse: "3 tools sign in through Slack". The reverse list is the whole
    # reason this is a relation rather than two more text fields on
    # ChapterResource - it is derived, so it cannot contradict the forward one.
    resource = models.ForeignKey(
        ChapterResource, on_delete=models.CASCADE, related_name="dependencies",
    )
    dependsOn = models.ForeignKey(
        ChapterResource, on_delete=models.CASCADE, related_name="dependents",
    )
    # No default, on purpose. SIGN_IN renders in the OPEN layer and RUNS_ON is
    # restricted, so a default would make the admin's forget-the-dropdown
    # mistake fail *open* - a committee member adding a hosting edge would
    # publish it to every member. With no default the admin renders an empty
    # "---------" and forces the choice.
    kind = models.IntegerField(choices=KIND_CHOICES)
    note = models.CharField(
        max_length=300, blank=True,
        help_text=(
            "Optional qualifier, e.g. 'DNS only' or 'staging as well'. "
            "Note that a note on a sign-in edge is shown to every member."
        ),
    )

    class Meta:
        verbose_name = "Resource Dependency"
        verbose_name_plural = "Resource Dependencies"
        ordering = ["resource__name", "kind", "dependsOn__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "dependsOn", "kind"],
                name="unique_resource_dependency",
            ),
            models.CheckConstraint(
                condition=~models.Q(resource=models.F("dependsOn")),
                name="resource_dependency_not_self",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.resource.name} {self.get_kind_display().lower()} {self.dependsOn.name}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.resource_id and self.resource_id == self.dependsOn_id:
            raise ValidationError("A resource cannot depend on itself.")


class ResourceGrant(models.Model):
    """The grant ledger - an M2 hook only. Not read by any M1 view; exists so
    the M2 request/approve/fulfill flow (AccessRequests gaining a `resource`
    target) has somewhere to land without another migration. Grants made
    through the app are events; pre-existing access is state (ResourceHolder,
    above) - the two must not be merged."""

    resource = models.ForeignKey(ChapterResource, on_delete=models.CASCADE, related_name="grants")
    personName = models.CharField(max_length=200, blank=True)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="resourceGrantsReceived",
    )
    grantedBy = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="resourceGrantsMade",
    )
    grantedAt = models.DateTimeField(auto_now_add=True)
    fulfilledAt = models.DateTimeField(null=True, blank=True, default=None)
    revokedAt = models.DateTimeField(null=True, blank=True, default=None)
    revokedBy = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="resourceGrantsRevoked",
    )
    request = models.ForeignKey(
        AccessRequests, on_delete=models.SET_NULL, blank=True, null=True, related_name="resourceGrants",
    )

    class Meta:
        verbose_name = "Resource Grant"
        ordering = ["-grantedAt"]

    def __str__(self) -> str:
        who = self.personName or (self.user.getDisplayName() if self.user else "unknown")
        return f"{self.resource.name}: {who}"


class ResourceQuestion(models.Model):
    """Restricted layer: the UNCONFIRMED-item workbench (the 08-20 agenda
    item - split the open questions among the room, in the tool). `resource`
    is nullable so chapter-wide questions (not tied to one resource) are
    representable. Unlike the other Chapter Tools models this one IS edited
    in-app (add/assign/resolve) rather than admin-only - see
    chapterToolsViews.chapter_tools_questions."""

    resource = models.ForeignKey(
        ChapterResource, on_delete=models.SET_NULL, blank=True, null=True, related_name="questions",
    )
    question = models.TextField()
    assignedTo = models.CharField(
        max_length=200, blank=True, help_text="Free-text name - most assignees have no Echo account.",
    )
    raisedAt = models.DateTimeField(auto_now_add=True)
    resolvedAt = models.DateTimeField(null=True, blank=True, default=None)
    resolution = models.TextField(blank=True)

    class Meta:
        verbose_name = "Resource Question"
        ordering = ["-raisedAt"]

    def __str__(self) -> str:
        target = self.resource.name if self.resource_id else "Chapter-wide"
        return f"{target}: {self.question[:60]}"

    def isResolved(self) -> bool:
        return self.resolvedAt is not None


class ToolAuditReadLog(models.Model):
    """Append-only: every restricted-section render writes one row (index,
    a resource detail, or the questions workbench). The plan's own briefing
    rule is 'reads are logged, and the log protects you too' - the sensitive
    layer audits itself, and this covers every viewer equally, superusers
    included."""

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="toolAuditReads",
    )
    at = models.DateTimeField(auto_now_add=True)
    target = models.CharField(max_length=200, help_text="Resource name, 'index', or 'questions'.")

    class Meta:
        verbose_name = "Tool Audit Read Log"
        ordering = ["-at"]

    def __str__(self) -> str:
        who = self.user.username if self.user else "unknown"
        return f"{who} read {self.target} @ {self.at:%Y-%m-%d %H:%M}"
