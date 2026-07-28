"""Member-facing audience tags for published events.

MEC asked for a visual indicator on the calendar so members can tell at a glance
which events suit newcomers, families, or are purely social. The indicator has to
live in the event **title**, not in a calendar colour: the chapter's public
Google Calendar feed carries no per-event colour or category, so a colour set in
Google Calendar reaches neither the styledcalendar embed on austindsa.org nor
anyone's subscribed calendar. The title is the only field present on every
surface members actually look at (the website embed, the Google Calendar
subscription, the iCal/Outlook/Apple download, and the Action Network event
page), and EventAutomationDriver already sends one title string to all three
publish targets - so composing it once marks every surface identically.

Emoji in a title used to fail the publish outright (ChromeDriver rejects non-BMP
characters through send_keys); see Utils.typeTextIntoElement in
ActionNetworkAutomation for the JS-set path that makes this possible at all.

EVENT_TAGS is the whole vocabulary - a closed set, edited here and nowhere else.
Keep it short: a shelf of a dozen icons stops meaning anything at a glance.

Two glyph choices worth knowing, both trivially changeable:
- The family tag uses U+1F46A (a single code point) rather than the
  man-woman-girl ZWJ sequence, which renders inconsistently across Android,
  Outlook and Apple and degrades to three separate human figures where the
  sequence is unsupported. A visual indicator has to survive that.
- The social tag's beer glyph implies alcohol. If MEC would rather not, the
  neutral swap is a party popper.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class EventTag:
    # `key` is the stable identifier stored in the DB and the PublishJob
    # payload. Renaming a label or swapping an emoji is safe; changing a key
    # orphans existing rows (tagsFor drops keys it does not recognize).
    key: str
    emoji: str
    label: str
    helpText: str


EVENT_TAGS: tuple[EventTag, ...] = (
    EventTag(
        key="newcomer",
        emoji="\N{ROSE}",
        label="Good for new members",
        helpText="Someone who has never been to a chapter event can show up to this one and follow along.",
    ),
    EventTag(
        key="family",
        emoji="\N{FAMILY}",
        label="Kid and family friendly",
        helpText="Children are welcome and the space and timing suit them.",
    ),
    EventTag(
        key="social",
        emoji="\N{CLINKING BEER MUGS}",
        label="Social",
        helpText="Primarily social rather than a meeting or a work session.",
    ),
)

TAGS_BY_KEY: dict[str, EventTag] = {tag.key: tag for tag in EVENT_TAGS}

# Separates entries in the description legend. A middle dot rather than a comma
# so it still reads as a legend when the emoji are stripped (see the Redactor
# fallback in ActionNetworkAutomation.Utils.typeTextIntoElement).
LEGEND_SEPARATOR = "  \N{MIDDLE DOT}  "

# Emoji presentation selector. Not part of any tag's glyph, but it can arrive
# attached to one in a title the publisher typed - see composeTitle.
VARIATION_SELECTOR = "\N{VARIATION SELECTOR-16}"


def formFieldName(key: str) -> str:
    """The NewEventForm field name for a tag. The fields are added dynamically
    from EVENT_TAGS, so this prefix is the only coupling between the vocabulary
    and the form."""
    return f"tag_{key}"


def tagsFor(keys) -> list[EventTag]:
    """Resolve stored keys to tags, in EVENT_TAGS order rather than the order
    they were stored or submitted, so the icons always appear in the same
    sequence on every event.

    Unknown keys are dropped silently: retiring a tag from EVENT_TAGS must not
    break the display of historic events that still reference it.
    """
    if not keys:
        return []
    keySet = set(keys)
    return [tag for tag in EVENT_TAGS if tag.key in keySet]


def composeTitle(title: str, keys) -> str:
    """Prefix the selected tags' emoji onto an event title.

    With no tags selected this is a pure no-op - most events carry no tags, so
    that path must be byte-identical to the untagged behaviour, and a publisher
    who hand-typed an emoji into their title keeps it.

    When tags ARE selected, a leading run of the *selected* tags' own emoji is
    stripped first, so ticking "good for new members" on a title the publisher
    already prefixed with a rose yields one rose, not two. Emoji belonging to
    tags that were NOT selected are deliberately left alone: an event called
    "Rose Sale" tagged family should become "<family> Rose Sale", not have the
    publisher's chosen glyph deleted. Never silently edit a title beyond the
    tags we were asked to manage.
    """
    tags = tagsFor(keys)
    if not tags:
        return title
    selectedEmoji = {tag.emoji for tag in tags}
    stripped = title.lstrip()
    while True:
        for emoji in selectedEmoji:
            if stripped.startswith(emoji):
                # Also consume a trailing variation selector: some keyboards
                # (Apple's especially) emit the emoji followed by U+FE0F, which
                # startswith() matches past but lstrip() would leave behind as an
                # invisible orphan in the middle of the composed title.
                stripped = stripped[len(emoji):].lstrip(VARIATION_SELECTOR).lstrip()
                break
        else:
            break
    prefix = "".join(tag.emoji for tag in tags)
    # rstrip for the degenerate case where the title was nothing but the
    # selected tags' own emoji: stripped is then empty and the f-string would
    # publish a title with a dangling trailing space.
    return f"{prefix} {stripped}".rstrip()


def legendLine(keys) -> str:
    """One line explaining what the title's emoji mean. Empty when no tags."""
    tags = tagsFor(keys)
    if not tags:
        return ""
    return LEGEND_SEPARATOR.join(f"{tag.emoji} {tag.label}" for tag in tags)


def composeDescription(description: str, keys) -> str:
    """Append the legend to an event description.

    The description reaches both the Action Network event page and the Google
    Calendar entry, so this is where a member finds out what the icons in the
    title mean. A no-op when no tags are selected.
    """
    legend = legendLine(keys)
    if not legend:
        return description
    return f"{description}\n\n{legend}"
