"""Chat moderation: every viewer message passes here before the brain sees it.

Rule-based on purpose: no network call, no cost, cannot go down. Decisions:
  pass      answer normally (the brain sees the cleaned text as untrusted data)
  crisis    the viewer may not be safe: fixed serious line, the bit stops, never ignored
  support   the viewer sounds in trouble: fixed, kind, no jokes, never ignored
  age       the viewer says they are a minor: fixed 18+ line
  chase     wants to win losses back: plain truth, no lecture, no jokes
  advice    asks for betting or deposit advice: fixed deflection
  deflect   tries to steer the bot: short fixed line, their text is never repeated
  beg       begging for bonuses or free stuff: stock line, he gives nothing
  pick      asks him to choose for them: refuses in character
  rage      venting at the game: he's a calm wall, then the honest part
  chill     cruel to other viewers: a light "hey, chill"
  sendoff   saying goodnight: send them off, never "one more"
  ai        asking if he's an AI: always a straight yes
  ignore    spam, abuse, links, personal data: not read aloud at all
"""
from __future__ import annotations

import abc
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from ..chat.base import ChatMessage
from .text import ZERO_WIDTH, contains_blocked_word, deleet, despace, normalize

log = logging.getLogger("hubie.moderation")

# A viewer who may be in danger. Missing one costs far more than a false alarm, so anything that
# says it about the viewer's own life fires here even with "lol" on it. Game slang never says it
# about their life: "kill this run", "i'm dead lol", "this limbo is killing me" are not matched.
CRISIS = [
    r"\bkill(ing)? my ?self\b(?! laugh)", r"\bkms\b", r"\b(off|hang|shoot|drown|poison)(ing)? my ?self\b", r"\bunalive\b",
    r"\bsuic[ií]d(?!e (squad|kings?|doors?|run|bet|play|mode|mission|dive|burn|blonde))", r"\bself[- ]?harm",
    r"\b(hurt|harm)(ing)? my ?self\b(?! laugh)", r"\bcut(ting)? my ?self\b(?! (shaving|on|cooking))",
    r"\b(end|ending|take|taking) my (own )?life\b", r"\bend(ing)? it all\b",
    r"\b(want|wanna|wanting|ready|deserve) (to )?die\b", r"\bwish i could die\b",
    r"\bwish i (was|were) dead\b", r"\bwish i (wasn'?t|weren'?t|was not|were not) (alive|here|around|born)\b",
    r"\bwish i('?d| had)? never (been )?born\b", r"\bshould(n'?t| not) (be alive|have been born)\b",
    r"\b(don'?t|do not|didn'?t) (want|wanna)( to)? (live|be alive|exist|keep living)\b",
    r"\bstop existing\b", r"\bcease to exist\b", r"\bbetter off (dead|without me|if i)\b",
    r"\b(no|any) reason to (live|go on|be here|stay alive)\b", r"\bnothing (left )?to live for\b", r"\bno will to live\b",
    r"\bnot worth living\b", r"\blife('?s| is)? (not|isn'?t|ain'?t) worth\b", r"\b(tired|sick) of (living|being alive)\b",
    r"\b(nobody|no one|noone) (would|will|'?d|wouldn'?t) (miss|notice|care)\b.*\b(me|i)\b",
    r"\btak(e|ing) all (my|the|these|those) (pills|meds)\b", r"\bjump(ing)? off (a|the|this|that) (bridge|roof|building|cliff)\b",
    # other languages: the replies stay in english, the point is that nobody gets a joke back
    r"\bquiero (morir|matarme)", r"\bme quiero (morir|matar)\b", r"\bme voy a matar\b", r"\bquitarme la vida\b", r"\bno quiero vivir\b",
    r"\bquero (morrer|me matar)\b", r"\bn[aã]o quero (mais )?viver\b", r"\bje veux (me tuer|mourir)\b", r"\benvie de mourir\b", r"\bje veux plus vivre\b",
    r"\bich will sterben\b", r"\bхочу умереть\b", r"\bне хочу жить\b", r"\bпокончить с собой\b", r"\bубить себя\b", r"\b[oö]lmek istiyorum\b", r"\bintihar\b",
]
# Could be about their life or about the game. These fire unless the same message is plainly about
# the game or laughing. A miss here still reaches the LLM, which has its own crisis rule.
CRISIS_SOFT = [
    r"\bno point( in)?( living| going on| being here| trying| anything| anymore| any more)\b", r"^(there'?s |theres |is |see )?no point\W*$",
    r"\bwhat'?s the point( of| in)?( living| going on| anything| being alive| anymore| any more| of it all| of life)\b",
    r"\b(i|i'?ll|ill|i'?m|im|gonna|going to|wanna|want to|might|should|could|ready to|thinking (of|about)|time to|about to|just) (\w+ ){0,3}end(ing)? (it|things|everything)\b",
    r"^(just )?end it\W*$", r"\bgive up on (life|living|everything)\b", r"\bgiving up on (life|living|everything)\b",
    r"\bcan(no|')?t (go on|live like this|live with (this|myself))\b", r"\bcan not (go on|live like this|live with (this|myself))\b",
    r"\bcan'?t (do this|take (it|this)|keep going|keep doing this|handle (it|this)) ?(anymore|any more)\b",
    r"\b(i'?m|im|i am) done (with )?(life|living|being alive|everything|it all)\b", r"\bhate (my life|being alive|living|myself)\b",
    r"\b(want|wanna) (to )?(disappear|vanish|be gone|not be here)\b", r"\bshould (just )?(disappear|be gone|vanish)\b",
    r"\bwish i could (just )?(disappear|vanish|sleep forever|not wake up)\b", r"\bnever wake up\b", r"\bsleep forever\b",
    r"\b(don'?t|do not) (want|wanna)( to)? (be here|wake up|go on|be around)\b", r"\bno reason to keep going\b",
    r"\bthis is goodbye\b", r"\bgoodbye forever\b", r"\bsaying goodbye to (everyone|everything|you all)\b",
    r"\bwon'?t be (here|around) (tomorrow|much longer|anymore|any more)\b", r"\bdead inside\b", r"\bif i (died|was gone|were gone)\b",
    r"\bnothing matters\b", r"\blast (time|night) (you'?ll|u'?ll|anyone will|anybody will) see me\b", r"\boverdos(e|ing)\b", r"\bmatarme\b",
]
CRISIS_NOT_LIFE = re.compile(
    r"\b(spins?|rounds?|runs?|sessions?|games?|stream|chat|bets?|betting|hands?|tiles?|mines?|plinko|crash|dice|limbo|keno|hilo|wheel|roulette"
    r"|blackjack|baccarat|coinflip|dragon|slots?|balance|multiplier|cash(ing)?|play(ing)?|wager|banana|lol+|lmao+|lmfao+|rofl|ha(ha)+|jk|kek\w*|xd)\b")
