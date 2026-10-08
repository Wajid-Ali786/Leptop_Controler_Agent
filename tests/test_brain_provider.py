"""
The Brain's provider boundary (Phase 3 Slice 2): the system prompt, the output schema, what is counted
as billable input, and what happens to a reply that cannot be believed.

No test here makes a paid call. The one real-API test lives at the bottom, is marked real_api, and is
skipped unless RUN_REAL_CLAUDE_TEST=1.

THE FACTS THIS SLICE RESTS ON were verified on 2026-09-30 against Anthropic's own documentation and
against the installed SDK, and the tests that pin them say which is which:

  * `claude-sonnet-5-5`, $2/MTok in and $10/MTok out, Active, structured outputs supported
    - platform.claude.com/docs/en/docs/about-claude/models/overview and .../pricing
  * output_config={"format": {"type": "json_schema", "schema": ...}}
    - verified in the installed anthropic 1.5.0 type definitions, not only in the docs
  * schemas sent with a request are billed as input, and the API injects its own system prompt for
    structured output whose size is not published
    - .../pricing (tool use pricing) and .../build-with-claude/structured-outputs

Where a documented limit exists, the test names the number so a future reader can re-check it.
"""
import ast
import json
from pathlib import Path

import httpx2
import pytest

from app.brain import cost_controls
from app.brain import logic as brain
from app.brain.models import (ARGS_FIELDS, ARGS_FOR_KIND, NavigateArgs, INTERPRETATION_KINDS, MAX_APP, MAX_BECAUSE,
                              ClickTargetArgs, CloseBrowserArgs, OpenBrowserArgs,
                              MAX_INTENTS, MAX_KEYS, MAX_MESSAGE, MAX_MISSING, MAX_QUESTION,
                              MAX_RESTATED, MAX_WHAT, MAX_WHY, RISK_FLOOR_NAMES,
                              NEUTRAL_ARGS_VALUES, NEUTRAL_INTERPRETATION_VALUES,
                              INTERPRETATION_FIELDS, SUPPORTED_MIN_ITEMS, SUPPORTED_SCHEMA_KEYWORDS,
                              UNSUPPORTED_SCHEMA_KEYWORDS, is_neutral, schema_incompatibilities,
                              schema_keywords, ClickArgs,
                              NeedsClarification, NotACommand, NotSupported, OpenAppArgs,
                              PreviousActionContext, RefreshArgs, ScrollArgs, ShortcutArgs,
                              TypeTextArgs, Understood, WindowControlArgs, interpretation_schema,
                              previous_action_context, schema_complexity)
from app.executor.models import (CLOSE_BROWSER, NAVIGATE, OPEN_BROWSER, CLICK_TARGET, CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL)
from app.planner.models import (MAX_PLAN_STEPS, ClarificationContinuation, FailureContext,
                                PlanStepSummary, ReplanRequest)
from app.safety.models import RiskLevel

# Anthropic's documented structured-output schema limits, from the structured outputs page on
# 2026-09-30. Named here so the test that checks them can be re-verified rather than trusted.
DOCUMENTED_OPTIONAL_LIMIT = 24
DOCUMENTED_UNION_LIMIT = 16

TYPE_LIMIT = 1000        # the Executor's own configured max_type_characters
SECRET = "ZZ-my-diary-password-hunter2-ZZ"


def schema():
    return interpretation_schema(TYPE_LIMIT)


def reply(**fields) -> str:
    """A complete structured reply: every field the schema requires, defaults empty."""
    body = {"kind": "understood", "restated": "", "intents": [], "question": "", "missing": "",
            "because": "", "what": "", "message": ""}
    body.update(fields)
    return json.dumps(body, ensure_ascii=False)


def intent(**fields) -> dict:
    """One complete flat intent, every schema field present."""
    body = {"kind": OPEN_APP, "why": "", "risk_floor": "low", "app": "", "x": 0, "y": 0, "text": "",
            "keys": "", "direction": "", "notches": 0, "operation": ""}
    body.update(fields)
    return body


def validate(payload, **kwargs):
    return brain.validate_interpretation(payload, max_type_characters=TYPE_LIMIT, **kwargs)


# --- §2 The installed SDK, checked in the package itself rather than in the docs -----------------------

def test_the_installed_sdk_accepts_the_request_shape_this_adapter_sends():
    """Verified in the installed anthropic 1.5.0, not from documentation: the docs and the package agree,
    and if a future upgrade changes either, this fails."""
    import inspect

    import anthropic
    from anthropic.resources.messages import Messages
    from anthropic.types.json_output_format_param import JSONOutputFormatParam
    from anthropic.types.output_config_param import OutputConfigParam

    assert anthropic.__version__.startswith("1."), anthropic.__version__
    parameters = inspect.signature(Messages.create).parameters
    assert "output_config" in parameters
    assert "system" in parameters
    assert "output_format" not in parameters, "it moved to output_config.format in this SDK"
    assert set(OutputConfigParam.__annotations__) >= {"format"}
    assert set(JSONOutputFormatParam.__annotations__) == {"type", "schema"}
    installed = Path(anthropic.__file__).parent / "types" / "json_output_format_param.py"
    source = installed.read_text(encoding="utf-8")
    assert "schema: Required[Dict[str, object]]" in source, source
    assert 'type: Required[Literal["json_schema"]]' in source, source
    assert "thinking" in parameters
    # The deprecated sampling parameters are gone in Python SDK 1.x; passing one raises TypeError.
    for gone in ("temperature", "top_p", "top_k"):
        assert gone not in parameters, gone


def test_the_installed_sdk_does_not_yet_type_between_tools_but_transmits_it():
    """The one mismatch found in this amendment, pinned so it cannot change silently.

    `thinking` is a documented public parameter and `between_tools` a documented public value that needs
    no beta header - but anthropic 1.5.0's ThinkingConfigParam union is only enabled|disabled|adaptive,
    and the string "between_tools" appears nowhere in the installed package. The runtime path passes the
    dict through unchanged, which the request-shape test below proves on the wire, so this is a gap in
    the SDK's type stubs rather than a missing capability. No private attribute, no extra_body, no
    monkeypatch. If a future SDK starts validating the value, that test fails loudly.
    """
    import anthropic
    from anthropic.types.thinking_config_param import ThinkingConfigParam

    variants = {arg.__name__ for arg in ThinkingConfigParam.__args__}
    assert variants == {"ThinkingConfigEnabledParam", "ThinkingConfigDisabledParam",
                        "ThinkingConfigAdaptiveParam"}, variants
    installed = Path(anthropic.__file__).parent
    assert not any("between_tools" in path.read_text(encoding="utf-8", errors="ignore")
                   for path in installed.rglob("*.py")), "the SDK now types it; re-read this test"


def test_the_adapter_sends_exactly_that_shape():
    """Checked on the code with comments and docstrings stripped: the adapter explains in a comment why
    the deprecated parameter name is not used, and a substring search would match the explanation."""
    tree = ast.parse(Path("app/brain/adapter.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value.value = ""          # drop every docstring
    code = ast.unparse(tree)
    assert "output_config" in code and "json_schema" in code
    assert "output_format" not in code, "the deprecated parameter name is not used in code"


# --- §6 The schema is derived from the typed contracts, and stays inside the documented limits ---------

def test_the_action_kinds_come_from_the_executors_own_vocabulary():
    kinds = schema()["properties"]["intents"]["items"]["properties"]["kind"]["enum"]
    assert kinds == list(ARGS_FOR_KIND)
    assert set(kinds) == set(ARGS_FIELDS)


def test_the_model_cannot_name_a_capability_that_does_not_exist():
    for invented in ("shell", "run", "send_message", "web_search", "delete_file"):
        assert invented not in schema()["properties"]["intents"]["items"]["properties"]["kind"]["enum"]


def test_there_is_no_target_field_for_the_model_to_write():
    """The strongest part of the contract: a model cannot hand over an ExecutorAction target string,
    because the wire shape has no such field. The Planner writes every target."""
    fields = schema()["properties"]["intents"]["items"]["properties"]
    assert "target" not in fields
    assert "command" not in fields and "action" not in fields


def test_the_scroll_and_window_vocabularies_are_the_executors():
    fields = schema()["properties"]["intents"]["items"]["properties"]
    assert fields["direction"]["enum"] == ["", "up", "down"]
    assert fields["operation"]["enum"] == ["", "minimize", "maximize", "restore", "close"]


def test_the_interpretation_kinds_are_the_four_and_only_the_four():
    assert schema()["properties"]["kind"]["enum"] == list(INTERPRETATION_KINDS)
    assert len(INTERPRETATION_KINDS) == 4


def test_the_schema_stays_inside_anthropics_documented_complexity_limits():
    """The reason the wire shape is flat. Documented on 2026-09-30: 24 optional parameters and 16
    union-typed parameters per request."""
    counts = schema_complexity(schema())
    assert counts["optional"] <= DOCUMENTED_OPTIONAL_LIMIT, counts
    assert counts["unions"] <= DOCUMENTED_UNION_LIMIT, counts
    # 21 since usability Slice 5 added `url` for navigate (20 before it, when Slice 3 added
    # `control`). What matters is unchanged and is asserted above: still zero optional parameters
    # and zero unions, so the documented limits of 24 and 16 are not approached by adding a field -
    # only the flat property count moves.
    assert counts == {"optional": 0, "unions": 0, "properties": 21}, "flat by construction"


def test_every_property_is_required_and_nothing_else_is_allowed():
    def check(node):
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])
    check(schema())
    check(schema()["properties"]["intents"]["items"])


def test_the_intent_cap_is_the_planners_own_step_cap():
    """The number is still ours and still 5. What changed is who can state it: array `maxItems` is
    unsupported by the provider, so the schema cannot carry the cap and the validator is the only place
    it is real. The prompt and the field description say "5" as guidance, which is not enforcement."""
    assert MAX_INTENTS == MAX_PLAN_STEPS
    intents = schema()["properties"]["intents"]
    assert "maxItems" not in intents, "unsupported by the provider; it caused the first 400"
    assert str(MAX_INTENTS) in intents["description"], "the model is still told the cap"
    assert intents["minItems"] == 0, "the other three interpretations send an empty list"
    six = validate(reply(intents=[intent(app="notepad")] * (MAX_INTENTS + 1)))
    assert isinstance(six, brain.InterpretationError) and six.reason == brain.BAD_INTENT_COUNT


