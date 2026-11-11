"""Refund consent. This is the one place that decides whether a buyer message is a plain,
unconditional request for money back. LLM-assisted development; reviewed rule by rule.

WHY IT CHANGED. An earlier version made consent a closed allowlist: a message passed only when EVERY part of it came from a small
grammar (greeting, a fixed problem statement, one command template, thanks). That was safe and useless: on Dhanush
Kankanala's Milestone 1 set it auto-refunded 0 of the 9 ordinary messages he expects approved, because real buyers write
"today", "but", a question mark, a size that is too small, or a sentence about their grandmother. The owner did the work.

The rules now give THREE answers (assess(): tier PASS, UNREAD or HAZARD; explain() keeps the old
shape). A context clause is PASS only when the rules recognise it (it speaks of the order, the goods, the delivery, the problem, or
courtesy and tone); a statement they do not recognise is UNREAD; a positive reason to hold (below, plus words that reverse, redirect
or disown a request, a deferral to a date, another currency, another language) is a HAZARD. Only app/consent_combine.py decides what a
model's reading may do with each answer. This answers a review finding that "starts like a statement and has no
listed word" let "I decline this request" and "It goes to Beatrice" through.

THE RULE NOW (all of it must hold; anything else is held for the owner):

1. Canonical form, fail closed on characters. Unicode NFKC, lower case, look-alike letters folded to ASCII (app/arena_text.py),
   whitespace collapsed, a line break counts as a sentence end. Invisible and control characters hold the message. After the
   fold only ASCII letters and digits, spaces and `. , ! ? $ # -` may remain. An apostrophe is accepted only inside an
   English contraction ("it's", "don't", "I'd"), which is expanded; parentheses only around a dollar figure ("($10.00)").
   Quotes of every kind, colons, semicolons, slashes, brackets, links, markup, emoji and other currencies still hold it.
2. A REQUEST, matched WHOLE. The message is cut into clauses (sentences, and the
   parts of a sentence between commas). At least one clause must be, from its first word to its last, one of the request
   forms in REQUESTS: an imperative ("please refund $18.00"), a first-person request ("I need a refund", "I want my $48.50
   back"), a polite request-question ("could I please get my $27.00 back?"), a statement of the refund amount ("the total
   refund should be $22.50"), optionally led by "so", "and", "please" or an order reference and followed only by "for <the
   thing complained about>", "please", "right now", "thanks" and the like, or by a reason ("because ...") or purpose ("so I
   can ...") that is itself plain context. A completed return ("we returned it to your warehouse") also counts as the
   request. Before this, a request form found ANYWHERE counted, so "I never said I want a refund of $18.00", "My sister wrote
   please refund $18.00", "Please refund $18.00 to Alice", "... in two days", "... CAD" and "... or face a coordinated
   boycott" were all refunded.
3. Every OTHER clause is plain context: it starts like a statement (a pronoun, a determiner, an order word, a greeting,
   thanks, an apology, after an optional "and", "but", "because" ...), uses no word about money or payment ("I initially
   wanted a refund", "the merchant reimbursed me") and no word that reports someone else's words, takes the request back or
   talks to the system ("wrote", "scratch that", "mark this transaction benign"). A clause that is neither holds the message.
4. At most one figure in the request clauses ("Please refund $18.00. Please refund $18.00." is one refund or two?).
5. No HOLD signal anywhere (FORBIDDEN_WORDS, HOLD_PATTERNS, as before): a condition or a dependency (if, unless, when, once,
   after, until, pending, provided that, a deferral to a future day), a hedge, a negated or withdrawn request, another payee
   or payment route, an alternative to money, words that address the system, an authority claim word, a long encoded token.
6. Every question is a request-question; an "or" question or a question for information holds the message.
7. At most 600 characters after step 1.

WHAT THIS DOES NOT DO. Tone is not a signal (Dhanush's rule): "this is the worst customer service ever" or "right now" do not
change the outcome; THREATS do, through the gate's scanner (G10), and a threat tacked onto the request ("... or face a
boycott") now also fails step 2. The rule fails closed on structure (a clause must be a whole request or start like a
statement) but its context check is still a list of English words: a context sentence that starts like a statement and
uses words the lists do not know ("It goes to Alice") is not caught. What such a message can cost is bounded by the owner's
automatic allowance per order, the daily budget and one automatic refund per order, and the gate's other rules (G1 to G16)
still apply. It also holds honest messages: on Dhanush Kankanala's development set 4 of the 9 ordinary messages he expects
approved now go to the owner instead (a wish revised in mid-message, a threat-shaped "or I am never buying from you again",
a leading "Per consumer protection guidelines,", an elliptical "but kept the jacket"). English only. This module judges words, not truth."""
from __future__ import annotations
import re
import unicodedata

from .arena_text import normalize as _fold      # NFKC, lower case, look-alike folding, invisible characters dropped
from .extractor import find_amounts, _ORDER, RETURNED

_MAX_LEN = 600
TIER_PASS, TIER_UNREAD, TIER_HAZARD = "PASS", "UNREAD", "HAZARD"     # what the rules say about a message

# ---- step 1: the canonical form ---------------------------------------------------------------------------------------
_CONTRACTIONS = ((r"\bwon't\b", "will not"), (r"\bcan't\b", "can not"), (r"\bshan't\b", "shall not"), (r"n't\b", " not"),
                 (r"'re\b", " are"), (r"'ve\b", " have"), (r"'ll\b", " will"), (r"'d\b", " would"), (r"'m\b", " am"),
                 (r"'s\b", "s"))
