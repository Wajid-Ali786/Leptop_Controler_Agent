"""
Screen observation: resolving a target the user named (docs/step4 Section 8, Build Plan Section 6.5).

Phase 5 Slice 1 is deliberately one layer wide. It answers exactly one question - "which control in this
window is the one the user called X?" - using Windows UI Automation and nothing else. There is no
fall-through hierarchy yet, because there is only one layer; DOM, OCR, vision and coordinate fallback
are later slices of the frozen hierarchy and are not stubbed in here.

WHAT THIS MODULE IS NOT. It cannot act. It imports no Executor, no Planner, no Safety and no Brain, and
nothing it returns can carry an ExecutorAction. Observation is evidence; the user's own words stay the
only thing that can become an action, and app/safety stays the only authority on whether it may run.

WHAT LEAVES THIS MODULE. Structural evidence only: identifiers, a control type, bounds, and enabled /
focused / offscreen / password flags. Accessible names are compared inside app/verifier/adapter.py and
never cross that boundary, so an unmatched label cannot be returned, logged or sent anywhere. Nothing
observed goes to Claude: the one planned provider-egress layer is the later screenshot layer, and it
will need its own explicit consent and its own review.
"""
import logging
import time

from app.verifier import adapter
from app.verifier.models import (Ambiguous, Found, NotFound, Observed, ObservationSource, Target,
                                 Unavailable, normalize_name)
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)

_now = time.time  # replaced in tests to control the clock


def resolve_target(target: Target) -> Found | Ambiguous | NotFound | Unavailable:
    """The one control in `target.window_handle` named `target.name`, as structural evidence.

    Found when exactly one matches. Ambiguous when more than one does - nothing is chosen, and the
    candidates carry structural distinctions only. NotFound when the window was readable and nothing
    matched. Unavailable when the window could not be observed at all, which is what a missing
    accessibility tree looks like and is the signal a later slice will escalate on.

    Matching is whitespace-normalised and case-insensitive EXACT equality. A substring does not match, a
    near spelling does not match, and nothing is guessed: two buttons called Login must be asked about,
    for the same reason two people called Ali must be."""
    if not isinstance(target, Target) or not isinstance(target.name, str) or not target.name.strip():
        return NotFound("I need the name of something to look for.")
    if not isinstance(target.window_handle, int) or target.window_handle <= 0:
        return Unavailable("I don't have a window to look in.")

    wanted = normalize_name(target.name)
    try:
        timeout = _timeout_seconds()
        elements = adapter.uia_find_by_name(target.window_handle, wanted, timeout)
    except SettingsError as exc:
        return Unavailable(str(exc))
    except adapter.VerifierAdapterError as exc:
        # No accessibility tree, the window is gone, or UI Automation refused. Reported, never guessed
        # around - and the reason names the problem, not anything read off the screen.
        log.info("Observation: source=%s resolution=unavailable escalation=false", ObservationSource.UIA.value)
        return Unavailable(f"I couldn't read that window's controls ({exc}).")

    observed_at = _now()
    candidates = tuple(_observed(element, observed_at) for element in elements)
    # Counts only. The name the user gave is theirs, but it is not needed here and the labels that
    # matched are never logged.
    log.info("Observation: source=%s resolution=%s candidates=%d escalation=false",
             ObservationSource.UIA.value,
             "found" if len(candidates) == 1 else "ambiguous" if candidates else "not_found",
             len(candidates))

    if not candidates:
        return NotFound(f"I couldn't find anything called '{target.name.strip()}' in that window.")
    if len(candidates) > 1:
        return Ambiguous(candidates,
                         f"There are {len(candidates)} things called '{target.name.strip()}' in that "
                         f"window, so I'm not going to guess which one you mean.")
    return Found(candidates[0])


def is_fresh(observed: Observed, now: float | None = None) -> bool:
    """Is this observation still worth acting on? Freshness is asked explicitly, never assumed: the
    screen can change between observing and acting, and a bounding rectangle in particular means
    nothing once the window has moved. Nothing here polls, and nothing acts either way."""
    if not isinstance(observed, Observed):
        return False
    try:
        window = _freshness_seconds()
    except SettingsError:
        return False                   # unreadable setting fails closed: treat it as stale
    return observed.is_fresh(_now() if now is None else now, window)


def _observed(element, observed_at: float) -> Observed:
    """One adapter element as evidence. The adapter's element carries no name, so there is none to drop."""
    return Observed(
        source=ObservationSource.UIA,
        window_handle=element.window_handle,
        control_type=element.control_type,
        observed_at=observed_at,
        runtime_id=element.runtime_id,
        automation_id=element.automation_id,
        class_name=element.class_name,
        bounds=element.bounds,
        enabled=element.enabled,
        focused=element.focused,
        offscreen=element.offscreen,
        is_password=element.is_password,
        patterns=element.patterns,
    )


def _timeout_seconds() -> float:
    return _positive("observation.snapshot_timeout_seconds")


def _freshness_seconds() -> float:
    return _positive("observation.freshness_seconds")


def _positive(name: str) -> float:
    value = get_setting(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number, got {value!r}.")
    return float(value)
