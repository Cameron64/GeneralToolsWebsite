# Demo seed for the demo DB. Run with:
#   manage.py shell --command "exec(open(r'<this file>', encoding='utf-8').read())"
#
# READ THIS BEFORE POINTING IT AT ANYTHING.
#
# "Idempotent" here means ROW COUNTS are stable across reruns. It does NOT mean
# field values are left alone. The Chapter Tools section upserts by name, so
# every rerun rewrites blurb, howToGetAccess, delegationTier, revocationNote,
# lastReviewed and the rest back to what this file says. A human edit made in
# the Django admin is silently reverted on the next deploy. That is fine on a
# throwaway demo box and is destructive anywhere else, which is what the guard
# below exists to enforce.
#
# It is also only partly resumable. The Events, Delegated Events and Link Trees
# sections guard on "does ANY row exist", so a crash halfway through one of them
# leaves the section permanently half-populated - the next run sees rows and
# skips. The Resolutions and Chapter Tools sections guard per row and do heal.
import datetime
import random
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.utils import timezone as djtz

from tools.models import (
    User, EventOwners, PostedEvents, DelegatedEvents, AccessRequests,
    LinkTree, LinkTreeItem, QRCode, LinkEvent,
)

# The guard. Until now the ONLY thing standing between this script and a real
# database was SEED_DEMO_DATA=1 in the entrypoint - one env var, set once, in a
# dashboard nobody reviews. DATABASE_URL decides which database that env var
# points at, and the two are set independently, so "demo seed" and "production
# data" was a two-variable accident away.
#
# DEMO_MODE is the switch that already means "this box is not real": it is what
# stubs event publishing in tools/tasks.py so the demo cannot post to Zoom or
# Action Network for real. Binding the seed to the same switch makes the
# contradictory state impossible - a box cannot be real enough to publish
# events and fake enough to be seeded.
if not getattr(settings, "DEMO_MODE", False):
    raise SystemExit(
        "railway-seed.py refused to run: DEMO_MODE is not on.\n"
        "This script overwrites Chapter Tools rows by name and would revert any "
        "hand-entered registry data on this database.\n"
        "If you meant to seed a demo box, set DEMO_MODE=1 there. If you are "
        "trying to load the REAL registry, you want:\n"
        "    manage.py seed_chapter_tools --file <absolute path> --dry-run"
    )

random.seed(42)
CT = ZoneInfo("America/Chicago")
NOW = djtz.now()
PASSWORD = "organize-atx-2026!"

def ct(y, m, d, hh, mm=0):
    """A Central-time wall clock instant, stored as UTC."""
    return datetime.datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(datetime.timezone.utc)

report = []

# --- Members -----------------------------------------------------------------
MEMBERS = [
    ("alex.rivera",     "Alex",   "Rivera"),
    ("jordan.castillo", "Jordan", "Castillo"),
    ("sam.nguyen",      "Sam",    "Nguyen"),
    ("maria.flores",    "Maria",  "Flores"),
    ("devon.brooks",    "Devon",  "Brooks"),
    ("priya.patel",     "Priya",  "Patel"),
    ("tomas.herrera",   "Tomás",  "Herrera"),
    ("kelly.osullivan", "Kelly",  "O'Sullivan"),
    ("rodrigo.salazar", "Rodrigo","Salazar"),
    ("rosa-demo",       "Rosa",   "Demo"),
]
users = {}
created = 0
for username, first, last in MEMBERS:
    user, wasCreated = User.objects.get_or_create(
        username=username,
        defaults={"first_name": first, "last_name": last,
                  "email": f"{username}@example.com"},
    )
    if wasCreated:
        user.set_password(PASSWORD)
        user.save()
        created += 1
    users[username] = user
report.append(f"users: +{created} (now {User.objects.count()})")

# Group -> (members, permission codenames). Groups are created with their
# grants when missing (fresh DBs); rosters are topped up either way.
GROUPS = {
    "Access Approvers":      (["jordan.castillo"],
                              ["approveAccessRequest"]),
    "Event Publishers":      (["maria.flores", "alex.rivera"],
                              ["publishEvent", "viewPublishedEventList", "viewDelegatedEventList"]),
    "Event Approvers":       (["sam.nguyen", "jordan.castillo"],
                              ["approveDelegatedEvent", "viewDelegatedEventList", "viewPublishedEventList"]),
    "Event Organizers":      (["devon.brooks", "priya.patel"],
                              ["requestDelegatedEvent", "viewDelegatedEventList"]),
    "Link Tree Maintainers": (["kelly.osullivan"],
                              ["manageLinkTree"]),
    "Metrics Viewers":       (["tomas.herrera"],
                              ["viewLinkMetrics"]),
    "Secretary":             (["rodrigo.salazar"],
                              ["administerResolutions"]),
    # A non-superuser committee account, so the Chapter Tools open/restricted
    # split can be shown side by side. Demoing it as "cam" would prove nothing:
    # a superuser sees every restricted section on every page regardless.
    "IT Sub-Committee":      (["jordan.castillo"],
                              ["viewChapterToolAudit", "manageChapterTools"]),
    # The middle tier. Holder rows without the audit block - the role that can
    # answer "who has this account" without being shown revocation notes,
    # staleness, or the credential inventory. Named distinctly from "Event
    # Organizers" above because the two grant unrelated things and both appear
    # by name on the My Access page.
    #
    # Without this account the demo shows only two of the three tiers, and the
    # gated holder section reads as a feature that lost a page rather than as a
    # role boundary. devon.brooks (not alex.rivera) carries it so alex stays the
    # plain-member persona the front-door walkthrough depends on.
    "Chapter Organizers":    (["devon.brooks"],
                              ["viewResourceHolders"]),
    # The point of this group is the WRITE boundary, which is invisible with only
    # one editor persona. devon.brooks can edit chapter tools but does NOT hold
    # viewChapterToolAudit, so the edit page serves them the open layer only: no
    # delegation tier, no revocation or continuity note, no credential rows, no
    # RUNS_ON edges - and saving leaves all of that untouched.
    #
    # jordan.castillo (IT Sub-Committee, above) holds audit AND manage and so
    # gets the full form. Comparing the two accounts on the same resource is the
    # demo: "allowed to edit" is deliberately not "allowed to read everything".
    "Chapter Tool Editors":  (["devon.brooks"],
                              ["manageChapterTools"]),
}
# Fail loudly if any codename above does not exist as a Permission row. The
# filter below would otherwise attach nothing and say nothing, and because
# permissions are only set `if wasCreated`, the group would stay empty forever -
# no later run repairs it. That is exactly how you get a demo account that
# logs in fine and silently sees the wrong tier.
#
# This is also the ordering trap: permissions are created by Django's
# post_migrate handler, so a codename added on a feature branch does not exist
# here until that branch is merged AND migrate has run. The entrypoint runs
# migrate before this script, so the only way to trip this is a half-merge.
# --- Directly-granted permissions ---------------------------------------------
# Every grant in GROUPS arrives through a group, and a group-granted permission
# is NOT self-revocable: user_permissions.remove() is a no-op on it, so the
# self-service access page renders it locked. With groups alone the revoke half
# of that page has nothing to act on and reads as broken rather than as a role
# boundary. Two shapes, one each:
#   priya.patel   - held ONLY directly, so it is the revocable row.
#   tomas.herrera - held directly AND via Metrics Viewers, so it renders locked
#                   and proves the direct grant underneath is not droppable.
# Declared above the guard below so its codenames are covered by it too.
DIRECT_PERMISSIONS = {
    "priya.patel":   ["viewPublishedEventList"],
    "tomas.herrera": ["viewLinkMetrics"],
}

_wanted = (
    {code for _, codes in GROUPS.values() for code in codes}
    | {code for codes in DIRECT_PERMISSIONS.values() for code in codes}
)
_found = set(Permission.objects.filter(
    content_type__app_label="tools",
    content_type__model="permissionrights",
    codename__in=_wanted,
).values_list("codename", flat=True))
_missing = sorted(_wanted - _found)
if _missing:
    raise SystemExit(
        f"railway-seed.py refused to run: no Permission row for {_missing}.\n"
        "Either the branch adding it is not merged here, or migrate has not run "
        "yet. Seeding now would create groups with no permissions and never "
        "repair them."
    )

for groupName, (names, codenames) in GROUPS.items():
    group, _ = Group.objects.get_or_create(name=groupName)
    # Granted on EVERY run, not just `if wasCreated`. That guard is the bug the
    # comment above GROUPS describes: adding a codename to a group that already
    # exists on the box silently did nothing, so the account logged in fine and
    # saw the wrong tier, and no later run repaired it. .add() is idempotent, so
    # re-granting costs nothing.
    #
    # One-way on purpose: this adds but never removes, so a permission deleted
    # from GROUPS stays granted until somebody revokes it by hand. Using .set()
    # instead would make GROUPS authoritative, at the cost of wiping any grant
    # made through the app during a demo - which is the worse surprise.
    group.permissions.add(*Permission.objects.filter(
        content_type__app_label="tools",
        content_type__model="permissionrights",
        codename__in=codenames,
    ))
    group.user_set.add(*[users[n] for n in names])
report.append("group rosters topped up")

# Applied after the group loop so My Access shows both sources side by side.
for username, codenames in DIRECT_PERMISSIONS.items():
    users[username].user_permissions.add(*Permission.objects.filter(
        content_type__app_label="tools",
        content_type__model="permissionrights",
        codename__in=codenames,
    ))
report.append("direct permission grants topped up")

# --- Event owners (the chapter's real standing committees + campaigns) --------
# Idempotent get_or_create by name; authorizer .add() is idempotent too. The
# chapter / electoral / mutualAid variable names are kept for the event sections
# below: LC is the permanent chapter-level owner, electoral -> Political
# Education, mutualAid -> Membership Engagement.
OWNERS = [
    # (name, isPermanent, expiration, [authorizer usernames])
    ("LC",                    True,  ct(2027, 1, 1, 0),   ["sam.nguyen", "jordan.castillo"]),
    ("Political Education",   False, ct(2026, 12, 31, 0), ["sam.nguyen", "priya.patel"]),
    ("Membership Engagement", False, ct(2026, 12, 31, 0), ["jordan.castillo", "devon.brooks"]),
    ("AAA",                   False, ct(2026, 12, 31, 0), ["maria.flores", "alex.rivera"]),
    ("Afro-Soc",              False, ct(2026, 12, 31, 0), ["devon.brooks", "tomas.herrera"]),
    ("Anti-ICE",              False, ct(2026, 12, 31, 0), ["kelly.osullivan", "priya.patel"]),
]
ownersByName = {}
ownersCreated = 0
for ownerName, isPermanent, expiration, authorizerNames in OWNERS:
    owner, wasCreated = EventOwners.objects.get_or_create(
        name=ownerName, defaults={"isPermanent": isPermanent, "expiration": expiration})
    ownersCreated += 1 if wasCreated else 0
    owner.authorizers.add(*[users[name] for name in authorizerNames])
    ownersByName[ownerName] = owner
chapter = ownersByName["LC"]
electoral = ownersByName["Political Education"]
mutualAid = ownersByName["Membership Engagement"]
aaa = ownersByName["AAA"]
report.append(f"owners: +{ownersCreated} (now {EventOwners.objects.count()})")

