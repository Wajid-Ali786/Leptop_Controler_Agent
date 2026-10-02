"""
DESIGN PROBE for recovering window ownership after HWND loss. Changes no production code.

The candidate rule, modelled here so it can be attacked before anything is built:

    PRIMARY   any originally recorded HWND still exists and still matches  -> owned (today's behaviour)
    FALLBACK  all original HWNDs are gone
              AND exactly ONE matching window is not in the pre-launch baseline
              AND that window's strong process identity equals the one recorded at open
                                                                        -> recover ownership
    OTHERWISE                                                           -> refuse

Everything below exists to find out where that rule breaks. The answer it produces is NOT "the rule
works": it is that the rule is safe only because of the uniqueness clause, and that the clause makes the
rule decline exactly the cases where a user's own window could be mistaken for ours.

`Identity` here stands for a Windows process identity strong enough to survive HWND replacement and to
defeat PID reuse - a (pid, creation_time) pair. See the report for what the adapter can obtain today.
"""
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class Identity:
    """A strong process identity. `created` is what defeats PID reuse: a recycled PID belongs to a
    process with a different creation time, so the pair never matches a dead process's."""
    pid: int
    created: float

    @staticmethod
    def weak(pid: int) -> "Identity":
        """PID alone, as the rejected proposal would have stored it."""
        return Identity(pid, created=0.0)


@dataclass(frozen=True)
class Window:
    handle: int
    identity: Identity
    matches: bool = True        # does the title match the app's pattern?


@dataclass(frozen=True)
class Record:
    """What a fix would record at open time."""
    handles: frozenset
    identity: Identity
    baseline: frozenset         # matching handles that existed BEFORE the launch


OWNED, REFUSE = "owned", "refuse"


def decide(record: Record, desktop: list, *, use_identity: bool, strong: bool = True) -> str:
    """The candidate rule. `use_identity=False` is today's production behaviour."""
    alive = [w for w in desktop if w.handle in record.handles and w.matches]
    if alive:
        return OWNED                                     # PRIMARY path, unchanged
    if not use_identity:
        return REFUSE                                    # today: nothing else is tried
    def same(a: Identity, b: Identity) -> bool:
        return (a.pid, a.created) == (b.pid, b.created) if strong else a.pid == b.pid
    candidates = [w for w in desktop
                  if w.matches and w.handle not in record.baseline and same(w.identity, record.identity)]
    return OWNED if len(candidates) == 1 else REFUSE


OURS = Identity(pid=4321, created=1000.0)
THEIRS = Identity(pid=9999, created=500.0)


# --- 1/2. Why PID alone is not safe --------------------------------------------------------------------

def test_a_reused_pid_can_falsely_match_when_only_the_pid_is_stored():
    """1 + 2: our Notepad's process exits; Windows later gives 4321 to an unrelated process that happens
    to own a matching window. With PID alone that window looks like ours."""
    record = Record(handles=frozenset({1001}), identity=Identity.weak(4321), baseline=frozenset())
    recycled = [Window(handle=2001, identity=Identity.weak(4321))]      # same number, different process
    assert decide(record, recycled, use_identity=True, strong=False) == OWNED, "the false match"


def test_a_creation_time_defeats_pid_reuse():
    """The same scenario with a strong identity: the recycled process has a different creation time, so
    it cannot impersonate the dead one."""
    record = Record(handles=frozenset({1001}), identity=OURS, baseline=frozenset())
    recycled = [Window(handle=2001, identity=Identity(pid=4321, created=7777.0))]
    assert decide(record, recycled, use_identity=True) == REFUSE


def test_the_session_being_short_is_not_a_defence():
    """PID reuse is not a function of our session length - the window we are reasoning about may have
    outlived its original process by any amount of time, and we only look at close time."""
    record = Record(handles=frozenset({1001}), identity=Identity.weak(4321), baseline=frozenset())
    for _ in range(5):
        assert decide(record, [Window(2001, Identity.weak(4321))],
                      use_identity=True, strong=False) == OWNED


# --- 3. The launcher PID is not the window's PID -------------------------------------------------------

def test_recording_the_launch_pid_instead_of_the_window_pid_loses_ownership_immediately():
    """3: if Popen.pid is a launcher that exits, or the request is handed to an already-running process,
    the recorded identity never matches any window - the fallback is dead on arrival.

    Conclusion: the identity MUST be read from the VERIFIED window at open time, never from Popen."""
    launcher = Identity(pid=1111, created=10.0)                 # what Popen handed back
    record = Record(handles=frozenset({1001}), identity=launcher, baseline=frozenset())
    desktop = [Window(handle=2001, identity=OURS)]              # the real owner of our window
    assert decide(record, desktop, use_identity=True) == REFUSE


def test_reading_the_identity_from_the_verified_window_works():
    record = Record(handles=frozenset({1001}), identity=OURS, baseline=frozenset())
    assert decide(record, [Window(2001, OURS)], use_identity=True) == OWNED


# --- 4/5. Does strong identity resolve a window, or only a process? -----------------------------------

def test_case_a_pre_existing_window_in_the_same_process_is_excluded_by_the_baseline():
    """A: the user already had W1 in process P; our launch produced W2 in the SAME P.

    Strong identity alone cannot tell W1 from W2 - they share it. The BASELINE does: W1 existed before
    the launch, so it can never be a recovery candidate."""
    record = Record(handles=frozenset({2}), identity=OURS, baseline=frozenset({1}))
    desktop = [Window(1, OURS), Window(3, OURS)]        # W1 survives; our W2 became W3
    assert decide(record, desktop, use_identity=True) == OWNED

    without_baseline = Record(handles=frozenset({2}), identity=OURS, baseline=frozenset())
    assert decide(without_baseline, desktop, use_identity=True) == REFUSE, (
        "without the baseline the two are indistinguishable and it must refuse")


