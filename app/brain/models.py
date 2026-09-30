"""
Data shapes defined by the Brain.

Phase 3 Slice 1A: the contracts only. Nothing here calls Claude, builds a prompt, or knows that a
JSON schema exists - the provider boundary is app/brain/adapter.py and arrives in a later slice.

Two rules shape everything below:

  * THE ACTION VOCABULARY DOES NOT WIDEN. An Intent can only name an Executor capability that Phase 1
    already implements, and it carries typed arguments for exactly that capability - never a free-form
    dict, never a command string. The Brain adds understanding, not capability.
  * THE RESULT IS A CLOSED SET. An interpretation is exactly one of Understood, NeedsClarification,
    NotSupported or NotACommand, so a caller cannot forget a case and fall through into acting.
"""
from dataclasses import dataclass

from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL)
from app.safety.models import RiskLevel

# The scroll directions and window operations the Executor implements, named here so an Intent cannot
# carry a value the Executor would refuse. Both mirror app/executor/logic.py and are checked against it
# by a test, rather than being a second opinion.
SCROLL_DIRECTIONS = ("up", "down")
WINDOW_OPERATIONS = ("minimize", "maximize", "restore", "close")


# --- The provider reply (Phase 0) ---------------------------------------------------------------------

@dataclass(frozen=True)
class ClaudeReply:
    """One Claude response, reduced to what the project needs."""
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


# --- What the Brain may ask for: one typed shape per implemented capability ----------------------------

@dataclass(frozen=True)
class OpenAppArgs:
    app: str


@dataclass(frozen=True)
class CloseAppArgs:
    app: str


@dataclass(frozen=True)
class ClickArgs:
    x: int
    y: int


@dataclass(frozen=True)
class TypeTextArgs:
    """`text` is kept verbatim: it is what the user wants typed, so it is never trimmed, re-spaced or
    normalised. It is also the one field that must never reach a log or a context summary."""
    text: str

    def __repr__(self) -> str:
        return f"TypeTextArgs(text=<{len(self.text) if isinstance(self.text, str) else 0} characters>)"


@dataclass(frozen=True)
class ShortcutArgs:
    keys: str


@dataclass(frozen=True)
class ScrollArgs:
    direction: str   # one of SCROLL_DIRECTIONS
    notches: int


@dataclass(frozen=True)
class RefreshArgs:
    """Refresh acts on the active window and takes nothing."""


@dataclass(frozen=True)
class WindowControlArgs:
    operation: str   # one of WINDOW_OPERATIONS


ActionArgs = (OpenAppArgs | CloseAppArgs | ClickArgs | TypeTextArgs | ShortcutArgs | ScrollArgs
              | RefreshArgs | WindowControlArgs)

# Which args shape belongs to which Executor kind. The Planner uses this to reject an Intent whose
# args do not match its kind, so a mismatch cannot reach the Executor.
ARGS_FOR_KIND = {
    OPEN_APP: OpenAppArgs,
    CLOSE_APP: CloseAppArgs,
    CLICK: ClickArgs,
    TYPE_TEXT: TypeTextArgs,
    SHORTCUT: ShortcutArgs,
    SCROLL: ScrollArgs,
    REFRESH: RefreshArgs,
    WINDOW_CONTROL: WindowControlArgs,
}


@dataclass(frozen=True, repr=False)
class Intent:
    """One thing the user wants, understood and typed.

    `why` is one short line shown beside the step, because the frozen requirement is an INSPECTABLE
    plan. `risk_floor` is the Brain's advisory opinion: it becomes Action.minimum_level, which the
    safety gate can raise from but never lower, so the Brain can never grant permission.

    `why` is written by the model, and a model can echo the user's own words into it, so it is display
    text and not log metadata: the repr gives its length only. The value is untouched and is what the
    user reads beside the step."""
    kind: str
    args: ActionArgs
    why: str = ""
    risk_floor: RiskLevel = RiskLevel.LOW

    def __repr__(self) -> str:
        length = len(self.why) if isinstance(self.why, str) else 0
        return (f"Intent(kind={self.kind!r}, args={self.args!r}, why=<{length} characters>, "
                f"risk_floor={self.risk_floor!r})")