def test_the_typed_text_bound_is_the_executors_configured_limit_not_a_new_one():
    """No invented product limit: the schema is built with whatever the Executor is configured to
    accept, so the two can never disagree - now stated in words, and enforced by the validator."""
    fields = interpretation_schema(250)["properties"]["intents"]["items"]["properties"]
    assert "maxLength" not in fields["text"]
    assert "at most 250 characters" in fields["text"]["description"]
    assert f"at most {TYPE_LIMIT} characters" in (
        schema()["properties"]["intents"]["items"]["properties"]["text"]["description"])
    over = reply(intents=[intent(kind=TYPE_TEXT, text="x" * 251)])
    assert brain.validate_interpretation(over, max_type_characters=250).reason == brain.TOO_LONG
    assert isinstance(brain.validate_interpretation(over, max_type_characters=1000), Understood), (
        "the limit is whatever the Executor is configured for, not a constant in the Brain")


# --- The provider-schema compatibility contract -------------------------------------------------------
# The first real request failed with 400 "For 'array' type, property 'maxItems' is not supported". These
# tests exist so the next unsupported keyword is found here instead of by paying for another request.

def test_the_emitted_schema_is_compatible_with_anthropics_documented_subset():
    assert schema_incompatibilities(schema()) == []
    assert schema_incompatibilities(interpretation_schema(1)) == []
    assert schema_incompatibilities(interpretation_schema(100000)) == []


@pytest.mark.parametrize("forbidden", ["maxItems", "minLength", "maxLength", "minimum", "maximum",
                                       "multipleOf", "uniqueItems", "oneOf", "patternProperties"])
def test_no_unsupported_keyword_appears_anywhere_in_the_tree(forbidden):
    assert forbidden not in schema_keywords(schema()), f"{forbidden} is unsupported and returns 400"


def test_every_keyword_emitted_is_on_the_verified_supported_list():
    """An allowlist, not a denylist: a keyword nobody has considered fails this rather than passing."""
    emitted = set(schema_keywords(schema()))
    assert emitted <= SUPPORTED_SCHEMA_KEYWORDS, emitted - SUPPORTED_SCHEMA_KEYWORDS
    assert emitted == {"type", "properties", "required", "additionalProperties", "items",
                       "description", "enum", "minItems"}, emitted


def test_min_items_is_only_ever_zero_or_one():
    """The one array constraint the provider supports, and only at those two values."""
    assert SUPPORTED_MIN_ITEMS == (0, 1)
    for paths in [schema_keywords(schema()).get("minItems", [])]:
        assert paths, "minItems is deliberately present"
    stack = [schema()]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "minItems" in node:
                assert node["minItems"] in SUPPORTED_MIN_ITEMS, node["minItems"]
            stack.extend(node.get("properties", {}).values())
            if isinstance(node.get("items"), dict):
                stack.append(node["items"])


def test_the_supported_and_unsupported_lists_do_not_overlap():
    assert not (SUPPORTED_SCHEMA_KEYWORDS & UNSUPPORTED_SCHEMA_KEYWORDS)
    for keyword in ("maxItems", "maxLength", "minLength", "minimum", "maximum", "multipleOf"):
        assert keyword in UNSUPPORTED_SCHEMA_KEYWORDS, keyword
    for keyword in ("type", "properties", "required", "additionalProperties", "enum", "const",
                    "minItems", "pattern"):
        assert keyword in SUPPORTED_SCHEMA_KEYWORDS, keyword


@pytest.mark.parametrize("planted, where", [
    ({"maxItems": 5}, "intents"),
    ({"maxLength": 10}, "restated"),
    ({"minimum": 0}, "kind"),
    ({"multipleOf": 2}, "kind"),
    ({"oneOf": []}, "kind"),
])
def test_the_compatibility_checker_actually_catches_a_planted_violation(planted, where):
    """Otherwise this whole section proves nothing. Each of these is the shape of the mistake that cost
    a real 400."""
    broken = interpretation_schema(TYPE_LIMIT)
    broken["properties"][where].update(planted)
    problems = schema_incompatibilities(broken)
    assert problems, f"{planted} slipped through"
    assert any(next(iter(planted)) in problem for problem in problems), problems


def test_the_checker_catches_a_violation_nested_inside_an_intent():
    broken = interpretation_schema(TYPE_LIMIT)
    broken["properties"]["intents"]["items"]["properties"]["why"]["maxLength"] = 120
    assert any("maxLength" in problem for problem in schema_incompatibilities(broken))


def test_the_checker_catches_a_bad_min_items_and_a_loose_object():
    broken = interpretation_schema(TYPE_LIMIT)
    broken["properties"]["intents"]["minItems"] = 2
    assert any("minItems" in problem for problem in schema_incompatibilities(broken))
    loose = interpretation_schema(TYPE_LIMIT)
    loose["additionalProperties"] = True
    assert any("additionalProperties" in problem for problem in schema_incompatibilities(loose))


def test_what_this_compatibility_check_cannot_promise():
    """Honest coverage. The documentation enumerates unsupported KEYWORDS, and that is what is checked
    here. It also mentions limits that are about values and size rather than keywords - compiled grammar
    complexity, regex complexity, and the 24-optional / 16-union counts - and a schema can satisfy every
    check above and still be refused for one of those. schema_complexity() covers the counts; the other
    two are not knowable offline, which is why one controlled real request is still the final check."""
    counts = schema_complexity(schema())
    assert counts["optional"] <= DOCUMENTED_OPTIONAL_LIMIT
    assert counts["unions"] <= DOCUMENTED_UNION_LIMIT
    assert "pattern" not in schema_keywords(schema()), "no regex, so no regex complexity limit to hit"


# --- One canonical wire form per kind -----------------------------------------------------------------
# The flat schema gives every intent every field, so an irrelevant field could carry anything. Ignoring
# it was execution-safe but left an unnecessary channel and made the output cap underivable. These tests
# fix the form: fields the kind does not use must be exactly neutral.

IRRELEVANT_PROBES = [
    (REFRESH, {}, "app", "ZZ-probe-app-ZZ"),
    (REFRESH, {}, "text", "ZZ-probe-text-ZZ"),
    (OPEN_APP, {"app": "notepad"}, "text", "ZZ-probe-text-ZZ"),
    (TYPE_TEXT, {"text": "hello"}, "app", "ZZ-probe-app-ZZ"),
    (SCROLL, {"direction": "down", "notches": 3}, "keys", "ZZ-probe-keys-ZZ"),
    (CLOSE_APP, {"app": "notepad"}, "x", 987654),
    (CLOSE_APP, {"app": "notepad"}, "y", 987654),
    (SHORTCUT, {"keys": "ctrl+c"}, "notches", 987654),
    (CLICK, {"x": 1, "y": 2}, "operation", "close"),
    (CLICK, {"x": 1, "y": 2}, "direction", "down"),
    (WINDOW_CONTROL, {"operation": "minimize"}, "app", "ZZ-probe-app-ZZ"),
    (REFRESH, {}, "keys", "ctrl+alt+delete"),
]


@pytest.mark.parametrize("kind, used, field, sentinel", IRRELEVANT_PROBES,
                         ids=[f"{case[0]}+{case[2]}" for case in IRRELEVANT_PROBES])
def test_a_non_neutral_irrelevant_field_is_refused(kind, used, field, sentinel):
    outcome = validate(reply(intents=[intent(kind=kind, **used, **{field: sentinel})]))
    assert isinstance(outcome, brain.InterpretationError), f"{kind} accepted a non-neutral {field}"
    assert outcome.reason == brain.NOT_CANONICAL
    assert outcome.detail == f"intents[1].{field}", outcome.detail


@pytest.mark.parametrize("kind, used, field, sentinel", IRRELEVANT_PROBES,
                         ids=[f"{case[0]}+{case[2]}" for case in IRRELEVANT_PROBES])
def test_the_refusal_names_the_field_and_never_its_value(kind, used, field, sentinel):
    outcome = validate(reply(intents=[intent(kind=kind, **used, **{field: sentinel})]))
    shown = f"{outcome!r} {outcome} {outcome.reason} {outcome.detail}"
    assert str(sentinel) not in shown, shown
    assert field in outcome.detail


@pytest.mark.parametrize("field, neutral", sorted(NEUTRAL_ARGS_VALUES.items()))
def test_the_neutral_value_of_every_args_field_is_what_the_wire_type_already_says(field, neutral):
    """Not invented: the empty string for a string, 0 for an integer, and for the two enum fields the
    empty member that is already in the enum because six of the eight kinds do not use them."""
    field_schema = schema()["properties"]["intents"]["items"]["properties"][field]
    if field_schema["type"] == "integer":
        assert neutral == 0
    else:
        assert neutral == ""
        if "enum" in field_schema:
            assert "" in field_schema["enum"], f"{field} has no neutral member"


def test_no_enum_was_widened_and_no_enum_names_a_non_capability():
    """The invariant Slice 1A was built around. The two enums that need a neutral value already had ""
    before this audit; `kind` and `risk_floor` are used by every intent and so need none. Nothing like
    an "unused" member was added to an action enum."""
    intent_fields = schema()["properties"]["intents"]["items"]["properties"]
    assert intent_fields["kind"]["enum"] == list(ARGS_FOR_KIND), "no extra member"
    assert "unused" not in intent_fields["kind"]["enum"]
    assert set(intent_fields["risk_floor"]["enum"]) == set(RISK_FLOOR_NAMES)
    assert intent_fields["direction"]["enum"] == ["", "up", "down"]
    assert intent_fields["operation"]["enum"] == ["", "minimize", "maximize", "restore", "close"]
    for field in ("kind", "risk_floor"):
        assert field not in NEUTRAL_ARGS_VALUES, f"{field} is used by every intent"


def test_a_field_relevant_to_the_kind_is_never_required_to_be_neutral():
    """The check must not turn into "everything must be empty"."""
    for kind, fields in [(OPEN_APP, {"app": "notepad"}), (CLICK, {"x": 500, "y": 300}),
                         (SCROLL, {"direction": "down", "notches": 3}),
                         (WINDOW_CONTROL, {"operation": "close"})]:
        assert isinstance(validate(reply(intents=[intent(kind=kind, **fields)])), Understood)


@pytest.mark.parametrize("value", [False, True])
def test_a_boolean_does_not_pass_as_a_neutral_number(value):
    """In Python False == 0, so a plain equality test would let a boolean through where a count
    belongs. is_neutral() is type-strict for exactly this."""
    outcome = validate(reply(intents=[intent(kind=REFRESH, notches=value)]))
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.NOT_CANONICAL