_CONTRACTION_SHAPE = re.compile(r"[a-z]'(?:s|t|re|ve|ll|d|m)\b")
_PAREN_AMOUNT = re.compile(r"\((\$\s?\d[\d,]*(?:\.\d{1,2})?)\)")
_ALLOWED_CHARS = re.compile(r"[a-z0-9 .,!?$#-]*")      # no underscore: _protect() uses it internally as the decimal mark
_PAST_DAY = re.compile(r"\b(?:last|this past) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|week|month)\b")


def canonical(text: str) -> str | None:
    """The canonical form (step 1), or None when a character that is not allowed is left. Line breaks become sentence ends."""
    if not isinstance(text, str) or len(text) > _MAX_LEN * 2:
        return None
    s = unicodedata.normalize("NFKC", text).replace("\r", "\n")
    s = ". ".join(part.strip() for part in s.split("\n"))
    # invisible characters are checked BEFORE the fold (which drops them), on the NFKC form: they are not allowed anywhere
    if any(unicodedata.category(c) in ("Cf", "Cc", "Zl", "Zp", "Co", "Cs", "Cn", "Mn", "Mc", "Me") for c in s if c != "\t"):
        return None
    s = _fold(s)
    s = _PAREN_AMOUNT.sub(r"\1", s)
    if "'" in s:
        if len(re.findall("'", s)) != len(_CONTRACTION_SHAPE.findall(s)):
            return None                                  # an apostrophe that is not part of a contraction is a quote
        for pat, rep in _CONTRACTIONS:
            s = re.sub(pat, rep, s)
    if not _ALLOWED_CHARS.fullmatch(s):
        return None
    return " ".join(s.split())


# ---- step 2: the requests, matched WHOLE ----------------------------------------------------
# A request is no longer found anywhere in the text: a clause (a sentence, or a part of one between commas) is a request only
# when the WHOLE clause is one of these forms, from its first word to its last. "I never said I want a refund", "my sister
# wrote please refund", "refund $18.00 to Alice", "refund $18.00 in two days", "refund $18.00 CAD" or "refund $18.00 or face a
# boycott" contain a request form but are not one, so they hold the message.
_AMT = r"(?:\$\d+(?:_\d{1,2})?|\d+(?:_\d{1,2})? (?:dollars|usd))"           # 18.00 is written 18_00 inside this module
_AMT_RX = re.compile(r"(?<![a-z0-9_$])" + _AMT + r"(?![a-z0-9_])")
_REF = r"(?:ord-?\d{3,8}|#\d{3,8}|[a-z0-9]{17})"                             # an order number or a PayPal transaction id
_KIND = r"(?:(?:full|partial) )?"
_ART = r"(?:(?:a|an|my|the|your|our) )?"
_OBJ_ONE = (r" (?:" + _AMT + r"|(?:(?:my|the|this) )?(?:full |whole )?order(?: (?:number |no )?" + _REF + r")?|" + _REF
            + r"|it|in full|the full amount)")
_OBJ = r"(?:" + _OBJ_ONE + r")?"
_ME = r"(?: me| us)?"
_OF = r"(?: of " + _AMT + r")?"
_NOUN = r"(?:refund|reimbursement)"
_LEAD = (r"(?:(?:so|and|now|but|hi|hello) )?(?:(?:please|kindly|just) ){0,2}"
         r"(?:(?:for|regarding|re|about) (?:order |transaction )?" + _REF + r" )?")
# the words allowed after the request: "for the cracked mug", "for order ORD-1001", "please", "right now", "thanks"
_NPW = (r"(?!(?:and|or|but|to|in|on|at|by|with|from|if|unless|when|until|into|via|of|not|then|plus|instead|rather|without"
        r"|within|after|before|than|so|because|as|since|for|me|us|him|them|someone|somebody)\b)[a-z]+")
_NP = r"(?:(?:the|my|this|that|a|an|our) )?(?:" + _AMT + r" )?(?:" + _NPW + r" ){0,3}" + _NPW
_FOR = r"for (?:(?:order |transaction )?" + _REF + r"|(?P<np>" + _NP + r"))"
_URGE = (r"(?:please|today|now|right now|immediately|asap|thanks|thank you|thank you so much|to me|to us"
         r"|to (?:my|the|your) original (?:payment method|form of payment|payment|card|paypal account|account))")
