"""
PHASE 4 WIRING: a typed "remember" and a typed "recall", answered locally and never sent anywhere.

THE THING BEING PROVEN IS A NEGATIVE, so it is proven in several independent places rather than once.
app/brain/logic.py sends the user's line to the provider verbatim - interpretation_request() quotes it,
replan_request() carries it again along with the correction text, clarification_request() carries the
clarification answer - so "remember Ali's whatsapp is +92-300-0000001" could leave this machine by three
separate routes, and two of them are nested prompts rather than the main path. Local retrieval alone
would have guaranteed nothing.

WHAT IS ASSERTED, AND WHY EACH ONE IS NOT THE OTHER:

  1. zero provider calls - with a fake that RAISES if it is called at all, so a call cannot be absorbed
  2. the address is in no provider request, in a session where a replan request really was built (there
     is a control test that proves that path is live, because an assertion about a path that never
     executed proves nothing)
  3. the value never becomes an ExecutorAction target, an ActionResult, a plan step or a log line
  4. the write gate has no default, and DENY stores nothing
  5. the four integrity cases: a repeat, a conflicting second address, two people with one name, and a
     failed second write
  6. a Roman Urdu remember is STORED, not merely refused - the trailing-marker shape is the one the
     owner actually types

Everything is offline. The provider is a function, the Executor is replaced, and every database lives
under tmp_path behind the repository's central SQLite guard.
"""
import ast
import inspect
import logging
import sqlite3

import pytest

from app import console
from app.brain import personal_memory
from app.console import Status
from app.executor.logic import configured_app_names
from app.memory import logic as memory
from app.memory import queries
from app.memory.models import (Ambiguous, Found, MemoryDatabase, MemoryUnavailable, NotFound,
                               PersistenceDecision)
from app.planner.models import VOICE_CONSOLE
from tests.test_console_brain import (Brainless, Script, executor, no_stop,  # noqa: F401
                                      open_notepad, run, understood)

ALLOW, DENY = PersistenceDecision.ALLOW, PersistenceDecision.DENY
ADDRESS = "+92-300-0000001"
OTHER_ADDRESS = "+92-300-0000002"
# The shapes the owner actually types. The Urdu one puts the marker at the END, which is the whole
# reason the guard looks at both ends of the line.
ENGLISH = f"remember Ali's whatsapp is {ADDRESS}"
URDU = f"Ali ka whatsapp {ADDRESS} yaad rakho"
LOOSE = "could you open notepad for me please"


class NoFocus:
    def note_console_window(self):
        pass

    def hand_over(self, *args, **kwargs):
        return None


@pytest.fixture
def memory_at(tmp_path, monkeypatch):
    """Point the real Memory logic at this test's own database, without creating it."""
    path = tmp_path / "memory" / "memory.db"
    monkeypatch.setattr(memory, "database_path", lambda: path)
    return path


@pytest.fixture
def empty_memory(memory_at):
    """An initialised, empty memory with the application alias the console path needs."""
    assert isinstance(memory.initialize(), MemoryDatabase)
    assert isinstance(memory.remember_application_alias("editor", "notepad", configured_app_names()),
                      Found)
    return memory_at


def must_not_be_called(prompt):
    raise AssertionError("a line answered from local memory must not cost a provider call")


def rows(path, table):
    with sqlite3.connect(path) as database:
        return database.execute(f"SELECT * FROM {table}").fetchall()


def addresses(path):
    return [row[3] for row in rows(path, "contacts")]


# =======================================================================================================
# 1. IT COSTS NOTHING, AND AN UNPARSEABLE ONE STILL COSTS NOTHING
# =======================================================================================================