def test_is_neutral_is_type_strict():
    assert is_neutral("", "") and is_neutral(0, 0) and is_neutral([], [])
    assert not is_neutral(False, 0) and not is_neutral(True, 0)
    assert not is_neutral(0, "") and not is_neutral("", 0)
    assert not is_neutral("0", 0) and not is_neutral(None, "")
    assert not is_neutral([1], []) and not is_neutral(" ", "")


def test_a_long_irrelevant_payload_is_refused_rather_than_measured():
    """The output-channel case: a thousand characters in a field the kind does not use. It is refused
    for its FORM, before any bound is considered, so it cannot even be smuggled in under a limit."""
    outcome = validate(reply(intents=[intent(kind=REFRESH, text="x" * TYPE_LIMIT)]))
    assert outcome.reason == brain.NOT_CANONICAL, "form first, not TOO_LONG"
    outcome = validate(reply(intents=[intent(kind=REFRESH, text="x" * (TYPE_LIMIT * 10))]))
    assert outcome.reason == brain.NOT_CANONICAL


# --- The same rule at the top level -------------------------------------------------------------------

TOP_LEVEL_PROBES = [
    ("not_a_command", {"message": "Hello."}, "restated", "ZZ-probe-restated-ZZ"),
    ("not_a_command", {"message": "Hello."}, "what", "ZZ-probe-what-ZZ"),
    ("not_a_command", {"message": "Hello."}, "question", "ZZ-probe-question-ZZ"),
    ("not_supported", {"what": "web search", "message": "no"}, "restated", "ZZ-probe-restated-ZZ"),
    ("not_supported", {"what": "web search", "message": "no"}, "because", "ZZ-probe-because-ZZ"),
    ("needs_clarification", {"question": "Which one?"}, "restated", "ZZ-probe-restated-ZZ"),
    ("needs_clarification", {"question": "Which one?"}, "message", "ZZ-probe-message-ZZ"),
    ("understood", {"intents": None}, "question", "ZZ-probe-question-ZZ"),
    ("understood", {"intents": None}, "what", "ZZ-probe-what-ZZ"),
    ("understood", {"intents": None}, "message", "ZZ-probe-message-ZZ"),
    ("understood", {"intents": None}, "missing", "ZZ-probe-missing-ZZ"),
]


@pytest.mark.parametrize("kind, used, field, sentinel", TOP_LEVEL_PROBES,
                         ids=[f"{case[0]}+{case[2]}" for case in TOP_LEVEL_PROBES])
def test_a_non_neutral_irrelevant_top_level_field_is_refused(kind, used, field, sentinel):
    """The same channel exists at the top level: a not_a_command reply carrying a full restatement, or
    a whole list of intents, was previously accepted and ignored."""
    fields = dict(used)
    if fields.get("intents") is None and "intents" in fields:
        fields["intents"] = [intent(app="notepad")]
    outcome = validate(reply(kind=kind, **fields, **{field: sentinel}))
    assert isinstance(outcome, brain.InterpretationError), f"{kind} accepted a non-neutral {field}"
    assert outcome.reason == brain.NOT_CANONICAL and outcome.detail == field
    assert str(sentinel) not in f"{outcome!r} {outcome}"


@pytest.mark.parametrize("kind", ["not_a_command", "not_supported", "needs_clarification"])
def test_intents_must_be_empty_for_every_interpretation_but_understood(kind):
    """The largest version of the top-level channel: five complete intents attached to an answer that
    has nothing to do."""
    used = {"not_a_command": {"message": "Hello."},
            "not_supported": {"what": "web search", "message": "no"},
            "needs_clarification": {"question": "Which one?"}}[kind]
    outcome = validate(reply(kind=kind, **used, intents=[intent(app="notepad")] * 5))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason == brain.NOT_CANONICAL and outcome.detail == "intents"


def test_the_interpretation_field_map_covers_every_variant():
    assert set(INTERPRETATION_FIELDS) == set(INTERPRETATION_KINDS)
    used = {field for fields in INTERPRETATION_FIELDS.values() for field in fields}
    assert used == set(NEUTRAL_INTERPRETATION_VALUES), used
    assert "kind" not in NEUTRAL_INTERPRETATION_VALUES, "the discriminator is always relevant"


def test_the_canonical_form_of_each_interpretation_is_accepted():
    assert isinstance(validate(reply(kind="not_a_command", message="Hello.")), NotACommand)
    assert isinstance(validate(reply(kind="not_supported", what="web search", message="no")),
                      NotSupported)
    assert isinstance(validate(reply(kind="needs_clarification", question="Which one?",
                                     missing="app", because="two match")), NeedsClarification)
    assert isinstance(validate(reply(restated="open notepad",
                                     intents=[intent(app="notepad")])), Understood)


# --- Nothing irrelevant reaches typed args, a Plan, or a log ------------------------------------------

def test_a_secret_in_an_irrelevant_field_produces_no_interpretation_and_no_plan():
    """The output-channel test. The reply is refused, so there is no Interpretation to plan from - which
    is the strongest possible statement about what reaches a Plan."""
    from app.brain.models import INTERPRETATIONS
    from app.executor.logic import resolve
    from app.planner.logic import build_plan
    from app.planner.models import TYPED_CONSOLE
    payload = reply(restated="refresh the window",
                    intents=[intent(kind=REFRESH, text=SECRET, app=SECRET, keys=SECRET)])
    outcome = validate(payload)
    assert isinstance(outcome, brain.InterpretationError)
    assert not isinstance(outcome, INTERPRETATIONS), "nothing plannable was produced"
    assert SECRET not in repr(outcome) and SECRET not in str(outcome)
    assert SECRET not in outcome.detail and "hunter2" not in f"{outcome!r}"
    with pytest.raises(AttributeError):
        build_plan(outcome, TYPED_CONSOLE, resolve)     # an error is not an Understood


def test_a_canonical_reply_produces_a_plan_holding_only_what_the_kind_uses():
    from app.executor.logic import resolve
    from app.planner.logic import build_plan
    from app.planner.models import Plan, TYPED_CONSOLE
    understood = validate(reply(restated="refresh then open notepad",
                                intents=[intent(kind=REFRESH), intent(app="notepad")]))
    built = build_plan(understood, TYPED_CONSOLE, resolve)
    assert isinstance(built, Plan)
    assert [step.action.target for step in built.steps] == ["", "notepad"]
    assert SECRET not in repr(built)


def test_the_canonicalization_refusal_never_reaches_a_log_with_content(caplog, fake_claude):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    payload = reply(intents=[intent(kind=REFRESH, text=SECRET)])
    fake_claude.respond = structured_response(payload)
    with caplog.at_level("INFO"):
        got = adapter.send_message("refresh", max_tokens=64, system=brain.SYSTEM_PROMPT,
                                  output_schema=schema())
    outcome = validate(got.text, stop_reason=got.stop_reason)
    assert outcome.reason == brain.NOT_CANONICAL
    assert SECRET not in caplog.text and "hunter2" not in caplog.text


# --- Provider-structural acceptance is NOT application acceptance --------------------------------------
# The schema is now looser than the product, on purpose, because the provider cannot express our bounds.
# Every one of these replies would satisfy the schema and must still be refused.

def test_a_reply_can_be_schema_valid_and_application_invalid():
    """The premise of the whole fix, stated once as a single case: 6 intents is structurally fine now."""
    payload = reply(intents=[intent(app="notepad")] * 6)
    assert schema_incompatibilities(schema()) == [], "the schema no longer forbids it"
    assert isinstance(validate(payload), brain.InterpretationError), "and we still do"


@pytest.mark.parametrize("label, payload_fields, reason", [
    ("6 intents", {"intents": [{"app": "notepad"}] * 6}, brain.BAD_INTENT_COUNT),
    ("20 intents", {"intents": [{"app": "notepad"}] * 20}, brain.BAD_INTENT_COUNT),
    ("0 intents", {"intents": []}, brain.BAD_INTENT_COUNT),
    ("overlong restated", {"restated": "r" * (MAX_RESTATED + 1),
                           "intents": [{"app": "notepad"}]}, brain.TOO_LONG),
    ("overlong why", {"intents": [{"app": "notepad", "why": "w" * (MAX_WHY + 1)}]}, brain.TOO_LONG),
    ("overlong app", {"intents": [{"app": "a" * (MAX_APP + 1)}]}, brain.TOO_LONG),
    ("overlong type_text", {"intents": [{"kind": TYPE_TEXT,
                                        "text": "t" * (TYPE_LIMIT + 1)}]}, brain.TOO_LONG),
    ("unknown kind", {"intents": [{"kind": "shell"}]}, brain.UNKNOWN_KIND),
    ("bad risk floor", {"intents": [{"app": "notepad", "risk_floor": "urgent"}]},
     brain.BAD_RISK_FLOOR),
    ("non-integer click", {"intents": [{"kind": CLICK, "x": "500", "y": 3}]}, brain.BAD_ARGS),
    ("boolean click", {"intents": [{"kind": CLICK, "x": True, "y": 3}]}, brain.BAD_ARGS),
    ("bad scroll direction", {"intents": [{"kind": SCROLL, "direction": "left",
                                           "notches": 3}]}, brain.BAD_ARGS),
    ("bad window operation", {"intents": [{"kind": WINDOW_CONTROL,
                                           "operation": "sideways"}]}, brain.BAD_ARGS),
])
def test_structurally_acceptable_but_application_invalid_replies_are_refused(label, payload_fields,
                                                                            reason):
    fields = dict(payload_fields)
    if "intents" in fields:
        fields["intents"] = [intent(**item) for item in fields["intents"]]
    outcome = validate(reply(**fields))
    assert isinstance(outcome, brain.InterpretationError), f"{label} was accepted"
    assert outcome.reason == reason, f"{label}: {outcome.reason}"


def test_none_of_those_replies_can_reach_the_planner():
    from app.brain.models import INTERPRETATIONS
    for count in (0, 6, 20):
        outcome = validate(reply(intents=[intent(app="notepad")] * count))
        assert not isinstance(outcome, INTERPRETATIONS)


# --- Numeric action bounds live at the existing resolve seam -------------------------------------------
# The schema never carried numeric bounds (minimum/maximum are unsupported), and the validator checks
# only that the values are whole numbers. The RANGE is the Executor's own rule, enforced once at
# resolve() - so these tests prove the bound is real without adding a second opinion about the number.