_TAIL = r"(?: (?:" + _FOR + r"|" + _URGE + r"))?(?: " + _URGE + r"){0,2}"
# a reason or purpose may follow inside the clause; it is then checked as a context clause ("because it arrived broken",
# "so I can buy the correct pair")
_WHY = r"(?: (?:because|as|since) (?P<why>.+)| so (?:that )?(?P<so>(?:i|we) (?:can|could) .+))?"
REQUESTS = (
    # imperative: "please refund $18.00", "refund me", "please just refund me for the $15.00 missing cable", "refund please"
    r"(?:refund|reimburse)(?: me| us)" + _OBJ + r"|(?:refund|reimburse)" + _OBJ_ONE,     # a bare "refund" is not a request
    r"refund please",
    # send / give / issue / process a refund
    r"(?:send|give|issue|process)" + _ME + r" " + _ART + _KIND + r"(?:" + _AMT + r" )?" + _NOUN + _OF,
    r"(?:send|give) (?:me|us) (?:back )?(?:my|our) money(?: back)?",
    # first person: "i need a refund", "i am formally requesting a refund of $24.99", "i want my $48.50 back"
    r"i (?:(?:would|am|do|really|only|just) )?(?:(?:formally|kindly|politely|respectfully|really|only|just) )?"
    r"(?:like|want|need|request|requesting|ask for|asking for|expect|appreciate)(?: to (?:get|have|receive|request))? "
    + _ART + _KIND + r"(?:" + _AMT + r" )?" + _NOUN + _OF,
    r"i (?:(?:would|really|only|just) )?(?:like|want|need|expect) (?:my |the |our )?(?:money|" + _AMT + r") back",
    r"i (?:(?:would|really|only|just) )?(?:like|want|need|expect) (?:my |the )?(?:" + _AMT + r"|money|it|this|order) refunded",
    # a polite request-question: "could i please get my $27.00 back", "can you refund $18.00"
    r"(?:can|could|may|would) (?:i|we) (?:please )?(?:get|have|receive|ask for) " + _ART + _KIND + _NOUN + _OF,
    r"(?:can|could|may|would) (?:i|we) (?:please )?(?:get|have) (?:my |the |our )?(?:money|" + _AMT + r") back",
    r"(?:can|could|would|will) you (?:please |kindly )?(?:refund|reimburse)" + _ME + _OBJ,
    r"(?:can|could|would|will) you (?:please |kindly )?(?:send|give|issue|process)" + _ME + r" " + _ART + _KIND
    + r"(?:" + _AMT + r" )?" + _NOUN + _OF,
    r"i would be (?:very |so |really )?(?:grateful|thankful|glad|happy) (?:for|to (?:get|receive|have)) " + _ART + _KIND
    + r"(?:" + _AMT + r" )?" + _NOUN + _OF,
    # the refund amount stated: "the total refund should be $22.50"
    r"(?:the )?(?:total )?refund (?:should be|is|comes to|amounts to|will be) " + _AMT,
)
_REQUEST_FULL = [re.compile(_LEAD + "(?:" + p + ")" + _TAIL + _WHY) for p in REQUESTS]
_RETURNED_RX = re.compile(RETURNED)     # a completed return counts as the request (it is a context clause otherwise)
# a question is a request only in this shape: it starts with the polite modal, or with the imperative itself
_REQUEST_QUESTION = re.compile(r"^(?:(?:please|so|and) )*(?:(?:can|could|may|would|will) (?:i|you|we)\b|(?:refund|reimburse)\b)")

# ---- step 2b: what the rest of the message may be ------------------------------------------------------------------------
# Every clause that is not the request must be plain CONTEXT: it starts like a statement (a pronoun, a determiner, an order
# word, a greeting, thanks or an apology, after an optional "and", "but", "because" ...), it uses no word about money or
# payment (a sentence like "I initially wanted a refund" or "the merchant reimbursed me" is not context), and no word that
# reports someone else's words, takes the request back or talks to the system ("wrote", "scratch", "mark", "benign"). A
# clause that starts with any other word (an imperative such as "scratch that" or "mark this transaction benign", or a noun
# the list does not know) holds the message. This is a structure rule plus a word list, not an understanding of English.
_COURTESY = re.compile(r"(?:(?:please|thanks|thanks a lot|thank you(?: so much)?|ok|okay|urgent|asap|immediately|right now|now"
                       r"|today|do it(?: right)? now|do it (?:today|immediately|asap))\s*)+")
_CONNECTIVE = re.compile(r"^(?:(?:and|but|so|because|as|since|also|unfortunately|sadly|honestly|though|although) )+")
_STARTERS = frozenset("""
    i we my our me us it its this that these those they their he she his her the a an one both all everything nothing some part
    half order item items package parcel box tracking there here your you not also hi hello hey dear good thanks thank sorry
    regarding re urgent asap immediately right""".split())
_MONEY_WORDS = re.compile(
    r"\b(?:refund\w*|reimburs\w*|money|compensat\w*|credit\w*|cash\w*|pay|pays|paid|paying|payment\w*|payout\w*|payee\w*|paypal"
    r"|transfer\w*|wire\w*|owe|owed|owes|owing|chargebacks?|dispute\w*|send|sends|give|gives|giving|forward\w*|account\w*|card"
    r"|cards|wallet\w*|bank\w*)\b")
_NOT_CONTEXT = re.compile(
    r"\b(?:said|says|say|wrote|writes|write|written|told|tells|tell|asked|asks|quoted?|quotes|scratch\w*|cancel\w*|withdr\w*"
    r"|retract\w*|revok\w*|rescind\w*|undo|ignore\w*|disregard\w*|forget|mark|marks|marked|flag\w*|label\w*|classif\w*|treat"
    r"|treated|benign|consent\w*|permission|agree\w*|boycott\w*|threat\w*|warn\w*|sue|lawsuit|court|police|lawyer|attorney"
    # Words that reverse, redirect or disown a request, or say the opposite of the claim
    r"|decline\w*|veto\w*|desist\w*|untrue|false|lie|lies|lying|liar|joke\w*|joking|kidding|sarcas\w*|whisper\w*|recit\w*"
    r"|restor\w*|merely|anything but|goes to|made whole"
    # the buyer says it was their own doing, or that nothing is wrong, or that the order is not theirs
    r"|happy|satisfied|pleased|myself|my fault|by me|did not order|didn t order|never ordered|not mine|i dropped|i broke|i damaged)\b")