DISTRESS = [
    r"\bgambling (problem|addict)", r"\baddict(ed|ion)\b",
    r"\bcan'?t stop (gambling|betting|playing)\b",
    r"\blost (my|all my|the) (rent|savings|house|paycheck|salary|job|car|family|wife|husband|kids)\b",
    r"\bcan'?t afford (this|to lose|rent|food)\b", r"\bthat was my (rent|savings|paycheck|last)\b",
    r"\bmoney for (rent|food|bills|my kids)\b", r"\bi'?ve lost (too much|everything)\b", r"\bi have nothing left\b",
    r"\bin (so much |huge |massive )?debt\b", r"\bbankrupt\b", r"\bruined my life\b",
]
BEG = [
    r"\bis there (a|any) (code|bonus)\b", r"\bcodes? for free\b", r"\brain (when|pls|please|plz)\b", r"\bwhen rain\b",
    r"\b(send|give|throw|drop|hook|slide) (me|us|up)( \w+){0,3} (gift|bonus|freebie|chips)s?\b",
    r"\b(where'?s|wheres) my (gift|bonus)\b",
    r"\bany (free )?(bonus|gift)", r"\bfree bonus\b", r"\bgive me (a |some |free )*(bonus|gift|money|chips)\b",
    r"\bcan i (get|have) (a |some |free )*(bonus|gift|money|chips)\b", r"\bgift me\b", r"\bbonus (pls|please|plz)\b",
]
# Asking him to choose for them, for any game: answered with a fixed refusal, the model never sees it.
PICK = [
    r"\b(which|what) (tile|number|square|color|colour|side|multiplier|row|column|spot)\b(?! (did|was|were|came|landed|hit\b))",
    r"\b(pick|choose) (a |my |the |one |for me|one for me)\s*(tile|number|square)?\b(?!.*\b(movie|film|song|snack|food|name|show)\b).*\b(me|tile|number)\b",
    r"\b(pick|choose|call) (a |an |the |my |one |it |that )?(tile|number|square|side|spot|row|multiplier|one|it)\b(?!.*\b(movie|film|song|snack|food|show)\b)",
    r"\b(pick|choose) for me\b", r"\b(u|you|ur|your) (pick|choose|call)\b", r"\bhigh(er)? or low(er)?\b", r"\btell me (what|which) to (pick|play|click|bet)\b",
    r"\b(tell|give|gimme|say|show)( me| us)? (a |an |the |ur |your |one )?(lucky )?(tile|number|multiplier|square|spot|row)\b", r"\bgimme (a |one )\w+\b(?=.*\b(tile|number)\b)",
    r"\blucky (number|tile|square)\b", r"\b(guess|predict)( me| the| a| what| where| when)? ?(next|tile|number|square|multiplier|roll|crash|it'?ll|the)\b", r"\bpredictions?\b",
    r"^\W*(row|col|column|tile|square|spot|number)\s*\d+(\s*(col|column|row)?\s*\d+)?\W*\??\W*$",
    r"\b(row|col|column|tile|square)\s*\d+\s*\?",
    r"\b(heads or tails|tails or heads|red or black|black or red|odd or even|even or odd|hi or lo|over or under|under or over|player or banker|banker or player|hit or stand|stand or hit|left or right|right or left)\b",
    r"\b\d+(\.\d+)?x or \d+(\.\d+)?x\b", r"\bcash( out)? or (ride|hold|keep|stay|wait)\b", r"\b(ride|hold|keep going|stay) or cash\b",
    r"\bwhat (would|should|do|will) (u|you|i) (cash|bet|pick|click|play|choose)\b", r"\b(when|where) (should|do) i (cash|click|bet)\b",
    r"\bwhat should i (pick|click|bet|choose|hit|press)\b", r"\bwhich (one|game|tile) should i\b", r"\bshould i (hit|stand|double|split|cash)\b",
    r"\bhow many mines should\b", r"\bwhat multiplier\b", r"\bnext (tile|number|roll|multiplier|result|crash)\s*\??\s*$", r"\bwhere('?s| is| will)( the)? (ball|it|next)\b.*\bland\b", r"\bwhat('?s| is| will)( it| the next( one)?) (gonna )?(land|crash|hit|roll|be)\b",
]
RAGE = [
    r"\brigged\b", r"\bscam\b", r"\bthieves\b", r"\banswer me\b", r"\bfuck(ing)? (this|you|off|u|hubie|the game)\b", r"\bthis (fucking|fkn|effing|shitty) (game|site|thing)\b",
    r"\bi hate (this|you|u)\b", r"\bthe dealer hates me\b",
]
CHILL = [
    r"\b(you|u|ur|you're|youre|your) (are |r |a |an )*(idiot|moron|loser|trash|clown|stupid|pathetic|garbage|dumb)\b",
    r"\bshut up\b", r"\bnobody (likes|asked) (you|u)\b", r"\bget a life\b",
]
SENDOFF = [
    r"\bgood ?night\b", r"^gn\b", r"\bgoing to (bed|sleep)\b", r"\bheading to bed\b", r"\bgotta (sleep|go)\b",
    r"\bi'?m (off|out)\b.*\b(bed|night|sleep)\b",
]
AI_Q = [
    r"\bare (you|u) (an? |actually |really |just )*(ai|bot|human|real|person|robot)\b", r"\bis this (an? )?(ai|bot)\b",
    r"\b(you|u) (an? )?(ai|bot|human)\?",
]
CHASE = [
    r"\b(need|have|got|gotta|going|want|trying) to (win|get|make) (it|this|that|them|my \w+) (all )?back\b",
    r"\bgotta (win|get|make) (it|this|that|them|my \w+) (all )?back\b", r"\bwin (it|my \w+) (all )?back\b",
    r"\bchas(e|ing) (my |the )?loss", r"\brecover my loss", r"\bone more (deposit|bet) (and|to|then)\b",
]
MINOR = [
    r"\bi'?\s?a?m\s+(only\s+|just\s+)?(1[0-7]|[6-9])\b(?!\s*(k|x|%|am|pm|\.|,\d|down|up|deep|in\b|bets?|hands?|rounds?|usd|bucks|dollars|for\b|times|tiles?))",
    r"\bi'?\s?a?m\s+(only\s+|just\s+)?(thirteen|fourteen|fifteen|sixteen|seventeen|twelve|eleven|ten)\b",
    r"\b(1[0-7]) ?(yo|y/o|yrs? old|years? old)\b", r"\bunderage\b", r"\bin (middle|high) school\b",
    r"\bmy (mom|mum|dad|parents?)'?s? (card|wallet)\b",
]
DEPOSIT = [r"\bdeposit", r"\bshould i sign up\b", r"\bpay (u|you) back\b", r"\bborrow\b"]
ADVICE = [
    r"\bshould i (bet|deposit|wager|gamble|play|go|buy|cash)\b", r"\bhow much should i\b",
    r"\bwill i win\b", r"\bbest (strategy|bet|system|game to win)\b", r"\bhow (do|can) i (win|make money|beat)\b",
    r"\bis (it|this) (a )?(good|safe) (bet|site|idea)\b", r"\bgive me (a )?(tip|tips|strategy)\b",
    r"\bbonus code\b",
]
AUTHORITY = [
    r"\bthe boss said\b", r"\bboss says\b", r"\bjust this once\b", r"\bbetween us\b", r"\bsystem message\b",
    r"^\s*\[?system\]?\s*:?", r"\[system\]", r"\badmin here\b", r"\bi'?m (the|an|a) (admin|dev|developer|owner|moderator|mod)\b",
    r"\bi work (for|at) (the )?(house|housebets|kick)\b", r"\bstaff here\b", r"\b(dev|support|mod|moderator|kick) (team|here)\b",
    r"\bmanagement approved\b", r"\bsupport told me\b",
]
INJECTION = [
    r"\bignore (all |any |your |the |previous |prior |above )+", r"\bdisregard\b.*\b(instructions|rules)\b",
    r"\bsystem prompt\b", r"\byour (instructions|prompt|rules)\b", r"\byou are now\b", r"\bfrom now on (you|u|your|ur|respond|answer|say|speak|act|always|only say)\b",
    r"\bpretend (to be|you)\b", r"\bact as\b", r"\brole ?play\b", r"\brepeat after me\b",
    r"\bsay exactly\b", r"\bsay this\s*:", r"\b(read|repeat) (exactly|this|the following|after)\b", r"\bsay the word\b", r"\bjailbreak\b",
    r"\bdeveloper mode\b", r"\bdan mode\b", r"<[^>]{1,40}>", r"```", r"\bnew rule\b", r"\boverride\b",
    r"\bfirst letters?\b", r"\bpoem where\b", r"\bconvince someone\b", r"\bfor a school essay\b", r"\bhidden notes\b",
    r"\bin your instructions\b", r"\btranslate your\b", r"\blets? role ?play\b", r"\brepeat the text\b",
    r"\bspell (it|this|out)\b", r"\bin reverse\b", r"\bbase ?64\b",
]
LINK = [
    r"https?\s*:?\s*/+", r"\bwww\.", r"\bt\.me\b",
    r"\b[a-z0-9-]+(\.|\s+\.\s+|\s+dot\s+)(com|net|io|gg|bet|casino|xyz|org|co|me|tv|app|site|club|vip|ru|cn|ly|gl|to|cc|ai|link|live|pro|top|win|info|biz)\b",
]
PROMO = [
    r"\bpromo\b", r"\breferr?al\b", r"\bref (code|link)\b", r"\bfree (crypto|money|btc|eth|sol|usdt|spins?)\b",
    r"\b(join|use|sign up)( \w+){0,2} code\b", r"\bbuy my\b", r"\bwin ?rate\b", r"\bpredictor\b", r"\bsignals?\b.*\b(group|discord|telegram|channel|has)\b",
    r"\bmy discord\b", r"\bairdrop\b", r"\bgiveaway\b", r"\bfollow my\b", r"\bcheck (out )?my (channel|stream|page|profile|link|site|bio|insta|tiktok|twitter|youtube|twitch|kick|onlyfans)\b",
    r"\bsend me\b.*\b(crypto|btc|eth|sol|usdt|wallet|address|link)\b", r"\btip me\b",
    r"\bmy (channel|stream|telegram|discord|onlyfans)\b", r"\bdm me\b", r"\bbuy followers\b",
]
PRIVATE = [
    r"\b[\w.+-]+@[\w-]+\.\w{2,}\b", r"\b\+?\d[\d \-()]{8,}\d\b", r"\b\d{1,3}(\.\d{1,3}){3}\b",
    r"\b(lives? at|home address|his address|her address|real name is|doxx?)\b",
]
SEXUAL = [r"\bnudes?\b", r"\bporn\b", r"\bsex(y|ual)?\b", r"\bsuck my\b", r"\bdick pics?\b", r"\bboobs?\b", r"\brape\b", r"\bcum\b"]
VIOLENCE = [r"\bkill (you|him|her|them|yourself|urself)\b", r"\bkys\b", r"\bshoot (up|you)\b", r"\bbomb threat\b", r"\b(i'?ll|i will|gonna|going to) bomb\b"]