@pytest.mark.parametrize("notches", [0, -1, -20, 21, 9999])
def test_a_scroll_amount_outside_the_executors_range_never_becomes_a_step(notches):
    from app.executor.logic import resolve
    from app.planner.logic import build_plan
    from app.planner.models import PlanRefusal, PLAN_UNRESOLVED, TYPED_CONSOLE
    understood = validate(reply(intents=[intent(kind=SCROLL, direction="down", notches=notches)]))
    assert isinstance(understood, Understood), "a whole number is structurally fine"
    built = build_plan(understood, TYPED_CONSOLE, resolve)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_UNRESOLVED
    assert built.message == resolve(__import__("app.executor.models", fromlist=["ExecutorAction"])
                                    .ExecutorAction(SCROLL, f"down {notches}")).message


@pytest.mark.parametrize("notches", [1, 3, 20])
def test_a_scroll_amount_inside_the_range_does_become_a_step(notches):
    from app.executor.logic import resolve
    from app.planner.logic import build_plan
    from app.planner.models import Plan, TYPED_CONSOLE
    understood = validate(reply(intents=[intent(kind=SCROLL, direction="down", notches=notches)]))
    built = build_plan(understood, TYPED_CONSOLE, resolve)
    assert isinstance(built, Plan) and built.steps[0].action.target == f"down {notches}"


def test_the_brain_checks_the_type_and_the_executor_checks_the_range():
    """Where each rule lives, asserted so nobody moves one and duplicates the other."""
    assert validate(reply(intents=[intent(kind=SCROLL, direction="down",
                                          notches="3")])).reason == brain.BAD_ARGS
    assert isinstance(validate(reply(intents=[intent(kind=SCROLL, direction="down", notches=0)])),
                      Understood)
    # Checked on the CODE with comments stripped. A bare "20" anywhere above SYSTEM_PROMPT used to be
    # the proxy, and usability Slice 5 broke it with a COMMENT reading "$0.0020" while the rule it
    # guards stayed perfectly true - the eleventh time in this project a substring has matched prose
    # rather than code. A comment cannot duplicate a limit; only an expression can.
    source = Path("app/brain/logic.py").read_text(encoding="utf-8")
    code = " ".join(line.split("#")[0] for line in source.split("SYSTEM_PROMPT")[0].splitlines())
    assert "max_scroll" not in source
    assert "20" not in code, code


# --- §7 Every free-text field the model writes is bounded ---------------------------------------------

TOP_LEVEL_BOUNDS = [("restated", MAX_RESTATED), ("question", MAX_QUESTION), ("missing", MAX_MISSING),
                    ("because", MAX_BECAUSE), ("what", MAX_WHAT), ("message", MAX_MESSAGE)]
INTENT_BOUNDS = [("why", MAX_WHY), ("app", MAX_APP), ("keys", MAX_KEYS)]


@pytest.mark.parametrize("field, limit", TOP_LEVEL_BOUNDS)
def test_top_level_text_fields_state_their_bound_in_words(field, limit):
    """String `maxLength` is unsupported, so the bound is in the description - the same transformation
    Anthropic's own SDK helpers perform. It is guidance to the model, not a guarantee."""
    field_schema = schema()["properties"][field]
    assert "maxLength" not in field_schema, "unsupported by the provider"
    assert f"at most {limit} characters" in field_schema["description"]
    assert 0 < limit <= 200, "long enough for one short sentence, not for an essay"


@pytest.mark.parametrize("field, limit", INTENT_BOUNDS)
def test_intent_text_fields_state_their_bound_in_words(field, limit):
    field_schema = schema()["properties"]["intents"]["items"]["properties"][field]
    assert "maxLength" not in field_schema
    assert f"at most {limit} characters" in field_schema["description"]


def test_every_string_field_has_a_bound_somewhere_even_though_the_schema_cannot_hold_it():
    """The invariant survives the fix: every free-text field is either a fixed vocabulary or has a
    stated character limit, and the limit is checked by our validator when the reply arrives."""
    def walk(node, path=""):
        for name, field in node.get("properties", {}).items():
            where = f"{path}.{name}"
            if field.get("type") == "string":
                assert "enum" in field or "at most" in field.get("description", ""), (
                    f"{where} states no bound")
            if isinstance(field.get("items"), dict):
                walk(field["items"], f"{where}[]")
    walk(schema())


@pytest.mark.parametrize("field, limit", TOP_LEVEL_BOUNDS + INTENT_BOUNDS)
def test_our_validator_enforces_every_bound_the_schema_can_no_longer_express(field, limit):
    """The point of the whole fix: removing a provider constraint moved nothing. One character over the
    bound is refused - for every bounded field, at both levels."""
    over = "x" * (limit + 1)
    if field in dict(INTENT_BOUNDS):
        # The kind must be one that USES the field, and nothing irrelevant may be filled in, or
        # canonicalization refuses the reply before the bound is reached.
        kind = {"keys": SHORTCUT, "app": OPEN_APP, "why": REFRESH}[field]
        payload = reply(intents=[intent(kind=kind, **{field: over})])
    elif field == "restated":
        payload = reply(restated=over, intents=[intent(app="notepad")])
    elif field in ("question", "missing", "because"):
        defaults = {"question": "Which one?", "missing": "app", "because": "two match"}
        payload = reply(kind="needs_clarification", **{**defaults, field: over})
    else:
        defaults = {"what": "web search", "message": "I can't do that yet."}
        payload = reply(kind="not_supported", **{**defaults, field: over})
    outcome = validate(payload)
    assert isinstance(outcome, brain.InterpretationError), f"{field} over its bound was accepted"
    assert outcome.reason == brain.TOO_LONG


@pytest.mark.parametrize("field, limit", [("restated", MAX_RESTATED), ("why", MAX_WHY)])
def test_why_and_restated_cannot_be_used_as_a_large_output_channel(field, limit):
    """Our own bound, not only the schema's: a reply one character over is refused, not trimmed."""
    if field == "restated":
        payload = reply(restated="x" * (limit + 1), intents=[intent(app="notepad")])
    else:
        payload = reply(intents=[intent(app="notepad", why="x" * (limit + 1))])
    outcome = validate(payload)
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.TOO_LONG


def test_a_typed_payload_over_the_executors_limit_is_refused():
    payload = reply(intents=[intent(kind=TYPE_TEXT, text="x" * (TYPE_LIMIT + 1))])
    outcome = validate(payload)
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.TOO_LONG


# --- §8 The output cap covers the worst reply the schema permits ---------------------------------------

def canonical_max_reply(payloads: int = 1) -> str:
    """The largest raw wire reply validate_interpretation() would ACCEPT, in canonical form.

    Every relevant bound at its maximum - 5 intents, `why` at 120, restatement at 200, `payloads` of
    them carrying a full 1000-character type_text payload in Urdu script, which costs the most bytes -
    and every irrelevant field at its canonical neutral value, because a non-neutral one would now be
    refused and so is not part of any accepted reply.

    This is neither "the worst the schema permits" (the provider cannot express our lengths at all) nor
    the old pre-canonicalization figure, which counted irrelevant fields stuffed to their maximum - a
    shape that is no longer valid."""
    intents = [intent(kind=TYPE_TEXT, why="y" * MAX_WHY, risk_floor="medium",
                      text="ا" * TYPE_LIMIT if position < payloads else "")
               for position in range(MAX_INTENTS)]
    return reply(restated="r" * MAX_RESTATED, intents=intents)


def test_the_configured_output_cap_covers_the_largest_application_valid_reply(fake_claude):
    """The derivation behind cost.max_output_tokens_per_request, checked rather than asserted in a
    comment. If a bound above grows, this fails and the cap has to be re-derived."""
    realistic = cost_controls.estimate_input_tokens(canonical_max_reply(payloads=1))
    assert realistic == 1855, f"the derivation changed: now {realistic} tokens"
    assert realistic < 3072 and 3072 - realistic >= 1000, "the cap keeps real headroom"
    real = Path("config/config.yaml").read_text(encoding="utf-8")
    assert "max_output_tokens_per_request: 3072" in real


def test_canonicalization_made_the_accepted_maximum_smaller_not_larger():
    """The pre-canonicalization figure, 2467, counted irrelevant fields stuffed to their maximum. That
    shape is no longer accepted, so the largest reply we would take is genuinely smaller now."""
    assert cost_controls.estimate_input_tokens(canonical_max_reply(payloads=1)) < 2467


def test_the_one_case_the_cap_does_not_cover_and_what_happens_then():
    """A KNOWN, ACCEPTED LIMITATION, recorded rather than hidden.

    Five intents each carrying a maximal 1000-character Urdu payload is application-valid and needs
    about 5855 estimated tokens - well over the 3072 cap. It means dictating five thousand characters in
    one command, which the cap deliberately does not budget for: covering it would double the output
    reservation on every request for a case that does not occur in desktop use.

    It fails safely rather than silently. Generation reaches the cap, stop_reason is max_tokens, and the
    existing handling refuses the reply - so the user is told the request was too big, and nothing
    half-understood reaches the Planner."""
    theoretical = cost_controls.estimate_input_tokens(canonical_max_reply(payloads=MAX_INTENTS))
    assert theoretical > 3072, theoretical
    assert theoretical == 5855, f"the derivation changed: now {theoretical}"
    truncated = validate(canonical_max_reply(payloads=MAX_INTENTS), stop_reason="max_tokens")
    assert truncated.reason == brain.TRUNCATED
    assert isinstance(validate(canonical_max_reply(payloads=MAX_INTENTS)), Understood), (
        "it is application-valid; it simply does not fit the budget")


def test_the_output_cap_is_only_derivable_while_up_front_thinking_is_off():
    """The derivation above counts the visible JSON and nothing else. That is only the whole story
    because thinking tokens - which count toward max_tokens and are billed as output - are not produced
    up front under this policy. If the policy ever goes back to adaptive, the cap has to be re-derived
    with a thinking allowance, so the two settings are tied together here rather than in a comment."""
    import yaml
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    assert config["brain"]["thinking"] == "between_tools", (
        "the 3072 cap was derived from the schema alone; adaptive thinking would share that budget")
    assert config["cost"]["max_output_tokens_per_request"] == 3072


def test_the_largest_accepted_reply_still_validates_so_the_cap_is_not_cutting_off_valid_work():
    outcome = validate(canonical_max_reply(payloads=1))
    assert isinstance(outcome, Understood) and len(outcome.intents) == MAX_INTENTS
    assert len(outcome.intents[0].args.text) == TYPE_LIMIT


def test_a_reply_larger_than_the_cap_is_refused_by_one_of_the_two_paths():
    """Both safety nets, named. A. it arrives whole but breaks our bounds -> validator refuses.
    B. generation hits the cap -> the max_tokens stop reason refuses it."""
    too_big = reply(restated="r" * (MAX_RESTATED + 1), intents=[intent(app="notepad")])
    assert validate(too_big).reason == brain.TOO_LONG
    assert validate(reply(intents=[intent(app="notepad")]),
                    stop_reason="max_tokens").reason == brain.TRUNCATED