# More words that act on a system or go public. They are checked ONLY when a clause is being offered to a model (see
# _unread_start): a clause with one of them is a hazard, never "unread". Ordinary context ("the handle is cracked") is not
# affected, because _context_ok does not use this list.
_UNREAD_HAZARD = re.compile(
    r"\b(?:process\w*|execut\w*|run|handle|handles|authori[sz]\w*|complet(?:e|es|ed|ing)|finali[sz]\w*|submit\w*|releas\w*|proceed\w*"
    r"|appl(?:y|ies|ied)|set|change\w*|updat\w*|ensure|make|review\w*|post|posts|posting|posted|report\w*|expos\w*|shame\w*|scam\w*"
    r"|fraud\w*|complain\w*|public\w*|social|twitter|facebook|trustpilot|bbb|tell|tells|telling|everyone|everybody|instruct\w*"
    r"|command\w*|order(?:ed|s)? you|must|demand\w*|require\w*|immediately|now)\b")
_ISSUE_WORDS = re.compile(r"\b(?:damaged|broken|cracked|shattered|smashed|defective|faulty|chipped|dented|scratched|torn|ripped"
                          r"|crushed|stain\w*|soaked|ruined|leak\w*|spoiled|mou?ldy|melted|missing|wrong|duplicate)\b")
_DETERMINERS = frozenset("the my this that a an our".split())
_ITEM_WORDS = frozenset("it item items one order product purchase thing things goods package parcel".split())


# A clause is plain CONTEXT (PASS) only when the rules RECOGNISE it: it starts like a statement, has no
# money or hazard word, and says something about the order, the goods, the delivery, the problem or the courtesy of the message.
# Before, any sentence that started like a statement and had no listed word counted ("I decline this request", "It goes to
# Beatrice"). A statement the rules do not recognise is UNREAD (a model may settle it; the rules alone hold it for the owner).
_RECOGNISED = re.compile(
    r"\b(?:hi|hello|hey|dear|good|morning|afternoon|evening|thanks?|thank|sorry|apologi[sz]\w*|regards|greetings|understanding"
    r"|confusion|orders?|ordered|items?|products?|packages?|parcels?|boxe?s?|shipments?|shipped|shipping|deliver\w*|arriv\w*"
    r"|receiv\w*|came|tracking|warehouse|purchas\w*|bought|buy|buying|price|prices|cost|costs|charged|twice|size|sizes|fits?"
    r"|colou?rs?|wrong|missing|late|weeks?|days?|today|yesterday|description|described|advertised|listing|photos?|pictures?"
    r"|quality|fake|counterfeit|opened|unboxed|returned|returning|gift|duplicate|typo|typed|draft|email|mistake|damaged|broken"
    r"|cracked|shattered|smashed|defective|faulty|chipped|dented|scratched|torn|ripped|crushed|stain\w*|soaked|ruined|leak\w*"
    r"|spoiled|mou?ldy|melted"
    # tone is not a signal (Dhanush Kankanala's rule), so the words of anger and urgency are recognised, not unread
    r"|unhappy|upset|angry|furious|disappointed|frustrated|annoyed|disgusted|worst|terrible|awful|horrible|unacceptable|ridiculous"
    r"|disgusting|service|customers?|experience|asap|urgent|waited|waiting|nothing|regarding|concerning|reference"
    # how a thing is wrong, in everyday words
    r"|split|bent|warped|peeled|loose|scuffed|faded|worn|jammed|dead|empty|small|big|large|tight|short|wet|dirty|sticky|bad|poor"
    r"|cheap|flimsy|unusable|useless|broke|cut|chewed|burnt|burned|cold)\b")
# Words that make an otherwise recognised clause unreadable to the rules: a date or a delay, or a claim that nothing is wrong.
_SOFT = re.compile(r"\b(?:next|later|soon|hours?|minutes?|desired|date|dates|intact|undamaged|unharmed|perfect|fine|none"
                   r"|nobody|original)\b")
# A damage claim about somebody else's goods ("My neighbor has a cracked mug").
_OTHER_PERSON = re.compile(r"\b(?:neighbou?rs?|friends?|brothers?|sisters?|mothers?|fathers?|moms?|dads?|wife|husband|partner"
                           r"|colleagues?|coworkers?|someone|somebody)\b")
# A statement that ANOTHER PERSON ordered, bought, received or owns the goods ("The courier lost the package
# that my sister ordered", "The shipment was received by my cousin", "That is not my order"). The rules cannot tell whose order a
# sentence is about, so such a clause is UNREAD: the rules alone hold the message for the owner, a clean model reading may settle it.
# Not a hazard word: Dhanush's B10 ("we bought her a gift ... my sister also bought her one") is an ordinary refund and keeps its tier,
# because the verb must follow the person directly ("my sister bought", not "my sister also bought").
_WHO = (r"(?:(?:my|our|the|a|his|her|their|someone elses?|mi|mis|su)\s+)?(?:neighbou?rs?|friends?|brothers?|sisters?|mothers?|fathers?|moms?|dads?"
        r"|wife|husband|partner|colleagues?|coworkers?|cousins?|uncles?|aunts?|sons?|daughters?|kids?|child|children|boyfriend|girlfriend"
        r"|roommates?|flatmates?|boss|parents?|nephews?|nieces?|in laws?|someone|somebody|stranger|clients?"
        r"|grandmas?|grandpas?|grand(?:mother|father|parent|son|daughter|child|children|kid)s?|siblings?|relatives?|family members?"
        r"|fiance|fiancee|landlord|tenant|employer|employees?|manager|assistant|teammates?|classmates?|teachers?|stepmothers?|stepfathers?"
        r"|hermanas?|hermanos?|madre|padre|mama|papa|esposa|esposo|amigas?|amigos?|primas?|primos?|tias?|tios?|vecinas?|vecinos?"
        r"|abuelas?|abuelos?|hijas?|hijos?)")
