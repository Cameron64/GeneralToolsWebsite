"""Inline definitions for words that look ordinary and are not.

`explain` renders one glossary entry as a disclosure the reader can open in
place. It is a native <details>, which is the same primitive the masthead
dropdowns and the user menu already use (see _dropdowns.css) - so it needs no
JavaScript, it opens on tap as well as on click, and it is reachable from the
keyboard without anything being added for that. A hover tooltip would have been
unreachable on a phone, which is where half of this registry gets read.

This library is named for the mechanism rather than for Chapter Tools, because
`tools/common/formRow.html` loads it - and that partial renders the forms of
every domain in the app. The entries themselves are still Chapter Tools' own
(chapterToolsHelp.GLOSSARY); a second source would be added there, not here.
"""
from django import template

from ..chapterToolsHelp import getEntry

register = template.Library()


@register.inclusion_tag("tools/chapter-tools/_explain.html")
def explain(slug, label="What's this?"):
    """Usage: {% explain "steward" %} or {% explain "review" label="Explain" %}.

    Renders nothing at all for an unknown slug rather than raising - see
    chapterToolsHelp.getEntry for why, and test_every_explain_slug_resolves for
    where a typo actually gets caught.
    """
    return {"entry": getEntry(slug), "label": label}


@register.filter
def explainSlugFor(boundField) -> str:
    """The glossary slug for one bound form field, or "" if it has none.

    Usage, inside a {% for field in form %} loop:
        {% include "tools/common/formRow.html" with field=field explainSlug=field|explainSlugFor %}

    The mapping lives on the form class (its EXPLAIN_SLUGS dict) rather than in
    the template, so which fields carry a definition is decided next to the
    fields themselves and cannot be forgotten on the second surface that renders
    the same form. Forms with no mapping - every form in the other domains -
    return "" and render nothing.
    """
    slugs = getattr(boundField.form, "EXPLAIN_SLUGS", None)
    if not slugs:
        return ""
    return slugs.get(boundField.name, "")
