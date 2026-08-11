"""Template tags for the Chapter Tools registry.

`explain` renders one glossary entry as a disclosure the reader can open in
place. It is a native <details>, which is the same primitive the masthead
dropdowns and the user menu already use (see _dropdowns.css) - so it needs no
JavaScript, it opens on tap as well as on click, and it is reachable from the
keyboard without anything being added for that. A hover tooltip would have been
unreachable on a phone, which is where half of this registry gets read.
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
