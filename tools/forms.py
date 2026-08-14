import typing
import pytz
import logging
import datetime
import copy

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Count, Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.utils.safestring import mark_safe

from .timezones import DateTimeWithAcceptedTimeZone, TZ_TO_AN_TZ
from .EventAutomation import EventAutomationDriver, ActionNetworkAutomation

from . import permissions
from .models import User, EventOwners, AccessRequests, LinkTree, LinkTreeItem, QRCode

STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]

class EventTypes:
    TYPES = [
        (ActionNetworkAutomation.ANTypes.IN_PERSON, "In Person"),
        (ActionNetworkAutomation.ANTypes.VIRTUAL, "Virtual"),
        (ActionNetworkAutomation.ANTypes.HYBRID,"Hybrid")
    ]

# The chapter's timezone. Owner expirations are entered and displayed in this
# zone (the DB stores UTC, per the localize-at-the-edges discipline).
CHAPTER_TIMEZONE = "America/Chicago"


def _activeOwnerQueryset():
    """Owners that can actually receive events: active (permanent or unexpired)
    AND with at least one authorizer. This is the single definition of a
    "healthy" owner for form dropdowns; ownerViews.py recomputes the same
    predicate per-owner for its health badges - keep the two in sync.

    Called from form __init__, never at module level: the ``now`` comparison
    must be evaluated per-request, not frozen at import.
    """
    now = datetime.datetime.now(datetime.UTC)
    return (EventOwners.objects
            .annotate(ownerAuthorizerCount=Count("authorizers"))
            .filter(ownerAuthorizerCount__gt=0)
            .filter(Q(isPermanent=True) | Q(expiration__gt=now)))


class StaticTextWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        value = value if value is not None else ""
        return mark_safe(f'<span class="static-form-text form-field w-full" name="{name}">{value}</span>')