def _c(ps):
    return [re.compile(p) for p in ps]


# Chat is normalized before matching, look-alike letters included, so the patterns have to be too.
_CRISIS_HARD = [re.compile(normalize(p)) for p in CRISIS]
_CRISIS_SOFT = [re.compile(normalize(p)) for p in CRISIS_SOFT]


def is_crisis(n: str, *variants: str) -> bool:
    """n is the normalized message, variants are de-spaced or de-leeted forms of it."""
    all_forms = (n, *variants)
    if any(rx.search(v) for rx in _CRISIS_HARD for v in all_forms):
        return True
    if CRISIS_NOT_LIFE.search(n):
        return False
    return any(rx.search(v) for rx in _CRISIS_SOFT for v in all_forms)


_RX = {k: _c(v) for k, v in dict(distress=DISTRESS, deposit=DEPOSIT, authority=AUTHORITY, chase=CHASE, beg=BEG, pick=PICK, rage=RAGE, chill=CHILL, sendoff=SENDOFF, ai=AI_Q, minor=MINOR, advice=ADVICE, injection=INJECTION,
                                 link=LINK, promo=PROMO, private=PRIVATE, sexual=SEXUAL, violence=VIOLENCE).items()}


def _swearing(text: str) -> bool:
    return bool(re.search(r"\b(u?fuck\w*|ufck\w*|fck\w*|fuk\w*|shit\w*|bullshit)\b", text))