# --- §9 Input accounting includes everything that will be billed --------------------------------------

def test_the_estimate_counts_the_system_prompt_the_user_text_and_the_schema(fake_claude):
    """Not just the user's command. The schema alone is over a thousand estimated tokens, so counting
    only the command would under-reserve every request by an order of magnitude."""
    from app.brain import adapter
    billable, overhead = adapter._billable_input("open notepad", brain.SYSTEM_PROMPT, schema())
    for part in ("open notepad", "understanding step", '"json_schema"' if False else "risk_floor"):
        assert part in billable
    assert overhead == 8, "from the test config"
    only_command = cost_controls.estimate_input_tokens("open notepad")
    assert cost_controls.estimate_input_tokens(billable) > only_command * 50


def test_the_injected_structured_output_prompt_is_counted_even_though_it_is_not_text(fake_claude):
    from app.brain import adapter
    _billable, overhead = adapter._billable_input("open notepad", brain.SYSTEM_PROMPT, schema())
    assert overhead > 0
    _billable, none = adapter._billable_input("open notepad", brain.SYSTEM_PROMPT, None)
    assert none == 0, "nothing is injected when no format is requested"


def test_the_overhead_is_added_to_the_reservation_not_ignored(fake_claude):
    fake_claude.configure(max_input_tokens=100000)
    plain = cost_controls.authorize(model="test-model", prompt="hello", max_tokens=10)
    withoverhead = cost_controls.authorize(model="test-model", prompt="hello", max_tokens=10,
                                          overhead_tokens=500)
    import sqlite3
    with sqlite3.connect(fake_claude.ledger_path) as conn:
        rows = dict(conn.execute("SELECT id, estimated_input_tokens FROM claude_requests").fetchall())
    assert rows[withoverhead] == rows[plain] + 500


def test_the_estimate_helper_reports_what_would_be_reserved(fake_claude):
    """What the real smoke compares against the provider's own count. It sends nothing."""
    from app.brain import adapter
    fake_claude.configure()
    estimated = adapter.estimated_input_tokens("open notepad", brain.SYSTEM_PROMPT, schema())
    billable, overhead = adapter._billable_input("open notepad", brain.SYSTEM_PROMPT, schema())
    assert estimated == cost_controls.estimate_input_tokens(billable) + overhead
    assert fake_claude.requests == []


def test_the_estimator_over_counts_ascii_which_is_why_it_is_the_conservative_side(fake_claude):
    """2 bytes per token against a real tokenizer's ~4 for English: the estimate should sit well above
    any plausible real count for an ASCII prompt. The real smoke is what confirms it end to end."""
    fake_claude.configure()
    from app.brain import adapter
    text = "please open notepad" * 20
    assert cost_controls.estimate_input_tokens(text) >= len(text.split()) * 2


def test_an_over_large_request_is_refused_before_it_is_sent(fake_claude):
    """The schema plus the system prompt are most of the input, so the token limit has to be checked
    against the whole thing."""
    fake_claude.configure(max_input_tokens=50)
    with pytest.raises(cost_controls.CostLimitError):
        cost_controls.authorize(model="test-model", prompt="x" * 40, max_tokens=10,
                                overhead_tokens=512)
    assert fake_claude.requests == [], "nothing was sent"


# --- §10/§11 The real request shape passes through the existing controls -------------------------------

def structured_response(payload: str, input_tokens=120, output_tokens=40):
    def respond(request):
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": payload}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}})
    return respond


def test_a_structured_request_is_authorized_reserved_and_settled(fake_claude):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000, max_output_tokens=3072)
    payload = reply(restated="open notepad", intents=[intent(app="notepad", why="you asked")])
    fake_claude.respond = structured_response(payload)
    result = adapter.send_message("open notepad", max_tokens=1024, system=brain.SYSTEM_PROMPT,
                                 output_schema=schema())
    assert result.text == payload
    import sqlite3
    with sqlite3.connect(fake_claude.ledger_path) as conn:
        rows = conn.execute("SELECT model, max_tokens, cost_usd FROM claude_requests").fetchall()
    assert len(rows) == 1 and rows[0][1] == 1024
    assert rows[0][2] == pytest.approx((120 * 5.0 + 40 * 25.0) / 1e6), "settled at actual usage"


def test_the_request_on_the_wire_carries_the_system_prompt_and_the_schema(fake_claude):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = structured_response(reply(kind="not_a_command", message="Hello."))
    adapter.send_message("hello", max_tokens=64, system=brain.SYSTEM_PROMPT, output_schema=schema())
    sent = json.loads(fake_claude.requests[-1].content)
    assert sent["system"] == brain.SYSTEM_PROMPT
    # output_config also carries `effort` since the thinking-policy amendment; that is asserted by
    # test_the_request_carries_the_approved_thinking_policy. Here the schema itself is what matters.
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": schema()}
    assert sent["messages"] == [{"role": "user", "content": "hello"}]
    assert "temperature" not in sent and "top_p" not in sent


def test_the_request_carries_the_approved_thinking_policy(fake_claude):
    """Sonnet 5.5 thinks by default at `high` effort, and thinking tokens count toward max_tokens and
    are billed as output. This request turns up-front thinking off, which is what makes the output cap
    derivable from the schema alone."""
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = structured_response(reply(kind="not_a_command", message="Hello."))
    adapter.send_message("hello", max_tokens=64, system=brain.SYSTEM_PROMPT, output_schema=schema())
    sent = json.loads(fake_claude.requests[-1].content)
    assert sent["thinking"] == {"type": "between_tools"}
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": schema()}
    assert sent["model"] == "test-model", "the model is whatever config says; the real one is checked below"
    for gone in ("temperature", "top_p", "top_k"):
        assert gone not in sent, gone
    assert "tools" not in sent, "no tools: the condition under which between_tools means text only"


def test_the_real_config_asks_for_the_approved_policy_on_the_selected_model():
    import yaml
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    assert config["brain"]["model"] == "claude-sonnet-5-5"
    assert config["brain"]["thinking"] == "between_tools"
    assert config["brain"]["effort"] == "medium"


def test_the_thinking_policy_reaches_even_the_health_ping(fake_claude):
    """One request path, one policy. It also closes a latent problem: ping asks for 256 output tokens,
    which adaptive thinking would have had to share."""
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = structured_response("OK")
    adapter.ping()
    sent = json.loads(fake_claude.requests[-1].content)
    assert sent["thinking"] == {"type": "between_tools"}
    assert "format" not in sent["output_config"], "no schema on a ping"


def test_the_effort_and_thinking_values_are_configuration_not_code():
    """So acceptance can revise them without touching the adapter (CLAUDE.md rule 3)."""
    source = Path("app/brain/adapter.py").read_text(encoding="utf-8")
    assert 'get_setting("brain.thinking")' in source
    assert 'get_setting("brain.effort")' in source
    assert '"between_tools"' not in source, "the value is not hardcoded"
    assert '"medium"' not in source, "the value is not hardcoded"


def test_effort_stays_within_what_between_tools_allows(fake_claude):
    """Documented: between_tools is rejected with a 400 at `xhigh` or `max`. The config must not ask for
    a combination the API refuses."""
    import yaml
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    if config["brain"]["thinking"] == "between_tools":
        assert config["brain"]["effort"] in ("low", "medium", "high"), config["brain"]["effort"]


