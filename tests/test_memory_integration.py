"""
Phase 4 FINAL INTEGRATION: Memory reachable from the application, and the frozen acceptance cases.

Three things are proven here that the Memory slices could not prove on their own:

  1. a remembered application alias resolves inside the REAL console path, locally and for free, with
     configuration still deciding first and Memory unable to add capability
  2. the Brain/Planner-facing query boundary answers the frozen "message my friend Ali" case as a
     LOOKUP - and no resolved person, contact or path reaches the provider
  3. D6 at application level: with memory wiped, missing or corrupt, the companion still works

Everything is offline. The provider is a scripted function, the Executor is replaced wherever a test is
about orchestration, and the central SQLite guard keeps every database under tmp_path.
"""
import ast
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from app import console
from app.brain import logic as brain
from app.brain.interpreter import Unavailable
from app.brain.models import Intent, OpenAppArgs, Understood
from app.console import Status
from app.executor.logic import configured_app_names, resolve
from app.executor.models import CLOSE_APP, OPEN_APP, ExecutorAction, Resolved, Unresolved
from app.brain import personal_memory
from app.memory import logic as memory
from app.memory import queries
from app.memory.models import (Ambiguous, Correction, CorrectionLearner, CorrectionTask, Found,
                               MemoryDatabase, MemoryUnavailable, NotFound, PersistenceDecision,
                               Redacted)
from app.planner.models import TurnContext
from config import settings
from config.settings import SettingsError
from tests.test_console_brain import Brainless, Script, executor, no_stop, run, understood  # noqa: F401

ALLOW, DENY = PersistenceDecision.ALLOW, PersistenceDecision.DENY
ADDRESS = "+92-300-0000001"
SCOPE, WRONG = "website:example.com/login", "button-A"


