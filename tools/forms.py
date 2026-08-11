import typing
import pytz
import logging
import datetime
import copy

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.safestring import mark_safe

from .timezones import DateTimeWithAcceptedTimeZone, TZ_TO_AN_TZ
from .EventAutomation import EventAutomationDriver, ActionNetworkAutomation

from . import permissions
from .models import (User, EventOwners, AccessRequests, LinkTree, LinkTreeItem, QRCode,
                     Resolution, PostedEvents,
                     ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder)

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


class _OpaqueMultipleValueField(forms.MultipleChoiceField):
    """Carries the renderedChecked snapshot (plan access-self-service-form D1):
    the set of item keys ('o:<id>' / 'p:<id>') the page rendered as checked at
    GET time. The view diffs against this on POST rather than against absolute
    DB state - see accessViews.request_access for why (a pure absolute diff
    silently revokes a held-and-pending item or an authorizer of an expired
    owner, neither of which the page can render as checked/unchecked).

    A forged value is harmless (D1's forgery analysis: it can only narrow what
    the view is willing to treat as a removal, never grant new access), so this
    field must accept ANY string rather than rejecting stale/forged keys as
    "not a valid choice" - the ordinary MultipleChoiceField behavior would do
    exactly that and break the mechanism it exists to support.
    """
    def valid_value(self, value):
        return True