def test_nothing_is_sent_when_a_cost_control_blocks_it(fake_claude):
    from app.brain import adapter
    fake_claude.configure(daily_usd=0.0)
    with pytest.raises(cost_controls.CostLimitError):
        adapter.send_message("open notepad", max_tokens=1024, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == []


def test_only_the_adapter_imports_anthropic():
    for path in Path("app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        if "anthropic" in imported:
            assert path.as_posix() == "app/brain/adapter.py", path


def test_the_semantic_half_does_not_read_settings():
    """Provider specifics in the adapter, semantics in logic: the schema's one variable - the typed-text
    limit - is passed in, so brain/logic.py never reaches for configuration."""
    source = Path("app/brain/logic.py").read_text(encoding="utf-8")
    assert "get_setting" not in source
    assert "config.settings" not in source


# --- §5 The system prompt is Phase 3 shaped -----------------------------------------------------------

def test_the_system_prompt_names_only_implemented_capabilities():
    """Every implemented kind is described to the model.

    This test deliberately stops there. The prompt also names email and web search, as examples of what
    is NOT supported, so any negative search here would be testing my own prose rather than a property
    of the system. What the model may actually ASK FOR is enforced by the schema, not by the wording -
    see test_the_action_kinds_come_from_the_executors_own_vocabulary and
    test_the_model_cannot_name_a_capability_that_does_not_exist, which read the enum itself."""
    for kind in ARGS_FOR_KIND:
        assert kind in brain.SYSTEM_PROMPT, kind


def test_the_system_prompt_states_the_rules_this_phase_depends_on():
    prompt = brain.SYSTEM_PROMPT.lower()
    assert "data" in prompt and "instruction" in prompt, "external content is data (CLAUDE.md rule 7)"
    assert "never say an action was done" in prompt
    assert "at most 5 intents" in prompt
    assert "floor" in prompt and "never lowers" in prompt
    assert "roman urdu" in prompt and "hindi" in prompt
    assert "do not transliterate" in prompt
    for forbidden in ("shell", "code", "registry"):
        assert forbidden in prompt, forbidden


def test_the_system_prompt_contains_no_private_or_historical_data():
    assert SECRET not in brain.SYSTEM_PROMPT
    assert "hunter2" not in brain.SYSTEM_PROMPT
    # 4400 since 2026-10-07, raised once and deliberately: the limit is a DISCIPLINE limit, and
    # app/brain/logic.py records the measurement that showed the cost of 400 more characters is
    # about $0.0004 accounted per request against a $1.00 daily budget.
    assert len(brain.SYSTEM_PROMPT) < 4400, "a prompt this phase can afford on every request"


# --- §4/§15 The three request shapes, and what may not get into them ----------------------------------

def test_an_initial_request_carries_the_text_and_the_safe_previous_action():
    built = brain.interpretation_request("close it", previous_action_context(OPEN_APP, "notepad"))
    assert "close it" in built
    assert "open_app" in built and "notepad" in built


def test_an_initial_request_without_context_says_nothing_about_a_previous_action():
    built = brain.interpretation_request("open notepad")
    assert "notepad" in built and "last thing actually done" not in built


def test_a_typed_payload_can_never_reach_the_prompt_through_previous_action_context():
    """previous_action_context() already drops it; this proves the request builder cannot reintroduce
    it, because there is nothing left to reintroduce."""
    context = previous_action_context(TYPE_TEXT, SECRET)
    built = brain.interpretation_request("do that again", context)
    assert SECRET not in built and "hunter2" not in built
    assert context.safe_target is None


def test_a_clarification_request_carries_the_original_the_question_the_field_and_the_answer():
    continuation = ClarificationContinuation(original_text="open the editor",
                                             question="Which editor did you mean?",
                                             missing="app", answer="notepad")
    built = brain.clarification_request(continuation)
    for part in ("open the editor", "Which editor did you mean?", "app", "notepad"):
        assert part in built, part


def test_a_replan_request_carries_the_correction_the_safe_summary_and_what_was_done():
    request = ReplanRequest(
        original_text="open the calculator and notepad", correction="use wordpad instead",
        steps=(PlanStepSummary(1, OPEN_APP, "calculator", "you asked", True),
               PlanStepSummary(2, OPEN_APP, "notepad", "then this", False)),
        completed_steps=(1,), failure=FailureContext(2, "I couldn't find notepad."))
    built = brain.replan_request(request)
    for part in ("open the calculator and notepad", "use wordpad instead", "step 1", "step 2",
                 "calculator", "I couldn't find notepad."):
        assert part in built, part
    assert "must not be repeated: 1" in built


def test_a_replan_request_describes_a_typed_step_by_its_length_not_its_text():
    """The summary comes from app/planner/logic.plan_summary(), which uses log_label. If a future change
    put the payload in the summary, this catches it at the boundary where it would be sent."""
    request = ReplanRequest(original_text="type it", correction="say it differently",
                            steps=(PlanStepSummary(1, TYPE_TEXT, f"{len(SECRET)} characters",
                                                   "you asked", True),),
                            completed_steps=(1,))
    built = brain.replan_request(request)
    assert SECRET not in built and "hunter2" not in built
    assert f"{len(SECRET)} characters" in built


def test_an_exception_cannot_be_serialised_into_a_replan_request():
    """FailureContext refuses anything but a string at the lifecycle boundary; here we prove that what
    reaches the prompt is a sentence, and that a raw exception has nowhere to go."""
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal, Plan, PlanStep, TurnContext, PLAN_RUNNING
    from app.executor.models import ExecutorAction
    plan = Plan(steps=(PlanStep(1, ExecutorAction(OPEN_APP, "notepad")),))
    context = session.propose_plan(TurnContext(), plan, "open notepad", "plan-1")
    context = session.accept_plan(context, "plan-1")
    assert isinstance(session.fail_plan(context, 1, RuntimeError("boom")), LifecycleRefusal)
    assert isinstance(session.fail_plan(context, 1, Exception("trace")), LifecycleRefusal)


def test_a_plan_id_is_never_sent_to_the_model():
    """Lifecycle metadata, not semantics: the model has no business knowing or choosing one."""
    request = ReplanRequest(original_text="open both", correction="differently",
                            steps=(PlanStepSummary(1, OPEN_APP, "notepad", "", False),))
    built = brain.replan_request(request)
    assert "plan_id" not in built and "plan-1" not in built
    assert not any(field.name == "plan_id" for field in __import__("dataclasses").fields(ReplanRequest))


# --- §12 A structured reply is still untrusted ---------------------------------------------------------

def test_a_good_reply_becomes_the_typed_contracts():
    payload = reply(restated="open notepad then type hello",
                    intents=[intent(app="notepad", why="you asked", risk_floor="low"),
                             intent(kind=TYPE_TEXT, text="hello", why="then this",
                                    risk_floor="medium")])
    outcome = validate(payload)
    assert isinstance(outcome, Understood)
    assert [type(step.args) for step in outcome.intents] == [OpenAppArgs, TypeTextArgs]
    assert outcome.intents[0].args == OpenAppArgs(app="notepad")
    assert outcome.intents[1].args == TypeTextArgs(text="hello")
    assert outcome.intents[1].risk_floor is RiskLevel.MEDIUM


@pytest.mark.parametrize("kind, fields, expected", [
    (OPEN_APP, {"app": "notepad"}, OpenAppArgs("notepad")),
    (CLOSE_APP, {"app": "calculator"}, None),
    (CLICK, {"x": 500, "y": 300}, ClickArgs(500, 300)),
    (TYPE_TEXT, {"text": "hello"}, TypeTextArgs("hello")),
    (SHORTCUT, {"keys": "ctrl+c"}, ShortcutArgs("ctrl+c")),
    (SCROLL, {"direction": "down", "notches": 3}, ScrollArgs("down", 3)),
    (REFRESH, {}, RefreshArgs()),
    (WINDOW_CONTROL, {"operation": "minimize"}, WindowControlArgs("minimize")),
    (CLICK_TARGET, {"control": "Seven", "app": "calculator"},
     ClickTargetArgs(control="Seven", app="calculator")),
    (OPEN_BROWSER, {}, OpenBrowserArgs()),
    (CLOSE_BROWSER, {}, CloseBrowserArgs()),
    (NAVIGATE, {"url": "https://example.com"}, NavigateArgs("https://example.com")),
])
def test_every_kind_builds_its_own_typed_args_shape(kind, fields, expected):
    outcome = validate(reply(intents=[intent(kind=kind, **fields)]))
    assert isinstance(outcome, Understood)
    built = outcome.intents[0].args
    assert type(built) is ARGS_FOR_KIND[kind]
    if expected is not None:
        assert built == expected


def test_this_file_covers_every_kind_there_is():
    """The list above is written out rather than derived, so that each kind's FIELDS are stated
    explicitly. The cost is that adding a capability can silently leave it uncovered - which is what
    happened when click_target arrived - so the list's completeness is asserted here instead."""
    cases = test_every_kind_builds_its_own_typed_args_shape.pytestmark[0].args[1]
    covered = {case[0] for case in cases}
    assert covered == set(ARGS_FOR_KIND), set(ARGS_FOR_KIND) - covered


def test_a_placeholder_field_for_another_kind_makes_the_whole_reply_invalid():
    """This behaviour CHANGED in the wire-canonicalization audit. It used to be accepted and the
    irrelevant fields ignored, which was execution-safe but left the wire with a channel for content
    nobody asked for, and left the output cap underivable. Now it is refused outright."""
    outcome = validate(reply(intents=[intent(kind=REFRESH, app="photoshop", text=SECRET, x=9, y=9,
                                             keys="ctrl+q", operation="close")]))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason == brain.NOT_CANONICAL
    assert SECRET not in repr(outcome) and "photoshop" not in repr(outcome)


def test_the_canonical_form_of_every_kind_is_accepted():
    """The other half: filling in only what the kind uses works for all eight."""
    for kind, fields in [(OPEN_APP, {"app": "notepad"}), (CLOSE_APP, {"app": "notepad"}),
                         (CLICK, {"x": 5, "y": 6}), (TYPE_TEXT, {"text": "hi"}),
                         (SHORTCUT, {"keys": "ctrl+c"}),
                         (SCROLL, {"direction": "down", "notches": 3}), (REFRESH, {}),
                         (WINDOW_CONTROL, {"operation": "minimize"}),
                         (CLICK_TARGET, {"control": "Seven", "app": "calculator"}),
                         (OPEN_BROWSER, {}), (CLOSE_BROWSER, {})]:
        outcome = validate(reply(intents=[intent(kind=kind, **fields)]))
        assert isinstance(outcome, Understood), f"{kind}: {getattr(outcome, 'detail', outcome)}"
        assert type(outcome.intents[0].args) is ARGS_FOR_KIND[kind]


def test_the_other_three_interpretations_validate():
    clarify = validate(reply(kind="needs_clarification", question="Which editor?", missing="app",
                             because="there are two"))
    assert clarify == NeedsClarification(question="Which editor?", missing="app",
                                         because="there are two")
    unsupported = validate(reply(kind="not_supported", what="web search",
                                 message="I can't search the web yet."))
    assert unsupported == NotSupported(what="web search", message="I can't search the web yet.")
    chatter = validate(reply(kind="not_a_command", message="Hello."))
    assert chatter == NotACommand(message="Hello.")


@pytest.mark.parametrize("payload, reason", [
    ("", brain.INVALID_JSON),
    ("not json at all", brain.INVALID_JSON),
    ('{"kind": "understood"', brain.INVALID_JSON),
    ("[]", brain.INVALID_SHAPE),
    ('"understood"', brain.INVALID_SHAPE),
    ("null", brain.INVALID_SHAPE),
])
def test_a_reply_that_is_not_an_object_is_refused(payload, reason):
    outcome = validate(payload)
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == reason


@pytest.mark.parametrize("kind", ["", "understand", "UNDERSTOOD", "plan", "tool_use", None, 7])
def test_an_unknown_interpretation_variant_is_refused(kind):
    outcome = validate(reply(kind=kind))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason == brain.UNKNOWN_INTERPRETATION


@pytest.mark.parametrize("count", [0, 6, 7, 20])
def test_an_understood_reply_must_have_one_to_five_intents(count):
    outcome = validate(reply(intents=[intent(app="notepad")] * count))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason == brain.BAD_INTENT_COUNT


@pytest.mark.parametrize("kind", ["shell", "run_command", "send_email", "", "open app", "OPEN_APP"])
def test_an_unknown_action_kind_is_refused(kind):
    outcome = validate(reply(intents=[intent(kind=kind)]))
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.UNKNOWN_KIND


@pytest.mark.parametrize("kind, fields", [
    (CLICK, {"x": "500", "y": 300}),
    (CLICK, {"x": True, "y": 3}),
    (CLICK, {"x": 1.5, "y": 3}),
    (SCROLL, {"direction": "left", "notches": 3}),
    (SCROLL, {"direction": "down", "notches": "3"}),
    (SCROLL, {"direction": "", "notches": 3}),
    (WINDOW_CONTROL, {"operation": "sideways"}),
    (WINDOW_CONTROL, {"operation": ""}),
    (OPEN_APP, {"app": 7}),
    (TYPE_TEXT, {"text": None}),
])
def test_args_that_do_not_fit_the_kind_are_refused(kind, fields):
    outcome = validate(reply(intents=[intent(kind=kind, **fields)]))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason in (brain.BAD_ARGS, brain.INVALID_SHAPE)


def test_a_missing_required_field_is_refused():
    incomplete = intent(kind=CLICK, x=1, y=2)
    del incomplete["y"]
    outcome = validate(reply(intents=[incomplete]))
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.MISSING_FIELD


@pytest.mark.parametrize("floor", ["", "LOW", "urgent", "1", None, 2, "none"])
def test_a_risk_floor_outside_the_scale_is_refused(floor):
    outcome = validate(reply(intents=[intent(app="notepad", risk_floor=floor)]))
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.BAD_RISK_FLOOR


@pytest.mark.parametrize("floor, level", list(RISK_FLOOR_NAMES.items()))
def test_each_named_risk_floor_maps_to_the_existing_scale(floor, level):
    outcome = validate(reply(intents=[intent(app="notepad", risk_floor=floor)]))
    assert outcome.intents[0].risk_floor is level
    assert isinstance(level, RiskLevel)


@pytest.mark.parametrize("question", ["", "   ", None, 7])
def test_a_clarification_without_a_real_question_is_refused(question):
    outcome = validate(reply(kind="needs_clarification", question=question))
    assert isinstance(outcome, brain.InterpretationError)
    assert outcome.reason in (brain.MISSING_FIELD, brain.INVALID_SHAPE)


def test_a_not_supported_reply_must_name_what_is_missing():
    outcome = validate(reply(kind="not_supported", what="   "))
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.MISSING_FIELD


def test_a_truncated_reply_is_refused_rather_than_parsed():
    good = reply(intents=[intent(app="notepad")])
    assert isinstance(validate(good), Understood)
    outcome = validate(good, stop_reason="max_tokens")
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == brain.TRUNCATED


@pytest.mark.parametrize("stop_reason, expected", [
    ("max_tokens", brain.TRUNCATED),
    ("refusal", brain.REFUSED),
    ("model_context_window_exceeded", brain.CONTEXT_EXCEEDED),
])
def test_an_unreadable_stop_reason_is_refused_whatever_the_content_says(stop_reason, expected):
    """All three are current stop reasons. Even a perfectly well-formed body is not read, because the
    reason says there is no complete answer in it."""
    good = reply(intents=[intent(app="notepad")])
    assert isinstance(validate(good), Understood), "the same body is fine without the stop reason"
    outcome = validate(good, stop_reason=stop_reason)
    assert isinstance(outcome, brain.InterpretationError) and outcome.reason == expected


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "model_context_window_exceeded"])
def test_an_unreadable_stop_reason_reaches_no_planner_and_is_not_retried(stop_reason, monkeypatch):
    from app.brain import adapter
    from app.brain.models import INTERPRETATIONS
    monkeypatch.setattr(adapter, "send_message", lambda *a, **k: pytest.fail("no automatic retry"))
    outcome = validate(reply(intents=[intent(app="notepad")]), stop_reason=stop_reason)
    assert not isinstance(outcome, INTERPRETATIONS)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "model_context_window_exceeded"])
