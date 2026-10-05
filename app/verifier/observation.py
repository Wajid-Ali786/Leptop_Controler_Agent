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
from app.verifier.models import (ActionTarget, Ambiguous, DomElement, DomTarget, Found,
                                 FramesNotSupported, NotEligible, NotFound, Observed,
                                 ObservationSource, Stale, Target, Unavailable, normalize_name)
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


# --- Re-identification: turning an old observation into something safe to act on NOW ------------------
# WHAT runtime_id ACTUALLY PROMISES, checked rather than assumed. pywinauto's own uia_element_info
# documents runtime_id as a "hashable value but may be different from run to run", and its element
# equality uses UI Automation's CompareElements instead - which needs two LIVE COM elements, something
# this project deliberately does not hold across a user confirmation. Microsoft documents the runtime id
# as unique among live elements at one moment, not as a durable name for a control.
#
# So runtime_id DOES NOT decide identity here. It is carried and may be compared as corroboration, but a
# changed runtime id alone is not treated as a different control - if it were, this feature would refuse
# at random for reasons the platform never promised to avoid. Identity is decided by evidence that is
# documented to mean something: the control's automation id, its control type and its window class,
# inside the same window, matching the user's own word for it, and matching it UNIQUELY.
#
# This is the window-handle lesson applied a second time. A number that happens to be stable in practice
# is not an identity, and the moment it is trusted as one the failure is silent and lands on the wrong
# control.

_IDENTITY_WITH_AUTOMATION_ID = "automation id, control type and window class"
_IDENTITY_STRUCTURAL_ONLY = "control type and window class (this control exposes no automation id)"

Reidentified = ActionTarget | Stale | NotEligible | Ambiguous | NotFound | Unavailable


def reidentify(target: Target, observed: Observed) -> Reidentified:
    """Find the control that was observed, AGAIN, right now, and say whether it may be acted on.

    This is not a convenience re-read: it is the step between acting on evidence and acting on a memory.
    Everything the Executor is given - the bounds, and the point inside them - comes from this call and
    never from the observation the user was shown, because by the time a confirmation has been read and
    answered the window may have moved, the list may have scrolled and the button may have been disabled.

    Refuses rather than choosing whenever identity is in doubt: a control that cannot be found again, one
    whose identity no longer matches, and two controls that now answer to the same name are all
    refusals. Nothing here guesses, and nothing here acts."""
    if not isinstance(target, Target) or not isinstance(observed, Observed):
        return Unavailable("I don't have an observed target to re-check.")
    if not isinstance(target.name, str) or not target.name.strip():
        # Asked here rather than left to the adapter: an empty name matching nothing is the right
        # outcome by luck, and a rule that holds by luck is one refactor away from not holding.
        return NotFound("I need the name of something to look for.")
    if observed.source is not ObservationSource.UIA:
        return Unavailable(f"I can only re-check targets found through UI Automation, "
                           f"not {observed.source.value}.")
    if not isinstance(target.window_handle, int) or target.window_handle != observed.window_handle:
        return Unavailable("That target belongs to a different window from the one it was found in.")

    try:
        timeout = _timeout_seconds()
        window_bounds = adapter.uia_window_bounds(target.window_handle)
        elements = adapter.uia_find_by_name(target.window_handle, normalize_name(target.name), timeout)
    except SettingsError as exc:
        return Unavailable(str(exc))
    except adapter.VerifierAdapterError as exc:
        # The commonest reason is the one that matters most: the window closed while the user was reading
        # the confirmation. Reported as it is, never worked around.
        log.info("Reidentify: source=%s resolution=unavailable candidates=0 escalation=false",
                 ObservationSource.UIA.value)
        return Unavailable(f"I couldn't read that window any more ({exc}).")

    reidentified_at = _now()
    candidates = tuple(_observed(element, reidentified_at) for element in elements)
    log.info("Reidentify: source=%s resolution=%s candidates=%d escalation=false",
             ObservationSource.UIA.value,
             "found" if len(candidates) == 1 else "ambiguous" if candidates else "not_found",
             len(candidates))
    if not candidates:
        return NotFound(f"'{target.name.strip()}' isn't in that window any more, so I didn't click.")
    if len(candidates) > 1:
        return Ambiguous(candidates,
                         f"There are now {len(candidates)} things called '{target.name.strip()}' in that "
                         f"window, so I'm not going to guess which one you meant.")

    current = candidates[0]
    identity = _identity_match(observed, current)
    if identity is None:
        return Stale(f"The '{target.name.strip()}' in that window isn't the one I found a moment ago, so "
                     f"I didn't click it.")
    return _action_target(target, current, window_bounds, identity, reidentified_at)


def _identity_match(observed: Observed, current: Observed) -> str | None:
    """Is `current` the same control as `observed`? The NAME OF THE EVIDENCE that says so, or None.

    runtime_id is deliberately absent from this decision - see the note above. The evidence name is
    returned rather than a bare True so that a caller, and a prompt, can say how strong the match was;
    it names kinds of evidence and never their values, so it is safe to show and to log."""
    if observed.control_type != current.control_type or observed.class_name != current.class_name:
        return None
    if observed.automation_id or current.automation_id:
        if observed.automation_id != current.automation_id:
            return None
        return _IDENTITY_WITH_AUTOMATION_ID
    # No automation id to lean on. The match then rests on the control type, the window class, the user's
    # own word for it, and the fact that exactly ONE control in this window answers to it. That is
    # genuinely weaker evidence, so it is named rather than quietly treated as equivalent.
    return _IDENTITY_STRUCTURAL_ONLY


