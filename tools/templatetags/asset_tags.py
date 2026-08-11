"""Cache-busting for the compiled stylesheet.

THE BUG THIS FIXES
------------------
`{% static 'css/output.css' %}` produces a URL that never changes, so whether a
browser picks up a CSS change depends entirely on the response headers. Behind
nginx (the real deploy, via DeploymentTools) that is fine: nginx sends
Last-Modified and an ETag, and the browser revalidates. But the demo box runs
`manage.py runserver --insecure`, and Django's own staticfiles handler sends
NEITHER a Cache-Control nor an ETag. With no validators at all a browser applies
heuristic freshness and may reuse the file it already has - so a redeploy serves
NEW server-rendered HTML with the OLD stylesheet, and the page renders with the
new markup and none of the new rules.

That is not a hypothetical. It is exactly what a reviewer saw on the questions
workbench after a deploy: correct HTML, correct CSS on the server, a page that
looked unstyled, and a stylesheet in their browser from before the deploy.

WHY A QUERY STRING AND NOT ManifestStaticFilesStorage
-----------------------------------------------------
The manifest storage is the more standard answer and also hashes filenames, but
it post-processes CSS to rewrite every url() reference and fails the whole
collectstatic run if any one of them cannot be resolved. output.css carries
@font-face rules, so that is a real risk for a fix whose entire job is to stop a
deploy from looking broken. A query string needs no post-processing step and
degrades to a plain URL when the file cannot be found.
"""
import hashlib
import logging

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

logger = logging.getLogger(__name__)

register = template.Library()

# path -> short content hash. Populated once per process: these files are built
# at image-build time and cannot change under a running server, so re-hashing on
# every render would buy nothing. A new deploy is a new process.
_versionCache: dict[str, str] = {}


def _contentVersion(path: str) -> str:
    """A short hash of the file's contents, or "" when it cannot be read.

    Returns "" rather than raising, and rather than falling back to something
    like a timestamp: an unversioned URL is the behaviour we have today and is
    merely suboptimal, whereas a 500 on the base template would take every page
    down. A missing static file is already reported by collectstatic.
    """
    if path in _versionCache:
        return _versionCache[path]

    version = ""
    absolutePath = finders.find(path)
    if absolutePath is None:
        # Under a collected-static deploy the finders are not what serves the
        # file, so this is not necessarily an error - just no version available.
        logger.info("versionedStatic: %s not found by the staticfiles finders", path)
    else:
        try:
            with open(absolutePath, "rb") as handle:
                version = hashlib.sha256(handle.read()).hexdigest()[:12]
        except OSError:
            logger.warning("versionedStatic: could not read %s", absolutePath, exc_info=True)

    _versionCache[path] = version
    return version


@register.simple_tag
def versionedStatic(path: str) -> str:
    """Like {% static %}, plus a ?v=<content hash> so a changed file is a
    changed URL. Identical output to {% static %} when the file is unreadable."""
    url = static(path)
    version = _contentVersion(path)
    if not version:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version}"
