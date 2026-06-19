"""Resolutions domain - submission, member sign-on, and the Secretary dashboard.

Echo owns the canonical resolution text and enforces the integrity guarantee:
the text locks on the first verified sign-on, and any later edit resets the
sign-ons (see Resolution.replaceText). Each signer is validated live as a Member
in Good Standing against Action Network at sign-on time, via the ANMIGValidator
protocol (the demo box and tests use the deterministic mock; production uses the
live OSDI client when an AN token is configured).

The bylaws set the rules by kind: a general resolution needs no sign-ons (the
Leadership Committee sets the agenda); a project committee needs 25 (Section
7.1.5); a bylaws amendment needs proponent + 35 (Section 10.1). The single source
of that truth is Resolution.Kind.
"""
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe

from . import permissions
from .forms import ResolutionEditForm, ResolutionForm
from .models import Resolution, ResolutionSignature
from .resolutionText import normalizedTextHash, renderMarkdown
from .ActionNetworkAPI.migValidator import LiveANMIGValidator, MockANMIGValidator

logger = logging.getLogger(__name__)

BYLAWS_BASE = "https://github.com/Austin-DSA/Bylaws-Resolutions/blob/main/bylaws.md"

# Self-documenting copy for the submit-form type cards. Thresholds/lead-days come
# from Resolution.Kind (the single source of truth); these are just the labels.
_TYPE_LABELS = {
    Resolution.Kind.GENERAL: {
        "thresholdLabel": "No member sign-ons required",
        "deadlineLabel": "submit before the agenda is published",
        "sectionAnchor": "",
    },
    Resolution.Kind.PROJECT_COMMITTEE: {
        "thresholdLabel": "25 member sign-ons",
        "deadlineLabel": "10 days before the meeting",
        "sectionAnchor": "#section-71-project-committees",
    },
    Resolution.Kind.BYLAWS_AMENDMENT: {
        "thresholdLabel": "Proponent + 35 sign-ons",
        "deadlineLabel": "21 days before the GBM",
        "sectionAnchor": "#section-101-notice-of-proposed-amendments",
    },
}


def _typeCards():
    """The type-card context, derived from Resolution.Kind so the cards and the
    model never drift on thresholds."""
    cards = []
    for key, title in Resolution.Kind.CHOICES:
        labels = _TYPE_LABELS.get(key, {})
        anchor = labels.get("sectionAnchor", "")
        cards.append({
            "key": key,
            "title": title,
            "coverage": Resolution.Kind.COVERAGE.get(key, ""),
            "threshold": Resolution.Kind.THRESHOLDS.get(key),
            "thresholdLabel": labels.get("thresholdLabel", ""),
            "deadlineLabel": labels.get("deadlineLabel", ""),
            "section": Resolution.Kind.SECTION.get(key, ""),
            "sectionUrl": BYLAWS_BASE + anchor if anchor else BYLAWS_BASE,
        })
    return cards


def getMIGValidator():
    """The MIG validator to use for a sign-on.

    Live validator only when an AN OSDI token is configured AND we are not in
    DEBUG/DEMO_MODE; otherwise the deterministic mock. The demo box has no AN
    credentials, and tests must never hit the network, so both fall back to the
    mock. Tests override this function to inject specific failure cases."""
    token = None
    try:
        from .SecretManager import SecretManager
        token = SecretManager.getANAPIKey()
    except Exception:
        logger.exception("Could not read the Action Network API token; using mock validator")
    if token and not settings.DEBUG and not getattr(settings, "DEMO_MODE", False):
        return LiveANMIGValidator(token)
    return MockANMIGValidator()


@login_required
def submit_resolution(request):
    """The standard submission form. On a valid POST, persists a real Resolution
    and redirects to its detail page."""
    form = ResolutionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        resolution = Resolution.objects.create(
            title=form.cleaned_data["title"],
            kind=form.cleaned_data["kind"],
            text=form.cleaned_data["text"],
            proponent=request.user,
            targetMeeting=form.cleaned_data["targetMeeting"],
        )
        return redirect("resolution-detail", pk=resolution.pk)
    return render(request, "tools/resolutions/submit.html", {
        "form": form,
        "types": _typeCards(),
    })


