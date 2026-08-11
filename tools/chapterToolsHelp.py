"""In-page definitions for the Chapter Tools vocabulary.

Why this module exists at all. The registry uses five words that look ordinary
and are not: steward, review, delegation tier, requestable, and access level. A
reader who guesses at any of them fills the row in wrong, and the two ladders
(tier and access level) were previously rendered as a bare colour name or a bare
noun with nothing behind them.

Why the text lives here rather than in the templates. Each term appears on at
least two surfaces - the edit form and the reader-facing detail page - and the
form and the page had already drifted apart once. Prose duplicated across
templates cannot be kept honest, so every surface renders through the `explain`
template tag and reads from this one dict.

Why it is not `help_text` on the model fields. It is *also* help_text where a
form field exists (that renders automatically through formRow.html, and the two
do not contradict: help_text tells an editor what to type, these entries tell
any reader what the word means). But three of these terms have no single field
to hang off - a review is two fields, the tier ladder is a set of values, and
what members may request is a policy spanning three fields - and help_text is
invisible to somebody who is only reading.

The ladders deliberately do NOT restate their rungs here. `getEntry` pulls those
from the model classmethods, so the tier wording has exactly one home
(ChapterResource.DELEGATION_TIER_EXPLANATIONS) and this module cannot contradict
it.
"""
from .models import ChapterResource, ResourceCredential, ResourceHolder

# Slug -> entry. `title` is the popover heading, `body` the definition, and the
# optional `bullets` a short list where the definition is genuinely a set of
# conditions rather than a sentence.
GLOSSARY = {
    "steward": {
        "title": "What a steward is",
        "body": (
            "The one person answerable for this tool. They keep this page true, "
            "they are who to ask when access here is unclear, and they review "
            "requests for it. A steward is not the only person with access, and "
            "does not have to be on the IT Sub-Committee. The point is that one "
            "name is on the hook, so the question never falls to “somebody”."
        ),
    },
    "review": {
        "title": "What a review means here",
        "body": (
            "Somebody checked this row against reality and put their name on it. "
            "A review is a check, not an edit: the holder list is still right, the "
            "people on it can still get in, and the cost and steward are current. "
            f"After {ChapterResource.STALE_AFTER_DAYS} days a row is marked stale. "
            "Stale means nobody has looked recently. It does not mean the row is "
            "wrong, and it is not a reason to distrust it - it is a reason to look."
        ),
    },
    "requestable": {
        "title": "What members can request through Echo",
        "body": (
            "Whether a member can ask for this inside Echo instead of finding the "
            "right person. Open it only when all three of these are true:"
        ),
        "bullets": [
            "There is a steward, so the request reaches a named reviewer.",
            "The delegation tier is Green or Yellow. Red and unclassified tools "
            "cannot be opened for requests.",
            "Holding it gives somebody no power over other members - they cannot "
            "spend chapter money, read member data, or remove anybody's access.",
        ],
        "closing": (
            "Leave it closed when access is a shared password that is expensive "
            "to change, and when nobody is ever granted the thing directly. A "
            "calendar that Echo writes to is reached through Echo, so the thing "
            "worth requesting is Echo."
        ),
    },
    "delegation-tier": {
        "title": "What the delegation tiers mean",
        "body": (
            "How freely this tool may be handed to somebody else. The tier is "
            "about the cost of being wrong, not about how important the tool is."
        ),
        "ladder": "delegation-tier",
    },
    "access-level": {
        "title": "The kinds of access somebody can hold",
        "body": (
            "How much a person can do once they are in, which is a different "
            "question from which door they came through. Two people can both "
            "“have Slack” while one of them could delete it."
        ),
        "ladder": "access-level",
    },
    "credential-age": {
        "title": "Credential age and rotation",
        "body": (
            "Age is counted from the day the secret was last actually changed, or "
            "from the day it was added when it has never been changed. A "
            "credential with no dates at all reads as “not recorded” rather "
            "than as new, because not knowing and knowing it is old are "
            "different problems. After "
            f"{ResourceCredential.ROTATE_AFTER_DAYS} days without a change it "
            "is flagged as overdue."
        ),
    },
    "holders": {
        "title": "What this list is and is not",
        "body": (
            "Who can get into this today, as far as the chapter has checked. "
            "“Not confirmed yet” means nobody has verified that person's "
            "access, not that they have lost it. An empty list is an open job, "
            "not evidence that nobody has access."
        ),
    },
}


def getEntry(slug: str) -> dict | None:
    """The entry for `slug`, with a ladder attached when it has one.

    Returns None for an unknown slug so the template tag can render nothing
    rather than raising - a typo in a template should not 500 a page whose real
    content is fine. The tag's own test asserts every slug used in a template
    resolves, which is where a typo is meant to be caught.
    """
    entry = GLOSSARY.get(slug)
    if entry is None:
        return None

    resolved = dict(entry)
    ladder = resolved.pop("ladder", None)
    if ladder == "delegation-tier":
        resolved["items"] = ChapterResource.getDelegationTierLegend()
    elif ladder == "access-level":
        resolved["items"] = ResourceHolder.getAccessLevelLegend()
    return resolved