# --- What the Brain answers: a closed set of four -----------------------------------------------------

@dataclass(frozen=True, repr=False)
class Understood:
    """The request maps onto implemented capabilities.

    `restated` is the request in the user's own words, shown so they can check the understanding
    before accepting anything. It is display text, never something that is acted on - and because it
    is the user's own words, the repr gives its length only (the project's convention for a private
    string, as in app/safety/models.py and app/listener/models.py)."""
    intents: tuple[Intent, ...]
    restated: str = ""

    def __repr__(self) -> str:
        length = len(self.restated) if isinstance(self.restated, str) else 0
        return f"Understood(intents={self.intents!r}, restated=<{length} characters>)"


@dataclass(frozen=True)
class NeedsClarification:
    """A capability exists, but a required target or choice is unresolved - so ASK, never guess.

    This is the interpretation only. What the session then does with it (which text it came from, what
    it is waiting for) is session lifecycle and lives in app/planner/models.py, not here."""
    question: str
    missing: str = ""    # which field is absent: "app", "recipient", "which_of", ...
    because: str = ""    # why it could not be resolved; safe to show, never the raw request


@dataclass(frozen=True)
class NotSupported:
    """A real request, understood, that no implemented capability can carry out - checking the weather,
    searching the web, actually sending a message. `what` names the missing capability."""
    what: str
    message: str = ""


@dataclass(frozen=True)
class NotACommand:
    """No identifiable request at all: a greeting, chatter, or garbled recognition."""
    message: str = ""


Interpretation = Understood | NeedsClarification | NotSupported | NotACommand

# The closed set, for callers that must handle every case and for the test that proves it is closed.
INTERPRETATIONS = (Understood, NeedsClarification, NotSupported, NotACommand)


# --- Short-term context, and what may never be in it --------------------------------------------------

@dataclass(frozen=True)
class PreviousActionContext:
    """The immediately previous accepted action, reduced to what is safe to send to a cloud model.

    Deliberately NOT an ExecutorAction: a `type_text` action's target is the user's own words, and
    sending those to a model to resolve "open it" would be an egress nobody asked for. Built only by
    previous_action_context(), which decides per kind what may be kept."""
    kind: str
    safe_target: str | None = None


def previous_action_context(kind: str, target: str) -> PreviousActionContext:
    """Summarise an accepted action for short-term context, keeping only what is safe AND useful.

    Kept: the app name for open_app/close_app, and the operation for window_control - short, from a
    fixed set, and exactly what makes "close it" or "do that again" resolvable.

    Dropped: type_text (the user's own words), click coordinates, shortcut keys and scroll amounts -
    none of them helps interpret a follow-up, and the first is private."""
    if kind in (OPEN_APP, CLOSE_APP, WINDOW_CONTROL) and isinstance(target, str) and target.strip():
        return PreviousActionContext(kind=kind, safe_target=target.strip())
    return PreviousActionContext(kind=kind, safe_target=None)


# --- The provider-facing shape of an Interpretation (Phase 3 Slice 2) ---------------------------------
# The typed contracts above are the truth. What follows is a TRANSLATION of them into the one shape a
# JSON-schema-constrained model can be held to, plus the bounds that keep a reply small.
#
# WHY THE SCHEMA IS FLAT. Anthropic's structured outputs documentation states hard limits on schema
# complexity: 24 optional parameters in total, and 16 union-typed (anyOf) parameters, per request. A
# faithful rendering of "an Interpretation is one of four variants, and each of eight action kinds has
# its own args shape" is a dozen unions and dozens of optional fields, which would sit against those
# limits with no room to grow. So the wire shape is ONE flat object per interpretation and ONE flat
# object per intent, every field required, with a `kind` discriminator: zero unions, zero optional
# parameters.
#
# THE CONTRACT DID NOT LOOSEN. The model still cannot name an action kind the Executor does not
# implement - the enum is built from ARGS_FOR_KIND - and it still cannot write an ExecutorAction target,
# because there is no target field on the wire at all. app/brain/logic.validate_interpretation() reads
# the flat object and constructs the real typed args shape for the named kind, ignoring the placeholder
# fields belonging to other kinds. The translation lives here, at the boundary; the contracts stay as
# strict as they were.