# --- Published events ----------------------------------------------------------
if not PostedEvents.objects.exists():
    COMMON = dict(city="Austin", state="TX", country="US", instructions="",
                  reason="", timezone="America/Chicago",
                  zoomAccount="events@austindsa.org")
    rows = [
        dict(title="Socialist Night School: Labor History in Texas",
             start=ct(2026, 5, 19, 19), end=ct(2026, 5, 19, 20, 30),
             locationName="Online", streetAddress="", zip="",
             description="Part two of our spring series - the 1968 garbage workers' strike and what it teaches us about public-sector organizing.",
             zoomLink="https://us02web.zoom.us/j/89227431105", zoomRequired=True,
             creator=users["maria.flores"], authorizer=None,
             dateCreated=ct(2026, 5, 5, 12), datePublished=ct(2026, 5, 5, 12, 5)),
        dict(title="Brake Light Clinic",
             start=ct(2026, 5, 23, 10), end=ct(2026, 5, 23, 14),
             locationName="Parque Zaragoza", streetAddress="2608 Gonzales St", zip="78702",
             description="Free brake light replacement - no questions, no tickets. Volunteers needed for intake, install, and outreach tables.",
             zoomLink="", zoomRequired=False,
             creator=users["alex.rivera"], authorizer=None,
             dateCreated=ct(2026, 5, 8, 9), datePublished=ct(2026, 5, 8, 9, 10)),
        dict(title="Medicare for All Canvass - Riverside",
             start=ct(2026, 5, 30, 10), end=ct(2026, 5, 30, 13),
             locationName="Mabel Davis District Park", streetAddress="3427 Parker Ln", zip="78741",
             description="Door-knocking for the M4A pledge drive. Training at 10, pairs out by 10:30, debrief over tacos.",
             zoomLink="", zoomRequired=False,
             creator=users["maria.flores"], authorizer=users["sam.nguyen"],
             dateCreated=ct(2026, 5, 16, 15), datePublished=ct(2026, 5, 17, 9)),
        dict(title="Monthly General Meeting - June",
             start=ct(2026, 6, 14, 14), end=ct(2026, 6, 14, 16),
             locationName="AFL-CIO Auditorium", streetAddress="1106 Lavaca St", zip="78701",
             description="Chapter business, working-group report-backs, and the vote on the fall priority campaign. Childcare provided.",
             zoomLink="https://us02web.zoom.us/j/82110934477", zoomRequired=True,
             creator=users["maria.flores"], authorizer=users["sam.nguyen"],
             dateCreated=ct(2026, 6, 1, 11), datePublished=ct(2026, 6, 1, 11, 30)),
        dict(title="New Member Orientation",
             start=ct(2026, 6, 20, 11), end=ct(2026, 6, 20, 12, 30),
             locationName="Online", streetAddress="", zip="",
             description="What Austin DSA is, how the working groups fit together, and how to plug in this summer.",
             zoomLink="https://us02web.zoom.us/j/85513092246", zoomRequired=True,
             creator=users["alex.rivera"], authorizer=None,
             dateCreated=ct(2026, 6, 3, 17), datePublished=ct(2026, 6, 3, 17, 15)),
        dict(title="Know Your Rights Training (with Workers Defense)",
             start=ct(2026, 6, 27, 18), end=ct(2026, 6, 27, 20),
             locationName="Carver Branch Library", streetAddress="1161 Angelina St", zip="78702",
             description="Bilingual training on workplace and immigration rights, co-hosted with Workers Defense Project.",
             zoomLink="", zoomRequired=False,
             creator=users["maria.flores"], authorizer=users["jordan.castillo"],
             dateCreated=ct(2026, 6, 4, 10), datePublished=ct(2026, 6, 4, 16)),
    ]
    for i, row in enumerate(rows):
        slug = row["title"].lower().replace(" ", "-")[:40].strip("-")
        PostedEvents.objects.create(
            **COMMON, **row, owner=chapter,
            anManageLink=f"https://actionnetwork.org/events/{slug}/manage",
            anShareLink=f"https://actionnetwork.org/events/{slug}",
            gCalLink=f"https://calendar.google.com/calendar/event?eid=demo{i}",
        )
    report.append(f"posted events: {len(rows)} created")
else:
    report.append("posted events: already present")

# --- Delegated events ----------------------------------------------------------
if not DelegatedEvents.objects.exists():
    COMMON = dict(city="Austin", state="TX", country="US", instructions="",
                  timezone="America/Chicago")
    DelegatedEvents.objects.create(
        **COMMON, title="Mutual Aid Supply Drive",
        start=ct(2026, 6, 21, 9), end=ct(2026, 6, 21, 12),
        locationName="Givens Park", streetAddress="3811 E 12th St", zip="78721",
        description="Collecting water, sunscreen, and hygiene kits ahead of the July heat. Drop-off and sorting shifts.",
        creator=users["devon.brooks"], owner=mutualAid,
        status=DelegatedEvents.Status.REQUESTED, zoomRequired=False,
        dateCreated=ct(2026, 6, 4, 19))
    DelegatedEvents.objects.create(
        **COMMON, title="Tenant Union Info Session - Riverside Towers",
        start=ct(2026, 6, 25, 19), end=ct(2026, 6, 25, 20, 30),
        locationName="Riverside Towers Community Room", streetAddress="2401 E Riverside Dr", zip="78741",
        description="Organizing meeting with tenants facing the September rent increase. Spanish interpretation arranged.",
        creator=users["rosa-demo"], owner=chapter,
        status=DelegatedEvents.Status.REQUESTED, zoomRequired=False,
        dateCreated=ct(2026, 6, 5, 21))
    DelegatedEvents.objects.create(
        **COMMON, title="Voter Registration Drive at ACC Highland",
        start=ct(2026, 6, 17, 11), end=ct(2026, 6, 17, 14),
        locationName="ACC Highland Campus", streetAddress="6101 Highland Campus Dr", zip="78752",
        description="Tabling with the Electoral WG - deputized registrars on site, summer-session foot traffic.",
        creator=users["priya.patel"], owner=electoral,
        status=DelegatedEvents.Status.APPROVED, approver=users["sam.nguyen"],
        zoomRequired=False, reason="",
        dateCreated=ct(2026, 6, 1, 13), dateReviewed=ct(2026, 6, 2, 9, 30))
    DelegatedEvents.objects.create(
        **COMMON, title="Community Park Cleanup",
        start=ct(2026, 6, 14, 9), end=ct(2026, 6, 14, 11),
        locationName="Edward Rendon Sr. Park", streetAddress="2101 Jesse E. Segovia St", zip="78702",
        description="Trash pickup and tabling along the hike-and-bike trail.",
        creator=users["devon.brooks"], owner=mutualAid,
        status=DelegatedEvents.Status.DENIED, approver=users["jordan.castillo"],
        zoomRequired=False,
        reason="Conflicts with the June General Meeting - resubmit for the following weekend and we'll fast-track it.",
        dateCreated=ct(2026, 5, 31, 16), dateReviewed=ct(2026, 6, 1, 10))
    report.append("delegated events: 4 created (2 pending / 1 approved / 1 denied)")
else:
    report.append("delegated events: already present")

# --- Access requests -----------------------------------------------------------
def perm(codename):
    return Permission.objects.get(codename=codename, content_type__app_label="tools")

def seedRequest(requesterName, dateCreated, **fields):
    requester = users[requesterName]
    if AccessRequests.objects.filter(requester=requester).exists():
        return 0
    request = AccessRequests.objects.create(requester=requester, **fields)
    AccessRequests.objects.filter(id=request.id).update(dateCreated=dateCreated)
    return 1

# Whatever admin account this environment has: the local dev one, else the
# first superuser (the Railway box bootstraps "cam" via createsuperuser).
admin = (User.objects.filter(username="claude-admin").first()
         or User.objects.filter(is_superuser=True).order_by("id").first())
added = 0
# Owner-join requests showcase the self-service flow. Peer reviewers are the
# owner's current authorizers (plus admins): Political Education -> sam.nguyen,
# priya.patel; AAA -> maria.flores, alex.rivera. Approving a join adds the
# member as an authorizer AND into the Event Leads role group (the full
# event-lead bundle: publish + approve delegated events).
added += seedRequest(
    "alex.rivera", ct(2026, 6, 5, 8, 40),
    owner=electoral,
    justification="I've been leading reading groups with Political Education and want to publish our session events directly.",
    status=AccessRequests.Status.REQUESTED)
added += seedRequest(
    "kelly.osullivan", ct(2026, 6, 6, 14, 10),
    owner=aaa,
    justification="I make the graphics for AAA actions and want to post the events myself so they go out on time.",
    status=AccessRequests.Status.REQUESTED)
added += seedRequest(
    "devon.brooks", ct(2026, 6, 4, 17, 15),
    permission=perm("viewPublishedEventList"),
    justification="Need to cross-check campaign dates against the chapter calendar before proposing new ones.",
    status=AccessRequests.Status.REQUESTED)
added += seedRequest(
    "maria.flores", ct(2026, 5, 20, 9, 5),
    group=Group.objects.get(name="Event Publishers"),
    justification="Taking over event publishing from the outgoing co-chair.",
    status=AccessRequests.Status.APPROVED, reviewer=admin,
    reason="Welcome aboard - granted with the June cohort.",
    dateReviewed=ct(2026, 5, 21, 12))
added += seedRequest(
    "sam.nguyen", ct(2026, 5, 27, 20),
    group=Group.objects.get(name="Access Approvers"),
    justification="Happy to help triage the access queue.",
    status=AccessRequests.Status.DENIED, reviewer=admin,
    reason="Keeping the approver list small until after the bylaws vote - let's revisit in August.",
    dateReviewed=ct(2026, 5, 28, 14, 30))
report.append(f"access requests: +{added} (now {AccessRequests.objects.count()})")

# --- Link trees ------------------------------------------------------------------
# Fresh DBs (the Railway demo box) have no trees; the metrics section needs them.
if not LinkTree.objects.exists():
    publicTree = LinkTree.objects.create(
        slug="links", title="Austin DSA",
        description="One link for everything the chapter is doing right now.",
        visibility=LinkTree.Visibility.PUBLIC)
    memberTree = LinkTree.objects.create(
        slug="members", title="Member Resources",
        description="Internal resources - log in to view.",
        visibility=LinkTree.Visibility.MEMBERS)
    for order, (label, url) in enumerate([
        ("Join Austin DSA", "https://act.dsausa.org/join"),
        ("Upcoming Events", "https://austindsa.org/events"),
        ("Brake Light Clinic Signup", "https://austindsa.org/brake-lights"),
        ("Newsletter Signup", "https://austindsa.org/newsletter"),
        ("Donate to the Chapter", "https://austindsa.org/donate"),
    ]):
        LinkTreeItem.objects.create(tree=publicTree, order=order, label=label, url=url)
    for order, (label, url) in enumerate([
        ("Member Handbook", "https://austindsa.org/handbook"),
        ("Committee Directory", "https://austindsa.org/committees"),
        ("Reimbursement Form", "https://austindsa.org/reimburse"),
    ]):
        LinkTreeItem.objects.create(tree=memberTree, order=order, label=label, url=url)
    report.append("link trees: 2 created with items")

