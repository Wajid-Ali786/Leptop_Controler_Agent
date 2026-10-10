"""
Recognise the two things the user may ask about a PERSON, locally, and never send them anywhere.

WHY THIS FILE EXISTS AT ALL. app/brain/logic.py is the provider boundary: the line the user typed
reaches Claude verbatim in interpretation_request(), and it reaches it again inside replan_request()
and clarification_request(). So "remember Ali's whatsapp is +92-300-0000001" cannot be answered by
teaching the model a new intent kind - by the time the model could answer, the number has already left
the machine. It has to be recognised BEFORE that, which is what this module is for, and the caller
(app/console.py) runs it ahead of every provider call.

WHAT IT CANNOT DO, BY CONSTRUCTION RATHER THAN BY CARE:

  * it imports nothing from app.executor, so it has no ExecutorAction to build and no action kind to
    name. A remembered value cannot become an action target here because there is no action here.
  * it imports no provider adapter and makes no call of any kind. recognise() is a pure function of one
    string.
  * none of the three results it returns carries a `kind` or a `target` field, so neither can be picked
    up by a caller that was written for actions.

WRITES FAIL CLOSED, READS FALL THROUGH. That asymmetry is the whole safety design:

  * a line that CARRIES a marker meaning "keep this" (remember, note that, save that, yaad rakho, yad
    rakho) is answered here or refused here - KeepLocal - and in neither case does it continue to the
    Brain. A form this grammar cannot parse is therefore refused rather than sent, which costs the user
    a retype and cannot leak. "remember to call Ali tomorrow" is a reminder, not a fact, and is refused
    for exactly that reason.
  * a line that only ASKS (recall, what is ...) and does not parse returns None and goes to the Brain
    unchanged, because the words the user typed are all it contains. Nothing stored is in it yet.

ROMAN URDU IS SOV, AND THAT IS NOT A DETAIL. "Ali ka whatsapp +92-300-0000001 yaad rakho" puts the
marker at the END. A guard that only looked at the start of the line would have let that sentence - the
one shape the owner actually types - go straight to the provider with the number in it. The Urdu
markers are therefore tested at both ends of the line.

NO NEW TABLE AND NO NEW COLUMN. This recognises people and contacts, which are two of Phase 4's fifteen
frozen structures, and it reaches them through app/memory. Work folders, websites and preferences are
deliberately not here: they are a separate decision the owner has not made.
"""
from dataclasses import dataclass

from app.memory.models import normalize

# The markers that mean "keep this". A line carrying one of these is NEVER sent to the provider: it is
# stored, or it is refused with a message. Kept small on purpose - the guard is fail-closed, so a narrow
# set refuses more and leaks nothing, while a wider one can only keep more things local.
KEEP_MARKERS = ("remember", "note that", "save that", "yaad rakho", "yad rakho")
# The ones Urdu word order puts at the END of the sentence, where a leading-marker test cannot see them.
TRAILING_KEEP_MARKERS = ("yaad rakho", "yad rakho")
# Asking, not keeping. These do NOT fail closed: an unparsed question carries nothing stored.
ASK_MARKERS = ("recall", "what is", "whats", "what's")

# The channels a contact may be on. A closed vocabulary, because it is what makes the grammar
# deterministic: the channel word is the pivot the name and the address are split on. It is ALSO the
# reason the caller may authorise the write without inspecting the value - a line with no channel word
# in it is not a contact and never reaches the database, so "remember my password is ..." is refused
# here rather than stored as a fact.
CHANNELS = {"whatsapp": "whatsapp", "phone": "phone", "number": "phone", "mobile": "phone",
            "cell": "phone", "email": "email", "mail": "email", "telegram": "telegram"}

# Words that sit between the name and the channel, or between the channel and the address, in the shapes
# the owner actually types: English possessive, Urdu ka/ki/ke, and the copulas.
_POSSESSIVE = ("'s", "’s", "s")
_CONNECTORS = ("ka", "ki", "ke", "kaa", "of")
_LEAD_FILLER = ("that", "this", "my", "the", "mera", "meri")
_COPULA = ("is", "are", "hai", "hain", "=", ":", "to")

_NAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz.-'")
_ADDRESS_CHARACTERS = set("0123456789+()- ")
_NAME_WORD_LIMIT = 3

CANNOT_STORE = ("I keep anything you ask me to remember on this machine, so I did NOT send that to the "
                "reasoning service - and I couldn't work out what to store from it, so I've stored "
                "nothing either. I can remember how to reach someone, like: remember Ali's whatsapp is "
                "+92-300-0000001.")


@dataclass(frozen=True)
class RememberContact:
    """A parsed "keep this": one person, one channel, one address. Deliberately not an action."""
    name: str
    channel: str
    address: str


@dataclass(frozen=True)
class RecallContact:
    """A parsed question about how to reach someone. Carries no value - the answer is looked up."""
    name: str
    channel: str | None = None


@dataclass(frozen=True)
class KeepLocal:
    """A line carrying a keep marker that this grammar could not parse.

    It is the FAIL-CLOSED result: the caller refuses the line with `message` and the line stops here.
    `message` names nothing the user typed, so refusing cannot echo the value either."""
    message: str


