# Demo seed for the auth-self-service branch demo DB. Idempotent: each section
# checks before inserting, so reruns are safe. Run with:
#   manage.py shell --command "exec(open(r'<this file>', encoding='utf-8').read())"
import datetime
import random
from zoneinfo import ZoneInfo

from django.contrib.auth.models import Group, Permission
from django.utils import timezone as djtz

from tools.models import (
    User, EventOwners, PostedEvents, DelegatedEvents, AccessRequests,
    LinkTree, LinkTreeItem, QRCode, LinkEvent,
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
}
for groupName, (names, codenames) in GROUPS.items():
    group, wasCreated = Group.objects.get_or_create(name=groupName)
    if wasCreated:
        group.permissions.add(*Permission.objects.filter(
            content_type__app_label="tools",
            content_type__model="permissionrights",
            codename__in=codenames,
        ))
    group.user_set.add(*[users[n] for n in names])
report.append("group rosters topped up")

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

print("\n".join("  " + line for line in report))