# --- Link metrics traffic --------------------------------------------------------
if LinkEvent.objects.count() < 50:
    trees = {t.slug: t for t in LinkTree.objects.all()}
    linksTree, membersTree = trees.get("links"), trees.get("members")

    qrTabling, _ = QRCode.objects.get_or_create(
        code="tabling-june",
        defaults=dict(label="June Tabling Flyer", campaign="Summer Recruitment",
                      tree=linksTree, createdBy=admin))
    qrBrake, _ = QRCode.objects.get_or_create(
        code="brake-light",
        defaults=dict(label="Brake Light Clinic Card", campaign="Mutual Aid",
                      tree=linksTree, createdBy=admin))
    existingQr = QRCode.objects.exclude(code__in=["tabling-june", "brake-light"]).first()

    UAS = ["Mobile Safari", "Chrome Mobile", "Chrome", "Firefox", "Instagram"]
    REFS = ["", "instagram.com", "linktr.ee", "twitter.com", ""]

    madeEvents = []
    for daysAgo in range(14, -1, -1):
        day = NOW - datetime.timedelta(days=daysAgo)
        # Webby weekday rhythm with a spike around the brake-light clinic post
        for tree, base in ((linksTree, 14), (membersTree, 6)):
            if tree is None:
                continue
            items = list(tree.items.all())
            n = max(1, int(random.gauss(base, base / 3)))
            for _ in range(n):
                item = random.choice(items)
                event = LinkEvent.objects.create(
                    tree=tree, item=item, source=LinkEvent.Source.WEB,
                    destinationUrl=item.resolvedUrl or item.url or "https://austindsa.org",
                    visitorHash=f"{random.getrandbits(64):016x}",
                    uaFamily=random.choice(UAS), referrerHost=random.choice(REFS))
                madeEvents.append((event.id, day - datetime.timedelta(
                    minutes=random.randint(0, 14 * 60))))
        for qr, base in ((qrTabling, 4), (qrBrake, 2), (existingQr, 1)):
            if qr is None:
                continue
            for _ in range(max(0, int(random.gauss(base, 1.5)))):
                event = LinkEvent.objects.create(
                    tree=qr.tree, qr=qr, source=LinkEvent.Source.QR,
                    destinationUrl="https://austindsa.org",
                    visitorHash=f"{random.getrandbits(64):016x}",
                    uaFamily=random.choice(UAS[:2]))
                madeEvents.append((event.id, day - datetime.timedelta(
                    minutes=random.randint(0, 14 * 60))))
    # occurredAt is auto_now_add, so spread the timestamps afterwards
    for eventId, occurredAt in madeEvents:
        LinkEvent.objects.filter(id=eventId).update(occurredAt=occurredAt)
    report.append(f"link events: +{len(madeEvents)} across 15 days "
                  f"(now {LinkEvent.objects.count()}), QR codes: {QRCode.objects.count()}")
else:
    report.append("link events: already seeded")

# --- Resolutions ---------------------------------------------------------------
# Sample resolutions across every lifecycle state so the demo exercises the full
# system: gathering sign-ons, on the agenda, adopted (in effect), did-not-pass,
# withdrawn, and superseded - across all four kinds. Idempotent and RESUMABLE:
# each resolution is get_or_create'd by title, and its sign-ons / audit events
# are added only when first created and isolated in try/except, so a re-deploy
# fills in anything a prior boot left behind and one bad row can't abort the rest.
import traceback as _tb
from tools.models import Resolution, ResolutionSignature, ResolutionEvent
from tools.resolutionText import normalizedTextHash
from tools.ActionNetworkAPI.migValidator import MIGStatus

K, S = Resolution.Kind, Resolution.Status

# A pool of signers: the nine named demo members alone cannot reach the
# 25 / 35 sign-on thresholds.
signerPool = []
for n in range(1, 41):
    uname = f"demo-signer-{n:02d}"
    signer, signerNew = User.objects.get_or_create(
        username=uname,
        defaults={"first_name": "Member", "last_name": f"{n:02d}",
                  "email": f"{uname}@example.com"})
    if signerNew:
        signer.set_password(PASSWORD)
        signer.save()
    signerPool.append(signer)

# A future GBM for resolutions still heading to a vote.
julyGBM, _ = PostedEvents.objects.get_or_create(
    title="Monthly General Meeting - July",
    defaults=dict(
        start=ct(2026, 7, 12, 14), end=ct(2026, 7, 12, 16),
        timezone="America/Chicago", locationName="AFL-CIO Auditorium",
        streetAddress="1106 Lavaca St", city="Austin", state="TX",
        zip="78701", country="US",
        description="Chapter business and resolutions up for a vote.",
        instructions="", dateCreated=NOW, datePublished=NOW,
        anManageLink="", anShareLink="", gCalLink="", zoomLink="",
        zoomAccount="events@austindsa.org", reason=""))


def addSignatures(res, count):
    for member in signerPool[:count]:
        ResolutionSignature.objects.get_or_create(
            resolution=res, member=member,
            defaults=dict(textHashAtSigning=normalizedTextHash(res.text),
                          verified=True, verificationStatus=MIGStatus.OK,
                          checkedAt=NOW))


def lockAt(res, when):
    res.locked = True
    res.lockedTextHash = normalizedTextHash(res.text)
    res.lockedAt = when
    res.save()


def addEvent(res, fromStatus, toStatus, note="", at=None):
    event = ResolutionEvent.objects.create(
        resolution=res, actor=admin, fromStatus=fromStatus,
        toStatus=toStatus, note=note)
    if at is not None:
        ResolutionEvent.objects.filter(id=event.id).update(at=at)


def seedRes(title, kind, proponent, status, text, then=None, **extra):
    """Idempotent by title; runs ``then(res)`` (sign-ons + events) only on first
    create, isolated so one failure logs a traceback but does not abort the seed."""
    res, created = Resolution.objects.get_or_create(
        title=title,
        defaults=dict(kind=kind, proponent=proponent, status=status, text=text, **extra))
    if created and then is not None:
        try:
            then(res)
        except Exception:
            _tb.print_exc()
            print(f"SEED_RES_EXTRAS_FAILED: {title}")
    return res


# Gathering sign-ons (general has no threshold).
seedRes("Endorse the Eastside Bus Rapid Transit Plan", K.GENERAL, users["maria.flores"], S.GATHERING,
        "**Whereas** reliable transit on the Eastside is a racial and economic justice issue, and\n\n"
        "**Whereas** the proposed line connects three working-class neighborhoods to downtown jobs,\n\n"
        "**Therefore, be it resolved** that Austin DSA endorses the Eastside BRT Plan and mobilizes members to testify at the next Capital Metro board meeting.")

seedRes("Form a Tenant Organizing Committee", K.PROJECT_COMMITTEE, users["devon.brooks"], S.GATHERING,
        "**Whereas** rent in Austin has outpaced wages for a decade,\n\n"
        "**Therefore, be it resolved** that the chapter form a standing Tenant Organizing Committee to support tenant unions citywide.",
        targetMeeting=julyGBM,
        then=lambda r: (addSignatures(r, 18), lockAt(r, NOW)))

seedRes("Amend Article 7 to Lower Committee Quorum", K.BYLAWS_AMENDMENT, users["priya.patel"], S.GATHERING,
        "**Whereas** several working groups have struggled to reach quorum,\n\n"
        "**Therefore, be it resolved** that Article 7, Section 3 be amended to set committee quorum at three members.",
        targetMeeting=julyGBM,
        then=lambda r: (addSignatures(r, 31), lockAt(r, NOW)))

# On the agenda (threshold met, scheduled for the July GBM).
seedRes("Launch a Healthcare Justice Campaign", K.PROJECT_COMMITTEE, users["kelly.osullivan"], S.SCHEDULED,
        "**Whereas** Texas has the highest uninsured rate in the country,\n\n"
        "**Therefore, be it resolved** that the chapter launch a Healthcare Justice Campaign with a Medicare for All pledge drive.",
        targetMeeting=julyGBM,
        then=lambda r: (addSignatures(r, 27), lockAt(r, NOW),
                        addEvent(r, S.GATHERING, S.SCHEDULED, note="Threshold met; placed on the July agenda.")))

# Adopted, in effect (mirroring the chapter's real passed resolutions).
seedRes("Endorse Jose Garza for District Attorney", K.GENERAL, users["maria.flores"], S.ADOPTED,
        "**Whereas** Jose Garza has advanced decarceral, pro-worker policies,\n\n"
        "**Therefore, be it resolved** that Austin DSA endorses Jose Garza for re-election as Travis County District Attorney.",
        slug="endorse-jose-garza", decidedAt=ct(2023, 12, 13, 15),
        effectiveDate=datetime.date(2023, 12, 13), votesYes=88, votesNo=1, votesAbstain=3,
        then=lambda r: (lockAt(r, ct(2023, 12, 13, 15)),
                        addEvent(r, S.SCHEDULED, S.ADOPTED, note="Adopted at the December GBM.", at=ct(2023, 12, 13, 16))))

seedRes("Schools for All Campaign", K.PROJECT_COMMITTEE, users["devon.brooks"], S.ADOPTED,
        "**Whereas** fully funded public schools are a precondition for a just society,\n\n"
        "**Therefore, be it resolved** that the chapter authorize a multi-year Schools for All campaign.",
        slug="schools-for-all", decidedAt=ct(2023, 9, 26, 15),
        effectiveDate=datetime.date(2023, 9, 26), votesYes=66, votesNo=0, votesAbstain=0,
        then=lambda r: (addSignatures(r, 31), lockAt(r, ct(2023, 9, 26, 15)),
                        addEvent(r, S.SCHEDULED, S.ADOPTED, note="Adopted unanimously.", at=ct(2023, 9, 26, 16))))

r7 = seedRes("YDSA Standing Committee Bylaws Amendment", K.BYLAWS_AMENDMENT, users["priya.patel"], S.ADOPTED,
             "**Whereas** the chapter should formally recognize its youth and student organizing,\n\n"
             "**Therefore, be it resolved** that the bylaws be amended to establish a YDSA Standing Committee.",
             slug="ydsa-standing-committee", decidedAt=ct(2024, 3, 10, 15),
             effectiveDate=datetime.date(2024, 3, 10), votesYes=52, votesNo=10, votesAbstain=3,
             then=lambda r: (addSignatures(r, 38), lockAt(r, ct(2024, 3, 10, 15)),
                             addEvent(r, S.SCHEDULED, S.ADOPTED, note="Adopted by a two-thirds vote.", at=ct(2024, 3, 10, 16))))

seedRes("Endorse Greg Casar for Congress", K.CANDIDATE_ENDORSEMENT, users["kelly.osullivan"], S.ADOPTED,
        "**Whereas** Greg Casar has a record of pro-labor organizing,\n\n"
        "**Therefore, be it resolved** that Austin DSA endorses Greg Casar for Congress in TX-35.",
        slug="endorse-greg-casar", decidedAt=ct(2024, 2, 1, 19),
        effectiveDate=datetime.date(2024, 2, 1), votesYes=70, votesNo=5, votesAbstain=2,
        then=lambda r: (lockAt(r, ct(2024, 2, 1, 19)),
                        addEvent(r, S.SCHEDULED, S.ADOPTED, note="Adopted by a two-thirds vote.", at=ct(2024, 2, 1, 20))))

# Did not pass.
seedRes("Relocate Monthly Meetings Downtown", K.GENERAL, users["tomas.herrera"], S.REJECTED,
        "**Whereas** some members find the current venue hard to reach,\n\n"
        "**Therefore, be it resolved** that monthly meetings move to a downtown location.",
        decidedAt=ct(2026, 5, 17, 15), votesYes=18, votesNo=40, votesAbstain=6,
        then=lambda r: addEvent(r, S.SCHEDULED, S.REJECTED, note="Did not pass.", at=ct(2026, 5, 17, 16)))

# Withdrawn before a vote.
seedRes("Form a Crypto Working Group", K.PROJECT_COMMITTEE, users["sam.nguyen"], S.WITHDRAWN,
        "**Whereas** some members are interested in blockchain technology,\n\n"
        "**Therefore, be it resolved** that the chapter form a Crypto Working Group.",
        then=lambda r: (addSignatures(r, 4), lockAt(r, NOW),
                        addEvent(r, S.GATHERING, S.WITHDRAWN, note="Withdrawn by the proponent.")))