def test_a_typed_remember_is_stored_without_a_provider_call(empty_memory):
    """The feature, and the first guarantee. The fake provider RAISES, so "no call" is not a count that
    a stray call could be absorbed into."""
    reply, _context = run(ENGLISH, interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED, reply.message
    assert addresses(empty_memory) == [ADDRESS]
    assert [row[1] for row in rows(empty_memory, "people")] == ["Ali"], "the user's own spelling"
    # FOUND BY MUTATION. The write path applies no disclosure rule, so it is not a place that may
    # disclose: the confirmation says what happened and does not read the value back.
    assert ADDRESS not in reply.message, reply.message
    assert "whatsapp" in reply.message, reply.message


def test_a_roman_urdu_remember_is_stored_and_not_merely_refused(empty_memory):
    """SOV word order, and the owner's requirement that a natural Roman Urdu remember SUCCEEDS.

    "Ali ka whatsapp <number> yaad rakho" carries its marker at the end. A guard that only looked at the
    start of the line would not have recognised it - and would have sent the number to the provider."""
    reply, _context = run(URDU, interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED, reply.message
    assert addresses(empty_memory) == [ADDRESS]


@pytest.mark.parametrize("line", [
    "remember to call Ali tomorrow",                 # a reminder, not a fact: refused, by design
    "remember my work folder is C:/Users/Wajid/work",  # out of this slice entirely, so refused
    "remember my password is hunter2",               # no channel word, so never parsed as a contact
    "note that Ali",
    "save that",
    "remember",
    "yaad rakho",
    f"Ali ka whatsapp {ADDRESS} aur email bhi yaad rakho",
])
def test_a_remember_this_grammar_cannot_parse_is_refused_locally_and_never_sent(line, empty_memory):
    """THE FAIL-CLOSED GUARD, which is the part that cannot be got wrong.

    Each of these carries a marker meaning "keep this" and none of them parses. The rule is that such a
    line is refused HERE - it never continues to the Brain - so the cost of an unparsed remember is a
    retype and never a leak. The owner accepted that cost explicitly: a refusal costs a retype, a leak
    cannot be undone."""
    reply, _context = run(line, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED, reply.message
    assert rows(empty_memory, "contacts") == [], "a refusal must not store anything either"


@pytest.mark.parametrize("line", ["open the file I remembered yesterday",
                                  "I will note that later",
                                  "tell me what remembering means"])
def test_a_marker_word_inside_a_sentence_is_not_a_remember(line, empty_memory):
    """FOUND BY MUTATION. _leading() matches on a word boundary at the START of the line, and nothing
    proved it: a mutant that matched the marker anywhere in the line survived.

    The mutant's direction was fail-CLOSED, so nothing could have leaked from it - what it would have
    cost is usability, by refusing ordinary sentences that merely mention remembering. That is still
    worth pinning, because the fail-closed guard is only acceptable while it stays narrow."""
    assert personal_memory.is_covered(line) is False
    assert personal_memory.recognise(line) is None


def test_a_question_that_does_not_parse_still_reaches_the_brain(empty_memory):
    """READS FALL THROUGH, and that asymmetry is deliberate. A question carries only the words the user
    typed - nothing stored is in it yet - so an unrecognised one behaves exactly as it did before this
    slice rather than being refused."""
    brainless = Brainless(understood(open_notepad()))
    reply, _context = run("what is the weather", interpret=brainless, script=Script("cancel"))
    assert brainless.calls == 1, "an unparsed question must still be the Brain's to answer"
    assert reply.status is not Status.REFUSED


# =======================================================================================================
# 2. THE VALUE IS IN NO PROVIDER REQUEST - INCLUDING FROM THE NESTED PROMPTS
# =======================================================================================================

def failing_step(monkeypatch):
    """Make every executed action fail, which is what reaches the correction prompt."""
    from app.executor.models import ActionResult

    def failing(action, confirm=None, offer_retry=None, *, risk_floor=None):
        return ActionResult(action, False, "it didn't open")

    monkeypatch.setattr(console, "execute_with_recovery", failing)


def session(monkeypatch, *answers, provider=None):
    """One real run_console session, with the real orchestration and a scripted provider."""
    brainless = Brainless(understood(open_notepad()), understood(open_notepad())) \
        if provider is None else provider
    monkeypatch.setattr(console.interpreter, "interpret", brainless)
    script = Script(*answers)
    code = console.run_console(read=script.read, write=script.write, focus=NoFocus())
    return code, script, brainless


def test_the_correction_prompt_really_does_send_what_is_typed_there(monkeypatch, empty_memory):
    """THE CONTROL, and without it the next test proves nothing.

    This is the leak path, demonstrated live: ordinary prose typed at the correction prompt becomes a
    SECOND provider request. So the prompt is reached, a replan really is built, and the next test's
    "no second request" is a difference the guard made rather than a path that never ran."""
    failing_step(monkeypatch)
    _code, _script, brainless = session(monkeypatch, LOOSE, "yes", "use the other notepad", "exit")
    assert brainless.calls == 2, "the correction prompt did not reach the provider at all"
    assert "use the other notepad" in repr(brainless.requests[1]), repr(brainless.requests[1])


def test_a_remember_typed_at_the_correction_prompt_is_never_sent(monkeypatch, empty_memory):
    """THE SAME PROMPT, THE SAME SESSION SHAPE, one line different - and the number does not leave.

    The remember line is handed back to the loop instead of being consumed as correction text, so no
    replan request is built at all, and the address appears in nothing the provider was given. It is
    then stored, because handing it back means it runs as the command it always was."""
    failing_step(monkeypatch)
    _code, script, brainless = session(monkeypatch, LOOSE, "yes", ENGLISH, "exit")
    assert brainless.calls == 1, f"a replan was built from a remember line: {brainless.requests[1:]}"
    assert ADDRESS not in repr(brainless.requests), "the address reached a provider request"
    assert addresses(empty_memory) == [ADDRESS], f"it was not stored either: {script.output}"


def test_a_remember_typed_at_the_clarification_prompt_is_never_sent(monkeypatch, empty_memory):
    """The second nested prompt, which is a separate provider call with its own builder.

    A clarification answer goes into clarification_request(). The same guard covers it, because
    is_fresh_command() is what all three prompts ask."""
    from app.brain.models import NeedsClarification

    brainless = Brainless(NeedsClarification(question="which browser?", missing="app"),
                          understood(open_notepad()))
    _code, _script, used = session(monkeypatch, "open the browser", ENGLISH, "exit", provider=brainless)
    assert used.calls == 1, f"the clarification answer was sent: {used.requests[1:]}"
    assert ADDRESS not in repr(used.requests)
    assert addresses(empty_memory) == [ADDRESS]


def test_without_a_console_loop_a_remember_is_abandoned_rather_than_consumed(empty_memory):
    """THE VOICE SHAPE. app/voice_console.py owns no loop, so there is nowhere to hand a line back to -
    and the pre-existing rule was that a line typed at a prompt without a loop is consumed as the
    answer, which for this one line is exactly what sends it.

    So this is the single documented exception to that rule: a covered line is never an answer. It
    abandons the question instead, which costs the user a retype and sends nothing."""
    assert console.classify_answer(ENGLISH, console.FREE_TEXT, handoff=False) is console.Answer.ABANDON
    assert console.classify_answer(URDU, console.CLARIFICATION, handoff=False) is console.Answer.ABANDON
    # And nothing else moved: ordinary prose with no loop is still an ordinary answer.
    assert console.classify_answer("use the other ali", console.FREE_TEXT,
                                   handoff=False) is console.Answer.ANSWER


def test_voice_cannot_store_a_contact_and_cannot_send_one_either(empty_memory):
    """Phase 2's closeout records that unclear speech comes back as other words entirely, so a spoken
    phone number is not something to store quietly. Refused - and refused LOCALLY, which is the part
    that matters: it is not forwarded to the Brain as a consolation."""
    reply, _context = run(ENGLISH, interpret=must_not_be_called, frontend=VOICE_CONSOLE)
    assert reply.status is Status.REFUSED
    assert reply.message == console.KEYBOARD_ONLY
    assert rows(empty_memory, "contacts") == []


# =======================================================================================================
# 3. NOT AN ACTION, NOT A RESULT, NOT A LOG LINE
# =======================================================================================================

def test_a_remembered_value_never_reaches_the_executor(empty_memory, executor):
    """Nothing is executed by either half of this feature, so there is no action to carry a value."""
    run(ENGLISH, interpret=must_not_be_called)
    run("what is Ali's whatsapp", interpret=must_not_be_called)
    assert executor.actions == [], "a local memory answer must not reach the Executor at all"


def test_the_recogniser_has_no_action_to_build(empty_memory):
    """BY CONSTRUCTION RATHER THAN BY CARE, which is what the owner asked for.

    app/brain/personal_memory.py imports nothing from app.executor, so there is no ExecutorAction type
    in scope and no action kind to name; and none of the results it returns has a `kind` or a `target`
    field, so a caller written for actions cannot pick one up by accident.

    Checked on the AST rather than by searching the text: a substring search over this project's source
    has matched its own prose 13 times, including inside the comment that explains the rule."""
    tree = ast.parse(inspect.getsource(personal_memory))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.startswith("app.executor") for name in imported), imported
    assert not any("anthropic" in name or name.startswith("app.brain.adapter") for name in imported)

    fields = {target.id
              for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
              for statement in node.body if isinstance(statement, ast.AnnAssign)
              for target in [statement.target] if isinstance(target, ast.Name)}
    assert fields and not {"kind", "target"} & fields, fields


def test_the_recall_answer_is_a_command_reply_and_not_an_action_result(empty_memory):
    """The owner's item 4. The answer carries the value because showing it back IS the answer - so what
    carries it must be the reply, and nothing that an Executor, a plan or a log can see."""
    run(ENGLISH, interpret=must_not_be_called)
    reply, _context = run("what is Ali's whatsapp", interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED
    assert ADDRESS in reply.message
    assert reply.action is None and reply.result is None, "it must not be an action or a result"


def test_nothing_about_a_remembered_contact_is_logged(empty_memory, caplog):
    """A log file is persistent, which is why it is checked separately from the provider requests.

    The test is not vacuous: Memory DOES log that one contact was remembered, and that record is
    asserted to exist. What it may not contain is the name or the address."""
    with caplog.at_level(logging.DEBUG):
        run(ENGLISH, interpret=must_not_be_called)
        run("what is Ali's whatsapp", interpret=must_not_be_called)
    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "remembered one whatsapp contact" in written, f"nothing was logged at all: {written}"
    assert ADDRESS not in written and "Ali" not in written, written


# =======================================================================================================
# 4. THE WRITE GATE
# =======================================================================================================

def test_the_persistence_decision_is_required_and_has_no_default():
    """The gate exists because Memory cannot tell a phone number from a password by looking at it, so a
    caller that has not thought about it must not be able to write at all. A default would be exactly
    that caller."""
    signature = inspect.signature(memory.remember_contact)
    decision = signature.parameters["decision"]
    assert decision.default is inspect.Parameter.empty, "the gate gained a default"
    assert decision.annotation is PersistenceDecision, "the gate must be the decision type itself"


def test_deny_stores_nothing_at_all(empty_memory):
    """Not the person either: a denied write leaves the database exactly as it was."""
    refused = memory.remember_contact("Ali", "whatsapp", ADDRESS, DENY)
    assert isinstance(refused, NotFound)
    assert refused.message == memory.NOT_APPROVED
    assert rows(empty_memory, "people") == [] and rows(empty_memory, "contacts") == []


# =======================================================================================================
# 5. THE FOUR INTEGRITY CASES
# =======================================================================================================

def test_the_same_remember_twice_creates_no_duplicate(empty_memory):
    """people has NO unique constraint and add_person() deliberately never merges, so a second call
    would create a second "Ali" and make every later lookup Ambiguous. The composition therefore has to
    look first."""
    first, _ = run(ENGLISH, interpret=must_not_be_called)
    second, _ = run(ENGLISH, interpret=must_not_be_called)
    assert first.status is Status.ANSWERED and second.status is Status.ANSWERED, second.message
    assert len(rows(empty_memory, "people")) == 1
    assert addresses(empty_memory) == [ADDRESS]


def test_a_different_address_on_the_same_channel_is_refused_and_names_no_value(empty_memory):
    """Overwriting silently would lose what the user said before; keeping both would degrade every
    later lookup to Ambiguous. So it is refused - and the refusal names the channel but NOT the stored
    address, because the write path applies no disclosure rule and so may not disclose."""
    run(ENGLISH, interpret=must_not_be_called)
    reply, _context = run(f"remember Ali's whatsapp is {OTHER_ADDRESS}", interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert "whatsapp" in reply.message and ADDRESS not in reply.message, reply.message
    assert addresses(empty_memory) == [ADDRESS], "the first one must survive untouched"


def test_a_withheld_address_is_still_compared_against(empty_memory):
    """FOUND BY MUTATION, and it was a real hole rather than a weak assertion.

    _channel_state() reads the contact rows directly instead of through find_contact(), and its
    docstring says why: find_contact() is the disclosure boundary and may leave the address OUT, which
    would make an existing contact read as "nothing stored" and let the comparison wave a second row
    through. Nothing proved it - no test had both a deny rule and a second remember - so the mutant that
    routed the comparison through the boundary survived.

    With BOTH contexts denied, a repeat must still be recognised as the same address and a conflicting
    one must still be refused. The disclosure rules govern what is SHOWN; they must not govern what is
    COMPARED."""
    run(ENGLISH, interpret=must_not_be_called)
    memory.set_sensitive_rule("contacts", "address", queries.BRAIN, "deny")
    memory.set_sensitive_rule("contacts", "address", queries.USER, "deny")

    again, _context = run(ENGLISH, interpret=must_not_be_called)
    assert again.status is Status.ANSWERED, again.message
    assert addresses(empty_memory) == [ADDRESS], "a withheld address was not compared against"

    conflicting, _context = run(f"remember Ali's whatsapp is {OTHER_ADDRESS}",
                                interpret=must_not_be_called)
    assert conflicting.status is Status.REFUSED, conflicting.message
    assert addresses(empty_memory) == [ADDRESS]


def test_two_people_with_one_name_refuses_rather_than_picking(empty_memory):
    """find_person() reports Ambiguous and this never resolves it by choosing. Attaching a phone number
    to the wrong person silently is the failure being avoided."""
    memory.add_person("Ali")
    memory.add_person("Ali")
    reply, _context = run(ENGLISH, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert isinstance(memory.remember_contact("Ali", "whatsapp", ADDRESS, ALLOW), Ambiguous)
    assert rows(empty_memory, "contacts") == []


def test_a_failed_contact_write_leaves_no_person_behind(empty_memory, monkeypatch):
    """THE COMPENSATION. The person row is committed by its own connection before the contact is
    attempted - there is no transaction around both - so a failed second write is undone by a third
    write that deletes the person. Without it, a failed remember would leave a person who is not a
    person, and the next attempt would find them and report success for half a write."""
    def refuses(*args, **kwargs):
        raise memory.adapter.MemoryAdapterError("disk is full")

    monkeypatch.setattr(memory.adapter, "insert_contact", refuses)
    reply, _context = run(ENGLISH, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED, reply.message
    assert "Remembered" not in reply.message
    assert rows(empty_memory, "people") == [], "the half-written person was left behind"


def test_a_failed_compensation_is_reported_rather_than_hidden(empty_memory, monkeypatch):
    """THE WINDOW THE COMPENSATION LEAVES, stated rather than papered over: the delete can fail too.

    Then the person IS in memory with no contact, and the message says so. What it must never do is
    report success, and what it must never claim is that nothing was written."""
    def refuses(*args, **kwargs):
        raise memory.adapter.MemoryAdapterError("disk is full")

    monkeypatch.setattr(memory.adapter, "insert_contact", refuses)
    monkeypatch.setattr(memory.adapter, "delete_row", refuses)
    reply, _context = run(ENGLISH, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert "may be in my memory with no contact" in reply.message, reply.message
    assert len(rows(empty_memory, "people")) == 1, "the premise: the person really is still there"


# =======================================================================================================
# 6. THE DISCLOSURE CONTEXT SPLIT
# =======================================================================================================

def test_a_deny_rule_for_the_brain_does_not_blind_the_user(empty_memory):
    """WHY THE SPLIT EXISTS, as one sentence the owner can now write: "never let my phone numbers reach
    the reasoning service, but do show them to me". That is one deny rule on BRAIN and nothing on USER,
    and it is unsayable while both read under the same context name."""
    run(ENGLISH, interpret=must_not_be_called)
    person = queries.person("Ali")
    assert isinstance(person, Found)
    memory.set_sensitive_rule("contacts", "address", queries.BRAIN, "deny")

    withheld = queries.contact(person.value.id, "whatsapp")
    assert withheld.value.address is None and withheld.value.disclosed is False
    shown = queries.contact_for_user(person.value.id, "whatsapp")
    assert isinstance(shown, Found) and shown.value.address == ADDRESS

    reply, _context = run("what is Ali's whatsapp", interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED and ADDRESS in reply.message


def test_a_deny_rule_for_the_user_withholds_the_recall_answer(empty_memory):
    """The other direction, and the reason the user context is a real context rather than a label: a
    rule written against it is honoured by the answer the user sees."""
    run(ENGLISH, interpret=must_not_be_called)
    person = queries.person("Ali")
    memory.set_sensitive_rule("contacts", "address", queries.USER, "deny")
    reply, _context = run("what is Ali's whatsapp", interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED
    assert ADDRESS not in reply.message, reply.message
    assert isinstance(queries.contact(person.value.id, "whatsapp"), Found), "BRAIN is unaffected"


def test_the_two_contexts_are_different_values_and_neither_is_the_default():
    """A split that shared one string would be no split. And nothing changed for an existing caller:
    queries.contact() still reads under BRAIN, which is checked on the AST of the call itself."""
    assert queries.USER != queries.BRAIN
    tree = ast.parse(inspect.getsource(queries.contact))
    keywords = {keyword.arg: ast.unparse(keyword.value)
                for node in ast.walk(tree) if isinstance(node, ast.Call)
                for keyword in node.keywords}
    assert keywords.get("context") == "BRAIN", keywords
    user_tree = ast.parse(inspect.getsource(queries.contact_for_user))
    user_keywords = {keyword.arg: ast.unparse(keyword.value)
                     for node in ast.walk(user_tree) if isinstance(node, ast.Call)
                     for keyword in node.keywords}
    assert user_keywords.get("context") == "USER", user_keywords


# =======================================================================================================
# 7. THE ORDER, AND WHAT DID NOT CHANGE
# =======================================================================================================

def test_the_guard_runs_before_the_brain_can_be_asked(empty_memory):
    """THE GUARANTEE IS POSITIONAL, so it is checked positionally: inside handle_typed_line() the local
    memory call comes before the one that can reach a provider. Checked on the AST, with the docstring
    discarded, because the explanation of this rule is itself written in the function above it."""
    tree = ast.parse(inspect.getsource(console.handle_typed_line).strip())
    function = tree.body[0]
    body = function.body[1:] if isinstance(function.body[0], ast.Expr) else function.body
    called = [ast.unparse(node.func) for node in ast.walk(ast.Module(body=body, type_ignores=[]))
              if isinstance(node, ast.Call)]
    assert called.index("_personal_memory") < called.index("_ask_the_brain"), called


def test_a_remembered_application_alias_still_resolves_locally(empty_memory, executor):
    """Phase 4's existing wiring, unchanged by the new branch in front of it: the alias path is still
    reached, still local, and still free."""
    reply, _context = run("open editor", interpret=must_not_be_called)
    assert reply.status is Status.RAN, reply.message
    assert [action.target for action in executor.actions] == ["notepad"]


def test_an_unrecognised_line_is_still_the_brains(empty_memory):
    """The new branch returns None for everything it does not own, and None is the only way past it."""
    brainless = Brainless(understood(open_notepad()))
    reply, _context = run(LOOSE, interpret=brainless, script=Script("cancel"))
    assert brainless.calls == 1
    assert reply.status is Status.CANCELLED, reply.message


def test_memory_being_unavailable_does_not_claim_a_remember_succeeded(memory_at):
    """D6 for the new path: with no database at all, the refusal is honest and nothing is sent."""
    reply, _context = run(ENGLISH, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert isinstance(memory.remember_contact("Ali", "whatsapp", ADDRESS, ALLOW), MemoryUnavailable)