def is_covered(line) -> bool:
    """Does this line carry a marker meaning "keep this"?

    The question the nested prompts ask. A covered line may never be consumed as a correction, a
    clarification answer or a retry answer, because consuming it there is precisely what puts it into
    replan_request() or clarification_request(). Cheap and pure: no parse, no lookup, no call."""
    return _keep_marker(normalize(line)) is not None


def recognise(line) -> RememberContact | RecallContact | KeepLocal | None:
    """What this line is, locally. None means "not mine - carry on to the Brain".

    A keep marker never returns None: it returns RememberContact when the grammar parses and KeepLocal
    when it does not. An ask marker returns RecallContact when it parses and None when it does not,
    which leaves today's behaviour for a question alone."""
    text = normalize(line)
    if not text:
        return None
    keep = _keep_marker(text)
    if keep is not None:
        parsed = _parse(_remainder(line, keep))
        if parsed is None or not parsed[2]:
            return KeepLocal(CANNOT_STORE)
        name, channel, address = parsed
        return RememberContact(name, channel, address)
    ask = _leading(text, ASK_MARKERS)
    if ask is None:
        return None
    parsed = _parse(_remainder(line, ask))
    if parsed is None or parsed[2]:
        # An address on the right of the channel means it was not a question after all. This falls
        # through rather than refusing: asking fails open, keeping fails closed.
        return None
    return RecallContact(parsed[0], parsed[1])


def _keep_marker(text: str) -> str | None:
    """The keep marker in this line, at either end, or None. Both ends, because Urdu is SOV."""
    leading = _leading(text, KEEP_MARKERS)
    if leading is not None:
        return leading
    for marker in TRAILING_KEEP_MARKERS:
        if text == marker or text.endswith(" " + marker):
            return marker
    return None


def _leading(text: str, markers) -> str | None:
    """The marker this line STARTS with, on a word boundary. "remembered the name" is not "remember"."""
    for marker in markers:
        if text == marker or text.startswith(marker + " "):
            return marker
    return None


def _remainder(line: str, marker: str) -> list[str]:
    """The words of the ORIGINAL line with one occurrence of its marker removed.

    The words come back as the user typed them, not folded. normalize() decides WHERE the marker is,
    because that is a comparison; what is handed on keeps its capitals, because Person.name is
    documented as never being rewritten and "Ali" is the user's spelling of their friend's name. The
    leading end is tried first, matching _leading()'s own precedence."""
    words = line.split()
    count = len(marker.split())
    if normalize(" ".join(words[:count])) == marker:
        return words[count:]
    return words[:-count]


def _parse(words):
    """(name, channel, address) from the middle of the sentence, or None when it is not that shape.

    ONE channel word is the pivot: it has to appear exactly once, because two would mean there is no
    single answer to which side the name is on, and nothing here guesses. `address` comes back as "" for
    a question, which is how the caller tells asking from keeping."""
    hits = [index for index, word in enumerate(words) if _channel(word) is not None]
    if len(hits) != 1:
        return None
    pivot = hits[0]
    name = _name(words[:pivot])
    address = _address(words[pivot + 1:])
    if name is None or address is None:
        return None
    return name, _channel(words[pivot]), address


def _channel(word: str) -> str | None:
    return CHANNELS.get(_key(word))


def _bare(word: str) -> str:
    """One word with the punctuation that only ever decorates it taken off. CASE IS KEPT: this is the
    form that gets stored and shown back."""
    return word.strip(".,:;!?\"'’").strip()


def _key(word: str) -> str:
    """The form one word is COMPARED by, against this file's own closed vocabularies. Never stored."""
    return _bare(word).lower()


def _name(words) -> str | None:
    """The person's name from the words left of the channel, or None when they are not a name.

    Filler and possessives are dropped; everything else must LOOK like a name, so a sentence ("to call
    ali tomorrow about his") is rejected rather than stored as a person. At most three words, because a
    longer run is prose."""
    kept = [word for word in words if _key(word) not in _LEAD_FILLER]
    while kept and _key(kept[-1]) in _CONNECTORS:
        kept.pop()
    if kept:
        last = _bare(kept[-1])
        for possessive in _POSSESSIVE:
            if last.lower().endswith(possessive) and len(last) > len(possessive):
                kept[-1] = last[: -len(possessive)].rstrip("'’")
                break
    if not kept or len(kept) > _NAME_WORD_LIMIT:
        return None
    if not all(_key(word) and set(_key(word)) <= _NAME_CHARACTERS for word in kept):
        return None
    return " ".join(_bare(word) for word in kept)


def _address(words) -> str | None:
    """The address from the words right of the channel, "" when there are none, or None when they are not
    an address.

    Stored EXACTLY as the user wrote it, minus a copula: a phone number is not something to normalise,
    reformat or validate - a reformatted number is a different number. One word is accepted as written,
    and several are accepted only when every one of them is made of digits and phone punctuation, which
    is how "+92 300 0000001" works while a sentence does not."""
    kept = list(words)
    while kept and _key(kept[0]) in _COPULA:
        kept.pop(0)
    if not kept:
        return ""
    if len(kept) == 1:
        return kept[0] if _bare(kept[0]) else None
    if all(set(word) <= _ADDRESS_CHARACTERS for word in kept):
        return " ".join(kept)
    return None