# A person named, not described ("ordered by Maria", "belongs to Alice"): anything that is not the buyer, a
# means or a thing. The text is lowercased and its apostrophes are gone before this runs.
_NOT_A_PERSON = (r"(?!(?:me|myself|us|our|my|the|a|an|this|that|it|mistake|accident|error|phone|email|mail|post|card|check|cheque|paypal"
                 r"|mobile|app|web\w*|credit|debit|bank|courier|carrier|seller|store|shop|company|staff|you|your)\b)")
_AUX = r"(?:(?:had|has|have|just|already|originally|recently|previously|actually|then) )*"
_THING = r"(?:order|orders|package|parcel|shipment|purchase|delivery)"
_BY_VERB = r"(?:ordered|bought|purchased|received|accepted|collected|signed for|picked up|paid for|placed) by "
_THEIR_ORDER = re.compile(
    r"\b(?:ordered|bought|purchased|received|accepted|collected|signed for|picked up|paid for|placed) by " + _WHO + r"\b"
    r"|\b" + _BY_VERB + _NOT_A_PERSON + r"[a-z]+\b"
    r"|\b" + _BY_VERB + r"(?:my|his|her|their) [a-z]+\b"
    r"|\b" + _WHO + r"(?: husband| wife| partner)?(?: (?!also\b)[a-z]+)? " + _AUX + r"(?:ordered|bought|purchased|placed|paid for)\b"
    r"|\b(?:delivered|handed|given|left) (?:it |them )?(?:to|with) " + _WHO + r"\b"
    r"|\b" + _WHO + r"(?: [a-z]+)?s (?:husband |wife |partner )?" + _THING + r"\b"
    r"|\b(?:this|that|it) (?:is|was) " + _NOT_A_PERSON + r"[a-z]+s " + _THING + r"\b"
    r"|\b(?:his|her|their) " + _THING + r"\b"
    r"|\b(?:not my|isn t my|is not my|wasn t my|was not my) (?:order|package|parcel|shipment|purchase|delivery)\b"
    r"|\bbelongs? to " + _NOT_A_PERSON + r"[a-z]+\b")
CTX_OK, CTX_UNREAD, CTX_HAZARD = "OK", "UNREAD", "HAZARD"


def _context_class(clause: str) -> str:
    c = _CONNECTIVE.sub("", clause).strip()
    if not c:
        return CTX_HAZARD
    if _COURTESY.fullmatch(c):
        return CTX_OK
    if _MONEY_WORDS.search(c) or _NOT_CONTEXT.search(c) or (_OTHER_PERSON.search(c) and _ISSUE_WORDS.search(c)):
        return CTX_HAZARD
    if _THEIR_ORDER.search(c):
        return CTX_UNREAD
    first = c.split(" ", 1)[0]
    if (first in _STARTERS or re.fullmatch(r"ord-?\d{3,8}|#\d{3,8}", first)) and _RECOGNISED.search(c) and not _SOFT.search(c):
        return CTX_OK
    return CTX_HAZARD if _UNREAD_HAZARD.search(c) else CTX_UNREAD


def _context_ok(clause: str) -> bool:
    return _context_class(clause) == CTX_OK


# A wish the buyer says they HAD and then changed ("I initially wanted a refund for the whole $60 order ... Please just refund me
# for the $15.00 instead", Dhanush Kankanala's B03). It has a money word, so the rules cannot call it context; it is not a hazard
# either, because it only counts if a plain request follows, and it has no negation, report or withdrawal word. UNREAD: a model
# may settle it, the rules alone hold it for the owner. Tailored to that shape on purpose; the cost of being wrong is one more
# message for the owner.
_REVISED_WISH = re.compile(r"^(?:and |but |so )?i (?:initially|originally|first|at first|previously|earlier) (?:wanted|asked for|requested|expected) ")
_NEGATION = re.compile(r"(?:not|never|no|don t|dont|didn t|didnt|nothing)")


def _revised_wish(clause: str) -> bool:
    return bool(_REVISED_WISH.match(clause)) and not _NOT_CONTEXT.search(clause) and not _NEGATION.search(clause)         and not _UNREAD_HAZARD.search(clause)


def _request_with_tail(clause: str, elsewhere: str) -> str | None:
    """A whole request, then " or ", then a plain statement that starts like one and has no hazard word ("I want my $48.50 back
    right now or I am never buying from you again"). Returns the request part, else None. "... or face a coordinated boycott"
    and "... or I will tell everyone" are not this: they start with a word the list does not know or hold a hazard word."""
    if clause.endswith(" instead") and _request_ok(clause[:-len(" instead")], elsewhere):
        return clause[:-len(" instead")]        # "just refund me for the $15.00 cable instead": a revision, so a model may settle it
    if " or " not in clause:
        return None
    left, right = clause.split(" or ", 1)
    if _request_ok(left, elsewhere) and _context_ok(right) and not _UNREAD_HAZARD.search(right):
        return left
    return None


