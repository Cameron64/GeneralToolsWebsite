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
                              ["viewChapterToolAudit"]),
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
# Seven real chapter systems, seeded exactly as approved for the demo box - no
# additional services beyond the two infrastructure rows added 2026-08-11
# (Cloudflare, DigitalOcean) plus their dependency edges on the other five.
# Every field is populated so the pages can be pressure tested with nothing
# rendering as a dash.
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
            dict(personName="Cam D.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=False,
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
        accessModel=Access.INDIVIDUAL, payer=Payer.CHAPTER,
        blurb="The one shared chapter calendar. Events published through Echo land here automatically.",
        annualCost=None,
        costNote="No separate bill. Included in the chapter's Google Workspace subscription.",
        howToGetAccess=(
            "Ask the IT Sub-Committee in Slack and tell them which email address you want the "
            "rights on. You get edit rights on the shared calendar under your own account, so "
            "there is no password to hand over and nothing to share."
        ),
        siteUrl="https://calendar.google.com",
        accessRequestUrl="",
        stewardName="IT Sub-Committee",
        delegationTier=Tier.YELLOW,
        revocationNote="Edit rights are granted per address, so removing one person is a single change that leaves everyone else alone.",
        continuityNote="Sits on top of the Workspace account, so whoever holds Workspace super-admin can always restore calendar access.",
        holders=[
            dict(personName="Marisol T.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
                 note="Holds edit rights on the shared calendar under her own chapter address."),
        ],
        credentials=[
            dict(label="Per-address edit rights", kind=CredKind.INDIVIDUAL_LOGIN,
                 vaultCollection="", status=CredStatus.LIVE,
                 note="Granted to a person's own chapter address, not to a shared login."),
        ],
        questions=["Confirm which chapter addresses currently hold edit rights on the calendar."],
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
            dict(personName="Cam D.", how=HolderHow.INDIVIDUAL_LOGIN, confirmed=True,
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
            dict(personName="Devon K.", how=HolderHow.VAULT_COLLECTION, confirmed=True,
                 note="In the Cloudflare vault collection. Can manage DNS records and domain settings."),
            dict(personName="Priya R.", how=HolderHow.VAULT_COLLECTION, confirmed=False,
                 note="Added to the vault collection. Access not yet confirmed against the live account."),
        ],
        credentials=[
            dict(label="Shared account login", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="cloudflare", status=CredStatus.LIVE,
                 note="One password, held by everyone who manages DNS or the domain."),
            dict(label="Shared 2FA token", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="cloudflare", status=CredStatus.LIVE,
                 note="Stored next to the password so it stays usable by everyone on the login. Counted separately because it rotates separately."),
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
            dict(personName="Theo A.", how=HolderHow.VAULT_COLLECTION, confirmed=True,
                 note="In the DigitalOcean vault collection. Can manage the droplet running the wiki."),
            dict(personName="Nadia S.", how=HolderHow.VAULT_COLLECTION, confirmed=False,
                 note="Added to the vault collection. Access not yet confirmed against the live account."),
        ],
        credentials=[
            dict(label="Shared account login", kind=CredKind.VAULT_SHARED_LOGIN,
                 vaultCollection="digitalocean", status=CredStatus.LIVE,
                 note="One password, held by everyone who manages the droplet."),
            dict(label="Shared 2FA token", kind=CredKind.TWO_FACTOR_TOKEN,
                 vaultCollection="digitalocean", status=CredStatus.LIVE,
                 note="Stored next to the password so it stays usable by everyone on the login. Counted separately because it rotates separately."),
        ],
        questions=["Confirm who is currently in the DigitalOcean vault collection."],
    ),
]

DEPENDENCY_EDGES = [
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.SIGN_IN, toName="Slack", note=""),
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.RUNS_ON, toName="DigitalOcean", note=""),
    dict(fromName="Chapter wiki (Outline)", kind=DepKind.RUNS_ON, toName="Cloudflare", note="DNS and domain only"),
    dict(fromName="Echo (this site)", kind=DepKind.RUNS_ON, toName="Cloudflare", note=""),
]

CHAPTER_WIDE_QUESTIONS = [
    "Confirm the payer and the annual amount for every row with an unconfirmed cost, in time for the next budget cycle.",
    "Agree how often this list gets reviewed, so it stays true rather than drifting.",
]

# Retired: an earlier boot seeded a single placeholder question. Remove it by its
# exact text so the demo box does not accumulate two generations of examples.
ResourceQuestion.objects.filter(question__startswith="(Demo placeholder)").delete()

chapterResourcesByName = {}
credentialCount = holderCount = questionsCreated = 0
for spec in CHAPTER_RESOURCES:
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
            requestable=False,  # the request button is M2 and is not gated yet
            lastReviewed=CHAPTER_TOOLS_LAST_REVIEWED, reviewedBy=CHAPTER_TOOLS_REVIEWED_BY,
            delegationTier=spec["delegationTier"], revocationNote=spec["revocationNote"],
            continuityNote=spec["continuityNote"],
        ),
    )
    chapterResourcesByName[spec["name"]] = resource

    for holder in spec["holders"]:
        ResourceHolder.objects.update_or_create(
            resource=resource, personName=holder["personName"],
            defaults=dict(how=holder["how"], confirmed=holder["confirmed"], note=holder["note"]),
        )
        holderCount += 1
    for credential in spec["credentials"]:
        ResourceCredential.objects.update_or_create(
            resource=resource, label=credential["label"],
            defaults=dict(kind=credential["kind"], vaultCollection=credential["vaultCollection"],
                          status=credential["status"], note=credential["note"]),
        )
        credentialCount += 1
    for question in spec["questions"]:
        _, wasCreated = ResourceQuestion.objects.get_or_create(resource=resource, question=question)
        questionsCreated += 1 if wasCreated else 0

for question in CHAPTER_WIDE_QUESTIONS:
    _, wasCreated = ResourceQuestion.objects.get_or_create(resource=None, question=question)
    questionsCreated += 1 if wasCreated else 0

dependencyCount = 0
for edge in DEPENDENCY_EDGES:
    ResourceDependency.objects.update_or_create(
        resource=chapterResourcesByName[edge["fromName"]],
        dependsOn=chapterResourcesByName[edge["toName"]],
        kind=edge["kind"],
        defaults=dict(note=edge["note"]),
    )
    dependencyCount += 1

report.append(f"chapter resources: now {ChapterResource.objects.count()} (all fields populated)")
report.append(f"chapter resource holders: {holderCount} seeded (now {ResourceHolder.objects.count()})")
report.append(f"chapter resource credentials: {credentialCount} seeded (now {ResourceCredential.objects.count()})")
report.append(f"chapter resource questions: +{questionsCreated} (now {ResourceQuestion.objects.count()})")
report.append(f"chapter resource dependencies: {dependencyCount} seeded (now {ResourceDependency.objects.count()})")

print("\n".join("  " + line for line in report))