# Superseded by a later adopted resolution (the YDSA amendment, r7 above).
seedRes("Standing Committee Rules (2022)", K.BYLAWS_AMENDMENT, users["priya.patel"], S.SUPERSEDED,
        "**Whereas** the chapter needed interim rules for standing committees in 2022,\n\n"
        "**Therefore, be it resolved** that the attached interim committee rules be adopted.",
        slug="standing-committee-rules-2022", decidedAt=ct(2022, 4, 9, 15),
        effectiveDate=datetime.date(2022, 4, 9), votesYes=40, votesNo=8, votesAbstain=1,
        supersededBy=r7,
        then=lambda r: (addSignatures(r, 36), lockAt(r, ct(2022, 4, 9, 15)),
                        addEvent(r, S.SCHEDULED, S.ADOPTED, note="Adopted as interim rules.", at=ct(2022, 4, 9, 16)),
                        addEvent(r, S.ADOPTED, S.SUPERSEDED, note="Superseded by the 2024 YDSA Standing Committee amendment.", at=ct(2024, 3, 10, 17))))

report.append("resolutions: now %d (%s)" % (
    Resolution.objects.count(),
    dict((s, Resolution.objects.filter(status=s).count()) for s, _ in S.CHOICES)))

# --- Chapter Tools (IT access registry) -----------------------------------------
# Thirteen real chapter systems, seeded for the demo box: the original five, the
# two infrastructure rows added 2026-08-11 (Cloudflare, DigitalOcean), and six
# added 2026-08-13 so that every state the registry can represent has at least
# one row demonstrating it. Every field is populated so the pages can be
# pressure tested with nothing rendering as a dash.
#
# WHY THE SIX ARE THESE SIX. They are not padding and they are not invented
# services. Five of them were already named in the prose of the original rows
# with no row of their own, which made the register quietly self-contradictory -
# four rows sent a reader to "the chapter password vault" and the vault was not
# in the list; Echo's continuity note rests on the GitHub organisation and that
# was not in the list either; and Action Network is the system this entire site
# exists to publish into. A registry that omits the thing its own instructions
# point at is the defect, so adding them is a correction rather than a demo prop.
#
# The sixth (the bank account) is the one genuinely new category, and it earns
# its place by being the only row where somebody owns a thing they cannot hand
# out - which the holder model calls out in its own docstring as the case a
# single privilege ladder cannot express.
#
# THE COVERAGE RULE, and why it is checked rather than trusted. The register is
# a demo of what the registry can SAY, so every enum value, every rotation
# state, and both staleness states need a live example. That silently rots the
# moment somebody edits one row and takes the last example of a state with it,
# which no test catches because the seed is not under test. The check at the
# bottom of this block reports what is uncovered into the boot log. It
# deliberately warns rather than raising - see its comment: a raise here would
# fail the entrypoint and 502 the box over a cosmetic gap.
#
# TWO RULES THIS BLOCK EXISTS TO KEEP, both easy to break by "just adding one
# more row" and neither caught by any test:
#
#   1. OPEN-LAYER FIELDS ARE REAL, RESTRICTED-LAYER PROSE IS GENERIC. The
#      delegation tiers, revocation notes, continuity notes, credential rows and
#      open questions below describe how a *category* of credential behaves
#      (shared password, own login, service account). None of them state an
#      Austin DSA weakness, because this box is reachable by URL. The chapter's
#      actual posture goes in the off-repo seed JSON, run locally only.
#   2. HOLDER NAMES ON THIS BOX ARE FICTIONAL DEMO PERSONAS. Cam (IT Co-chair)
#      explicitly authorised inventing them for the demo box on 2026-08-11, so
#      this reverses what used to be rule 2 here ("no invented people"). The
#      names below are not any real chapter member, confirmed or not - they
#      exist so the registry can be pressure-tested with a full holder map
#      instead of mostly-empty tables. The real holder map lives only in the
#      off-repo seed JSON, run locally only. One resource below is still left
#      deliberately holder-free - see its comment - because "Nobody recorded
#      yet" is itself a real state the registry needs to keep showing, not a
#      gap to be padded away.
#
# Resources/holders/credentials are upserted (this block is the refresh source
# every boot). Questions are create-only, keyed on their text, so assigning or
# resolving one in the app survives the next redeploy. Dependency edges are
# upserted too, keyed on (resource, dependsOn, kind) - the same triple the
# model's unique constraint enforces.
import decimal

from tools.models import (
    ChapterResource, ResourceCredential, ResourceDependency, ResourceHolder,
    ResourceQuestion,
)

Cat, Access, Payer = ChapterResource.Category, ChapterResource.AccessModel, ChapterResource.Payer
Tier = ChapterResource.DelegationTier
CredKind, CredStatus = ResourceCredential.Kind, ResourceCredential.Status
HolderHow = ResourceHolder.How
DepKind = ResourceDependency.Kind
CHAPTER_TOOLS_LAST_REVIEWED = datetime.date(2026, 8, 10)
CHAPTER_TOOLS_REVIEWED_BY = "IT Sub-Committee"

# Most rows share the review stamp above; a few override it, and one of them
# overrides it to None. That is why this is a sentinel rather than
# spec.get("lastReviewed", CHAPTER_TOOLS_LAST_REVIEWED): None and "" are REAL
# values on these two fields - "nobody has ever reviewed this" is the state that
# raises the Stale flag - so a plain .get() default would make the honest answer
# unrepresentable and silently stamp the never-reviewed row as reviewed. Same
# reasoning the credential model gives for leaving addedAt nullable instead of
# auto_now_add.
_UNSET = object()


def _rowValue(spec, key, default):
    value = spec.get(key, _UNSET)
    return default if value is _UNSET else value


def _seedQuestion(resource, question):
    """Create one open question if its text is not already on the box, and
    return 1 if it was created.

    A question may be written as a bare string or as a dict carrying
    assignedTo / resolvedAt / resolution. Both forms exist because most
    questions are genuinely just a question, and making every one of them a dict
    to accommodate the few that are assigned or closed would bury the text - the
    only part a reader of this file cares about - inside punctuation.

    get_or_create, and the extra fields ride in `defaults` so they apply ON
    CREATE ONLY. That is what keeps the create-only promise this block has always
    made: assigning or resolving a question inside the app must survive the next
    redeploy, and an update_or_create here would silently stamp it back to
    whatever this file says every time the box boots."""
    if isinstance(question, str):
        question = {"question": question}
    _, wasCreated = ResourceQuestion.objects.get_or_create(
        resource=resource, question=question["question"],
        defaults=dict(assignedTo=question.get("assignedTo", ""),
                      resolvedAt=question.get("resolvedAt"),
                      resolution=question.get("resolution", "")),
    )
    return 1 if wasCreated else 0