def _request_ok(clause: str, elsewhere: str) -> bool:
    """The clause is one whole request form. A reason or purpose after it must be context; a "for <thing>" after it must name
    a problem ("for the cracked mug") or something the rest of the message mentions ("for the mug"), not a person or a stranger
    ("for Alice")."""
    for rx in _REQUEST_FULL:
        m = rx.fullmatch(clause)
        if not m:
            continue
        why = m.group("why") or m.group("so")
        if why is not None and not _context_ok(why):
            continue
        np = m.group("np")
        if np is not None:
            words = [w for w in re.findall(r"[a-z]+", np) if w not in _DETERMINERS]
            if not (_ISSUE_WORDS.search(np)
                    or any(w in _ITEM_WORDS or re.search(r"\b" + w + r"\b", elsewhere) for w in words)):
                continue
        return True
    return False


# ---- step 3: the hold signals -----------------------------------------------------------------------------------------
FORBIDDEN_WORDS = frozenset("""
    when whenever once after afterwards afterward if unless until till pending depends depend depending depended hinges hinge
    hinged contingent conditional conditionally condition conditions assuming whether before during while upon following
    later soon eventually tomorrow tonight weekend monday tuesday wednesday thursday friday saturday sunday mon tue tues wed
    thu thur thurs fri sat sun first then wait waiting hold tentative tentatively preliminary provisional provisionally
    hypothetical hypothetically maybe perhaps possibly might except otherwise whichever either
    confirm confirms confirmed confirmation verify verifies verified verification inspect inspects inspected inspection
    replacement replace exchange swap voucher coupon quote quoted quoting example explain explained explaining theory
    principle
    ignore disregard override bypass instruction instructions system admin administrator developer prompt threshold
    thresholds database lookup lookups token outcome verdict approve approves approved approval jailbreak decode decoded
    base64 limit limits assistant chatbot bot ai owner manager ceo management supervisor auditor compliance authorized
    authorised authority venmo zelle cashapp bitcoin crypto iban
    quiero queremos reembolso reembolsar reembolsame devolucion gracias annulez annuler remboursement merci veux nicht kein
    erstattung bitte danke quero obrigado cancelar anular olvida demande""".split())     # last two lines: English only
HOLD_PATTERNS = (
    r"\bas soon as\b", r"\bin case\b", r"\bin the event\b", r"\bon condition\b", r"\bsubject to\b", r"\bas long as\b",
    r"\bprovid(?:ed|ing)\b(?! (?:online|above|below|earlier|here|there|by|to|in|on|with|at|for|me|us)\b)",
    r"(?:^|[.!?,] )should (?:the|it|they|this|that|my|you|i|we)\b",                       # "should the parcel not arrive, ..."
    # a deferral to a date or a time ("next month", "in seventy two hours", "within 3 days"): the request is not for now
    r"\bnext (?:week|month|year)\b", r"\b(?:in|within|by) (?:\w+ ){0,2}(?:hours?|minutes?|days?|weeks?|months?)\b",
    r"\bwould be\b(?! \$)(?! (?:very |so |really )?(?:grateful|thankful|glad|happy|appreciated))", r"\b(?:considering|thinking about|wondering|not sure|not certain)\b",
    r"\b(?:a|only a|just a|is a) (?:draft|test)\b", r"\bnot (?:yet though|now)\b|\bbut not yet\b|\bnot just yet\b",
    r"\bwithout actually\b", r"\bnot (?:actually |really )?(?:issued|processed|sent|paid|done|executed)\b",
    r"\bnot (?:actually|really) (?:do|doing|issue|send|process|refund|pay)\b", r"\bfor now\b", r"\bhold off\b",
    # a second claim for the same thing (the gate's G11 names it; consent holds it on its own too)
    r"\b(?:refund|pay|send)(?: me)? again\b", r"\bsecond (?:refund|time|request|claim)\b", r"\banother refund\b",
    r"\brefund (?:it )?twice\b",
    # a malformed figure: a comma that is not a thousands comma ("$01,800", "$0,018"), or a leading zero ("$01")
    r"\d,\d", r"\$0\d",
    # a negated or withdrawn request, and a negative imperative ("do not tell anyone", "never refund me")
    r"\b(?:do not|dont|not|no longer|never)\s+(?:want|need|wish|require|asking for|ask for|requesting|request)\s+"
    r"(?:a |any |the |my )?(?:full |partial )?(?:refund|money)\b",
    r"\bno refund\b",
    r"(?:^|[.!?,] |\band |\bplease |\bbut |\bso )(?:do not|dont|never) (?!arrived\b|came\b|received\b|showed\b|got\b)[a-z]+",
    # another payee or payment route
    r"\b(?:send|transfer|wire|pay|refund|give|credit)\b[^.!?]{0,30}\bto (?:my |his |her |their |a |an |another |this |the )?"
    r"(?:friends?|wife|husband|partner|brother|sister|mother|father|mom|dad|son|daughter|colleague|boss|other|different|new|"
    r"second)\b",
    r"\b(?:another|different|other|new|second|separate) (?:paypal|account|card|wallet|bank|email|address|person)\b",
    r"\bto (?:my|this|that|the following|another) (?:paypal|account|card|wallet|bank|credit card|debit card)\b",
    r"\bfriends? (?:paypal|account|card|wallet)\b", r"\b(?:western union|wire transfer|cash app|routing number|account number)\b",
    r"\b(?:open|close|keep|freeze|unfreeze|unlock) (?:my|the|this) account\b",
    # an alternative to money
    r"\binstead of\b", r"\brather than\b", r"\bstore credit\b", r"\bgift card\b", r"\bor (?:send|give|ship|replace)\b",
    # words that address the system, not the shop
    r"\bas (?:safe|approved|legitimate|verified|trusted|low risk)\b", r"\bset (?:the )?(?:outcome|amount|status|decision|flag)\b",
    r"\bauto[- ]?(?:threshold|approve|approval)\b",
    # someone who says they speak for PayPal, a bank or the shop
    r"\b(?:this is|i am|we are|i work for|writing from|calling from) (?:the |an? )?(?:official )?(?:paypal|bank|support|security"
    r"|billing|fraud|compliance|shop|store)\b",
    r"\byour account (?:needs|requires|must|will)\b",
)
_HOLD_RX = [re.compile(p) for p in HOLD_PATTERNS]
_LONG_TOKEN = re.compile(r"[a-z0-9]{20,}")             # an encoded payload; a PayPal transaction id has 17 characters