def test_an_unreadable_stop_reason_says_nothing_about_provider_content(stop_reason):
    """No refusal category, no stop_details, no fragment of the partial reply - the detail names the
    condition only."""
    payload = reply(restated=SECRET, intents=[intent(app="notepad", why=SECRET)])
    outcome = validate(payload, stop_reason=stop_reason)
    shown = f"{outcome.reason} {outcome.detail}"
    assert SECRET not in shown and "hunter2" not in shown
    assert "notepad" not in shown and "restated" not in shown


def test_a_refusal_is_not_answered_by_trying_another_model():
    """The documentation suggests retrying a refusal on a fallback model. Phase 3 has one model by
    design, so a refusal is simply the AI-unavailable path - and there is no second model configured to
    fall back to."""
    import yaml
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    assert "fallback_model" not in config["brain"]
    source = Path("app/brain/adapter.py").read_text(encoding="utf-8")
    assert "fallback" not in source.lower()


def test_every_unreadable_stop_reason_is_listed_in_one_place():
    assert set(brain.UNREADABLE_STOP_REASONS) == {"max_tokens", "refusal",
                                                  "model_context_window_exceeded"}
    for reason, detail in brain.UNREADABLE_STOP_REASONS.values():
        assert reason and detail and detail == detail.strip()


@pytest.mark.parametrize("stop_reason", ["end_turn", None, "stop_sequence"])
def test_an_ordinary_stop_reason_still_reads_the_reply(stop_reason):
    outcome = validate(reply(intents=[intent(app="notepad")]), stop_reason=stop_reason)
    assert isinstance(outcome, Understood)


def test_malformed_json_is_never_repaired():
    """No silent fixing: a reply with a trailing comma or a stray fence is refused, not cleaned up."""
    for broken in ('{"kind": "understood",}', '```json\n{"kind":"understood"}\n```',
                   "{'kind': 'understood'}"):
        assert isinstance(validate(broken), brain.InterpretationError)


def test_a_refusal_never_quotes_the_field_it_rejected():
    """The detail names the place, not the contents - the contents may be the user's own words."""
    outcome = validate(reply(intents=[intent(kind=TYPE_TEXT, text="x" * (TYPE_LIMIT + 1),
                                             why=SECRET)]))
    assert isinstance(outcome, brain.InterpretationError)
    assert SECRET not in outcome.detail and "hunter2" not in repr(outcome)
    assert "x" * 50 not in outcome.detail


def test_a_refused_reply_produces_no_interpretation_at_all():
    """It can never reach the Planner, because there is nothing a Planner would accept: an
    InterpretationError is not an Interpretation."""
    from app.brain.models import INTERPRETATIONS
    outcome = validate("garbage")
    assert not isinstance(outcome, INTERPRETATIONS)
    assert isinstance(outcome, brain.InterpretationError)


def test_validation_is_pure_and_retries_nothing(monkeypatch):
    from app.brain import adapter
    monkeypatch.setattr(adapter, "send_message", lambda *a, **k: pytest.fail("no automatic retry"))
    for payload in ("garbage", reply(kind="not_a_command"), reply(intents=[intent(app="notepad")])):
        validate(payload)


# --- §14 Multilingual input needs no special path -----------------------------------------------------

MULTILINGUAL = [
    ("Roman Urdu", "notepad kholo"),
    ("Urdu script", "نوٹ پیڈ کھولو"),
    ("Devanagari", "नोटपैड खोलो"),
    ("mixed English and Urdu", "notepad کھولو please"),
    ("mixed English and Hindi", "please खोलो notepad"),
]


@pytest.mark.parametrize("label, text", MULTILINGUAL, ids=[case[0] for case in MULTILINGUAL])
def test_a_request_in_any_script_reaches_the_prompt_unchanged(label, text):
    """No transliteration anywhere: the bytes the user gave are the bytes that go out."""
    built = brain.interpretation_request(text)
    assert text in built


@pytest.mark.parametrize("label, text", MULTILINGUAL, ids=[case[0] for case in MULTILINGUAL])
def test_a_restatement_in_any_script_validates_unchanged(label, text):
    """Proves the schema and the validator accept Unicode as-is. It says nothing about how well the
    model understands it - a mocked reply cannot show that."""
    outcome = validate(reply(restated=text, intents=[intent(app="notepad", why=text)]))
    assert isinstance(outcome, Understood)
    assert outcome.restated == text
    assert outcome.intents[0].why == text


def test_a_typed_payload_in_urdu_script_survives_validation_exactly():
    payload = "میں نے یہ لکھا"
    outcome = validate(reply(intents=[intent(kind=TYPE_TEXT, text=payload)]))
    assert outcome.intents[0].args.text == payload


def test_the_text_bound_counts_characters_so_urdu_is_not_penalised():
    """maxLength in JSON schema is characters, and our check is len() - so a 1000-character Urdu
    payload is as acceptable as a 1000-character English one, even though it is more bytes."""
    urdu = "ا" * TYPE_LIMIT
    assert len(urdu.encode("utf-8")) > TYPE_LIMIT
    assert isinstance(validate(reply(intents=[intent(kind=TYPE_TEXT, text=urdu)])), Understood)


# --- §16 Provider and cost failures -------------------------------------------------------------------

def test_a_missing_api_key_fails_before_anything_is_sent(fake_claude, monkeypatch):
    from app.brain import adapter
    from config.settings import SettingsError
    fake_claude.env_path.write_text("", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises((SettingsError, adapter.ClaudeAuthError)):
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == []


@pytest.mark.parametrize("overrides, note", [
    ({"rate_limit": 0}, "rate limit"),
    ({"max_input_tokens": 1}, "input token limit"),
    ({"daily_usd": 0.0}, "daily budget"),
    ({"monthly_usd": 0.0}, "monthly budget"),
])
def test_each_cost_control_blocks_the_structured_request(fake_claude, overrides, note):
    from app.brain import adapter
    fake_claude.configure(**overrides)
    with pytest.raises(cost_controls.CostLimitError):
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == [], note


def test_asking_for_more_output_than_allowed_is_blocked(fake_claude):
    from app.brain import adapter
    fake_claude.configure(max_output_tokens=100, max_input_tokens=100000)
    with pytest.raises(cost_controls.CostLimitError):
        adapter.send_message("open notepad", max_tokens=101, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == []


def test_an_unpriced_model_cannot_be_called_at_all(fake_claude):
    """Fail closed: the price table is what makes a budget possible, so a model without one is blocked
    even though the API would happily answer."""
    from app.brain import adapter
    fake_claude.configure(model="claude-not-in-the-price-table")
    with pytest.raises(cost_controls.CostLimitError):
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == []


def test_the_configured_model_has_a_price(fake_claude):
    """The real config, not the test one: brain.model and the price table must agree, or every request
    fails closed at runtime."""
    import yaml
    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    model = config["brain"]["model"]
    prices = config["cost"]["prices_usd_per_million_tokens"]
    assert model in prices, f"{model} has no price entry"
    assert prices[model] == {"input": 2.00, "output": 10.00}, "verified Sonnet 5.5 pricing"
    assert model == "claude-sonnet-5-5"


def test_an_unusable_ledger_blocks_the_request(fake_claude):
    from app.brain import adapter
    fake_claude.configure()
    fake_claude.ledger_path.write_bytes(b"this is not a database")
    with pytest.raises(cost_controls.CostLimitError):
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert fake_claude.requests == []


@pytest.mark.parametrize("status, expected", [(401, "ClaudeAuthError"), (403, "ClaudeAuthError"),
                                              (404, "ClaudeRequestError"),
                                              (429, "ClaudeUnavailableError"),
                                              (400, "ClaudeRequestError"),
                                              (500, "ClaudeUnavailableError"),
                                              (503, "ClaudeUnavailableError")])
def test_a_provider_error_becomes_a_safe_translated_failure(fake_claude, status, expected):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = lambda request: httpx2.Response(status, json={
        "type": "error", "error": {"type": "x", "message": "provider detail"}})
    with pytest.raises(adapter.ClaudeError) as caught:
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())
    assert type(caught.value).__name__ == expected
    assert caught.value.__cause__ is None, "no raw SDK exception is chained"


@pytest.mark.parametrize("failure", [httpx2.ConnectError("no route"), httpx2.ReadTimeout("slow")])
def test_a_transport_failure_becomes_unavailable_not_a_crash(fake_claude, failure):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)

    def explode(request):
        raise failure

    fake_claude.respond = explode
    with pytest.raises(adapter.ClaudeUnavailableError):
        adapter.send_message("open notepad", max_tokens=64, system=brain.SYSTEM_PROMPT,
                            output_schema=schema())