CHAPTER_RESOURCES = [
    dict(
        name="Chapter wiki (Outline)", category=Cat.COMMUNICATION,
        accessModel=Access.INDIVIDUAL, payer=Payer.CHAPTER,
        # The address used to lead this blurb. It now appears in the how-to line
        # and again on the link button, so saying it here made three copies of
        # the same string on one card. The blurb's job is what the thing is FOR.
        blurb="Meeting notes, committee pages, onboarding guides, and chapter documentation. The chapter's written memory.",
        annualCost=None,
        costNote="No separate bill. Runs on chapter-paid hosting shared with the other self-hosted services.",
        # Deliberately does NOT repeat the Slack precondition. The SIGN_IN edge
        # below renders "You need Slack first" directly under this line, so
        # saying it here too printed the same instruction twice in a row. The
        # edge is the source of truth for a precondition; this field is only
        # the step you take once you meet it.
        #
        # The previous text here read "Ask in the IT channel and somebody will
        # add you." That was FALSE and was the worst kind of wrong: it sent a
        # member to go wait on a human for something that needs no human at
        # all. Outline is configured with Slack as its OAuth provider (see the
        # wiki's own "Wiki Installation (Outline)" page - SLACK_CLIENT_ID in
        # docker.env, and "only allow users who have a valid log in for your
        # slack server"), so anyone already in the chapter Slack is already in
        # the wiki. Nobody grants wiki access, because nobody has to.
        howToGetAccess=(
            "Nobody has to add you. Go to wiki.austindsa.org and choose Sign in with Slack. "
            "Everybody in the chapter Slack already has a wiki account, so if you are in Slack "
            "you are in."
        ),
        siteUrl="https://wiki.austindsa.org",
        # No accessRequestUrl on purpose: there is nothing to request. An empty
        # field here is the honest answer, and the template renders no button.
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.YELLOW,
        revocationNote="Individual accounts, so removing one person is a single action and costs nobody else anything.",
        continuityNote="Self-hosted, so continuity depends on the server underneath rather than on any one person's login.",
        holders=[
            dict(personName="Cam D.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
                 note="Reads and writes through the API. The full admin list is still to be confirmed."),
        ],
        credentials=[
            dict(label="Per-person wiki account", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One account per person. There is no shared login for this service."),
        ],
        questions=["Confirm the full list of people who hold wiki admin."],
    ),
    dict(
        name="Slack", category=Cat.COMMUNICATION,
        accessModel=Access.INDIVIDUAL, payer=Payer.UNCONFIRMED,
        blurb="The chapter's day-to-day communication workspace. Committee channels, announcements, and most coordination between meetings.",
        annualCost=None,
        costNote="Plan and payer both still to be confirmed with the Treasurer.",
        # Restated in full rather than pointed at, because this is the end of
        # the chain: a member sent here from the wiki card has already been
        # bounced once and must not be bounced again. The screenshot step is
        # the part people get stuck on, so it is named explicitly along with
        # what to do when you cannot find the email.
        #
        # Source: the wiki's "Slack Access (Admin)" page. The admin who
        # verifies and invites is a named person there and is deliberately NOT
        # named here - see rule 1 in the block above. "A Slack admin" carries
        # the same instruction without publishing who to pester.
        howToGetAccess=(
            "Fill in the chapter's Slack request form. It asks for three things: your name, "
            "the email address you want your Slack account on, and a screenshot of the welcome "
            "email you got when you joined DSA. A Slack admin checks the screenshot and sends "
            "the invite to that address, so this is not instant. If you cannot find a welcome "
            "email, check the address you actually joined DSA with before asking - it is usually "
            "sitting in a different inbox."
        ),
        siteUrl="https://austindsa.slack.com",
        # JUDGEMENT CALL, easy to reverse: this is the live chapter request
        # form, and the demo box is reachable by anyone who registers an
        # account. The link is already mass-emailed to every new member and is
        # on the wiki, so it is not a secret, but it does accept submissions.
        # Tracking parameters from the welcome-email version (can_id,
        # email_referrer) are stripped. Clear this string if you would rather
        # the demo box not point at the real form.
        accessRequestUrl="https://airtable.com/appmDxHAxJKmNNhxI/shrmcJLer3LSYE30y",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.GREEN,
        revocationNote="Deactivating one member's account affects nobody else's.",
        continuityNote="Primary Owner is a single role that can only be transferred, never granted, so it should always sit with someone currently active.",
        holders=[
            # Ordinary, not unconfirmed: we DO know this is a member account. The
            # gap is that no Owner or Admin is recorded, which is what the
            # privileged-access page reports as "no owner recorded".
            dict(personName="Cam D.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
                 accessLevel="Member", canGrantAccess=False, ownsAccount=False,
                 note="Holds a member account. Who holds Owner and Admin is still to be confirmed."),
        ],
        credentials=[
            dict(label="Per-person Slack account", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One account per person, with per-person roles on top."),
        ],
        questions=["Confirm who holds the Slack Owner and Admin roles."],
    ),
    dict(
        name="Zoom", category=Cat.COMMUNICATION,
        accessModel=Access.SHARED_VAULT, payer=Payer.CHAPTER,
        # "Two licenses, used interchangeably" was wrong - there are two
        # SEPARATE paid accounts with different purposes. Corrected 2026-08-11.
        # The account names themselves are real restricted-layer detail and
        # stay in the off-repo seed; the demo box carries only the shape.
        blurb="Chapter Zoom for general meetings, committee meetings, and virtual events. Two separate paid accounts with different purposes, not two interchangeable seats.",
        annualCost=decimal.Decimal("299.80"),
        costNote="Two paid accounts. The figure here is Zoom's published list price, not an invoice anyone has seen - the amount the chapter is actually billed still needs confirming with the Treasurer.",
        # "Ask the IT Sub-Committee" is where the old text stopped, which is the
        # same defect as the wiki row: it names a destination with no route to
        # it. Every one of these ask-a-human paths actually runs through Slack,
        # so the prose says so. Note this is NOT modelled as a dependency edge -
        # SIGN_IN means "signs you in through", and requesting access in a
        # channel is not signing in. The plan bans a third edge kind, and this
        # is exactly the sort of thing that would tempt one.
        howToGetAccess=(
            "Ask the IT Sub-Committee in Slack. The login is shared and handed out through the "
            "chapter password vault, so you need a vault account first and somebody has to add "
            "you to the Zoom collection before the password is visible to you. Say which "
            "meeting you are running: there are two paid accounts and they are not "
            "interchangeable."
        ),
        siteUrl="https://zoom.us",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.RED,
        revocationNote="Shared passwords, and there are two of them. Taking someone out of the vault collection does not take away a password they have already copied, so real revocation means changing the password on BOTH paid accounts and re-sharing each one. Rotating only the main account leaves the second still open to whoever had it.",
        continuityNote="The second factor is shared through the vault alongside the password. Moving 2FA onto a personal phone silently breaks it for everyone else on the login, so it has to stay where it is.",
        holders=[],  # deliberately left empty - see rule 2 above: this is the
        # one resource on the demo box keeping the honest "Nobody recorded
        # yet" state, chosen because it is not part of any dependency edge so
        # the empty state doesn't interfere with pressure-testing those.
        # Two separate paid accounts means two credential rows, not one. Modelling
        # them as a single "shared login" hid the fact that revoking access means
        # rotating TWO passwords, which is the whole point of the credential table
        # being finer-grained than the resource.
        credentials=[
            dict(label="Shared meeting-host login (main account)", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="zoom", status=CredStatus.LIVE,
                 note="One password, held by everyone who runs general meetings."),
            dict(label="Shared meeting-host login (second account)", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="zoom", status=CredStatus.LIVE,
                 note="A separate paid account with its own password, used for a different set of meetings."),
            dict(label="Shared 2FA token", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="zoom", status=CredStatus.LIVE,
                 note="Stored next to the password so it stays usable by everyone on the login. Counted separately because it rotates separately."),
        ],
        questions=[
            "Confirm who is currently in the Zoom vault collection.",
            "Confirm which meetings each of the two paid accounts is meant for, and whether that is written down anywhere.",
        ],
    ),
    dict(
        name="Google Calendar", category=Cat.ORGANIZING,
        # SERVICE_ACCOUNT, not INDIVIDUAL. No person signs into this calendar.
        # Echo's service account writes to it, which is exactly what the
        # SERVICE_ACCOUNT explanation already says.
        accessModel=Access.SERVICE_ACCOUNT, payer=Payer.FREE,
        blurb=(
            "The public chapter calendar. Events published through Echo land here automatically, "
            "which is how nearly everything on it got there."
        ),
        annualCost=None,
        costNote="Free. There is no subscription and no bill.",
        howToGetAccess=(
            "There is nothing to request here, and nobody to request it from. Members do not get "
            "logins to this calendar. Things appear on it because Echo puts them there, so the "
            "way to add an event is to get event-publishing access in Echo and publish it."
        ),
        siteUrl="https://calendar.google.com",
        accessRequestUrl="",
        # Deliberately not blank, and deliberately not a committee name. Blank
        # reads as "not filled in yet" and invites somebody to fill it in with a
        # guess. The honest answer is that we looked and there is no owner - and
        # that answer is the single most useful thing on this card.
        stewardName="Nobody - owner unknown, see continuity note",
        delegationTier=Tier.UNCLASSIFIED,
        revocationNote=(
            "Nothing to revoke from a member, because no member is granted anything. Cutting off "
            "Echo's ability to write here would mean changing Echo's own Google credentials."
        ),
        continuityNote=(
            "NOT COVERED. Somebody created this calendar on a personal Google account years ago, "
            "the chapter started using it, and it stayed that way. We do not know whose account "
            "owns it, so there is nobody to ask for a password reset and no way to add an owner. "
            "If that account is closed or its owner becomes unreachable, the chapter loses the "
            "calendar and the published history on it, and would have to start a new one and "
            "repoint Echo. This is not a theoretical risk and it has no current mitigation."
        ),
        # No holders and no credential rows on purpose: inventing either would
        # re-tell the individual-logins story this card exists to correct. The
        # empty-state prose already says nobody recorded is a job, not a fact.
        holders=[],
        credentials=[],
        questions=[
            "Find out which Google account owns the chapter calendar. Start with whoever was "
            "running comms when it first appeared.",
            "Decide whether to keep this calendar or create one the chapter provably owns and "
            "repoint Echo at it. Keeping it is a choice to accept the loss risk, not a null option.",
        ],
    ),
    dict(
        name="Echo (this site)", category=Cat.INFRASTRUCTURE,
        accessModel=Access.INDIVIDUAL, payer=Payer.CHAPTER,
        blurb="The chapter tools site itself. Publishes an event to Zoom, Action Network, and Google Calendar in one step, and holds this registry.",
        annualCost=None,
        costNote="No separate bill. Runs on chapter-paid hosting shared with the other self-hosted services.",
        howToGetAccess=(
            "Register an account on this site, then open Request Access from the menu and apply "
            "for the permission you need. A brand new account starts with no permissions and an "
            "empty menu: that is expected, not a fault. Somebody who already holds the thing you "
            "asked for reviews the request, so you do not need to find an admin."
        ),
        # Both URLs blank on purpose. You are already on Echo, so a "go here"
        # link would point at the page you are reading, and its request flow is
        # in-app rather than an external form - accessRequestUrl is a URLField
        # and cannot hold the relative /request-access path anyway. This is the
        # one row where the M2 in-app front door already exists.
        siteUrl="",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.GREEN,
        revocationNote="Per-person accounts with per-permission grants, so access can be taken back one permission at a time without touching anyone else.",
        continuityNote="The source code lives in the chapter GitHub organisation, so the site can be rebuilt and redeployed without depending on any one person's account.",
        holders=[
            # Admin, deliberately not Owner: an admin can change other people's
            # access but cannot recover or delete the account, so this row still
            # reads as "no owner recorded" - which is the distinction the level
            # ladder exists to make.
            dict(personName="Cam D.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Site admin", canGrantAccess=True, ownsAccount=False,
                 note="Site admin."),
        ],
        credentials=[
            dict(label="Per-person app account", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One account per person, with permissions granted individually in the app."),
            dict(label="Publishing service-account key", kind=CredKind.SERVICE_ACCOUNT_KEY,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="Used by the site itself to publish events. No person signs in with it."),
        ],
        questions=["Decide whether access requests for the other tools on this list should be routed through this site."],
    ),
    dict(
        name="Cloudflare", category=Cat.INFRASTRUCTURE,
        accessModel=Access.SHARED_VAULT, payer=Payer.CHAPTER,
        blurb="DNS and domain registration for austindsa.org. Nothing else in the chapter's stack routes through Cloudflare's other products.",
        annualCost=None,
        costNote="Domain registration renews annually. The exact amount still needs confirming with the Treasurer.",
        howToGetAccess=(
            "Ask the IT Sub-Committee in Slack, and say what you need to change. DNS and domain "
            "changes go through a shared vault login, so you need a vault account first and "
            "somebody has to add you to the Cloudflare collection. This one breaks the website "
            "and chapter email if it goes wrong, so expect to be asked why rather than handed "
            "the password."
        ),
        siteUrl="https://dash.cloudflare.com",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.RED,
        revocationNote="Shared password. Taking someone out of the vault collection does not take away a password they have already copied, so real revocation means changing the password and re-sharing it with everyone else on it.",
        continuityNote="Whoever holds the vault collection can restore DNS or transfer the domain if the usual admin is unreachable, but there is no automatic fallback outside the vault - losing the vault means losing the domain.",
        holders=[
            # One primary owner and one admin - so this row reads as
            # "one owner only", which is the bus-factor finding rather than a
            # gap in the register.
            dict(personName="Devon K.", how=HolderHow.VAULT_COLLECTION, confirmed=True,
                 accessLevel="Super Administrator", canGrantAccess=True, ownsAccount=True,
                 note="In the Cloudflare vault collection. Can manage DNS records and domain settings."),
            dict(personName="Priya R.", how=HolderHow.VAULT_COLLECTION, confirmed=False,
                 accessLevel="Administrator", canGrantAccess=True, ownsAccount=False,
                 note="Added to the vault collection. Access not yet confirmed against the live account."),
        ],
        credentials=[
            # Dated deliberately far back so the demo box shows the overdue flag.
            dict(label="Shared account login", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="cloudflare", status=CredStatus.LIVE,
                 addedAt=datetime.date(2024, 6, 1), lastRotated=datetime.date(2025, 1, 15),
                 note="One password, held by everyone who manages DNS or the domain."),
            # Added recently and never rotated: a distinct state from overdue,
            # and from having no dates at all. On an API token rather than the
            # 2FA token that used to carry it - a 2FA token is bound to one
            # person's device, so the chapter neither vaults nor rotates it and
            # it has no rotation state to demonstrate.
            dict(label="DNS API token", kind=CredKind.API_TOKEN,
                 vaultCollection="cloudflare", status=CredStatus.LIVE,
                 addedAt=datetime.date(2026, 3, 1),
                 note="Used by the deploy scripts to update DNS records. Rotates separately from the login."),
            # The not-tracked state, so the demo box shows that one too. No vault
            # collection and no dates, which is what the form would now save.
            dict(label="Shared 2FA token", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="Held on one person's device. Not vaulted and not rotated on a schedule, so no dates are recorded for it."),
        ],
        questions=["Confirm who is currently in the Cloudflare vault collection."],
    ),
    dict(
        name="DigitalOcean", category=Cat.INFRASTRUCTURE,
        accessModel=Access.SHARED_VAULT, payer=Payer.CHAPTER,
        blurb="Hosts the chapter wiki. No other chapter system currently runs on this account.",
        annualCost=None,
        costNote="Monthly droplet cost. The exact amount still needs confirming with the Treasurer.",
        howToGetAccess=(
            "Ask the IT Sub-Committee in Slack, and say what you need to do on the server. "
            "Access goes through a shared vault login, so you need a vault account first and "
            "somebody has to add you to the DigitalOcean collection. This account runs the wiki, "
            "so expect to be asked why rather than handed the password."
        ),
        siteUrl="https://cloud.digitalocean.com",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.RED,
        revocationNote="Shared password. Taking someone out of the vault collection does not take away a password they have already copied, so real revocation means changing the password and re-sharing it with everyone else on it.",
        continuityNote="Whoever holds the vault collection can restore the server if the usual admin is unreachable, but there is no automatic fallback outside the vault - losing the vault means losing the host.",
        holders=[
            # Two owners - the only shape on this demo register that is not a
            # finding, so the page has something healthy to contrast against.
            dict(personName="Theo A.", how=HolderHow.VAULT_COLLECTION, confirmed=True,
                 accessLevel="Owner", canGrantAccess=True, ownsAccount=True,
                 note="In the DigitalOcean vault collection. Can manage the droplet running the wiki."),
            dict(personName="Nadia S.", how=HolderHow.VAULT_COLLECTION, confirmed=False,
                 accessLevel="Owner", canGrantAccess=True, ownsAccount=True,
                 note="Added to the vault collection. Access not yet confirmed against the live account."),
        ],
        credentials=[
            dict(label="Shared account login", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="digitalocean", status=CredStatus.LIVE,
                 addedAt=datetime.date(2025, 9, 1), lastRotated=datetime.date(2026, 7, 12),
                 note="One password, held by everyone who manages the droplet."),
            dict(label="Shared 2FA token", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="digitalocean", status=CredStatus.LIVE,
                 note="Stored next to the password so it stays usable by everyone on the login. Counted separately because it rotates separately."),
        ],
        questions=["Confirm who is currently in the DigitalOcean vault collection."],
    ),
    # --- added 2026-08-13, see "WHY THE SIX ARE THESE SIX" above ---------------
    dict(
        name="Action Network", category=Cat.ORGANIZING,
        # MIXED is the honest answer and not a hedge: organisers sign in as
        # themselves, and the key Echo publishes with is a separate credential no
        # person signs in with. Calling the whole row INDIVIDUAL would hide the
        # second half, and calling it SERVICE_ACCOUNT would hide the first.
        accessModel=Access.MIXED,
        # The one genuinely mixed payer in the register. National provides the
        # platform to chapters at no cost to us; metered add-ons are billed to
        # whoever turns them on. Both halves are true at once, which is exactly
        # what this value is for.
        payer=Payer.MIXED,
        blurb=(
            "The chapter's membership, dues, and public event platform. Every event published "
            "through Echo is created here, and this is where RSVPs and member records live."
        ),
        annualCost=None,
        costNote=(
            "National provides the chapter's access to the platform at no charge to us. Metered "
            "add-ons such as text banking are billed separately, which is why the payer reads "
            "Mixed. Whether the chapter has ever been billed for one still needs confirming with "
            "the Treasurer."
        ),
        howToGetAccess=(
            "Ask the IT Sub-Committee in Slack and say which committee you organise for. "
            "Organisers get their own login, so there is no shared password to be handed. The key "
            "Echo publishes with is a separate thing and is not given out to people."
        ),
        siteUrl="https://actionnetwork.org",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        # Red for a reason worth stating precisely, because it is NOT the usual
        # one: revoking here is cheap (individual logins), so the tier is not
        # about revocation cost at all. It is Red because a login reads the
        # member list, which is the "read member data" clause of the Red rung.
        delegationTier=Tier.RED,
        revocationNote=(
            "Individual logins, so removing one organiser is a single action that costs nobody "
            "else anything. The reason this is Red is not the cost of revoking it - it is that "
            "anybody who holds a login can read the member list while they have it."
        ),
        continuityNote=(
            "The platform is national's, so a lost login is recovered through them rather than "
            "through anything the chapter holds. What the chapter has to keep is more than one "
            "person able to add and remove organiser logins."
        ),
        holders=[
            dict(personName="Marisol Vega", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Group administrator", canGrantAccess=True, ownsAccount=False,
                 note="Adds and removes organiser logins on the chapter's group."),
            # The service-account door, which no other row demonstrates. Not a
            # person, deliberately named as one anyway: the roster answers "who
            # can get in", and an automated account that can get in belongs on it
            # or the list quietly under-reports.
            dict(personName="Echo publishing account", how=HolderHow.SERVICE_ACCOUNT, confirmed=True,
                 accessLevel="API user", canGrantAccess=False, ownsAccount=False,
                 note="Not a person. This is what Echo signs in as when it publishes an event, "
                      "and it is on this list because it can get in."),
            dict(personName="Ines Okafor", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
                 accessLevel="Organizer", canGrantAccess=False, ownsAccount=False,
                 note="Organiser login for one committee. Not yet checked against the live account."),
        ],
        credentials=[
            dict(label="Per-person organiser login", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One login per organiser. There is no shared web login for this service."),
            dict(label="Publishing API key", kind=CredKind.API_TOKEN,
                 vaultCollection="action-network", status=CredStatus.LIVE,
                 addedAt=datetime.date(2025, 4, 10), lastRotated=datetime.date(2026, 6, 20),
                 note="Used by Echo to create events. Rotating it stops event publishing until "
                      "Echo's copy is updated too, so the two have to move together."),
            # The retired state, which nothing else in the register shows. Kept as
            # a row rather than deleted on purpose, and the note says why: a
            # credential that vanishes from a list is indistinguishable from one
            # that never existed, so the register loses the fact that it was
            # replaced.
            dict(label="Retired publishing API key", kind=CredKind.API_TOKEN,
                 vaultCollection="action-network", status=CredStatus.RETIRED,
                 addedAt=datetime.date(2023, 9, 1), lastRotated=datetime.date(2024, 2, 14),
                 note="Replaced by the key above and no longer accepted. Kept as a row so the "
                      "register shows it was retired rather than letting it silently disappear."),
        ],
        questions=[
            dict(question="Confirm which committees currently hold organiser logins here, and "
                          "whether any belong to people who have stepped back.",
                 assignedTo="Marisol Vega"),
            dict(question="Decide where Echo's publishing key is stored so exactly one copy is "
                          "authoritative.",
                 assignedTo="IT Sub-Committee", resolvedAt=ct(2026, 7, 28, 18),
                 resolution="Agreed that the vault collection holds the only authoritative copy "
                            "and Echo reads it from there. Closed at the July committee meeting."),
        ],
    ),
    dict(
        name="National membership list", category=Cat.ORGANIZING,
        accessModel=Access.INDIVIDUAL,
        # The only NATIONAL payer, and it is a real one: chapters are given this
        # access, they do not buy it.
        payer=Payer.NATIONAL,
        blurb=(
            "National's own record of who is a member in good standing. Where the chapter's "
            "membership numbers come from when they have to be right."
        ),
        annualCost=None,
        costNote="Provided by national. There is nothing for the chapter to pay and no plan to choose.",
        howToGetAccess=(
            "This one is not granted locally. National decides which chapter officers may pull the "
            "list, so the route is through whoever currently holds it rather than through the IT "
            "Sub-Committee."
        ),
        siteUrl="",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.RED,
        revocationNote=(
            "Not the chapter's to revoke. Access is held per person at national, so removing "
            "somebody means asking national to remove them, and the chapter cannot verify that it "
            "happened."
        ),
        continuityNote=(
            "More than one officer needs to hold this, because a chapter that cannot see its own "
            "membership numbers cannot run a vote. Nothing about that recovery path is in the "
            "chapter's hands."
        ),
        # No owner and no one who can grant: national holds both. That makes this
        # row read as "no owner recorded" on the privileged-access page, which is
        # the correct reading rather than a gap in the register - and it is the
        # only row where that is true by design rather than by omission.
        holders=[
            dict(personName="Teodora Marchetti", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Named recipient", canGrantAccess=False, ownsAccount=False,
                 note="Named at national as able to pull the list. Cannot add anybody else."),
        ],
        credentials=[
            dict(label="Per-person national login", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One login per named officer, issued by national. Nothing shared."),
        ],
        questions=[
            dict(question="Confirm which chapter officers national currently has on file for this, "
                          "and whether that list is still the right one.",
                 assignedTo="Teodora Marchetti"),
        ],
    ),
    dict(
        name="Password vault", category=Cat.INFRASTRUCTURE,
        accessModel=Access.INDIVIDUAL,
        payer=Payer.CHAPTER,
        blurb=(
            "Where the chapter's shared passwords live. Four other things on this list are reached "
            "by getting a password out of here first, which makes this the door in front of the "
            "doors."
        ),
        annualCost=None,
        # Deliberately does not claim a host. DigitalOcean's row says only the
        # wiki runs on that account, and inventing a hosting fact here to fill the
        # blank would contradict a row that was checked. "Not known" is the true
        # answer and it is an open question below rather than a shrug.
        costNote=(
            "No per-seat charge. Where this is hosted, and whether anything is billed for it, "
            "still needs confirming - see the open question."
        ),
        howToGetAccess=(
            "Ask for an account, and say which tool you actually need. An account on its own gives "
            "you nothing: it is only the door, and each shared password sits in a collection that "
            "somebody still has to add you to. Two steps, and people routinely stop after the "
            "first one and assume it did not work."
        ),
        siteUrl="",
        accessRequestUrl="",
        # The steward is an Echo ACCOUNT here, not a text name - the only two rows
        # in the register where that is true. Everything else falls back to
        # stewardName, which is the realistic shape (most stewards have no
        # account), but a register where the FK is never exercised cannot show
        # that a steward can be a real reviewable person.
        steward="jordan.castillo",
        stewardName="",
        delegationTier=Tier.YELLOW,
        # One of two requestable rows. Nothing renders a button yet - that is M2,
        # and `requestable` currently shows up only as a Yes/No on the committee
        # tab - so this exists to demonstrate the field and the invariant behind
        # it: the model refuses to let a row be requestable without a steward, or
        # above Yellow. These two rows are the honest candidates because both
        # genuinely should be self-serve.
        requestable=True,
        revocationNote=(
            "Removing somebody's vault account does not take back a password they already copied "
            "out of it, so a departure means changing the shared passwords they could see, not "
            "just deleting the account. That is the work, and it lands on whoever is left."
        ),
        continuityNote=(
            "More than one person holds vault admin, so accounts and collections can still be "
            "managed if any one of them is unreachable. Losing every admin at once would strand "
            "every shared password in the chapter, which is why that number must never be one."
        ),
        holders=[
            # Both linked to real Echo accounts, which no other row does. The
            # roster renders the account's own name through getDisplayName, so
            # this is the branch where the register and the app agree on who
            # somebody is instead of holding two spellings of them.
            dict(personName="Jordan Castillo", user="jordan.castillo",
                 how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Vault admin", canGrantAccess=True, ownsAccount=True,
                 note="Creates vault accounts and decides who is in each collection."),
            dict(personName="Maria Flores", user="maria.flores",
                 how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Ordinary member", canGrantAccess=False, ownsAccount=False,
                 note="Has an account and is in one collection. Cannot add anybody."),
        ],
        credentials=[
            dict(label="Per-person vault account", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One account per person, with collections shared onto it."),
            # Retire-candidate: a distinct state from retired, and the one the
            # register exists to surface. It also carries dates old enough to be
            # overdue, so this row shows both badges at once - which is realistic,
            # because "nobody knows what uses it" is exactly why it never got
            # rotated.
            dict(label="Admin panel token", kind=CredKind.API_TOKEN,
                 vaultCollection="vault", status=CredStatus.RETIRE_CANDIDATE,
                 addedAt=datetime.date(2024, 11, 5),
                 note="Nothing is known to use this. Flagged to retire rather than deleted, "
                      "because pulling a token no-one has traced is how something breaks a week "
                      "later with no obvious cause."),
        ],
        questions=[
            dict(question="Confirm where the vault is hosted and whether anything is billed for it.",
                 assignedTo="Jordan Castillo"),
        ],
    ),
    dict(
        name="Chapter bank account", category=Cat.FINANCE,
        accessModel=Access.INDIVIDUAL,
        payer=Payer.FREE,
        blurb=(
            "The chapter's money: dues passed back from national, fundraising, and everything the "
            "chapter spends. Signing authority rather than a login is the thing that matters here."
        ),
        annualCost=None,
        costNote=(
            "No subscription. Any account fee is a bank charge rather than a software bill, and "
            "the amount still needs confirming with the Treasurer."
        ),
        howToGetAccess=(
            "You do not request this one. Who may sign for the chapter is a chapter decision, not "
            "something a committee hands out, so the route is a vote and then paperwork at the "
            "bank."
        ),
        siteUrl="",
        accessRequestUrl="",
        stewardName="Treasurer",
        delegationTier=Tier.RED,
        # A review date old enough to be stale, so the flag has a live example.
        # Picked deliberately on the row where going unreviewed is most obviously
        # a problem rather than on a harmless one.
        lastReviewed=datetime.date(2026, 2, 20),
        revocationNote=(
            "Removing a signer is paperwork at the bank, not a setting somebody can change. It "
            "takes days rather than minutes, it cannot be done quietly, and until it completes the "
            "former signer can still sign."
        ),
        continuityNote=(
            "Two signers, so the chapter is not one person away from being locked out of its own "
            "money. Neither of them can add a third without a chapter decision."
        ),
        holders=[
            # The "Owns it" summary with nothing beside it - the case the holder
            # model's own docstring names (a billing contact who owns the account
            # and cannot touch membership) and which no other row in the register
            # produces. Every other privileged holder here can also grant, so
            # without this row the power summary has three of its four states.
            dict(personName="Teodora Marchetti", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Primary signer", canGrantAccess=False, ownsAccount=True,
                 note="Signing authority on the account. Cannot add another signer without a "
                      "chapter decision, which is why this reads as owning it while being unable "
                      "to hand it out."),
            dict(personName="Wendell Achebe", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Second signer", canGrantAccess=False, ownsAccount=False,
                 note="Second signer, so no single absence stops the chapter paying for anything."),
        ],
        credentials=[
            dict(label="Per-person online banking login", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="One login per signer, in their own name. Nothing shared, and nothing that "
                      "could be shared without the bank noticing."),
            dict(label="Second-factor device", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="Bound to one signer's own phone. Not vaulted, because vaulting it would "
                      "defeat the point of it."),
        ],
        questions=[
            dict(question="Check that the signers recorded here still match what the bank has on file.",
                 assignedTo="Treasurer"),
        ],
    ),
    dict(
        name="Chapter social accounts", category=Cat.SOCIAL,
        # The row that demonstrates not knowing. Every field below is either a
        # real answer or an honest "not known", and the point of it is that the
        # registry has to be able to hold a row like this without it looking
        # half-filled-in - because a register that can only express settled facts
        # gets the unsettled ones left out of it entirely.
        accessModel=Access.UNCONFIRMED,
        payer=Payer.UNCONFIRMED,
        blurb=(
            "The chapter's public accounts on the main social platforms. Announcements, event "
            "promotion, and most of what somebody sees of the chapter before they ever turn up."
        ),
        annualCost=None,
        costNote="Nothing recorded. Posting is free; whether anything was ever paid for promotion is not known.",
        howToGetAccess=(
            "Ask in Slack and say what you need to post. Nobody has written down how getting into "
            "these actually works, so expect the answer to be a person rather than a process."
        ),
        siteUrl="",
        accessRequestUrl="",
        # Blank on purpose, and it is the only blank steward in the register. It
        # is NOT the same statement as the calendar's "Nobody - owner unknown":
        # there, somebody looked and found no owner. Here nobody has looked yet,
        # and an empty field is what that looks like.
        stewardName="",
        delegationTier=Tier.YELLOW,
        # Never reviewed, so the Stale flag has its other example - the one where
        # there is no date at all rather than an old one. The two read
        # differently to somebody deciding what to pick up, which is why both are
        # here.
        lastReviewed=None,
        reviewedBy="",
        revocationNote=(
            "Not known, because how people get in is not known. If any of these turns out to be a "
            "shared password, removing one person means changing it for everybody who posts."
        ),
        continuityNote=(
            "Not known. Whether a second person can get into each account is the first thing to "
            "find out, because the answer being no is how a chapter loses its own name to an "
            "account nobody can log into."
        ),
        holders=[
            # accessLevel left blank so the roster renders "Role not recorded" -
            # a real state, and a different claim from recording a guessed role.
            dict(personName="Odalys Ferrer", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
                 accessLevel="", canGrantAccess=False, ownsAccount=False,
                 note="Posts to at least one of the accounts. How they get in, and what else they "
                      "can do once in, is not confirmed."),
        ],
        # No credential rows, because nobody has established what the credentials
        # are. Inventing one would answer the exact question this row exists to
        # keep open.
        credentials=[],
        questions=[
            "For each account, find out whether people sign in with their own login or a shared password.",
            dict(question="Confirm who can currently post to each account.", assignedTo="Odalys Ferrer"),
        ],
    ),
    dict(
        name="GitHub organisation", category=Cat.INFRASTRUCTURE,
        accessModel=Access.INDIVIDUAL,
        payer=Payer.FREE,
        blurb=(
            "Where the chapter's code lives, including this site. Public repositories plus the "
            "deploy history, which is what makes Echo rebuildable by somebody who was not there "
            "when it was built."
        ),
        annualCost=None,
        costNote="Free plan. There is no bill for a public organisation of this size.",
        howToGetAccess=(
            "Ask for an invite and say which repository you want to work on. You sign in with your "
            "own GitHub account, so there is nothing shared to be handed out and nothing to change "
            "when you leave."
        ),
        siteUrl="https://github.com/Austin-DSA",
        accessRequestUrl="",
        steward="jordan.castillo",
        stewardName="",
        delegationTier=Tier.YELLOW,
        requestable=True,
        revocationNote=(
            "Removing somebody from the organisation is a single action and costs nobody else "
            "anything. What it does not undo is anything they already merged, which is the reason "
            "this is Yellow rather than Green."
        ),
        continuityNote=(
            "More than one person holds owner rights, so the organisation survives any single "
            "account being lost. Anything that currently lives on one person's personal account "
            "should be moved in here, because that is the part this does not cover."
        ),
        holders=[
            dict(personName="Jordan Castillo", user="jordan.castillo",
                 how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Owner", canGrantAccess=True, ownsAccount=True,
                 note="Organisation owner. Can invite and remove members."),
            dict(personName="Sam Nguyen", user="sam.nguyen",
                 how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 accessLevel="Owner", canGrantAccess=True, ownsAccount=True,
                 note="Second organisation owner, so ownership is not one account deep."),
            dict(personName="Kelly O'Sullivan", user="kelly.osullivan",
                 how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
                 accessLevel="Repo maintainer", canGrantAccess=False, ownsAccount=False,
                 note="Maintainer on one repository. Not yet checked against the live organisation."),
        ],
        credentials=[
            dict(label="Per-person GitHub account", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="Everybody uses their own account. The chapter holds no password for this."),
            dict(label="Deploy key", kind=CredKind.SERVICE_ACCOUNT_KEY,
                 vaultCollection="github", status=CredStatus.LIVE,
                 addedAt=datetime.date(2025, 11, 1), lastRotated=datetime.date(2026, 5, 30),
                 note="Used by the deploy scripts to check the code out on the server."),
            dict(label="Legacy deploy key", kind=CredKind.SERVICE_ACCOUNT_KEY,
                 vaultCollection="github", status=CredStatus.RETIRE_CANDIDATE,
                 addedAt=datetime.date(2023, 5, 20),
                 note="Predates the key above and nothing is known to use it. Flagged rather than "
                      "removed until somebody traces it."),
        ],
        questions=[
            dict(question="Trace what the legacy deploy key is used by, then remove it.",
                 assignedTo="Sam Nguyen"),
        ],
    ),
]

DEPENDENCY_EDGES = [
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.SIGN_IN, toName="Slack", note=""),
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.RUNS_ON, toName="DigitalOcean", note=""),
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.RUNS_ON, toName="Cloudflare", note="DNS and domain only"),
    dict(fromName="Echo (this site)", kind=DepKind.RUNS_ON, toName="Cloudflare", note=""),
    # The three things Echo is the front door for. Read backwards on Echo's page
    # this is the useful half - it names what publishing an event in Echo
    # actually reaches, which is the whole argument for Echo existing: one form
    # creates the calendar entry, the Zoom meeting, and the Action Network event.
    #
    # The calendar is the case REACHED_THROUGH was invented for: nobody is
    # granted access to it at all.
    #
    # Zoom and Action Network are the softer case, and the notes carry why. Both
    # DO have a direct route for a different audience - a meeting host gets the
    # shared Zoom login, an organiser gets their own Action Network account - so
    # the edge is true for the person attending or RSVPing, not for the person
    # running the thing. That is what the note slot is for, and it is also why
    # the wording on both surfaces no longer claims access is impossible
    # directly: see the templates.
    dict(fromName="Google Calendar", kind=DepKind.REACHED_THROUGH, toName="Echo (this site)",
         note="publish an event in Echo and it appears here"),
    dict(fromName="Zoom", kind=DepKind.REACHED_THROUGH, toName="Echo (this site)",
         note="attendees get the join link from the Echo-published event; hosts get the shared login instead"),
    dict(fromName="Action Network", kind=DepKind.REACHED_THROUGH, toName="Echo (this site)",
         note="Echo creates the event here, which is what people RSVP to; organisers get their own login instead"),
]

CHAPTER_WIDE_QUESTIONS = [
    "Confirm the payer and the annual amount for every row with an unconfirmed cost, in time for the next budget cycle.",
    "Agree how often this list gets reviewed, so it stays true rather than drifting.",
    dict(question="Decide who reviews access requests for each tool once requests can be made in "
                  "Echo itself, rather than leaving it to whoever happens to answer in Slack.",
         assignedTo="IT Sub-Committee"),
    # The workbench is only half a workbench if nothing in it is ever finished.
    # One closed question with its answer attached shows what resolving actually
    # produces - a decision somebody can be pointed at later, not a disappeared row.
    dict(question="Agree a single place where shared passwords live, so no tool has two "
                  "authoritative copies of the same secret.",
         assignedTo="Jordan Castillo", resolvedAt=ct(2026, 7, 28, 18),
         resolution="Agreed: the chapter password vault is the only authoritative copy, and any "
                    "tool with a shared login gets a collection there. Anything found outside it "
                    "gets moved in rather than noted."),
]

# Retired questions, removed by exact text.
#
# Questions are create-only by design - assigning or resolving one in the app has
# to survive a redeploy - which means they are the ONE child table with no sweep
# behind it. Holders and credentials get pruned to match the spec further down,
# so editing one of those is self-healing; editing a question's text just leaves
# the old row on the box forever, and the register accumulates generations.
#
# That is not only clutter. Both of the entries below were found live on
# 2026-08-13 and the first one is the reason this list exists: it asks who holds
# EDIT RIGHTS on the calendar, which is the exact claim the calendar row was
# corrected to remove. Its card now says nobody is granted anything and nobody
# signs in, and directly underneath sat an open question presupposing the
# opposite - the same defect the stale-holder sweep was written to stop, arriving
# through the one door that has no sweep.
#
# So: when a question's text is CHANGED rather than added, retire the old text
# here in the same edit. Nothing else will.
RETIRED_QUESTIONS = [
    # Generic placeholder from the first demo boot.
    "(Demo placeholder)",
    # Contradicts the corrected Google Calendar row (see above).
    "Confirm which chapter addresses currently hold edit rights on the calendar.",
    # Answered by the Slack card itself, which now documents per-person accounts.
    "How does account sharing work?",
]
ResourceQuestion.objects.filter(question__startswith=RETIRED_QUESTIONS[0]).delete()
_retired = ResourceQuestion.objects.filter(question__in=RETIRED_QUESTIONS[1:])
for row in _retired:
    report.append(f"  retired stale question: {row.resource.name if row.resource_id else 'chapter-wide'}"
                  f" / {row.question[:60]}")
_retired.delete()

chapterResourcesByName = {}
credentialCount = holderCount = questionsCreated = 0
for spec in CHAPTER_RESOURCES:
    # Mirrors ChapterResource.clean(). update_or_create writes through the
    # manager and never calls full_clean, so without this the seed can put a row
    # on the box that the edit form would refuse to save - a demo whose data is
    # illegal in its own app. Raising is right here, unlike the coverage check at
    # the bottom: this is a contradiction in the spec, it is caught before any
    # write, and the fix is a one-line edit rather than a judgement call.
    requestable = spec.get("requestable", False)
    if requestable and spec.get("steward") is None:
        raise SystemExit(
            f"railway-seed.py refused to run: {spec['name']} is requestable with no steward. "
            "A request button with no reviewer is a dead letter - give it a steward or set "
            "requestable=False."
        )
    if requestable and spec["delegationTier"] not in ChapterResource.REQUESTABLE_TIERS:
        raise SystemExit(
            f"railway-seed.py refused to run: {spec['name']} is requestable at a tier that "
            "forbids it. Classify it Green or Yellow, or leave requests closed."
        )

    resource, _ = ChapterResource.objects.update_or_create(
        name=spec["name"],
        defaults=dict(
            category=spec["category"], accessModel=spec["accessModel"], payer=spec["payer"],
            blurb=spec["blurb"], annualCost=spec["annualCost"], costNote=spec["costNote"],
            howToGetAccess=spec["howToGetAccess"], stewardName=spec["stewardName"],
            # Subscripted, not .get(), so adding a resource without deciding
            # these two fails loudly here rather than shipping a card with no
            # route on it - which is the defect this pair was added to fix.
            siteUrl=spec["siteUrl"], accessRequestUrl=spec["accessRequestUrl"],
            # Stewards are declared by USERNAME rather than by passing a User
            # object, so the spec list stays a plain data literal that can be read
            # without knowing what is in scope above it. Subscripting `users`
            # means a typo'd username is a KeyError here, not a silently
            # steward-less row.
            steward=users[spec["steward"]] if spec.get("steward") else None,
            requestable=requestable,
            lastReviewed=_rowValue(spec, "lastReviewed", CHAPTER_TOOLS_LAST_REVIEWED),
            reviewedBy=_rowValue(spec, "reviewedBy", CHAPTER_TOOLS_REVIEWED_BY),
            delegationTier=spec["delegationTier"], revocationNote=spec["revocationNote"],
            continuityNote=spec["continuityNote"],
        ),
    )
    chapterResourcesByName[spec["name"]] = resource

    # accessLevel, the two power booleans and the two credential dates are read
    # with .get() rather than [], so a spec that does not state them lands on the
    # model's own honest default (blank / False / not recorded) instead of this
    # loader inventing one. That is the same rule the rest of this block follows:
    # a demo row may be invented, but it must not claim a fact nobody decided.
    for holder in spec["holders"]:
        ResourceHolder.objects.update_or_create(
            resource=resource, personName=holder["personName"],
            # Keyed on personName and NOT on user, deliberately: personName is
            # required on every row and user is set on only a few, so keying on
            # the optional half would make every text-only holder collide with
            # every other one on the same resource. personName stays the identity
            # even when a user is attached, which is also what keeps the
            # stale-holder sweep below able to name what it removed.
            defaults=dict(how=holder["how"], confirmed=holder["confirmed"], note=holder["note"],
                          user=users[holder["user"]] if holder.get("user") else None,
                          accessLevel=holder.get("accessLevel", ""),
                          canGrantAccess=holder.get("canGrantAccess", False),
                          ownsAccount=holder.get("ownsAccount", False)),
        )
        holderCount += 1
    for credential in spec["credentials"]:
        ResourceCredential.objects.update_or_create(
            resource=resource, label=credential["label"],
            defaults=dict(kind=credential["kind"], vaultCollection=credential["vaultCollection"],
                          status=credential["status"], note=credential["note"],
                          addedAt=credential.get("addedAt"),
                          lastRotated=credential.get("lastRotated")),
        )
        credentialCount += 1

    # Drop child rows this spec no longer lists. Upsert-by-name only ever adds
    # and rewrites, so renaming or removing a holder/credential used to strand
    # the old row forever: renaming a Zoom credential left the previous label
    # behind, and the card showed both. Worse, when a resource's access story is
    # CORRECTED - the calendar going from "individual edit rights" to "nobody is
    # granted anything" - the stale rows keep asserting the exact claim the
    # correction removed, directly under the corrected prose.
    #
    # Deleting is safe here in a way it would not be in the real registry: this
    # script refuses to run outside DEMO_MODE, and on a demo box the spec is by
    # definition the whole truth. The real loader (seed_chapter_tools) does NOT
    # do this - it has to assume a human may have added rows it knows nothing
    # about. Each deletion is named below rather than counted, because a silent
    # delete is what made the first orphan take a live audit to notice.
    staleHolders = resource.holders.exclude(
        personName__in=[holder["personName"] for holder in spec["holders"]],
    )
    staleCredentials = resource.credentials.exclude(
        label__in=[credential["label"] for credential in spec["credentials"]],
    )
    for row in staleHolders:
        report.append(f"  removed stale holder: {resource.name} / {row.personName}")
    for row in staleCredentials:
        report.append(f"  removed stale credential: {resource.name} / {row.label}")
    staleHolders.delete()
    staleCredentials.delete()
    for question in spec["questions"]:
        questionsCreated += _seedQuestion(resource, question)

for question in CHAPTER_WIDE_QUESTIONS:
    questionsCreated += _seedQuestion(None, question)

dependencyCount = 0
for edge in DEPENDENCY_EDGES:
    ResourceDependency.objects.update_or_create(
        resource=chapterResourcesByName[edge["fromName"]],
        dependsOn=chapterResourcesByName[edge["toName"]],
        kind=edge["kind"],
        defaults=dict(note=edge["note"]),
    )
    dependencyCount += 1

# Coverage check. This register's job on the demo box is to show every state the
# registry can represent, and the way that quietly rots is somebody editing one
# row and taking the last example of a state away with it. Nothing catches that
# otherwise: the seed is not under test, and a missing state looks exactly like a
# state that renders as nothing.
#
# It WARNS rather than raising, which is the opposite of the requestable guard
# above, and the difference is what a failure would cost. That guard catches
# illegal data before any write and the box is fine either way. This one runs
# after the writes, inside the entrypoint - so raising here would fail the boot
# and 502 the box over a demo row being less complete than intended. A line in
# the boot log is the proportionate answer.
#
# Read from the DB rather than from the spec lists on purpose: the rotation
# states are computed against today's date, so the only way to know the box
# actually shows "overdue" is to ask a credential that is on it.
_gaps = []
for label, choices, seen in (
    ("category", ChapterResource.CATEGORY_CHOICES,
     set(ChapterResource.objects.values_list("category", flat=True))),
    ("access model", ChapterResource.ACCESS_MODEL_CHOICES,
     set(ChapterResource.objects.values_list("accessModel", flat=True))),
    ("payer", ChapterResource.PAYER_CHOICES,
     set(ChapterResource.objects.values_list("payer", flat=True))),
    ("delegation tier", ChapterResource.DELEGATION_TIER_CHOICES,
     set(ChapterResource.objects.values_list("delegationTier", flat=True))),
    ("credential kind", ResourceCredential.KIND_CHOICES,
     set(ResourceCredential.objects.values_list("kind", flat=True))),
    ("credential status", ResourceCredential.STATUS_CHOICES,
     set(ResourceCredential.objects.values_list("status", flat=True))),
    ("holder route", ResourceHolder.HOW_CHOICES,
     set(ResourceHolder.objects.values_list("how", flat=True))),
    ("dependency kind", ResourceDependency.KIND_CHOICES,
     set(ResourceDependency.objects.values_list("kind", flat=True))),
):
    absent = [name for value, name in choices if value not in seen]
    if absent:
        _gaps.append(f"{label} not demonstrated: {', '.join(absent)}")

# The derived states, which no choices tuple covers. Each of these is a distinct
# thing the pages render differently, and each was unrepresented before
# 2026-08-13 - so they are the states most likely to go missing again.
_rotationStates = {c.getRotationStatus() for c in ResourceCredential.objects.all()}
_missingRotation = sorted(
    {"not-applicable", "retired", "unknown", "overdue", "never-rotated", "ok"} - _rotationStates
)
if _missingRotation:
    _gaps.append(f"credential rotation state not demonstrated: {', '.join(_missingRotation)}")

_powerSummaries = {h.getPowerSummary() for h in ResourceHolder.objects.all()}
if len(_powerSummaries) < 4:
    _gaps.append(f"holder power summary: only {len(_powerSummaries)} of 4 shapes present")

for label, condition in (
    ("a stale row with an old review date",
     ChapterResource.objects.filter(lastReviewed__isnull=False).exclude(
         lastReviewed__gte=datetime.date.today() - datetime.timedelta(
             days=ChapterResource.STALE_AFTER_DAYS)).exists()),
    ("a row never reviewed at all", ChapterResource.objects.filter(lastReviewed__isnull=True).exists()),
    ("a requestable row", ChapterResource.objects.filter(requestable=True).exists()),
    ("a steward held as an Echo account", ChapterResource.objects.filter(steward__isnull=False).exists()),
    ("a holder linked to an Echo account", ResourceHolder.objects.filter(user__isnull=False).exists()),
    ("a holder with no recorded role", ResourceHolder.objects.filter(accessLevel="").exists()),
    ("a resource with no holders recorded", ChapterResource.objects.filter(holders__isnull=True).exists()),
    ("an assigned open question", ResourceQuestion.objects.exclude(assignedTo="").exists()),
    ("a resolved question", ResourceQuestion.objects.filter(resolvedAt__isnull=False).exists()),
    ("a chapter-wide question", ResourceQuestion.objects.filter(resource__isnull=True).exists()),
):
    if not condition:
        _gaps.append(f"missing: {label}")

if _gaps:
    report.append("chapter resources - DEMO COVERAGE GAPS (%d):" % len(_gaps))
    report.extend(f"    {gap}" for gap in _gaps)
else:
    report.append("chapter resources: every representable state has a live example")

report.append(f"chapter resources: now {ChapterResource.objects.count()} (all fields populated)")
report.append(f"chapter resource holders: {holderCount} seeded (now {ResourceHolder.objects.count()})")
report.append(f"chapter resource credentials: {credentialCount} seeded (now {ResourceCredential.objects.count()})")
report.append(f"chapter resource questions: +{questionsCreated} (now {ResourceQuestion.objects.count()})")
report.append(f"chapter resource dependencies: {dependencyCount} seeded (now {ResourceDependency.objects.count()})")

print("\n".join("  " + line for line in report))