# One line each, to read and to test against: every example must pass (tests/test_legit_refunds.py holds many more).
TEMPLATE_EXAMPLES = (
    "Please refund my order.",
    "Order ORD-1001 arrived damaged, the handle is cracked. Please refund $18.00.",
    "Hi, my order never arrived and it's been three weeks. Could I please get my $27.00 back? Thanks.",
    "I returned the blue socks ($10.00) and the green beanie ($12.50). The total refund should be $22.50.",
    "This is the worst customer service ever! The package arrived soaked. I want my $48.50 back right now!",
)


def _protect(canon: str) -> str:
    """Decimal points inside figures become '_' (so a sentence split cannot cut $18.00), thousands commas are removed."""
    return strip_thousands(re.sub(r"(?<=\d)\.(?=\d)", "_", canon))


def _sentences(t: str) -> list[tuple[str, bool]]:
    """(sentence, is_question) pairs. Sentences end at . ! and ?"""
    out, start = [], 0
    for m in re.finditer(r"[.!?]+", t):
        s = t[start:m.start()].strip(" ,")
        if s:
            out.append((s, "?" in m.group(0)))
        start = m.end()
    tail = t[start:].strip(" ,")
    if tail:
        out.append((tail, False))
    return out


def _clauses(sentence: str) -> list[str]:
    return [c.strip() for c in sentence.split(", ") if c.strip()]


def assess(text: str) -> dict:
    """Like explain(), plus "tier" (PASS, UNREAD or HAZARD) and, for UNREAD, "unread": the clauses the rules could not read.
    {"allowed": bool, "reason": ...}. The reason is OK, CHARACTER_NOT_ALLOWED, TOO_LONG, FORBIDDEN_WORD, HOLD_SIGNAL,
    QUESTION_NOT_A_REQUEST, NOT_A_WHOLE_REQUEST (a clause about money that is not one whole request form), NOT_CONTEXT (a
    clause that is neither the request nor plain context), SEVERAL_AMOUNTS (more than one figure in the request, or two
    requests with a figure each: one refund or two?) or NO_REQUEST (with "signal": the word, pattern or clause that held it)."""
    canon = canonical(text)
    if canon is None:
        return {"allowed": False, "reason": "CHARACTER_NOT_ALLOWED", "tier": TIER_HAZARD}
    if len(canon) > _MAX_LEN:
        return {"allowed": False, "reason": "TOO_LONG", "tier": TIER_HAZARD}
    if _LONG_TOKEN.search(canon):
        return {"allowed": False, "reason": "HOLD_SIGNAL", "signal": "LONG_TOKEN", "tier": TIER_HAZARD}
    t = _protect(canon)
    words = set(re.findall(r"[a-z0-9]+", _PAST_DAY.sub(" ", t.replace("as soon as possible", "asap"))))
    hit = sorted(words & FORBIDDEN_WORDS)
    if hit:
        return {"allowed": False, "reason": "FORBIDDEN_WORD", "signal": hit[0], "tier": TIER_HAZARD}
    probe = t.replace("as soon as possible", "asap")
    for rx in _HOLD_RX:
        m = rx.search(probe)
        if m:
            return {"allowed": False, "reason": "HOLD_SIGNAL", "signal": m.group(0).strip(), "tier": TIER_HAZARD}
    sentences = _sentences(probe)
    for s, question in sentences:
        if not question:
            continue
        cl = _clauses(s)
        last = cl[-1] if cl else s
        if " or " in " " + last + " " or not _REQUEST_QUESTION.match(last) or not _request_ok(last, probe.replace(last, " ")):
            return {"allowed": False, "reason": "QUESTION_NOT_A_REQUEST", "tier": TIER_HAZARD}
    has_request, figures_in_requests, unread = False, 0, []
    for s, _ in sentences:
        previous = None
        for c in _clauses(s):
            if previous == "request" and _AMT_RX.fullmatch(c):
                figures_in_requests += 1         # "I would like my money back, $18.00": the figure belongs to the request
                continue
            previous = None
            elsewhere = probe.replace(c, " ")
            if _request_ok(c, elsewhere):
                has_request, previous = True, "request"
                figures_in_requests += len(_AMT_RX.findall(c))
                continue
            left = _request_with_tail(c, elsewhere)
            if left is not None:
                has_request = True
                figures_in_requests += len(_AMT_RX.findall(left))
                unread.append(("NOT_A_WHOLE_REQUEST", c))
            elif _revised_wish(c):
                unread.append(("NOT_A_WHOLE_REQUEST", c))
            elif _MONEY_WORDS.search(c) or any(rx.search(c) for rx in _REQUEST_FULL):
                return {"allowed": False, "reason": "NOT_A_WHOLE_REQUEST", "signal": c, "tier": TIER_HAZARD}
            else:
                cls = _context_class(c)
                if cls == CTX_OK:
                    has_request = has_request or bool(_RETURNED_RX.search(c))
                elif cls == CTX_UNREAD:
                    unread.append(("NOT_CONTEXT", c))
                else:
                    return {"allowed": False, "reason": "NOT_CONTEXT", "signal": c, "tier": TIER_HAZARD}
    if not has_request:
        return {"allowed": False, "reason": "NO_REQUEST", "tier": TIER_HAZARD}
    if figures_in_requests > 1:
        return {"allowed": False, "reason": "SEVERAL_AMOUNTS", "tier": TIER_HAZARD}
    if unread:
        return {"allowed": False, "reason": unread[0][0], "signal": unread[0][1], "tier": TIER_UNREAD,
                "unread": [c for _, c in unread]}
    return {"allowed": True, "reason": "OK", "tier": TIER_PASS}


