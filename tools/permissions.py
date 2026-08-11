from django.db import models

def _publicPermissionName(perm: str) -> str:
    return "tools."+perm

_PUBLISH_EVENT = "publishEvent"
PUBLISH_EVENT = _publicPermissionName(_PUBLISH_EVENT)
_VIEW_PUBLISHED_EVENTS = "viewPublishedEventList"
VIEW_PUBLISHED_EVENTS = _publicPermissionName(_VIEW_PUBLISHED_EVENTS)

_REQUEST_DELEGATED_EVENT = "requestDelegatedEvent"
REQUEST_DELEGATED_EVENT = _publicPermissionName(_REQUEST_DELEGATED_EVENT)
_APPROVE_DELEGATED_EVENT = "approveDelegatedEvent"
APPROVE_DELEGATED_EVENT = _publicPermissionName(_APPROVE_DELEGATED_EVENT)
_VIEW_DELEGATED_EVENTS = "viewDelegatedEventList"
VIEW_DELEGATED_EVENTS = _publicPermissionName(_VIEW_DELEGATED_EVENTS)
_MANAGE_EVENT_OWNERS = "manageEventOwners"
MANAGE_EVENT_OWNERS = _publicPermissionName(_MANAGE_EVENT_OWNERS)

_MANAGE_LINK_TREE = "manageLinkTree"
MANAGE_LINK_TREE = _publicPermissionName(_MANAGE_LINK_TREE)
_VIEW_LINK_METRICS = "viewLinkMetrics"
VIEW_LINK_METRICS = _publicPermissionName(_VIEW_LINK_METRICS)

_APPROVE_ACCESS_REQUEST = "approveAccessRequest"
APPROVE_ACCESS_REQUEST = _publicPermissionName(_APPROVE_ACCESS_REQUEST)

_VIEW_CHAPTER_TOOL_AUDIT = "viewChapterToolAudit"
VIEW_CHAPTER_TOOL_AUDIT = _publicPermissionName(_VIEW_CHAPTER_TOOL_AUDIT)

_VIEW_RESOURCE_HOLDERS = "viewResourceHolders"
VIEW_RESOURCE_HOLDERS = _publicPermissionName(_VIEW_RESOURCE_HOLDERS)

# Write access to the registry. Deliberately SEPARATE from
# _VIEW_CHAPTER_TOOL_AUDIT rather than folded into it: this one may be held by
# somebody who keeps the open layer accurate (blurbs, how-to-get-access, the
# holder roster) without being trusted with the restricted layer. So holding it
# alone must never become a back door that reads delegation tiers, revocation
# notes, or credentials through an edit form - the CRUD views drop those fields
# unless the viewer ALSO holds the audit permission. See
# chapterToolsViews._resourceFieldSet.
_MANAGE_CHAPTER_TOOLS = "manageChapterTools"
MANAGE_CHAPTER_TOOLS = _publicPermissionName(_MANAGE_CHAPTER_TOOLS)


# Display taxonomy for the access pages - mirrors the home-menu categories.
# A permission missing from every tuple lands in "Other", so new permissions
# stay visible even before they're categorized here.
PERMISSION_CATEGORIES = (
    ("Events", (_PUBLISH_EVENT, _VIEW_PUBLISHED_EVENTS, _REQUEST_DELEGATED_EVENT,
                _APPROVE_DELEGATED_EVENT, _VIEW_DELEGATED_EVENTS, _MANAGE_EVENT_OWNERS)),
    ("Link Trees", (_MANAGE_LINK_TREE, _VIEW_LINK_METRICS)),
    ("Access", (_APPROVE_ACCESS_REQUEST, _VIEW_CHAPTER_TOOL_AUDIT, _VIEW_RESOURCE_HOLDERS,
                _MANAGE_CHAPTER_TOOLS)),
)


def getPermissionCategory(codename: str) -> str:
    for title, codenames in PERMISSION_CATEGORIES:
        if codename in codenames:
            return title
    return "Other"


def shortPermissionLabel(name: str) -> str:
    """'Allowed to publish events' -> 'Publish events', for compact checklists."""
    prefix = "Allowed to "
    if name.startswith(prefix) and len(name) > len(prefix):
        rest = name[len(prefix):]
        return rest[0].upper() + rest[1:]
    return name


def getRequestablePermissions():
    """The permissions a member may ask for on the request-access page.

    Scoped to the custom permissions registered on PermissionRights below -
    users request things like manageLinkTree, never Django's internal model
    CRUD permissions (add_user, delete_linktree, ...).
    """
    # Imported here: this module is imported by models.py at startup, before
    # the app registry is ready for auth model imports.
    from django.contrib.auth.models import Permission

    return Permission.objects.filter(
        content_type__app_label="tools",
        content_type__model="permissionrights",
    ).order_by("name")

class PermissionRights(models.Model):
    class Meta:
        managed = False

        default_permissions = ()

        permissions = (
            (_PUBLISH_EVENT, 'Allowed to publish events'),
            (_REQUEST_DELEGATED_EVENT, 'Allowed to request delegated events'),
            (_APPROVE_DELEGATED_EVENT, 'Allowed to approve delegated events'),
            (_VIEW_DELEGATED_EVENTS, 'Allowed to view delegated events'),
            (_VIEW_PUBLISHED_EVENTS, 'Allowed to view published events'),
            (_MANAGE_EVENT_OWNERS, 'Allowed to manage event owners'),
            (_MANAGE_LINK_TREE, 'Allowed to manage link trees, items, and QR codes'),
            (_VIEW_LINK_METRICS, 'Allowed to view link tree click/scan metrics'),
            (_APPROVE_ACCESS_REQUEST, 'Allowed to approve or deny any access request'),
            (_VIEW_CHAPTER_TOOL_AUDIT, 'Allowed to view chapter-tool audit details'),
            (_VIEW_RESOURCE_HOLDERS, 'Allowed to see who holds access to chapter tools'),
            (_MANAGE_CHAPTER_TOOLS, 'Allowed to add, edit, and delete chapter tools'),
        )