# Bounds on every free-text field the model writes. Long enough for a short desktop command and a
# one-line explanation, short enough that `why` and `restated` are useless as a covert output channel.
MAX_RESTATED = 200
MAX_WHY = 120
MAX_QUESTION = 200
MAX_MISSING = 40
MAX_BECAUSE = 200
MAX_WHAT = 80
MAX_MESSAGE = 200
MAX_APP = 40
MAX_KEYS = 40
MAX_INTENTS = 5           # mirrors app/planner/models.MAX_PLAN_STEPS; a test holds them together

UNDERSTOOD = "understood"
NEEDS_CLARIFICATION = "needs_clarification"
NOT_SUPPORTED = "not_supported"
NOT_A_COMMAND = "not_a_command"
INTERPRETATION_KINDS = (UNDERSTOOD, NEEDS_CLARIFICATION, NOT_SUPPORTED, NOT_A_COMMAND)

# The model names a risk floor in words; only these four map to a RiskLevel, and nothing else does.
RISK_FLOOR_NAMES = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM,
                    "high": RiskLevel.HIGH, "critical": RiskLevel.CRITICAL}

# Which flat wire fields build which typed args shape. One source for both the schema and the
# validator, so a field can never be in one and missing from the other.
ARGS_FIELDS = {
    OPEN_APP: ("app",),
    CLOSE_APP: ("app",),
    CLICK: ("x", "y"),
    TYPE_TEXT: ("text",),
    SHORTCUT: ("keys",),
    SCROLL: ("direction", "notches"),
    REFRESH: (),
    WINDOW_CONTROL: ("operation",),
}

# --- One canonical wire form per kind -----------------------------------------------------------------
# The flat shape means every intent carries every field, so `refresh` still has an `app` and a `text`.
# Ignoring what is in them would leave the reply with two problems: an unnecessary channel for content
# nobody asked for, and no honest upper bound on the size of a reply we would accept - the whole point of
# deriving the output cap.
#
# So a valid reply has exactly ONE representation for its kind: the fields that kind uses carry values,
# and every other field carries the neutral value below. A non-neutral irrelevant field makes the reply
# invalid; it is never trimmed, stripped or executed.
#
# The neutral values are not invented. They are what the existing wire types already say: the empty
# string for a string, 0 for an integer, and for the two enums the empty member that is already in them
# ("" is in direction and operation because those fields are irrelevant to six of the eight kinds). No
# enum was widened for this, and no enum member names a capability: `kind` and `risk_floor` are relevant
# to every intent and therefore need no neutral value at all.

NEUTRAL_ARGS_VALUES = {
    "app": "",
    "x": 0,
    "y": 0,
    "text": "",
    "keys": "",
    "direction": "",
    "notches": 0,
    "operation": "",
}

# Which top-level fields each interpretation uses. `kind` is always used; everything else not listed
# here for the named kind must be neutral.
INTERPRETATION_FIELDS = {
    UNDERSTOOD: ("restated", "intents"),
    NEEDS_CLARIFICATION: ("question", "missing", "because"),
    NOT_SUPPORTED: ("what", "message"),
    NOT_A_COMMAND: ("message",),
}

NEUTRAL_INTERPRETATION_VALUES = {
    "restated": "",
    "intents": [],
    "question": "",
    "missing": "",
    "because": "",
    "what": "",
    "message": "",
}


def is_neutral(value, neutral) -> bool:
    """Whether a wire value is exactly the canonical neutral one.

    Type-strict on purpose: in Python False == 0, so a plain equality test would accept
    `notches: false` as neutral and let a boolean through where a count belongs."""
    if isinstance(neutral, list):
        return isinstance(value, list) and not value
    if isinstance(neutral, bool) or isinstance(value, bool):
        return value is neutral
    if isinstance(neutral, int):
        return isinstance(value, int) and value == neutral
    return isinstance(value, str) and value == neutral