@pytest.fixture
def memory_at(tmp_path, monkeypatch):
    """Point the real Memory logic at this test's own database, without creating it."""
    path = tmp_path / "memory" / "memory.db"
    config = tmp_path / "config.yaml"
    config.write_text(f"memory:\n  db_path: {path.as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(memory, "database_path", lambda: path)
    return path


@pytest.fixture
def remembered(memory_at):
    """An initialised memory holding one alias, one person with a friend relationship and a contact."""
    assert isinstance(memory.initialize(), MemoryDatabase)
    assert isinstance(memory.remember_application_alias("editor", "notepad", configured_app_names()),
                      Found)
    ali = memory.add_person("Ali").value.id
    memory.add_relationship(ali, "friend")
    memory.add_contact(ali, "whatsapp", ADDRESS)
    return memory_at


def provider_that_must_not_be_called(prompt):
    raise AssertionError("a locally resolvable line must not cost a provider call")


# =======================================================================================================
# 1. APPLICATION ALIASES, IN THE REAL CONSOLE PATH
# =======================================================================================================

def test_a_direct_configured_app_is_untouched_by_the_integration(remembered, executor):
    """Item 1, and the §2 weight: the Phase 1 path must be byte-identical. "open notepad" resolves in
    configuration, becomes a LocalAction before the alias branch is reachable, and never asks Memory."""
    reply, _context = run("open notepad", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN, reply.message
    assert [action.kind for action in executor.actions] == [OPEN_APP]
    assert executor.actions[0].target == "notepad", "the target was rewritten"


def test_a_remembered_alias_resolves_to_the_configured_app(remembered, executor):
    """Item 2. "open editor" cannot be resolved by configuration, Memory supplies the configured key
    `notepad`, and the existing Executor path runs it."""
    assert isinstance(brain.route("open editor", resolve), brain.BrainEligible), "config refuses it"

    reply, _context = run("open editor", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN, reply.message
    assert [(action.kind, action.target) for action in executor.actions] == [(OPEN_APP, "notepad")]


def test_resolving_an_alias_costs_no_provider_call(remembered, executor):
    """Items 3 and 13, and §13. The interpret function fails if touched, and the Brain allowance is
    untouched - a remembered name is a local answer, not a reason to spend a request."""
    asked = []

    def counting(prompt):
        asked.append(prompt)
        raise AssertionError("no provider call is allowed here")

    reply, context = run("open editor", interpret=counting)
    assert reply.status is Status.RAN
    assert asked == []
    assert context.budget.calls == 0, context.budget


def test_a_remembered_close_also_resolves(remembered, executor):
    """The alias branch covers both verbs, because the Memory alias names an app, not an action."""
    reply, _context = run("close editor", interpret=provider_that_must_not_be_called,
                          confirm=lambda action, assessment: True)
    assert reply.status is Status.RAN, reply.message
    assert [(action.kind, action.target) for action in executor.actions] == [(CLOSE_APP, "notepad")]


def test_a_stale_alias_cannot_widen_capability(memory_at, executor, monkeypatch):
    """Item 4. The alias was stored while its app was configured; configuration no longer lists it, so
    the line goes to the Brain exactly as an unknown app always has - Memory adds nothing."""
    assert isinstance(memory.initialize(), MemoryDatabase)
    assert isinstance(memory.remember_application_alias("editor", "notepad", ("notepad",)), Found)
    monkeypatch.setattr(console, "configured_app_names", lambda: ["calculator"])

    reply, _context = run("open editor", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE, reply.message
    assert executor.calls == [], "a stale alias opened something"


def test_the_existing_resolver_has_the_final_word_on_whatever_memory_returns(memory_at, executor,
                                                                               monkeypatch):
    """Defence in depth, and the one case the stale-alias test cannot reach. There, Memory itself refuses
    the alias; here Memory is handed a configured-looking key that configuration does not actually have,
    so it answers Found - and the EXISTING resolver is what refuses it.

    If the branch trusted Memory's answer instead of re-resolving it, this would try to open photoshop."""
    assert isinstance(memory.initialize(), MemoryDatabase)
    assert isinstance(memory.remember_application_alias("editor", "photoshop", ("photoshop",)), Found)
    monkeypatch.setattr(console, "configured_app_names", lambda: ["photoshop"])
    assert isinstance(queries.application("editor", ["photoshop"]), Found), "Memory does answer"
    assert isinstance(resolve(ExecutorAction(OPEN_APP, "photoshop")), Unresolved), "config does not"

    reply, _context = run("open editor", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE, reply.message
    assert executor.calls == [], "an app configuration does not have was opened"


def test_memory_can_never_supply_an_executable_or_a_path(remembered, executor):
    """Item 4's other half. Whatever Memory returns is run through the EXISTING resolver, and the
    applications table has no column that could hold an executable in the first place."""
    resolved = queries.application("editor", configured_app_names())
    assert isinstance(resolved, Found) and resolved.value == "notepad"
    for forbidden in (".exe", "/", "\\", "cmd"):
        assert forbidden not in resolved.value
    assert isinstance(resolve(ExecutorAction(OPEN_APP, resolved.value)), Resolved)
    assert isinstance(resolve(ExecutorAction(OPEN_APP, "notepad.exe")), Unresolved), (
        "the resolver would refuse an executable anyway")


def test_an_unknown_alias_behaves_exactly_as_before(remembered, executor):
    """Item 5. Nothing is remembered for "photoshop", so the line reaches the Brain unchanged."""
    reply, _context = run("open photoshop", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE
    assert executor.calls == []


# --- The ORDER: configuration first, and a Memory failure changes nothing -----------------------------

def test_configuration_is_consulted_before_memory(remembered, executor, monkeypatch):
    """§2's ordering invariant, proven by making Memory hostile. If the alias lookup ran FIRST, a
    remembered entry for a CONFIGURED name could redirect it; because configuration decides first, the
    lookup is never even reached for "open notepad"."""
    assert isinstance(memory.remember_application_alias("notepad", "calculator",
                                                        configured_app_names()), Found)
    asked = []
    real_application = queries.application
    monkeypatch.setattr(console.memory_queries, "application",
                        lambda alias, keys: asked.append(alias) or real_application(alias, keys))

    reply, _context = run("open notepad", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN
    assert executor.actions[0].target == "notepad", "Memory overrode a configured name"
    assert asked == [], "Memory was consulted for a name configuration already knew"


@pytest.mark.parametrize("state", ["missing", "corrupt", "wiped"])
def test_a_memory_failure_never_changes_a_deterministic_command(memory_at, executor, state):
    """Items 6, 7 and 8, and §14. "open notepad" is deterministic, so it must behave identically
    whatever has happened to memory.db - and a Memory failure must NOT make it fall through to the
    Brain, because that would spend a request for nothing."""
    if state == "corrupt":
        memory_at.parent.mkdir(parents=True, exist_ok=True)
        memory_at.write_bytes(b"SQLite format 3\x00" + b"\x3f" * 300)
    elif state == "wiped":
        memory.initialize()
        memory.wipe()

    reply, _context = run("open notepad", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN, reply.message
    assert [(action.kind, action.target) for action in executor.actions] == [(OPEN_APP, "notepad")]


@pytest.mark.parametrize("state", ["missing", "corrupt", "wiped"])
def test_an_alias_line_falls_back_to_the_brain_when_memory_cannot_answer(memory_at, executor, state):
    """The complement: with no usable Memory, "open editor" is simply an unknown app again - the
    behaviour before this slice existed. It is not an error, and nothing is executed."""
    if state == "corrupt":
        memory_at.parent.mkdir(parents=True, exist_ok=True)
        memory_at.write_bytes(b"SQLite format 3\x00" + b"\x3f" * 300)
    elif state == "wiped":
        memory.initialize()
        memory.wipe()

    reply, _context = run("open editor", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE, reply.message
    assert reply.message == brain.UNAVAILABLE_MESSAGE
    assert executor.calls == []


def test_the_alias_branch_is_only_reached_for_an_unknown_app(remembered, executor, monkeypatch):
    """Memory is not consulted for anything else: not an empty target, not a non-app line, not a
    settings problem. The branch exists for exactly one question."""
    asked = []
    monkeypatch.setattr(console.memory_queries, "application",
                        lambda alias, keys: asked.append(alias) or NotFound("x"))
    lines = (
        "open", "close",                     # an app verb with no target: config said "which app?"
        "scroll down 999", "shortcut ctrl+zzz", "click 1,",   # parse to a NON-app kind, then fail to
        "type " + "x" * 2000,                                 # resolve - Memory answers none of these
        "nonsense words here", "minimize", "type hello",
    )
    for line in lines:
        run(line, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert asked == [], f"Memory was consulted for {asked}"


# =======================================================================================================
# 2. THE BRAIN / PLANNER QUERY BOUNDARY
# =======================================================================================================

def test_the_kind_check_is_now_the_guard_not_defence_in_depth():
    """This test used to record that the kind check was REDUNDANT, because RESOLVE_UNKNOWN_APP had
    exactly one emitter - _resolve_app - so no non-app action could ever carry that reason.

    Phase 5 Slice 3 ended that. _resolve_click_target refuses an unknown app with the same reason, so
    the reason alone no longer implies "this is an open or close". The kind check is now what stops an
    Applications alias being used to resolve a UI CONTROL name, which Phase 5 forbids outright: app
    aliases map a user's app name to a configured app, and they are not a control-synonym table.

    The redundancy that was recorded here as a deliberate, cost-free choice is exactly what made that
    change safe. It is now load-bearing, and tested as such below."""
    import ast as _ast
    source = (settings.PROJECT_ROOT / "app" / "executor" / "logic.py").read_text(encoding="utf-8")
    emitters = sorted(node.name for node in _ast.walk(_ast.parse(source))
                      if isinstance(node, _ast.FunctionDef)
                      and "Unresolved(RESOLVE_UNKNOWN_APP" in _ast.unparse(node))
    assert emitters == ["_resolve_app", "_resolve_click_target"], emitters

    # The kind check, read from the branch itself: only these two kinds may consult Memory.
    branch = next(node for node in _ast.walk(_ast.parse(
        (settings.PROJECT_ROOT / "app" / "console.py").read_text(encoding="utf-8")))
        if isinstance(node, _ast.FunctionDef) and node.name == "_remembered_app")
    unparsed = _ast.unparse(branch)
    assert "parsed.kind not in (OPEN_APP, CLOSE_APP)" in unparsed, unparsed
    assert "CLICK_TARGET" not in unparsed


def test_an_unknown_app_on_a_named_click_never_consults_applications_memory(remembered, executor):
    """Phase 5 forbids Applications aliases as UI-control aliases. A named click whose app is unknown
    gets the resolver's own refusal and asks Memory nothing - proved by driving the real branch."""
    from app.executor.models import CLICK_TARGET, RESOLVE_UNKNOWN_APP
    from types import SimpleNamespace
    action = ExecutorAction(CLICK_TARGET, "notanapp", "Seven")
    resolution = resolve(action)
    assert isinstance(resolution, Unresolved) and resolution.reason == RESOLVE_UNKNOWN_APP

    asked = []
    route = SimpleNamespace(parsed=action, reason=brain.UNRESOLVED_TARGET)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(console.memory_queries, "application",
                      lambda *a, **k: asked.append(a) or None)
        assert console._remembered_app(route) is None
    assert asked == [], "Memory was consulted about a UI control name"


def test_a_broken_app_configuration_degrades_instead_of_crashing(remembered, executor, monkeypatch):
    """The branch's own fail-safe. Configuration is read twice on this path - once by the resolver and
    once to hand Memory the configured set - so a configuration that becomes unusable in between must
    make the branch step aside, not let a SettingsError escape the console."""
    def broken():
        raise SettingsError("executor.apps is unusable")

    monkeypatch.setattr(console, "configured_app_names", broken)
    reply, _context = run("open editor", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE, reply.message
    assert executor.calls == []


def test_the_frozen_happy_path_resolves_ali_through_the_query_boundary(remembered):
    """Items 9 and 32. The frozen Happy path: "message my friend Ali" resolves Ali from the People
    table. What the boundary receives is name="Ali", relationship="friend" - the sentence is not parsed
    here, and will be translated by the Brain in a later slice."""
    found = queries.person(name="Ali", relationship="friend")
    assert isinstance(found, Found), getattr(found, "message", found)
    assert found.value.name == "Ali"

    reachable = queries.contact(found.value.id)
    assert isinstance(reachable, (Found, Redacted)), reachable
    assert queries.MESSAGING_UNSUPPORTED == "I can't send messages yet."


def test_messaging_is_still_not_a_capability(remembered):
    """Item 14, and §3. Resolving Ali is where Phase 4 stops: nothing was added that could send."""
    from app.brain.models import ARGS_FOR_KIND
    from app.planner.models import TYPED_CONSOLE, VOICE_CONSOLE
    for invented in ("send_message", "message", "whatsapp", "sms", "email"):
        assert invented not in ARGS_FOR_KIND
        assert invented not in TYPED_CONSOLE.may_plan and invented not in VOICE_CONSOLE.may_plan
    assert not hasattr(queries, "send_message")


def test_the_query_boundary_uses_memory_logic_and_not_sqlite(remembered):
    """Item 10, and §4. Brain- and Planner-side code reaches lookup semantics, never a database."""
    source = (settings.PROJECT_ROOT / "app" / "memory" / "queries.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "sqlite3" not in imported
    assert "app.memory.adapter" not in imported, "the boundary reaches past logic.py"
    assert {"app.memory", "app.memory.models"} <= imported, imported


def test_neither_brain_nor_planner_touches_sqlite_or_the_memory_adapter():
    """§4's hard rules, over the real modules."""
    for module in ("brain/logic.py", "brain/adapter.py", "brain/models.py", "brain/interpreter.py",
                   "planner/logic.py", "planner/models.py"):
        source = (settings.PROJECT_ROOT / "app" / module).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert "sqlite3" not in imported, module
        assert "app.memory.adapter" not in imported, module


def test_the_planner_cannot_change_memory_while_planning():
    """§4. Planning is a pure transition over a TurnContext; it has no Memory import at all, so a plan
    cannot write, delete or learn anything as a side effect."""
    for module in ("planner/logic.py", "planner/models.py"):
        source = (settings.PROJECT_ROOT / "app" / module).read_text(encoding="utf-8")
        assert "app.memory" not in source, module


def test_two_people_called_ali_are_ambiguous_at_the_boundary(remembered):
    """Item 11, and the frozen Ambiguous case. No guess, and the model is never asked to choose."""
    second = memory.add_person("Ali").value.id
    memory.add_relationship(second, "friend")

    outcome = queries.person("Ali", "friend")
    assert isinstance(outcome, Ambiguous), outcome
    assert len(outcome.candidates) == 2
    assert "guess" in outcome.message


def test_an_unknown_person_is_a_clear_local_answer(remembered):
    """Item 12, and the frozen Missing-info case."""
    outcome = queries.person("Zubair")
    assert isinstance(outcome, NotFound)
    assert outcome.message == "I don't know who that is."


def test_a_local_memory_query_needs_no_provider(remembered):
    """Items 13 and 15. The provider guard is armed for every test, so a request would raise; and
    nothing here hands a resolved value to anything that could send it."""
    assert isinstance(queries.person("Ali", "friend"), Found)
    assert isinstance(queries.application("editor", configured_app_names()), Found)


def test_no_memory_value_can_reach_a_provider_request(remembered):
    """Item 15 and §5, the critical one. The Brain's request builders take the user's text and at most a
    PreviousActionContext; there is no parameter through which a person, a contact, a path or a
    correction could travel, and the Brain imports no Memory module."""
    import inspect
    source = inspect.getsource(brain)
    assert "app.memory" not in source and "memory" not in {
        name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
        for name in [(node.module or "").split(".")[0]]}

    built = brain.interpretation_request("could you open the editor", None)
    for secret in (ADDRESS, "Ali", "friend", "whatsapp"):
        assert secret not in built, f"{secret!r} reached a provider request"


def test_a_resolved_alias_does_not_put_memory_into_a_later_prompt(remembered, executor):
    """A line resolved from Memory runs locally; a LATER Brain line must carry nothing from it except
    the existing PreviousActionContext (kind + safe target), which is a configured app name."""
    requests = []

    def record(prompt):
        requests.append(prompt)
        return understood(Intent(OPEN_APP, OpenAppArgs("notepad")))

    reply, context = run("open editor", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN
    run("could you do that again please", context=context, script=Script("no"), interpret=record)

    assert len(requests) == 1
    for secret in (ADDRESS, "Ali", "friend", "whatsapp", "editor"):
        assert secret not in requests[0], f"{secret!r} reached the provider"
    assert "notepad" in requests[0], "the existing safe context is still sent"


# =======================================================================================================
# 3. SENSITIVE DATA THROUGH THE INTEGRATION BOUNDARY
# =======================================================================================================

def test_a_denied_address_stays_redacted_at_the_boundary(remembered, caplog):
    """Items 16, 17 and 36, and the frozen Permission-denied case. The rule is written against the
    boundary's own context, so a reasoning-side lookup cannot see the address."""
    assert isinstance(memory.set_sensitive_rule("contacts", "address", queries.BRAIN, "deny"), Found)
    ali = queries.person("Ali", "friend").value.id

    with caplog.at_level(logging.DEBUG):
        outcome = queries.contact(ali)
    assert isinstance(outcome, Redacted), outcome
    assert outcome.value.address is None and outcome.value.disclosed is False

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for surface in (repr(outcome), str(outcome), outcome.message, repr(outcome.value), logged):
        assert ADDRESS not in surface and "0000001" not in surface, surface


def test_the_boundary_always_supplies_a_disclosure_context(remembered):
    """Item 18, and §7. There is no way to call the contact query without a context, so a caller cannot
    omit one and be handed a value the rules meant to withhold."""
    import inspect
    source = inspect.getsource(queries.contact)
    assert "context=BRAIN" in source, "the boundary does not pin a context"
    signature = inspect.signature(memory.find_contact)
    assert signature.parameters["context"].default is inspect.Parameter.empty, (
        "find_contact gained a default context, which would let a caller forget it")


def test_nothing_in_the_integration_unwraps_a_redacted_result():
    """Item 18's structural half: no caller reaches into a Redacted to recover the field.

    REWRITTEN FOR THE PHASE 4 WIRING SLICE, which added the one legitimate place an address is read -
    the recall answer, which exists to show the user their own value. A substring ban on ".value.address"
    can no longer tell that read from a Redacted one (FOURTEENTH time a text search in this project has
    matched something it was not written for), so the rule is checked where it actually lives: the only
    function that touches the field returns early for everything that is not Found, and the guard comes
    BEFORE the access in the body.

    The behavioural half - a deny rule on the user context really does withhold the recall answer - is
    tests/test_memory_brain_wiring.py::test_a_deny_rule_for_the_user_withholds_the_recall_answer."""
    assert ".value.address" not in (settings.PROJECT_ROOT / "app" / "memory" / "queries.py").read_text(
        encoding="utf-8"), "the query boundary must not unwrap a result at all"

    tree = ast.parse((settings.PROJECT_ROOT / "app" / "console.py").read_text(encoding="utf-8"))

    def unwraps(node):
        """Reading the address OUT OF A RESULT - `<result>.value.address` - which is the thing Redacted
        exists to prevent. `request.address` is the value the user just typed on their way IN, which is
        a different direction and not what this rule is about."""
        return (isinstance(node, ast.Attribute) and node.attr == "address"
                and isinstance(node.value, ast.Attribute) and node.value.attr == "value")

    readers = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and any(unwraps(inner) for inner in ast.walk(node))}
    assert readers == {"_recall_contact"}, f"something else now unwraps an address: {readers}"

    recall = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_recall_contact")
    guards = [statement.lineno for statement in ast.walk(recall)
              if isinstance(statement, ast.Call) and ast.unparse(statement.func) == "isinstance"
              and "MemoryFound" in ast.unparse(statement)]
    access = min(node.lineno for node in ast.walk(recall) if unwraps(node))
    assert guards and max(guards) < access, "the Found check no longer precedes the read"


# =======================================================================================================
# 4. THE PERSISTENCE / SENSITIVITY DECISION GATE
# =======================================================================================================

def a_correction(right="button-B"):
    return Correction(SCOPE, WRONG, right)


def correction_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT level, right_value, occurrences FROM corrections ORDER BY id").fetchall()


def test_a_durable_write_without_a_decision_is_impossible(remembered):
    """Item 19, and §8. The argument is required and has no default, so a caller that has not decided
    cannot persist anything - it is a TypeError, not a quiet success."""
    with pytest.raises(TypeError):
        memory.remember_explicitly(a_correction())
    with pytest.raises(TypeError):
        memory.observe_correction(CorrectionTask("t"), CorrectionLearner(), a_correction())
    assert correction_rows(remembered) == []


def test_an_explicit_write_with_deny_is_refused(remembered):
    """Item 20."""
    outcome = memory.remember_explicitly(a_correction(), DENY)
    assert isinstance(outcome, NotFound)
    assert outcome.message == memory.NOT_APPROVED
    assert correction_rows(remembered) == []


def test_an_explicit_write_with_allow_persists(remembered):
    """Item 21."""
    assert isinstance(memory.remember_explicitly(a_correction("button-Z"), ALLOW), Found)
    assert correction_rows(remembered) == [("explicit", "button-Z", 1)]


def test_a_learned_promotion_without_allow_never_persists(remembered):
    """Item 22. Twice with DENY is still nothing durable - and the observation is not even counted, so a
    later approved correction cannot promote on evidence nobody agreed to keep."""
    learner = CorrectionLearner()
    for name in ("task-A", "task-B", "task-C"):
        task, outcome = memory.observe_correction(CorrectionTask(name), learner, a_correction(), DENY)
        assert isinstance(outcome, NotFound) and outcome.message == memory.NOT_APPROVED
        assert task.correction_for(SCOPE, WRONG) is not None, "the current task lost its fix"
    assert correction_rows(remembered) == []
    assert learner.count(a_correction()) == 0, "unapproved evidence was counted"


def test_a_learned_promotion_with_allow_persists_on_the_threshold(remembered):
    """Item 23. The Slice 3 behaviour is unchanged once the decision is given."""
    learner = CorrectionLearner()
    _task, first = memory.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    assert isinstance(first, NotFound), "one is not a pattern"
    assert correction_rows(remembered) == []

    _task, second = memory.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)
    assert isinstance(second, Found) and second.value == 2
    assert correction_rows(remembered) == [("learned_pattern", "button-B", 2)]


def test_a_temporary_correction_needs_no_approval_at_all(remembered):
    """Item 24, and §8's last line. Level 1 is not kept, so there is nothing to approve - and it works
    even with no database."""
    task = memory.correct_for_this_task(CorrectionTask("task-A"), a_correction("button-W"))
    assert memory.resolve_value(SCOPE, WRONG, task=task).value == "button-W"
    assert correction_rows(remembered) == []


def test_the_decision_carries_no_value_of_its_own():
    """Item 25, and §8: do not build a classifier. The decision is two words, so it can never become a
    place a secret is passed through."""
    assert {decision.value for decision in PersistenceDecision} == {"allow", "deny"}
    for decision in PersistenceDecision:
        assert isinstance(decision.value, str) and len(decision.value) <= 5
        assert not hasattr(decision, "payload") and not hasattr(decision, "text")


def test_the_console_still_holds_no_phrase_vocabulary_of_its_own():
    """§9, REWRITTEN for the Phase 4 wiring slice, which was approved to recognise a typed "remember".

    The original form of this test asserted that no remember phrase appeared in app/console.py at all.
    That is no longer true by design, so what is pinned instead is the part that still is, and still
    matters: the WORDS live in app/brain/personal_memory.py, and the console holds none of them. A
    second vocabulary in a second place is how two parsers drift apart and one of them stops being
    fail-closed.

    The correction-level writes are still not wired to the console, which is unchanged and still
    checked: remember_explicitly() and observe_correction() belong to app/memory's own API."""
    source = (settings.PROJECT_ROOT / "app" / "console.py").read_text(encoding="utf-8")
    literals = {node.value.lower() for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    vocabulary = set(personal_memory.KEEP_MARKERS) | set(personal_memory.ASK_MARKERS) \
        | set(personal_memory.CHANNELS)
    assert not (literals & vocabulary), f"the console grew its own phrases: {literals & vocabulary}"
    assert "remember_explicitly" not in source, "a correction write path was wired into the console"
    assert "observe_correction" not in source


def test_no_broad_write_integration_reached_the_console():
    """§15. Only the required gate; no People, contact or correction writing from the console."""
    source = (settings.PROJECT_ROOT / "app" / "console.py").read_text(encoding="utf-8")
    for written in ("add_person", "add_contact", "add_relationship", "set_sensitive_rule",
                    "delete_entry", "wipe", "export_memory", "restore_memory"):
        assert written not in source, written


# =======================================================================================================
# 5. D6 - FULL APPLICATION-LEVEL PROOF
# =======================================================================================================

def brain_plan():
    return understood(Intent(OPEN_APP, OpenAppArgs("notepad")))


@pytest.mark.parametrize("state", ["valid", "wiped", "missing", "corrupt"])
def test_the_companion_still_works_whatever_has_happened_to_memory(memory_at, executor, state):
    """Items 26-29, and the frozen Done-when's "a full memory wipe leaves the app functional (just
    memoryless), not broken" - now proven at APPLICATION level, which Slice 1 deliberately deferred
    until Memory was actually in a real path.

    A: valid   B: wiped   C: missing   D: corrupt"""
    if state == "valid":
        memory.initialize()
    elif state == "wiped":
        memory.initialize()
        memory.wipe()
    elif state == "corrupt":
        memory_at.parent.mkdir(parents=True, exist_ok=True)
        memory_at.write_bytes(b"SQLite format 3\x00" + b"\x2a" * 300)

    # the deterministic Phase 0/1 path
    reply, _context = run("open notepad", interpret=provider_that_must_not_be_called)
    assert reply.status is Status.RAN, f"{state}: {reply.message}"
    assert [(action.kind, action.target) for action in executor.actions] == [(OPEN_APP, "notepad")]

    # the typed Brain path, through the scripted provider
    reply, context = run("could you open notepad for me please", script=Script("yes"),
                         interpret=Brainless(brain_plan()))
    assert reply.status is Status.RAN, f"{state}: {reply.message}"
    assert len(executor.actions) == 2


@pytest.mark.parametrize("state", ["missing", "corrupt"])
def test_a_memory_dependent_lookup_reports_unavailable_rather_than_crashing(memory_at, state):
    """Item 30, and the frozen Recovery case: a clear result with a way forward, not an exception."""
    if state == "corrupt":
        memory_at.parent.mkdir(parents=True, exist_ok=True)
        memory_at.write_bytes(b"SQLite format 3\x00" + b"\x2a" * 300)

    for call in (lambda: queries.person("Ali"),
                 lambda: queries.contact(1),
                 lambda: queries.application("editor", configured_app_names())):
        outcome = call()
        assert isinstance(outcome, MemoryUnavailable), outcome
        assert outcome.recovery, "no way forward was offered"
        assert "restore" in outcome.message


def test_a_wiped_memory_answers_cleanly_rather_than_failing(memory_at):
    """B's Memory half: an empty database is a working database. The alias is simply not known."""
    memory.initialize()
    memory.wipe()
    assert isinstance(queries.application("editor", configured_app_names()), NotFound)
    assert isinstance(queries.person("Ali"), NotFound)


def test_a_read_never_creates_a_database(memory_at, executor):
    """Item 31. Nothing is initialised behind the user's back just because something looked."""
    assert not memory_at.exists()
    run("open editor", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    queries.person("Ali")
    queries.application("editor", configured_app_names())
    assert not memory_at.exists(), "a read created a memory database"


# =======================================================================================================
# 6. THE FROZEN PHASE 4 TEST CASES
# =======================================================================================================

def test_frozen_wrong_input_a_one_off_correction_affects_only_this_task(remembered):
    """Item 33, and the frozen Wrong-input case: "a correction to a wrong resolution ('no, the other
    Ali') fixes the current command only". Structured, not parsed - and nothing durable."""
    task = memory.correct_for_this_task(CorrectionTask("task-A"),
                                        Correction("person:ali", "Ali #1", "Ali #2"))
    assert memory.resolve_value("person:ali", "Ali #1", task=task).value == "Ali #2"
    assert memory.resolve_value("person:ali", "Ali #1", task=task.ended()).value == "Ali #1"
    assert memory.resolve_value("person:ali", "Ali #1").value == "Ali #1"
    assert correction_rows(remembered) == [], "a one-off correction persisted"


def test_frozen_recovery_gives_a_clear_result_and_a_way_forward(memory_at):
    """Item 37, and the frozen Recovery case: "a corrupted/missing memory.db produces a clear error and
    a path to restore from export, not a silent crash"."""
    for prepare in (lambda: None,
                    lambda: memory_at.write_bytes(b"SQLite format 3\x00" + b"\x19" * 300)):
        memory_at.parent.mkdir(parents=True, exist_ok=True)
        prepare()
        outcome = memory.open_memory()
        assert isinstance(outcome, MemoryUnavailable)
        assert "restore" in outcome.message and "fresh" in outcome.message


def test_frozen_emergency_stop_is_not_applicable_and_was_not_invented():
    """Item 38. Frozen Step 4 says of Phase 4: "Emergency stop - N/A for this phase - no live action is
    running during a pure memory lookup". So there is no live-action memory test here, and Memory has no
    path to one: it cannot produce an action and imports no Executor.

    The stop itself is unchanged and still covered by its own Phase 1 and Phase 3 tests."""
    for name in ("logic.py", "models.py", "adapter.py", "queries.py"):
        source = (settings.PROJECT_ROOT / "app" / "memory" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not any(module.startswith("app.executor") for module in imported), name
        assert "emergency_stop" not in source, name


# =======================================================================================================
# 7. PHASE 1-3 BEHAVIOUR IS UNCHANGED
# =======================================================================================================

def test_the_routing_table_is_unchanged_by_the_integration(remembered):
    """§12. brain.route() still answers exactly as before: the alias branch lives ABOVE it in the
    console and changes no routing decision."""
    for text, expected in (("open notepad", brain.LocalAction), ("open editor", brain.BrainEligible),
                           ("type hello world", brain.LocalAction), ("", brain.LocalRefusal),
                           ("open the calculator", brain.BrainEligible)):
        assert isinstance(brain.route(text, resolve), expected), text


def test_is_brain_eligible_stayed_pure(remembered):
    """It is documented as making no provider call and changing no context, and it is what the voice
    console asks on its own behalf. Memory was deliberately NOT added to it, so it stays a pure
    parse-and-resolve question; the alias is resolved later, in handle_typed_line, still for free."""
    import inspect
    source = inspect.getsource(console.is_brain_eligible)
    assert "memory" not in source.lower()
    assert console.is_brain_eligible("open editor") is True
    assert console.is_brain_eligible("open notepad") is False


def test_previous_action_context_is_unchanged(remembered):
    """Item 43, and §12."""
    from app.brain.models import PreviousActionContext, previous_action_context
    assert previous_action_context(OPEN_APP, "notepad") == PreviousActionContext(OPEN_APP, "notepad")
    assert previous_action_context("type_text", "my diary password").safe_target is None
    source = (settings.PROJECT_ROOT / "app" / "brain" / "models.py").read_text(encoding="utf-8")
    assert "app.memory" not in source


def test_the_consoles_whole_memory_surface_is_enumerated():
    """Each memory branch is reachable from exactly ONE place, and the set of memory calls is CLOSED.

    The alias branch was the only one when this was written; the Phase 4 wiring slice added the local
    remember and recall. The value of the test is unchanged and is the reason it is kept rather than
    relaxed: a fifth way into Memory from the console cannot be added without this failing, so no
    branch can appear that nobody audited."""
    tree = ast.parse((settings.PROJECT_ROOT / "app" / "console.py").read_text(encoding="utf-8"))
    calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    # Every function that DOES something: reachable from exactly one place.
    for once in ("_remembered_app", "_personal_memory", "_remember_contact", "_recall_contact",
                 "_start_memory"):
        assert calls.count(once) == 1, f"{once} is reachable from more than one place"
    # _is_start_memory is deliberately NOT in that list: it is a pure predicate with no side effect,
    # and it is asked at both routing points on purpose - once by handle_typed_line and once by
    # is_fresh_command, which is what makes the command work at a nested prompt as well as at ">".
    assert calls.count("_is_start_memory") == 2, "the command lost one of its two routing points"
    reached = sorted({name for name in calls if "memory" in name.lower()})
    assert reached == ["MEMORY_STARTED.format", "_is_start_memory", "_personal_memory",
                       "_start_memory", "memory_logic.remember_contact",
                       "memory_logic.start_fresh_memory", "memory_queries.application",
                       "memory_queries.contact_for_user", "memory_queries.person",
                       "personal_memory.is_covered", "personal_memory.recognise"], reached