class NewEventForm(forms.Form):
    class Keys:
        TITLE = "title"
        DESCRIPTION = "description"
        EVENT_TYPE = "eventType"
        # START_DATE = "startDate"
        START_TIME = "startTime"
        # END_DATE = "endDate"
        END_TIME = "endTime"
        TIMEZONE = "timezone"
        INSTRUCTIONS = "instructions"
        LOCATION_NAME = "locationName"
        ADDRESS = "address"
        CITY = "city"
        STATE = "state"
        COUNTRY = "country"
        ZIP_CODE = "zipcode"
        OWNER = "owner"
        IGNORE_RESOLVEABLE_CONFLICTS = "ignoreResolveableConflics"
        # ZOOM_REQUIRED = "zoomRequired"

    # Restricted to healthy owners in __init__ (see _activeOwnerQueryset). This
    # is a selection filter, not the security gate - the views' isActive() and
    # authorizer-membership checks remain authoritative. A crafted POST naming
    # an unhealthy owner fails validation here as an invalid choice.
    owner = forms.ModelChoiceField(
        label="Event Owner",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        to_field_name="name",
        queryset=EventOwners.objects.none()
    )
    title = forms.CharField(
        label="Event title",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    description = forms.CharField(
        label="Description",
        widget=forms.Textarea(attrs={"rows": "5", "class": "form-field w-full"}),
    )
    eventType = forms.TypedChoiceField(
        label="Event Type",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        choices=EventTypes.TYPES,
        coerce=int,
        empty_value=0
    )
    timezoneWarning = forms.CharField(
        initial="""
        When creating an In Person event, Action Network will deduce the timezone by the physical location you enter. 
        So you will need to make sure the timezone field matches the physical location or else the calendar won't be correct. 
        If you are doing an in person event where the location is secret and will be sent by email to RSVPs, you can put '-' in the Location name and address and then fill out the city and zip code.
        """,
        widget=StaticTextWidget(),
        required=False,
        label="Time Zone Warning"
    )
    timezone = forms.ChoiceField(
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        choices={timezone: timezone for timezone in TZ_TO_AN_TZ.keys()},
        initial="America/Chicago",
    )
    # type="datetime-local" gives the native browser date/time picker; its
    # value format is fixed as YYYY-MM-DDTHH:MM, hence the explicit
    # widget format + input_formats.
    startTime = forms.DateTimeField(
        label="Start time",
        widget=forms.DateTimeInput(
            attrs={"class": "form-field w-full", "type": "datetime-local", "step": "60"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
    )
    endTime = forms.DateTimeField(
        label="End time",
        widget=forms.DateTimeInput(
            attrs={"class": "form-field w-full", "type": "datetime-local", "step": "60"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
    )
    instructions = forms.CharField(
        label="Instructions",
        widget=forms.Textarea(attrs={"rows": "5", "class": "form-field w-full"}),
    )
    locationName = forms.CharField(
        label="Location name",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}), required=False
    )
    address = forms.CharField(
        label="Address", widget=forms.TextInput(attrs={"class": "form-field w-full"}), required=False
    )
    city = forms.CharField(
        label="City",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
        initial="Austin",
        required=False
    )
    choices = {state: state for state in STATES}
    state = forms.ChoiceField(
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        choices=choices,
        initial="TX",
        required=False
    )
    country = forms.CharField(
        label="Country",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
        initial="US",
        required=False,
    )
    zipcode = forms.IntegerField(
        label="Zip code", 
        widget=forms.NumberInput(attrs={"class": "form-field w-full"}),
        required=False,
    )
    ignoreResolveableConflics = forms.BooleanField(
        label="Publish even if the calendar is busy",
        help_text="Normally we stop if another event already overlaps this time on Google Calendar. "
        "Check this to publish anyway. (A Zoom conflict can never be overridden - there has to be a free Zoom account.)",
        widget=forms.CheckboxInput(),
        required=False,
    )
    # zoomRequired = forms.BooleanField(
    #     label="Zoom Meeting Required",
    #     widget=forms.CheckboxInput(),
    #     required=False,
    #     initial=True
    # )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[NewEventForm.Keys.OWNER].queryset = _activeOwnerQueryset()

    def clean_zipcode(self):
        data = self.cleaned_data[NewEventForm.Keys.ZIP_CODE]
        # Zip code is not always necessary
        if data is None:
            return ""
        zip_str = str(data)

        if len(zip_str) == 5:
            return data

        else:
            raise ValidationError(_("Zip code must be five digits long"))

    def convertToEventInfo(self) -> EventAutomationDriver.EventInfo | None:
        if not self.is_valid():
            return None
        formData = self.cleaned_data
        timezoneStr = formData[NewEventForm.Keys.TIMEZONE]
        start: datetime.datetime = formData[NewEventForm.Keys.START_TIME]
        end: datetime.datetime = formData[NewEventForm.Keys.END_TIME]
        # The start and end dates in the form appear to assume UTC time zone
        # We need to force localize to the input timezone
        # BTW I hate timezones
        if start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        eventType = formData[NewEventForm.Keys.EVENT_TYPE]
        zoomRequired = eventType in [ActionNetworkAutomation.ANTypes.HYBRID, ActionNetworkAutomation.ANTypes.VIRTUAL]
        eventInfo = EventAutomationDriver.EventInfo(
            title=formData[NewEventForm.Keys.TITLE],
            start=DateTimeWithAcceptedTimeZone(wallTime=start, zoneName=timezoneStr),
            end=DateTimeWithAcceptedTimeZone(wallTime=end, zoneName=timezoneStr),
            locationName=formData[NewEventForm.Keys.LOCATION_NAME],
            streetAddress=formData[NewEventForm.Keys.ADDRESS],
            city=formData[NewEventForm.Keys.CITY],
            state=formData[NewEventForm.Keys.STATE],
            zip=formData[NewEventForm.Keys.ZIP_CODE],
            description=formData[NewEventForm.Keys.DESCRIPTION],
            instructions=formData[NewEventForm.Keys.INSTRUCTIONS],
            country=formData[NewEventForm.Keys.COUNTRY],
            zoomRequired=zoomRequired,
            eventType=eventType
        )
        return eventInfo
    
class ApproveDelegatedEventForm(forms.Form):
    class Keys:
        APPROVE = "approve"
        REASON = "reason"
    approve = forms.ChoiceField(
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        choices={x: x for x in ["YES", "NO"]},
        initial="YES",
    )
    reason = forms.CharField(
        label="Reason (optional)",
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
        required=False,
    )


class RegisterForm(UserCreationForm):
    """Self-service account creation. New accounts are active immediately but
    carry no permissions - everything useful is granted later via groups or an
    access request."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Approval emails and getUserNameString() depend on these.
        for name in ("first_name", "last_name", "email"):
            self.fields[name].required = True
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-field w-full")
        # Autocomplete hints so browser password managers treat this as a
        # proper sign-up form (offer to generate + save the password).
        # UserCreationForm already sets autocomplete="new-password" on both
        # password fields and "username" on username.
        for name, token in (
            ("email", "email"),
            ("first_name", "given-name"),
            ("last_name", "family-name"),
        ):
            self.fields[name].widget.attrs.setdefault("autocomplete", token)
        # Mirror MinimumLengthValidator for native browser validation
        self.fields["password1"].widget.attrs.setdefault("minlength", "8")

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists.")
        return email


class AccessRequestForm(forms.Form):
    class Keys:
        TARGET = "target"
        JUSTIFICATION = "justification"

    OWNER_PREFIX = "o"
    PERMISSION_PREFIX = "p"

    target = forms.ChoiceField(
        label="What access do you need?",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    justification = forms.CharField(
        label="Why do you need it?",
        widget=forms.Textarea(attrs={"rows": "5", "class": "form-field w-full"}),
        min_length=1,
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # Members apply to join an event owner (committee) - approval adds them
        # to owner.authorizers, and the owner's current authorizers become the
        # peer reviewers (mirrors the old group-peer rule). Only owners that can
        # still receive events are offered (permanent or unexpired); an expired
        # owner can't approve anything. RBAC groups are assigned by an admin via
        # Manage Access, not self-requested here.
        now = datetime.datetime.now(datetime.UTC)
        activeOwners = (
            EventOwners.objects
            .filter(Q(isPermanent=True) | Q(expiration__gt=now))
            .order_by("name")
        )
        ownerChoices = [
            (f"{self.OWNER_PREFIX}:{owner.id}", owner.name)
            for owner in activeOwners
        ]
        permissionChoices = [
            (f"{self.PERMISSION_PREFIX}:{permission.id}", permission.name)
            for permission in permissions.getRequestablePermissions()
        ]
        self.fields[self.Keys.TARGET].choices = [
            ("Event Owners", ownerChoices),
            ("Permissions", permissionChoices),
        ]

    def clean(self):
        cleanedData = super().clean()
        targetValue = cleanedData.get(self.Keys.TARGET)
        if not targetValue:
            return cleanedData
        kind, _, targetId = targetValue.partition(":")
        owner = None
        permission = None
        # The ChoiceField already validated the value against the rendered
        # choices, but the target may have been deleted since the form loaded
        if kind == self.OWNER_PREFIX:
            owner = EventOwners.objects.filter(id=targetId).first()
            if owner is None:
                raise ValidationError("The selected option is no longer available.")
            if owner.authorizers.filter(id=self.user.id).exists():
                raise ValidationError(f"You can already publish events for {owner.name}.")
            alreadyPending = AccessRequests.objects.filter(
                requester=self.user, owner=owner, status=AccessRequests.Status.REQUESTED
            ).exists()
        else:
            permission = Permission.objects.filter(id=targetId).first()
            if permission is None:
                raise ValidationError("The selected option is no longer available.")
            if self.user.has_perm("tools." + permission.codename):
                raise ValidationError(f"You already have the permission {permission.name}.")
            alreadyPending = AccessRequests.objects.filter(
                requester=self.user, permission=permission, status=AccessRequests.Status.REQUESTED
            ).exists()
        if alreadyPending:
            raise ValidationError("You already have a pending request for this access.")
        # group is no longer self-requestable here, but the view reads all three
        # keys uniformly when creating the row - keep it present and null.
        cleanedData["group"] = None
        cleanedData["owner"] = owner
        cleanedData["permission"] = permission
        return cleanedData


class ReviewAccessRequestForm(forms.Form):
    class Keys:
        APPROVE = "approve"
        REASON = "reason"

    approve = forms.ChoiceField(
        widget=forms.Select(attrs={"class": "form-field w-full"}),
        choices={x: x for x in ["YES", "NO"]},
        initial="YES",
    )
    reason = forms.CharField(
        label="Reason (optional)",
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
        required=False,
    )


class _PermissionMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.name


class ManageAccessForm(forms.Form):
    """Direct grant/revoke of a member's groups and custom permissions
    (the admin-side counterpart to the request/approve flow)."""

    class Keys:
        GROUPS = "groups"
        PERMISSIONS = "permissions"

    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Groups",
    )
    permissions = _PermissionMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Directly-granted permissions",
        help_text="Permissions the member holds individually, on top of whatever their groups grant.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[self.Keys.PERMISSIONS].queryset = permissions.getRequestablePermissions()


class GroupForm(forms.Form):
    """Create/edit a group on the front-end Manage Groups pages.

    The list page renders only the name field (create); the detail page
    renders all three. Like ManageAccessForm, the checkboxes are rendered
    manually in the template - this form just parses them back."""

    class Keys:
        NAME = "name"
        PERMISSIONS = "permissions"
        ADD_MEMBERS = "addMembers"
        REMOVE_MEMBERS = "removeMembers"

    name = forms.CharField(
        max_length=150,
        label="Group name",
        widget=forms.TextInput(attrs={"class": "form-field"}),
    )
    permissions = _PermissionMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        label="Permissions this group grants",
    )
    # Membership is submitted as deltas, not the full set - the page only ever
    # names the members it's changing, so a stale tab can't wipe a roster and
    # the form scales past orgs too big to render as checkboxes.
    addMembers = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
    )
    removeMembers = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
    )

    def __init__(self, *args, group: Group | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._group = group
        self.fields[self.Keys.PERMISSIONS].queryset = permissions.getRequestablePermissions()
        activeUsers = User.objects.filter(is_active=True).order_by("username")
        self.fields[self.Keys.ADD_MEMBERS].queryset = activeUsers
        self.fields[self.Keys.REMOVE_MEMBERS].queryset = activeUsers

    def clean_name(self):
        name = self.cleaned_data[self.Keys.NAME].strip()
        existing = Group.objects.filter(name__iexact=name)
        if self._group is not None:
            existing = existing.exclude(id=self._group.id)
        if existing.exists():
            raise ValidationError("A group with that name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        adds = set(cleaned.get(self.Keys.ADD_MEMBERS) or [])
        removes = set(cleaned.get(self.Keys.REMOVE_MEMBERS) or [])
        if adds & removes:
            raise ValidationError("A member can't be both added and removed in the same save.")
        return cleaned


class EventOwnerForm(forms.Form):
    """Create/edit an event owner on the front-end Manage Event Owners pages.

    Like GroupForm, authorizer membership is submitted as deltas
    (addAuthorizers / removeAuthorizers hidden inputs staged by the page's
    typeahead JS), so a stale tab can't wipe a roster. Pass ``owner`` for edit
    mode; create mode leaves it None."""

    class Keys:
        NAME = "ownerName"
        EXPIRATION = "ownerExpiration"
        IS_PERMANENT = "ownerIsPermanent"
        ADD_AUTHORIZERS = "addAuthorizers"
        REMOVE_AUTHORIZERS = "removeAuthorizers"

    ownerName = forms.CharField(
        max_length=100,
        label="Owner name",
        widget=forms.TextInput(attrs={"class": "form-field"}),
    )
    # Same datetime-local widget contract as NewEventForm.startTime above.
    ownerExpiration = forms.DateTimeField(
        label="Expires on (Central Time)",
        required=False,
        widget=forms.DateTimeInput(
            attrs={"class": "form-field w-full", "type": "datetime-local", "step": "60"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
        help_text="Ignored while Permanent is on. To deactivate an owner, set a date in the past.",
    )
    ownerIsPermanent = forms.BooleanField(
        label="Permanent (never expires)",
        required=False,
        widget=forms.CheckboxInput(),
    )
    addAuthorizers = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
    )
    removeAuthorizers = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
    )

    def __init__(self, *args, owner: EventOwners | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._owner = owner
        activeUsers = User.objects.filter(is_active=True).order_by("username")
        self.fields[self.Keys.ADD_AUTHORIZERS].queryset = activeUsers
        self.fields[self.Keys.REMOVE_AUTHORIZERS].queryset = activeUsers

    def clean_ownerName(self):
        name = self.cleaned_data[self.Keys.NAME].strip()
        existing = EventOwners.objects.filter(name__iexact=name)
        if self._owner is not None:
            existing = existing.exclude(id=self._owner.id)
        if existing.exists():
            raise ValidationError("An event owner with that name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        adds = set(cleaned.get(self.Keys.ADD_AUTHORIZERS) or [])
        removes = set(cleaned.get(self.Keys.REMOVE_AUTHORIZERS) or [])
        if adds & removes:
            raise ValidationError("An authorizer can't be both added and removed in the same save.")

        isPermanent = cleaned.get(self.Keys.IS_PERMANENT, False)
        expiration = cleaned.get(self.Keys.EXPIRATION)
        if expiration is not None:
            # The datetime-local input is naive Central Time, but under
            # USE_TZ Django hands it to us tagged as the current (UTC)
            # timezone - strip that and localize properly, exactly like
            # NewEventForm.convertToEventInfo does for start/end.
            chapterTimezone = pytz.timezone(CHAPTER_TIMEZONE)
            if expiration.tzinfo is not None and expiration.tzinfo.utcoffset(expiration) is not None:
                expiration = expiration.replace(tzinfo=None)
            cleaned[self.Keys.EXPIRATION] = chapterTimezone.localize(expiration).astimezone(pytz.utc)
        elif isPermanent:
            if self._owner is not None:
                # Edit mode: keep whatever expiration is already stored.
                cleaned[self.Keys.EXPIRATION] = self._owner.expiration
            else:
                # Sentinel: only valid alongside isPermanent=True. isActive()
                # short-circuits on isPermanent so the value is never read,
                # but it must never be written without the flag - a
                # non-permanent owner with this expiration would read as
                # active until 2099.
                cleaned[self.Keys.EXPIRATION] = datetime.datetime(2099, 12, 31, 23, 59, tzinfo=datetime.UTC)
        else:
            self.add_error(self.Keys.EXPIRATION, "Set an expiration date or mark the owner as permanent.")
        return cleaned


# --- Link Tree management forms (maintainers only) -------------------------
#
# All three are plain forms.Form subclasses (the established convention in this
# module) with the widget class declared on each field. Form-level validation is
# the sole, authoritative guard: the views assign cleaned values to the model
# instance field-by-field and call .save() - they never call the model's
# full_clean()/clean(), so the uniqueness and one-target checks below are the
# only enforcement points (mirroring GroupForm.clean_name).


class LinkTreeSettingsForm(forms.Form):
    """Create/edit a link tree's settings. owner is deliberately not exposed
    (reserved for future per-owner scoping); the view leaves it untouched."""

    class Keys:
        SLUG = "slug"
        TITLE = "title"
        DESCRIPTION = "description"
        VISIBILITY = "visibility"
        IS_ACTIVE = "isActive"

    slug = forms.SlugField(
        label="Slug",
        help_text="Used in the public URL, e.g. 'links' -> /t/links/. Lowercase, no spaces.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    title = forms.CharField(
        label="Title",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    description = forms.CharField(
        label="Description",
        required=False,
        help_text="Optional blurb shown under the title on the public page.",
        widget=forms.Textarea(attrs={"rows": "4", "class": "form-field w-full"}),
    )
    visibility = forms.TypedChoiceField(
        label="Visibility",
        choices=LinkTree.VISIBILITY_CHOICES,
        coerce=int,
        empty_value=LinkTree.Visibility.PUBLIC,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    isActive = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Uncheck to take the whole tree offline (returns 404).",
        widget=forms.CheckboxInput(),
    )

    def __init__(self, *args, tree=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._tree = tree

    def clean_slug(self):
        slug = self.cleaned_data[self.Keys.SLUG].strip()
        existing = LinkTree.objects.filter(slug=slug)
        if self._tree is not None:
            existing = existing.exclude(pk=self._tree.pk)
        if existing.exists():
            raise ValidationError("A link tree with that slug already exists.")
        return slug


class LinkTreeItemForm(forms.Form):
    """Add/edit a single link tree item. The view never writes the resolve cache
    (resolvedUrl/resolvedLabel/resolvedAt) - wiki resolution is out-of-band, so
    those fields are not exposed here. visibleFrom/visibleUntil round-trip as
    UTC: the naive datetime-local value is read back as UTC, and stored UTC
    values are rendered without localization."""

    class Keys:
        KIND = "kind"
        ORDER = "order"
        ICON = "icon"
        LABEL = "label"
        SUBTITLE = "subtitle"
        URL = "url"
        IS_ACTIVE = "isActive"
        VISIBLE_FROM = "visibleFrom"
        VISIBLE_UNTIL = "visibleUntil"
        WIKI_MODE = "wikiMode"
        WIKI_QUERY = "wikiQuery"
        WIKI_COLLECTION_ID = "wikiCollectionId"
        PINNED_WIKI_DOC_ID = "pinnedWikiDocId"

    kind = forms.TypedChoiceField(
        label="Kind",
        choices=LinkTreeItem.KIND_CHOICES,
        coerce=int,
        empty_value=LinkTreeItem.Kind.MANUAL,
        help_text="Manual link (you type the URL), wiki link (auto-pulled from Outline), "
        "or a section header (a non-clickable heading that groups the items below it).",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    order = forms.IntegerField(
        label="Order",
        min_value=0,
        help_text="Lower numbers appear first.",
        widget=forms.NumberInput(attrs={"class": "form-field w-full"}),
    )
    icon = forms.CharField(
        label="Icon",
        required=False,
        help_text="Optional emoji shown before the label, e.g. a calendar or ballot box.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    label = forms.CharField(
        label="Label",
        required=False,
        help_text="Button text - or the heading text for a section header. For wiki "
        "links, leave blank to use the document's own title.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    subtitle = forms.CharField(
        label="Subtitle",
        required=False,
        help_text="Optional smaller line shown under the label.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    url = forms.URLField(
        label="URL",
        required=False,
        help_text="Destination URL. Used for manual links (ignored for wiki links and headers).",
        widget=forms.URLInput(attrs={"class": "form-field w-full"}),
    )
    isActive = forms.BooleanField(
        label="Active",
        required=False,
        help_text="Uncheck to hide this item from the page without deleting it.",
        widget=forms.CheckboxInput(),
    )
    # type="datetime-local" gives the native browser picker; its value format is
    # fixed as YYYY-MM-DDTHH:MM. The value is treated as UTC (see clean_* below).
    visibleFrom = forms.DateTimeField(
        label="Visible from (UTC)",
        required=False,
        help_text="Optional: don't show the item before this time (UTC).",
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": "form-field w-full"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
    )
    visibleUntil = forms.DateTimeField(
        label="Visible until (UTC)",
        required=False,
        help_text="Optional: stop showing the item after this time (UTC).",
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "class": "form-field w-full"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
    )
    wikiMode = forms.TypedChoiceField(
        label="Wiki mode",
        choices=LinkTreeItem.WIKI_MODE_CHOICES,
        coerce=int,
        empty_value=LinkTreeItem.WikiMode.LATEST_MATCH,
        required=False,
        help_text="For wiki links: surface the newest document matching the query, "
        "or always link one specific pinned document.",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    wikiQuery = forms.CharField(
        label="Wiki query",
        required=False,
        help_text="For 'latest matching': title text to search, e.g. 'GBM Agenda'.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    wikiCollectionId = forms.CharField(
        label="Wiki collection id",
        required=False,
        help_text="Optional Outline collection id to scope the search.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    pinnedWikiDocId = forms.CharField(
        label="Pinned wiki document id",
        required=False,
        help_text="For 'pinned': the Outline document id.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )

    def clean_visibleFrom(self):
        return self._asUtc(self.cleaned_data.get(self.Keys.VISIBLE_FROM))

    def clean_visibleUntil(self):
        return self._asUtc(self.cleaned_data.get(self.Keys.VISIBLE_UNTIL))

    @staticmethod
    def _asUtc(value):
        """Treat the naive datetime-local value as UTC so the stored value is an
        aware UTC datetime (the model documents these fields as UTC-in-DB)."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get(self.Keys.KIND)
        isManualKind = kind == LinkTreeItem.Kind.MANUAL
        isWikiKind = kind == LinkTreeItem.Kind.WIKI
        isSectionHeaderKind = kind == LinkTreeItem.Kind.SECTION_HEADER

        # Clear cross-kind fields that don't apply, so a kind switch never leaves
        # stale data behind (hidden rows still POST their values).
        if isWikiKind or isSectionHeaderKind:
            cleaned[self.Keys.URL] = ""
        if isManualKind or isSectionHeaderKind:
            cleaned[self.Keys.WIKI_QUERY] = ""
            cleaned[self.Keys.WIKI_COLLECTION_ID] = ""
            cleaned[self.Keys.PINNED_WIKI_DOC_ID] = ""

        # Per-kind required fields.
        if isManualKind and not cleaned.get(self.Keys.URL):
            raise ValidationError("A manual link needs a destination URL.")
        if isWikiKind:
            wikiMode = cleaned.get(self.Keys.WIKI_MODE)
            if wikiMode == LinkTreeItem.WikiMode.PINNED:
                if not cleaned.get(self.Keys.PINNED_WIKI_DOC_ID):
                    raise ValidationError("A pinned wiki link needs a pinned document id.")
            elif not cleaned.get(self.Keys.WIKI_QUERY):
                raise ValidationError("A latest-matching wiki link needs a wiki query.")
        if isSectionHeaderKind and not cleaned.get(self.Keys.LABEL):
            raise ValidationError("A section header needs a label.")

        return cleaned


class LinkTreeItemSelect(forms.Select):
    """Item dropdown that tags every option with the tree it belongs to.

    LinkTreeItem.__str__ is just the link label, so an unfiltered list of every
    item across every tree is a wall of bare labels with no way to tell which
    tree each one came from. The data-tree attribute lets the QR form narrow the
    list to one tree first. Without JS every option still renders, so the field
    keeps working - it is just longer.
    """

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        item = getattr(value, "instance", None)
        if item is not None:
            option["attrs"]["data-tree"] = str(item.tree_id)
        return option


class QRCodeForm(forms.Form):
    """Create/edit a QR code. Field names mirror the model columns directly, so
    no Keys class is needed. Exactly-one-target is re-implemented here on the
    cleaned data (the model's clean() never runs via the UI save path).

    The field order below is the on-screen order and is deliberate: ask what the
    code is for, then ask up front WHERE it points, then show only the one target
    field that answer needs. Before that question existed, every member saw three
    target fields, filled one, and left two blank wondering if that was wrong.

    Two other member-facing traps are handled here rather than in the template:

    - `code` is a plain CharField, not a SlugField. A SlugField rejected 'Summer
      Mutual Aid' with Django's "Enter a valid 'slug'..." message, which reads as
      "everything I type is wrong" to anyone who has never heard the word slug.
      We slugify whatever is typed instead, and fall back to the label so nobody
      has to invent a code at all.
    - `rawUrl` is a plain CharField, not a URLField. A URLField renders
      <input type="url">, and the browser's own validation refuses a
      scheme-less 'austindsa.org' before the request is ever sent - no server
      error, no explanation, just a tooltip. We accept it and add https:// here.
    """

    # Two branches, not three. "A link tree page" and "one link inside a link
    # tree" were the same question asked twice - both start with "which tree?" -
    # so they are one branch that then asks how much of that tree to point at.
    # An empty `item` inside the tree branch means the whole page.
    TARGET_URL = "url"
    TARGET_TREE = "tree"
    TARGET_CHOICES = [
        (TARGET_URL, "A web address"),
        (TARGET_TREE, "A link tree on this site"),
    ]

    label = forms.CharField(
        label="What is this code for?",
        help_text="A name only staff see, so you can find this code again later.",
        widget=forms.TextInput(attrs={
            "class": "form-field w-full",
            "placeholder": "Spring tabling flyer",
        }),
    )
    # required=False so posts that predate this field (the tests, and any script
    # hitting the view directly) still get the old exactly-one-target rule in
    # clean() rather than a hard failure on a field they don't know about.
    targetKind = forms.ChoiceField(
        label="Where should the code send people?",
        choices=TARGET_CHOICES,
        required=False,
        widget=forms.RadioSelect(attrs={"class": "choice-radio"}),
    )
    rawUrl = forms.CharField(
        label="Web address",
        required=False,
        max_length=2000,
        help_text="You can leave off https:// - we add it for you.",
        widget=forms.TextInput(attrs={
            "class": "form-field w-full",
            "inputmode": "url",
            "spellcheck": "false",
            "autocapitalize": "none",
            "placeholder": "austindsa.org/join",
        }),
    )
    tree = forms.ModelChoiceField(
        label="Which link tree?",
        required=False,
        queryset=LinkTree.objects.order_by("title"),
        empty_label="Choose a link tree...",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    item = forms.ModelChoiceField(
        label="How much of it?",
        required=False,
        queryset=LinkTreeItem.objects.select_related("tree").order_by("tree__title", "order"),
        # Blank is a real answer here, not a prompt to choose: it means the whole
        # link tree page. That is what folds the old third branch into this one.
        empty_label="The whole page, with every link on it",
        widget=LinkTreeItemSelect(attrs={"class": "form-field w-full"}),
    )
    code = forms.CharField(
        label="Short code for the scan address",
        required=False,
        max_length=60,
        help_text="Filled in from the name above. Change it if you want something shorter.",
        widget=forms.TextInput(attrs={
            "class": "form-field w-full",
            "spellcheck": "false",
            "autocapitalize": "none",
            "placeholder": "spring-tabling",
        }),
    )
    campaign = forms.CharField(
        label="Campaign tag (optional)",
        required=False,
        help_text="Groups scan counts together, e.g. 'flyer' or 'table-tent'.",
        widget=forms.TextInput(attrs={
            "class": "form-field w-full",
            "placeholder": "flyer",
            # Suggests tags already in use without limiting them to it - the
            # value stays free text, so a new campaign needs no setup step.
            "list": "campaignTagOptions",
            "autocomplete": "off",
        }),
    )
    isActive = forms.BooleanField(
        label="Active",
        required=False,
        widget=forms.CheckboxInput(),
    )

    def __init__(self, *args, qr=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._qr = qr

    def clean_rawUrl(self):
        raw = (self.cleaned_data.get("rawUrl") or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        try:
            URLValidator(schemes=["http", "https"])(raw)
        except ValidationError:
            raise ValidationError(
                "That does not look like a web address. Try something like austindsa.org/join."
            )
        return raw

    def clean_code(self):
        # Reduce whatever was typed to the URL-safe token instead of rejecting it.
        # Empty is allowed here; clean() fills it from the label.
        return slugify(self.cleaned_data.get("code") or "")[:40].strip("-")

    def clean(self):
        cleaned = super().clean()
        self._cleanCode(cleaned)
        self._cleanTarget(cleaned)
        return cleaned

    def _cleanCode(self, cleaned):
        code = cleaned.get("code") or ""
        if not code:
            code = slugify(cleaned.get("label") or "")[:40].strip("-")
        cleaned["code"] = code

        if not code:
            # Reachable when the label is missing or is all punctuation. A missing
            # label already reports itself, so don't stack a second complaint on it.
            if not self.has_error("label"):
                self.add_error("code", "Enter a short code using letters, numbers and dashes.")
            return

        existing = QRCode.objects.filter(code=code)
        if self._qr is not None:
            existing = existing.exclude(pk=self._qr.pk)
        if existing.exists():
            self.add_error(
                "code",
                f"A QR code with the short code '{code}' already exists. Pick a different one.",
            )

    def _cleanTarget(self, cleaned):
        kind = cleaned.get("targetKind")
        if not kind:
            # No up-front answer (old-style post): fall back to inferring the
            # target from whichever field was filled in.
            targets = [
                bool(cleaned.get("tree")),
                bool(cleaned.get("item")),
                bool(cleaned.get("rawUrl")),
            ]
            if sum(1 for target in targets if target) != 1:
                self.add_error(None, ValidationError(
                    "Tell us where this code should send people. It must point at "
                    "exactly one target: a link tree, a link tree item, or a raw URL."
                ))
            return

        if kind == self.TARGET_URL:
            # The tree branch is irrelevant now - drop it rather than making the
            # member go back and clear a field by hand.
            cleaned["tree"] = None
            cleaned["item"] = None
            # has_error means rawUrl already said what was wrong with it (e.g. an
            # unparseable address) - don't also say it is missing.
            if not cleaned.get("rawUrl") and not self.has_error("rawUrl"):
                self.add_error("rawUrl", "Enter the web address this code should open.")
            return

        cleaned["rawUrl"] = ""
        tree = cleaned.get("tree")
        item = cleaned.get("item")

        if not tree and not self.has_error("tree"):
            self.add_error("tree", "Choose the link tree this code should open.")
            return
        if not tree:
            return

        if item is None:
            # Whole page. The model wants exactly one target, so tree stays set.
            return

        # Only reachable by bypassing the tree filter in the browser, but the
        # mismatch would silently point the code into a different tree.
        if item.tree_id != tree.pk:
            self.add_error(
                "item",
                f"'{item}' is not in {tree.title}. Pick a link from that tree, "
                "or choose the whole page.",
            )
            return

        # One link. Narrowing from the tree to a single item replaces the tree as
        # the target - QRCode allows exactly one, and item already implies tree.
        cleaned["tree"] = None