def test_case_b_our_handle_replaced_while_the_stranger_remains():
    """B: recovery succeeds, and it picks the non-baseline window - never the stranger."""
    record = Record(handles=frozenset({2}), identity=OURS, baseline=frozenset({1}))
    assert decide(record, [Window(1, OURS), Window(3, OURS)], use_identity=True) == OWNED


def test_case_c_a_window_the_user_opens_later_makes_recovery_ambiguous():
    """C and §6, THE MANDATORY ATTACK.

        t0  no Notepad      t1  agent opens W1      t2  user opens W2 in the same process
        t3  W1's handle is replaced by W3

    At close we see W2 and W3. Both match, both share our process identity, neither is in the baseline.
    Nothing distinguishes them, so the rule REFUSES. A false refusal is the required outcome: choosing
    would be a coin flip on the user's window."""
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset())
    desktop = [Window(2, OURS), Window(3, OURS)]
    assert decide(record, desktop, use_identity=True) == REFUSE


def test_case_d_a_lone_window_whose_handle_changed_is_recoverable():
    """D: exactly one matching window, not in the baseline, same strong identity. This is the real
    session's shape - "1 notepad window is open" - and the only case worth recovering."""
    record = Record(handles=frozenset({1001}), identity=OURS, baseline=frozenset())
    assert decide(record, [Window(2001, OURS)], use_identity=True) == OWNED
    assert decide(record, [Window(2001, OURS)], use_identity=False) == REFUSE, "today it refuses"


# --- 7. The pre-existing window is never closed -------------------------------------------------------

def test_a_pre_existing_window_is_never_recovered_even_alone(monkeypatch):
    """§7, NON-NEGOTIABLE. The user had W0. We launched; our W1 vanished. W0 is all that remains, and it
    shares our process identity. It must NOT become ours."""
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset({0}))
    assert decide(record, [Window(0, OURS)], use_identity=True) == REFUSE


def test_process_membership_never_becomes_blanket_app_ownership():
    """Three strangers in our process, our handle gone: refuse, because uniqueness fails."""
    record = Record(handles=frozenset({9}), identity=OURS, baseline=frozenset())
    desktop = [Window(1, OURS), Window(2, OURS), Window(3, OURS)]
    assert decide(record, desktop, use_identity=True) == REFUSE


def test_a_stranger_in_a_different_process_is_never_a_candidate():
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset())
    assert decide(record, [Window(2, THEIRS)], use_identity=True) == REFUSE


# --- 10/11. The process restarting, and the primary path -----------------------------------------------

def test_a_restarted_process_does_not_confer_ownership():
    """10: same executable, same alias, new process - a different creation time, so no match."""
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset())
    restarted = Identity(pid=OURS.pid, created=OURS.created + 60)
    assert decide(record, [Window(2, restarted)], use_identity=True) == REFUSE


def test_the_exact_handle_path_is_unchanged_by_any_of_this():
    """11: while an original handle lives, nothing about identity is consulted - so the fallback cannot
    introduce a regression into the path that works today."""
    record = Record(handles=frozenset({1, 2}), identity=OURS, baseline=frozenset())
    desktop = [Window(2, THEIRS)]          # identity does not even match
    assert decide(record, desktop, use_identity=True) == OWNED
    assert decide(record, desktop, use_identity=False) == OWNED


def test_a_non_matching_window_is_never_a_candidate():
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset())
    assert decide(record, [Window(2, OURS, matches=False)], use_identity=True) == REFUSE


# --- 12. Conversational context stays irrelevant -------------------------------------------------------

def test_nothing_in_this_design_consults_conversational_context():
    """12: the rule's inputs are handles, a baseline and a process identity. There is no place for
    "the last thing I did was open notepad" to enter it."""
    import inspect
    source = inspect.getsource(decide)
    for forbidden in ("previous_action", "context", "alias", "name"):
        assert forbidden not in source, f"the rule consults {forbidden}"


# --- §9. The information problem, stated as a test ----------------------------------------------------

def test_two_observations_cannot_distinguish_replacement_from_a_new_window():
    """§9. At open we saw {W1}. At close we see {W2, W3}, same process, neither in the baseline.

    Was W1 replaced by W2 (and W3 is the user's), or replaced by W3 (and W2 is the user's)? The two
    observations are IDENTICAL in both worlds. No amount of process identity resolves it: the
    information needed - which window replaced which - exists only in the transition we did not watch.

    So safe recovery is possible for the unique case and provably impossible for the ambiguous one,
    without continuous lineage observation."""
    record = Record(handles=frozenset({1}), identity=OURS, baseline=frozenset())
    world_one = [Window(2, OURS), Window(3, OURS)]     # W2 is ours, W3 the user's
    world_two = [Window(3, OURS), Window(2, OURS)]     # W3 is ours, W2 the user's
    assert decide(record, world_one, use_identity=True) == decide(record, world_two,
                                                                 use_identity=True) == REFUSE


@pytest.mark.parametrize("candidates, expected", [(0, REFUSE), (1, OWNED), (2, REFUSE), (3, REFUSE)])
def test_recovery_happens_only_when_exactly_one_candidate_exists(candidates, expected):
    """The uniqueness clause is the whole safety argument, so it is worth stating alone."""
    record = Record(handles=frozenset({99}), identity=OURS, baseline=frozenset())
    desktop = [Window(index, OURS) for index in range(1, candidates + 1)]
    assert decide(record, desktop, use_identity=True) == expected