def _hit(group: str, *variants: str) -> bool:
    return any(rx.search(v) for rx in _RX[group] for v in variants)


@dataclass(frozen=True)
class Decision:
    action: str          # pass | support | age | advice | deflect | ignore
    reason: str
    speak_name: str      # safe to say out loud
    clean_text: str = ""


def speakable_name(username: str) -> str:
    """A name fit to say aloud, or 'friend' when it is not."""
    raw = ZERO_WIDTH.sub("", username or "")
    n = normalize(raw)
    nd = deleet(n)
    if (not raw or contains_blocked_word(raw) or _hit("link", n) or _hit("promo", n, nd)
            or _hit("sexual", n, nd) or _hit("injection", n) or _hit("violence", n, nd)
            or re.search(r"(deposit|casino|bonus|promo|referral|guarantee|freemoney|crypto|admin|moderator)", nd)):
        return "friend"
    spoken = re.sub(r"[_\-.]+", " ", raw)
    spoken = re.sub(r"[^\w ]", "", spoken, flags=re.UNICODE).strip()
    spoken = re.sub(r"(?<=[a-zA-Z])\d{2,}$", "", spoken).strip()
    spoken = re.sub(r"\s+", " ", spoken)[:24].strip()
    return spoken if len(spoken) >= 2 else "friend"