# --- §13 Privacy: what may and may not be logged ------------------------------------------------------

def test_the_request_log_line_carries_metadata_and_no_content(fake_claude, caplog):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = structured_response(reply(restated=SECRET,
                                                    intents=[intent(app="notepad", why=SECRET)]))
    with caplog.at_level("INFO"):
        adapter.send_message(brain.interpretation_request(SECRET), max_tokens=64,
                            system=brain.SYSTEM_PROMPT, output_schema=schema())
    logged = caplog.text
    assert SECRET not in logged and "hunter2" not in logged
    assert "understanding step" not in logged, "the system prompt is not logged"
    assert "json_schema" not in logged and "risk_floor" not in logged, "the schema is not logged"
    for metadata in ("model=", "max_tokens=", "structured=True", "estimated_input_tokens=",
                     "input_tokens=", "output_tokens=", "cost_usd=", "stop_reason="):
        assert metadata in logged, metadata


def test_a_failure_log_line_carries_no_content(fake_claude, caplog):
    from app.brain import adapter
    fake_claude.configure(max_input_tokens=100000)
    fake_claude.respond = lambda request: httpx2.Response(500, json={"type": "error", "error": {}})
    with caplog.at_level("WARNING"):
        with pytest.raises(adapter.ClaudeError):
            adapter.send_message(brain.interpretation_request(SECRET), max_tokens=64,
                                system=brain.SYSTEM_PROMPT, output_schema=schema())
    assert SECRET not in caplog.text and "hunter2" not in caplog.text


def test_a_cost_refusal_message_carries_no_content(fake_claude):
    from app.brain import adapter
    fake_claude.configure(daily_usd=0.0)
    with pytest.raises(cost_controls.CostLimitError) as caught:
        adapter.send_message(brain.interpretation_request(SECRET), max_tokens=64,
                            system=brain.SYSTEM_PROMPT, output_schema=schema())
    assert SECRET not in str(caught.value)


def test_model_prose_stays_out_of_every_repr():
    """This closes the residual Slice 1B concern: `why` and `restated` are display text, and a model
    could echo private words into either, so neither appears in a repr that might be logged."""
    outcome = validate(reply(restated=SECRET, intents=[intent(app="notepad", why=SECRET)]))
    assert isinstance(outcome, Understood)
    assert SECRET not in repr(outcome), "restated is redacted"
    assert outcome.restated == SECRET, "and still available to show the user"
    from app.planner.models import PlanStep
    from app.executor.models import ExecutorAction
    from app.brain.models import Intent
    step = PlanStep(1, ExecutorAction(OPEN_APP, "notepad"), why=SECRET)
    assert SECRET not in repr(step) and step.why == SECRET
    reason = Intent(OPEN_APP, OpenAppArgs("notepad"), why=SECRET)
    assert SECRET not in repr(reason) and reason.why == SECRET
    assert f"<{len(SECRET)} characters>" in repr(step)


def test_no_module_in_this_slice_logs_a_prompt_or_a_reply():
    """Structural: the only logging calls in the provider path pass metadata, never the prompt, the
    schema or the reply text."""
    source = Path("app/brain/adapter.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
            continue
        for argument in node.args:
            shown = ast.unparse(argument)
            # Whole expressions, not substrings: "output_schema is not None" is a boolean saying
            # WHETHER a schema was sent, which is metadata; `output_schema` itself would be content.
            assert shown not in ("prompt", "billable", "output_schema", "system", "reply.text",
                                 "reply", "self.prompt"), f"log call passes content: {shown}"
            assert "SYSTEM_PROMPT" not in shown, shown


# --- §17 The deterministic path still needs no provider -----------------------------------------------

def test_the_frozen_unavailable_message_is_exactly_as_agreed():
    assert brain.UNAVAILABLE_MESSAGE == ("I can't reach my reasoning service right now, so I can only "
                                         "do direct commands until it's back.")


@pytest.mark.parametrize("text", ["open notepad", "type hello world", "minimize", "refresh",
                                  "scroll down 3", "shortcut ctrl+a", "click 500, 300"])
def test_a_resolved_deterministic_command_never_needs_the_provider(text, monkeypatch):
    from app.brain import adapter
    from app.executor.logic import resolve
    monkeypatch.setattr(adapter, "send_message", lambda *a, **k: pytest.fail("no provider needed"))
    assert isinstance(brain.route(text, resolve), brain.LocalAction)


@pytest.mark.parametrize("text", ["open the calculator", "close everything", "notepad kholo",
                                  "aaj mausam kaisa hai?"])
def test_only_brain_eligible_text_is_ever_a_provider_candidate(text):
    from app.executor.logic import resolve
    assert isinstance(brain.route(text, resolve), brain.BrainEligible)


@pytest.mark.parametrize("text", ["open the calculator", "notepad kholo", "close everything"])
def test_provider_unavailable_never_turns_eligible_text_into_an_action(text):
    """The whole point of the fallback contract: when the Brain cannot be reached there is no
    interpretation, so there is nothing to execute - only something to say."""
    from app.executor.logic import resolve
    outcome = brain.route(text, resolve)
    assert not isinstance(outcome, brain.LocalAction)
    assert isinstance(validate("provider never answered"), brain.InterpretationError)
    assert outcome.local_message and brain.UNAVAILABLE_MESSAGE


def test_the_deterministic_parser_was_not_changed():
    from app.executor import commands
    parsed = commands.parse("open notepad")
    assert parsed.kind == OPEN_APP and parsed.target == "notepad"
    assert isinstance(commands.parse("notepad kholo"), commands.CommandRefusal)


# --- §18 The opt-in real call. PREPARED, NOT RUN. -----------------------------------------------------

@pytest.mark.real_api
def test_a_real_sonnet_call_returns_a_planable_interpretation():
    """PREPARED IN SLICE 2 AND DELIBERATELY NOT RUN. It is skipped unless RUN_REAL_CLAUDE_TEST=1, which
    this slice does not set.

    When it is approved, it proves end to end that: the verified model id is callable; the cost controls
    authorize, reserve and settle a real request; a harmless natural sentence comes back as a
    schema-valid Understood interpretation that the existing pure Planner will accept; and nothing is
    executed - no Safety call, no Executor call, no window, no microphone, no speaker.

    It uses no private text: the request is "please open notepad" and nothing else. The assertions are
    on SHAPE, never on wording, so it cannot fail for a stylistic change in the model's reply.

    IT ALSO CHECKS OUR OWN ESTIMATOR. brain.structured_output_overhead_tokens is a conservative guess,
    because Anthropic does not publish the size of the system prompt it injects for structured output.
    This test is the only way to find out whether the guess is big enough, so it REQUIRES

        locally estimated input tokens >= provider-reported usage.input_tokens

    and fails with a metadata-only diagnostic if not. If it fails that way, the overhead has to be
    raised before any front end is wired up: under-counting means every reservation is too small, and
    the budget stops being a bound.
    """
    from app.brain import adapter
    from app.executor.logic import resolve
    from app.planner.logic import build_plan
    from app.planner.models import Plan, TYPED_CONSOLE
    from config.settings import get_setting

    # The request policy this smoke is meant to exercise, read from config so it cannot drift.
    assert get_setting("brain.model") == "claude-sonnet-5-5"
    assert get_setting("brain.thinking") == "between_tools"
    assert get_setting("brain.effort") == "medium"

    max_type_characters = int(get_setting("executor.max_type_characters"))
    output_schema = interpretation_schema(max_type_characters)
    prompt = brain.interpretation_request("please open notepad")
    estimated = adapter.estimated_input_tokens(prompt, brain.SYSTEM_PROMPT, output_schema)

    reply_text = adapter.send_message(
        prompt,
        max_tokens=int(get_setting("cost.max_output_tokens_per_request")),
        system=brain.SYSTEM_PROMPT,
        output_schema=output_schema,
    )

    # Metadata only - never the prompt, never the schema, never the reply text.
    settled = (reply_text.input_tokens * 2.00 + reply_text.output_tokens * 10.00) / 1e6
    print(f"\nreal call: model={reply_text.model} stop_reason={reply_text.stop_reason} "
          f"estimated_input_tokens={estimated} provider_input_tokens={reply_text.input_tokens} "
          f"output_tokens={reply_text.output_tokens} settled_cost_usd={settled:.6f}")

    assert reply_text.stop_reason not in brain.UNREADABLE_STOP_REASONS, (
        f"unusable stop_reason={reply_text.stop_reason}")
    assert estimated >= reply_text.input_tokens, (
        f"OUR ESTIMATE IS TOO SMALL: estimated {estimated} < provider {reply_text.input_tokens}. "
        f"Raise brain.structured_output_overhead_tokens before integrating any front end.")

    interpretation = brain.validate_interpretation(reply_text.text,
                                                   max_type_characters=max_type_characters,
                                                   stop_reason=reply_text.stop_reason)
    assert isinstance(interpretation, Understood), interpretation.reason
    assert 1 <= len(interpretation.intents) <= MAX_INTENTS
    assert interpretation.intents[0].kind == OPEN_APP

    plan = build_plan(interpretation, TYPED_CONSOLE, resolve)
    assert isinstance(plan, Plan), getattr(plan, "reason", plan)
    assert plan.steps[0].action.kind == OPEN_APP
    print(f"real call: variant=Understood intents={len(interpretation.intents)} "
          f"kinds={[step.action.kind for step in plan.steps]}")


def test_the_real_call_is_gated_and_was_not_run():
    """Proof for the report: the only real-API test in this file is marked, and its env gate is unset."""
    import os
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    marked = [node.name for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef)
              for decorator in node.decorator_list
              if ast.unparse(decorator) == "pytest.mark.real_api"]
    assert marked == ["test_a_real_sonnet_call_returns_a_planable_interpretation"], marked
    assert os.environ.get("RUN_REAL_CLAUDE_TEST") in (None, "", "0")