def interpretation_schema(max_type_characters: int) -> dict:
    """The JSON schema sent as output_config.format.schema.

    Built from the contracts above rather than written out by hand: the action kinds come from
    ARGS_FOR_KIND, the scroll and window vocabularies from their own tuples, and the typed-text bound
    from the Executor's own configured limit - never a second product limit invented here.

    Every property is required and additionalProperties is false, so a reply has exactly one shape.
    Fields that do not belong to the named kind are sent empty, and the validator ignores them.

    WHAT THIS SCHEMA DELIBERATELY CANNOT SAY. Anthropic's structured outputs support only a subset of
    JSON Schema (SUPPORTED_SCHEMA_KEYWORDS below, verified 2026-09-30). String `maxLength`, array
    `maxItems`, and every numeric bound are UNSUPPORTED and return a 400 - which is exactly how the
    first real request failed. So no length and no cardinality is expressed here. Each bound instead
    appears in the field's `description`, which is the same transformation Anthropic's own SDK helpers
    perform: the model is told the limit, the schema does not claim to enforce it.

    NOTHING WAS RELAXED. Every one of those bounds is checked in
    app/brain/logic.validate_interpretation() after the reply arrives, where it always was - a reply
    outside them is refused, never trimmed. The schema constrains SHAPE; we constrain SIZE.
    """
    def bounded(description: str, limit: int) -> dict:
        """A string field whose bound the provider cannot enforce, so it is stated in words."""
        return {"type": "string", "description": f"{description} (at most {limit} characters)"}

    intent = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": list(ARGS_FOR_KIND),
                     "description": "the capability to use; only these exist"},
            "why": bounded("one short line, shown to the user beside this step", MAX_WHY),
            "risk_floor": {"type": "string", "enum": list(RISK_FLOOR_NAMES),
                           "description": "advisory; the safety gate may raise it, never lower it"},
            "app": bounded("app name for open_app and close_app, otherwise empty", MAX_APP),
            "x": {"type": "integer", "description": "click x, a whole number; otherwise 0"},
            "y": {"type": "integer", "description": "click y, a whole number; otherwise 0"},
            "text": bounded("exactly what to type for type_text, otherwise empty",
                            max_type_characters),
            "keys": bounded("a shortcut such as ctrl+c for shortcut, otherwise empty", MAX_KEYS),
            "direction": {"type": "string", "enum": ["", *SCROLL_DIRECTIONS],
                          "description": "scroll direction, otherwise empty"},
            # No ceiling stated: the Executor owns that number and enforces it at the resolve seam, so
            # repeating it here would be a second opinion that could drift out of date.
            "notches": {"type": "integer",
                        "description": "scroll notches for scroll, a whole number of at least 1; "
                                       "otherwise 0"},
            "operation": {"type": "string", "enum": ["", *WINDOW_OPERATIONS],
                          "description": "window_control operation, otherwise empty"},
        },
    }
    intent["required"] = list(intent["properties"])
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": list(INTERPRETATION_KINDS),
                     "description": "which kind of answer this is"},
            "restated": bounded("the request in the user's own words, for understood", MAX_RESTATED),
            # minItems 0 and 1 are the only supported array constraints, and it must be 0: the other
            # three interpretations send an empty list. The 1..5 bound is the validator's, not this
            # schema's, so it is stated here only in words.
            "intents": {"type": "array", "minItems": 0, "items": intent,
                        "description": f"for understood, 1 to {MAX_INTENTS} intents in the order they "
                                       f"should happen; otherwise an empty list"},
            "question": bounded("what to ask, for needs_clarification", MAX_QUESTION),
            "missing": bounded("which detail is missing, such as app", MAX_MISSING),
            "because": bounded("why it could not be resolved, for needs_clarification", MAX_BECAUSE),
            "what": bounded("the capability that does not exist, for not_supported", MAX_WHAT),
            "message": bounded("what to say, for not_supported and not_a_command", MAX_MESSAGE),
        },
    }
    schema["required"] = list(schema["properties"])
    return schema