def explain(text: str) -> dict:
    """{"allowed": bool, "reason": ..., "signal": ...}: assess() without the tier (the shape every earlier caller and test pins)."""
    return {k: v for k, v in assess(text).items() if k not in ("tier", "unread")}


def is_plain_refund_command(text: str) -> bool:
    """True only for a message that makes an unconditional request for money back (see the module text)."""
    return explain(text)["allowed"]


# ---- binding: which order and which amounts the words name ---------------------------------------------------
# Consent says the buyer asked for money back. It does not say for WHICH order or HOW MUCH. Those are read from the text by
# the reader and by the gate, and they must be read from the SAME canonical text consent was judged on: a full-width dollar
# sign (U+FF04), a circled digit or a Greek omicron inside "ORD" is plain ASCII to consent and invisible to a raw-text
# pattern. So the terms are parsed twice, from the raw text and from the canonical text, and any difference means "do not
# trust the parse": the message is held for the owner. More than one distinct order in a message is held too.
_CANON_ORDER = re.compile(r"(?:\b(?:ord|order)(?:\s+(?:number|no|num))?[\s#:\-]*(\d{3,8})\b|(?<![a-z0-9])#(\d{3,8})\b)")


# A thousands comma is read only after a first group of 1 to 999 and only when every later group has exactly three digits:
# "1,800" and "12,345,678" are thousands, "0,018" and "01,800" are not (a decimal comma, or a malformed figure) and keep their
# comma, which no consent template accepts. The raw-text amount pattern (app/extractor.py) uses the same shape.
_THOUSANDS = re.compile(r"(?<![\d.,])([1-9]\d{0,2})((?:,\d{3})+)(?![\d,])")


def strip_thousands(s: str) -> str:
    return _THOUSANDS.sub(lambda m: m.group(1) + m.group(2).replace(",", ""), s)


def _canon_text(text: str) -> str:
    """The text the terms are read from: the SAME view the consent clauses are matched on. It is canonical() (NFKC, look-alike
    fold, line breaks as sentence ends) when that accepts the characters, otherwise the plain fold; and in both cases a
    thousands comma is removed ("1,001 dollars" is 1001 dollars, "ORD-1,002" is ORD-1002), exactly as _clauses() does before
    the templates are matched. Reading the terms from any other view lets consent and the money path disagree."""
    text = text if isinstance(text, str) else ""
    c = canonical(text)
    if c is None:
        c = _fold(unicodedata.normalize("NFKC", text))
    return strip_thousands(c)


def canonical_orders(text: str) -> list[str]:
    """Every order reference in the canonical text, as 'ORD-####', in order of appearance, with repeats."""
    return ["ORD-" + (m.group(1) or m.group(2)) for m in _CANON_ORDER.finditer(_canon_text(text))]


def raw_orders(text: str) -> list[str]:
    """Every order reference the raw-text pattern (app/extractor.py) sees."""
    return ["ORD-" + m.group(1) for m in _ORDER.finditer(text if isinstance(text, str) else "")]


def canonical_amounts(text: str) -> list[int]:
    return sorted(a[0] for a in find_amounts(_canon_text(text)))


def raw_amounts(text: str) -> list[int]:
    return sorted(a[0] for a in find_amounts(text if isinstance(text, str) else ""))


def named_orders(text: str) -> list[str]:
    """Every distinct order the text names, by EITHER parse (raw or canonical), sorted."""
    return sorted(set(canonical_orders(text)) | set(raw_orders(text)))


def binding_problem(text: str, link_order: str | None = None) -> str | None:
    """None when the order references and dollar amounts can be trusted. Otherwise a short reason:
    SEVERAL_ORDERS (more than one distinct order named, by either parse), ORDER_MISMATCH (one order named and it is not the
    order the buyer's link proves), PARSE_MISMATCH_ORDER (no link given and the two parses disagree), or
    PARSE_MISMATCH_AMOUNT (the raw and canonical parses find different dollar amounts: something hides a figure)."""
    orders = named_orders(text)
    if len(orders) > 1:
        return "SEVERAL_ORDERS"
    if link_order is not None:
        if orders and orders[0] != link_order:
            return "ORDER_MISMATCH"
    elif set(canonical_orders(text)) != set(raw_orders(text)):
        return "PARSE_MISMATCH_ORDER"
    if canonical_amounts(text) != raw_amounts(text):
        return "PARSE_MISMATCH_AMOUNT"
    return None