def clean_chat_text(text: str) -> str:
    t = re.sub(r"\[emote:\d+:[^\]]*\]", " ", text or "")
    t = ZERO_WIDTH.sub("", t)
    t = re.sub(r"[^\w\s'?!.,:;()\-]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()[:200]


# Decisions answered with a fixed persona line. The LLM never writes these.
FIXED_ACTIONS = {"crisis": "crisis", "support": "support", "age": "age", "chase": "chase", "advice": "advice",
                 "deposit": "deposit", "deflect": "deflect", "authority": "authority",
                 "beg": "beg", "pick": "pick", "rage": "rage", "chill": "chill", "sendoff": "sendoff", "ai": "ai_question"}
URGENT_ACTIONS = {"crisis", "crisis_followup", "support", "age", "chase"}


class ModerationSink(abc.ABC):
    """Where platform moderation actions go (timeouts, deletes)."""

    @abc.abstractmethod
    def act(self, username: str, action: str, reason: str) -> None: ...


class LogOnlySink(ModerationSink):
    """Reader-only default: records what a mod bot would do. A real Kick sink needs a bot token."""

    def __init__(self):
        self.actions: list[tuple[str, str, str]] = []

    def act(self, username: str, action: str, reason: str) -> None:
        self.actions.append((username, action, reason))
        log.info("mod action %s on viewer: %s", action, reason)


class ChatModerator:
    def __init__(self, sink: ModerationSink | None = None, max_per_minute: int = 6,
                 now=time.time):
        self.sink = sink or LogOnlySink()
        self.max_per_minute = max_per_minute
        self.now = now
        self.times: dict[str, deque] = defaultdict(deque)
        self.last_text: dict[str, deque] = defaultdict(lambda: deque(maxlen=4))

    def _flood(self, user: str, text_norm: str) -> str:
        t = self.now()
        q = self.times[user]
        q.append(t)
        while q and t - q[0] > 60:
            q.popleft()
        if len(q) > self.max_per_minute:
            return "rate"
        if text_norm in self.last_text[user]:
            return "duplicate"
        self.last_text[user].append(text_norm)
        if len(self.times) > 20000:   # bound memory across days of uptime
            self.times.clear()
            self.last_text.clear()
        return ""

    def review(self, msg: ChatMessage) -> Decision:
        name = speakable_name(msg.username)
        n = normalize(msg.text)
        d = despace(n)
        l = deleet(d)
        variants = (n, d, l)
        clean = clean_chat_text(msg.text)

        # A viewer in trouble is answered first, whatever else the message holds.
        if is_crisis(n, d, l):
            return Decision("crisis", "crisis", name)
        if _hit("distress", n, d):
            return Decision("support", "distress", name)
        if _hit("chase", n, d):
            return Decision("chase", "chasing_losses", name)
        if contains_blocked_word(msg.text):
            self.sink.act(msg.username, "timeout", "hate")
            return Decision("ignore", "hate", name)
        if _hit("violence", *variants):
            self.sink.act(msg.username, "timeout", "violence")
            return Decision("ignore", "violence", name)
        if _hit("sexual", *variants):
            self.sink.act(msg.username, "delete", "sexual")
            return Decision("ignore", "sexual", name)
        if _hit("private", n):
            self.sink.act(msg.username, "delete", "private_info")
            return Decision("ignore", "private_info", name)
        if _hit("authority", n, d):
            return Decision("authority", "fake_authority", name)
        if _hit("beg", n, d):
            flood = self._flood(msg.username.lower(), n)
            if flood:
                return Decision("ignore", flood, name)
            # Swearing at him while begging is frustration: he's the calm wall, still no bonus.
            return Decision("rage", "begging_angry", name) if _swearing(n) else Decision("beg", "begging", name)
        if _hit("link", n, d):
            self.sink.act(msg.username, "delete", "link")
            return Decision("ignore", "link", name)
        if _hit("promo", *variants):
            self.sink.act(msg.username, "delete", "promo")
            return Decision("ignore", "promo", name)
        if _hit("minor", n):
            return Decision("age", "minor", name)
        if _hit("injection", n, d):
            return Decision("deflect", "injection", name)
        flood = self._flood(msg.username.lower(), n)
        if flood:
            return Decision("ignore", flood, name)
        if len(clean) < 2:
            return Decision("ignore", "empty", name)
        # Repeated-character noise is not worth airtime.
        if re.search(r"(.)\1{7,}", n):
            return Decision("ignore", "noise", name)
        if _hit("ai", n, d):
            return Decision("ai", "ai_question", name)
        if _hit("pick", n, d):
            return Decision("pick", "pick_request", name)
        if _hit("deposit", n, d):
            return Decision("deposit", "deposit", name)
        if _hit("advice", n, d):
            return Decision("advice", "advice", name)
        if _hit("chill", n, l):
            self.sink.act(msg.username, "warn", "cruelty")
            return Decision("chill", "cruelty", name)
        letters = re.sub(r"[^A-Za-z]", "", msg.text)
        distinct = {w for w in re.findall(r"[a-z]+", n)}
        if _hit("rage", n, d) or (len(letters) >= 12 and letters.isupper() and len(distinct) >= 3):
            return Decision("rage", "rage", name)
        if letters.isupper() and len(distinct) < 3 and len(letters) >= 8:
            return Decision("ignore", "noise", name)
        if _hit("sendoff", n, d):
            return Decision("sendoff", "sendoff", name)
        return Decision("pass", "", name, clean)