def _action_target(target: Target, current: Observed, window_bounds, identity: str,
                   reidentified_at: float) -> ActionTarget | NotEligible:
    """The eligibility rules. Every one of them refuses; none of them adjusts anything to make a target
    usable, because a target that needs adjusting is a target that is not understood."""
    named = target.name.strip()
    if not current.enabled:
        return NotEligible(f"'{named}' is there but greyed out, so clicking it would do nothing.")
    if current.offscreen:
        return NotEligible(f"'{named}' is scrolled or hidden out of view, so I didn't click it.")
    if not _usable(current.bounds):
        return NotEligible(f"I can't tell where '{named}' is on screen, so I didn't click it.")
    bounds = current.bounds
    point = (bounds[0] + (bounds[2] - bounds[0]) // 2, bounds[1] + (bounds[3] - bounds[1]) // 2)
    if _usable(window_bounds) and not (window_bounds[0] <= point[0] < window_bounds[2]
                                       and window_bounds[1] <= point[1] < window_bounds[3]):
        # The control reports a position outside its own window. There is nothing to reconcile here: the
        # evidence contradicts itself, and the one thing not to do is click where it points.
        return NotEligible(f"'{named}' is reported outside its own window, so I didn't click it - that "
                           f"position can't be right.")
    return ActionTarget(source=ObservationSource.UIA, window_handle=current.window_handle, bounds=bounds,
                        point=point, target_name=target.name, reidentified_at=reidentified_at,
                        identity=identity)


def _usable(bounds) -> bool:
    """Four whole numbers describing a rectangle with real area. An empty or inverted rectangle is not a
    very small target: it is unreadable evidence, and it is treated as such."""
    return (isinstance(bounds, tuple) and len(bounds) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in bounds)
            and bounds[2] > bounds[0] and bounds[3] > bounds[1])


# --- Layer 2: the browser DOM (Phase 5 DOM Slice 1) ---------------------------------------------------
# PURE. This function performs no browser IO, holds no Playwright object and cannot act - it is handed
# the structural matches that app/executor/adapter.dom_query() already filtered, and it decides what
# they mean. That split exists because Playwright can click as well as read: the library stays in the
# one module allowed to control this computer, and the judgement stays here, so no module both decides
# and acts.
#
# It is deliberately NOT wired to the UIA layer yet. The frozen hierarchy (UIA first, DOM second) is
# still binding, but the mapping between a Playwright page and the top-level window UIA reads has not
# been designed, and inventing one here would be guessing which window a page belongs to. Orchestration
# is a later slice, after that mapping is audited.


def resolve_dom_target(target: DomTarget,
                       elements, has_frames: bool = False
                       ) -> Found | Ambiguous | NotFound | FramesNotSupported | Unavailable:
    """Which page control the user meant, out of the matches already found for their word.

    Found when exactly one matched. Ambiguous when more than one did - across different roles too,
    because two things called Login are two things called Login whether one is a button and the other
    a link. Nothing is chosen.

    A miss is reported two different ways on purpose. NotFound means the top-level document really has
    no such control. FramesNotSupported means it has none AND the page has child frames this slice does
    not look inside - "I did not find it" and "I did not look everywhere it could be" are different
    claims, and a later layer must not act on the second as though it were the first."""
    if not isinstance(target, DomTarget) or not isinstance(target.name, str) or not target.name.strip():
        return NotFound("I need the name of something to look for.")
    if not isinstance(target.session_id, str) or not target.session_id \
            or not isinstance(target.page_id, str) or not target.page_id:
        return Unavailable("I don't have a browser page to look in.")
    if elements is None:
        return Unavailable("I couldn't read that page.")

    matches = tuple(e for e in elements
                    if isinstance(e, DomElement)
                    and e.session_id == target.session_id and e.page_id == target.page_id)
    _log_dom(len(matches), bool(has_frames))

    if len(matches) == 1:
        return Found(_observed_from_dom(matches[0]))
    if len(matches) > 1:
        return Ambiguous(tuple(_observed_from_dom(match) for match in matches),
                         f"There are {len(matches)} things called '{target.name.strip()}' on that "
                         f"page, so I'm not going to guess which one you mean.")
    if has_frames:
        return FramesNotSupported(
            f"I couldn't find '{target.name.strip()}' on that page, but parts of it are embedded "
            f"frames I can't read yet - so it may be in there. I'm not going to say it isn't there.")
    return NotFound(f"I couldn't find anything called '{target.name.strip()}' on that page.")


def _observed_from_dom(element: DomElement) -> Observed:
    """A DOM match as the one observation shape every layer shares, so the provenance travels with it.

    `window_handle` is 0: a page is not a window, and this slice has no mapping between the two. Saying
    0 is honest; inventing a handle would be the beginning of guessing which window a page is in."""
    return Observed(
        source=ObservationSource.DOM,
        window_handle=0,
        control_type=element.role,
        observed_at=element.observed_at,
        runtime_id=element.element_token,
        automation_id="",
        class_name=element.tag,
        bounds=element.bounds,
        enabled=element.enabled,
        focused=False,
        offscreen=not element.visible,
        is_password=element.is_password,
        patterns=(),
    )


def _log_dom(candidates: int, has_frames: bool) -> None:
    """Counts and layer metadata only - never a name, never page text, never a URL or a domain."""
    log.info("Observation: source=%s resolution=%s candidates=%d frames_unread=%s escalation=false",
             ObservationSource.DOM.value,
             "found" if candidates == 1 else "ambiguous" if candidates > 1
             else "frames_unread" if has_frames else "not_found",
             candidates, str(bool(has_frames)).lower())