class SelfServiceAccessForm(ManageAccessForm):
    """The one-form request+revoke page (plan access-self-service-form D1/D2).

    Checked means held, same convention as ManageAccessForm's admin-side
    checkboxes - but unlike that form's .set()-everything apply step, POST
    semantics here are a DELTA against the renderedChecked snapshot, not an
    absolute diff. See accessViews.request_access for the diff math and
    REVIEW.md findings F1/F2 for the phantom-revoke failure mode it avoids.

    - drops the inherited ``groups`` field entirely (shadowed to None) - raw
      RBAC groups are an admin concept, not member-facing.
    - adds ``committees`` - the EventOwners a member may join/leave, using the
      same "active" filter as the old AccessRequestForm did (isPermanent OR
      unexpired). No user-specific filtering needed: unlike permissions, an
      owner is never "locked" for a particular member.
    - adds ``justification`` - ManageAccessForm has none; conditionally
      required only when the submitted diff adds something (enforced in the
      view, since that requires reading current DB state the form doesn't have).
    - adds the hidden ``renderedChecked`` snapshot field.
    - keeps ``permissions`` unchanged (still all requestable permissions -
      lock status is a per-request, per-member computation the view does when
      building the checklist rows, not a queryset restriction here).
    """

    class Keys:
        COMMITTEES = "committees"
        PERMISSIONS = ManageAccessForm.Keys.PERMISSIONS
        JUSTIFICATION = "justification"
        RENDERED_CHECKED = "renderedChecked"

    OWNER_PREFIX = "o"
    PERMISSION_PREFIX = "p"

    groups = None

    committees = forms.ModelMultipleChoiceField(
        queryset=EventOwners.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Committees",
    )
    justification = forms.CharField(
        label="Why do you need any newly-checked access?",
        widget=forms.Textarea(attrs={"rows": "4", "class": "form-field w-full"}),
        required=False,
    )
    renderedChecked = _OpaqueMultipleValueField(
        required=False,
        widget=forms.MultipleHiddenInput,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = datetime.datetime.now(datetime.UTC)
        self.fields[self.Keys.COMMITTEES].queryset = (
            EventOwners.objects
            .filter(Q(isPermanent=True) | Q(expiration__gt=now))
            .order_by("name")
        )


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


class QRCodeForm(forms.Form):
    """Create/edit a QR code. Field names mirror the model columns directly, so
    no Keys class is needed. Exactly-one-target is re-implemented here on the
    cleaned data (the model's clean() never runs via the UI save path)."""

    code = forms.SlugField(
        label="Code",
        help_text="Short token in the QR URL, e.g. 'spring-tabling' -> /qr/spring-tabling/.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    label = forms.CharField(
        label="Label",
        help_text="Human label, e.g. 'Spring 2026 tabling flyer'.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    campaign = forms.CharField(
        label="Campaign",
        required=False,
        help_text="Optional medium/source tag to break down scans, e.g. 'flyer' or 'table-tent'.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    tree = forms.ModelChoiceField(
        label="Target: link tree",
        required=False,
        queryset=LinkTree.objects.order_by("title"),
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    item = forms.ModelChoiceField(
        label="Target: link tree item",
        required=False,
        queryset=LinkTreeItem.objects.select_related("tree").order_by("tree__title", "order"),
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    rawUrl = forms.URLField(
        label="Target: raw URL",
        required=False,
        help_text="Target an arbitrary URL instead of a tree/item.",
        widget=forms.URLInput(attrs={"class": "form-field w-full"}),
    )
    isActive = forms.BooleanField(
        label="Active",
        required=False,
        widget=forms.CheckboxInput(),
    )

    def __init__(self, *args, qr=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._qr = qr

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        existing = QRCode.objects.filter(code=code)
        if self._qr is not None:
            existing = existing.exclude(pk=self._qr.pk)
        if existing.exists():
            raise ValidationError("A QR code with that code already exists.")
        return code

    def clean(self):
        cleaned = super().clean()
        targets = [
            bool(cleaned.get("tree")),
            bool(cleaned.get("item")),
            bool(cleaned.get("rawUrl")),
        ]
        chosen = sum(1 for target in targets if target)
        if chosen != 1:
            raise ValidationError(
                "A QR code must point at exactly one target: a link tree, a link tree item, or a raw URL."
            )
        return cleaned


class ResolutionForm(forms.Form):
    """Submit a new resolution. Hand-rolled forms.Form (house style); the view
    calls Resolution.objects.create() directly. The ``kind`` radios are
    hand-rendered as type cards in the template, but the field validates the
    posted value against Kind.CHOICES."""

    title = forms.CharField(
        label="Title", max_length=200,
        widget=forms.TextInput(attrs={
            "class": "form-field w-full",
            "placeholder": "e.g. Endorse the Eastside BRT Plan",
        }),
    )
    kind = forms.ChoiceField(
        label="Type", choices=Resolution.Kind.CHOICES, widget=forms.RadioSelect,
    )
    # Restricted to upcoming meetings in __init__ (runtime ``now`` comparison
    # must not be frozen at import). Optional so a draft can exist before a
    # meeting is chosen.
    targetMeeting = forms.ModelChoiceField(
        label="Target meeting", queryset=PostedEvents.objects.none(), required=False,
        empty_label="Select a meeting (you can add one later)",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    text = forms.CharField(
        label="Resolution text",
        widget=forms.Textarea(attrs={
            "class": "form-field w-full", "rows": "12", "data-markdown-editor": "1",
            "placeholder": "Whereas...\n\nTherefore, be it resolved...",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = datetime.datetime.now(datetime.UTC)
        self.fields["targetMeeting"].queryset = (
            PostedEvents.objects.filter(start__gt=now).order_by("start")
        )


class ResolutionEditForm(forms.Form):
    """Edit a resolution's text and (optionally) re-target its meeting while it is
    still gathering. A change to a locked resolution resets its sign-ons
    (Resolution.replaceText); the view requires confirmReset before applying such
    a change. Re-targeting the meeting is independent - it shifts the filing
    deadline and never resets sign-ons - and is how a proponent gives a resolution
    more time by moving it to a later meeting."""

    text = forms.CharField(
        label="Resolution text",
        widget=forms.Textarea(attrs={
            "class": "form-field w-full", "rows": "12", "data-markdown-editor": "1",
        }),
    )
    # Restricted to upcoming meetings in __init__ (runtime ``now`` must not be
    # frozen at import), mirroring ResolutionForm. Optional so it can be cleared
    # back to "no meeting" (gathers indefinitely, no deadline).
    targetMeeting = forms.ModelChoiceField(
        label="Target meeting", queryset=PostedEvents.objects.none(), required=False,
        empty_label="No meeting yet (gather without a deadline)",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    confirmReset = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = datetime.datetime.now(datetime.UTC)
        self.fields["targetMeeting"].queryset = (
            PostedEvents.objects.filter(start__gt=now).order_by("start")
        )


class ScheduleForm(forms.Form):
    """Place a resolution on an upcoming meeting agenda (GATHERING -> SCHEDULED)."""

    targetMeeting = forms.ModelChoiceField(
        label="Meeting", queryset=PostedEvents.objects.none(),
        empty_label="Select a meeting",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = datetime.datetime.now(datetime.UTC)
        self.fields["targetMeeting"].queryset = (
            PostedEvents.objects.filter(start__gt=now).order_by("start")
        )


class RecordVoteForm(forms.Form):
    """Record the membership's Yes / No / Abstain tally. The view applies
    Resolution.recordVote, which decides adopted vs rejected by the kind's vote
    threshold (two-thirds for amendments and endorsements, otherwise majority)."""

    votesYes = forms.IntegerField(
        label="Yes", min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-field", "min": "0"}),
    )
    votesNo = forms.IntegerField(
        label="No", min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-field", "min": "0"}),
    )
    votesAbstain = forms.IntegerField(
        label="Abstain", min_value=0, initial=0,
        widget=forms.NumberInput(attrs={"class": "form-field", "min": "0"}),
    )

    def clean(self):
        cleaned = super().clean()
        yes = cleaned.get("votesYes") or 0
        no = cleaned.get("votesNo") or 0
        if yes + no <= 0:
            raise ValidationError("Record at least one Yes or No vote.")
        return cleaned


class WithdrawForm(forms.Form):
    """Pull an in-flight resolution before it is voted on."""

    note = forms.CharField(
        label="Reason (optional)", required=False,
        widget=forms.TextInput(attrs={
            "class": "form-field w-full", "placeholder": "e.g. duplicate of an earlier resolution",
        }),
    )


class SupersedeForm(forms.Form):
    """Mark an adopted resolution as repealed, optionally pointing at the later
    resolution that replaced it."""

    replacement = forms.ModelChoiceField(
        label="Replaced by (optional)", queryset=Resolution.objects.none(), required=False,
        empty_label="Repealed (no replacement)",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    note = forms.CharField(
        label="Note (optional)", required=False,
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )

    def __init__(self, *args, excludePk=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Resolution.objects.filter(status=Resolution.Status.ADOPTED)
        if excludePk is not None:
            qs = qs.exclude(pk=excludePk)
        self.fields["replacement"].queryset = qs.order_by("-decidedAt")


# --- Chapter Tools registry forms (manageChapterTools) ---------------------
#
# Same convention as the Link Tree forms above: plain forms.Form subclasses with
# the widget class declared per field, and the form is the SOLE validation
# point - the views assign cleaned values to the model instance field-by-field
# and call .save(), never full_clean(). Two model-level rules are therefore this
# module's responsibility and are re-implemented below:
#
#   ChapterResource.clean()    - a requestable resource must have a steward
#   ResourceDependency.clean() - a resource cannot depend on itself
#
# plus the unique_resource_dependency constraint, which would otherwise reach the
# user as an IntegrityError 500 rather than a field error. Deleting these checks
# does not fall back to the model - it just removes the enforcement.


class _UserChoiceField(forms.ModelChoiceField):
    """Renders "First Last - email" instead of the bare username.

    getUserNameString() is the convention for detail surfaces (see User in
    models.py); a picker of stewards or holders is exactly the case where two
    members with similar names have to be told apart, and the username alone
    often cannot do it."""

    def label_from_instance(self, user):
        return user.getUserNameString()


def _activeUsers():
    return User.objects.filter(is_active=True).order_by("first_name", "last_name", "username")


class UserTypeaheadWidget(forms.Widget):
    """Pick one member by typing, instead of scrolling a <select> of everybody.

    Why not a <select>: it renders one <option> per active member, so the page
    weight and the time to find a name both grow with the chapter. Past a few
    hundred members that control is unusable on a phone and merely bad on a
    desktop, and this registry is meant to outlive the chapter being small.

    The rendered control has TWO states and never both at once - a search box
    when nobody is chosen, and a chip naming the person when somebody is. That
    is a correctness choice, not a tidiness one: a visible text box sitting next
    to a hidden id can disagree with it (type a second name, never pick it, and
    submit), and nothing on screen would show which of the two was about to be
    saved. Clearing the chip is what puts the search box back.

    Only the hidden input carries `name`, so the search box is never submitted
    and value_from_datadict needs no override. The submitted value is a pk, which
    is what ModelChoiceField.to_python already expects - so this stays a plain
    widget swap and every existing validation rule keeps working.
    """

    # Not `is_hidden`: the field still has a visible control and a label.
    template_name = "tools/common/_userTypeahead.html"

    def __init__(self, searchUrlName: str, placeholder: str = "", attrs=None):
        super().__init__(attrs)
        # The URL is resolved at render time, not here - reversing at import time
        # would run before the URLConf is loaded.
        self._searchUrlName = searchUrlName
        self._placeholder = placeholder or "Type a name, username, or email"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        # `value` is a pk (or "" on an unbound form). The chip needs a name to
        # show, and only the widget renders the chip, so the lookup happens here.
        # One query per render of one field, on a page that already runs several.
        # is_active, matching _activeUsers() - which is the queryset the FIELD
        # will validate the submission against. Looking somebody up here that the
        # field would then reject is how the chip ends up naming a person the form
        # refuses to save, with nothing on the page saying why.
        chosen = None
        if value not in (None, ""):
            chosen = User.objects.filter(pk=value, is_active=True).first()
        context["widget"].update({
            "searchUrl": reverse(self._searchUrlName),
            "placeholder": self._placeholder,
            "chosenLabel": chosen.getUserNameString() if chosen is not None else "",
            # A pk that no longer resolves (a member deactivated between two
            # page loads) must not render as "nobody chosen" - the field would
            # then silently clear on save. Say so instead.
            "danglingValue": value if (value not in (None, "") and chosen is None) else "",
        })
        return context


class ChapterResourceForm(forms.Form):
    """Create/edit one ChapterResource.

    The restricted half of the model is gated at the FIELD level, not just in
    the template. RESTRICTED_KEYS are removed from the form entirely unless the
    editor holds viewChapterToolAudit, because a bound form renders current
    values - so leaving them in place for a manageChapterTools-only editor would
    publish delegation tiers and continuity notes to somebody the read views
    deliberately hide them from (chapterToolsViews.chapter_tool_detail builds
    those into context only under hasAudit).

    The restricted set is exactly what detail.html renders inside its
    {% if hasAudit %} block, and the two must stay in step: a field that becomes
    visible there without becoming restricted here is a leak, and the reverse is
    a field nobody can ever edit."""

    class Keys:
        NAME = "name"
        BLURB = "blurb"
        CATEGORY = "category"
        ACCESS_MODEL = "accessModel"
        PAYER = "payer"
        ANNUAL_COST = "annualCost"
        COST_NOTE = "costNote"
        HOW_TO_GET_ACCESS = "howToGetAccess"
        SITE_URL = "siteUrl"
        ACCESS_REQUEST_URL = "accessRequestUrl"
        STEWARD = "steward"
        STEWARD_NAME = "stewardName"
        REQUESTABLE = "requestable"
        LAST_REVIEWED = "lastReviewed"
        REVIEWED_BY = "reviewedBy"
        DELEGATION_TIER = "delegationTier"
        REVOCATION_NOTE = "revocationNote"
        CONTINUITY_NOTE = "continuityNote"

    RESTRICTED_KEYS = (
        Keys.REQUESTABLE,
        Keys.LAST_REVIEWED,
        Keys.REVIEWED_BY,
        Keys.DELEGATION_TIER,
        Keys.REVOCATION_NOTE,
        Keys.CONTINUITY_NOTE,
    )

    # Field -> chapterToolsHelp slug, read by the explainSlugFor filter so the
    # definition renders beside that field's own label. Only the words that look
    # ordinary and are not: `annualCost` needs no gloss, `delegationTier` does.
    # A field whose slug is missing renders exactly as it did before.
    EXPLAIN_SLUGS = {
        Keys.STEWARD: "steward",
        Keys.REQUESTABLE: "requestable",
        Keys.LAST_REVIEWED: "review",
        Keys.DELEGATION_TIER: "delegation-tier",
    }

    name = forms.CharField(
        label="Name",
        max_length=200,
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    blurb = forms.CharField(
        label="What this is",
        required=False,
        help_text="One or two sentences, shown to every logged-in member.",
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
    )
    category = forms.TypedChoiceField(
        label="Category",
        choices=ChapterResource.CATEGORY_CHOICES,
        coerce=int,
        empty_value=ChapterResource.Category.ORGANIZING,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    accessModel = forms.TypedChoiceField(
        label="How access works",
        choices=ChapterResource.ACCESS_MODEL_CHOICES,
        coerce=int,
        empty_value=ChapterResource.AccessModel.UNCONFIRMED,
        help_text="A summary only - the detail lives on this resource's credential rows.",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    payer = forms.TypedChoiceField(
        label="Who pays",
        choices=ChapterResource.PAYER_CHOICES,
        coerce=int,
        empty_value=ChapterResource.Payer.UNCONFIRMED,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    annualCost = forms.DecimalField(
        label="Annual cost",
        required=False,
        max_digits=8,
        decimal_places=2,
        min_value=0,
        help_text="Leave blank if unknown or free.",
        widget=forms.NumberInput(attrs={"step": "0.01", "class": "form-field w-full"}),
    )
    costNote = forms.CharField(
        label="Cost note",
        required=False,
        max_length=300,
        help_text=(
            "Seat caps or per-seat pricing. Shown to every member. Example: "
            "“Five seats on the paid plan; a sixth is $8/month more.”"
        ),
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    howToGetAccess = forms.CharField(
        label="How to get access",
        required=False,
        help_text="The one line a member came for, e.g. 'Ask in #it-committee'. Shown to everyone.",
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
    )
    siteUrl = forms.URLField(
        label="Where it lives",
        required=False,
        assume_scheme="https",
        help_text="For somebody who already has access.",
        widget=forms.URLInput(attrs={"class": "form-field w-full"}),
    )
    accessRequestUrl = forms.URLField(
        label="Access request link",
        required=False,
        assume_scheme="https",
        help_text="An external form or signup page that starts a request today.",
        widget=forms.URLInput(attrs={"class": "form-field w-full"}),
    )
    steward = _UserChoiceField(
        label="Steward (Echo account)",
        required=False,
        queryset=User.objects.none(),
        help_text=(
            "The one person answerable for this tool: they keep this row true and "
            "they review requests for it. Not necessarily the only person with "
            "access, and not necessarily on the committee."
        ),
        widget=UserTypeaheadWidget(
            searchUrlName="chapter-tool-member-search",
            placeholder="Type a name, username, or email",
        ),
    )
    stewardName = forms.CharField(
        label="Steward (name only)",
        required=False,
        max_length=200,
        help_text="Use this when the steward has no Echo account yet.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    requestable = forms.BooleanField(
        label="Members can request this in Echo",
        required=False,
        help_text=(
            "Needs all three: a steward with an Echo account, a Green or Yellow "
            "delegation tier, and no power over other members. Open the "
            "definition next to this label before ticking it."
        ),
        widget=forms.CheckboxInput(),
    )
    lastReviewed = forms.DateField(
        label="Last reviewed",
        required=False,
        help_text=(
            "The day somebody last checked this row against reality - a check, not "
            f"an edit. Blank, or older than {ChapterResource.STALE_AFTER_DAYS} days, "
            "shows a stale flag to the committee."
        ),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-field w-full"}),
    )
    reviewedBy = forms.CharField(
        label="Reviewed by",
        required=False,
        max_length=200,
        help_text="Who did that check, so the claim has a name attached to it.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    delegationTier = forms.TypedChoiceField(
        label="Delegation tier",
        choices=ChapterResource.DELEGATION_TIER_CHOICES,
        coerce=int,
        empty_value=ChapterResource.DelegationTier.UNCLASSIFIED,
        help_text="How freely this may be handed to somebody else. All four tiers are in the definition next to this label.",
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    revocationNote = forms.CharField(
        label="Revocation note",
        required=False,
        help_text=(
            "What taking access away actually costs here. Write the cost, not the "
            "intention. Example: “One shared password, so removing one person "
            "means changing it and telling the other four.”"
        ),
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
    )
    continuityNote = forms.CharField(
        label="Continuity note",
        required=False,
        help_text=(
            "How somebody gets in if the steward is unreachable. Name a second "
            "person or a second route, not a hope. Example: “Recovery codes are "
            "in the vault collection; two other committee members can read it.”"
        ),
        widget=forms.Textarea(attrs={"rows": "3", "class": "form-field w-full"}),
    )

    def __init__(self, *args, resource=None, includeRestricted=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._resource = resource
        self._includeRestricted = includeRestricted
        # Assigned here rather than at class definition time: a queryset
        # evaluated in the class body is captured once per process, so a member
        # who registers after startup would be missing from the picker.
        self.fields[self.Keys.STEWARD].queryset = _activeUsers()
        if not includeRestricted:
            for key in self.RESTRICTED_KEYS:
                del self.fields[key]

    def clean_name(self):
        name = self.cleaned_data[self.Keys.NAME].strip()
        existing = ChapterResource.objects.filter(name__iexact=name)
        if self._resource is not None:
            existing = existing.exclude(pk=self._resource.pk)
        if existing.exists():
            raise ValidationError("A chapter tool with that name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        # ChapterResource.clean()'s rule, re-implemented because the view never
        # calls full_clean(). Read the CURRENT value when the field was dropped
        # for a non-audit editor: they cannot turn `requestable` on, but they
        # can clear the steward of a row that is already requestable, which
        # breaks the same invariant from the other direction.
        if self._includeRestricted:
            requestable = cleaned.get(self.Keys.REQUESTABLE, False)
        else:
            requestable = self._resource is not None and self._resource.requestable
        if requestable and cleaned.get(self.Keys.STEWARD) is None:
            message = (
                "A resource members can request needs a steward with an Echo account - "
                "a request button with no resolvable reviewer would be a dead letter."
            )
            if self._includeRestricted:
                self.add_error(self.Keys.STEWARD, message)
            else:
                # The checkbox that caused this is not on their form, so a
                # field error on `steward` alone would read as arbitrary.
                self.add_error(self.Keys.STEWARD, message)
                self.add_error(None, (
                    "This resource is currently marked requestable by members, so it cannot "
                    "be left without a steward."
                ))

        # The tier rule, also re-implemented from ChapterResource.clean() for the
        # same reason. Enforced only for an audit editor, and deliberately so:
        # both fields in this rule are restricted, so a non-audit editor can
        # neither create the violation nor fix one. Raising it at them would make
        # a row with legacy data permanently unsaveable by the only person in
        # front of it, and the error would name a tier they are not allowed to
        # see.
        if self._includeRestricted:
            tier = cleaned.get(self.Keys.DELEGATION_TIER)
            if (
                cleaned.get(self.Keys.REQUESTABLE, False)
                and tier is not None
                and tier not in ChapterResource.REQUESTABLE_TIERS
            ):
                label = dict(ChapterResource.DELEGATION_TIER_CHOICES).get(tier, tier)
                self.add_error(self.Keys.REQUESTABLE, (
                    f"A {label}-tier tool cannot be opened for member requests. Classify it "
                    "Green or Yellow first if that is genuinely what it is, or leave requests "
                    "closed and keep handing this out deliberately."
                ))
        return cleaned


class ResourceHolderForm(forms.Form):
    """Add/edit one ResourceHolder row - who already has access to a resource.

    personName and user are independently optional but at least one is required:
    the model treats personName as primary (most holders have no Echo account),
    while getDisplayName() prefers the linked user when there is one, so
    requiring both would mean retyping a name Echo already knows."""

    class Keys:
        PERSON_NAME = "personName"
        USER = "user"
        HOW = "how"
        ACCESS_LEVEL = "accessLevel"
        CONFIRMED = "confirmed"
        NOTE = "note"

    EXPLAIN_SLUGS = {Keys.ACCESS_LEVEL: "access-level"}

    personName = forms.CharField(
        label="Name",
        required=False,
        max_length=200,
        help_text="Use this for holders with no Echo account - most of them.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    user = _UserChoiceField(
        label="Echo account",
        required=False,
        queryset=User.objects.none(),
        help_text="Only if this holder has one. Most do not - use the name field above.",
        widget=UserTypeaheadWidget(
            searchUrlName="chapter-tool-member-search",
            placeholder="Type a name, username, or email",
        ),
    )
    how = forms.TypedChoiceField(
        label="How they get in",
        choices=ResourceHolder.HOW_CHOICES,
        coerce=int,
        empty_value=ResourceHolder.How.INDIVIDUAL_LOGIN,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    accessLevel = forms.TypedChoiceField(
        label="What they can do here",
        choices=ResourceHolder.ACCESS_LEVEL_CHOICES,
        coerce=int,
        empty_value=ResourceHolder.AccessLevel.UNCONFIRMED,
        help_text=(
            "How much power they hold, which is a different question from which "
            "door they come through. All five levels are in the definition next to this label."
        ),
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    confirmed = forms.BooleanField(
        label="Confirmed",
        required=False,
        help_text="Leave unchecked until somebody has actually checked. Unconfirmed reads as open work, not as a warning.",
        widget=forms.CheckboxInput(),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        help_text=(
            "Shown to every holder of viewResourceHolders (organizers), not only "
            "the IT committee - so write it for that audience. Example: “Added "
            "for the newsletter, only needs it until the drive ends.”"
        ),
        widget=forms.Textarea(attrs={"rows": "2", "class": "form-field w-full"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[self.Keys.USER].queryset = _activeUsers()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get(self.Keys.PERSON_NAME) and cleaned.get(self.Keys.USER) is None:
            self.add_error(self.Keys.PERSON_NAME, "Give a name, or pick an Echo account.")
        return cleaned


class ResourceCredentialForm(forms.Form):
    """Add/edit one ResourceCredential. RESTRICTED LAYER - every caller must
    already have checked viewChapterToolAudit; this form has no open-layer mode
    because a credential row has no open-layer half."""

    class Keys:
        LABEL = "label"
        KIND = "kind"
        VAULT_COLLECTION = "vaultCollection"
        STATUS = "status"
        ADDED_AT = "addedAt"
        LAST_ROTATED = "lastRotated"
        NOTE = "note"

    # On lastRotated rather than on both dates: one entry covers both fields, and
    # the same panel twice on one short form reads as two different definitions.
    EXPLAIN_SLUGS = {Keys.LAST_ROTATED: "credential-age"}

    label = forms.CharField(
        label="Label",
        max_length=200,
        help_text="What this credential is, e.g. 'shared login #2'. Not the secret itself.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    kind = forms.TypedChoiceField(
        label="Kind",
        choices=ResourceCredential.KIND_CHOICES,
        coerce=int,
        empty_value=None,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    vaultCollection = forms.CharField(
        label="Vault collection",
        required=False,
        max_length=200,
        help_text="The collection name in the password vault - the hook for a future drift sync.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )
    status = forms.TypedChoiceField(
        label="Status",
        choices=ResourceCredential.STATUS_CHOICES,
        coerce=int,
        empty_value=ResourceCredential.Status.LIVE,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    addedAt = forms.DateField(
        label="First existed",
        required=False,
        help_text=(
            "As far as anybody knows. Leave it blank rather than guessing - blank "
            "reads as “not recorded”, and a guess reads as a fact."
        ),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-field w-full"}),
    )
    lastRotated = forms.DateField(
        label="Last rotated",
        required=False,
        help_text=(
            "The day the secret was last actually changed. Not the day somebody "
            f"looked at it. Flagged as overdue after {ResourceCredential.ROTATE_AFTER_DAYS} days."
        ),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-field w-full"}),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        help_text=(
            "What somebody needs to know before touching this. Example: “Rotating "
            "this signs everybody out, so do it after a meeting, not during one.”"
        ),
        widget=forms.Textarea(attrs={"rows": "2", "class": "form-field w-full"}),
    )

    def clean(self):
        cleaned = super().clean()
        # A secret cannot have been changed before it existed. Caught here rather
        # than left to the reader, because getAgeDays() prefers lastRotated and
        # would otherwise report a negative age as though it were fresh.
        addedAt = cleaned.get(self.Keys.ADDED_AT)
        lastRotated = cleaned.get(self.Keys.LAST_ROTATED)
        if addedAt and lastRotated and lastRotated < addedAt:
            self.add_error(self.Keys.LAST_ROTATED, (
                "A credential cannot have been rotated before it existed."
            ))
        return cleaned

    def clean_kind(self):
        # `kind` has no model default on purpose, and TypedChoiceField coerces
        # an empty submission to empty_value=None rather than failing, so the
        # required check has to be made here or a None reaches a NOT NULL column.
        kind = self.cleaned_data.get(self.Keys.KIND)
        if kind is None:
            raise ValidationError("Pick what kind of credential this is.")
        return kind


class ResourceDependencyForm(forms.Form):
    """Add/edit one dependency edge from a resource to another.

    `allowRestrictedKinds` controls whether RUNS_ON is offered. The kinds are
    split across visibility layers (ResourceDependency.OPEN_KINDS /
    RESTRICTED_KINDS), so an editor without viewChapterToolAudit must not be
    able to create or read a RUNS_ON edge - and, just as importantly, must not
    be able to retarget an existing one."""

    class Keys:
        DEPENDS_ON = "dependsOn"
        KIND = "kind"
        NOTE = "note"

    dependsOn = forms.ModelChoiceField(
        label="Depends on",
        queryset=ChapterResource.objects.none(),
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    kind = forms.TypedChoiceField(
        label="Relationship",
        choices=(),
        coerce=int,
        empty_value=None,
        widget=forms.Select(attrs={"class": "form-field w-full"}),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        max_length=300,
        help_text="Optional qualifier. A note on a sign-in edge is shown to every member.",
        widget=forms.TextInput(attrs={"class": "form-field w-full"}),
    )

    def __init__(self, *args, resource=None, dependency=None, allowRestrictedKinds=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._resource = resource
        self._dependency = dependency
        allowedKinds = list(ResourceDependency.OPEN_KINDS)
        if allowRestrictedKinds:
            allowedKinds += list(ResourceDependency.RESTRICTED_KINDS)
        self._allowedKinds = set(allowedKinds)
        self.fields[self.Keys.KIND].choices = [
            (value, label) for value, label in ResourceDependency.KIND_CHOICES
            if value in self._allowedKinds
        ]
        # This exclusion is the guard that actually fires for a self-reference,
        # including a hand-edited POST body: the id is not in the queryset, so
        # ModelChoiceField rejects it before clean() ever sees it (verified
        # against the deployed box - the error reads "Select a valid choice",
        # not the message in clean() below). clean()'s check is the backstop for
        # whoever removes this line; keep both.
        candidates = ChapterResource.objects.order_by("name")
        if resource is not None:
            candidates = candidates.exclude(pk=resource.pk)
        self.fields[self.Keys.DEPENDS_ON].queryset = candidates

    def clean_kind(self):
        kind = self.cleaned_data.get(self.Keys.KIND)
        if kind is None:
            raise ValidationError("Pick how the two are related.")
        if kind not in self._allowedKinds:
            # Reachable only by editing the POST body: the choices above never
            # offer a restricted kind to an editor without the audit permission.
            raise ValidationError("That relationship is not one you can set.")
        return kind

    def clean(self):
        cleaned = super().clean()
        dependsOn = cleaned.get(self.Keys.DEPENDS_ON)
        kind = cleaned.get(self.Keys.KIND)
        if dependsOn is None or self._resource is None:
            return cleaned
        # ResourceDependency.clean()'s rule and the model's CheckConstraint.
        # Normally unreachable, because the queryset above already excludes self -
        # this is the backstop if that exclusion is ever dropped, not the primary
        # guard. Do not "simplify" by deleting one of the two.
        if dependsOn.pk == self._resource.pk:
            self.add_error(self.Keys.DEPENDS_ON, "A resource cannot depend on itself.")
            return cleaned
        # unique_resource_dependency. Without this the duplicate reaches the DB
        # and surfaces as an IntegrityError 500 rather than a field error.
        if kind is not None:
            clash = ResourceDependency.objects.filter(
                resource=self._resource, dependsOn=dependsOn, kind=kind,
            )
            if self._dependency is not None:
                clash = clash.exclude(pk=self._dependency.pk)
            if clash.exists():
                self.add_error(self.Keys.DEPENDS_ON, "That relationship is already recorded.")
        return cleaned