# --- What Anthropic's structured outputs actually accept -----------------------------------------------
# VERIFIED 2026-09-30 at https://platform.claude.com/docs/en/build-with-claude/structured-outputs.
# "If you use an unsupported feature, you'll receive a 400 error with details" - and a 400 is a real
# request that has to be paid for in attention, so this list exists to keep the next one offline.
#
# WHAT THE ALLOWLIST CAN AND CANNOT PROMISE. It is an allowlist, not a denylist, so a keyword nobody has
# thought about fails the check rather than passing it. What it cannot see are limits the documentation
# does not enumerate: the compiled-grammar size limit behind "Schema is too complex for compilation",
# the regex complexity limit behind "Complex patterns may result in 400 errors", and the documented
# counts of 20 strict tools / 24 optional parameters / 16 union-typed parameters, which are about VALUES
# rather than keywords and are checked separately by schema_complexity(). A schema can satisfy every
# check here and still be refused for one of those reasons.

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    # structure
    "type", "properties", "required", "additionalProperties", "items", "description", "default",
    # the supported value constraints
    "enum", "const", "pattern", "format",
    # composition, with the documented limits (no external $ref, no allOf with $ref, no recursion)
    "anyOf", "allOf", "$ref", "$def", "$defs", "definitions",
    # the one supported array constraint, and only with the value 0 or 1
    "minItems",
})

UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "maxItems", "uniqueItems", "minContains", "maxContains", "contains",
    "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minProperties", "maxProperties", "propertyNames", "patternProperties", "dependentRequired",
    "oneOf", "not", "if", "then", "else",
})

SUPPORTED_MIN_ITEMS = (0, 1)


def schema_keywords(schema: dict) -> dict:
    """Every JSON Schema keyword in the tree, mapped to the paths where it appears.

    Used by the compatibility test. It walks `properties` and `items` as schema positions and treats
    everything else at a node as a keyword, which is why a stray keyword cannot hide in a nested object.
    """
    found: dict[str, list[str]] = {}
    stack = [(schema, "$")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, list):
            stack.extend((item, path) for item in node)
            continue
        if not isinstance(node, dict):
            continue
        for keyword, value in node.items():
            found.setdefault(keyword, []).append(path)
            if keyword == "properties" and isinstance(value, dict):
                stack.extend((sub, f"{path}.{name}") for name, sub in value.items())
            elif keyword in ("items", "anyOf", "allOf", "oneOf"):
                stack.append((value, f"{path}[]"))
    return found


def schema_incompatibilities(schema: dict) -> list[str]:
    """Every reason this schema would be refused, by the documented rules. Empty means compatible.

    Offline, so the next 400 does not have to be discovered by sending a real request.
    """
    problems = []
    found = schema_keywords(schema)
    for keyword, paths in sorted(found.items()):
        if keyword in UNSUPPORTED_SCHEMA_KEYWORDS:
            problems.append(f"unsupported keyword {keyword!r} at {', '.join(sorted(set(paths)))}")
        elif keyword not in SUPPORTED_SCHEMA_KEYWORDS:
            problems.append(f"keyword {keyword!r} is not on the verified-supported list "
                            f"at {', '.join(sorted(set(paths)))}")
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if "minItems" in node and node["minItems"] not in SUPPORTED_MIN_ITEMS:
            problems.append(f"minItems={node['minItems']!r}; only {SUPPORTED_MIN_ITEMS} are supported")
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            problems.append("an object must set additionalProperties to false")
        if "enum" in node and not all(isinstance(value, (str, int, float, bool, type(None)))
                                      for value in node["enum"]):
            problems.append("enum values must be strings, numbers, bools or nulls")
        stack.extend(node.get("properties", {}).values())
        if isinstance(node.get("items"), dict):
            stack.append(node["items"])
    return problems


def schema_complexity(schema: dict) -> dict:
    """Count the things Anthropic's documented structured-output limits are about, so a test can prove
    this schema stays inside them: optional parameters (limit 24) and union-typed parameters (limit 16).
    """
    optional = unions = properties = 0
    stack = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        fields = node.get("properties")
        if isinstance(fields, dict):
            properties += len(fields)
            optional += len(set(fields) - set(node.get("required", ())))
            stack.extend(fields.values())
        if "anyOf" in node or "oneOf" in node or isinstance(node.get("type"), list):
            unions += 1
        if isinstance(node.get("items"), dict):
            stack.append(node["items"])
    return {"optional": optional, "unions": unions, "properties": properties}