@login_required
def sign_resolution(request):
    """Browse resolutions currently gathering sign-ons. Each links to its detail
    page, where the actual sign-on happens."""
    resolutions = (
        Resolution.objects.filter(status=Resolution.Status.GATHERING)
        .select_related("proponent", "targetMeeting")
    )
    return render(request, "tools/resolutions/browse.html", {
        "resolutions": resolutions,
    })


def _detailContext(request, resolution, signOutcome=None):
    alreadySigned = ResolutionSignature.objects.filter(
        resolution=resolution, member=request.user,
    ).exists()
    return {
        "resolution": resolution,
        "renderedText": mark_safe(renderMarkdown(resolution.text)),
        "alreadySigned": alreadySigned,
        "canSign": resolution.isOpenForSignOn() and not alreadySigned,
        "isProponent": resolution.proponent_id == request.user.id,
        "signOutcome": signOutcome,
    }


@login_required
def resolution_detail(request, pk):
    """The in-Echo read surface: rendered text, live count/threshold/deadline,
    and the sign-on action. POST records a sign-on after a live MIG check."""
    resolution = get_object_or_404(Resolution, pk=pk)

    if request.method != "POST":
        return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution))

    # --- sign-on ---
    if not resolution.isOpenForSignOn():
        outcome = {"ok": False, "message": "This resolution is not open for sign-ons."}
        return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution, outcome))

    if ResolutionSignature.objects.filter(resolution=resolution, member=request.user).exists():
        outcome = {"ok": True, "message": "You have already signed on to this resolution."}
        return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution, outcome))

    result = getMIGValidator().verify(request.user.email)
    if not result.ok:
        # Fail closed: never record an unverified signer as verified.
        outcome = {"ok": False, "message": result.help(), "status": result.status}
        return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution, outcome))

    try:
        ResolutionSignature.objects.create(
            resolution=resolution,
            member=request.user,
            textHashAtSigning=normalizedTextHash(resolution.text),
            verified=True,
            verificationStatus=result.status,
            checkedAt=result.checkedAt,
        )
    except IntegrityError:
        outcome = {"ok": True, "message": "You have already signed on to this resolution."}
        return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution, outcome))

    # Lock the text on the first sign-on (idempotent).
    resolution.lockText()
    resolution.refresh_from_db()
    outcome = {"ok": True, "message": "Your sign-on is recorded. Thank you."}
    return render(request, "tools/resolutions/detail.html", _detailContext(request, resolution, outcome))


@login_required
def resolution_edit(request, pk):
    """The proponent edits the resolution text. A real change to a locked
    resolution resets its sign-ons (see Resolution.replaceText); we require an
    explicit confirm before applying such a reset."""
    resolution = get_object_or_404(Resolution, pk=pk)
    if resolution.proponent_id != request.user.id and not request.user.has_perm(permissions.ADMINISTER_RESOLUTIONS):
        raise PermissionDenied
    if resolution.status != Resolution.Status.GATHERING:
        raise PermissionDenied  # an adopted/closed resolution is read-only

    needsResetConfirm = False
    if request.method == "POST":
        form = ResolutionEditForm(request.POST)
        if form.is_valid():
            newText = form.cleaned_data["text"]
            changesLocked = (
                resolution.locked
                and normalizedTextHash(newText) != resolution.lockedTextHash
            )
            if changesLocked and not form.cleaned_data.get("confirmReset"):
                needsResetConfirm = True
            else:
                resolution.replaceText(newText)
                return redirect("resolution-detail", pk=resolution.pk)
    else:
        form = ResolutionEditForm(initial={"text": resolution.text})

    return render(request, "tools/resolutions/edit.html", {
        "resolution": resolution,
        "form": form,
        "needsResetConfirm": needsResetConfirm,
        "signatureCount": resolution.signatureCount,
    })


@login_required
@permission_required(permissions.ADMINISTER_RESOLUTIONS)
def resolution_status(request):
    """The Secretary's dashboard: everything in flight with its bylaws checks."""
    resolutions = (
        Resolution.objects.exclude(status=Resolution.Status.CLOSED)
        .select_related("proponent", "targetMeeting")
    )
    return render(request, "tools/resolutions/status.html", {
        "resolutions": resolutions,
    })
