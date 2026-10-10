"""
The only surface reasoning-side code may use to ask Memory a question.

Frozen Step 4 Section 7 asks for "Memory retrieval (lookups the Brain/Planner can query)". This is that
boundary, and it is deliberately four functions wide.

WHY IT EXISTS RATHER THAN LETTING CALLERS USE logic.py DIRECTLY. Two things are guaranteed here that a
direct call could forget:

  * every contact lookup carries a disclosure context, so a caller cannot omit one and be handed an
    address the sensitive-data rules meant to withhold. BRAIN is that context for reasoning-side code,
    and USER is the context for a lookup whose answer goes straight back to the person who asked.
  * what comes back is a local Memory result and nothing else. There is no function here that could put
    a remembered value into a provider request, and none that could produce an ExecutorAction.

NOTHING HERE SENDS ANYTHING TO CLAUDE. A resolved person, relationship, address or path stays on this
machine. PreviousActionContext (kind + safe target) remains the only Phase 3 payload that leaves it, and
widening that needs its own privacy review - it is not something this file can do.

NO SENTENCE IS PARSED HERE. The frozen happy path is "message my friend Ali"; what arrives is
name="Ali", relationship="friend". Translating one into the other is the Brain's job, in a later slice.

Messaging itself is Phase 10: resolving Ali is where Phase 4 stops.
"""
from app.memory import logic
from app.memory.models import Ambiguous, Found, MemoryUnavailable, NotFound, Redacted

# The disclosure context reasoning-side lookups are made under. A sensitive-data rule written against
# this context is what withholds a field from anything the Brain or Planner can see.
BRAIN = "brain"
# The context for a lookup the USER asked for and whose answer is shown straight back to them.
#
# WHY IT IS A SECOND CONTEXT RATHER THAN THE SAME ONE. These two readers are not the same reader, and a
# rule written for one would otherwise silently govern the other. "Never let my phone numbers reach the
# reasoning service, but do show them to me" is one deny rule on BRAIN and nothing on USER - and it is
# unsayable while both read under the same name. The split also makes the asymmetry visible at the call
# site: a caller has to pick which reader it is, and the two cannot be confused by accident.
#
# IT CHANGES NO DEFAULT. The sensitive_data_rules table is consulted for an exact (table, field,
# context) tuple and there is no rule for either context out of the box, so both are disclosed today
# exactly as before. This adds the place a rule can be written, not a rule.
USER = "user"

MESSAGING_UNSUPPORTED = "I can't send messages yet."


def person(name: str, relationship: str | None = None) -> Found | Ambiguous | NotFound | MemoryUnavailable:
    """The person `name` refers to, narrowed by `relationship` when one is known.

    Found when exactly one person matches. Ambiguous when more than one does, with the candidates a
    clarification would be built from - never a guess, and never a choice handed to the model. NotFound
    with "I don't know who that is." when nobody matches."""
    return logic.find_person(name, relationship)


def contact(person_id: int, channel: str | None = None):
    """A person's contact, under the BRAIN disclosure context.

    Redacted when a sensitive-data rule withholds the address for this context: the field is absent from
    the result rather than removed afterwards, and callers must not try to recover it."""
    return logic.find_contact(person_id, channel, context=BRAIN)


def contact_for_user(person_id: int, channel: str | None = None):
    """A person's contact, under the USER disclosure context: the answer to a question the user typed.

    Same function underneath, same Redacted behaviour, different reader - and the difference is the
    point. What comes back is shown to the user and nothing else: it is still a local Memory result,
    there is still no ExecutorAction anywhere in this module, and the value in it still has no path from
    here into a provider request."""
    return logic.find_contact(person_id, channel, context=USER)


def application(alias: str, configured_app_keys) -> Found | NotFound | MemoryUnavailable:
    """The configured app key the user's own name for an app refers to.

    `configured_app_keys` comes from the caller because configuration is the source of truth for what
    may be opened; Memory cannot discover it and cannot extend it. The result is a KEY configuration
    already has - never an executable, a path or a command."""
    return logic.resolve_application_alias(alias, configured_app_keys)
