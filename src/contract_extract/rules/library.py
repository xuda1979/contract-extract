"""Clause rule library.

Each rule is a callable (doc, sentences) -> list[Match].  Rules are pure
string-pattern based so the offline layer needs no models.  A `Match` carries
rule name, page (1-based), sentence, risk (LOW/MEDIUM/HIGH) and a flag.

Design notes:
- Every rule must avoid firing on *negated* statements (e.g. "No confidentiality
  obligation applies", "non-exclusive").  Negation probes are explicit per rule.
- Amounts/dates are extracted with currency-aware patterns so bare numbers
  ("90", "12") are never treated as financial amounts.
"""

from __future__ import annotations

import re

from contract_extract.rules.match import Match
from contract_extract.rules import utils
from contract_extract.schema import RiskLevel

# ---------------------------------------------------------------------------
# shared patterns
# ---------------------------------------------------------------------------

_CURR = r"(?:US\$|USD|EUR|GBP|AUD|CAD|CNY|RMB|SGD|HKD|JPY|\$|€|£|¥|元)"
_NUMTOKEN = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
AMOUNT = (
    rf"{_CURR}\s*{_NUMTOKEN}"
    rf"|{_NUMTOKEN}\s*(?:US\$|USD|EUR|GBP|AUD|CAD|CNY|RMB|SGD|HKD|JPY|元|人民币|dollars|euros|pounds|yuan)"
)
_AMOUNT_RE = re.compile(rf"\b(?:{AMOUNT})\b(?!\s*%)")

_DURATION_UNIT = r"(?:year|month|week|day|quarter|annual|yearly|monthly|weekly)"
_DURATION_RE = re.compile(
    rf"(?P<num>{_NUMTOKEN})\s*\-?\s*(?P<unit>{_DURATION_UNIT})s?\b",
    re.IGNORECASE,
)
# Duration pattern 1: "term of 2 years", "initial term is 5 years", "duration of 6 months"
_DUR_PAT1 = re.compile(
    rf"\b(?:initial\s+)?(?:term|duration|period|lease|sponsorship|employment|loan)\s+"
    rf"(?:of|is|shall\s+be|will\s+be|commences?)?\s*"
    rf"(?:for\s+|a\s+period\s+of\s+)?(?P<num>{_NUMTOKEN})\s*(?P<unit>{_DURATION_UNIT})s?\b",
    re.IGNORECASE,
)
# Duration pattern 4: "for 2 years", "for a period of 5 years" with agreement context
_DUR_PAT4 = re.compile(
    rf"\bfor\s+(?:a\s+period\s+of\s+)?(?:the\s+)?(?:initial\s+|first\s+)?"
    rf"(?P<num>{_NUMTOKEN})\s*(?P<unit>{_DURATION_UNIT})s?\b",
    re.IGNORECASE,
)
# Duration pattern 5: "term of employment is 2 years", "term of this Agreement is 5 years"
_DUR_PAT5 = re.compile(
    rf"\b(?:term|duration|period)\s+of\s+(?:the\s+)?[a-z\u4e00-\u9fff]+(?:\s+[a-z\u4e00-\u9fff]+)?"
    rf"\s+(?:is|shall\s+be|will\s+be)\s+(?P<num>{_NUMTOKEN})\s*(?P<unit>{_DURATION_UNIT})s?\b",
    re.IGNORECASE,
)
_DUR_CONTEXT = (
    "term",
    "duration",
    "period",
    "agreement",
    "contract",
    "lease",
    "engagement",
    "joint venture",
    "venture",
    "sponsorship",
    "employment",
    "appointment",
    "license",
    "licence",
    "subscription",
    "loan",
    "arrangement",
    "facility",
)
_DUR_SURVIVAL = ("surviv", "after termination", "following termination", "post-termination",
                 "after the termination", "non-compete", "not compete", "following the termination")
# Illustrative/hypothetical lead-ins: "for example, a period of 3 weeks may
# be reasonable" is not a contract term.  Zero fixture files use these
# phrases (checked against fixtures/generate_fixtures.py), so the guard is
# safe against the AC1/AC2 recall baseline.
_DUR_ILLUSTRATIVE_RE = re.compile(
    r"\b(?:for\s+example|for\s+instance|such\s+as|by\s+way\s+of\s+example|e\.g\.|i\.e\.)\b",
    re.IGNORECASE,
)

_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE_RE = re.compile(
    rf"\b(?:{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?[\s,]+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?[\s,]+\d{{4}}"
    rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"
    rf"|\d{{1,2}}-\d{{1,2}}-\d{{4}}"
    rf"|\d{{4}}-\d{{1,2}}-\d{{1,2}})\b",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*%")

# Bare "N% of <money kind>" (fees / invoices / sales / price / ...) is a share
# of money — a cap, buy-back rate, retention or late-payment rate — not the
# price itself, so the price rule must not fire on it.  A genuine price
# percent ("30% off the list price", "price drops by 10%") never puts "of"
# right after the number.
_PCT_OF_MONEY_RE = re.compile(
    rf"{_PERCENT_RE.pattern}\s*of\s+(?:the\s+)?[a-z\s]{{0,24}}?"
    rf"\b(?:fees?|invoices?|sales|revenue|consideration|costs?|prices?|values?|amounts?)\b",
    re.IGNORECASE,
)

_UNIT_TO_YEARS = {
    "year": 1, "annual": 1, "yearly": 1,
    "month": 1 / 12, "monthly": 1 / 12,
    "week": 1 / 52, "weekly": 1 / 52,
    "quarter": 1 / 4, "day": 1 / 365,
}


def _page_of(doc, sentence: str) -> int:
    for page in doc.pages:
        if sentence in page.text:
            return page.page_number
    best, best_sc = 1, -1
    words = sentence.split()
    for page in doc.pages:
        n = sum(1 for w in words if w in page.text)
        if n > best_sc:
            best, best_sc = page.page_number, n
    return best


def _agg(risk: RiskLevel) -> RiskLevel:
    return risk


# ---------------------------------------------------------------------------
# 1  duration
# ---------------------------------------------------------------------------


def rule_duration(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if any(k in lower for k in _DUR_SURVIVAL):
            continue
        # "The consultant agrees not to provide services to clients of the
        # Company for 2 years after this agreement" (fixture line 315) is a
        # post-term restrictive covenant (non-compete / non-solicit), not the
        # agreement's term.  The exclusivity rule still fires on it (that
        # firing is expected); the duration firing is the false positive.
        # The guard requires a covenant verb bound to a negation in the same
        # sentence plus a length, so a genuine "term of this Agreement shall
        # be for a period of 2 years" (fixture line 40) still fires.
        if (
            re.search(
                r"\bnot\b[^.]{0,40}\b(?:provide\s+(?:any\s+)?services?|compet\w*|"
                r"solicit\w*|canvass\w*|poach\w*|recruit\w*|entice\w*|induce\w*)\b",
                lower,
            )
            and re.search(
                r"\b\d+\s*(?:year\w*|month\w*|week\w*|quarter\w*|day\w*)\b",
                lower,
            )
        ):
            continue
        # "The renewal term shall be 12 months" (fixture line 164) states the
        # length of a renewal period — already captured by the renewal rule
        # (that firing is expected); the duration firing double-counts the
        # span as the agreement's term and is the false positive.  Zero
        # fixture lines pair "renewal term" with an initial-term meaning
        # (checked against fixtures/generate_fixtures.py); "the term of this
        # Agreement shall be for a period of 2 years" has no renewal noun.
        if re.search(
            r"\brenewal\s+term\b[^.]{0,35}"
            r"\b\d+\s*(?:year\w*|month\w*|week\w*|day\w*)\b",
            lower,
        ):
            continue
        if _DUR_ILLUSTRATIVE_RE.search(sentence):
            # "For example, a period of 3 weeks may be reasonable" is
            # illustrating, not a binding term.
            continue
        # "The term of the court order / the duration of the injunction" is a
        # legal instrument's effective period, not a commercial contract term.
        if re.search(
            r"\b(?:term|duration|period)\s+of\s+(?:the\s+)?"
            r"(?:court\s+order|injunction|judg\w+|award|subpoena|warrant|ruling|decree|"
            r"statute|legislation|regulation|ordinance|directive|senten\w+|probation|ban\b|"
            r"restraining\s+order|protective\s+order|litigation|arbitration)\b",
            lower,
        ):
            continue
        # "The court imposed a prison term of 5 years" / "a jail term of 3
        # years" is a criminal-sentence length, not the agreement's term.
        # Zero fixture lines mention prison/jail/imprison (checked against
        # fixtures/generate_fixtures.py); a genuine "term of the Agreement"
        # still fires.
        if re.search(
            r"\b(?:prison|jail|imprison(?:ment)?)\b[^.]{0,40}"
            r"\b(?:term|sentence|period|duration)\b"
            r"|\b(?:term|sentence|period|duration)\b[^.]{0,40}"
            r"\b(?:prison|jail|imprison(?:ment)?)\b",
            lower,
        ):
            continue
        # "The duration of copyright protection shall be 70 years after the
        # author's death" is a statutory IP-protection term (fixed by law, not
        # negotiated) — extending the instrument guard above to statutory
        # protection families.  A genuine negotiated licence / agreement term
        # ("the term of the Licence shall be 3 years") has no protection
        # noun and still fires.
        if re.search(
            r"\b(?:term|duration|period)\s+of\b[^.]{0,25}"
            r"\b(?:copyright|patent\w*|trademark|industrial\s+design)\s+protection\b",
            lower,
        ):
            continue
        # "The warranty period is 12 months from delivery", "the standstill
        # period of 60 days" are coverage or procedural windows, not
        # agreement terms.  Zero fixture files use these phrases (checked
        # against fixtures/generate_fixtures.py).  A "transition / notice /
        # limitation period" can itself be a substantive term, so the guard
        # stays narrow to the two validated families.
        if re.search(r"\b(?:warranty|standstill)\s+period\b", lower):
            continue
        # "The supplier warrants the product to be free from defects for a
        # period of 2 years" is a product coverage statement (a sales warranty
        # describing the merchantability window), not the agreement's term.
        # The frame requires the warrant verb bound to a coverage/fitness
        # phrase ("free from defects", "satisfactory quality") or a "for a
        # period of N" window; a genuine term sentence ("the initial term of
        # this Agreement is 7 years from the effective date") has no warrant
        # verb and still fires.  Zero fixture lines bind a warrant to a
        # period or a defects frame (fixture line 62 "disclaims all
        # warranties" has no period; checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bwarrant\w*\b[^.]{0,70}"
            r"\b(?:free\s+from\s+defects?\b|satisfactory\s+(?:quality|condition)\b|"
            r"merchantab\w*\b|for\s+(?:a\s+)?(?:period|term)\s+of\b)",
            lower,
        ):
            continue
        # "negotiate in good faith for a period of 30 days before mediation",
        # "the period for objections shall be 14 days" are procedural
        # windows, not the agreement's term.  Only "negotiate in good faith"
        # with a length follows; fixture line 365 ("negotiate in good faith
        # on any extension", no period) does not match.
        if re.search(
            r"\b(a\s+|the\s+)?period\s+(?:of|for)\b[^.]{0,40}"
            r"\b(?:negotiat\w*|objection\w*)\b"
            r"|\bnegotiat\w*\s+in\s+good\s+faith\b[^.]{0,40}\b(?:for\s+|for\s+a\s+period\s+of\s+)",
            lower,
        ):
            continue
        # "The course is offered for a period of 8 weeks each semester" is an
        # educational-program window (course / semester / curriculum), not the
        # agreement's term.  Zero fixture lines mention these (checked against
        # fixtures/generate_fixtures.py); a "term of the License" still fires.
        if re.search(
            r"\b(?:period|term|duration)\b[^.]{0,50}"
            r"\b(?:course|semester|curriculum|degree|academic\s+(?:year|term)|"
            r"school(?:year)?|trimester|quarter\s+of\s+studies?)\b"
            r"|\b(?:course|semester|curriculum|degree|academic\s+(?:year|term)|"
            r"school(?:year)?|trimester)\b[^.]{0,50}\b(?:period|term|duration)\b",
            lower,
        ):
            continue
        # "The project spans a period of 12 months" states the timeline of a
        # project or activity (a project-scope window), not the agreement's
        # term.  Zero fixture lines combine "project" with a period/term/
        # duration (fixture "project" lines 187/312 are exclusivity and
        # payment-instalment language), so the guard is safe — a genuine
        # "term of the Agreement" has no project noun.
        if re.search(
            r"\b(?:project|programme?|initiative\w*|undertaking\w*)\b"
            r"[^.]{0,45}\b(?:span\w*|last\w*|run\w*|continu\w*)\b"
            r"[^.]{0,40}\b(?:period|term|duration)\b"
            r"|\b(?:project|programme?|initiative\w*|undertaking\w*)\b"
            r"[^.]{0,45}\b(?:period\s+of\b|term\b|duration\b)\b[^.]{0,35}"
            r"\b\d+\s*(?:year\w*|month\w*|week\w*|day\w*|quarter\w*)\b",
            lower,
        ):
            continue
        # "The cooling-off period shall be 14 days from the date of the
        # order" is a statutory consumer-protection window (right to cancel a
        # distance purchase), not the agreement's term.
        if re.search(r"\bcooling[- ]?off\s+period\b", lower):
            continue
        # "Fixed term deposits shall be offered for a minimum period of 6
        # months" and "The bank guarantee shall remain in force for a period
        # of 12 months" describe the lifetime of a banking / financial
        # product (deposit, bank guarantee), not the agreement's term.
        # Genuine agreement terms ("the term of this Agreement shall be for
        # a period of 2 years", fixture line 40) still fire.
        if re.search(
            r"\bfixed[- ]?term\s+deposits?\b[^.]{0,40}\b(?:period|term)\b"
            r"|\bbank\s+guarantee\b[^.]{0,45}"
            r"\b(?:period|term|in\s+force)\b"
            r"|\b(?:period|term|in\s+force)\b[^.]{0,45}"
            r"\bbank\s+guarantee\b",
            lower,
        ):
            continue
        # "The probation period of the employee shall be 3 months" is an HR
        # trial window (probation review period), not the agreement's term.
        # A genuine "term of the Licence" still fires.
        if re.search(r"\bprobation(?:ary)?\s+(?:period|term)\b", lower):
            continue
        # "The gestation period of the animal is 6 months" describes a
        # biological development window (pregnancy / maturation), not the
        # agreement's term.  Zero fixture lines use gestation, pregnancy or
        # incubation wording (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:gestation|pregnancy|incubation)\s+(?:period|term)\b",
            lower,
        ):
            continue
        # "The statute of limitations period is 2 years" is the legal window
        # for bringing a claim (fixed by law), not the agreement's term.
        if re.search(
            r"\b(?:statute\s+of\s+limitations|limitations?\s+period|limitation\s+period)\b",
            lower,
        ):
            continue
        # "The Contractor is liable for the repair of defective workmanship
        # for a period of 12 months after practical completion" is a
        # post-completion repair / snagging window (the construction DLP
        # family), not the agreement's term.  A "term of the Lease" still
        # fires.
        # "The interest-free period is 30 days from the invoice date" is a
        # financing / billing arrangement window (no interest accrues within
        # it), not the agreement's term.  Genuine agreement terms ("the term
        # of this Agreement shall be for a period of 2 years") still fire —
        # the guard requires the "interest-free period" compound or an
        # interest-free term in the sentence.
        if re.search(
            r"\binterest[- ]?free\s+period\b"
            r"|\bperiod\s+of\s+(?:interest|finance\w*)[\s-]?free\b"
            r"|\binterest[- ]?free\b[^.]{0,30}\bperiod\b"
            r"|\bperiod\b[^.]{0,30}\binterest[- ]?free\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:workmanship|snag\w*|remedial|rectification)\b[^.]{0,40}"
            r"\b(?:repair\w*|remed\w*|defect\w*)\b"
            r"|\b(?:repair\w*|remed\w*|defect\w*)\b[^.]{0,40}"
            r"\b(?:workmanship|snag\w*|remedial|rectification)\b",
            lower,
        ):
            continue
        # "The notice period is 30 days, unless the parties agree otherwise" is
        # an HR or administrative notice timetable (how long before a step a
        # party must give notice) — not the fixed term of the agreement.
        # "The vesting period for the options is 4 years" is an equity-grant
        # vesting schedule — not the term of the contract.  Genuine agreement
        # terms ("the term of this Agreement shall be for a period of 2
        # years") still fire.  Zero fixture lines use "notice period" or
        # "vesting period" (checked against fixtures/generate_fixtures.py;
        # the fixture vesting lines use "vest in" — fixture lines 65/185).
        if re.search(
            r"\bnotice\s+period\b"
            r"|\bperiod\s+of\s+notice\b"
            r"|\bvesting\s+period\b"
            r"|\bperiod\s+of\s+vesting\b",
            lower,
        ):
            continue
        # "The transitional period shall be 12 months from the effective date"
        # and "The trial period of the software license is 30 days" are
        # migration / evaluation windows for a rollout or license trial, not
        # the fixed term of the agreement.  Genuine agreement terms ("the term
        # of this Agreement shall be for a period of 2 years") still fire.
        # Zero fixture lines use "transitional period" or "trial period"
        # (checked against fixtures/generate_fixtures.py).
        if re.search(r"\btransitional\s+period\b|\btrial\s+period\b", lower):
            continue
        # "The term of the trademark registration is 10 years", "the
        # registration certificate is valid for 5 years", "the building permit
        # expires after 3 years" and "the warranty card is valid for 24
        # months" state the lifetime of a statutory or administrative
        # registration / certificate (issued by a registry or authority), not
        # the negotiated term of the agreement.  Genuine agreement terms
        # ("the term of this Agreement shall be for a period of 2 years",
        # fixture line 40) still fire — the guard requires a registration /
        # permit / certificate / card noun beside a term or validity marker.
        # Zero fixture lines use these noun families (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:term|period|duration)\s+of\b[^.]{0,30}"
            r"\b(?:trademark\s+registration|registration\b|permit\b|certificate\w*|"
            r"licen[cs]e\b|patent\b)\b[^.]{0,40}\b(?:valid|expires?|period|term)\b"
            r"|\bregistration\s+certificate\b[^.]{0,40}\b(?:valid|expir\w*)\b"
            r"|\b(?:building\s+permit|warranty\s+card)\b[^.]{0,40}\b(?:valid|expires?)\b"
            r"|\b(?:term|period|duration)\s+of\b[^.]{0,30}"
            r"\b(?:trademark\s+registration|registration\b|permit\b|certificate\w*|"
            r"warranty\s+card\b)\b[^.]{0,35}"
            r"\b(?:is\b|are\b|shall\s+run\s+for\b|runs?\s+for\b)\b[^.]{0,15}\b\d+\s+"
            r"(?:year\w*|month\w*|day\w*)\b",
            lower,
        ):
            continue
        # "Planning permission is valid for a period of 5 years" and "The
        # transport permit is valid for a period of 2 years" state the
        # effective lifetime of a planning approval or transport credential
        # issued by an authority, not the negotiated term of the agreement.
        # Genuine agreement terms still fire — the guard requires the
        # administrative-permission noun bound to a "valid for" validity
        # marker (there is no "valid for a period of" fixture line).
        if re.search(
            r"\b(?:planning\s+permission|transport\s+permit|planning\s+approval)\b"
            r"[^.]{0,35}\bvalid\s+for\b"
            r"|\bvalid\s+for\b[^.]{0,35}"
            r"\b(?:planning\s+permission|transport\s+permit|planning\s+approval)\b",
            lower,
        ):
            continue
        # "The electoral term of the committee is 4 years" is the tenure of a
        # governing body (an elected office), not the agreement's term.
        # Genuine "term of the Agreement" sentences have no electoral noun.
        if re.search(
            r"\belectoral\b[^.]{0,45}\bterm\b"
            r"|\bterm\b[^.]{0,45}\belectoral\b",
            lower,
        ):
            continue
        # "She served a term of 4 years as chair of the committee." is the
        # tenure of an elected office (how long someone held a governance
        # post), not the term of an agreement.  The biographical guard above
        # requires the "for a period of" frame (absent here) and the electoral
        # guard requires the word "electoral" (absent too), so a separate
        # guard is needed.  Genuine agreement terms ("The term of this
        # Agreement shall be 2 years" — fixture line 40; "The Services
        # Agreement has a term of 12 months" — fixture line 358) carry no
        # served-in-office / governance-role frame and still fire.  Zero
        # fixture lines pair a service verb with a term of years and an office
        # noun (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:served|serving|serve)\s+(?:a|the)\s+term\s+of\b[^.]{0,35}"
            r"\b\d+\s+years?\b[^.]{0,30}\b(?:as|of)\b[^.]{0,25}"
            r"\b(?:chairs?\b|presidents?\b|directors?\b|governors?\b|"
            r"officers?\b|secretary\w*\b|treasur\w*\b|board\b|committee\w*)\b",
            lower,
        ):
            continue
        # "The loan term is 5 years with a fixed rate" describes a banking /
        # mortgage product (a loan priced at a fixed interest rate), not the
        # agreement's term.  The guard requires the fixed-rate marker so a
        # genuine "The loan term is 10 years." (fixture line 234, no banking
        # marker) still fires as a term.
        if re.search(
            r"\bloan\s+term\b[^.]{0,40}\bfixed[- ]?rate\b"
            r"|\bfixed[- ]?rate\b[^.]{0,40}\bloan\s+term\b",
            lower,
        ):
            continue
        # "The protection period of the database right is 15 years" is the
        # statutory lifetime of a sui-generis database right (fixed by the
        # Database Directive), not a negotiated agreement term.  The
        # statutory-IP guard above covers copyright/patent/trademark
        # "protection"; the database-right frame has "protection" before the
        # noun, so a separate guard is required.  Zero fixture lines mention
        # database rights (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:term|period|duration|protection\s+period)\s+of\b[^.]{0,30}"
            r"\b(?:database\s+right|sui[- ]generis)\b"
            r"|\b(?:database\s+right|sui[- ]generis)\b[^.]{0,30}"
            r"\b(?:term|period|duration|protection\s+period)\b",
            lower,
        ):
            continue
        # "All business records shall be retained for a period of 7 years" is
        # a recordkeeping/retention duty (statutory records-keeping), not a
        # negotiated agreement term: the retained subject is a record, not the
        # Agreement/rights under it.  Genuine durations ("The term of this
        # Agreement shall be for a period of 2 years" — fixture line 40;
        # "This Agreement shall remain in effect for a period of 3 years")
        # never bind "records ... retained/kept" objects, so they still fire.
        # Zero fixture lines combine records with retain/keep + a period
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\brecord\w*\b[^.]{0,40}\b(?:retain\w*|keep\w*|maintain\w*|stor\w*)\b"
            r"[^.]{0,40}\bfor\s+(?:a\s+)?period\s+of\b",
            lower,
        ):
            continue
        # "The data retention period is 3 years" / "The record retention period
        # is 7 years" state a recordkeeping / data-retention window (a
        # statutory or corporate retention policy), not the negotiated term of
        # the agreement.  The recordkeeping guard above requires
        # "records ... retained/kept ... for a period of" (verb form); the
        # "retention period" compound (noun form) needs its own guard.
        # Genuine durations ("The term of this Agreement shall be for a period
        # of 2 years" — fixture line 40) never bind the "retention period"
        # compound, so they still fire.  Zero fixture lines use "retention
        # period" (fixture line 181 "Retention of 10% of each invoice" and
        # line 220 "retention bonus" are not the compound; checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:data|record\w*|document\w*|information)\s+retention\s+period\b",
            lower,
        ):
            continue
        # "The ban lasts for a period of 10 years" is the effective window of
        # a prohibition or sanction imposed by a rule (a ban's duration), not
        # the term of an agreement.  The legal-instrument guard above catches
        # "term/duration/period of the ban" (ban as object); this guard covers
        # ban as subject with a following period/term/duration.  Genuine
        # agreement terms ("the term of this Agreement shall be for a period
        # of 2 years" — fixture line 40) carry no ban noun and still fire.
        # Zero fixture lines mention bans (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bbans?\b[^.]{0,45}\b(?:period|term|duration)\b"
            r"|\b(?:period|term|duration)\b[^.]{0,45}\bbans?\b",
            lower,
        ):
            continue
        # "The lock-in period is 2 years" is a market / contractual lock-in
        # window (a customer or supplier tied-in period), not the term of the
        # agreement itself.  Genuine agreement terms carry no "lock-in" noun
        # and still fire.  Zero fixture lines use "lock-in" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(r"\block[- ]?in\s+period\b", lower):
            continue
        # "She taught piano for a period of 20 years before retiring" is a
        # biographical/life-history statement, not an agreement term: the
        # duration belongs to a past personal activity, not to any agreement
        # or right.  Genuine term lengths ("term of this Agreement ... for a
        # period of 2 years" — fixture line 40; "shall remain in force for a
        # period of 3 years") carry no past-tense activity verbs, so they
        # still fire.  Zero fixture lines pair an activity verb with a period
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:taught|worked|studied|practi[cs]\w*|trained|coached|resided|"
            r"lived|served|performed|competed)\b[^.]{0,40}"
            r"\bfor\s+(?:a\s+)?period\s+of\b",
            lower,
        ):
            continue
        m = _DUR_PAT1.search(sentence) or _DUR_PAT4.search(sentence) or _DUR_PAT5.search(sentence)
        if not m:
            continue
        if not (_DUR_PAT1.search(sentence) or _DUR_PAT5.search(sentence)):
            # "for N unit" alone needs agreement-context to be a term.
            if not any(k in lower for k in _DUR_CONTEXT):
                continue
        n = float(m.group("num"))
        unit = m.group("unit").lower()
        years = n * _UNIT_TO_YEARS.get(unit, 1)
        flag = f"term {m.group('num')} {m.group('unit')}(s)"
        risk = RiskLevel.LOW
        if years > 5:
            risk = RiskLevel.HIGH
            flag += " (long fix-term >5y)"
        elif years >= 2:
            risk = RiskLevel.MEDIUM
        out.append(Match("duration", page, sentence, risk, flag, None))
    return out


# ---------------------------------------------------------------------------
# 2  quantity (length/quantity)
# ---------------------------------------------------------------------------


def rule_quantity(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in ("total of", "quantity", "quantities of",
                                        " equal to ", "consists of", " units",
                                        " units ", " items", " copies ", " events",
                                        " pages", " documents", " sets of")):
            continue
        # Procedural time periods ("a total of 5 business days to respond") are
        # deadlines, not a quantity of goods.  Countable deliverables ("a total
        # of 10,000 units", "a total of 12 events") still fire.
        if re.search(
            r"\btotal\s+of\s+\d+(?:[.,]\d+)?\s*(?:business|working|calendar)?\s*"
            r"(?:days?|weeks?|months?|years?)\b",
            lower,
        ):
            continue
        # "Quantity discounts of 5% apply for orders above 10,000 units" is a
        # pricing threshold for discounts, not a committed quantity.  A
        # purchase commitment ("Buyer shall purchase 10,000 units") still fires.
        if re.search(
            r"\bquantity\s+discount\w*\b"
            r"|\bdiscount\w*\b[^.]{0,30}\borders?\s+(?:above|exceeding|over|greater\s+than)\b"
            r"|\borders?\s+(?:above|exceeding|over|greater\s+than)\b[^.]{0,30}\bdiscount\w*\b",
            lower,
        ):
            continue
        # "distributed in 5 copies to the committee members" counts copies of
        # a document produced, not a quantity of goods purchased.  A genuine
        # count ("a total of 5 documents were exchanged") fired here before
        # Round 2's daily-doc guard — copy counts of physical delivery in a
        # distribution setting are procedural.
        if re.search(
            r"\bdistribut\w*\b[^.]{0,40}\b\d+(?:[.,]\d+)?\s+copies?\b"
            r"|\b\d+(?:[.,]\d+)?\s+copies?\b[^.]{0,40}\bdistribut\w*\b",
            lower,
        ):
            continue
        # "The report shall not exceed 5 pages" / "not be more than 500 pages
        # in length" is a document-length limit for a deliverable, not a
        # quantity of goods.  A genuine committed count ("a total of 10,000
        # units", "12 events") still fires — the guard requires a page/length
        # noun bound to a shall-not-exceed / no-more-than cap or a page-name
        # helper.  Zero fixture lines use page caps (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bshall\s+not\s+exceed\b[^.]{0,25}\b\d+(?:[.,]\d+)?\s+pages?\b"
            r"|\bno\s+more\s+than\b[^.]{0,25}\b\d+(?:[.,]\d+)?\s+pages?\b"
            r"|\bnot\s+(?:be\s+)?more\s+than\b[^.]{0,25}\b\d+(?:[.,]\d+)?\s+pages?\b"
            r"|\b\d+(?:[.,]\d+)?\s+pages?\s+in\s+length\b",
            lower,
        ):
            continue
        # "The batch size is 100 units" / "the lot size is 50 units" state a
        # production/sampling lot definition (a descriptive measure of a
        # batch), not a committed quantity of goods.  The noun "size" ties
        # the number to a specification, not to an order commitment.  Genuine
        # quantities ("Buyer shall purchase 10,000 units") still fire; zero
        # fixture lines use "batch/lot/sample size" with a unit count
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:batch|lot|sample|parcel|consignment)\s+size\b"
            r"[^.]{0,25}\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\b"
            r"[^.]{0,25}\b(?:batch|lot|sample|parcel|consignment)\s+size\b",
            lower,
        ):
            continue
        # "The document is 30 pages long" is a description of a deliverable's
        # length (a specification, not a count of goods).  The existing page
        # guard covers "in length" / cap forms; this adds the bare "pages
        # long" adjective form.  Zero fixture lines join a page count to
        # "long".
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+pages?\s+long\b",
            lower,
        ):
            continue
        # "The novel has 300 pages" is a description of a document's length
        # (an editorial / content fact), not a quantity of goods or services
        # committed under the agreement.  The guard pins a content-document
        # noun next to the page count; genuine committed goods quantities
        # ("10,000 units", "12 events") never count pages.  Zero fixture
        # lines count document pages (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:novel\w*|book\w*|documents?\b|manual\w*|report\w*|"
            r"thesis|draft\w*|article\w*|chapter\w*|letter\w*|essay\w*|"
            r"story|manuscript\w*|paper\w*|handbook\w*|guide\w*)\b"
            r"[^.]{0,25}\b\d+(?:[.,]\d+)?\s+pages?\b"
            r"|\b\d+(?:[.,]\d+)?\s+pages?\b[^.]{0,25}"
            r"\b(?:novel\w*|book\w*|documents?\b|manual\w*|report\w*|"
            r"thesis|draft\w*|article\w*|chapter\w*|letter\w*|essay\w*|"
            r"story|manuscript\w*|paper\w*|handbook\w*|guide\w*)\b",
            lower,
        ):
            continue
        # "The auditors filed 12 copies of the report with the court" /
        # "The application was submitted in 8 copies to the registry" count
        # copies of a document filed / lodged with an institution — a
        # procedural filing, not a quantity of goods.  "Three counterparts of
        # the agreement were executed" counts execution counterparts (duplicate
        # originals), likewise not goods.  Genuine committed goods quantities
        # ("a total of 10,000 units", "12 events") still fire — the guard
        # requires an institutional / filing noun or the "counterpart" noun
        # near the count.
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+copies?\b[^.]{0,45}"
            r"\b(?:court\w*|tribunal\w*|registry|registrar|authority|"
            r"government\w*|agency|filed?|lodg\w*|submitted?|stamp\w*)\b"
            r"|\b(?:court\w*|tribunal\w*|registry|registrar|authority|"
            r"government\w*|agency|filed?|lodg\w*|submitted?|stamp\w*)\b"
            r"[^.]{0,45}\b\d+(?:[.,]\d+)?\s+copies?\b"
            r"|\b\d+(?:[.,]\d+)?\s+counterparts?\b[^.]{0,30}"
            r"\b(?:agreement|contract|deed|instrument)\b"
            r"|\b(?:agreement|contract|deed|instrument)\b[^.]{0,30}"
            r"\b\d+(?:[.,]\d+)?\s+counterparts?\b",
            lower,
        ):
            continue
        # "The customer returned 3 items to the store" / "The report contains
        # 12 items on the agenda" count return batches, agenda entries or
        # inspection findings — not contractual quantities of goods, units or
        # copies.  The guard is pinned to the non-commercial noun contexts
        # (returned / agenda / meeting / audit / defect / checklist), so a
        # genuine "deliver 500 items" procurement quantity still fires.  Zero
        # fixture lines pair a number with items in these contexts (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+items?\b[^.]{0,40}"
            r"\b(?:return(?:ed|s|ing)?|agenda|meeting\w*|audit\b|defect\w*|"
            r"inspected?|found?\s+during|checklist|complaint\w*)\b"
            r"|\b(?:return(?:ed|s|ing)?|agenda|meeting\w*|audit\b|defect\w*|"
            r"inspected?|found?\s+during|checklist|complaint\w*)\b"
            r"[^.]{0,40}\b\d+(?:[.,]\d+)?\s+items?\b",
            lower,
        ):
            continue
        # "The batch contains 500 units of the vaccine" counts the contents of
        # a production batch (a lot specification), not a committed quantity of
        # goods.  "The minimum order quantity is 100 units" states a supplier's
        # ordering threshold (MOQ), not a quantity one party commits to
        # purchase.  Genuine purchase commitments ("Buyer shall purchase
        # 10,000 units") still fire — the guard requires the batch-contains
        # frame or the MOQ compound.  Zero fixture lines use these frames
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bbatch\b[^.]{0,30}\bcontain(?:s|ing)?\b[^.]{0,30}"
            r"\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\b[^.]{0,30}\bbatch\b[^.]{0,30}\bcontain(?:s|ing)?\b"
            r"|\bminimum\s+order\s+quantity\b",
            lower,
        ):
            continue
        # "One serving of the cereal contains 200 units of vitamin D", "the
        # patient received 2 units of blood" count nutritional or medical
        # units, not contractual goods.  "The apartment block comprises 12
        # units" counts dwellings; "5,000 units of inventory", "30 units of
        # credit transfer" and "1,000 units per hour" are stock, academic or
        # capacity measures — none is a committed purchase quantity.  Genuine
        # purchase commitments ("Buyer shall purchase a total of 10,000 units
        # of the Products", fixture line 41) still fire — the guard requires a
        # non-commercial noun frame ("units of X" with an inanimate substance,
        # dwelling or capacity context).  Zero fixture lines match (checked
        # against fixtures/generate_fixtures.py; "units of the Products" and
        # "units per year" fixture frames are unaffected).
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+units?\s+of\b[^.]{0,35}"
            r"\b(?:vitamin\w*|blood|inventory|stock|storage\s+capacity|"
            r"credit\w*\s+transfer|rental\w*|dwelling\w*|bedroom\w*|apartment\w*)\b",
            lower,
        ):
            continue
        # "The apartment block comprises 12 units" counts dwellings (the
        # comprises-verb frame) and "the generator has a rated capacity of
        # 1,000 units per hour" is an equipment throughput rate (units-per-
        # hour), neither a committed purchase quantity.  Zero fixture lines
        # use "comprises" or "per hour" (checked against
        # fixtures/generate_fixtures.py); "purchase a minimum of 1,000 units
        # per year" (fixture 147) has no comprises/per-hour frame and still
        # fires.
        if re.search(
            r"\bcompris(?:es|ing|ed)\b[^.]{0,30}\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\s+per\s+hour\b"
            r"|\bcapa(?:city|bilit)\w*\b[^.]{0,30}\b\d+(?:[.,]\d+)?\s+units?\b",
            lower,
        ):
            continue
        # "The stock on hand is 500 units" describes an inventory level, not a
        # contractual quantity commitment.  Genuine purchase quantities
        # ("Buyer shall purchase a total of 10,000 units", fixture 41) still
        # fire — they tie the units to a purchase commitment verb.
        if re.search(
            r"\b(?:stock|inventory|goods?)\b[^.]{0,25}\bon\s+hand\b[^.]{0,35}"
            r"\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\b[^.]{0,35}\bon\s+hand\b[^.]{0,25}"
            r"\b(?:stock|inventory|goods?)\b",
            lower,
        ):
            continue
        # "The store sold 5,000 units last quarter" reports a sales result
        # (units sold in a period), not a quantity one party commits to
        # purchase.  Genuine commitments ("Buyer shall purchase a total of
        # 10,000 units", fixture 41) never use "sold" — the word-boundary
        # keeps "unsold" (fixture 281 "repurchase unsold inventory") out.
        if re.search(
            r"\b(?:sold|sells?|sale\w*)\b[^.]{0,45}"
            r"\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\b[^.]{0,45}"
            r"\b(?:sold|sells?|sale\w*)\b",
            lower,
        ):
            continue
        # "The ship carries 4,000 units of cargo" counts freight volume (units
        # of cargo in transit), not a contracted goods quantity.  The
        # units-of-substance guard already covers vitamin/blood/inventory
        # frames; "cargo"/"freight"/"container" extend it to logistics
        # contexts.  Zero fixture lines use these (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+units?\s+of\b[^.]{0,35}"
            r"\b(?:cargo|freight|container\w*)\b",
            lower,
        ):
            continue
        # "The plant assembled 10,000 units in December" reports a production
        # output (units turned out by a factory), not a quantity commitment
        # between the parties.  Genuine purchase commitments still fire — they
        # tie units to a purchase verb, not a production verb.  "Total units
        # produced in 2024 were 50,000" is the same output statement with the
        # count at the end of the sentence.  The production verbs are spelled
        # out (not "produc\w*") so the noun "Products" in a genuine
        # commitment ("Buyer shall purchase a total of 10,000 units of the
        # Products", fixture line 41) is never matched.
        if re.search(
            r"\b(?:assembl\w*|manufactur\w*|produced|produces|producing|"
            r"production|output)\b[^.]{0,45}\b\d+(?:[.,]\d+)?\s+units?\b"
            r"|\b\d+(?:[.,]\d+)?\s+units?\b[^.]{0,45}"
            r"\b(?:assembl\w*|manufactur\w*|produced|produces|producing|"
            r"production|output)\b"
            r"|\btotal\s+units?\b[^.]{0,45}\bproduc\w*\b[^.]{0,45}"
            r"\b(?:were|was|is|are|amount\w*ed\s+to)\s+\d+(?:[.,]\d+)?\b",
            lower,
        ):
            continue
        # "A minimum quantity of 1,000 units must be ordered" is an internal
        # ordering rule, not a quantity commitment between the parties.
        # Fixture 147's "Distributor shall purchase a minimum of 1,000 units
        # per year" still fires — it has no "must be ordered" frame.
        if re.search(
            r"\bminimum\s+quantity\b[^.]{0,40}\bmust\s+be\s+ordered\b"
            r"|\bmust\s+be\s+ordered\b[^.]{0,40}\bminimum\s+quantity\b",
            lower,
        ):
            continue
        # Retail shelf-stock frames ("The store keeps a total of 500 items on
        # the shelves") describe inventory on display, not a contracted
        # quantity.  Genuine commitments still fire ("Buyer shall purchase a
        # total of 10,000 units", fixture line 41).
        if re.search(
            r"\b\d+(?:[.,]\d+)?\s+items?\b[^.]{0,45}"
            r"\b(?:on\s+the\s+shel(?:f|ves)|keeps?\b|stocks?\b|display(?:ed|s)?\b)\b",
            lower,
        ):
            continue
        # Pack-content statements ("Each box contains 12 items for the retail
        # pack") quote how many pieces come in a unit of packaging, not a
        # quantity commitment between the parties.
        if re.search(
            r"\b(?:box\w*|carton\w*|pack(?:age|aging)?s?\b|case\s+of)\b"
            r"[^.]{0,40}\b(?:contains?|holds?\b|includes?)\b[^.]{0,35}"
            r"\b\d+(?:[.,]\d+)?\s+items?\b",
            lower,
        ):
            continue
        # Print-run statements ("The publisher printed a total of 5,000 copies
        # of the novel") report how many copies were produced, not a
        # contracted quantity.
        if re.search(
            r"\b(?:publish\w*|printed|prints|printing|print\s+run)\b[^.]{0,45}"
            r"\b\d+(?:[.,]\d+)?\s+copies?\b",
            lower,
        ):
            continue
        m = re.search(
            rf"\b{_NUMTOKEN}\s+units?\b"
            rf"|\b{_NUMTOKEN}\s+items?\b"
            rf"|\b{_NUMTOKEN}\s+copies?\b"
            rf"|\b{_NUMTOKEN}\s+events?\b"
            rf"|\b{_NUMTOKEN}\s+pages?\b",
            sentence,
            re.IGNORECASE,
        )
        if not m:
            continue
        detail = m.group(0)
        out.append(Match("quantity", page, sentence, RiskLevel.LOW, f"quantity {utils.compact(detail)}", detail))
    return out


# ---------------------------------------------------------------------------
# 3  price
# ---------------------------------------------------------------------------


def rule_price(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "price", "fee", "fees", "rent ", "retainer", "capital contribution",
            "loan", "lend", "borrow", "consideration", "contract sum", "budget", "charge",
            "salary", "reimburse", "quote", "to pay", "payable",
        )):
            continue
        if any(k in lower for k in ("liquidated damages", "penalty", "penalties", "breach", "surcharge", "interest")):
            continue
        # Require a tagged financial number: amount OR "N% of <money-kind>" OR price/discount pct.
        amounts = _AMOUNT_RE.findall(sentence)
        pct = _PERCENT_RE.search(sentence)
        risk = RiskLevel.LOW
        detail = None
        if amounts:
            detail = ", ".join(a.strip() for a in amounts[:3])
        elif pct and ("price" in lower or "fee" in lower or "rent" in lower or "capital contribution" in lower):
            # A bare "N% of <money kind>" (fees / invoices / prices / ...) is a
            # share or rate of money (cap, buy-back, retention, late fee), not
            # the price itself — skip the bare-% route.
            if not _PCT_OF_MONEY_RE.search(sentence):
                detail = pct.group(0)
        if not detail:
            continue
        # "redemption price of the shares", "strike price of each Option" are
        # security / instrument prices, not a contract purchase price.  Zero
        # fixture files use these phrases (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:redemption|strike|exercise|conversion|repurchase|buy[- ]back)\s+price\b",
            lower,
        ):
            continue
        # "stock price / share price / market price / retail price" are
        # securities-market or consumer-market prices, not the price agreed
        # for goods or services under this contract.  A "transfer price" is
        # an intra-group accounting rate (per-unit cost allocation between
        # related entities), not a contract purchase price.  Genuine contract
        # prices ("the price of the Goods is USD 100 per unit", "Supply price
        # is USD 12.00 per unit") still fire — the guard requires the
        # market-noun prefix, so "per unit" sentence-final pricing is
        # untouched.  Zero fixture lines use these prefixes (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:stock|share|market|retail|transfer|spot|listed|quoted)\s+price\b",
            lower,
        ):
            continue
        # "average price of comparable products" is a market benchmark, not a
        # contract price; "notes payable" on a balance sheet is an accounting
        # line item, not money owed under this Agreement.  Neither phrase
        # appears in the fixtures.
        if re.search(
            r"\baverage\s+price\b[^.]{0,20}\bcomparable\b"
            r"|\bnotes?\s+payable\b",
            lower,
        ):
            continue
        # "Reimbursable costs shall not exceed USD 50,000 in any calendar
        # year" is an expense-reimbursement cap (the amount a party may
        # claim back for out-of-pocket spend), not the price of goods or
        # services under this Agreement.  No fixture line pairs
        # reimburse/expense with a "shall not exceed" amount: the fixture
        # line "Client shall reimburse Agency for out-of-pocket costs within
        # net 30 days" has no amount, and fixture expenses are expressed as
        # sums certain.  Genuine prices still fire.
        if re.search(
            r"\b(?:reimburs\w*|expense\w*|out[- ]of[- ]pocket\w*|expenditure\w*)\b"
            r"[^.]{0,45}\bshall\s+(?:not\s+)?(?:exceed|be\s+limited\s+to|be\s+capped\s+at)\b"
            r"|\b(?:cap\w*|limit\w*)\b[^.]{0,30}\b(?:reimburs\w*|expense\w*|"
            r"out[- ]of[- ]pocket\w*)\b",
            lower,
        ):
            continue
        # "A fee of USD 500 shall be charged for cancellation of the booking"
        # is an administrative cancellation charge (a booking / reservation /
        # event / account fee), not the price of goods or services.  Genuine
        # prices ("the price of the Goods is USD 100 per unit") still fire.
        # Zero fixture lines mention cancellation fees (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:fee|fees|charge\w*)\b[^.]{0,50}\bcancel(?:l|ll)?(?:ation|l?ed|l?ing)?\b"
            r"|\bcancel(?:l|ll)?(?:ation|(\s+and\s+)?refund)?\b[^.]{0,50}"
            r"\b(?:fee|fees|charge\w*|penalty)\b",
            lower,
        ):
            continue
        # "The domain name renewal fee is USD 12 per year" / "a renewal fee of
        # USD 50 applies for the membership" quote the fee charged to re-issue
        # an account, registration or credential — an administrative billing
        # amount, not the price of goods or services under this Agreement.
        # The renewal-rule guards already suppress the renewal meaning; this
        # guard suppresses the price meaning.  Genuine prices ("the price of
        # the Goods is USD 100 per unit") still fire — the guard requires the
        # "renewal fee" noun.
        if re.search(
            r"\brenewal\s+fees?\b",
            lower,
        ):
            continue
        # "Charging extra attracts a 5% late fee from the bank" is a bank
        # customer-charges frame (the bank levies the fee on the customer's
        # account), not the price of goods or services under this Agreement.
        # The penalty-rule guard already suppresses the penalty side; this
        # guards the bare-% price route.  Zero fixture lines pair a late fee
        # with a bank / account context (checked against
        # fixtures/generate_fixtures.py) — genuine prices still fire.
        if re.search(
            r"\blate\s+fee\w*\b[^.]{0,50}"
            r"\b(?:from\s+the\s+bank|charged\s+by\s+(?:the\s+)?bank\b|"
            r"by\s+the\s+bank|at\s+the\s+bank)\b"
            r"|\b(?:bank\s+charges?|bank\s+fees?\b|bank's?\s+fees?\b|"
            r"account\s+charges?)\b[^.]{0,45}\blate\s+fee\w*\b",
            lower,
        ):
            continue
        # "The customer asked for a quote for the new widgets" / "We received
        # a price quote of USD 500 for the repair" / "The bid price was USD
        # 1,200,000 for the construction project" quote a market ask or a
        # submitted tender bid — a procurement estimate, not the price agreed
        # for goods or services under this contract.  The guard requires the
        # "price quote" compound or a bare "quote"/"bid" with a price amount
        # and a bidding frame; a genuine contracted price still fires (the
        # fixture price lines never use quote/bid phrasing — fixture line 178
        # "Subcontractor quote: USD 4,520,000 total." is a heading label with
        # no "price" prefix or bid verb and still fires).
        if re.search(
            r"\bprice\s+quote\w*\b"
            r"|\bbid\s+price\b"
            r"|\b(?:quoted?\s+price|quote\w*|bid\s+price|bids?\b)\b[^.]{0,25}"
            r"\b(?:request(?:ed|s)?\s+for|solicit\w*|submit\w*|received?|"
            r"invitation\s+to|sealed|tender\w*)\b"
            r"|\b(?:request(?:ed|s)?\s+for|solicit\w*|submit\w*|received?|"
            r"invitation\s+to|sealed|tender\w*)\b[^.]{0,25}\b(?:quote\w*|bid\w*)\b",
            lower,
        ):
            continue
        # "The list price of the item is USD 50" quotes a manufacturer's
        # suggested / catalogue price (a market sticker price a seller sets),
        # not the price agreed for goods or services under this contract.  The
        # market-prefix guard above covers "listed price" but not the "list
        # price" compound.  Genuine contract prices ("the price of the Goods
        # is USD 100 per unit") still fire.  Zero fixture lines use "list
        # price" (checked against fixtures/generate_fixtures.py).
        if re.search(r"\blist\s+price\b", lower):
            continue
        # "The budget for the project is USD 100,000" states a budgetary
        # allocation (how much money is set aside for the project), not the
        # price agreed for goods or services under this Agreement.  The guard
        # keys on the "budget for ... is/amounts to/shall be" copula — NOT on
        # bare "budget" or "shall not exceed", so fixture line 344 ("Media
        # buying budget shall not exceed USD 2,000,000 in any calendar year.")
        # — a genuine price cap — still fires.  Genuine prices ("the price of
        # the Goods is USD 100 per unit") keep firing.
        if re.search(
            r"\bbudget\s+for\b[^.]{0,30}\b(?:is\b|amounts?\s+to\b|shall\s+be\b|"
            r"stands?\s+at\b)\b",
            lower,
        ):
            continue
        # "The advertised price of the car is USD 25,000" quotes a seller's
        # sticker / advertisement price (consumer-market asking price), not a
        # price agreed for goods or services under this contract.  Genuine
        # contract prices still fire — the guard requires the
        # advertised/advertising modifier on "price".  Zero fixture lines use
        # "advertised price" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\badvertis\w*d\s+price\b",
            lower,
        ):
            continue
        # "The fee for the membership is USD 30 per month" quotes a membership
        # subscription tariff (a consumer club / account charge), not the price
        # of goods or services under this Agreement.  The guard requires the
        # fee/membership pairing; genuine contract fees ("a service fee of USD
        # 500", "the fees shall be split equally") still fire (no membership
        # noun).  Zero fixture lines mention membership (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bfee\w*\b[^.]{0,30}\bmembership\b"
            r"|\bmembership\b[^.]{0,30}\bfee\w*\b",
            lower,
        ):
            continue
        # "The supplier charges a restocking fee of 10% for returns", "A
        # handling fee of EUR 20 applies to each transaction" and "The payment
        # gateway charges a processing fee of 2%" quote merchant / tariff
        # charges or registration clearances, not the price of goods or
        # services under this Agreement.  Genuine contract prices (service fee
        # of USD 500, fees split equally) still fire — none of these nouns
        # appear in fixtures/generate_fixtures.py.
        if re.search(
            r"\b(?:processing|restocking|handling|registration)\s+fees?\b",
            lower,
        ):
            continue
        # "The shares were offered at a price of USD 10 per share" quotes a
        # share/stock issuance price, not the price of goods or services under
        # this Agreement.  The frame requires the "offered ... at a price"
        # construction in both word orders; genuine contract prices ("The price
        # of the first batch shall be EUR 5,000 per unit") have no "offered"
        # verb and still fire.  The redemption-price positive is protected by
        # the redemption guard above.  Zero fixture lines combine "offered"
        # with "price" (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\boffered\b[^.]{0,50}\bat\s+(?:a\s+)?price\b"
            r"|\bat\s+(?:a\s+)?price\b[^.]{0,50}\boffered\b",
            lower,
        ):
            continue
        # "The government fixed the electricity price at EUR 0.30 per kWh"
        # reports a state-set utility tariff, not the price of goods or
        # services under this Agreement.  The frame requires a regulator
        # subject or the electricity/price pairing; a genuine contract price
        # ("Buyer shall purchase ... at the price of USD 24.50 per unit") has
        # neither and still fires (checked against
        # fixtures/generate_fixtures.py line 41).
        if re.search(
            r"\b(?:government\w*|regulat\w*|state\b|authority\w*|ministr\w*)\b[^.]{0,35}\bfix\w*\b[^.]{0,40}\b(?:price\w*|tariff\w*)\b"
            r"|\belectricity\w*\b[^.]{0,20}\bprice\b"
            r"|\bprice\b[^.]{0,20}\belectricity\w*\b",
            lower,
        ):
            continue
        # "The court awarded attorney fees of USD 5,000 to the prevailing
        # party" quotes litigation or professional legal costs, not the price
        # of goods or services under this Agreement.  The frame requires an
        # attorney / legal-costs phrase on either side of the amount; genuine
        # contract prices ("The price of the Goods is USD 100 per unit",
        # fixture line 327 "Base rent of USD 48,000 per year") have no
        # attorney / legal-costs noun and still fire.  Zero fixture lines
        # mention attorney or legal costs/fees (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:attorney\w*|attorneys?['’]?\s*fees?|legal\s+(?:costs?|fees?|"
            r"expenses?)|solicitor\w*\s+fees?|lawyer\w*\s+fees?)\b",
            lower,
        ):
            continue
        # "Tuition fees for the semester amount to USD 5,000", "The
        # application fee is USD 50", "The bank charges an overdraft fee of
        # USD 30 per month" and "The consultation fee is USD 200 per hour"
        # quote price-goods fees charged to consumers or students (admissions,
        # banking, professional advisory), not the price of goods or services
        # under this Agreement.  The guard lists the fee-family nouns that pin
        # these non-contract frames; genuine contract prices ("the licence
        # fees of USD 2,500 per year", fixture line 59, "a subscription fee of
        # USD 99 per month", fixture line 163) use other fee nouns and still
        # fire.  Zero fixture lines use tuition / application / overdraft /
        # consultation fees (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:tuition\w*|application\s+fees?|overdraft\w*\s+fees?|"
            r"consultation\w*\s+fees?)\b",
            lower,
        ):
            continue
        # "A late fee of USD 25 applies after the due date" is a
        # late-payment-charge notice framed by the due date (a billing /
        # consumer fee), not the price of goods or services under this
        # Agreement.  The guard requires a "late fee" bound to a due-date
        # phrase within the clause; genuine contract prices and fixture fee
        # lines (59, 79, 115, 163, 265, 297, 311, 359) have no late-fee /
        # due-date pairing and still fire.  Zero fixture lines combine a late
        # fee with a due date (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\blate\s+fees?\b[^.]{0,45}\bdue\s+date\b"
            r"|\bdue\s+date\b[^.]{0,45}\blate\s+fees?\b",
            lower,
        ):
            continue
        # "The airline ticket price is now USD 450", "The asking price for the
        # house is USD 400,000" and "The parking charge is USD 3 per hour"
        # quote a consumer / market price tag (tickets, listed homes, parking
        # tariffs) instead of the price of goods or services under this
        # Agreement.  "The entrance fee for the museum is USD 15 per adult" is
        # a venue tariff, "The hourly rate charged by the plumber is EUR 60"
        # quotes a tradesperson's going rate, and "Wages for the new hires are
        # set at an annual salary of EUR 48,000" reports a payroll figure.
        # Genuine contract prices (fixture 41 "the price of USD 24.50 per
        # unit", 148 "Supply price USD 12.00 per unit", 215 "Base salary of
        # USD 180,000 per annum" -- which lacks the wages/salary pairing --
        # and 327 "Base rent of USD 48,000 per year") are framed as a
        # purchase/supply price for goods/services and lack these consumer /
        # market / tariff nouns.  Zero fixture lines pair ticket/asking/
        # parking/entrance with price or match an hourly-rate-plus-occupation
        # frame (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bticket\w*[^.]{0,15}price\b|\bprice\b[^.]{0,15}ticket\w*",
            lower,
        ):
            continue
        if re.search(r"\basking\s+price\b", lower):
            continue
        if re.search(r"\bparking\b[^.]{0,20}\b(?:charge\w*|fees?|rate\w*)\b", lower):
            continue
        if re.search(
            r"\b(?:entrance|admission|entry)\s+(?:charge\w*|fees?)\b",
            lower,
        ):
            continue
        if re.search(
            r"\bhourly\s+rate\b[^.]{0,40}\b(?:plumber\w*|electrician\w*|"
            r"mechanic\w*|handyman\w*|gardener\w*|cleaner\w*|tutor\w*|"
            r"consultant\w*|lawyer\w*|attorney\w*)\b"
            r"|\b(?:plumber\w*|electrician\w*|mechanic\w*|handyman\w*|"
            r"gardener\w*|cleaner\w*|tutor\w*|consultant\w*|lawyer\w*|"
            r"attorney\w*)\b[^.]{0,30}\bhourly\s+rate\b",
            lower,
        ):
            continue
        # A "wages ... annual salary" pairing is payroll reporting, not a
        # goods/services price; the guard requires both nouns so the genuine
        # "Base salary of USD 180,000 per annum" fixture line still fires.
        if re.search(
            r"\bwages?\b[^.]{0,40}\bsalar\w*\b"
            r"|\bsalar\w*\b[^.]{0,40}\bwages?\b",
            lower,
        ):
            continue
        flag = f"price: {detail}"
        if any(k in lower for k in ("in advance", "advance payment", "upon execution",
                                    "upfront", "payable in advance", "non-refundable")):
            risk = RiskLevel.MEDIUM
            flag += " (upfront payment)"
        out.append(Match("price", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 4  delivery
# ---------------------------------------------------------------------------

_DELIVER_VERB = re.compile(
    r"\b(?:shall\s+deliver|deliver\s+(?:the|all)|delivery\s+(?:of|shall|date|deadline|schedule|period|within)|"
    r"delivered\s+within|delivered\s+by|must\s+be\s+delivered|shipment|ship\s+within|"
    r"delivery\s+of\s+the\s+licensed|deliver\s+services|conform\s+to)",
    re.IGNORECASE,
)
_DATE_KW = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{1,2}-\d{1,2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}\b")


def rule_delivery(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not _DELIVER_VERB.search(lower):
            continue
        # Legal/institutional process deadlines ("deliver judgment/award/verdict/
        # ruling", "notice of termination shall be delivered") are procedural,
        # not a goods/services delivery obligation.  "deliver the goods" and date
        # deadlines ("delivered by 30 June") still fire.
        if re.search(
            r"\bdeliver(?:s|ed|y)?\b[^.]{0,40}\b(?:judgment|judgement|award|verdict|ruling|"
            r"decision|opinion|writ|notice\s+of\s+termination|notice\s+of\s+cancellation)\b"
            r"|\b(?:judgment|judgement|award|verdict|ruling|decision|opinion|writ|"
            r"notice\s+of\s+termination|notice\s+of\s+cancellation)\b[^.]{0,40}"
            r"\bmust\s+be\s+delivered\b|\b(?:judgment|judgement|award|verdict|ruling|"
            r"notice\s+of\s+termination|notice\s+of\s+cancellation)\b[^.]{0,40}\bshall\s+be\s+delivered\b"
            r"|\bdelivery\s+of\s+(?:this\s+|the\s+)?(?:notice|notification|letter|demand|"
            r"disclosure|report|statement)\b"
            r"|\b(?:notice|notification|letter|demand|disclosure|report|statement)\s+delivery\b",
            lower,
        ):
            continue
        # "The bank shall deliver the Letter of Credit / documentary credit
        # to the Beneficiary within 5 business days" is trade-finance
        # instrument issuance (the bank conveys a payment instrument), not a
        # goods/services delivery obligation.  "deliver the goods to the
        # buyer's warehouse" fixture lines still fire.
        if re.search(
            r"\bdeliver(?:s|ed|y)?\b[^.]{0,40}\b(?:letter\s+of\s+credit|"
            r"documentary\s+credit|standby\s+letter\s+of\s+credit|"
            r"bank(?:er)?'s?\s+(?:guarantee|draft|cheque|check)|"
            r"bill\s+of\s+exchange|promissory\s+note)\b"
            r"|\b(?:letter\s+of\s+credit|documentary\s+credit|"
            r"standby\s+letter\s+of\s+credit|bank(?:er)?'s?\s+(?:guarantee|draft|"
            r"cheque|check)|bill\s+of\s+exchange|promissory\s+note)\b"
            r"[^.]{0,40}\b(?:shall|will|must)\s+be\s+delivered\b",
            lower,
        ):
            continue
        # Professional / knowledge-delivery events ("deliver a keynote /
        # lecture / training programme / report / presentation slides /
        # workshop") are services milestones, not goods delivery obligations,
        # even when they carry a deadline.  Goods deliveries ("deliver the
        # goods to the buyer's warehouse within 30 days") still fire.
        if re.search(
            r"\bdeliver(?:s|ed|y|ing)?\b[^.]{0,60}\b(?:training\s+program(?:me)?\w*|"
            r"training\s+course\w*|training\s+materials?|presentation\s+slides?|"
            r"lecture\w*|keynote\b|\breport\b|the\s+training\b|workshop\w*|seminar\w*)\b",
            lower,
        ):
            continue
        # "The Contractor shall deliver the business plan to the Board within
        # 14 days" / "The vendor shall deliver the proposal by 30 June" are
        # document/proposal knowledge submissions to a corporate body — not a
        # goods delivery obligation, even with a deadline.  "deliver the
        # business plan" is the professional-output family of the knowledge
        # guard above.  Goods deliveries ("deliver the goods to the buyer's
        # warehouse within 30 days") still fire — the guard requires the
        # non-goods document noun after the deliver verb.
        if re.search(
            r"\bdeliver(?:s|ed|y|ing)?\b[^.]{0,45}"
            r"\b(?:business\s+plans?|proposals?|plans?\s+to\s+(?:the\s+)?"
            r"(?:board|committee|management)|budgets?|strateg(?:y|ies)|"
            r"presentations?|drafts?|memorandum|memo\b)\b"
            r"|\b(?:business\s+plans?|proposals?|budgets?|strateg(?:y|ies)|"
            r"presentations?|drafts?|memorandum|memo\b)\b[^.]{0,45}"
            r"\bmust\s+be\s+delivered\b"
            r"|\b(?:business\s+plans?|proposals?|budgets?|strateg(?:y|ies)|"
            r"presentations?|drafts?|memorandum|memo\b)\b[^.]{0,45}"
            r"\bshall\s+be\s+delivered\b",
            lower,
        ):
            continue
        # Administrative correspondence turn-around deadlines ("the response /
        # reply / invitation / survey results shall be delivered within N days")
        # are communications commitments, not goods/services delivery
        # obligations.  Goods deliveries ("Seller shall deliver the Products
        # within 45 working days", "delivers all Services within 20 business
        # days") still fire — the guard requires the non-goods correspondence
        # noun after the deliver verb.
        if re.search(
            r"\bdeliver(?:s|ed|y|ing)?\b[^.]{0,45}\b(?:responses?|repl(?:y|ies)|"
            r"invitation\w*|survey\s+results?)\b"
            r"|\b(?:responses?|repl(?:y|ies)|invitation\w*|survey\s+results?)\b"
            r"[^.]{0,45}\b(?:shall|will|must)\s+be\s+delivered\b"
            r"|\b(?:responses?|repl(?:y|ies)|invitation\w*|survey\s+results?)\b"
            r"[^.]{0,45}\bdelivered\s+within\b",
            lower,
        ):
            continue
        # Courier / parcel shipping promises ("The courier shall deliver the
        # parcel within 3 days", "Delivery of the parcel ... within 3 working
        # days") are logistics narration, not an inter-party goods-delivery
        # obligation.  Genuine deliveries still fire ("Seller shall deliver the
        # Products within 45 working days", fixture line 42; "Provider shall
        # deliver all Services within 20 business days", fixture line 80).
        if re.search(r"\bcouriers?\b|\bparcels?\b", lower):
            continue
        # Academic / administrative credential handover ("The school shall
        # deliver the certificates to the graduates within 30 days") is a
        # procedural dispatch, not a goods delivery obligation.
        if re.search(
            r"\bdeliver(?:s|ed|y|ing)?\b[^.]{0,45}"
            r"\b(?:certificates?|diplomas?|degrees?)\b[^.]{0,45}"
            r"\b(?:graduates?|students?|recipients?)\b",
            lower,
        ):
            continue
        # Deemed-receipt rules ("Documents delivered by email shall be deemed
        # received within 2 days") state when written notice is treated as
        # received, not a goods/services delivery deadline.
        if re.search(
            r"\bdeliver(?:s|ed|y|ing)?\b[^.]{0,50}\bemail\b"
            r"[^.]{0,40}\bdeemed\s+received\b",
            lower,
        ):
            continue
        m = re.search(
            rf"\bwithin\s+({_NUMTOKEN})\s*(?:business|working|calendar)?\s*(day|week|month)s?\b"
            rf"|\bat\s+least\s+({_NUMTOKEN})\s+days?\s+before\b"
            rf"|\bup\s+to\s+({_NUMTOKEN})\s+days?\b"
            rf"|\bby\s+{_DATE_RE.pattern}",
            sentence,
            re.IGNORECASE,
        )
        date_kw = _DATE_KW.search(sentence)
        deadline = None
        detail = None
        if m:
            num = m.group(1)
            unit = (m.group(2) or "day").lower()
            if num:
                deadline = f"within {num} {unit}s"
                detail = f"{num} {unit}s"
            elif m.group(3):
                deadline = f"at least {m.group(3)} days before"
                detail = f"{m.group(3)} days notice"
            elif m.group(4):
                deadline = f"up to {m.group(4)} days"
                detail = f"{m.group(4)} days"
            else:
                deadline = f"by {date_kw.group(0) if date_kw else 'date'}"
                detail = date_kw.group(0) if date_kw else None
        elif date_kw:
            deadline = f"by {date_kw.group(0)}"
            detail = date_kw.group(0)
        if deadline is None:
            continue
        # risk by length
        if m and m.group(1):
            dnum = float(m.group(1))
            unit = (m.group(2) or "day").lower()
            days = {"day": 1, "week": 7, "month": 30}.get(unit, 1) * dnum
        elif m and m.group(3):
            days = float(m.group(3))
        elif m and m.group(4):
            days = float(m.group(4))
        else:
            days = None
        risk = RiskLevel.LOW
        if days is not None and days > 60:
            risk = RiskLevel.MEDIUM
            deadline += " (long)"
        elif days is not None and days <= 7:
            risk = RiskLevel.MEDIUM
            deadline += " (aggressive)"
        if any(k in lower for k in ("penalty", "liquidated damages", "liable for")):
            risk = RiskLevel.MEDIUM
        out.append(Match("delivery", page, sentence, risk, f"delivery deadline: {deadline}", detail))
    return out


# ---------------------------------------------------------------------------
# 5  acceptance
# ---------------------------------------------------------------------------


def rule_acceptance(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in ("inspection", "acceptance", "accept the",
                                        "acceptance inspection", "acceptance test",
                                        "to inspect", "to accept", "inspect and",
                                        "to have accepted")):
            continue
        # Negated acceptance requirements ("no acceptance inspection is
        # required") make the acceptance machinery optional — not a clause.
        if re.search(r"\b(?:no|not|without|neither)\b[^.]{0,40}"
                     r"\b(?:acceptance|inspection)\b[^.]{0,30}"
                     r"\b(?:required?|necessary|needed|applicable|applies?)\b", lower):
            continue
        # "The bank shall accept the documentary credit / bill of exchange" and
        # "accept the offer / appointment" are trade-finance or formation
        # acceptances of documents or relationships — not of deliverables.
        # "acceptance of the job offer" is a recruitment acceptance (an
        # intervening job/position/employment token before "offer").
        if re.search(
            r"\baccept\w*\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:documentary\s+credit(?:s)?|letter\s+of\s+credit|bill(?:s)?\s+of\s+exchange|"
            r"promissory\s+note(?:s)?|draft(?:s)?|(?:job\s+|position\s+|employment\s+)?"
            r"offer(?:s)?|tender(?:s)?|bid(?:s)?|"
            r"purchase\s+order(?:s)?|quotation(?:s)?|term(?:s)?\s+(?:and|&)\s+condition(?:s)?|"
            r"appointment(?:s)?|engagement(?:s)?)\b"
            r"|\bacceptance\s+of\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:documentary\s+credit(?:s)?|letter\s+of\s+credit|bill(?:s)?\s+of\s+exchange|"
            r"promissory\s+note(?:s)?|draft(?:s)?|(?:job\s+|position\s+|employment\s+)?"
            r"offer(?:s)?|tender(?:s)?|bid(?:s)?|"
            r"purchase\s+order(?:s)?|quotation(?:s)?)\b"
            # "The Bank's acceptance letter shall be presented together with
            # the shipping documents" is documentary trade finance — a bank
            # acceptance of a bill/draft, not acceptance of deliverables.
            # The context is restricted to trade-finance tokens so a genuine
            # acceptance letter tied to an acceptance window ("acceptance
            # letter within 10 days of delivery of the Deliverables") still
            # fires via the numeric-window patterns below.
            r"|\bacceptance\s+letter\b[^.]{0,45}"
            r"\b(?:bank\w*|documentary|letter\s+of\s+credit|ship\w*|"
            r"bill\s+of\s+lading)\b"
            r"|\b(?:bank\w*|documentary|letter\s+of\s+credit|ship\w*|"
            r"bill\s+of\s+lading)\b"
            r"[^.]{0,45}\bacceptance\s+letter\b",
            lower,
        ):
            continue
        # "Acceptance of the risk under this policy" is an underwriting /
        # risk-appetite statement, and "Acceptance of liability for loss of
        # use is excluded" restates an exclusion carve-out — neither is a
        # deliverables acceptance.  No fixture uses "acceptance of risk" or
        # "acceptance of liability".
        if re.search(
            r"\bacceptance\s+of\s+(?:the\s+|all\s+|any\s+|such\s+)?"
            r"(?:risk|liabilit\w*)\b"
            r"|\bunderwrit\w*\b",
            lower,
        ):
            continue
        # "Acceptance of cash payments is at the sole discretion of the
        # seller" is a payment-method statement, not a deliverables
        # acceptance.  Genuine acceptance windows ("10 days to inspect and
        # accept each deliverable") still fire.
        if re.search(
            r"\bacceptance\s+of\s+cash\s+payments?\b"
            r"|\bacceptance\s+of\s+payments?\b[^.]{0,45}\bdiscretion\b",
            lower,
        ):
            continue
        # "The acceptance of the application / registration / request shall
        # be notified to the applicant" is administrative paperwork (licensing,
        # registration, immigration), not a deliverables acceptance.  Genuine
        # acceptance windows still fire.
        if re.search(
            r"\bacceptance\s+of\s+(?:the\s+|a\s+|this\s+|an\s+)?"
            r"(?:application\w*|registration\w*|enrollment\w*|enrolment\w*|"
            r"request\w*|submission\w*)\b"
            r"|\b(?:application\w*|registration\w*|enrollment\w*|enrolment\w*|"
            r"request\w*|submission\w*)\b[^.]{0,35}\baccept(?:ed|s|ing)?\b",
            lower,
        ):
            continue
        # "She received an acceptance letter from the university" is an
        # admissions / academic decision (a study offer), not a deliverables
        # acceptance.  The trade-finance letter guard above requires a bank /
        # shipping context, so an academic or recruiting context is guarded
        # here.  Genuine deliverable acceptance windows keep firing — no
        # fixture line mixes acceptance letters with universities or
        # employers (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\baccept(?:ance|ed|ing)?\s+(?:of\s+)?letter\w*\b[^.]{0,40}"
            r"\b(?:university|college|school|employer|tenant|institution)\b"
            r"|\b(?:university|college|school|employer|tenant|institution)\b"
            r"[^.]{0,40}\baccept(?:ance|ed|ing)?\s+(?:of\s+)?letter\w*\b",
            lower,
        ):
            continue
        # "Acceptance of the settlement was confirmed by the parties" is a
        # dispute-resolution milestone (a settlement / mediation /
        # compromise), not acceptance of deliverables.  No fixture uses
        # "acceptance of" with a settlement noun.
        if re.search(
            r"\bacceptance\s+of\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:settlement\w*|mediation\w*|compromise\w*|mediation\s+agreement)\b"
            r"|\b(?:settlement\w*|mediation\w*|compromise\w*)\b[^.]{0,35}"
            r"\bacceptance\b",
            lower,
        ):
            continue
        # "His acceptance speech lasted ten minutes" is an events / oratory
        # description (a speech given when taking on a role), not a
        # deliverables acceptance.
        if re.search(
            r"\bacceptance\s+(?:speech|oration|presentation)\b"
            r"|\b(?:speech|oration|presentation)\b[^.]{0,30}\bacceptance\b",
            lower,
        ):
            continue
        # "Acceptance sampling is used for quality control" is a statistical /
        # quality-management technique, not a contract acceptance clause.
        if re.search(
            r"\bacceptance\s+sampling\b"
            r"|\bsampling\b[^.]{0,30}\bacceptance\b",
            lower,
        ):
            continue
        # "University acceptance rates fell this year" is an admissions
        # statistics report, not a deliverables acceptance.
        if re.search(
            r"\bacceptance\s+rates?\b"
            r"|\brates?\b[^.]{0,30}\bacceptance\b",
            lower,
        ):
            continue
        # "Social acceptance of the policy grew" is a broad public / societal
        # reaction to an idea or reform (measured in opinions or polls), not
        # the taking-up of contract deliverables.  Genuine acceptance of
        # goods / works / services still fires — the guard requires an
        # attributive modifier (social, public, market, widespread, cultural)
        # before "acceptance".  Zero fixture lines use that frame.
        if re.search(
            r"\b(?:social|public\b|market\b|widespread|cultural|societal|"
            r"political|general|popular|broader?|audience|wide\b)\s+acceptance\b"
            r"|\bacceptance\b[^.]{0,30}\b(?:social|public\b|market\b|"
            r"widespread|cultural|societal|political|general|popular|"
            r"broader?|audience|wide\b)\b",
            lower,
        ):
            continue
        # "The Acceptance Certificate shall be issued upon the completion of
        # the Works" is construction / works paperwork (a certificate
        # evidencing completion), not a deliverables acceptance window.  A
        # certificate deadline ("an acceptance certificate shall be issued
        # within 10 days after delivery") is still a genuine acceptance
        # timeline and keeps firing via the numeric-window patterns.
        if re.search(
            r"\bacceptance\s+certificate\b[^.]{0,60}"
            r"\b(?:issu\w*|deliver\w*|certif\w*|present\w*)\b"
            r"|\b(?:issu\w*|deliver\w*|certif\w*|present\w*)\b[^.]{0,60}"
            r"\bacceptance\s+certificate\b",
            lower,
        ) and not re.search(
            r"\bwithin\s+\d+\s*(?:working|business)?\s*days?\b"
            r"|\b(?:inspect|to\s+inspect|to\s+accept|inspect\s+and)\b",
            lower,
        ):
            continue
        # "The Buyer shall have the exclusive right to inspect the Books and
        # Records of the Seller" is an audit / verification right (a party may
        # examine records), not a window in which deliverables are examined
        # and accepted.  Genuine acceptance windows ("10 days to inspect and
        # accept each deliverable") still fire — "inspect and ... accept"
        # (verb chain) is untouched.  Both word orders are covered.
        if re.search(
            r"\bto\s+inspect\b[^.]{0,45}\b(?:books?|records?|accounts?|"
            r"book\s+(?:s?\w*)?s?\s+(?:and|&)?|audit\b|documentation|premises|"
            r"facilities|sites?|plants?|factories?)\b"
            r"|\b(?:books?|records?|accounts?|audit\s+right\w*|"
            r"documentation|premises|facilities|sites?|plants?|factories?)\b"
            r"[^.]{0,45}\bto\s+inspect\b"
            r"|\b(?:premises|facilities|sites?|plants?|factories?|books?|"
            r"records?|accounts?|documentation)\b[^.]{0,25}\bfor\s+inspection\b"
            r"|\binspection\s+of\b[^.]{0,35}\b(?:the\s+|its\s+|their\s+|our\s+)?"
            r"(?:premises|facilities|sites?|plants?|factories?|books?|records?|"
            r"accounts?|documentation)\b",
            lower,
        ):
            continue
        # "The Customer's continued use of the Services constitutes acceptance
        # of the amended terms" is an acceptance-by-conduct / contract-formation
        # statement (assent to amendments and notices), not a deliverables
        # acceptance window.  Similarly "a notice shall be deemed accepted upon
        # receipt" is procedural receipt mechanics.  Genuine acceptance windows
        # ("10 days to inspect and accept each deliverable") still fire.
        if re.search(
            r"\bconstitut\w*\b[^.]{0,30}\bacceptance\b"
            r"|\bacceptance\b[^.]{0,30}\bconstitut\w*\b"
            r"|\b(?:deemed|deem|shall\s+be\s+considered)\s+accept"
            r"(?:ed|ance)?\b"
            r"|\bacceptance\s+of\s+(?:the\s+|these\s+|any\s+)?"
            r"(?:amended|revised|changed|modified|new)\s+terms?\b"
            r"|\bacceptance\s+of\s+(?:the\s+)?(?:terms?\b|agreement\b|"
            r"amendment\w*)\b",
            lower,
        ):
            continue
        # "The Term shall run from the Effective Date until the Final
        # Acceptance of the last Deliverable" defines the term's end by a
        # milestone event — it is a duration / term-end statement, not a
        # window in which deliverables are inspected and accepted.  The guard
        # requires a term-family framing (Term / Effective Date / commence /
        # expiry / the Agreement continuing) together with the milestone
        # "acceptance of" a deliverable-like object, so genuine acceptance
        # windows ("10 days to inspect and accept each deliverable") still
        # fire; milestone mentions in payment/retention contexts that lack
        # the term-family framing ("10% on final acceptance") stay live too.
        if re.search(
            r"\b(?:term|agreement|effective\s+date|commenc\w*|expir\w*|"
            r"runs?\s+from|continue\w*|remain\s+in\s+effect)\b"
            r"[^.]{0,45}\bacceptance\s+of\b[^.]{0,35}"
            r"\b(?:deliverable\w*|work\w*|services?\b|goods\b|products?\b|"
            r"milestone\w*)\b",
            lower,
        ) and not re.search(
            r"\bwithin\s+\d+\s*(?:working|business)?\s*days?\b"
            r"|\b\d+\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\b"
            r"|\b(?:inspect\b|to\s+inspect\b|inspect\s+and\b)",
            lower,
        ):
            continue
        # "Acceptance of the final deliverable on completion" states the mere
        # fact that acceptance happens at a milestone — a definitional /
        # procedural pointer, not a review window imposing an acceptance
        # procedure.  Genuine acceptance windows ("10 days to inspect and
        # accept each deliverable", "acceptance tests ... within 7 days")
        # still fire — the negative leg keeps any day-count, inspection or
        # test signal live.
        if re.search(
            r"\bacceptance\s+tests?\b[^.]{0,45}"
            r"\bin\s+accordance\s+with\b[^.]{0,35}"
            r"\b(?:the\s+)?(?:test\s+plan|schedule\s+\d|exhibit\s+\d|appendix\s+\w+)\b",
            lower,
        ):
            continue
        # "The Buyer shall not unreasonably refuse to accept delivery of the
        # Goods" is a party's obligation about accepting deliveries — a
        # delivery/conduct rule, not a window in which deliverables are
        # examined and accepted.  Genuine acceptance windows still fire.
        if re.search(
            r"\brefus\w*\b[^.]{0,35}\bto\s+accept\b[^.]{0,45}\bdeliver\w*\b"
            r"|\bdeliver\w*\b[^.]{0,45}\brefus\w*\b[^.]{0,35}\bto\s+accept\b"
            r"|\brefus\w*\bto\s+accept\b(?:ance)?\s+of\s+deliver",
            lower,
        ):
            continue
        # "The parties shall execute this Deed upon the acceptance of the
        # appended signature page" is a deed-execution / signature formalities
        # statement (when the deed is deemed executed), not a window in which
        # deliverables are inspected and accepted.  The guard anchors on
        # deed / signature-page / counterpart execution language on either
        # side of "acceptance"; genuine deliverables acceptance ("10 days to
        # inspect and accept each deliverable") still fires.
        if re.search(
            r"\b(?:deed\w*|signature\s+(?:page|pages?)|signed\s+page|counterpart\w*)"
            r"\b[^.]{0,45}\baccept\w*\b"
            r"|\baccept\w*\b[^.]{0,45}\b(?:deed\b|signature\s+(?:page|pages?)|"
            r"signed\s+page|counterpart\w*)\b"
            r"|\bexecut\w*\b[^.]{0,40}\b(?:deed\b|signature\s+page\b)\b[^.]{0,40}"
            r"\baccept\w*\b",
            lower,
        ):
            continue
        # "Final acceptance shall take place after site handover" states when
        # acceptance happens as a project milestone (site handover / completion
        # / commissioning) — a schedule pointer, not a window in which
        # deliverables are inspected and accepted.  Genuine acceptance windows
        # ("final acceptance within 10 days of delivery", "acceptance tests ...
        # within 7 days") still fire — the negative leg keeps any day-count,
        # inspection or test signal live.
        if re.search(
            r"\b(?:final\s+|provisional\s+)?acceptance\b[^.]{0,60}"
            r"\b(?:takes?\s+place|shall\s+be\s+(?:made|carried\s+out|effected|given))"
            r"\b[^.]{0,40}\b(?:after|upon|on)\b[^.]{0,40}"
            r"\b(?:handover|completion|commissioning|practical\s+completion)\b",
            lower,
        ) and not re.search(
            r"\bwithin\s+\d+\s*(?:working|business)?\s*days?\b"
            r"|\b\d+\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\b"
            r"|\b(?:inspect\b|to\s+inspect\b|to\s+accept\b|test\w*\b)",
            lower,
        ):
            continue
        # "The acceptance test will take place after the pilot run" / "Site
        # acceptance is scheduled for 31 December" schedule a project
        # milestone in a software / construction delivery plan — not a window
        # in which deliverables are examined and accepted.  Genuine acceptance
        # windows ("acceptance tests ... within 7 days", "Final acceptance
        # shall be completed within 60 days following delivery") still fire —
        # the negative leg keeps any day-count, inspection or accept signal
        # live, and the sentence itself contains the word "test" so the
        # milestone guard above cannot catch this family (its negative leg
        # suppresses "test"-bearing sentences).
        if re.search(
            r"\bacceptance\s+tests?\b[^.]{0,45}"
            r"\b(?:(?:will|shall|may)\s+)?takes?\s+place\b[^.]{0,40}"
            r"\b(?:after|upon|on|following)\b[^.]{0,40}"
            r"\b(?:pilot\s+run\w*|trial\w*|site\b|delivery\s+phase\w*|phase\w*)\b"
            r"|\bsite\s+acceptance\b[^.]{0,45}"
            r"\b(?:is|shall\s+be|will\s+be)\s+scheduled\b"
            r"[^.]{0,40}\b(?:on|for|to\s+take\s+place)\b",
            lower,
        ) and not re.search(
            r"\bwithin\s+\d+\s*(?:working|business)?\s*days?\b"
            r"|\b\d+\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\b"
            r"|\b(?:inspect\b|to\s+inspect\b|to\s+accept\b)\b",
            lower,
        ):
            continue
        # "The end user must accept the terms before using the software" is a
        # clickwrap / browsewrap consent statement (how a consumer assents to
        # online terms), not a window in which deliverables are inspected and
        # accepted.  "Terms of acceptance are set out in the request for
        # proposal" names where acceptance criteria are defined in a tender
        # document (the RFP), not an acceptance window under this contract.
        # Genuine acceptance windows ("acceptance tests ... within 7 days")
        # still fire — the guard requires the end-user/terms-of-acceptance
        # frame.  Zero fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:end\s+)?users?\b[^.]{0,40}\bmust\s+accept\b[^.]{0,40}"
            r"\bterms?\b"
            r"|\bterms?\s+of\s+acceptance\b[^.]{0,40}"
            r"\b(?:set\s+out|defined?|specified?|request\s+for\s+proposal)\b"
            r"|\brequest\s+for\s+proposal\b[^.]{0,40}\bacceptance\b",
            lower,
        ):
            continue
        # "Annual inspection of the boiler is mandatory under the fire code",
        # "the police carried out an inspection of the premises", "the health
        # inspector visited the restaurant for a routine inspection" describe
        # administrative or statutory inspections (safety, police, health,
        # technical, factory) — not acceptance of contract deliverables.
        # "Acceptance of the resignation takes effect immediately" accepts a
        # person's resignation, not goods or services.  Genuine deliverable
        # acceptance ("Provider must complete acceptance inspection of the
        # venue", fixture line 268) still fires — the guard requires a
        # specific non-deliverable inspection noun or the resignation frame.
        # Zero fixture lines pair acceptance with these nouns (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\binspections?\b[^.]{0,40}"
            r"\b(?:boiler|fire\s+code|auditor|factory|health\s+inspector|"
            r"technical|police|routine|annual|regulatory|premises?)\b"
            r"|\b(?:boiler|fire\s+code|auditor|factory|health\s+inspector|"
            r"technical|police|routine|annual|regulatory)\b[^.]{0,40}"
            r"\binspections?\b"
            r"|\bacceptance\s+of\s+(?:the\s+)?(?:resignation|retirement|"
            r"nomination|invitation)\b",
            lower,
        ):
            continue
        # "The goods are subject to prior inspection by the buyer" is a
        # condition-precedent statement (delivery may be checked in advance),
        # not an acceptance window binding the parties to inspect and accept
        # deliverables.  Genuine acceptance windows ("10 days to inspect and
        # accept each deliverable", fixture line 81) still fire — the guard
        # requires the subject-to-prior-inspection frame with no day-count.
        # Zero fixture lines use "prior inspection" or "subject to" with an
        # inspection noun (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bsubject\s+to\b[^.]{0,30}\bprior\s+inspection\b"
            r"|\bprior\s+inspection\b[^.]{0,30}\bsubject\s+to\b"
            r"|\bprior\s+inspection\b[^.]{0,30}\bby\b",
            lower,
        ) and not re.search(
            r"\bwithin\s+\d+\s*(?:working|business)?\s*days?\b"
            r"|\b\d+\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\b",
            lower,
        ):
            continue
        # "Users accept the app at their own risk" is a consumer assumption-of-
        # risk disclaimer (the user agrees to use the software at their own
        # risk), not acceptance of contract deliverables under an acceptance
        # window.  Genuine deliverable acceptance ("the product must pass the
        # acceptance criteria", "acceptance testing shall be completed by the
        # vendor", "final acceptance occurs upon commissioning") still fires —
        # the guard requires the at-own-risk consumer frame.  Zero fixture
        # lines pair accept with "at their own risk" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:user\w*|consumer\w*|customer\w*|buyer\w*)\b[^.]{0,40}"
            r"\baccept\w*\b[^.]{0,40}\bat\s+(?:their|its|the)\s+own\s+risk\b"
            r"|\baccept\w*\b[^.]{0,40}\b(?:the\s+app\b|the\s+software\b)[^.]{0,40}"
            r"\bat\s+(?:their|its|the)\s+own\s+risk\b",
            lower,
        ):
            continue
        # "The offer lapses without acceptance within 30 days" describes the
        # expiry of a contract offer (contract-formation mechanics), not
        # acceptance of deliverables under an acceptance window.  Genuine
        # deliverable acceptance ("Acceptance of the goods requires inspection
        # within 7 days", "the product must pass the acceptance criteria")
        # still fires — the guard requires the offer-lapse frame, and an offer
        # sentence lacks a deliverable noun.  Zero fixture lines use "lapse"
        # with acceptance (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\boffers?\b[^.]{0,45}\blapse\w*\b[^.]{0,45}\baccept"
            r"|\bwithout\s+acceptance\b[^.]{0,35}\boffers?\b",
            lower,
        ):
            continue
        # "acceptance into" tracks admission to a programme / institution /
        # group ("acceptance into the apprenticeship scheme", "acceptance
        # into the college") — a status decision, not contract deliverables.
        # Zero fixture lines use "acceptance into" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(r"\bacceptance\b[^.]{0,20}\binto\b", lower):
            continue
        # "acceptance of the minutes" records approval of meeting minutes /
        # committee notes — corporate-housekeeping, not deliverable
        # acceptance.  Genuine "acceptance of the goods/works/deliverables"
        # still fires (no minutes frame).  Zero fixture lines use it.
        if re.search(r"\bacceptance\b[^.]{0,25}\b(?:the\s+)?minutes\b", lower):
            continue
        # "acceptance of a cheque / check" is a payment instrument being
        # honoured by a bank, not the acceptance of works or services.
        # Zero fixture lines combine acceptance with cheque/check.
        if re.search(r"\bacceptance\b[^.]{0,25}\b(?:a|the)\s+cheque\b"
                     r"|\bacceptance\b[^.]{0,25}\b(?:a|the)\s+check\b", lower):
            continue
        # "favorable acceptance from critics" describes how a work was
        # received by reviewers — reception, not contractual acceptance.
        # Zero fixture lines pair acceptance with critics/reviewers.
        if re.search(r"\bacceptance\b[^.]{0,25}\b(?:from|by)\b[^.]{0,25}"
                     r"\b(?:critics?|reviewers?|audiences?)\b", lower):
            continue
        # "subject to acceptance by the board" is a governance pre-condition
        # (board approval), not a deliverables acceptance window.  Genuine
        # "acceptance by the Customer within X days" keeps the numeric-window
        # path.  Zero fixture lines use the board frame.
        if re.search(r"\bsubject\s+to\s+acceptance\s+by\b[^.]{0,25}"
                     r"\b(?:board|committee|management|directors?)\b", lower):
            continue
        # "acceptance of delivery notices" via a tracking portal is an
        # administrative acknowledgement of dispatch paperwork, not the
        # acceptance of the delivered goods themselves.  Zero fixture lines
        # combine acceptance with delivery notices.
        if re.search(r"\bacceptance\b[^.]{0,30}\b(?:delivery\s+)?notices?\b"
                     r"[^.]{0,40}\bportal\b", lower):
            continue
        # "met with a warm acceptance" describes how a proposal / product was
        # received socially — reception, not the acceptance of deliverables.
        # Genuine "Buyer shall accept each deliverable within 10 business
        # days" keeps the numeric-window path.  Zero fixture lines pair
        # acceptance with "met with" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(r"\b(?:met|received|greeted)\s+with\s+(?:a|an)?\s*\w*\s*"
                     r"\bacceptance\b", lower):
            continue
        # "The university gratefully acknowledged acceptance of the donation"
        # records that a gift was received/accepted (a donation receipt
        # statement), not the contractual acceptance of deliverables subject
        # to an inspection window.  Genuine acceptance clauses target works/
        # goods/services ("The Customer shall accept the Works within 14 days
        # of delivery" — fixture line 268; "Final acceptance shall be
        # completed within 60 days following delivery" — fixture line 182)
        # and never combine "acceptance of" with a gift noun.  Zero fixture
        # lines pair acceptance with donation/gift/grant/bequest/endowment
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bacceptance\b[^.]{0,45}\b(?:donation\w*|gift\w*|grant\w*|"
            r"bequest\w*|endowment\w*)\b",
            lower,
        ):
            continue
        # "Acceptance of the committee's proposal was unanimous" is a
        # deliberative-body endorsement (a committee voting an idea through),
        # not the taking-up of contract deliverables.  Genuine acceptance of
        # goods / works / services still fires — the guard requires a
        # committee/proposal/unanimous frame bound to the acceptance noun.
        # Zero fixture lines pair acceptance with a committee or proposal
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bacceptance\b[^.]{0,45}\b(?:committ\w*|proposal\w*|unanimous\w*|"
            r"agenda\w*|resolution\w*)\b"
            r"|\b(?:committ\w*|proposal\w*|unanimous\w*|agenda\w*|resolution\w*)\b"
            r"[^.]{0,45}\bacceptance\b",
            lower,
        ):
            continue
        within = re.search(r"\bwithin\s+(\d+)\s*(?:working|business)?\s*days?\b", lower)
        window = re.search(r"\b(\d+)\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\s+(?:inspect|accept|complete)", lower)
        risk = RiskLevel.LOW
        detail = None
        flag = "acceptance clause present"
        if within or window:
            days = int((within or window).group(1))
            risk = RiskLevel.MEDIUM
            detail = f"acceptance window: {days} days"
            flag = detail
        out.append(Match("acceptance", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 6  payment
# ---------------------------------------------------------------------------


def rule_payment(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "payment", "payable", "invoice", "net 30", "net 45", "net 60", "net ",
            "installment", "instalment", "advance payment", "prepay", "to pay",
            "deposit", "retainer", "recurring", "credit card",
        )):
            continue
        if any(k in lower for k in ("penalty", "liquidated damages", "breach fee")):
            continue
        # "Late payment shall attract interest at 2% per month on any overdue
        # amounts" is a late-payment interest remedy (an economic consequence
        # of delay), not a payment-terms clause (no due date, sequence,
        # tranche or method is fixed).  Genuine payment clauses survive
        # through the negative leg: net/within terms, a payment/instalment
        # percentage tranche, payable/fee/retention phrasing, or an explicit
        # advance/recurring/instalment signal.
        if re.search(
            r"\b(?:late\s+payment|overdue\s+amounts?|past\s+due|interest)"
            r"[^.]{0,45}(?:\d+(?:\.\d+)?\s*%|interest\b)"
            r"|\binterest\b[^.]{0,45}(?:late\s+payment|overdue\s+amounts?|past\s+due)"
            r"|\b(?:late\s+payment|overdue\s+amounts?)\b[^.]{0,45}\binterest\b",
            lower,
        ) and not re.search(
            r"\bnet\s+\d+\s*days?\b"
            r"|\bwithin\s+\d+\s*(?:days?|weeks?|months?)\b"
            r"|\b\d+\s*(?:business|working)?\s*days?\s+(?:to|in\s+which\s+to)\s+(?:pay|settle)\b"
            r"|\b(?:recurring|credit\s+card)\b"
            r"|\b(?:advance|in\s+advance|upfront|prepay)\b"
            r"|\b(?:payable|instalment|installment|retention)\b",
            lower,
        ):
            continue
        # "deposit" as a placement verb ("shall deposit the goods at a warehouse",
        # "deposit this Agreement with solicitors") is not a payment term.
        # "Security deposit of USD X" / "refundable deposit" (the noun form) is
        # still a payment term and must fire.
        if re.search(
            r"\b(?:shall|will|must|to)\s+deposit\s+(?:the|any|such)\s+"
            r"(?:goods?|equipment|product|products|merchandise|inventory|materials|"
            r"property|assets?|documents?|agreement|contract|instrument)\b",
            lower,
        ) or re.search(
            r"\bdeposits?\s+(?:the|any|such)\s+"
            r"(?:goods?|equipment|product|products|merchandise|inventory|materials|"
            r"property|assets?|documents?|agreement|contract|instrument)\b",
            lower,
        ):
            continue
        # "notes payable" on a balance sheet is an accounting line item, not a
        # payment term between the parties.  None of the fixtures use it.
        if re.search(r"\bnotes?\s+payable\b", lower):
            continue
        # "The Borrower may prepay the Loan in whole or in part" / "A
        # prepayment penalty..." is loan-facility mechanics (the borrower may
        # accelerate repayment), not a payment term for goods or services.
        # Genuine payment terms ("invoices shall be paid within 30 days",
        # "buyer shall make an advance payment") still fire — the gate keeps
        # "to pay" / "advance payment" obligations untouched.
        if re.search(
            r"\b(?:prepay(?:s|ed|ing)?\b|prepayments?|pre[- ]?paid)\b[^.]{0,45}"
            r"\b(?:loan\w*|fixtures?|facilit\w*|debt\w*|borrow\w*|mortgage\w*|"
            r"credit\s+line\b|advance\s+facilit\w*|bond\w*)\b"
            r"|\b(?:loan\w*|fixtures?|facilit\w*|debt\w*|borrow\w*|mortgage\w*|"
            r"credit\s+line\b|advance\s+facilit\w*|bond\w*)\b[^.]{0,45}"
            r"\b(?:prepay(?:s|ed|ing)?\b|prepayments?|pre[- ]?paid)\b",
            lower,
        ):
            continue
        # "Payments may be made in advance of delivery at the Buyer's
        # discretion" is a discretionary / optional early-payment allowance —
        # no primary payment obligation for goods or services is created.
        # Genuine payment terms ("buyer shall make an advance payment of 30%")
        # still fire (the gate's "advance payment" noun phrase requires a
        # committed amount or obligation).
        if re.search(
            r"\b(?:payment|payments|instalments?|installments?|fees?)\b"
            r"[^.]{0,50}\badvance\b[^.]{0,50}\bdiscretion\b"
            r"|\bdiscretion\b[^.]{0,50}\badvance\b[^.]{0,50}"
            r"\b(?:payment|payments|instalments?|installments?|fees?)\b",
            lower,
        ):
            continue
        # "The Insurer shall make payment of the claim within 30 days of the
        # loss assessment" / "payment of the claim shall be made upon receipt
        # of the proof of loss" is an insurer's claims-settlement commitment
        # (the insurer pays its insured's claim), not a commercial payment
        # term between contracting parties for goods or services.  The guard
        # anchors on claim / insured / loss language either side of
        # "payment"; a genuine invoice / purchase-price payment sibling
        # ("payment of the invoice within 30 days") has no insurer context
        # and still fires.
        if re.search(
            r"\b(?:payment|payments?)\s+of\b[^.]{0,40}"
            r"\b(?:claim\w*|loss\b|losses?\b|insured\b|indemnit\w*)\b"
            r"|\b(?:claim\w*|loss\b|losses?\b|insured\b|indemnit\w*)\b[^.]{0,40}"
            r"\b(?:payment|payments?)\s+of\b"
            r"|\b(?:insurer|insurance\s+company|underwriter\w*|policy\b|"
            r"policyholder\w*)\b[^.]{0,45}\b(?:payment|payments?|pay\b)\b",
            lower,
        ):
            continue
        # "The Court awarded payment of USD 50,000 in damages" is a
        # judgment/remedy quantification (what a court awards), not a payment
        # term between the parties.  Genuine payment terms naming the buyer /
        # invoice / purchase price still fire.
        if re.search(
            r"\b(?:court|tribunal|arbitrat\w+)\b[^.]{0,50}"
            r"\b(?:award\w*|order\w*|grant\w*|adjudicat\w*)\b[^.]{0,50}"
            r"\bpayment\b",
            lower,
        ):
            continue
        # "The settlement payment of USD 1 million was court-ordered" is a
        # judgment / remedy event (a court-ordered compensatory payment), not
        # a payment term agreed between the parties — the court frame comes
        # AFTER the payment noun.  Genuine payment terms naming the buyer /
        # invoice / purchase price still fire (zero fixture lines pair a
        # payment with "court-ordered" — checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:payment|payments?)\b[^.]{0,50}"
            r"\bcourt[- ]ordered\b"
            r"|\bcourt[- ]ordered\b[^.]{0,50}"
            r"\b(?:payment|payments?)\b",
            lower,
        ):
            continue
        # "She makes a payment of USD 50 to the charity each month" is a
        # donor's recurring contribution to a charitable body, not a payment
        # term owed between contracting parties.  Genuine payment terms under
        # this rule name the buyer / invoice / purchase price and still fire
        # (zero fixture lines mention charities or donations — checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:payment|payments?|pay\b)[^.]{0,50}"
            r"\b(?:charity|charities|donat\w*|non[- ]profit\w*)\b"
            r"|\b(?:charity|charities|donat\w*|non[- ]profit\w*)\b[^.]{0,50}"
            r"\b(?:payment|payments?|pay\b)\b",
            lower,
        ):
            continue
        # "The Seller accepts payment via credit card" / "We accept payment by
        # card or bank transfer" is a payment-method acceptance statement (the
        # seller's willingness to be paid in a given form), not a payment term
        # fixing an amount, due date or tranche.  Genuine payment terms
        # ("invoices shall be paid within 30 days", "recurring billing to the
        # customer's credit card") still fire — the negative leg keeps
        # recurring / advance / invoice / net terms untouched.
        if re.search(
            r"\baccept\w*\s+payment\w*\b[^.]{0,40}"
            r"\b(?:via|by|through|in)\b[^.]{0,30}"
            r"\b(?:credit\s+card|card\b|bank\s+transfer|debit\s+card|paypal|"
            r"wire\s+transfer|cheque\w*|check\b)\b",
            lower,
        ) and not re.search(
            r"\b(?:recurring|credit\s+card)\b[^.]{0,25}\b(?:billing|charged?|"
            r"debit\w*|payment\s+of)\b"
            r"|\b(?:invoice\w*|net\s+\d+\s+days?\b|within\s+\d+\s+days?\b|"
            r"advance\s+payment|upfront|prepay)\b",
            lower,
        ):
            continue
        # "The store accepts credit cards and mobile payments." is a merchant
        # payment-methods statement (which payment instruments the business
        # takes), not a payment term fixing an amount, due date or tranche.
        # The accept-payment-via guard above needs "payment" as the accept
        # object in a via/by frame, and the may/can-be-made guard needs "for
        # payment" after the card nouns — this sentence has neither, so a
        # separate guard is required.  Genuine payment terms still fire:
        # fixture line 163 ("Monthly subscription fee ... charged to the
        # customer's credit card on a recurring basis") carries the recurring
        # billing marker, and fixture line 298 ("All fees are payable in
        # advance by credit card") carries payable/advance — neither matches
        # the accept-card-and-mobile-payments noun chain (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\baccept\w*\s+(?:credit\s+cards?|debit\s+cards?|cards?)\b"
            r"[^.]{0,35}\b(?:and|or)\b[^.]{0,25}(?:mobile\s+)?payments?\b",
            lower,
        ):
            continue
        # "Payment may be made by bank transfer or credit card" / "Payment can
        # be made by credit card or bank transfer at the store" states the
        # methods a party may use to pay (an invoicing/collection detail), not
        # a payment term fixing an amount, due date or tranche.  "We accept
        # most major credit cards for payment" is the same genre with the
        # credit-card noun pulled to the front.  Genuine payment terms under
        # this rule name the buyer / invoice / purchase price and still fire
        # (zero fixture lines mention "may/can be made by" or "for payment" —
        # checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:payment|payments?)\s+(?:may|can)\s+be\s+made\s+by\b[^.]{0,40}"
            r"\b(?:bank\s+transfer|credit\s+card|card\b|wire|cheque\w*|"
            r"check\b|paypal)\b",
            lower,
        ) or re.search(
            r"\baccept\w*\b[^.]{0,30}\b(?:credit\s+cards?|debit\s+cards?)\b"
            r"[^.]{0,25}\bfor\s+payment\b",
            lower,
        ):
            continue
        # "The payment gateway charges a processing fee of 2%" describes a
        # payment-services tariff (the gateway's charge), not a payment term
        # between the contracting parties.  "The registration fee payable to
        # the agency is EUR 200" is a statutory/agency clearance charge, and
        # "Stamp duty on the deed is payable within 30 days" is a tax levy.
        # Genuine payment terms (invoice/net/within frames) still fire.
        if re.search(
            r"\b(?:processing|registration)\s+fee\w*\b"
            r"|\bstamp\s+duty\b[^.]{0,40}\bpayable\b",
            lower,
        ):
            continue
        # "The fine is payable within 14 days of the notice" is a statutory /
        # regulatory levy (a fine imposed by law), not a payment term between
        # the parties — the "within N days" window here measures the deadline
        # to settle a fine, not an invoice.  Both word orders of the
        # fine-payable frame are covered; a genuine invoice payment term
        # ("The invoice must be paid within 14 days of receipt") still fires —
        # it has no fine noun.  Zero fixture lines pair a fine with payable
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bfines?\b[^.]{0,45}\bpayable\b"
            r"|\bpayable\b[^.]{0,45}\bfines?\b",
            lower,
        ):
            continue
        # find a specific financial term
        pct = _PERCENT_RE.search(sentence)
        amount = _AMOUNT_RE.search(sentence)
        net = re.search(r"\bnet\s+(\d+)\s+days?\b", lower)
        within = re.search(r"\bwithin\s+(\d+)\s+days?\b", lower)
        risk = RiskLevel.LOW
        detail_parts: list[str] = []
        flag_parts: list[str] = []
        if net:
            if int(net.group(1)) > 45:
                risk = RiskLevel.MEDIUM
            detail_parts.append(f"net {net.group(1)} days")
            flag_parts.append(net.group(0))
        if within:
            if int(within.group(1)) > 45:
                risk = RiskLevel.MEDIUM
            detail_parts.append(f"within {within.group(1)} days")
        if amount:
            detail_parts.append(amount.group(0).strip())
            flag_parts.append(amount.group(0).strip())
        if pct and any(k in lower for k in ("payment", "payable", "instalment", "installment")):
            if float(pct.group("pct")) >= 30:
                risk = RiskLevel.MEDIUM
            detail_parts.append(f"{pct.group('pct')}% tranche")
            flag_parts.append(f"{pct.group('pct')}%")
        if re.search(r"\b\d{1,4}\s+(?:monthly|weekly|quarterly|annual)?\s*instal+ments?\b", lower):
            detail_parts.append("instalment schedule")
            flag_parts.append("instalments")
        if any(k in lower for k in ("advance", "in advance", "prepay", "upfront")):
            risk = RiskLevel.MEDIUM
            detail_parts.append("advance payment")
        if "recurring" in lower or "credit card" in lower:
            risk = RiskLevel.MEDIUM
            detail_parts.append("automatic recurring billing")
        if "retention" in lower:
            risk = RiskLevel.MEDIUM
            detail_parts.append("retention money")
        # still treat as a payment clause if we have any signal
        if not (detail_parts or net or within or flag_parts):
            continue
        flag = "payment terms: " + "|".join(filter(None, flag_parts)) if flag_parts else "payment terms present"
        detail = "; ".join(detail_parts) if detail_parts else None
        out.append(Match("payment", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 7  penalty
# ---------------------------------------------------------------------------


def rule_penalty(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "penalty", "penalties", "liquidated damages", "default damages",
            "breach fee", "late payment", "late fee", "interest of", "interest rate",
            "delay damages", "damages for delay",
        )):
            continue
        # Negated penalty statements ("no penalty shall apply", "penalties do
        # not apply", "not be subject to any penalty") must not fire the
        # clause.  A cap ("no penalty shall exceed X") is NOT a negation — the
        # penalty is still risk-relevant.
        if re.search(
            r"\b(?:no|not|without|neither)\b[^.]{0,60}"
            r"\b(?:penalty|penalties|liquidated damages|default damages|breach fee|"
            r"late fee|delay damages|damages for delay)\b[^.]{0,30}"
            r"\b(?:appl(?:y|ies|ied)|impos(?:e|ed|es)|charge[ds]?|incurr?|assess(?:ed)?|payable|liable)\b"
            r"|\b(?:penalty|penalties|liquidated damages)\b[^.]{0,30}"
            r"\b(?:does\s+not|do\s+not|shall\s+not|will\s+not)\s+apply\b"
            r"|\b(?:not|no|neither|without)\b[^.]{0,50}\bsubject\b[^.]{0,30}"
            r"\b(?:to\s+)?(?:any\s+)?(?:penalty|penalties|liquidated damages|default damages|"
            r"breach fee|late fee|late payment|delay damages|damages for delay)\b"
            r"|\b(?:cancelled?|canceled|terminated|withdrawn|amended|delayed|paid|"
            r"renewed|extended|waived)\s+without\s+(?:any\s+)?"
            r"(?:penalty|penalties|liquidated damages|default damages|breach fee|late fee)"
            r"|\bwithout\s+(?:any\s+)?(?:penalty|penalties|liquidated damages)\b[^.]{0,30}"
            r"\b(?:may|shall|will|can|could)\s+be\s+(?:cancelled?|canceled|terminated|"
            r"withdrawn|paid|renewed)\b",
            lower,
        ):
            continue
        # "Any penalty clause ... shall be void and unenforceable under
        # applicable law" denies contractual validity of penalty clauses —
        # a legal-enforceability statement, not a default penalty.  Genuine
        # quantified penalties ("a late payment penalty of 2% per month") still
        # fire.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:void|voidable|unenforce\w*|invalid\w*|not\s+enforce\w*|"
            r"not\s+be\s+impos(?:ed|able))\b"
            r"|\b(?:void|voidable|unenforce\w*|invalid\w*)\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "subject to prosecution and penalties under applicable law" and
        # "disciplinary penalties for breach of policy" are statutory or
        # disciplinary sanctions imposed by an authority — not contractual
        # default penalties for a breach.  "compliance penalty" (a fine
        # levied for failing a regulatory / inspection requirement) is the
        # same family.
        if re.search(
            r"\b(?:prosecution|criminal|fraud"
            r"|indictment|offen[cs]e(?:s)?|misrepresent\w*)\b[^.]{0,60}\bpenalt"
            r"|\b(?:statutory|regulatory|regulator|administrative|civil|"
            r"disciplinary|government(?:al)?|sanction(?:s|ed)?|compliance|"
            r"enforcement)\b[^.]{0,40}"
            r"\b(?:penalt(?:y|ies)?|fines?)\b"
            r"|\b(?:penalty|penalties)\b[^.]{0,50}"
            r"\b(?:under\s+(?:applicable|statutory|relevant)\s+law\b"
            r"|as\s+(?:so\s+)?provided\s+by\s+law\b"
            r"|imposed\s+by\s+(?:a\s+|the\s+|the\s+relevant\s+)?"
            r"(?:court|tribunal|regulator|authority|government|commission|agency)\b)",
            lower,
        ):
            continue
        # A sports / contest "penalty" ("penalty of 5 goals in the event of a
        # tie") is a game score, not a contractual default penalty.  Zero
        # fixture lines use these terms (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:goals?|tie|match|tournament|league|fixture|scor(?:e|ed|ing)|"
            r"red\s+card|free\s+kick|touchdown|wickets?|overs?)\b",
            lower,
        ):
            continue
        # "The player was shown a yellow card for the penalty" / "The penalty
        # kick was converted by the striker" / "The referee awarded a penalty
        # shot after the foul" are on-field sports sanctions (a card, kick or
        # shot as game events), not a contractual default penalty.  The guard
        # extends the sports family above (goals/match/tournament) to the
        # player-level event nouns.  Zero fixture lines use these (checked
        # against fixtures/generate_fixtures.py); a genuine quantified breach
        # penalty ("a late payment penalty of 2% per month") stays untouched.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:yellow\s+card|kick|corners?|shot|shots|strikers?|referees?|"
            r"players?|fouls?|tackles?|pitch|converted|spotted\s+the\s+ball)\b"
            r"|\b(?:yellow\s+card|red\s+card|referees?|umpires?|strikers?)\b"
            r"[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The penalty for late payment of the parking ticket is EUR 40" is a
        # municipal parking / traffic ticket fine (a payment penalty levied on
        # a ticket), not a contractual default penalty for a breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,40}\b(?:parking|traffic)\s+ticket\w*\b"
            r"|\b(?:parking|traffic)\s+ticket\w*\b[^.]{0,40}\bpenalt\w*\b"
            r"|\b(?:parking|street)?\s*(?:fine|fines)\b[^.]{0,35}\bpenalt\w*\b"
            r"|\bpenalt\w*\b[^.]{0,35}\b(?:parking\s+fine|fixed\s+penalty\s+notice|"
            r"parking\s+charge\w*)\b",
            lower,
        ):
            continue
        # "Drivers who park illegally risk a penalty of 100 pounds" expresses
        # an idiomatic exposure to a municipal fine ("risk a penalty"), not a
        # negotiated default penalty for a contractual breach.  Genuine
        # quantified breach penalties (fixture lines for "shall pay a penalty
        # of 1.5% per month", "penalties for late delivery apply at the rate
        # of 0.5% per day", "a penalty of 3% of the fees applies per week";
        # checked against fixtures/generate_fixtures.py) never use the
        # "risk" word and stay untouched.
        if re.search(
            r"\b(?:risks?|risked|risking)\b[^.]{0,35}\bpenalt\w*\b"
            r"|\bpenalt\w*\b[^.]{0,35}\b(?:risks?|risked|risking)\b",
            lower,
        ):
            continue
        # "The tax penalty for late filing shall be 5% of the tax due" is a
        # statutory revenue-filing sanction imposed on a taxpayer — not a
        # negotiated default penalty for a contractual breach.  A genuine
        # late-payment breach penalty ("late payment penalty of 2% per
        # month") has no tax / filing / returns context and stays untouched.
        if re.search(
            r"\b(?:tax|vat|duty|customs|excise|revenue|returns?|"
            r"late\s+(?:filing|submission)|filing|e-?filing)\b[^.]{0,45}"
            r"\bpenalt"
            r"|\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:tax|vat|duty|customs|excise|revenue|returns?|"
            r"late\s+(?:filing|submission)|filing|e-?filing)\b",
            lower,
        ):
            continue
        # "Under penalty of perjury, the declarant certifies ..." is the oath
        # formula of a legal declaration, not a contractual default penalty.
        if re.search(
            r"\bpenalty\s+of\s+perjury\b",
            lower,
        ):
            continue
        # "Night-shift penalty rates of 150 percent shall apply under the
        # award" is a statutory/industrial wage-premium rate (employment law),
        # not a negotiated default penalty for a contractual breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,40}"
            r"\b(?:night[- ]?shift|overtime|penalty\s+rates?|award|wages?|"
            r"hourly\s+rates?|industrial\s+award|workers)\b"
            r"|\b(?:night[- ]?shift|overtime|penalty\s+rates?|award|wages?|"
            r"hourly\s+rates?|industrial\s+award|workers)\b[^.]{0,40}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The penalty rates set out in the tariff schedule apply to late
        # filings" references an administrative rate schedule (a tariff /
        # price book of charges), not a negotiated default penalty for a
        # contractual breach.  The guard requires the penalty word bound to
        # a rate noun and a schedule-class noun, so a genuine per-breach
        # penalty ("penalties for late delivery apply at the rate of 0.5%
        # per day of the contract value" — fixture line 98, which has no
        # tariff / price-list / fee-schedule noun) still fires.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,15}\brates?\b[^.]{0,30}"
            r"\b(?:tariff\w*|price\s+list|price\s+schedule|fee\s+schedule|"
            r"rate\s+schedule|charges?\s+schedule)\b"
            r"|\b(?:tariff\w*|price\s+list|price\s+schedule|fee\s+schedule|"
            r"rate\s+schedule|charges?\s+schedule)\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The Club shall pay a transfer penalty of EUR 5 million to the
        # football association" is a sports-federation sanction on a club
        # (transfer rules), not a default penalty for a contractual breach.
        # "The penalty phase of the trial begins Monday" describes a stage of
        # a court / legal procedure (the sentencing phase), not a penalty a
        # party must pay for breach.  Genuine contractual penalties are never
        # a trial-phase noun.
        if re.search(
            r"\bpenalt\w*\s+(?:phase|stage)\b",
            lower,
        ):
            continue
        if re.search(
            r"\btransfer\s+(?:fee|penalty)\b",
            lower,
        ) and re.search(
            r"\b(?:club\b|football|soccer|federation|association|player\b|"
            r"agent\b|transfer\s+window|fixtures?)\b",
            lower,
        ):
            continue
        # "An early withdrawal penalty of 3% of the principal shall apply to
        # deposits withdrawn before maturity" is a banking product term (a
        # CD / deposit early-withdrawal fee), not a default penalty for a
        # contractual breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}\bearly\s+withdraw\w*\b"
            r"|\bearly\s+withdraw\w*\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ) and re.search(
            r"\b(?:deposit\w*|principal|maturity|savings|account\b|bank\w*|"
            r"certificate\s+of\s+deposit)\b",
            lower,
        ):
            continue
        # "A prepayment penalty of 1% shall apply to any early settlement of
        # the Loan" is a loan yield-protection charge (the borrower pays to
        # settle the facility early), not a default penalty for a contract
        # breach.  A genuine late-payment / liquidated-damages penalty has no
        # loan / borrow / settlement context and stays untouched.
        if re.search(
            r"\b(?:prepay\w*|pre[- ]?paid|early\s+(?:settlement|repayment|redemption))\b"
            r"[^.]{0,45}\b(?:penalt\w*|fee|charge\w*|damages?)\b"
            r"|\b(?:penalt\w*|fee|charge\w*|damages?)\b[^.]{0,45}"
            r"\b(?:prepay\w*|pre[- ]?paid|early\s+(?:settlement|repayment|redemption))\b",
            lower,
        ) and re.search(
            r"\b(?:loan\w*|borrow\w*|financ\w*|mortgage\w*|debt\w*|facilit\w*|"
            r"credit\w*|bank\w*|principal\b)\b",
            lower,
        ):
            continue
        # "A penalty of 3 points shall be deducted from the licence for the
        # traffic violation" is an administrative demerit-points sanction on a
        # licence (points deducted), not a contractual default penalty for a
        # breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,40}(?:\b\d+\s+)?\bpoints?\b"
            r"|\b(?:demerit|points?)\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ) and re.search(
            r"\b(?:licen[cs]e|permit|registration|parking|traffic|driver|"
            r"speeding|offen[cs]e|violation|conviction|endorsement\w*)\b",
            lower,
        ):
            continue

        # "The company faces a penalty of 4% of turnover for the breach" is a
        # statutory / regulatory revenue-share sanction (a percentage of the
        # undertaking's turnover), not a negotiated default penalty for a
        # contractual breach.  A genuine contract penalty quantifies the
        # penalty itself ("a late payment penalty of 2% per month") and keeps
        # firing.  The statutory/regulator family above already covers
        # "from the securities regulator" framings.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,50}"
            r"\b(?:\d+(?:\.\d+)?\s*%\s+of)?\s*turnover\b",
            lower,
        ):
            continue
        # "An employee who violates the safety policy shall incur a penalty
        # of one day's unpaid leave" is an employment / HR disciplinary
        # measure (internal policy enforcement), not a contractual default
        # penalty for a goods/services breach.  The guard needs BOTH an
        # internal work-context subject AND an internal-policy discipline
        # signal, so a genuine "penalty of 1.5% per month on overdue
        # invoices" or a named late-payment penalty stays untouched.
        if re.search(
            r"\b(?:employee\w*|staff\b|worker\w*|personnel\b)\b[^.]{0,80}"
            r"\bpenalt\w*\b"
            r"|\bpenalt\w*\b[^.]{0,80}"
            r"\b(?:employee\w*|staff\b|worker\w*|personnel\b)\b",
            lower,
        ) and re.search(
            r"\b(?:violat\w*|safety\s+policy|code\s+of\s+conduct|workplace\b|"
            r"unpaid\s+leave|suspension|discipline\w*)\b",
            lower,
        ):
            continue
        # "The parties agree that the penalty clause in clause 14 is reasonable
        # and proportionate" is a recital/enforceability statement about a
        # penalty clause, not a default penalty with rate or amount.  A
        # quantified penalty ("a late payment penalty of 2% per month") still
        # fires.
        if re.search(
            r"\bpenalt\w*\s+clause\b[^.]{0,60}"
            r"\b(?:reasonable|proportionate|enforceable|valid|fair|"
            r"not\s+(?:a\s+)?penalt\w*)\b"
            r"|\b(?:reasonable|proportionate|enforceable|valid|fair)\b[^.]{0,60}"
            r"\bpenalt\w*\s+clause\b",
            lower,
        ) and not (
            _AMOUNT_RE.search(lower)
            or re.search(
                r"\b(?:\d+(?:\.\d+)?\s*%|interest\s+rate|per\s+month|per\s+day)\b",
                lower,
            )
        ):
            continue
        # "Failure to give the required notice shall attract a penalty of one
        # month's salary" is an employment remedy (a wage sanction for
        # insufficient notice), not a negotiated default penalty for a
        # goods/services breach.  The guard requires BOTH a salary anchor on
        # either side of the penalty word AND an employment / notice /
        # dismissal context bound to the sentence, so a genuine quantified
        # contractual penalty ("a late payment penalty of 2% per month" — no
        # salary) or a "penalty of one month's rent" lease charge stays
        # untouched.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,40}\bsalary\b"
            r"|\bsalary\b[^.]{0,40}\bpenalt\w*\b",
            lower,
        ) and re.search(
            r"\b(?:employ\w*|notice\b|dismiss\w*|redundan\w*|severance|"
            r"resign\w*|terminat\w+\s+employment|month'?s?\s+wages?|"
            r"month'?s?\s+salary|week'?s?\s+(?:salary|wages?))\b",
            lower,
        ):
            continue
        # "A penalty of 5 points applies for each incorrect answer in the
        # quiz" is a competition / exam scoring rule (points for wrong
        # answers), not a contractual default penalty.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,75}"
            r"\b(?:quiz\w*|contest\w*|competition\w*|exam\w*|answ\w*\s+sheet)\b",
            lower,
        ):
            continue
        # "The team won the penalty shootout after extra time" is football
        # match-deciding procedure, not a contractual default penalty.  The
        # sports guard above already covers goals/match/league scoring, but a
        # shootout-specific frame is common.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,25}\bshootout\b",
            lower,
        ):
            continue
        # "The team was awarded a free throw after the penalty" is an
        # on-court/on-field game-event sanction (a free throw, free kick or
        # shot awarded by officials), not a contractual default penalty.  The
        # guard extends the sports family above (goals/match/shootout) to the
        # award mechanics.  Zero fixture lines use these (checked against
        # fixtures/generate_fixtures.py); a genuine quantified breach penalty
        # stays untouched.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,40}\b(?:free\s+throw|free\s+kick|"
            r"penalt\w*\s+shot|spot\s+kick|throw-?ins?)\b"
            r"|\b(?:free\s+throw|free\s+kick)\b[^.]{0,40}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The bond is subject to a call protection penalty of 1%" / "Lenders
        # charge an early-exit penalty on fixed-rate mortgages" is a financial-
        # instrument charge (a yield-protection or exit fee on a debt
        # security), not a contractual default penalty for a breach.  The
        # guard requires the financial noun bound to the penalty word on
        # either side, so a genuine contract penalty ("penalty of 1.5% per
        # month of the invoice amount" — fixture line 83) stays untouched.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,50}"
            r"\b(?:call\s+protection|fixed-?rate|mortgage\w*|"
            r"redemption|yield|bond\w*|note\w*)\b"
            r"|\b(?:call\s+protection|fixed-?rate|mortgage\w*|"
            r"redemption|yield|bond\w*|note\w*)\b[^.]{0,50}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The new law doubles the penalty for insider trading" / "The
        # company faces a civil penalty from the securities regulator" is a
        # market-conduct or securities-law sanction (insider trading, market
        # abuse, listing breaches) imposed against a market participant —
        # not a default penalty for a contractual breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,50}"
            r"\b(?:insider\s+trading|market\s+abuse|securities\w*|"
            r"stock\s+exchange|trading\s+rules?|listing\s+rules?)\b"
            r"|\b(?:insider\s+trading|market\s+abuse|securities\w*|"
            r"stock\s+exchange|trading\s+rules?|listing\s+rules?)\b[^.]{0,50}"
            r"\bpenalt\w*\b",
            lower,
        ):
            continue
        # "A penalty of 3% is charged on the redeemed units" is an investment
        # fund redemption fee (cancellation charge on units sold back), not a
        # contractual default penalty for a breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}\bredeem\w*\b"
            r"|\bredeem\w*\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The policy pays a penalty for late renewal of the insurance" is an
        # insurance-continuation surcharge payable for not renewing a policy on
        # time, not a contractual default penalty for a breach.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,50}\blate\s+renew\w*\b[^.]{0,30}\binsurance\b",
            lower,
        ):
            continue
        # "The penalty for the offence is a term of imprisonment" / "The
        # contract attracts a penalty of imprisonment" is a criminal-law
        # sanction (the statutory guard above covers offence-then-penalty word
        # order; this reverse-order leg closes the penalty-then-offence gap and
        # adds an imprisonment/jail/prison frame).  A genuine quantified breach
        # penalty ("a late payment penalty of 2% per month") has no criminal
        # vocabulary and stays untouched.  Zero fixture lines pair penalty with
        # imprisonment (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:offen[cs]e\w*|crime\w*|criminal\b|conviction\w*|"
            r"imprison\w*|jail\b|prison\b)\b"
            r"|\b(?:imprison\w*|jail\b|prison\b)\b[^.]{0,60}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "The penalty point ends the game for the losing team" is a
        # sports/contest scoring or match-procedure reference (penalty points,
        # game outcome), not a contractual default penalty.  The guard needs
        # the penalty word bound to a game/team/point/result noun on either
        # side; a genuine contract penalty ("late payment penalty of 2% per
        # month") has none of those nouns and stays untouched.  The licence
        # demerit-points guard above remains separate (it adds a
        # licence/permit context).
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:game\w*|team\w*|point\w*|win\w*|los\w*|score\w*)\b"
            r"|\b(?:game\w*|team\w*|point\w*|win\w*|los\w*)\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "Charging extra attracts a 5% late fee from the bank" is a bank's
        # customer-charges frame (the bank levies the fee on the customer's
        # account), not a contractual default penalty between the parties.
        # A genuine breach fee ("a late payment fee of 2% per month" — fixture
        # line 83) stays untouched because it has no bank / account context.
        if re.search(
            r"\b(?:late\s+fee|late\s+fees|late\s+payment|penalt\w*)\b[^.]{0,50}"
            r"\b(?:from\s+the\s+bank|charged\s+by\s+(?:the\s+)?bank\b|by\s+the\s+bank)\b"
            r"|\b(?:bank\s+charges?|bank\s+fee|bank's?\s+fee|account\s+charges?)\b"
            r"[^.]{0,45}\b(?:late\s+fee|late\s+fees|penalt\w*)\b",
            lower,
        ):
            continue
        # "A late fee of USD 25 applies after the due date" is a
        # late-payment-charge notice framed by the due date (a billing /
        # consumer fee), not a contractual default penalty.  The guard
        # requires a "late fee" bound to a due-date phrase within the clause;
        # a genuine quantified breach penalty ("a late payment penalty of 2%
        # per month" — fixture line 83) has no due-date framing and still
        # fires.  Zero fixture lines combine a late fee with a due date
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\blate\s+fees?\b[^.]{0,45}\bdue\s+date\b"
            r"|\bdue\s+date\b[^.]{0,45}\blate\s+fees?\b",
            lower,
        ):
            continue
        # "The penalty rate charged by the central bank rose to 4%" is a
        # monetary-policy benchmark (the central bank's policy rate), not a
        # contractual default penalty.  A genuine quantified breach penalty
        # ("a late payment penalty of 2% per month") has no central-bank
        # context and stays untouched.
        if re.search(
            r"\bpenalt\w*\s+rate\b[^.]{0,50}\bcentral\s+bank\b"
            r"|\bcentral\s+bank\b[^.]{0,50}\bpenalt\w*\s+rate\b",
            lower,
        ):
            continue
        # "The penalty rate on the credit card is 25%" is consumer-card
        # pricing (the card issuer's interest/APR), not a contractual default
        # penalty between the parties.  A genuine quantified breach penalty
        # ("a late payment penalty of 2% per month" — fixture line 83) has no
        # credit-card / APR / lending context and stays untouched.
        if re.search(
            r"\bpenalt\w*\s+rate\b[^.]{0,50}"
            r"\b(?:credit\s+card\w*|card\s+issuer\w*|\bAPR\b|lending\w*)\b"
            r"|\b(?:credit\s+card\w*|card\s+issuer\w*|\bAPR\b)\b[^.]{0,50}"
            r"\bpenalt\w*\s+rate\b",
            lower,
        ):
            continue
        # "Late books incur a small penalty at the public library" is a
        # library lending fine, not a contractual default penalty.  A genuine
        # breach penalty ("a late payment penalty of 2% per month shall apply
        # to overdue invoices") has no library / book context and stays
        # untouched (overdue is a payment word, not a library marker).
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}"
            r"\b(?:librar\w+|book\b|books\b|librarian\w*)\b"
            r"|\b(?:librar\w+|book\b|books\b|librarian\w*)\b[^.]{0,45}\bpenalt\w*\b",
            lower,
        ):
            continue
        # "Drivers face a penalty for using a phone at the wheel" is a
        # road-traffic offence, not a contractual default penalty.  A genuine
        # breach penalty ("a late payment penalty of 2% per month") has no
        # driver / phone / driving context and stays untouched.
        if re.search(
            r"\bpenalt\w*\b[^.]{0,45}\b(?:drivers?\b|motorists?\b|behind\s+the\s+wheel\b|"
            r"\bat\s+the\s+wheel\b|road\s+safety\b)\b",
            lower,
        ):
            continue
        pct = _PERCENT_RE.search(sentence)
        amount = _AMOUNT_RE.search(sentence)
        risk = RiskLevel.MEDIUM
        flag = "penalty clause present"
        detail = None
        # "the sums are liquidated damages and not a penalty" is boilerplate
        # characterization with no amount or rate — not a penalty clause.  Only
        # the no-detail fallback is guarded; a quantified penalty still fires.
        if not pct and not amount and re.search(
            r"\bliquidated damages\b[^.]{0,50}\bnot\s+a\s+penalty\b", lower
        ):
            continue
        is_interest = re.search(r"\b(interest|default interest)\b", lower) is not None
        is_penalty = re.search(r"\b(penalty|penalties|liquidated damages|delay damages|damages for delay|breach fee|breach fees|late fee|late fees|late payment|late payments)\b", lower) is not None
        # A negated accrual statement ("interest shall not accrue", "no interest
        # will accrue on late payments") states what will NOT happen — not a
        # penalty risk.  Genuine accrual clauses ("interest of 2% per month
        # shall accrue" / "default interest accrues") still fire.
        if re.search(
            r"\b(?:interest|late\s+fee|penalty)\b[^.]{0,50}\b(?:shall\s+)?not\s+accrue\b"
            r"|\bno\s+(?:interest|late\s+fee|penalty)\b[^.]{0,60}\b(?:shall\s+)?accrue\b",
            lower,
        ):
            continue
        # A rate-less interest mention with no late/breach-context tokens
        # ("the interest rate shall apply for the remaining period of the
        # loan") is loan pricing, not a default penalty.  Sentences that name
        # late payment / penalty / liquidated damages (is_penalty) still fire.
        if is_interest and not is_penalty and not pct and not amount:
            continue
        if pct:
            p = float(pct.group("pct"))
            if is_interest and not is_penalty:
                # "interest of 9.5% per annum", "interest rate is 6%" — financial
                # loan pricing, not a default penalty for a breach.
                continue
            if p >= 10:
                risk = RiskLevel.HIGH
                flag = f"penalty {pct.group(0)} (onerous)"
            elif any(u in lower for u in ("per month", "per week", "per day", "/month", "/week", "/day")):
                risk = RiskLevel.MEDIUM
                flag = f"penalty {pct.group(0)} per period"
            else:
                risk = RiskLevel.MEDIUM
                flag = f"penalty {pct.group(0)}"
            detail = pct.group(0)
        elif amount:
            flag = f"penalty amount {amount.group(0)}"
            risk = RiskLevel.MEDIUM
            detail = amount.group(0).strip()
        if re.search(r"\b(cap|limited|maximum|ceiling)\b", lower):
            flag += " (capped)"
            risk = RiskLevel.LOW if risk != RiskLevel.HIGH else risk
        out.append(Match("penalty", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 8  liability cap
# ---------------------------------------------------------------------------


_CAP_MARK = re.compile(
    r"\b(?:cap|capped|caps|limit|limited|maximum|ceiling|not\s+exceed|up\s+to|shall\s+not\s+exceed)\b",
    re.IGNORECASE,
)
_BROAD_DISCLAIM = re.compile(
    r"(?:disclaims?\s+all\s+warranties|without\s+limitation|not\s+be\s+liable\s+for\s+any\s+damages|"
    r"no\s+liability\s+whatsoever|shall\s+not\s+be\s+liable\s+for\s+any|disclaims\s+all\s+liability|"
    r"not\s+be\s+liable\s+for\s+any)",
    re.IGNORECASE,
)
_EXCLUSION_ONLY = re.compile(
    r"(?:indirect|incidental|consequential|special|punitive|loss\s+of\s+profit)\s+damages?",
    re.IGNORECASE,
)


def rule_liability(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        # Insurance-requirement sentences ("professional indemnity insurance",
        # "general liability insurance with limits of at least USD 1M") are
        # coverage obligations handled by rule_insurance, not party-to-party
        # liability caps.  The verb/agent forms covered include "insured" /
        # "insurer" / "insuring" ("the company insured its directors against
        # liability claims", "the bank insured the loan against the default
        # risk"), which carry no liability-cap semantics.  A genuine indemnity
        # obligation ("indemnifies", "hold harmless") still fires.
        if re.search(r"\binsur\w*\b", lower) and not re.search(
            r"\bindemnif|hold\s+harmless", lower
        ):
            continue
        if not any(k in lower for k in (
            "liability", "aggregate liability", "maximum liability", "indemn", "cap on damages",
            "not be liable for", "be liable for", "disclaims all", "no liability",
        )):
            continue
        # "The vendor indemnified the buyer against third-party claims last
        # year", "The insurance company indemnified the policyholder after
        # the fire" narrate a past compensation event (someone already paid
        # or reimbursed someone else), not an ongoing inter-party indemnity
        # obligation.  The past-tense "indemnified" pins the narration frame;
        # obligation auxiliaries are excluded so a genuine "shall be
        # indemnified" duty stays live.  The auxiliary leg deliberately lists
        # finite verb forms ("agrees", "agreed", "agreeing") rather than the
        # prefix "agree" so the noun "agreement" ("under the previous
        # agreement") does not cancel the guard.  Zero fixture lines use the
        # past tense (fixture line 269 "Provider indemnifies Client" is
        # present tense; checked against fixtures/generate_fixtures.py).
        if re.search(r"\bindemnif\w*ed\b", lower) and not re.search(
            r"\b(?:shall|will|must|may|agrees?\b|agreed\b|agreeing\b|"
            r"undertak\w*|oblig\w*|require\w*)\b",
            lower,
        ):
            continue
        # "Any indemnified party shall cooperate with the insurer in the
        # defence" is a cooperation covenant tethered to a hypothetical
        # indemnity (a party who is indemnified must assist the insurer),
        # not an agreed indemnity obligation between the parties.  The
        # guard requires "indemnified" and "cooperate" bound within the
        # clause in either order; a genuine inter-party indemnity
        # ("The Supplier shall indemnify the Customer against claims arising
        # from the Services", fixture line 269) has no cooperation verb and
        # still fires.  Zero fixture lines pair indemnified with cooperate
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bindemnif\w*ed\b[^.]{0,60}\bcooperat\w*\b"
            r"|\bcooperat\w*\b[^.]{0,60}\bindemnif\w*ed\b",
            lower,
        ):
            continue
        # "The company indemnifies its directors against legal costs" is a
        # corporate-governance posture (a company agrees to reimburse its own
        # officers' defence costs), not an inter-party indemnity obligation.
        # The guard requires the indemnify verb bound to an internal
        # management noun; obligation auxiliaries are excluded so a genuine
        # "shall indemnify the directors" duty stays live.  The auxiliary leg
        # deliberately lists finite verb forms ("agrees", "agreed",
        # "agreeing") rather than the prefix "agree" so the noun "agreement"
        # cannot cancel the guard.  Genuine inter-party indemnities
        # ("Provider indemnifies Client against all claims arising from staff
        # misconduct", fixture line 269) have no director/officer noun and
        # still fire.  Zero fixture lines pair indemnify with a
        # director/officer noun (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bindemnif\w*\b[^.]{0,40}"
            r"\b(?:directors?|officers?|trustees?|board\s+members?)\b"
            r"|\b(?:directors?|officers?|trustees?|board\s+members?)\b"
            r"[^.]{0,40}\bindemnif\w*\b",
            lower,
        ) and not re.search(
            r"\b(?:shall|will|must|may|agrees?\b|agreed\b|agreeing\b|"
            r"undertak\w*|oblig\w*|require\w*)\b",
            lower,
        ):
            continue
        # A force-majeure event ("neither party shall be liable for damages
        # caused by a force majeure event") relaxes one narrow excuse, and
        # "each party shall be liable for its own taxes and duties" allocates
        # statutory charges — neither is a liability-cap risk.
        if re.search(r"\bforce\s+majeure\b", lower):
            continue
        if re.search(r"\bliable\s+for\s+its\s+own\b", lower):
            continue
        # "The employer may be liable for harassment by its staff" states a
        # general vicarious-employer exposure to a tort committed by staff,
        # not an agreed liability cap; "Occupiers are liable to visitors under
        # the Occupiers Liability Act" restates a statutory duty of the
        # occupier.  Zero fixture lines use harassment/occupier (checked
        # against fixtures/generate_fixtures.py); genuine caps, disclaimers
        # and indemnities still fire.
        if re.search(r"\bliab\w*\b[^.]{0,60}\bharass\w*\b", lower):
            continue
        if re.search(r"\boccupiers?\b[^.]{0,60}\bliab\w*\b", lower):
            continue
        # "Each party shall be liable for the acts and omissions of its own
        # personnel" allocates responsibility for one's own conduct — the
        # mirror-image of the "its own taxes/duties" allocation above, not an
        # agreed cap, disclaimer or exclusion.  Both word orders are covered.
        # Genuine caps and disclaimers still fire.
        if re.search(
            r"\bliable\b[^.]{0,35}\b(?:its|their)\s+own\b"
            r"|\bacts?\s+and\s+omissions?\b[^.]{0,45}\bliable\b",
            lower,
        ):
            continue
        # "the parties acknowledge that product liability for defective goods
        # follows the manufacturer" restates a statutory allocation — no cap,
        # disclaimer or exclusion is being agreed.  Genuine caps ("aggregate
        # liability shall not exceed USD 1M") and disclaimers still fire.
        # Both directions: "the parties acknowledge that product liability
        # for defective goods follows the manufacturer" and "the Supplier
        # shall be liable to consumers for defective products under the
        # Product Liability Act" restate statutory allocation.
        if re.search(
            r"\bproduct\s+liability\b[^.]{0,45}"
            r"\b(?:defect\w*|manufacturer|consumer|statutory|"
            r"shall\s+follow|rests?\s+with|remains?\s+with)\b"
            r"|\b(?:defect\w*|consumer\w*|liable\s+to\b|manufacturer)\b"
            r"[^.]{0,45}\bproduct\s+liability\b",
            lower,
        ):
            continue
        # "Acceptance of the Works shall be deemed given upon the expiry of
        # the defects liability period" — the defects liability period (DLP)
        # is the post-acceptance warranty window common in construction
        # contracts, not an agreed cap, disclaimer or exclusion on liability.
        # Genuine caps ("aggregate liability shall not exceed USD 1M") still
        # fire.
        if re.search(r"\bdefects?\s+liability\s+period\b", lower):
            continue
        # "The company's liability in tort shall be governed by the applicable
        # statute" allocates substantive law (statutory text governs a cause of
        # action) — no cap, disclaimer or agreed exclusion.  Genuine agreed caps
        # ("aggregate liability shall not exceed USD 1M") still fire.
        if re.search(
            r"\bliability\b[^.]{0,45}\b(?:in\s+tort\b|in\s+negligence\b|for\s+negligence\b)"
            r"|\b(?:in\s+tort\b|in\s+negligence\b|for\s+negligence\b)[^.]{0,45}\bliability\b",
            lower,
        ) and re.search(
            r"\b(?:govern\w*|settle\w*|determine\w*|pursuant\s+to\s+(?:the\s+)?"
            r"applicable|under\s+applicable|statute|legislation|statutory\s+limits?)\b",
            lower,
        ):
            continue
        # "Each party's liability to the other shall be several, not joint"
        # allocates proportionate (several) responsibility among obligors — a
        # doctrine of joint-and-several liability, not an agreed cap,
        # disclaimer or exclusion.  Genuine caps and disclaimers still fire.
        if re.search(
            r"\bliability\b[^.]{0,40}"
            r"\b(?:several|proportionate)(?:\s+and)?,?\s+not\s+joint\b"
            r"|\b(?:several|proportionate),?\s+not\s+joint\b[^.]{0,40}\bliability\b"
            r"|\bjoint\s+and\s+several\b[^.]{0,40}\bliability\b"
            r"|\bliability\b[^.]{0,40}\bjoint\s+and\s+several\b",
            lower,
        ):
            continue
        # "Nothing in this clause creates strict liability for latent defects
        # in the Goods" / "Strict liability applies to defective products"
        # restate the strict-liability doctrine (tort) — they are not an
        # agreed cap, disclaimer or exclusion.  Genuine caps
        # ("aggregate liability shall not exceed USD 1M") and disclaimers
        # still fire.
        if re.search(
            r"\b(?:strict|absolute)\s+liability\b[^.]{0,45}"
            r"\b(?:creat\w*|impos\w*|incur\w*|aris\w*|attach\w*|result\w*|appli\w*)\b"
            r"|\b(?:creat\w*|impos\w*|incur\w*|aris\w*|attach\w*|result\w*|appli\w*)\b"
            r"[^.]{0,45}\b(?:strict|absolute)\s+liability\b",
            lower,
        ):
            continue
        # "The product is covered by a strict liability regime" states that a
        # product falls under a strict-liability legal scheme (tort doctrine —
        # typically Product Liability) as a factual/legal attribute; it is not
        # an agreed cap, disclaimer or exclusion between the parties.  The
        # guard requires a regime / passive-cover frame bound to the
        # strict/absolute liability compound (the verb guard above covers the
        # create/impose/found verbs but not the "covered by ... regime"
        # passive frame).  Zero fixture lines pair "strict/absolute liability"
        # with a regime or covered-by frame (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:strict|absolute)\s+liability\b[^.]{0,45}"
            r"\b(?:regime\w*|covered\s+by|subject\s+to)\b"
            r"|\b(?:regime\w*|covered\s+by|subject\s+to)\b[^.]{0,45}"
            r"\b(?:strict|absolute)\s+liability\b",
            lower,
        ):
            continue
        # "He admitted liability for the accident" / "The defendant's liability
        # was established at trial" / "The auditor disclaimed liability for the
        # report" report a past, factual legal outcome (an admission, a
        # judgment or a professional-capacity disclaimer) — not an agreed cap,
        # disclaimer or exclusion in the contract.  The guard requires the
        # liability word bound to a factual-outcome verb in the past /
        # passive frame (admitted, established, found, disclaimed, denied).
        # Genuine caps ("aggregate liability shall not exceed"), disclaimers
        # and exclusions between the parties still fire.  Zero fixture lines
        # report factual liability outcomes (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bliability\b[^.]{0,45}"
            r"\b(?:admit\w*|established?\b|establishe\w*|found\s+(?:liable)?\b|"
            r"disclaim\w*|denied\b|denies\b|accept\w*)\b"
            r"|\b(?:admit\w*|established?\b|establishe\w*|found\s+liable\b|"
            r"disclaim\w*|denied\b|denies\b|accept\w*)\b[^.]{0,45}\bliability\b",
            lower,
        ):
            continue
        # "Liability for the damage was disputed between the parties" reports a
        # litigation / negotiation fact (a dispute over who is at fault) — not
        # an agreed cap, disclaimer or exclusion.  Genuine caps and exclusions
        # still fire ("aggregate liability shall not exceed USD 1M" has no
        # dispute frame).
        if re.search(
            r"\bliability\b[^.]{0,45}\bdisput\w*\b"
            r"|\bdisput\w*\b[^.]{0,45}\bliability\b",
            lower,
        ):
            continue
        # "His liability under the will was limited to the estate" is a
        # testamentary / inheritance allocation (what a deceased person's
        # estate must fund), not an agreed cap in a contract.  Genuine caps
        # still fire (no will / estate / inheritance frame).
        if re.search(
            r"\bliability\b[^.]{0,45}\b(?:will\b|estate\b|testator\w*|bequeath\w*|"
            r"inheritance\w*|inherit\w*)\b"
            r"|\b(?:will\b|estate\b|testator\w*|bequeath\w*|inheritance\w*|"
            r"inherit\w*)\b[^.]{0,45}\bliability\b",
            lower,
        ):
            continue
        # "The firm's liability for the collapse is being investigated" reports
        # an ongoing inquiry (regulator / internal probe) — not an agreed cap,
        # disclaimer or exclusion.  Genuine caps and exclusions still fire
        # (no investigation / probe frame).
        if re.search(
            r"\bliability\b[^.]{0,45}\b(?:investigat\w*|prob\w*)\b"
            r"|\b(?:investigat\w*|prob\w*)\b[^.]{0,45}\bliability\b",
            lower,
        ):
            continue
        # "Nothing in this clause shall be construed as a limitation of
        # liability of the parties" is a drafting instruction about how the
        # clause is read (construed) — it is not itself an agreed cap,
        # disclaimer or exclusion.  Genuine caps and disclaimers still fire.
        if re.search(
            r"\bconstru\w*\b[^.]{0,55}\blimitation\s+of\s+liability\b"
            r"|\blimitation\s+of\s+liability\b[^.]{0,55}\bconstru\w*\b",
            lower,
        ):
            continue
        # "The parties agree not to rely on any limitation of liability in
        # respect of fraud" is a waiver of the cap's application to a
        # specific claim (the parties decline to invoke the cap), not the
        # creation of a limitation of liability.  Genuine caps
        # ("aggregate liability shall not exceed USD 1M") still fire.
        if re.search(
            r"\b(?:not\s+to\s+rely|shall\s+not\s+rely|cannot\s+rely|may\s+not\s+rely)"
            r"\s+on\b[^.]{0,50}\b(?:any\s+|a\s+)?limitation\s+of\s+liability\b"
            r"|\blimitation\s+of\s+liability\b[^.]{0,60}"
            r"\b(?:rely|relied|reliance)\b[^.]{0,40}"
            r"\b(?:fraud\w*|willful\s+misconduct|wilful\s+misconduct|"
            r"gross\s+negligence)\b",
            lower,
        ):
            continue
        # "The indemnity shall not apply to the extent prohibited by
        # applicable law" is a statutorily-mandated carve-out limiting the
        # scope of an indemnity to what the law allows — it does not agree a
        # cap, disclaimer or exclusion.  Genuine caps and disclaimers still
        # fire.
        if re.search(r"\bprohibited\s+by\b[^.]{0,30}\bapplicable\s+law\b", lower) \
                and re.search(
                    r"\b(?:not\s+apply|no\s+obligation|void\b|unenforceable|"
                    r"invalid\b|without\s+effect)\b",
                    lower,
                ):
            continue
        # "Contributory negligence by the Buyer shall reduce the Seller's
        # liability" applies a tort doctrine of comparative/apportionment of
        # fault (both word orders) — it states how a court shares damages,
        # not an agreed cap, disclaimer or exclusion.  Genuine caps and
        # disclaimers still fire.
        if re.search(
            r"\b(?:contributory\s+negligence|comparative\s+fault)\b[^.]{0,45}"
            r"\bliability\b"
            r"|\bliability\b[^.]{0,45}"
            r"\b(?:contributory\s+negligence|comparative\s+fault)\b",
            lower,
        ):
            continue
        # "Liability shall be determined in accordance with the law of the
        # place of the harmful event" is a conflicts-of-law / lex loci rule
        # naming which legal system resolves the claim (tort), not an agreed
        # cap, disclaimer or exclusion.  Genuine caps and disclaimers still
        # fire.
        if re.search(
            r"\bliability\b[^.]{0,55}"
            r"\b(?:determin\w*|govern\w*|assess\w*|appli(?:es|ed)|shall\s+be\s+"
            r"(?:determined|governed|assessed))\b[^.]{0,55}"
            r"\b(?:in\s+accordance\s+with|pursuant\s+to)\b[^.]{0,55}"
            r"\blaw\b"
            r"|\bliability\b[^.]{0,55}"
            r"\bgovern\w*\s+by\b[^.]{0,55}\blex\s+loci\b",
            lower,
        ) and re.search(
            r"\b(?:law\s+of\s+(?:the\s+)?(?:place|country|forum)|"
            r"law\s+of\s+the\s+(?:place\s+of\s+)?(?:harmful\s+event|wrong|tort)|"
            r"lex\s+loci\b)\b",
            lower,
        ):
            continue
        # "Nothing in this Agreement shall limit either party's liability for
        # death or personal injury caused by its negligence" is a statutory
        # carve-out — it *refuses* to cap the one head of liability that is
        # non-excludable by law.  That is the opposite of an agreed cap,
        # disclaimer or exclusion, yet the literal "shall limit" matches the
        # cap marker.  Genuine caps ("aggregate liability shall not exceed
        # USD 1M") and disclaimers still fire.
        if re.search(
            r"\b(?:death|personal\s+injury|fraud\b|gross\s+negligence)\b"
            r"[^.]{0,60}\bliab\w*\b"
            r"|\bliab\w*\b[^.]{0,60}"
            r"\b(?:death|personal\s+injury|fraud\b|gross\s+negligence)\b",
            lower,
        ) and re.search(
            r"\bnothing\s+in\b[^.]{0,80}\blimit\w*\b"
            r"|\b(?:shall\s+not|does\s+not|no)\s+limit\w*\b",
            lower,
        ):
            continue
        # "The Company shall be liable for the accuracy / completeness of the
        # information" holds a party responsible for the quality of the
        # information it provides — an information-accuracy representation,
        # not an agreed cap, disclaimer or exclusion.  Genuine caps and
        # disclaimers still fire.
        if re.search(
            r"\bliable\s+for\s+(?:the\s+)?(?:accuracy|completeness|correctness)\b"
            r"[^.]{0,35}\b(?:information\w*|data\b|statements?\b|reports?\b|"
            r"representations?\b|warranties?)\b",
            lower,
        ):
            continue
        # "The Parent Company shall be liable for the obligations of its
        # Subsidiary" is a parent guarantee / surety allocation (who answers
        # for another entity's debts), not an agreed cap, disclaimer or
        # exclusion imposed between parties.  Genuine caps and disclaimers
        # still fire.
        if re.search(
            r"\bliable\s+for\s+(?:the\s+)?(?:obligations?|debts?|liabilities?)\s+of\b"
            r"[^.]{0,25}\b(?:its|their|the|a)\s+(?:subsidiar\w+|affiliate\w*|"
            r"group\s+compan\w+|parent\b|associate\w*)\b",
            lower,
        ):
            continue
        # "The parties accept liability under this clause in proportion to
        # their respective shares" allocates liability proportionally among
        # obligors (each answers for its own share) — a contribution /
        # apportionment statement, not an agreed cap, disclaimer or
        # exclusion.  Genuine caps and disclaimers still fire.
        if re.search(
            r"\bliable?\s+for\b[^.]{0,20}\b(?:in\s+proportion|proportionate\w*|"
            r"according\s+to\s+(?:their|its)\s+respective)\b"
            r"|\b(?:in\s+proportion|proportionate\w*)\b[^.]{0,30}"
            r"\b(?:respective\s+)?(?:shares?|liabilities?|interests?|participations?)\b",
            lower,
        ):
            continue
        # "The Seller's liability shall reduce proportionately to the extent
        # the Buyer contributed to the loss" is a comparative-fault reduction
        # (the seller's share shrinks with the buyer's contributory conduct),
        # not an agreed cap, disclaimer or exclusion.  Genuine caps and
        # disclaimers still fire.  The gap between "reduce" and the
        # contributory noun is kept generous ("... proportionately to the
        # extent the Buyer contributed ...").
        if re.search(
            r"\blia(?:bility|ble)\b[^.]{0,50}\b(?:reduce\w*|reduction\w*|diminish\w*)"
            r"\b[^.]{0,60}\b(?:contributed?\b|contributory\b|fault\b)\b"
            r"|\b(?:contributed?\b|contributory\b|fault\b)\b[^.]{0,60}"
            r"\b(?:reduce\w*|reduction\w*|diminish\w*)\b[^.]{0,50}\bliab\w*\b",
            lower,
        ):
            continue
        # "Liability is excluded to the extent permitted by law" is a
        # statutorily-mandated saving provision (the exclusion only operates
        # where the law allows) — a legal-qualifier statement, not an agreed
        # cap, disclaimer or exclusion.  Genuine disclaimers ("The Licensor
        # disclaims all warranties") still fire.
        if re.search(
            r"\b(?:excluded|exclusion|exclude\w*)\b[^.]{0,40}"
            r"\bto\s+the\s+extent\b[^.]{0,35}\b(?:permitted\s+by|allowed\s+by|"
            r"required\s+by)\b[^.]{0,35}\blaw\b",
            lower,
        ):
            continue
        # "Criminal liability attaches to fraud under section 1" / "statutory
        # liability", "tax / VAT liability" name a legal regime under which a
        # person is prosecuted or assessed (criminal, fiscal or statutory
        # exposure), not an agreed cap, disclaimer or exclusion between the
        # parties.  Genuine caps and disclaimers still fire.  Zero fixture
        # lines join liability to a criminal/tax/penal noun (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:criminal|statutory|fiscal|tax|vat|penal)\s+liability\b"
            r"|\bliability\b[^.]{0,30}\b(?:offence|offense)\b",
            lower,
        ):
            continue
        # "The data subject has no liability for the controller's breach" is
        # a data-protection rights statement (who is accountable for what
        # under the GDPR), not an agreed cap, disclaimer or exclusion between
        # the parties.  Genuine caps and disclaimers still fire — the guard
        # requires the "data subject" noun, so a data-protection breach
        # exposure ("seller's liability for unauthorised processing of
        # personal data") stays.
        if re.search(
            r"\bdata\s+subject\w*\b[^.]{0,40}\bliab\w*\b"
            r"|\bliab\w*\b[^.]{0,40}\bdata\s+subject\w*\b",
            lower,
        ):
            continue
        # "The owner shall be liable for damage caused by their pet" is a
        # pet-owner / animal-owner tort liability (statutory duty of care for
        # one's animal), not an agreed cap, disclaimer or exclusion between
        # the parties.  Genuine caps and disclaimers still fire — the guard
        # requires the pet/animal noun near the liability verb.
        if re.search(
            r"\bliab\w*\b[^.]{0,40}\b(?:pet\w*|animals?\b|livestock\b|cattle\b|"
            r"dogs?\b|horses?\b)\b"
            r"|\b(?:pet\w*|animals?\b|livestock\b|cattle\b|dogs?\b|horses?\b)\b"
            r"[^.]{0,40}\bliab\w*\b",
            lower,
        ):
            continue
        # "Directors shall be liable for breach of their duties" is a
        # corporate-governance duty statement (officers answer for their
        # statutory duties to the company), not an agreed cap, disclaimer or
        # exclusion between the parties.  Genuine caps and disclaimers still
        # fire — the guard requires the director/officer noun bound to the
        # duties/breach frame.
        if re.search(
            r"\b(?:directors?|officers?|board\s+members?|trustees?)\b"
            r"[^.]{0,40}\bliab\w*\b[^.]{0,40}"
            r"\b(?:dut\w+|breach\w*\s+of)\b"
            r"|\bliab\w*\b[^.]{0,40}\b(?:directors?|officers?|board\s+members?|"
            r"trustees?)\b[^.]{0,40}\b(?:dut\w+|breach\w*\s+of)\b",
            lower,
        ):
            continue
        # "The auditor's professional liability shall not exceed the statutory
        # cap" / "The statutory liability of the carrier is limited by the CMR
        # Convention" restate a professional or statutory liability regime
        # (auditors, carriers) — not an agreed cap negotiated between the
        # parties.  Genuine caps ("Cap on liability shall be 100% of the
        # subcontract price", fixture 184) still fire — the guard requires the
        # professional/statutory/regulatory adjective bound to a liability
        # noun plus a cap/limit frame.  Zero fixture lines use these
        # adjectives with liability (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:professional|statutory|regulatory|legal)\b[^.]{0,25}"
            r"\bliab\w*\b[^.]{0,30}\b(?:cap\w*|limit\w*|ceiling|maximum)\b"
            r"|\bliab\w*\b[^.]{0,25}\b(?:professional|statutory|regulatory|legal)\b"
            r"[^.]{0,30}\b(?:cap\w*|limit\w*|ceiling|maximum)\b",
            lower,
        ):
            continue
        # "The employee shall be liable for any loss caused by gross
        # negligence" is an employment-law statement (an individual worker's
        # personal responsibility), not an agreed liability allocation
        # between the contracting parties.  Genuine indemnity/liability
        # clauses ("the Contractor shall be liable for loss or damage")
        # still fire — the guard requires the employee/employment noun bound
        # to gross-negligence/minor-wilful frames.  Zero fixture lines pair
        # employee with gross negligence (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:employee\w*|staff\w*|employment\b)\b[^.]{0,40}\bliab\w*\b"
            r"[^.]{0,40}\bgross\s+negligence\b"
            r"|\bgross\s+negligence\b[^.]{0,40}\bliab\w*\b[^.]{0,40}"
            r"\b(?:employee\w*|staff\w*|employment\b)\b",
            lower,
        ):
            continue
        # "The user assumes all liability for damage caused by misuse" is a
        # product-safety / end-user statement (the operator answers for
        # improper use), not an agreed cap or exclusion negotiated between the
        # parties.  "The state shall not be liable for damages arising from
        # war" restates a statutory sovereign-immunity carve-out, not a
        # contract allocation.  Genuine caps and disclaimers ("the Contractor
        # shall be liable for loss or damage", "neither party shall be liable
        # for indirect damages") still fire — the guard requires the
        # misuse/end-user or state/war frame.  Zero fixture lines use these
        # frames (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:user\w*|operator\w*|purchaser\w*|consumer\w*)\b"
            r"[^.]{0,40}\b(?:assume\w*|shall\s+be\s+liable)\b[^.]{0,40}"
            r"\b(?:misuse\w*|improper\s+use\w*|misuse\s+of)\b"
            r"|\b(?:misuse\w*|improper\s+use\w*)\b[^.]{0,40}\bliab\w*\b[^.]{0,40}"
            r"\b(?:user\w*|operator\w*)\b"
            r"|\b(?:state\w*|government\w*|public\s+authority\w*)\b"
            r"[^.]{0,40}\b(?:shall\s+not\s+be\s+liable|not\s+liable|exempt)|liab\w*\b"
            r"[^.]{0,40}\b(?:state\w*|government\w*)\b[^.]{0,40}\bwar\b",
            lower,
        ):
            continue
        # "Liability for latent defects is imposed by statute" restates a
        # statutory liability default (a law, not the contract, fixes who
        # bears latent-defect risk), not an allocation negotiated between the
        # parties.  Genuine liability clauses ("the Carrier shall be liable
        # for loss of or damage to the goods", fixture lines) still fire — the
        # guard requires the statute frame together with latent defects.
        # Zero fixture lines pair latent defects with statute (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\blatent\s+defect\w*\b[^.]{0,45}"
            r"\b(?:imposed|provided|required|mandated|imposed\s+by)\s?(?:by\s+)?"
            r"(?:statute|statut\w*|law\b)\b"
            r"|\bstatut\w*\b[^.]{0,45}\blatent\s+defect\w*\b",
            lower,
        ):
            continue
        # "The platform is provided as is, without liability on our part" is a
        # factual as-is disclaimer wording, not an agreed allocation between
        # the parties; genuine "as is" clauses that then cap or disclaim are
        # covered by the amount/_CAP_MARK / _BROAD_DISCLAIM branches below.
        if re.search(
            r"\bas\s+is\b[^.]{0,30}\b(?:without|no)\s+liab\w*\b"
            r"|\b(?:without|no)\s+liab\w*\b[^.]{0,30}\bas\s+is\b",
            lower,
        ):
            continue
        # "Liability for intentional wrongdoing cannot be excluded" describes
        # a non-excludable category of liability under the law, not an agreed
        # exclusion.  Genuine exclusions ("the Seller shall not be liable for
        # consequential losses") still fire.
        if re.search(
            r"\bliab\w*\b[^.]{0,30}\b(?:cannot|may\s+not|shall\s+not|will\s+not|"
            r"is\s+not)\s+be\s+exclud\w*\b"
            r"|\b(?:cannot|may\s+not|shall\s+not|will\s+not|is\s+not)\s+be\s+"
            r"exclud\w*\b[^.]{0,30}\bliab\w*\b",
            lower,
        ):
            continue
        # "The operator assumes no liability for lost luggage" restates a
        # no-liability policy posture rather than a negotiated clause; the
        # guard requires an operator-like party together with the assume-no-
        # liability frame.  Genuine "shall not be liable" obligations still
        # fire.
        if re.search(
            r"\b(?:operator|carrier|airline|station|manager|landlord|hotel)\w*\b"
            r"[^.]{0,30}\bassum\w*\b[^.]{0,25}\bno\s+liab\w*\b"
            r"|\bno\s+liab\w*\b[^.]{0,25}\bassum\w*\b[^.]{0,30}"
            r"\b(?:operator|carrier|airline|station|manager|landlord|hotel)\w*\b",
            lower,
        ):
            continue
        # "Breach of the warranty gives rise to liability in damages" is a
        # statement of legal consequence, not a liability clause allocating
        # risk between the parties.
        if re.search(
            r"\b(?:gives|gave|given|giving)\s+rise\s+to\b[^.]{0,30}\bliab\w*\b"
            r"|\bliab\w*\b[^.]{0,30}\b(?:gives|gave|given|giving)\s+rise\s+to\b",
            lower,
        ):
            continue
        # "No liability shall attach to the Commission for delays" protects a
        # public body / institution from liability — a factual statement, not
        # a contract clause between commercial parties.
        if re.search(
            r"\bno\s+liab\w*\b[^.]{0,40}\battach\w*\b[^.]{0,20}\b(?:commissi\w*|"
            r"committe\w*|authorit\w*|agency\w*)\b"
            r"|\b(?:commissi\w*|committe\w*|authorit\w*|agency\w*)\b[^.]{0,20}"
            r"\battach\w*\b[^.]{0,40}\bno\s+liab\w*\b",
            lower,
        ):
            continue
        # "The carrier's liability is limited by the Hague Convention" /
        # "The hospital's liability to patients is capped by statute" restate
        # a liability regime fixed by treaty or statute (transport, medical,
        # institutional) — not an agreed cap negotiated between the parties.
        # The statutory-adjective guard above covers "statutory/professional/
        # regulatory/legal liability"; this one catches the instrument-after-
        # verb frame ("limited/capped by <Convention|statute>").  Genuine
        # caps ("Liability of Licensor shall be limited to fees paid under
        # this Agreement", fixture line) still fire — they say "limited to" +
        # an amount, never "limited by" + an instrument.  Zero fixture lines
        # pair liability with a convention, treaty or statute (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bliab\w*\b[^.]{0,45}\b(?:capp?ed|limited)\s+by\b[^.]{0,45}"
            r"\b(?:the\s+|this\s+)?(?:convention\w*|treat\w*|statut\w*|"
            r"hague|warsaw|montreal|cmr|athens|berne|berlin|geneva)\b"
            r"|\b(?:convention\w*|treat\w*|statut\w*|hague|warsaw|montreal|cmr|"
            r"athens|berne|berlin|geneva)\b[^.]{0,45}\b(?:cap\w*|limit\w*)\b"
            r"[^.]{0,45}\bliab\w*\b",
            lower,
        ):
            continue
        # "Joint liability of the joint venturers is limited to the assets of
        # the venture" describes a joint-venture governance default (each
        # venturer answers only from the venture's own assets), not an agreed
        # cap negotiated between the parties.  The JV fixtures ("establish a
        # joint venture for 5 years", "joint venture may be terminated") never
        # bind liability to venture assets, and no fixture binds a cap to a
        # joint venture (checked against fixtures/generate_fixtures.py).
        # Genuine caps ("aggregate liability shall not exceed USD 1M") still
        # fire.
        if re.search(
            r"\b(?:joint\s+ventur\w*|venturers?)\b[^.]{0,55}\bliab\w*\b[^.]{0,50}"
            r"\blimit\w*\b[^.]{0,35}\b(?:assets?|capital|property)\s+"
            r"(?:of\s+the\s+)?venture\b"
            r"|\bliab\w*\b[^.]{0,45}\b(?:joint\s+ventur\w*|venturers?)\b"
            r"[^.]{0,45}\blimit\w*\b[^.]{0,35}\bassets?\b",
            lower,
        ):
            continue
        # "limited liability company / partnership / LLC" is a business-entity
        # formation term ("Delta Ltd. is a limited liability company"), not a
        # negotiated liability cap.  The _CAP_MARK "limited" keyword alone
        # triggered the fire; the entity frame disambiguates.  Zero fixture
        # lines contain "limited liability company/partnership/llc" (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\blimited\s+liability\s+(?:company|partnership|corporation)\b"
            r"|\bll[cC]\b[^.]{0,20}\b(?:company|incorporated|limited)\b",
            lower,
        ):
            continue
        # "Assume liability for" in past/present narrative ("the carrier
        # assumed liability for the lost cargo", "tenants assume liability
        # for damage", "She took liability for the project's failure" — the
        # irregular past tense) states who bears an existing risk, not a
        # quantified cap.  Genuine undertakings ("The Buyer shall assume
        # liability for the goods after delivery") still fire — the guard
        # requires no modal (shall/will/must/may).  Zero fixture lines pair
        # assumed liability with a modal (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:assumed?|takes?|took\b|take\s+on)\b[^.]{0,30}\bliabilit\w*\s+for\b",
            lower,
        ) and not re.search(r"\b(?:shall|will|must|may)\b", lower):
            continue
        # "discharged in bankruptcy" is insolvency law — a debt being wiped
        # out by a bankruptcy order, phrased with liability vocabulary, not a
        # contractual liability cap.  Zero fixture lines combine discharge
        # with bankruptcy.
        if re.search(
            r"\bdischarg\w*\b[^.]{0,35}\bbankrupt\w*\b"
            r"|\bbankrupt\w*\b[^.]{0,35}\bdischarg\w*\b",
            lower,
        ):
            continue
        amount = _AMOUNT_RE.search(sentence)
        pct = _PERCENT_RE.search(sentence)

        if amount and _CAP_MARK.search(sentence):
            risk = RiskLevel.MEDIUM if not re.search(r"\bup to\b", lower) else RiskLevel.LOW
            flag = f"liability cap {amount.group(0).strip()}"
            detail = f"liability capped at {utils.compact(amount.group(0))}"
        elif pct and _CAP_MARK.search(sentence):
            risk = RiskLevel.MEDIUM
            flag = f"liability tied to {pct.group(0)} of contract price"
            detail = pct.group(0)
        elif _CAP_MARK.search(sentence):
            if "contract price" in lower or "purchase price" in lower or "subcontract price" in lower:
                risk = RiskLevel.MEDIUM
                flag = "liability capped at contract price"
            elif "fees" in lower or "amounts paid" in lower or "amount actually paid" in lower \
                    or "paid by" in lower or "paid under" in lower or "months fees" in lower:
                risk = RiskLevel.MEDIUM
                flag = "liability capped at fees/amounts paid"
            elif "up to" in lower:
                risk = RiskLevel.LOW
                flag = "financial cap (up to) present"
            else:
                risk = RiskLevel.MEDIUM
                flag = "liability cap present (detail unspecified)"
            detail = flag
        elif _BROAD_DISCLAIM.search(lower):
            risk = RiskLevel.HIGH
            flag = "party disclaims liability (broad)"
            detail = "disclaimer / no-liability"
        elif _EXCLUSION_ONLY.search(lower):
            # Standard mutual exclusion of indirect/unconsequential damages;
            # low severity for both sides.
            risk = RiskLevel.LOW
            flag = "exclusion of indirect/consequential damages"
            detail = flag
        elif amount:
            risk = RiskLevel.MEDIUM
            flag = f"liability exposure {amount.group(0).strip()}"
            detail = amount.group(0).strip()
        else:
            # generic indemnity keeping risk visible, but not HIGH unless the
            # text actually disclaims all liability.
            risk = RiskLevel.MEDIUM
            flag = "indemnification / liability clause present"
            detail = flag
        out.append(Match("liability_cap", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 9  confidentiality
# ---------------------------------------------------------------------------


def rule_confidentiality(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in ("confidential", "non-disclosure", "privacy of data")):
            continue
        if re.search(r"\b(no|nothing|not)\s+confidential", lower) or re.search(
            r"\b(?:does|do|shall|will)\s+not\s+apply\b", lower
        ):
            continue
        # "Confidential Information means information that is not generally
        # known" and "Confidential Information does not include ..." are bare
        # definitions / carve-outs — they say what the term covers, not who
        # must keep what secret.  Genuine obligations naming who may disclose
        # or for how long ("shall remain confidential for 3 years",
        # "acknowledge that confidential information includes pricing") still
        # fire.
        if re.search(
            r"\bconfidential\s+(?:information|data|material\w*)\b[^.]{0,30}"
            r"\bmeans\b"
            r"|\bconfidential\s+(?:information|data|material\w*)\b[^.]{0,40}"
            r"\bdoes\s+not\s+(?:include|cover)\b"
            r"|\bconfidential\s+(?:information|data|material\w*)\b[^.]{0,50}"
            r"\bshall\s+not\s+(?:include|cover)\b",
            lower,
        ):
            continue
        # "Documents shall be marked as confidential / marked 'Confidential'"
        # is a marking duty (label the documents), not a restriction on who
        # may use or disclose them.  Genuine use/disclosure restrictions
        # ("shall keep strictly confidential", "shall not disclose") still
        # fire.  No fixture line uses "mark" + "confidential" together.
        if re.search(
            r"\b(?:mark\w*|label\w*|stamp\w*|designat\w*)\b[^.]{0,25}"
            r"\bconfidential\b"
            r"|\bconfidential\b[^.]{0,40}\b(?:mark\w*|label\w*|stamp\w*|designat\w*)\b",
            lower,
        ):
            continue
        # "The parties shall maintain the confidentiality of trade secrets as
        # required by the Trade Secrets Act" is a statutory-compliance
        # statement (honour trade-secret law), not a duty about the
        # contract's own confidential information.  Genuine confidentiality
        # obligations ("shall keep confidential for 3 years") still fire.
        if re.search(
            r"\bconfidential\w*\b[^.]{0,60}\btrade\s+secret\w*\b[^.]{0,60}"
            r"\b(?:as\s+required|pursuant\s+to|under\s+(?:the\s+|applicable\s+|"
            r"relevant\s+)?(?:law|act|statute|regulation|directive))\b"
            r"|\btrade\s+secret\w*\b[^.]{0,60}\bconfidential\w*\b[^.]{0,60}"
            r"\b(?:as\s+required|pursuant\s+to|under\s+(?:the\s+|applicable\s+|"
            r"relevant\s+)?(?:law|act|statute|regulation|directive))\b"
            r"|\bconfidential\w*\b[^.]{0,60}\b(?:as\s+required\s+by\s+law)\b",
            lower,
        ):
            continue
        # "The parties shall keep the terms of this settlement confidential" /
        # "confidential terms of the settlement" keeps a dispute-resolution
        # agreement's terms secret (settlement confidentiality), not the
        # contract's own confidential information.  A genuine obligation on
        # the parties' information ("information received during the course
        # of negotiations shall remain confidential for 3 years") still fires
        # — the guard requires a terms/details/contents-of-settlement noun
        # pair, not a bare information mention.
        if re.search(
            r"\b(?:terms?|details?|contents?|existence|amount\w*)\s+of\b"
            r"[^.]{0,40}\b(?:settlement|mediation)\b[^.]{0,40}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\b(?:settlement|mediation)\b"
            r"[^.]{0,40}\b(?:terms?|details?|contents?|existence)\b",
            lower,
        ):
            continue
        # "Employee confidentiality agreements remain in force after
        # termination" is a statement about a class of HR documents (the
        # company's employment NDAs) — background boilerplate, not an
        # obligation between the parties over this contract's information.
        # Genuine obligations ("shall keep strictly confidential for 3
        # years") still fire — the guard requires the employee/employment
        # confidentiality-agreement noun.
        if re.search(
            r"\b(?:employee\w*|staff\w*|employment\w*|personnel\b)\b[^.]{0,30}"
            r"\bconfidentiality\b[^.]{0,30}\b(?:agreement\w*|clause\w*|"
            r"undertaking\w*|obligation\w*|n[ds]as?\b)\b"
            r"|\bconfidentiality\b[^.]{0,30}\b(?:agreement\w*|clause\w*|"
            r"undertaking\w*|obligation\w*)\b[^.]{0,30}"
            r"\b(?:employee\w*|staff\w*|employment\w*|personnel\b)\b",
            lower,
        ):
            continue
        # "The opinion is privileged and confidential under attorney-client
        # privilege" marks a legal-advice privilege (litigation / solicitor
        # privilege), not a contractual duty of secrecy between the parties.
        # "The confidential annex to the judgment was released to the press"
        # is a court-document disclosure event.  Genuine confidentiality
        # obligations ("shall keep strictly confidential for 3 years") still
        # fire — the guards require the privilege / judgment / press frame.
        # Zero fixture lines use these phrases (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:attorney[-\s]client|legal\s+professional|solicitor[-\s]client|"
            r"litigation|lawyer[-\s]client)\s+privilege\b",
            lower,
        ):
            continue
        if re.search(
            r"\bconfidential\b[^.]{0,45}\b(?:annex\w*|schedule\w*|exhibit\w*)\b"
            r"[^.]{0,45}\b(?:judgment|ruling|award|order\b)\b"
            r"|\b(?:annex\w*|schedule\w*|exhibit\w*)\b[^.]{0,45}\bconfidential\b"
            r"[^.]{0,45}\b(?:judgment|ruling|award|order\b)\b",
            lower,
        ):
            continue
        # "The law imposes sanctions for non-disclosure" / "A fine applies for
        # disclosing confidential information" restate a statutory breach
        # consequence imposed by a legislature or regulator, not a contractual
        # duty of secrecy between the parties.  The guard requires the
        # statutory-imposer frame (law / statute / regulation / authority +
        # impose / sanction / fine).  Genuine "shall keep confidential"
        # obligations do not use it.  Zero fixture lines mention statutory
        # sanctions for non-disclosure (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:law\b|statute\w*|regulation\w*|legislation|authority\w*|"
            r"regulator\w*)\b[^.]{0,45}"
            r"\b(?:impos\w*|sanction\w*|fine\w*|penaliz\w*|penalis\w*)\b"
            r"[^.]{0,45}\bnon-?disclos\w*\b"
            r"|\bnon-?disclos\w*\b[^.]{0,45}"
            r"\b(?:impos\w*|sanction\w*|fine\w*|penaliz\w*|penalis\w*)\b"
            r"[^.]{0,45}\b(?:law\b|statute\w*|regulation\w*|legislation|"
            r"authority\w*|regulator\w*)\b",
            lower,
        ):
            continue
        # "The parties shall keep the fact of this settlement confidential" is
        # settlement confidentiality about the dispute resolution itself (seen
        # in mediation/adr boilerplate) — not the contract's own confidential
        # information.  The guard above already covers terms/details/contents
        # of settlement; this leg covers the bare "fact of" frame.  Genuine
        # obligations ("information exchanged shall remain confidential for 3
        # years") still fire.
        if re.search(
            r"\bfact\s+of\b[^.]{0,40}\b(?:settlement|mediation)\b[^.]{0,40}"
            r"\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\b(?:settlement|mediation)\b"
            r"[^.]{0,40}\bfact\s+of\b",
            lower,
        ):
            continue
        # "The patient's medical records shall be kept confidential" is a
        # healthcare-privacy / medical-records rule (clinical information), not
        # a contractual duty of secrecy between the parties.
        # "This document contains confidential information" is a bare
        # marking/classification statement about a document (no party duty),
        # not an obligation.  "Customers may contact our confidential helpline
        # at any time" advertises a private helpline — a consumer-service
        # feature, not a confidentiality clause.  "The survey responses were
        # collected confidentially" describes a data-collection practice, not
        # an ongoing secrecy obligation.  Genuine obligations ("information
        # received during negotiations shall remain confidential for 3
        # years") still fire — the guards require the medical-record /
        # contains-information / helpline / survey-collection frame.  Zero
        # fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:medical\s+records?\b|patient\w*\s+records?\b|health\s+records?\b)"
            r"[^.]{0,40}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\b(?:medical\s+records?|health\s+records?)\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:this\s+document|the\s+report|the\s+file|the\s+letter)\b"
            r"[^.]{0,30}\bcontain(?:s|ing)?\b[^.]{0,30}"
            r"\bconfidential\s+(?:information|data|material\w*)\b",
            lower,
        ):
            continue
        # "Please keep the meeting notes confidential" / "The minutes of the
        # meeting shall remain confidential" is a note-handling instruction for
        # a corporate meeting (governance of the meeting minutes), not a
        # contractual duty of secrecy between the parties over the contract's
        # information.  The minutes guard below already covers the "minutes of
        # the meeting" frame; the "meeting notes" compound is the note-taking
        # variant of the same family.  Genuine obligations ("information
        # received during negotiations shall remain confidential for 3 years")
        # have no meeting-notes frame and still fire.  Zero fixture lines use
        # "meeting notes" (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bmeeting\s+notes?\b[^.]{0,40}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\bmeeting\s+notes?\b",
            lower,
        ):
            continue
        if re.search(r"\bconfidential\s+helpline\b", lower):
            continue
        if re.search(
            r"\bsurveys?\b[^.]{0,40}\b(?:responses?|results?|answers?|data\b)"
            r"[^.]{0,40}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\bsurveys?\b[^.]{0,40}"
            r"\b(?:responses?|results?|answers?|data\b)\b",
            lower,
        ):
            continue
        # "The minutes of the meeting shall remain confidential" records who may
        # read what was discussed at a corporate meeting (governance of the
        # meeting minutes), not a contractual duty of secrecy between the
        # parties over the contract's information.  "The identity of the
        # whistleblower was kept confidential" is an employment-protection /
        # reporting-rule statement (who may name a whistleblower), not a
        # confidentiality clause between the parties.  "Trade secrets are
        # confidential by their nature" describes a property of trade-secret
        # law, not an agreed secrecy duty.  Genuine obligations ("information
        # received during negotiations shall remain confidential for 3 years")
        # still fire — the guards require the minutes / whistleblower /
        # by-nature frame.  Zero fixture lines use these frames (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\bminutes\s+of\b[^.]{0,40}\b(?:the\s+)?meeting\b[^.]{0,40}"
            r"\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\bminutes\s+of\b[^.]{0,40}"
            r"\b(?:the\s+)?meeting\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:identity|name|identity\s+of\s+the)\s\b[^.]{0,30}"
            r"\bwhistleblow\w*\b[^.]{0,40}\bconfidential\w*\b"
            r"|\bwhistleblow\w*\b[^.]{0,40}\b(?:kept|held|revealed|disclosed)\b"
            r"[^.]{0,30}\bconfidential\w*\b",
            lower,
        ):
            continue
        # "The witness's identity was kept confidential" is a witness-/
        # victim-protection statement (who may be identified in proceedings),
        # not a contractual secrecy duty between the parties — the same
        # identity-protection family as the whistleblower guard above.
        # Genuine obligations ("information received during negotiations shall
        # remain confidential for 3 years") carry no witness/victim identity
        # frame and still fire.  Zero fixture lines pair a witness/victim/
        # informant identity with "confidential" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:witness\w*|victim\w*|informant\w*|juror\w*|complainant\w*)"
            r"\s*'s\s+identity\b[^.]{0,40}\bconfidential\w*\b"
            r"|\bidentity\s+of\b[^.]{0,25}\b(?:witness\w*|victim\w*|"
            r"informant\w*|juror\w*|complainant\w*)\b[^.]{0,40}"
            r"\bconfidential\w*\b",
            lower,
        ):
            continue
        if re.search(
            r"\btrade\s+secrets?\b[^.]{0,30}\bconfidential\b[^.]{0,30}"
            r"\bby\s+their\s+nature\b"
            r"|\btrade\s+secrets?\b[^.]{0,40}\b(?:are|is|shall\s+be|deem\w*)\b"
            r"[^.]{0,30}\bconfidential\b",
            lower,
        ):
            continue
        # Status / attribute statements describing information as confidential
        # (a given state of affairs) are not agreed secrecy duties between the
        # parties.  Frames: "The report remains confidential until
        # publication" (a hold-until-release rule, no misuse restriction),
        # "negotiation positions are confidential to the two teams" (an
        # audience limitation), "Client information is treated as confidential
        # under the policy" (a factual policy description) and "Confidential
        # discussions took place at the summit" (a plain descriptive noun).
        # Genuine obligations still fire — the fixture line "Confidential
        # information shall remain confidential for 3 years after termination"
        # (checked against fixtures/generate_fixtures.py) has no until/treated/
        # to-audience/passive-noun frame.
        if re.search(
            r"\bconfidential\w*\b[^.]{0,35}\buntil\s+publication\b"
            r"|\bconfidential\w*\b[^.]{0,40}\bto\b[^.]{0,35}"
            r"\b(?:the|both|the\s+two|the\s+three)?\s*(?:parties?|teams?|"
            r"companies?|organi[sz]ations?|authorit\w*|police\b|prosecut\w*|"
            r"officials?\b|informants?)\b"
            r"|\btreat\w*d\s+as\s+confidential\b[^.]{0,30}\bunder\b"
            r"|\bconfidential\s+discussions?\b",
            lower,
        ):
            continue
        # "This email is confidential and intended only for the named
        # recipient" is a standard outbound email disclaimer (a transmission
        # legend), not an agreed secrecy duty between the parties.  Fixture
        # confidentiality lines (fixtures/generate_fixtures.py: obligations to
        # keep information confidential) contain no email disclaimer frame.
        if re.search(
            r"\b(?:email|e[- ]?mail|message|correspondence|transmission)\b"
            r"[^.]{0,50}\bintended\s+only\s+for\b[^.]{0,40}\bconfidential\b"
            r"|\bconfidential\b[^.]{0,50}\bintended\s+only\s+for\b[^.]{0,40}"
            r"\b(?:recipient|addressee|party|person)\b",
            lower,
        ):
            continue
        # "The report is confidential until next week / further notice" is a
        # temporary classification (a hold-until-date label), not an agreed
        # secrecy duty — the same family as the "until publication" status
        # guard above but ending at a time noun instead of a release event.
        # Genuine obligations still fire (no until + time-noun frame in
        # fixture confidentiality lines).
        if re.search(
            r"\bconfidential\w*\b[^.]{0,35}\buntil\b[^.]{0,25}"
            r"\b(?:next|the\s+following|this\s+)?(?:week|month|quarter|"
            r"year|further\s+notice|friday|monday|january|february|march|"
            r"april|may|june|july|august|september|october|november|"
            r"december)\b",
            lower,
        ):
            continue
        # "Advisers may receive confidential information on a need-to-know
        # basis", "The court ordered the records be kept confidential" and
        # "Confidential references were provided by the applicant's employers"
        # describe a disclosure-screening practice, a court-imposed record
        # seal, or a character-reference document — none is an agreed secrecy
        # duty between the parties.  Genuine obligations ("Confidential
        # information shall remain confidential for 3 years after
        # termination") have no need-to-know / court-order / reference frame
        # and still fire.  Zero fixture lines use these frames (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\bneed[- ]to[- ]know\b"
            r"|\b(?:court\w*|judge\w*|tribunal\w*)\b[^.]{0,45}\border\w*\b"
            r"[^.]{0,45}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,45}\border\w*\b[^.]{0,45}"
            r"\b(?:court\w*|judge\w*|tribunal\w*)\b"
            r"|\border\w*\b[^.]{0,45}\bconfidential\w*\b[^.]{0,45}"
            r"\b(?:court\w*|judge\w*|tribunal\w*)\b"
            r"|\bconfidential\s+references?\b"
            r"|\breferences?\b[^.]{0,35}\bconfidential\w*\b",
            lower,
        ):
            continue
        # "Doctor-patient confidentiality is protected by law" states a
        # professional-secrecy rule (medical ethics / statute), not an agreed
        # secrecy duty.  "The deliberations of the jury are confidential" and
        # "Confidential comments are removed from the filing" are procedural /
        # redaction statements (court process, filing censoring), not
        # contractual duties.  "The bank's duty of confidentiality is owed to
        # the customer" describes a banking-customs duty, not an inter-party
        # secrecy obligation.  Genuine obligations ("Confidential information
        # shall remain confidential for 3 years after termination") have no
        # such frames and still fire.  Zero fixture lines use doctor/patient,
        # jury, filing/redaction or bank/customer frames with confidential
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:doctor[-\s]patient|physician[-\s]patient|"
            r"medical[-\s]confidentiality|healthcare[-\s]professional)\b"
            r"|\bconfidentiality\b[^.]{0,35}\bprotected\s+by\s+law\b"
            r"|\b(?:jury|jurors?|deliberations?)\b[^.]{0,40}\bconfidential\w*\b"
            r"|\bconfidential\w*\b[^.]{0,40}\b(?:jury|jurors?|deliberations?)\b"
            r"|\bconfidential\w*\b[^.]{0,35}\b(?:comments?|remarks?)\b"
            r"[^.]{0,35}\b(?:remov\w*|redact\w*|excised|strik\w*|fil\w*d)\b"
            r"|\b(?:remov\w*|redact\w*|excised|strik\w*)\b[^.]{0,35}"
            r"\bconfidential\w*\b[^.]{0,30}\b(?:comments?|remarks?|filing\w*)\b"
            r"|\b(?:bank\w*|banking\w*|lender\w*)\b[^.]{0,40}"
            r"\bduty\s+of\s+confidentiality\b"
            r"|\bduty\s+of\s+confidentiality\b[^.]{0,40}"
            r"\b(?:bank\w*|banking\w*|customer\w*|institution\w*)\b",
            lower,
        ):
            continue
        # "Confidential sources" is journalism shorthand for anonymous
        # informants (whistle-blowers, leakers), not a commercial secrecy
        # duty between contracting parties.  Zero fixture lines use
        # "confidential sources" (checked against fixtures/
        # generate_fixtures.py).
        if re.search(r"\bconfidential\s+sources?\b", lower):
            continue
        # "The application is kept confidential for 18 months before the
        # patent office publishes it" is IP-filing publication timing, not a
        # secrecy obligation owed to a counterparty.  The publication /
        # expiry-month frame distinguishes it from genuine NDA terms.
        # Zero fixture lines combine confidential with a patent/publication
        # frame.
        if re.search(
            r"\b(?:patent\w*|trademark\w*)\b[^.]{0,45}\bconfidential\w*\b"
            r"[^.]{0,40}\b(?:publication\b|published\b|months?\b|grant\w*\b)",
            lower,
        ):
            continue
        # "Personal data is confidential" states a present-tense fact about
        # how information is treated, not a forward-looking secrecy duty.
        # Genuine NDA wording ("personal data shall remain confidential for
        # 3 years after termination", "personal data is confidential and
        # shall be safeguarded") still fires — the guard requires the
        # present-tense copula and no "shall" modal anywhere in the
        # sentence.  Zero fixture lines match the copula frame without
        # "shall" (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bpersonal\s+data\b[^.]{0,30}\b(?:is|are|remains?)\b[^.]{0,25}"
            r"\bconfidential\b",
            lower,
        ) and not re.search(r"\bshall\b", lower):
            continue
        # "confidential information shared between patient and doctor" is
        # clinical confidentiality (a physician's record-keeping duty), the
        # same family as the doctor-patient guard above but written with the
        # relational "between ... and ..." frame.  Zero fixture lines use the
        # patient-doctor frame.
        if re.search(
            r"\bconfidential\w*\b[^.]{0,35}\bbetween\b[^.]{0,30}"
            r"\b(?:patients?|clients?)\b[^.]{0,20}\band\b[^.]{0,20}"
            r"\b(?:doctors?|physicians?|therapists?)\b"
            r"|\bbetween\b[^.]{0,35}\b(?:patients?|clients?)\b[^.]{0,20}"
            r"\band\b[^.]{0,20}\b(?:doctors?|physicians?|therapists?)\b"
            r"[^.]{0,35}\bconfidential\w*\b",
            lower,
        ):
            continue
        # "Doctors owe their patients a duty of confidentiality" /
        # "Lawyers are bound by client confidentiality" state a professional
        # code-of-conduct duty owed in a fiduciary / advisory relationship, not
        # an inter-party secrecy duty in a contract.  Genuine NDA obligations
        # ("Confidential information shall remain confidential for 3 years")
        # lack professional-dependence vocabulary and still fire.  Zero fixture
        # lines pair confidentiality with a professional duty (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bduty\s+of\s+confidentiality\b[^.]{0,40}"
            r"\b(?:doctor\w*|patient\w*|physician\w*|lawyer\w*|solicitor\w*|"
            r"attorney\w*|client\w*|accountant\w*|therapists?\b|advisor\w*|"
            r"consultant\w*|auditor\w*)\b"
            r"|\b(?:doctor\w*|physician\w*|lawyer\w*|solicitor\w*|attorney\w*|"
            r"accountant\w*|therapists?\b|advisor\w*|consultant\w*|auditor\w*)\b"
            r"[^.]{0,40}\bduty\s+of\s+confidentiality\b"
            r"|\bbound\s+by\b[^.]{0,35}\b(?:client|professional)\s+confidentiality\b",
            lower,
        ):
            continue
        # "All conversations in the session were confidential" narrates a past
        # event in a meeting / therapy / journalism context — a factual note,
        # not a forward-looking secrecy duty.  "The committee required
        # confidentiality on the pending report" is internal governance around
        # an unpublished document.  Genuine NDA wording ("shall keep
        # confidential", "shall remain confidential for N years") still fires —
        # it has a modal obligation verb and no meeting-narration frame.
        # Zero fixture lines use these narration frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:conversations?|discussions?|sessions?|meetings?|gatherings?)\b"
            r"[^.]{0,35}\b(?:were|are|remained?)\s+confidential\b"
            r"|\b(?:committee\w*|board\w*|panel\w*)\b[^.]{0,35}\bconfidential\w*\b"
            r"[^.]{0,30}\b(?:pending\b|report\w*|draft\b|memorandum\b)\b"
            r"|\b(?:pending\b|report\w*|draft\b|memorandum\b)\b[^.]{0,30}"
            r"\bconfidential\w*\b[^.]{0,35}\b(?:committee\w*|board\w*|panel\w*)\b",
            lower,
        ):
            continue
        # "A confidential report was leaked to the press" narrates a past
        # disclosure episode, not a standing secrecy duty.  The guard needs the
        # leak/divulge verb AND a public-dissemination target (press/public/
        # reporter/news/media) so it cannot catch the genuine return-or-destroy
        # duties ("the receiving party shall return or destroy all Confidential
        # Information", "the consultant must return ... confidential documents
        # within 30 days"), which are obligations, not disclosures.  No fixture
        # line pairs confidential with leak/divulge + press/media (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\bconfidential\w*\b[^.]{0,45}\b(?:leaked?|divulged?)\b"
            r"[^.]{0,40}\b(?:press\b|public\b|reporter\w*|journalist\w*|"
            r"news\w*|media\b|newspaper\w*)\b",
            lower,
        ):
            continue
        # "Client confidentiality is guaranteed by the firm's code of conduct"
        # states a professional/ethical assurance, not a contractual secrecy
        # undertaking alongside rights and remedies.  Genuine NDA language
        # ("shall keep confidential") has a modal obligation and no
        # guarantee-by-code frame.  No fixture line uses this frame (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\bconfidentialit\w*\b[^.]{0,45}\b(?:guaranteed?|undertaken?|"
            r"ensured?|promised?|assured?)\s+by\b[^.]{0,40}"
            r"\b(?:code\s+of\s+conduct\b|professional\s+standards?\b|"
            r"professional\s+body\b|firm\w*|institute\w*|bodies?\b)\b",
            lower,
        ):
            continue
        # "Confidential briefings were held for senior staff before the merger"
        # describes a discrete internal information event, not a secrecy
        # obligation in this agreement.  Genuine duties keep the return/destroy
        # or keep-secret frames without a briefing noun.  No fixture line
        # combines briefings with confidential (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bconfidential\w*\b[^.]{0,35}\bbriefing\w*\b"
            r"[^.]{0,40}\b(?:were\s+held\b|held\b|conduct\w*|present\w*|"
            r"gave\w*|attend\w*)\b|\bbriefing\w*\b[^.]{0,35}\bconfidential\w*\b",
            lower,
        ):
            continue
        dur = _DURATION_RE.search(sentence)
        risk = RiskLevel.LOW
        flag = "confidentiality clause"
        detail = None
        if dur:
            detail = f"for {dur.group(0)}"
            if dur.group("unit").lower() in ("year",) and float(dur.group("num")) > 5:
                risk = RiskLevel.MEDIUM
                flag += f" long survival (> {dur.group('num')}y)"
        if re.search(r"\bsurvive\b.*\btermination\b|\bsurvives?\s+termination", lower):
            risk = RiskLevel.MEDIUM
            flag += " (survives termination)"
        out.append(Match("confidentiality", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 10  IP ownership
# ---------------------------------------------------------------------------


def rule_ip(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "intellectual property", "ip rights", "ownership", "assignment", "assign",
            "assigns", "copyright", "patent rights", "work product", "works made",
            "inventions", "trademark", "license to", "licences", "licence grant",
            "licensed", "grants .* license", "granted .* license", "exclusive license",
            "rights in its own", " vest",
        )):
            continue
        # A denied transfer ("nothing grants any right", "does not grant any
        # right", "no rights are transferred") states what will not happen —
        # not an IP risk.  "Grantees no right" covers multi-word right kinds
        # ("grants any intellectual property rights").
        if re.search(
            r"(nothing\s+.*\s+grants\s+any\b|does\s+not\s+grant\s+(?:any\s+|the\s+)?|"
            r"grants\s+no\s+)\b[^.]{0,35}\b(?:ip\s+rights?|intellectual\s+property|rights?)\b"
            r"|\bno\s+.*\s+licen[cs]e\b"
            r"|\b(?:no|not)\b[^.]{0,40}\b(?:rights?|ownership|title)\b[^.]{0,60}"
            r"\b(?:transfer(?:s|red)?|assign(?:s|ed|ing)?)\b",
            lower,
        ):
            continue
        # A "license agreement" heading should not produce an IP-risk clause by itself.
        if re.match(r"^(this\s+)?(software\s+|end[- ]user\s+)?license\w*\s+(agreement|terms)", lower):
            continue
        # "assign / transfer this/the Agreement to a third party" is a
        # contract-assignment right, not an IP-rights transfer.  "inventions /
        # work product assigned to" still fires (they bind the assigned
        # subject to an entity, not to the Agreement itself), and the gap
        # between the verb and the agreement noun is tempered so a sentence
        # mentioning data / "transferred under the Agreement" never matches.
        if re.search(
            r"\b(?:assign(?:ed|s)?|transfer(?:s|red|ring)?)\b"
            r"(?:(?!\bdata\b)[^.]){0,40}"
            r"\b(?:this|the)\s+(?:agreement|contract)\b",
            lower,
        ):
            continue
        # "licensed premises" (alcohol / gaming permits) licenses a place, not IP.
        if re.search(r"\blicen[cs]ed\s+premises\b", lower):
            continue
        # "project manager assigned to the account" allocates personnel, not
        # intellectual property: a person/role token (staff, manager, analyst,
        # engineer, consultant, team member, ...) as the assigned subject is
        # personnel direction.  "inventions / work product assigned to the
        # employer / the Client" is a genuine IP transfer and still fires.
        if re.search(
            r"\b(?:personnel|staff\s+members?|managers?|engineers?|consultants?|"
            r"analysts?|representatives?|specialists?|officers?|lawyers?|attorneys?|"
            r"contractors?)\b"
            r"[^.]{0,35}\bassigned\b",
            lower,
        ):
            continue
        # "assign its rights to the payment of receivables / to receive
        # payments" assigns money rights (factoring, receivables), not
        # intellectual property.  "inventions / work product assigned to the
        # employer / the Client" still fires (the subject is a creation).
        if re.search(
            r"\b(?:assign(?:s|ed|ing)?|transfer(?:s|red|ring)?)\s+(?:its|any|all|the)\s+"
            r"rights?\b[^.]{0,35}\b(?:to\s+(?:the\s+)?(?:payment|receive|receipt|collection)\w*|"
            r"receivables?|debts?)\b"
            r"|\b(?:payment|receive|receipt|collection)\w*\s+rights?\b[^.]{0,35}"
            r"\b(?:assign(?:s|ed|ing)?|transfer(?:s|red|ring)?)\b",
            lower,
        ):
            continue
        # "The Contractor warrants that it does not infringe third party
        # intellectual property rights" is a non-infringement warranty /
        # assurance — it confirms the deliverables are free of third-party
        # IP, not a transfer, grant or ownership clause about the parties'
        # own IP.  Genuine assignments and licence grants still fire.
        if re.search(
            r"\b(?:warrant\w*|represents?\b|undertake\w*|acknowledges?)\b"
            r"[^.]{0,60}\b(?:not\s+|no\s+)?infring\w*\b[^.]{0,40}"
            r"\b(?:third[- ]?part\w*|third\s+part\w*)\b",
            lower,
        ):
            continue
        # "The Supplier shall indemnify the Customer against third party
        # infringement of intellectual property" is an indemnity against
        # third-party IP claims — it allocates risk for infringement suits,
        # not a transfer, grant or ownership clause about the parties' own
        # IP.  Genuine assignments and licence grants still fire.
        if re.search(
            r"\bindemnif\w*\b[^.]{0,80}"
            r"\b(?:infring\w*|infringement\w*)\b[^.]{0,40}"
            r"\bintellectual\s+property\b",
            lower,
        ):
            continue
        # "Each party shall use reasonable care in handling the IP addresses
        # assigned to it" concerns internet-protocol addresses (routing /
        # networking), not intellectual property.  The gate keyword "assign" /
        # "assignment" admits it, so the guard pins the IP-address noun in
        # either word order.  Genuine IP transfers ("inventions assigned to
        # the Company") still fire.
        if re.search(
            r"\bip\s+address(?:es)?\b"
            r"|\baddress(?:es)?\b[^.]{0,30}\bip\s+subnet\b"
            r"|\b(?:internet\s+protocol|network)\b[^.]{0,35}\baddress(?:es)?\b",
            lower,
        ):
            continue
        # "This Agreement shall continue for the life of the copyright in the
        # Works" is a duration / term-of-agreement sentence (the term tracks
        # the copyright life), not a transfer, grant or ownership clause about
        # the parties' IP.  Genuine assignments and licence grants still fire.
        if re.search(
            r"\b(?:life|term|duration|period)\s+of\s+(?:the\s+)?copyright\b"
            r"|\bcopyright\b[^.]{0,30}\b(?:life|term|duration)\b",
            lower,
        ):
            continue
        # "The Employee shall promptly disclose any inventions to the
        # Company" is a disclosure / reporting duty, not an assignment, grant
        # or ownership clause (no right changes hands).  Genuine transfers
        # ("inventions ... are assigned to the employer") still fire via the
        # negative leg.
        if re.search(
            r"\binventions?\b[^.]{0,40}\bdisclos\w*\b"
            r"|\bdisclos\w*\b[^.]{0,40}\binventions?\b",
            lower,
        ) and not re.search(
            r"\b(?:assign\w*|transfer\w*|vest\w*|grant\w*|owned\s+by|"
            r"made\s+for\s+hire|work\s+made)\b",
            lower,
        ):
            continue
        # "Intellectual Property Rights means / shall mean all patents,
        # copyrights, trade marks ..." is a bare definition of the term — it
        # defines the scope of proprietary rights, not who owns, transfers or
        # licenses them.  Genuine ownership statements, assignments and
        # licence grants still fire (the negative leg keeps means-free
        # sentences live; the licence-grant branch below is untouched).
        # No fixture line defines the term with "means".
        if re.search(
            r"\b(?:intellectual\s+property(?:\s+rights?)?)\b[^.]{0,60}"
            r"\b(?:means?|shall\s+mean|is\s+defined\s+as)\b",
            lower,
        ):
            continue
        # "The Parties acknowledge the ownership of Intellectual Property is
        # set out in the Background IP schedule" is a recital pointing at a
        # schedule for the definition of ownership — a cross-reference, not
        # an ownership, transfer or licence clause.  Genuine ownership
        # statements ("all IP created under this Agreement is owned by the
        # Customer") still fire — the negative leg keeps assign/transfer/vest/
        # grant/owned-by sentences live.
        if re.search(
            r"\bownership\b[^.]{0,60}\bintellectual\s+property\b[^.]{0,50}"
            r"\b(?:set\s+out|described|specified|detailed|defined)\b[^.]{0,35}"
            r"\b(?:schedule\w*|annex\w*|exhibit\w*|appendix\w*|background\w*)\b"
            r"|\bintellectual\s+property\b[^.]{0,60}\bownership\b[^.]{0,50}"
            r"\b(?:set\s+out|described|specified|detailed|defined)\b[^.]{0,35}"
            r"\b(?:schedule\w*|annex\w*|exhibit\w*|appendix\w*|background\w*)\b",
            lower,
        ) and not re.search(
            r"\b(?:assign\w*|transfer\w*|vest\w*|grant\w*|owned\s+by|"
            r"shall\s+be\s+owned)\b",
            lower,
        ):
            continue
        # "The intellectual property clause in section 9 allocates ownership"
        # is a cross-reference to an IP provision elsewhere (the substantive
        # clause is set out in the section it points to), not a clause that
        # allocates or transfers IP itself.  Genuine ownership statements
        # ("all IP created under this Agreement is owned by the Customer")
        # still fire.
        if re.search(
            r"\b(?:shall\s+)?trademark\b[^.]{0,45}\bbrand\b"
            r"|\bbrand\b[^.]{0,45}\b(?:shall\s+)?trademark\b",
            lower,
        ):
            continue
        # "The Company intends to patent the device in the US and EU" is a
        # commercialization / registration intention (file a patent for a
        # product) — there is no transfer, grant or ownership allocation of
        # existing IP between the parties.  Genuine assignments ("the patent
        # rights ... are assigned to the Customer") still fire — the guard
        # requires the verb-form "patent the", not the noun "patent rights".
        if re.search(
            r"\b(?:shall\s+)?patent\s+the\b[^.]{0,50}"
            r"\b(?:device\w*|invention\w*|product\w*|technology|process\w*|"
            r"method\w*|system\w*|design\w*)\b",
            lower,
        ):
            continue
        # "The vesting period for the options is 4 years" is an equity-grant
        # vesting schedule, not an IP allocation.  "The copyright notice
        # appears on every page of the manual" is a marking duty statement,
        # not an ownership transfer.  "The trademark registration was renewed
        # for another 10 years" is a registry administration event, not an IP
        # right being allocated.  Genuine IP assignments ("the patent rights
        # ... are assigned to the Customer", "shall vest in the Licensor")
        # still fire.  Zero fixture lines use these phrases (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bvesting\s+period\b"
            r"|\bperiod\s+of\s+vesting\b"
            r"|\bcopyright\s+notice\b"
            r"|\btrademark\s+registr\w*\b[^.]{0,45}\brenew\w*\b[^.]{0,25}\byears?\b"
            r"|\brenew\w*\b[^.]{0,30}\btrademark\s+registr\w*\b",
            lower,
        ):
            continue
        # "The invention was assigned to the company by the inventor" reports
        # a past property-transfer event (facts about where a specific
        # invention ended up), not a negotiated allocation between the parties
        # for work under this contract.  The guard is keyed to the past-tense
        # "was assigned" frame so genuine assignment clauses ("inventions are
        # assigned to the employer", fixture line 219; "all work product shall
        # be assigned to the Client", fixture line 314) still fire.
        # "The website is protected by copyright automatically" restates a
        # statutory entitlement to protection (a work is protected from the
        # moment of creation), not a transfer, grant or ownership clause.
        # "The term of the trademark registration is 10 years" describes a
        # registry duration, not an IP allocation between the parties.  Zero
        # fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\binventions?\b[^.]{0,35}"
            r"\b(?:was\s+assigned|were\s+assigned|has\s+been\s+assigned)\b",
            lower,
        ):
            continue
        if re.search(
            r"\bwebsites?\b[^.]{0,45}\bcopyright\b"
            r"|\bcopyright\b[^.]{0,45}\bwebsites?\b",
            lower,
        ):
            continue
        if re.search(
            r"\btrademark\s+registration\b[^.]{0,40}\b(?:term|period|duration|"
            r"valid|expires?|renew\w*)\b"
            r"|\b(?:term|period|duration)\b[^.]{0,40}\btrademark\s+registration\b",
            lower,
        ):
            continue
        # "The trademark application is still pending" / "The patent was filed
        # in 2020" report the administrative status of a filing with the IP
        # registry (pending, filed, granted, refused, lapsed) — a fact about
        # the application, not a transfer, grant or ownership allocation of
        # existing IP between the parties.  Genuine assignments ("the patent
        # rights ... are assigned to the Customer") and licence grants (fixture
        # lines 219, 314) do not use the application-status frame and still
        # fire.  Zero fixture lines report application status (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:trademark|patent)\s+application\b[^.]{0,40}"
            r"\b(?:pending|filed|granted|refused|rejected|abandoned|"
            r"prosecut\w*|examined|published)\b"
            r"|\b(?:trademark|patent)\b[^.]{0,25}\b(?:filed|granted|refused|"
            r"rejected|abandoned|lapsed)\b"
            r"|\b(?:filed|granted|refused|rejected|abandoned|lapsed)\b"
            r"[^.]{0,25}\b(?:trademark|patent)\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:intellectual\s+property|ip)\s+(?:clause|provision|section|"
            r"article|term)\b[^.]{0,50}\b(?:allocat\w*|contemplat\w*|address\w*|"
            r"deal\w*|govern\w*)\b",
            lower,
        ):
            continue
        # "The software is registered with the copyright office" reports a
        # registration formality with a public IP registry (a filing fact),
        # not a transfer, grant or ownership allocation of the parties' IP.
        # Genuine assignments ("the patent rights are assigned to the
        # Customer") and licence grants (fixture lines 219, 314) do not use
        # the registered-with-registry frame and still fire.  Zero fixture
        # lines pair "registered" with an IP office/registry noun (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\bregist\w*\s+(?:with|at)\b[^.]{0,35}\b(?:copyright\s+office|"
            r"patent\s+office|trademark\s+office|registered\s+office|"
            r"intellectual\s+property\s+office|registry\b)"
            r"|\b(?:copyright\s+office|patent\s+office|trademark\s+office|"
            r"intellectual\s+property\s+office|registry)\b[^.]{0,35}"
            r"\bregist\w*\s+(?:with|at)\b",
            lower,
        ):
            continue
        # "The trademark is registered in the office registry / the United
        # States / our jurisdiction" states a bare registration fact (where a
        # mark is on file), not a transfer, grant or ownership allocation of
        # the parties' IP.  Genuine assignments and licence grants do not use
        # the "registered in" + registry/jurisdiction frame — the guard
        # requires a registration outcome noun, so "the trademark shall be
        # registered in the name of the Customer" (an ownership allocation,
        # fixture 76) still fires.  No fixture line pairs "registered in" with
        # a registry/jurisdiction noun (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:trademark\w*|patent\w*|design\w*|mark\w*|brand\w*)\b"
            r"[^.]{0,30}\bregist\w*\s+in\b[^.]{0,35}"
            r"\b(?:office\s+registry|registry|united\s+states|jurisdiction\b|"
            r"country\b|territory\b|region\b|european\s+union)\b"
            r"|\bregist\w*\s+in\b[^.]{0,35}\b(?:office\s+registry|registry|"
            r"united\s+states|jurisdiction\b|country\b|territory\b|region\b|"
            r"european\s+union)\b[^.]{0,30}\b(?:trademark\w*|patent\w*|design\w*|"
            r"mark\w*|brand\w*)\b",
            lower,
        ):
            continue
        # "The logo is protected by copyright law" / "copyright law protects
        # the logo" states a statutory entitlement to protection, not an
        # allocation or transfer of IP between the parties.  "Employees retain
        # intellectual property rights in their own inventions" / "inventions
        # remain the property of their creators" states a statutory or
        # policy default (individual creators keep their own work), not a
        # transfer to a party.  Genuine IP assignments ("the patent rights are
        # assigned to the Customer"), work-for-hire ("inventions created
        # during employment are assigned to the employer", fixture 219) and
        # licensing still fire — the guard requires the copyright-law
        # protection frame or the "own inventions / creators" retention frame.
        # Zero fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py; fixture 219 is an assignment, not a
        # retention).
        if re.search(
            r"\b(?:protected\s+by|protection\s+under|protection\s+(?:of|afforded\s+by))"
            r"[^.]{0,40}\bcopyright\s+law\b"
            r"|\bcopyright\s+law\b[^.]{0,40}\b(?:protect\w*|safeguard\w*)\b"
            r"|\b(?:retain\w*|keep\w*|reserve\w*)\b[^.]{0,40}\b(?:intellectual\s+property)"
            r"\s+rights?\b[^.]{0,40}\b(?:own|their\s+own)\s+(?:invention\w*|creations?\b|works?\b)\b"
            r"|\b(?:own\s+inventions?\b|creators?\b[^.]{0,25}\b(?:retain\w*|keep\w*)\b|\binventions?\b[^.]{0,30}"
            r"\bremain\w*\s+the\s+property\b)",
            lower,
        ):
            continue
        # "Our intellectual property includes customer lists" is a
        # composition / content statement about what a body of IP assets is
        # made up of, not an allocation, transfer or licence of IP between
        # the parties.  The guard pins the include/comprise verb frame.
        # Genuine assignments and licence grants (fixture lines 219, 314)
        # do not use it.  Note: fixture line 86 ("confidential information
        # includes pricing, customer lists") is a data-disclosure provision,
        # not an "intellectual property includes" frame, and stays untouched
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:intellectual\s+property|ip\b)\b[^.]{0,40}"
            r"\b(?:includ\w*|compris\w*|contain\w*|consist\w*)\b"
            r"|\b(?:includ\w*|compris\w*|contain\w*|consist\w*)\b"
            r"[^.]{0,40}\b(?:intellectual\s+property|ip\b)\b",
            lower,
        ):
            continue
        # "Copyright protection lasts 70 years after death" states the
        # statutory term of protection of a copyright (an entitlement), not
        # an allocation or transfer of IP between the parties.  Genuine IP
        # assignments and licence grants have no protection-term frame.
        # Zero fixture lines state a protection term (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:copyright|patent|trademark|design\s+right|database\s+right)"
            r"\s+protect\w*\b[^.]{0,35}"
            r"\b(?:last\w*|expir\w*|dur\w*|runs?\b|end\w*)\b[^.]{0,20}"
            r"\b\d+\s+(?:years?|months?)\b"
            r"|\b\d+\s+(?:years?|months?)\b[^.]{0,25}"
            r"\b(?:copyright|patent|trademark|design\s+right|database\s+right)"
            r"\s+protect\w*\b",
            lower,
        ):
            continue
        # "The copyright expired 70 years after the composer's death." states
        # the statutory life of a copyright (a right ending by operation of
        # law after a fixed period), not an allocation or transfer of IP
        # between the parties.  The protection-term guard above requires a
        # protect/ed verb next to the right, and the application-status guard
        # (trademark/patent filed/granted/lapsed) does not cover copyright
        # expiry, so a separate guard is needed.  Genuine IP clauses still
        # fire: "The patent and trademark rights in the Works are assigned to
        # the Customer." and "The trademark shall be registered in the name of
        # the Customer." (tests test_rules.py:3585/3719 and 5787) carry no
        # rights-expiry frame.  Zero fixture lines pair a specific IP right
        # with an expiry verb (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:copyright\w*|patent\s+rights?|patents?|trademark\s+rights?|"
            r"trademarks?)\b[^.]{0,40}\b(?:expires?|expired|expiry|expiration)\b"
            r"|\b(?:expires?|expired|expiry|expiration)\b[^.]{0,40}"
            r"\b(?:copyright\w*|patent\s+rights?|patents?|trademark\s+rights?|"
            r"trademarks?)\b",
            lower,
        ):
            continue
        # "The logo is the company's registered trademark" is a brand-ownership
        # fact (a company is the holder of a registered mark), not a transfer,
        # grant or licence between the parties.  Genuine assignments and
        # licensing still fire — they never use the possessive-holder frame.
        if re.search(
            r"\b(?:is|becomes?)\s+(?:the\s+)?"
            r"(?:company(?:'|\u2019)s|their\s+|our\s+|the\s+|its\s+)"
            r"\s*(?:registered\s+)?trademark\b"
            r"|\b(?:company(?:'|\u2019)s|their\s+|our\s+|its\s+)"
            r"\s*(?:registered\s+)?trademark\b",
            lower,
        ):
            continue
        # "Intellectual property rights are not exhausted by this sale" and
        # "Goodwill and intellectual property are excluded from the sale"
        # describe the scope of what the sale does or does not carry over —
        # a statutory exhaustion doctrine or a carve-out from the assets
        # transferred — not a negotiated allocation of IP between the parties.
        # Genuine IP assignments ("the patent rights are assigned to the
        # Customer") and licensing (fixture lines 219, 314) have no
        # exhaustion / excluded-from-sale frame and still fire.  Zero fixture
        # or test lines use exhaust / carve-out frames (checked against
        # fixtures/generate_fixtures.py and tests/test_rules.py).
        if re.search(
            r"\bexhaust\w*\b[^.]{0,40}\b(?:sale\b|rights?\b)"
            r"|\b(?:sale\b|rights?\b)[^.]{0,40}\bexhaust\w*\b"
            r"|\b(?:intellectual\s+property|ip)\b[^.]{0,45}\bexclud\w*\b[^.]{0,35}"
            r"\bfrom\s+(?:the\s+|this\s+)?sale\b"
            r"|\bexclud\w*\b[^.]{0,45}\b(?:intellectual\s+property|ip)\b[^.]{0,35}"
            r"\bfrom\s+(?:the\s+|this\s+)?sale\b",
            lower,
        ):
            continue
        # "The intellectual property in this report belongs to the author"
        # states a statutory default ownership (the creator owns the work by
        # law), not a negotiated allocation between the parties for work under
        # this contract.  The guard fires before the "belongs to" retention
        # branch below; genuine clauses ("all right, title and interest ...
        # belong to the Customer" — fixture line 147) still fire because they
        # name the Customer as recipient, not an author/creator/inventor.
        # "Copyright in the photographs belongs to the artist" is the same
        # statutory default for creators of works (an artist, photographer,
        # musician or other maker owns what they create under copyright law)
        # — not an allocation between the parties.
        if re.search(
            r"\bbelongs?\s+to\s+(?:its\s+|the\s+|their\s+)?"
            r"\b(?:author\w*|creator\w*|inventor\w*|artist\w*|photographer\w*|"
            r"musician\w*|writer\w*s?|designer\w*|maker\w*s?)\b",
            lower,
        ):
            continue
        # "Piracy infringes the intellectual property of studios" is an
        # infringement / enforcement narrative (a statement that copying
        # violates creators' rights), not an allocation, transfer or licence
        # between the parties.  Genuine IP clauses ("Licensee assigns all
        # intellectual property rights to the Licensor") have no piracy /
        # infringement-of-media vocabulary and still fire.
        if re.search(
            r"\bpirat\w*\b[^.]{0,45}\b(?:intellectual\s+property|\bip\b)\b"
            r"|\b(?:intellectual\s+property|\bip\b)\b[^.]{0,45}\bpirat\w*\b"
            r"|\binfring\w*\b[^.]{0,35}\bintellectual\s+property\b[^.]{0,30}"
            r"\b(?:studio\w*|industr\w*|creators?\b|artists?\b)\b",
            lower,
        ):
            continue
        # "The author sold the copyright in her novels to the publisher" is a
        # past-tense account of a one-off transfer of a completed work's
        # copyright, not an allocation of future intellectual property between
        # the parties to an agreement.  Genuine allocation clauses ("All
        # intellectual property created by Provider shall be owned by Client
        # upon full payment" — fixture line 87; "assigns all intellectual
        # property rights to the Licensor") never phrase the transfer as a
        # past "sold the copyright/trademark/patent", so they still fire.
        # Zero fixture lines pair sell/past-tense with a specific IP right
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bsold\b[^.]{0,35}\b(?:copyright\w*|trademark\w*|patent\w*)\b|"
            r"\b(?:copyright\w*|trademark\w*|patent\w*)\b[^.]{0,35}\bsold\b",
            lower,
        ):
            continue
        # "The band lost the trademark rights after years of disuse" narrates
        # the loss of a registration through non-use (a factual/legal
        # statement), not a negotiated allocation or transfer of rights.
        # Genuine IP clauses never state that a party "lost" its rights — they
        # allocate/transfer/retain ownership — so they still fire.  Zero
        # fixture lines combine rights with "lost"/"loss of"
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:lost|lose[s]?|loss\s+of)\b[^.]{0,35}"
            r"\b(?:copyright\w*|trademark\w*|patent\w*|intellectual\s+property|ip\s+rights?)\b",
            lower,
        ):
            continue
        # "The museum owns the copyright in the restored photographs" states
        # who holds the copyright in an artwork as a factual ownership
        # statement (creators/holders), not a contractual allocation between
        # the parties.  Genuine clauses allocate to named contract parties
        # ("All intellectual property ... shall be owned by Client upon full
        # payment" — fixture line 87) and never use "owns the copyright" with a
        # creative-holder noun.  Zero fixture lines pair "owns" with copyright/
        # trademark and a holder noun (checked against fixtures/
        # generate_fixtures.py).  (The "belongs to author/creator..." guard
        # above already handles the "belongs-to" form of this same narration.)
        if re.search(
            r"\bowns?\b[^.]{0,30}\b(?:copyright\w*|trademark\w*|patent\w*)\b"
            r"[^.]{0,30}\b(?:museum\w*|author\w*|artist\w*|photographer\w*|"
            r"musician\w*|band\w*|composer\w*|galler\w*|studio\w*)\b"
            r"|\b(?:museum\w*|author\w*|artist\w*|photographer\w*|musician\w*|"
            r"band\w*|composer\w*|galler\w*|studio\w*)\b[^.]{0,30}"
            r"\bowns?\b[^.]{0,30}\b(?:copyright\w*|trademark\w*|patent\w*)\b",
            lower,
        ):
            continue
        # "She transferred the copyright to her publisher" narrates a one-off
        # past assignment of a completed work (a transfer-of-copyright
        # transaction), not an allocation of future intellectual property
        # between the parties.  Genuine allocation clauses ("All intellectual
        # property created by Provider shall be owned by Client upon full
        # payment" — fixture line 87) bind future creation and never phrase
        # the transfer as a past "transferred the copyright/trademark/patent",
        # so they still fire.  Zero fixture lines pair a past transfer of a
        # specific IP right (checked against fixtures/generate_fixtures.py;
        # fixture line 89 "data transferred outside the EEA" is a data
        # transfer with an obligation frame and lives in rule_data_protection).
        if re.search(
            r"\btransferred\b[^.]{0,40}\b(?:copyright\w*|trademark\w*|patent\w*|"
            r"design\s+rights?|intellectual\s+property)\b"
            r"|\b(?:copyright\w*|trademark\w*|patent\w*|design\s+rights?|"
            r"intellectual\s+property)\b[^.]{0,40}\btransferred\b",
            lower,
        ):
            continue
        # "The design rights vest in the designer by operation of law" states
        # that ownership attaches automatically under a statutory rule (by
        # operation of law), not through a contractual allocation between the
        # parties.  Genuine allocation clauses use "shall vest in Licensor /
        # contractor" (fixture lines 65/185) without the statutory phrase, so
        # requiring "by operation of law" keeps them firing.  Zero fixture
        # lines pair vesting with "by operation of law" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bvest\w*\b[^.]{0,50}\bby\s+operation\s+of\s+law\b"
            r"|\bby\s+operation\s+of\s+law\b[^.]{0,50}\bvest\w*\b",
            lower,
        ):
            continue
        risk = RiskLevel.LOW
        flag = None
        detail = None
        # "intellectual property shall remain vested in the Licensor / stay
        # with the Licensor" reserves ownership with the holder — the opposite
        # of "assigned to other party".  (The " vest" keyword gate in Round 17
        # deliberately kept the leading space so "investment" does not match;
        # the guard here must not re-break "shall vest in Licensor".)
        if re.search(
            r"\bremain(?:s|ing|ed)?\s+vested\s+in\b"
            r"|\bremain(?:s|ing|ed)?\s+vested\b[^.]{0,35}\b(?:with|in)\b"
            r"|\bstay\w*[^.]{0,25}\b(?:with|in)\b[^.]{0,35}\b(?:vested|ownership)\b",
            lower,
        ):
            risk = RiskLevel.LOW
            flag = "each party retains ownership"
            detail = "in party remains with holder"
        elif re.search(r"\b(work[- ]for[- ]hire|works made|shall vest in|vested in|are assigned to|is assigned to|assigns? to|shall be assigned|ownership.*passes|owned by)", lower):
            conditional = re.search(r"\b(upon\s+(?:full\s+)?payment|upon\s+receipt|after\s+full\s+payment|made\s+for\s+hire\s+under|in\s+consideration\s+of\s+full\s+payment)", lower)
            if conditional:
                risk = RiskLevel.MEDIUM
                flag = "IP assigned to other party (upon payment)"
            else:
                risk = RiskLevel.HIGH
                flag = "IP assigned to other party (one-way)"
            detail = "ownership transfers out"
        elif re.search(r"\bretain(s|ing)?\s+(all\s+)?(ownership|title|rights)\b|\bremain(s|ing)?\s+the\s+property\b|\bbelongs\s+to\b", lower):
            risk = RiskLevel.LOW
            flag = "each party retains ownership"
        elif re.search(
            r"\b(?:grants?|granted)\b[^.]{0,45}\blicen[cs]e\b"
            r"|\blicen[cs]e[ds]?\b[^.]{0,45}\b(?:grants?|granted|rights?\s+to\s+use|to\s+use|permission)\b",
            lower,
        ):
            risk = RiskLevel.MEDIUM
            flag = "IP licence granted"
            detail = "licence terms"
        else:
            flag = "IP clause present"
            risk = RiskLevel.LOW
        out.append(Match("ip", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 11  renewal / termination
# ---------------------------------------------------------------------------


def rule_renewal(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in ("renewal", "renew", "renewable", "auto-renew",
                                        "auto renews", "renews")):
            continue
        if re.search(r"\b(no|not|shall\s+not|without)\s+(?:automatic\s+)?(?:renewal|renew|renewing)\b|does\s+not\s+renew|will\s+not\s+renew|no\s+auto", lower):
            continue
        # "renewable energy sources" — a resource adjective, not a contract
        # renewal provision.
        if re.search(r"\brenewable\s+(?:energy|sources?|resources?|power|technologies?|means?)\b", lower):
            continue
        # "Renewables generated 40% of electricity" uses the plural noun
        # "renewables" (renewable-energy resources) as the sentence subject
        # — an energy-sector report, not a contract renewal provision.  The
        # existing adjective guard needs a following resource noun; the noun
        # form has none.  A genuine renewal ("The initial term ... shall be
        # 3 years, renewable." — fixture line 78) uses the adjective form and
        # stays untouched.
        if re.search(
            r"\brenewables?\b[^.]{0,40}"
            r"\b(?:generat\w*|produc\w*|suppl\w*|account\w*|share\b|"
            r"million\s+k?wh|billion\s+k?wh)\b",
            lower,
        ):
            continue
        # A denied right to renew ("shall have no right to renew") is an express
        # non-renewal, not a renewal risk.
        if re.search(r"\b(?:no|not|without)\b[^.]{0,30}\bright\s+to\s+renew\b", lower) \
                or re.search(r"\bshall\s+not\s+have\s+(?:the\s+)?right\s+to\s+renew\b", lower):
            continue
        # "apply for renewal of the licence/permit/visa" is an administrative
        # re-issuance of a credential, not a contract renewal provision.
        if re.search(
            r"\bapply(?:\s+for\s+(?:a\s+|the\s+)?|ing\s+for\s+(?:a\s+|the\s+)?)"
            r"(?:renewal|a\s+renewal|the\s+renewal)\s+of\b"
            r"|\b(?:renewal|a\s+renewal|the\s+renewal)\s+of\s+(?:the\s+|this\s+)?"
            r"(?:licen[cs]e|licens\w+|permit|registration|visa|passport|endorsement|patent|trademark)\b"
            r"|\brenewal\s+fee(?:s)?\s+for\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:licen[cs]e|licens\w+|permit|registration|visa|passport|endorsement|patent|trademark)\b",
            lower,
        ):
            continue
        # "renewal of the domain name registration / subscription / policy" and
        # "the insurance policy renews annually" are renewals of accounts,
        # registrations or policies — not of the agreement itself.  "This SaaS
        # Subscription renews automatically" stays a genuine term renewal, so the
        # verb form is not suppressed for subscription.
        if re.search(
            r"\b(?:renewal|a\s+renewal|the\s+renewal)\s+of\s+(?:the\s+|this\s+)?"
            r"(?:domain\s+name|registration|subscription|membership|hosting|"
            r"insurance\s+policy|policy|account)\b"
            r"|\b(?:domain\s+name|membership|hosting|registration|account|"
            r"insurance\s+policy|policy)\b[^.]{0,35}\brenew(?:al|s|ing|ed)?\b"
            r"|\brenew(?:al|s|ing|ed)?\b[^.]{0,35}\b(?:domain\s+name|membership|hosting|"
            r"registration|account|insurance\s+policy|policy)\b",
            lower,
        ):
            continue
        # "renewal of the warranty / support renewal for the software /
        # extended warranty renewal" extend product coverage, not the
        # agreement's term.  Genuine term renewals ("the term shall renew
        # automatically") still fire.  Zero fixture lines mention warranty /
        # support renewals (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:renewal|(?:the|this)\s+renewal)\s+of\s+(?:the\s+|this\s+)?"
            r"(?:warrant\w*|support)\b"
            r"|\b(?:warrant\w*|support)\s+(?:renewal|renew\w*)\b"
            r"|\brenew\w*\b[^.]{0,25}\b(?:warrant\w*|support)\b",
            lower,
        ):
            continue
        # "The patient must renew the prescription every 3 months" is a
        # clinical / medical renewal (a prescription, vaccine or medication
        # course), not a renewal of the agreement's term.  Genuine term
        # renewals still fire.  Zero fixture lines mention prescriptions or
        # medical recurrences (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:prescription\w*|vaccination\w*|immunization\w*|"
            r"medication\w*s?)\b"
            r"|\b(?:prescription\w*|vaccination\w*|immunization\w*|"
            r"medication\w*s?)\b[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "renew the engagement / appointment / mandate" renews a consulting
        # relationship or an office ("the Board may renew the appointment of
        # the auditor"), not the agreement's term.  Genuine term renewals name
        # the Agreement / term / lease and still fire.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:engagement|appointment|mandate)\b"
            r"|\b(?:engagement|appointment|mandate)\b[^.]{0,35}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The Licence shall be renewed annually upon payment of the renewal
        # fee" and "the parties shall renew their accreditation on a biennial
        # basis" re-issue an administrative credential (a licence / permit /
        # accreditation) for a further period — not a renewal of the agreement
        # itself.  The verb-form branch requires a credential object after the
        # renewal verb (so a genuine "this Agreement renews" stays), and the
        # bare-licence branch is confined to "shall be renewed" + payment /
        # fee mechanics so "the lease shall renew for a further period" is
        # untouched.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:the\s+|a\s+|their\s+|its\s+)?(?:licen[cs]e\b|licens\w+|"
            r"permit\b|accredit\w+|registration\b|visa\b|passport\b|"
            r"endorsement\b)\b"
            r"|\b(?:licen[cs]e\b|licens\w+|permit\b|accredit\w+|registration\b|"
            r"visa\b|passport\b|endorsement\b)\b[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\blicen[cs]e\b[^.]{0,35}\bshall\s+be\s+renewed\b[^.]{0,35}"
            r"\b(?:payment|fee|renewal\s+fee|charge\w*|cost\w*)\b",
            lower,
        ) and not re.search(
            r"\b(?:this|the)\s+(?:agreement|contract|lease)\b[^.]{0,25}\brenew\w*\b"
            r"|\brenew\w*\b[^.]{0,25}\b(?:this|the)\s+(?:agreement|contract|lease)\b",
            lower,
        ):
            continue
        # "review and renew the annual budget / the forecasts shall be
        # reviewed and renewed" is an administrative planning cycle for
        # financial documents, not a renewal of the agreement's term.  The
        # guard requires "review(d) and renew(ed)" — a bare budget mention
        # (fixture line 344 "Media buying budget shall not exceed USD
        # 2,000,000") is untouched.
        if re.search(
            r"\breview(?:s|ed)?\s+and\s+renew(?:s|ed|al)?\b[^.]{0,45}"
            r"\b(?:budget|forecasts?|plans?|projections?|targets?)\b"
            r"|\b(?:budget|forecasts?|plans?|projections?|targets?)\b[^.]{0,45}"
            r"\b(?:is|are|shall\s+be|will\s+be|to\s+be)?\s+review(?:s|ed)?\s+and\s+"
            r"renew(?:s|ed|al)?\b",
            lower,
        ):
            continue
        # "the parties may renew the campaign for the upcoming season" renews
        # an advertising / marketing campaign cycle, not the agreement's term.
        # "The player's contract was renewed for two seasons" renews a
        # sporting / entertainment engagement cycle (a season, series or
        # production run), likewise not the agreement's term.  Zero fixture
        # lines mention renewing campaigns or seasons (checked against
        # fixtures/generate_fixtures.py; "campaign" appears only in a
        # confidentiality fixture, which is a different rule).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:campaign\w*|advertis\w*|marketing\w*|promotion\w*|season\w*)\b"
            r"|\b(?:campaign\w*|advertis\w*|marketing\w*|promotion\w*|season\w*)\b[^.]{0,40}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "the parties shall renew their grant commitments for the next cycle"
        # renews a funding-programme commitment, not the agreement's term.  The
        # guard requires the funding noun ("grant ... commitment") so a genuine
        # "renew the grant of the Licence for a further term" stays untouched
        # (and the credential guard already owns licence re-issuance).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}\bgrant\w*\b[^.]{0,25}"
            r"\b(?:commitment\w*|cycle|round)\b"
            r"|\b(?:commitment\w*|cycle|round)\b[^.]{0,25}\bgrant\w*\b[^.]{0,45}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The Board shall renew its commitment to the Diversity Charter" is
        # a corporate / policy pledge renewal (reaffirming a charter or
        # initiative), not a renewal of the agreement's term.  The guard
        # requires the pledge noun ("commitment" or "pledge") tied to a
        # charter / initiative-class object, so a genuine "renew the
        # commitment/joint-venture arrangement for a further term" with no
        # charter object stays untouched.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}\b(?:commitment\w*|pledge\w*)\b"
            r"[^.]{0,30}\b(?:charter\w*|initiative\w*|manifesto\w*|diversity|"
            r"declaration\w*|commitment\s+statement|principles?)\b"
            r"|\b(?:charter\w*|initiative\w*|manifesto\w*|diversity|"
            r"declaration\w*|commitment\s+statement|principles?)\b[^.]{0,30}"
            r"\b(?:commitment\w*|pledge\w*)\b[^.]{0,45}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The parties shall discuss renewal of this Agreement at least 3
        # months before expiry" states a discussion / notice lead-time
        # commitment, not a renewal of the term itself (no term is extended).
        # Genuine term renewals ("the term shall renew automatically") still
        # fire.
        if re.search(
            r"\bdiscuss\w*\b[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}\bdiscuss\w*\b",
            lower,
        ):
            continue
        # "Each party shall give written notice of renewal not less than 30
        # days before expiry" sets a notice lead-time for deciding about
        # renewal — a procedural notification duty, not a renewal of the
        # term itself (no term is extended here).  The guard needs BOTH a
        # notice signal AND a before-expiry framing, so a genuine
        # notification-triggered renewal ("renews automatically unless the
        # party gives notice") stays live via the negative leg.
        if re.search(
            r"\bnotice\b[^.]{0,45}\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}"
            r"\b(?:before|prior\s+to)\b[^.]{0,30}\b(?:expir\w*|end\b|"
            r"terminat\w*|anniversary|date)\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}\bnotice\b[^.]{0,45}"
            r"\b(?:before|prior\s+to)\b[^.]{0,30}\b(?:expir\w*|end\b|"
            r"terminat\w*|anniversary|date)\b",
            lower,
        ) and not re.search(
            r"\b(?:automatically|shall\s+renew|renews(?:\s+for|\s+on|\s+automatically))\b",
            lower,
        ):
            continue
        # "The Parties intend to renew this Agreement on terms to be agreed
        # between them" expresses an aspiration to renew — no term is
        # extended, no right or obligation arises.  The negative leg keeps
        # genuine term renewals ("shall renew automatically for a further
        # period") live.
        if re.search(
            r"\bintend\w*\b[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}\bintend\w*\b",
            lower,
        ) and not re.search(
            r"\b(?:shall|will|may|must|automatically)\b",
            lower,
        ):
            continue
        # "The magazine subscription renews unless cancelled" renews a consumer
        # periodical subscription (a magazine, newspaper or journal the reader
        # buys), not a renewal of the agreement's term.  The guard pins the
        # periodical noun, so "This SaaS Subscription renews automatically"
        # (a genuine term-renewal anchor) stays live.
        if re.search(
            r"\b(?:magazine|newspaper|periodical|journal|e-?zine)\s+subscription\b"
            r"[^.]{0,30}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,30}"
            r"\b(?:magazine|newspaper|periodical|journal|e-?zine)\s+subscription\b",
            lower,
        ):
            continue
        # "The partners renewed their vows after twenty years" is the idiom of
        # reaffirming a personal promise (marriage vows), not a renewal of the
        # agreement's term.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}\bvows?\b"
            r"|\bvows?\b[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The library card was renewed online for another year" re-issues a
        # lending credential (a library card or reader pass), not a renewal
        # of the agreement's term.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:library\s+card|reader\s+pass|library\s+membership)\b"
            r"|\b(?:library\s+card|reader\s+pass|library\s+membership)\b"
            r"[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The library renewal is available online" / "The library renewal
        # process takes 5 minutes" / "The magazine's renewal rate fell this
        # quarter" / "We offer a renewal discount to existing subscribers"
        # are administrative or consumer notices about a periodical or
        # lending account (a library, magazine, newspaper, journal or
        # periodical holding), not a renewal of the agreement's term.  The
        # negative leg keeps any genuine renewal of the agreement / contract
        # / lease / licence / term / subscription live — the fixtures "the
        # renewal term shall be 12 months" and "Tenant may renew the lease
        # for a further 12 months" have no periodical-admin noun and are
        # untouched (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:library|magazine\w*|newspaper\w*|journal\w*|periodical\w*)\b"
            r"[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:library|magazine\w*|newspaper\w*|journal\w*|periodical\w*)\b"
            r"|\brenew(?:al|s|ing|ed|able)?\s+"
            r"\b(?:rate|discount|process|available|option\b|fee|price)\b"
            r"|\b(?:rate|discount|process|available|option\b|fee|price)\b"
            r"[^.]{0,25}\brenewal\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term|subscription)\b",
            lower,
        ):
            continue
        # "Subscribers can choose auto-renewal at checkout" is a consumer
        # shopping-cart opt-in (recurring billing at the point of purchase),
        # not a renewal of the agreement's term.  A genuine automatic-renewal
        # clause tied to the agreement / lease / licence / term ("this
        # Agreement shall auto-renew for successive 12-month periods") stays
        # live via the negative leg.
        if re.search(
            r"\bauto\s*[-]?renew(?:al|s|ing|ed|able)?\b"
            r"[^.]{0,35}\b(?:checkout|cart|purchase\w*|order(?:ing)?|"
            r"subscribe|pay\b|billing)\b"
            r"|\b(?:checkout|cart|purchase\w*|order(?:ing)?|subscribe|"
            r"pay\b|billing)\b[^.]{0,35}"
            r"\bauto\s*[-]?renew(?:al|s|ing|ed|able)?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term)\b",
            lower,
        ):
            continue
        # "The conference badge was renewed at the desk" / "the seatbelt
        # buckle was renewed during servicing" re-issue or replace a physical
        # object (a badge, tag, buckle or other pass-item), not a renewal of
        # the agreement's term.  The guard pins the passive "was renewed"
        # frame with an inanimate physical-object noun, so "the lease was
        # renewed for a further term" — a genuine term renewal — stays live
        # (lease is not in the noun list).
        if re.search(
            r"\b(?:badge|buckle|tag|key\s+card|fob|sticker|lanyard)\b"
            r"[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:badge|buckle|tag|key\s+card|fob|sticker|lanyard)\b",
            lower,
        ):
            continue
        # "The bill was renewed for debate in the next session of parliament"
        # is legislative procedure (a statute re-introduced for consideration),
        # not a renewal of the agreement's term.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:parliament\w*|legislat\w*|session\s+of\s+(?:the\s+)?parliament|"
            r"house\s+of\s+(?:commons|lords|representatives))\b"
            r"|\b(?:parliament\w*|legislat\w*|session\s+of\s+(?:the\s+)?parliament|"
            r"house\s+of\s+(?:commons|lords|representatives))\b[^.]{0,40}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The team renewed their commitment to the project" / "The success
        # of the exhibition renewed interest in modern art" is an abstract
        # idiomatic re-affirmation (commitment / interest / efforts / focus as
        # a feeling or mind-set), not a renewal of a document or term.  The
        # pledge guard above requires a charter-class object; this one pins
        # the bare abstract nouns so "renew the commitment / arrangement for
        # a further term (no charter object) stays untouched.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,30}"
            r"\b(?:commitment\w*|interest\b|efforts?\b|focus\b|energy\b|"
            r"enthusiasm\b|determination\b)\b"
            r"|\b(?:commitment\w*|interest\b|efforts?\b|focus\b|energy\b|"
            r"enthusiasm\b|determination\b)\b[^.]{0,30}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The engineer's certification must be renewed by 31 December" /
        # "the accreditation must be renewed every five years" re-issues a
        # professional or institutional credential (a certification or
        # accreditation), not a renewal of the agreement's term.  The
        # credential guard above covers licences / permits / visas /
        # passports / registrations; certification and accreditation sit in
        # the same administrative family.  A genuine "renewal of the
        # agreement" (fixtures "the renewal term shall be 12 months", "shall
        # automatically renew") has no credential noun and stays live.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:certificat\w+|accredit\w+|qualification\w*)\b"
            r"|\b(?:certificat\w+|accredit\w+|qualification\w*)\b"
            r"[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The search warrant was renewed by the magistrate" re-issues a
        # judicial / investigatory instrument (a warrant or summons), not a
        # renewal of the agreement's term.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,35}"
            r"\b(?:search\s+warrant|warrant\b|summons\b|subpoena\w*)\b"
            r"|\b(?:search\s+warrant|warrant\b|summons\b|subpoena\w*)\b"
            r"[^.]{0,35}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "The customer may renew the subscription at the discounted price" is
        # a consumer-pricing offer (a vendor lets a customer keep a
        # subscription at a discount), not a renewal of the agreement's term —
        # renewing the subscription is the marketing hook, the term itself is
        # untouched.  "Tenant may renew the lease for a further 12 months"
        # (fixture line 329) still fires — the guard requires BOTH a
        # subscription object AND a discount, and the fixtures never pair
        # them (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}\bsubscription\b"
            r"[^.]{0,40}\bdiscount(?:ed)?\b"
            r"|\bsubscription\b[^.]{0,40}\bdiscount(?:ed)?\b[^.]{0,40}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # "This Agreement shall terminate on the expiry of the fixed Term
        # unless renewed by the parties" is a conditional continuation formula
        # (the term simply falls away unless somebody renews) — it states an
        # expiry default, not a renewal of the term itself.  The guard requires
        # a term-end marker (terminate / expire / cease / remain in force)
        # bound to the unless-renew formula, and the negative leg keeps any
        # explicit further-period or auto-renewal signal live — so a genuine
        # renewal ("the lease shall automatically renew for 12 months unless
        # the tenant gives notice") still fires, as does "renews on a
        # quarterly basis unless notice of non-renewal is given" (no term-end
        # marker, so the formula never engages).
        if re.search(
            r"\b(?:terminate\w*|expir\w*|cease\w*|come\s+to\s+an?\s+end\b|"
            r"remain\w*\s+in\s+force\b)\b[^.]{0,70}"
            r"\b(?:unless|if\s+not)\b[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:unless|if\s+not)\b[^.]{0,70}"
            r"\b(?:terminate\w*|expir\w*|cease\w*|come\s+to\s+an?\s+end\b|"
            r"remain\w*\s+in\s+force\b)\b",
            lower,
        ) and not re.search(
            r"\bauto\w*\b"
            r"|\b(?:further|additional|another)\b[^.]{0,25}\b(?:period|term|year)\b"
            r"|\b[0-9]+\s*(?:years?|months?)\b",
            lower,
        ):
            continue
        # "Event passes renewed at the gate" / "annual passes renewed early /
        # membership card renewals / the festival pass renewal window" /
        # "members renew their passes" re-issue an admission credential (an
        # event pass, membership card or entrance admission), not a renewal of
        # the agreement's term.  The negative leg keeps any genuine renewal
        # that names the agreement / contract / lease / licence / term /
        # subscription live.  Zero fixture lines mention event passes or
        # membership cards (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?s?\b[^.]{0,35}"
            r"\b(?:pass(?:es)?|membership\s+card\w*|admission\w*)\b"
            r"|\b(?:pass(?:es)?|membership\s+card\w*|admission\w*)\b"
            r"[^.]{0,35}\brenew(?:al|s|ing|ed|able)?s?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term|subscription)\b",
            lower,
        ):
            continue
        # "Your subscription will auto-renew unless you cancel / opt out", "the
        # free trial converts to auto-renewal", "subscribers are billed at each
        # auto-renewal", "renews monthly at $9.99" are consumer recurring
        # billing notices, not a renewal of the agreement's term.  The negative
        # leg (agreement / contract / lease / licence / term / notice / price /
        # fee) keeps genuine term renewals live — the fixture "This SaaS
        # Subscription renews automatically each month unless the customer
        # cancels with notice of 30 days" (fixture line 162) carries "notice"
        # and stays untouched, and a contract renewal price ("the renewal price
        # shall be $1,000") is protected by "price".  Bare "subscription" is
        # deliberately NOT in the leg so consumer billing statements still
        # fall in (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bauto\s*[-]?renew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:unless\s+you\s+(?:cancel|opt\s+out)|opt\s+out\b|billed\b|"
            r"free\s+trial\b|account\s+settings\b|in\s+the\s+app\b)\b"
            r"|\b(?:unless\s+you\s+(?:cancel|opt\s+out)|opt\s+out\b|billed\b|"
            r"free\s+trial\b|account\s+settings\b|in\s+the\s+app\b)\b"
            r"[^.]{0,40}\bauto\s*[-]?renew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,55}\bbilling\s+date\b"
            r"|\bbilling\s+date\b[^.]{0,55}\brenew(?:al|s|ing|ed|able)?\b"
            r"|\brenew(?:al|s|ing|ed|able)?\b[^.]{0,30}\$\s?\d"
            r"|\$\s?\d[^.]{0,30}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term|notice|price|fee)\b",
            lower,
        ):
            continue
        # "Grant funding is renewable based on the reviewer's recommendation" /
        # "the scholarship award is renewable annually" / "fellowship
        # appointments are renewable" / "bursaries are renewable for a second
        # year" re-award a study or research grant, scholarship, fellowship or
        # bursary — a funding-award administrative cycle, not a renewal of the
        # agreement's term.  The negative leg keeps a genuine funding-agreement
        # renewal live ("This Grant Agreement shall automatically renew").
        # Zero fixture lines mention grants / scholarships / fellowships /
        # bursaries (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}"
            r"\b(?:grant\w*|scholarship\w*|fellowship\w*|bursar\w*|"
            r"endowment\w*)\b"
            r"|\b(?:grant\w*|scholarship\w*|fellowship\w*|bursar\w*|"
            r"endowment\w*)\b[^.]{0,45}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term)\b",
            lower,
        ):
            continue
        # "Renew the data plan by purchasing a top-up card" / "gift cards are
        # renewed when the balance is topped up" / "renew your service plan at
        # the checkout" / "you may renew the plan via the app after purchase"
        # are consumer re-ordering / top-up / redemption transactions, not a
        # renewal of the agreement's term.  The negative leg keeps genuine
        # agreement / contract / lease / licence / term / subscription renewals
        # live.  Zero fixture lines mention top-up cards / gift cards / vouchers
        # / checkout / re-ordering (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:top\s*[-]?up\b|gift\s+card\w*|voucher\w*|checkout\b|"
            r"re\s*[-]?order\w*|purchase\b)\b"
            r"|\b(?:top\s*[-]?up\b|gift\s+card\w*|voucher\w*|checkout\b|"
            r"re\s*[-]?order\w*|purchase\b)\b[^.]{0,40}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e|term|notice|price|fee)\b",
            lower,
        ):
            continue
        # "The loan is renewable upon request at the end of the term" / "bank
        # loans are renewable on a year-by-year basis" / "the farmer's mortgage
        # was renewed for another five years" / "the credit facility is
        # renewable at the bank's discretion" describe a banking product's
        # rollover option, not a renewal of the agreement's term.  The negative
        # leg keeps a genuine loan-agreement renewal live ("This Loan Agreement
        # renews automatically each year").  "term" is deliberately NOT in the
        # leg — "at the end of the term" is generic loan-speak, not a named
        # instrument.  Zero fixture lines mention loans / mortgages / credit
        # facilities in a renewal context (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,45}"
            r"\b(?:loan\w*|mortgage\w*|credit\s+facilit\w*|overdraft\w*|"
            r"credit\s+line\b)\b"
            r"|\b(?:loan\w*|mortgage\w*|credit\s+facilit\w*|overdraft\w*|"
            r"credit\s+line\b)\b[^.]{0,45}"
            r"\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ) and not re.search(
            r"\b(?:agreement|contract|lease|licen[cs]e)\b",
            lower,
        ):
            continue
        # Enrolment / registration cycles ("The student's enrolment renews
        # automatically each semester") are administrative re-registration,
        # not renewal of a contract term.  Genuine term renewals still fire
        # ("This Agreement shall automatically renew for successive 1 year
        # terms", fixture line 46).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}\b(?:enrol\w*|registr\w*)\b"
            r"|\benrol\w*\b[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # Catalogue / programme rotation ("The course catalogue renews every
        # semester") is publishing-cycle narration, not a contract term.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}\bcatalog\w*\b"
            r"|\bcatalog\w*\b[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        # Discourse-recurrence statements ("The debate about the pricing
        # strategy renews each quarter") say a conversation topic comes up
        # again, extending the abstract-commitment family guarded above.
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:debates?|discussions?|arguments?|conversations?|dialogues?)\b"
            r"|\b(?:debates?|discussions?|arguments?|conversations?|dialogues?)\b"
            r"[^.]{0,40}\brenew(?:al|s|ing|ed|able)?\b",
            lower,
        ):
            continue
        risk = RiskLevel.LOW
        flag = "renewal clause"
        if re.search(r"automatic|auto-renew|auto\s+renew|renews\s+automatically|renew\s+automatically|quarterly|automatically\s+renew", lower):
            risk = RiskLevel.MEDIUM
            flag = "automatic renewal (auto-renew)"
            rr = _DURATION_RE.search(sentence)
            if rr:
                flag += f" for {rr.group(0)}"
        rr = _DURATION_RE.search(sentence)
        if rr and "automatic" not in flag:
            flag += f" renews for {rr.group(0)}"
            if rr.group("unit").lower() in ("year",) and float(rr.group("num")) > 3:
                risk = RiskLevel.MEDIUM
        if "notice" in lower and "day" in lower:
            flag += " (notice required)"
        out.append(Match("renewal", page, sentence, risk, flag))
    return out


_TERM_CONTEXT = re.compile(
    r"\b(either party|party may|may be terminated|right to terminate|shall terminate|"
    r"will terminate|terminates this|terminate this|termination of this|this agreement|"
    r"the agreement|the contract|the lease|for convenience|for cause|without cause|"
    r"upon default|immediately|notice)\b",
    re.IGNORECASE,
)


def rule_termination(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not re.search(r"\b(termination|terminate|terminates|terminated|withdraw)\b", lower):
            continue
        if not _TERM_CONTEXT.search(sentence):
            continue
        if re.search(r"\b(no termination|may not terminate|shall not terminate|cannot terminate)\b", lower):
            continue
        # "The non-compete restrictions shall terminate upon the expiry of the
        # Restricted Period" is a covenant sunset (the restriction itself ends
        # on a schedule), not a party's right to terminate the agreement.
        # "This Agreement shall terminate on the expiry of the sponsorship
        # period" (fixture line 200) still fires — the subject there is the
        # Agreement itself, not a restriction/covenant.
        if re.search(
            r"\b(?:non[- ]?compete|restriction\w*)\b[^.]{0,45}\bterminat\w*\b"
            r"[^.]{0,45}\b(?:expiry|expiration)\b"
            r"|\b(?:expiry|expiration)\b[^.]{0,55}\b(?:non[- ]?compete|"
            r"restriction\w*)\b[^.]{0,35}\bterminat\w*\b"
            r"|\b(?:expiry|expiration)\b[^.]{0,45}\bterminat\w*\b[^.]{0,45}"
            r"\b(?:non[- ]?compete|restriction\w*)\b",
            lower,
        ):
            continue
        # A sub-provision sunset ("this clause shall terminate upon expiry of
        # the warranty") is a schedule detail, not a party's right to terminate
        # the agreement.  "This Agreement shall terminate…" still fires via the
        # agreement-specific context in _TERM_CONTEXT.
        if re.search(r"\bthis\s+(?:clause|section|provision|warranty|covenant|obligation|sublause|condition)\b[^.]{0,40}\bterminat", lower):
            continue
        # A survival provision ("confidentiality obligations survive the
        # termination of this Agreement") states what outlives the agreement —
        # not a party's right to terminate it.
        if re.search(
            r"\bterminat\w*\b[^.]{0,50}\bsurviv\w*\b"
            r"|\bsurviv\w*\b[^.]{0,50}\bterminat\w*\b",
            lower,
        ):
            continue
        # "Upon termination of this Agreement, the provisions of Sections 4
        # and 7 shall survive ..." is a survival boilerplate that opens with
        # the termination event and lists provisions that outlive it — not a
        # party's right to terminate.  Because the survival verb comes last,
        # the 50-character window above is exhausted by the enumeration, so a
        # dedicated guard matches the document/section noun between the
        # termination event and the survival verb.
        if re.search(
            r"\bterminat\w*\b[^.]{0,90}"
            r"\b(?:provision\w*|section\w*|clause\w*|article\w*|paragraph\w*|"
            r"term\w*|agreement|contract)\b"
            r"[^.]{0,35}\bsurviv\w*\b"
            r"|\bsurviv\w*\b[^.]{0,35}"
            r"\b(?:provision\w*|section\w*|clause\w*|article\w*|paragraph\w*|"
            r"term\w*)\b[^.]{0,90}\bterminat\w*\b",
            lower,
        ):
            continue
        # "Termination of the Agreement shall not affect the accrued rights
        # of the parties" is survival / savings boilerplate (the rights that
        # outlive the ending), not a party's right to terminate the agreement.
        # Genuine rights ("either party may terminate this Agreement") still
        # fire.
        if re.search(
            r"\bterminat\w*\b[^.]{0,60}\bshall\s+not\s+affect\b[^.]{0,40}"
            r"\b(?:accrued|rights?|obligations?|provisions?)\b",
            lower,
        ):
            continue
        # "In the event of termination, the unpaid balance shall become due
        # and payable immediately" is an acceleration / payment-consequence
        # rule triggered by a termination event — not a party's right to
        # terminate the agreement.  "Either party may terminate this
        # Agreement" and "terminate immediately upon default" still fire.
        if re.search(
            r"\bin\s+the\s+event\s+of\b[^.]{0,45}\bterminat\w*\b[^.]{0,60}"
            r"\b(?:due\s+and\s+payable|shall\s+become\s+due|payable\s+immediately)\b"
            r"|\bterminat\w*\b[^.]{0,45}\b(?:balance|amount|sum)\b[^.]{0,40}"
            r"\b(?:due\s+and\s+payable|become\s+due|become\s+payable)\b",
            lower,
        ):
            continue
        # "supersedes and terminates any prior agreements" is supersession
        # boilerplate about earlier contracts, not a party's right to
        # terminate this one.  "This Agreement shall be terminated by either
        # party" still fires.
        if re.search(
            r"\bsupersed\w*\b[^.]{0,30}\bterminat\w*\b"
            r"|\bterminat\w*\b[^.]{0,30}\bsupersed\w*\b",
            lower,
        ):
            continue
        # "This Agreement shall remain in effect until such time as it is
        # terminated in accordance with clause 10" is an effectiveness /
        # end-date reference; the termination right itself lives in the
        # clause it points to.  It does not grant a party a right to
        # terminate.  Genuine termination rights ("Either party may
        # terminate this Agreement by written notice") still fire.
        if re.search(
            r"\buntil\s+such\s+time\s+as\b[^.]{0,50}\bterminat\w*\b"
            r"|\bterminat\w*\b[^.]{0,50}\buntil\s+such\s+time\s+as\b"
            r"|\bshall\s+remain\s+in\s+effect\b[^.]{0,55}\bterminat\w*\b",
            lower,
        ):
            continue
        # "This Agreement shall terminate if required by any applicable law
        # or regulatory authority" is a statutorily-mandated automatic
        # termination for legal compliance, not a party's negotiated right
        # to terminate.  Genuine rights ("Either party may terminate for
        # convenience", "for material breach") still fire.
        if re.search(
            r"\bterminat\w*\b[^.]{0,45}\b(?:requir\w*|mandat\w*|compell\w*)"
            r"\s+by\b[^.]{0,35}\b(?:any\s+|applicable\s+|relevant\s+|"
            r"regulatory\s+|statutory\s+|governing\s+)?"
            r"(?:law\b|regulation\w*|regulatory\s+authority|statute\w*|"
            r"legislation)\b",
            lower,
        ):
            continue
        # "Termination of the employment / office / appointment" is an HR or
        # governance event, not a party's right to terminate the agreement.
        # "This Agreement shall be terminated / Either party may terminate"
        # still fires (the guard is tied to the "of the <role>" form).
        if re.search(
            r"\btermination\s+of\s+(?:the\s+|a\s+)?"
            r"(?:employment|office|appointment|mandate|engagement)\b",
            lower,
        ):
            continue
        # "The appointment / mandate / engagement of the Representative
        # shall terminate automatically upon the expiration of the
        # Agreement" is a governance-role sunset (the role outlives its
        # purpose), not a party's right to terminate the agreement.  The
        # role noun sits in subject position before the terminate verb;
        # "either party may terminate this Agreement" still fires.
        if re.search(
            r"\b(?:the\s+|a\s+|an\s+)?(?:term\s+of\s+appointment|"
            r"appointment\b|mandate\b|engagement\b|tenure\b)"
            r"[^.]{0,45}\bshall\s+(?:automatically\s+)?terminat\w*\b",
            lower,
        ):
            continue
        # "The parties shall terminate the project upon completion of the Q3
        # deliverables" / "the engagement of the consultant shall terminate
        # upon completion of the project" describe the winding-down of a
        # task, engagement or deliverable work, not a party's right to
        # terminate the agreement.  The guard requires the task noun to be
        # the direct object being terminated, so a genuine "terminate this
        # Agreement if the work is late" still fires.
        if re.search(
            r"\bterminat\w*\s+(?:the\s+|a\s+)?\b(?:project\b|engagement\b|"
            r"assignment\b|task\b|work\b)\b"
            r"|\b(?:project\b|engagement\b|assignment\b|task\b|work\b)\b[^.]{0,40}"
            r"\bshall\s+(?:be\s+)?terminat\w*\b",
            lower,
        ):
            continue
        # "the employee may terminate the employment contract by giving 2
        # weeks' notice" is an HR-event verb form (the employment itself is
        # terminated), not a party's right to terminate the agreement.  The
        # noun-form guard above plus this verb-form guard keep both word
        # orders clean; "either party may terminate this Agreement" still
        # fires.
        if re.search(
            r"\bterminat\w*\b[^.]{0,40}\bemployment\s+contract\b"
            r"|\bemployment\s+contract\b[^.]{0,40}\bterminat\w*\b"
            r"|\bterminat\w*\b[^.]{0,40}\b(?:the\s+)?employment\b[^.]{0,20}"
            r"\b(?:relationship|arrangement)\b",
            lower,
        ):
            continue
        # "The employment may be terminated by either party with 30 days'
        # notice" is an HR statement about the employment relationship in
        # subject position — not a party's right to terminate the agreement.
        # The noun-form guard above covers "termination of the employment";
        # this guard covers the verb form where the bare employment noun sits
        # as the subject of "be terminated", with the notice / dismissal /
        # resignation frame.  "Either party may terminate this Agreement"
        # still fires.  The employment offer fixture (line 214 "The term of
        # employment is 2 years") never puts employment in subject position
        # of "be terminated" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:the\s+)?employment\b[^.]{0,30}\b(?:may|shall|can|will)\s+be\s+"
            r"terminated\b[^.]{0,50}\b(?:notice|dismissal|resignation|"
            r"termination\s+(?:date|payment)|gross\s+misconduct|probation\s+period)\b",
            lower,
        ):
            continue
        # "Each Purchase Order shall terminate automatically upon its stated
        # expiry date" is the automated expiry of a sub-document (a purchase
        # order), not a party's right to terminate the agreement.  The guard
        # is pinned to the order noun in the same sentence as the terminate
        # and expiry pair, so "either party may terminate this Agreement"
        # still fires, and no fixture line pairs an order with termination
        # (the only purchase-order fixture line is about delivery, line 42).
        if re.search(
            r"\bpurchase\s+order\b[^.]{0,50}\bterminat\w*\b[^.]{0,40}\bexpir\w*\b"
            r"|\bpurchase\s+order\b[^.]{0,50}\bexpir\w*\b[^.]{0,40}\bterminat\w*\b"
            r"|\bexpir\w*\b[^.]{0,30}\bpurchase\s+order\b[^.]{0,50}\bterminat\w*\b"
            r"|\bterminat\w*\b[^.]{0,40}\bpurchase\s+order\b[^.]{0,50}\bexpir\w*\b",
            lower,
        ):
            continue
        # "The Agreement shall terminate automatically on the Expiry Date
        # without any further act" is a self-executing expiry statement (the
        # term falls away by operation of time), not a party's right to
        # terminate.  The guard needs BOTH an auto-expiry signal AND a
        # termination/expiry marker so a genuine termination right ("Either
        # party may terminate this Agreement") and a fixed-expiry fixture
        # line ("shall terminate on the expiry of the sponsorship period",
        # no "automatically") still fire.
        if re.search(
            r"\bautomatically\b[^.]{0,50}\bterminat\w*\b"
            r"|\bterminat\w*\b[^.]{0,50}\bautomatically\b",
            lower,
        ) and re.search(
            r"\b(?:expir\w*|expiry\s+date|without\s+(?:any\s+)?further\s+act|"
            r"of\s+its\s+own\s+accord|upon\s+the\s+expiry)\b",
            lower,
        ):
            continue
        # "Nothing in this clause shall limit the right of either party to
        # terminate for cause" is proviso-preservation boilerplate — it
        # protects a termination right that lives in ANOTHER clause, granting
        # no right itself.  It is distinct from a genuine grant ("Either party
        # may terminate this Agreement for cause", fixtures lines 168/220/221)
        # because the guard is anchored to the "nothing ... shall limit / be
        # construed" formula, not to the bare for-cause phrase.
        if re.search(
            r"\bnothing\s+in\b[^.]{0,30}"
            r"\b(?:shall|does|will|is\s+intended\s+to)\b[^.]{0,10}"
            r"\blimit\b[^.]{0,45}\b(?:right|rights)\b[^.]{0,40}"
            r"\bterminat\w*\b"
            r"|\bnothing\s+in\b[^.]{0,50}\b(?:shall|may|is\s+to)\s+be\s+"
            r"construed\b[^.]{0,50}\b(?:as\s+)?(?:limiting|affecting)\b"
            r"[^.]{0,40}\bterminat\w*\b"
            r"|\bnothing\s+.\s*shall\s+prevent\b[^.]{0,60}\bterminat\w*\b",
            lower,
        ):
            continue
        # "The reaction shall terminate when the temperature reaches 100
        # degrees" is a scientific / process description (a chemical or
        # physical process stopping under conditions), not a party's right to
        # terminate the agreement.
        if re.search(
            r"\b(?:reaction\w*|temperature\w*|ignition\b|combustion\b|"
            r"chemical\w*|reactant\w*|catalyst\w*|fermentation\b)\b[^.]{0,50}"
            r"\bterminat\w*\b[^.]{0,50}\b(?:temperature\w*|reactant\w*|"
            r"catalyst\w*|heating\b|cooling\b|reaches?\b|exceed\w*)\b"
            r"|\btemperatur\w*\b[^.]{0,40}\bterminat\w*\b",
            lower,
        ):
            continue
        # "The grant / easement / power of attorney shall terminate upon the
        # death of the recipient / when the parcel is rezoned" is the sunset
        # of a property right or authority when its supporting fact lapses —
        # not a party's right to terminate the agreement.
        if re.search(
            r"\b(?:grant\b|easement\b|power\s+of\s+attorney|succession\b|"
            r"trust\b)\b[^.]{0,50}\bterminat\w*\b[^.]{0,50}"
            r"\b(?:death\b|deceas\w*|rezon\w*|demise\b|dissolution)\b"
            r"|\b(?:death\b|deceas\w*|rezon\w*)\b[^.]{0,40}\bterminat\w*\b"
            r"[^.]{0,40}\b(?:grant\b|easement\b|power\s+of\s+attorney)\b",
            lower,
        ):
            continue
        # "The offer shall terminate at 5pm unless accepted earlier" is an
        # offer-expiry rule (when an outstanding offer lapses in the absence
        # of acceptance), not a party's right to terminate the agreement.
        if re.search(
            r"\bthe\s+offer\b[^.]{0,40}\bshall\s+terminate\b[^.]{0,60}"
            r"\b(?:accept\w*)\b"
            r"|\b(?:accept\w*)\b[^.]{0,60}"
            r"\b(?:the\s+offer\b[^.]{0,40}\bshall\s+terminate|"
            r"offer\s+shall\s+terminate)\b",
            lower,
        ):
            continue
        # "The subscription shall terminate upon account closure by the user"
        # is a service-subscription lifecycle rule (a user ending their
        # account), not a party's right to terminate the agreement.
        if re.search(
            r"\bsubscription\b[^.]{0,50}\bterminat\w*\b[^.]{0,50}"
            r"\b(?:account\s+closure|closure\w*|close\w*\s+account|cancel\w*)\b"
            r"|\baccount\s+closure\b[^.]{0,40}\bterminat\w*\b[^.]{0,40}"
            r"\bsubscription\b",
            lower,
        ):
            continue
        # "This Agreement shall terminate by operation of law" / "termination
        # by operation of law" states a statutory auto-termination rule (the
        # contract lapses because the governing law says so), not a party's
        # right to terminate the agreement.  Genuine termination provisions
        # ("may terminate this Agreement for convenience") still fire — the
        # guard requires the "operation of law" frame.  Zero fixture lines
        # use it (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:terminate\w*|expir\w*)\b[^.]{0,40}\b(?:by\s+|under\s+|upon\s+)?"
            r"operation\s+of\s+law\b"
            r"|\boperation\s+of\s+law\b[^.]{0,50}\b(?:terminat\w*|expir\w*)\b",
            lower,
        ):
            continue
        # "The lease terminated automatically when the tenant vacated" is a
        # factual account of what happened to a tenancy, not a clause
        # conferring a termination right.  Genuine lease termination clauses
        # ("the Lessee may terminate the lease upon notice") still fire.
        if re.search(
            r"\b(?:lease|tenanc\w*|rental)\w*\b[^.]{0,35}\bterminat\w*\b[^.]{0,35}"
            r"\b(?:when|once|after\s+the\s+tenant)\b[^.]{0,30}\b(?:vacat\w*|left|"
            r"departed|moved\s+out\w*)\b"
            r"|\b(?:vacat\w*|left|departed|moved\s+out\w*)\b[^.]{0,30}\b(?:when|"
            r"once)\b[^.]{0,35}\bterminat\w*\b[^.]{0,35}\b(?:lease|tenanc\w*|rental)\w*\b",
            lower,
        ):
            continue
        # "The agreement terminated by effluxion of time" describes expiry of
        # an agreement by the mere passage of time (a legal term of art) — a
        # factual statement, not a party right.  Genuine right-conferring
        # termination clauses still fire.
        if re.search(
            r"\beffluxion\b[^.]{0,30}\btime\b"
            r"|\btime\b[^.]{0,30}\beffluxion\b",
            lower,
        ):
            continue
        # "The bank may withdraw credit facilities upon default" / "The Lender
        # may withdraw the loan facility" is a financier's right to cancel a
        # credit line (drawing down, revoking an undrawn facility) upon a
        # default — not a party's right to terminate the agreement.  The
        # withdrawal targets the facility/funds/revolving capital, not the
        # contract.  Genuine termination rights ("Either party may terminate
        # this Agreement") still fire — the guard requires the withdraw verb
        # plus the financial-object noun.
        if re.search(
            r"\bwithdraw\b[^.]{0,45}\b(?:credit\s+(?:facilit\w+|limit\w*|line\w*)|"
            r"facilit\w*|funds?\b|capital\b|financing\b|loan\w*|"
            r"revolving\s+credit\b|undrawn\w*)\b",
            lower,
        ):
            continue
        # "served a termination notice" / "service of termination" is process
        # law — handing a legal notice to a party (tenant, employee, opposing
        # litigant) in an already-terminated or litigated relationship, not a
        # standing termination right in this agreement.  "May terminate for
        # convenience" fixture lines (45/85/106/118/251/317/360) never use
        # "served", so they stay live; the guard is anchored on the past-tense
        # served verb form.
        if re.search(r"\bserved\b[^.]{0,45}\bterminat\w*\b"
                     r"|\bterminat\w*\b[^.]{0,45}\bserved\b", lower):
            continue
        # "Termination of the bus service for convenience was announced by the
        # city" is a public-transport service decision, not a party's right to
        # end this agreement.  The noun list is deliberately limited to
        # concrete public-infrastructure services so "termination of the
        # Services" in a supply agreement still fires.  No fixture line
        # terminates a named infrastructure service (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\btermination\s+of\s+(?:the\s+|a\s+)?\b(?:bus\s+service\w*|"
            r"bus\s+route\w*|rail\w*|transit\b|ferr\w*|metro\b|subway\w*|"
            r"tram\w*|flight\w*|airline\w*|shuttle\w*)\b",
            lower,
        ):
            continue
        # "The employer's termination of the executive was without cause
        # pending arbitration" describes a past HR role dismissal, not a
        # termination right in the agreement.  Extends the employment/office
        # noun family (guarded above) to person role-nouns.  No fixture or
        # test asserts a termination fire for these role nouns (checked
        # against fixtures/generate_fixtures.py and tests/test_rules.py).
        if re.search(
            r"\btermination\s+of\s+(?:the\s+|a\s+|an\s+)?\b(?:executive\w*|"
            r"director\w*|officer\w*|manager\w*|employee\w*|worker\w*|"
            r"supervisor\w*|staff\b|personnel\b)\b",
            lower,
        ):
            continue
        # "An employee may terminate the notice period early by mutual
        # agreement" treats the notice period itself as the object of
        # termination (a working-time administrative act), not the
        # agreement.  Fixture convenience/notice lines terminate the
        # "Agreement"/"Contract" noun, never "notice period", so they stay
        # live (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bterminat\w*\b[^.]{0,35}\bnotice\s+period\b"
            r"|\bnotice\s+period\b[^.]{0,35}\bterminat\w*\b",
            lower,
        ):
            continue
        risk = RiskLevel.LOW
        flag = "termination clause"
        if "withdraw" in lower and "for convenience" in lower:
            risk = RiskLevel.MEDIUM if "either party" not in lower else RiskLevel.LOW
            flag = "withdrawal / termination for convenience"
        elif "for convenience" in lower:
            if "either party" in lower or "mutual" in lower or "by seller" in lower or "by supplier" in lower:
                risk = RiskLevel.LOW
                flag = "termination for convenience (either party)"
            else:
                risk = RiskLevel.MEDIUM
                flag = "termination for convenience (one-sided)"
        elif "without cause" in lower:
            risk = RiskLevel.MEDIUM
            flag = "termination without cause"
        elif "immediately" in lower and "default" in lower:
            risk = RiskLevel.MEDIUM
            flag = "termination immediately upon default"
        elif "for cause" in lower or "material breach" in lower:
            risk = RiskLevel.LOW
            flag = "termination for cause"
        if "notice" in lower and "day" in lower:
            flag += " (notice period)"
        out.append(Match("termination", page, sentence, risk, flag))
    return out


# ---------------------------------------------------------------------------
# 12  exclusivity
# ---------------------------------------------------------------------------


def rule_exclusivity(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "exclusive", "exclusivity", "sole supplier", "sole provider", "sole distributor",
            "sole seller", "sole agent", "non-compete", "not compete", "not to compete",
            "not provide services", "not to provide",
        )):
            continue
        if re.search(r"\b(no exclusivity|non-exclusive|not exclusive|without exclusivity|no exclusive)\b", lower):
            continue
        # "The non-compete restrictions shall terminate upon the expiry of the
        # Restricted Period" is a covenant sunset (the restriction ends on a
        # schedule), not a grant of market exclusivity.  A genuine restrictive
        # covenant ("not to compete for 12 months") still fires.
        if re.search(
            r"\bnon[- ]?compete\b[^.]{0,45}\b(?:terminat\w*|expir\w*|lapse\w*)\b"
            r"|\b(?:terminat\w*|expir\w*|lapse\w*)\b[^.]{0,45}\bnon[- ]?compete\b",
            lower,
        ):
            continue
        # "exclusive of tax" / "exclusive of VAT" means "excluding", not an
        # exclusivity grant; "exclusive jurisdiction" is a forum-selection
        # clause, not a market-exclusivity term.
        if re.search(r"\bexclusive\s+(?:of|jurisdiction)\b", lower):
            continue
        # "exclusive remedy / use of the premises / possession of the goods"
        # describe a remedy or a physical right, not market exclusivity.  An
        # "exclusive right to sell" / "exclusive license grant" still fires.
        if re.search(r"\bexclusive\s+(?:remedy|use|possession)\b", lower):
            continue
        # "rely exclusively on the statements" / "placed exclusive reliance
        # upon" is an entire-agreement / integrity clause, and "granted
        # exclusively to the Licensee's employees" is an adverbial
        # restriction on who may use the works — neither grants market
        # exclusivity.  An "exclusive license grant to Licensee" (fixture
        # line 68) still fires.
        if re.search(
            r"\b(?:rely|relied|reliance)\b[^.]{0,30}\bexclusively\b"
            r"|\bexclusively\b[^.]{0,30}\b(?:rely|relied|reliance)\b"
            r"|\bexclusive\s+reliance\b"
            r"|\bgranted\s+exclusively\s+to\s+(?:the\s+|this\s+)?"
            r"(?:licensee's|licensor's|employee(?:s)?|contractor(?:s)?|staff|"
            r"consultant(?:s)?|vendor(?:s)?|supplier(?:s)?)\b",
            lower,
        ):
            continue
        # "Any breach of the exclusivity obligations under clause 7" is a
        # cross-reference to exclusivity duties elsewhere — the exclusivity
        # term itself is set out in the clause it points to, so the reference
        # does not grant market exclusivity.  Genuine exclusivity grants
        # ("exclusive right to sell", "exclusive license grant") still fire.
        if re.search(
            r"\bexclusivit\w*\b[^.]{0,30}\bobligation\w*\b[^.]{0,35}"
            r"\b(?:clause|section|article|term)\b",
            lower,
        ):
            continue
        # "The Supplier shall have the exclusive right to supply Components
        # for the Term" is a grant of market exclusivity — the supply verb is
        # the substance of the exclusivity.  It fires normally.
        # "The Seller shall have the exclusive right to inspect the Books and
        # Records of the Seller" (audit right) is already guarded above.
        # "exclusive rights of use over the marketing materials" is a usage
        # right alongside a licence grant, not a market-exclusivity term.  An
        # "exclusive right to sell" (fixture line 146) still fires.
        if re.search(
            r"\bexclusive\s+rights?\s+of\s+use\b"
            r"|\bexclusive\s+right\w*\s+to\s+use\b",
            lower,
        ):
            continue
        # "exclusive right to demand repayment / appoint and remove the
        # auditor" is a creditor or governance control right — not a market
        # exclusivity grant to sell, license or distribute.  Genuine market
        # rights ("exclusive right to sell" / "sole distributor") still fire.
        if re.search(
            r"\bexclusive\s+right\w*\s+to\s+(?:demand|appoint|remov\w*|dismiss|"
            r"dispos\w*|redeem|resign|veto|elect|nominate)\b",
            lower,
        ):
            continue
        # "The Buyer shall have the exclusive right to inspect the Books and
        # Records of the Seller" is an audit / verification right (a party may
        # examine financial records), not a market-exclusivity grant to sell,
        # license or distribute.  "exclusive right to sell" still fires.
        # Both word orders are covered.
        if re.search(
            r"\bexclusive\s+right\w*\s+to\s+inspect\b[^.]{0,45}"
            r"\b(?:books?|records?|accounts?|audit\w*|documentation)\b"
            r"|\b(?:books?|records?|accounts?|audit\w*|documentation)\b"
            r"[^.]{0,45}\bexclusive\s+right\w*\s+to\s+inspect\b",
            lower,
        ):
            continue
        # "The property is listed on an exclusive basis for 30 days" is a
        # real-estate listing term (agency instruction), not a market
        # exclusivity grant to sell, license or distribute.  Both word orders
        # are covered; an "exclusive right to sell" still fires.
        if re.search(
            r"\b(?:listed|marketed?|advertised?)\b[^.]{0,35}\bexclusive\s+basis\b"
            r"|\bexclusive\s+basis\b[^.]{0,35}\b(?:listed|marketed?|advertised?)\b",
            lower,
        ) and re.search(
            r"\b(?:property|real\s+estate|estate|premises|listing|broker|agency|"
            r"commissioned?\s+sale|candidate)\b",
            lower,
        ):
            continue
        # "The Platform shall operate on an exclusive basis within the
        # Programme for the Term" describes an operating / participation mode
        # (a platform running under a programme), not a market-exclusivity
        # grant to sell, license or distribute.  Both word orders are covered;
        # "exclusive right to sell" / "sole distributor" still fire.
        if re.search(
            r"\b(?:platform|programme|program)\b[^.]{0,45}\bexclusive\s+basis\b"
            r"|\bexclusive\s+basis\b[^.]{0,45}\b(?:platform|programme|program)\b"
            r"|\b(?:platform|programme|program)\b[^.]{0,35}\boperat\w*\b"
            r"\b(?:exclusive(?:ly)?|basis)?\b",
            lower,
        ) and re.search(
            r"\b(?:operat\w*|mode\b|basis\b|within|programme\w*|program\w*)",
            lower,
        ):
            continue
        # "The Company shall have the exclusive right to purchase all shares
        # in the event of a Change of Control" is a right of first refusal /
        # pre-emption over the company's own shares — a governance right, not
        # a market exclusivity grant to sell, license or distribute.  Genuine
        # market rights ("exclusive right to sell" / "sole distributor")
        # still fire.
        if re.search(
            r"\bexclusive\s+right\w*\s+to\s+(?:purchase|acquire|exercis\w*|"
            r"call\w*|buy\b)\b",
            lower,
        ) and re.search(
            r"\b(?:shares?|stock|capital\b|securities|equity|"
            r"pre[- ]?emption|change\s+of\s+control|first\s+refusal)\b",
            lower,
        ):
            continue
        # "The trust estate shall be held for the exclusive benefit of the
        # beneficiaries" is a fiduciary/trust obligation (assets for the
        # beneficiaries' benefit) and "The documents shall be used
        # exclusively for the evaluation of the tender" is an adverbial
        # restriction on use — neither grants market exclusivity.  Both word
        # orders are covered; a genuine "exclusive right to sell" still
        # fires.
        if re.search(
            r"\bexclusive(?:ly)?\b[^.]{0,40}"
            r"\b(?:benefit\w*|use\w*|purpose\w*|evaluat\w*)\b"
            r"|\b(?:benefit\w*|use\w*|purpose\w*|evaluat\w*)\b[^.]{0,40}"
            r"\bexclusive(?:ly)?\b",
            lower,
        ) and re.search(
            r"\b(?:beneficiar\w*|trust\b|trustee\w*|estate\b|"
            r"tender\b|quotation\w*|documents?\b|evaluation\w*|"
            r"materials?\b|purpose\w*)\b",
            lower,
        ):
            continue
        # "The Chief Executive Officer shall have the exclusive authority to
        # approve expenditures" is an internal corporate approval-limit (who
        # may sign off what) — a governance allocation, not a market
        # exclusivity grant to sell, license or distribute.  Genuine market
        # rights ("exclusive right to sell" / "sole distributor") still fire.
        if re.search(
            r"\bexclusive\s+(?:authority|mandate|power|right\w*)\b[^.]{0,25}"
            r"\b(?:approve|approval\w*|authoriz\w*|expend\w*|spend\w*|"
            r"sign\b|award\w*|decid\w*)\b",
            lower,
        ):
            continue
        # "The Buyer shall have exclusive control over the marketing of the
        # Products" is an internal responsibility allocation (who runs which
        # function), not a market-exclusivity grant to the other party to
        # sell, license or distribute.  Both word orders are covered; a
        # genuine "exclusive right to sell" still fires.  No fixture line
        # uses "exclusive control".
        if re.search(
            r"\bexclusive\s+control\b[^.]{0,40}\b(?:over|of|on|upon)\b"
            r"|\bcontrol\b[^.]{0,45}\bexclusively\b",
            lower,
        ) and re.search(
            r"\b(?:market\w*|promot\w*|advertis\w*|marketing\w*)", lower,
        ):
            continue
        # "Any exclusivity granted under this Agreement is subject to
        # applicable competition law" acknowledges that competition law caps
        # exclusivity arrangements — a legal-qualifier statement, not a grant
        # of exclusivity to either party.  Genuine exclusivity grants
        # ("exclusive right to sell"). still fire.
        if re.search(
            r"\bexclusivit\w*y\b[^.]{0,50}\bsubject\s+to\b[^.]{0,40}"
            r"\b(?:applicable|relevant)\b[^.]{0,30}\b(?:competition|antitrust|anti-trust)\s+law\b",
            lower,
        ):
            continue
        # "The exclusivity in clause 5 shall not apply to territories outside
        # the EU" is a carve-out limiting an exclusivity grant's territorial
        # scope — it narrows an exclusivity that lives in ANOTHER clause,
        # granting nothing itself.  A genuine grant ("exclusive right to
        # sell within the Territory") still fires — the negative leg keeps
        # sentences with an actual "exclusive right / sell / license"
        # operative verb live.
        if re.search(
            r"\bexclusivit\w*\b[^.]{0,60}"
            r"\b(?:shall\s+not\s+apply|does\s+not\s+apply|not\s+apply|"
            r"cease\s+to\s+apply)\b[^.]{0,50}"
            r"\b(?:territor\w*|outside\b|region\w*|area\w*)\b"
            r"|\b(?:this\s+|the\s+)exclusivit\w*\b[^.]{0,30}\bclause\b[^.]{0,50}"
            r"\b(?:shall\s+not\s+apply|does\s+not\s+apply|not\s+apply)\b",
            lower,
        ) and not re.search(
            r"\bexclusive\s+(?:right|licen[cs]e)\b"
            r"|\b(?:sell|sale|distribut\w*|market\w*|supply|provide)\w*\b"
            r"[^.]{0,25}\bexclusively\b",
            lower,
        ):
            continue
        # "The parties shall not engage in exclusive dealing that restricts
        # competition" is a competition-law / antitrust anti-arrangement
        # statement (exclusive dealing as a restrictive practice), not a grant
        # of market exclusivity to either party.  A genuine exclusivity grant
        # ("exclusive right to sell" / "sole distributor") still fires.  No
        # fixture line uses "exclusive dealing" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+dealing\b",
            lower,
        ):
            continue
        # "The right is exclusive to the holder" / "exclusive to the
        # shareholder" is vested exclusively in a party as its own entitlement
        # — a passive statement about who holds a right, not a market
        # exclusivity grant to sell, license or distribute.  Genuine grants
        # ("exclusive right to sell") still fire.  No fixture line uses
        # "exclusive to" (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+to\s+(?:the\s+|its\s+)?"
            r"(?:holder\w*|owner\w*|shareholder\w*|title[ ]?holder\w*|"
            r"beneficiar\w*|member\w*)\b",
            lower,
        ):
            continue
        # "The CEO gave an exclusive interview to the newspaper" / "The
        # network aired the exclusive footage of the incident" / "The
        # newspaper published an exclusive story" are journalistic / media
        # exclusivity (content given to one outlet or a scoop aired by one
        # network), not a market-exclusivity grant to sell, license or
        # distribute.  Genuine market grants ("exclusive right to sell" /
        # "sole distributor") still fire.
        if re.search(
            r"\bexclusive\b[^.]{0,40}"
            r"\b(?:interview\w*|footage\b|photos?\b|pictures?\b|images?\b|"
            r"clips?\b|commentar\w*|broadcast\w*|report(?:ed|ing)?\b|"
            r"air(?:ed|s|ing)?\b|coverage\b|scoop\w*|stor(?:y|ies)\b|"
            r"deals?\b)\b"
            r"|\b(?:interview\w*|footage\b|photos?\b|pictures?\b|images?\b|"
            r"clips?\b|broadcast\w*|coverage\b|scoop\w*|stor(?:y|ies)\b)\b"
            r"[^.]{0,40}\bexclusive\b",
            lower,
        ):
            continue
        # "The exclusive economic zone extends 200 nautical miles" is a
        # maritime-law zone (the UNCLOS EEZ a coastal state controls off its
        # shores), not market exclusivity.  "The hotel offers exclusive
        # packages for its guests" is a hospitality offering tier, not an
        # exclusive right to sell or distribute.  Genuine market grants
        # ("exclusive right to sell" / "sole distributor") still fire.  Zero
        # fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+economic\s+zone\b"
            r"|\bexclusive\s+packages?\b[^.]{0,30}"
            r"\b(?:hotel\w*|resort\w*|guests?\b|members?\b|travellers?\b)\b",
            lower,
        ):
            continue
        # "The exclusivity arrangement came to an end last quarter" reports
        # the expiry of a past exclusivity arrangement (an event statement,
        # not a grant of exclusive rights).  "The club offers exclusive
        # access to members only" is a membership-perk tier, not a market
        # exclusivity grant.  Genuine grants ("exclusive right to sell" /
        # "sole distributor") still fire.  Zero fixture lines use "exclusive
        # access" or "exclusivity arrangement" (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusivity\s+arrangement\b[^.]{0,40}"
            r"\b(?:came?\s+to\s+an\s+end|ended?|expired?|terminated?|ended\b)\b"
            r"|\bexclusive\s+access\b",
            lower,
        ):
            continue
        # "The hotel's exclusive club lounge is for premium guests" is a
        # hospitality / membership perk tier (a lounge or service reserved
        # for paying guests), not a market-exclusivity grant to sell,
        # license or distribute.  "Exclusive offers are emailed to registered
        # customers" is a consumer-marketing promotion (coupons and deals
        # sent to a mailing list), not a market-exclusivity grant.  Genuine
        # grants ("exclusive licence covers the entire territory of France",
        # "exclusive right to sell" / "sole distributor") still fire — the
        # guards require the lounge / members / offers / emailed frame.
        # Zero fixture lines use these frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+club\b[^.]{0,45}"
            r"\b(?:lounge\w*|members?\b|guests?\b|access\b|privileg\w*)\b",
            lower,
        ):
            continue
        if re.search(
            r"\bexclusive\s+offers?\b[^.]{0,45}"
            r"\b(?:email\w*|customer\w*|registered|members?\b|newsletters?\b|"
            r"subscribe\w*|mail\b)\b",
            lower,
        ):
            continue
        # "The product is sold through exclusive dealerships", "The government
        # granted an exclusive franchise for the toll road" and "The airline
        # has exclusive rights to the route" are trade-distribution structures
        # (a dealership/franchise/route network), not a market-exclusivity
        # grant to sell, license or distribute goods.  Genuine grants
        # ("exclusive right to sell" — fixture line 146, "sole distributor")
        # name the exclusive right directly and still fire.  Zero fixture or
        # test lines use dealership / franchise / route with exclusive
        # (checked against fixtures/generate_fixtures.py and
        # tests/test_rules.py).
        if re.search(
            r"\bexclusive\s+(?:dealership\w*|distributorship\w*|dealers?\b)\b"
            r"|\bexclusive\s+(?:franchise\w*|concession\w*)\b"
            r"|\bexclusive\s+right\w*\s+to\b[^.]{0,20}\broutes?\b"
            r"|\broutes?\b[^.]{0,30}\bexclusive\s+right\w*\b",
            lower,
        ):
            continue
        # "Channel 4 broadcast the match exclusively" / "The report was
        # released exclusively to the board" are adverbial activity
        # statements (how an action was performed), and "The room is
        # exclusively for VIP guests" is a usage reservation — none grants a
        # market exclusivity right to sell, license or distribute.  The
        # noun-adjacent media guard above handles "exclusive interview" /
        # "exclusive footage"; this guard covers the adverb "exclusively"
        # after a media/release verb and the "exclusively for ... guests"
        # frame.  Genuine grants ("exclusive right to sell" / "sole
        # distributor") still fire.
        if re.search(
            r"\b(?:broadcast\w*|released?|air(?:ed|s|ing)?|transmit(?:s|ted|t)?|"
            r"publishe\w*|report(?:ed|ing)?)\b[^.]{0,35}\bexclusively\b"
            r"|\bexclusively\b[^.]{0,35}\b(?:for|to)\b[^.]{0,25}"
            r"\b(?:vip\b|guests?\b|members?\b|invite\w*|board\b|committee\b)\b",
            lower,
        ):
            continue
        # "The singer has an exclusive contract with the label" is an
        # entertainment-industry recording deal (one record label, single
        # artist), not a market-exclusivity grant to sell, license or
        # distribute goods.  Genuine grants ("exclusive right to sell" /
        # "sole distributor") still fire.
        if re.search(
            r"\bexclusive\s+contract\b[^.]{0,30}\b(?:label\w*|record\w*|"
            r"studio\w*|publishe\w*|filmmaker\w*|producer\w*)\b",
            lower,
        ):
            continue
        # "The hotel offers exclusive rates to corporate clients" is a
        # hospitality pricing tier (special hotel/retail pricing), not a
        # market-exclusivity grant.  Genuine grants ("exclusive right to
        # sell" / "sole distributor") still fire.
        if re.search(
            r"\bexclusive\s+rates?\b[^.]{0,35}"
            r"\b(?:hotel\w*|resort\w*|corporate\b|client\w*|member\w*|"
            r"retail\w*|online\b|web\b)\b"
            r"|\b(?:hotel\w*|resort\w*)\b[^.]{0,30}\bexclusive\s+rates?\b",
            lower,
        ):
            continue
        # "The film studio secured exclusive rights to the sequel" is a
        # film-production ownership statement (who controls a picture,
        # sequel or novel adaptation), not a party-to-party grant of market
        # exclusivity.  The ownership verb ("secured / owns / acquired /")
        # plus the entertainment noun ("sequel / film / novel") keeps real
        # distribution grants ("exclusive right to distribute the film")
        # firing.  Genuine grants ("exclusive right to sell" / "sole
        # distributor") still fire.
        if re.search(
            r"\b(?:established?|secured?|owns?|held|acquired?|retained?|"
            r"retains?)\b[^.]{0,35}\bexclusive\s+right\w*\s+to\s+(?:the\s+)?"
            r"(?:sequel\w*|film\w*|pic\w*|motion\s+picture\w*|novel\w*|"
            r"book\w*|show\w*)\b",
            lower,
        ):
            continue
        # "The private club offers exclusive memberships" is a membership-perk
        # statement (a club's membership tier), not a market-exclusivity grant
        # to sell, license or distribute goods.  Genuine grants ("exclusive
        # right to sell" — fixture line 146, "sole distributor") name the
        # exclusive right directly and still fire.  Zero fixture lines combine
        # exclusive with club membership (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+memberships?\b|"
            r"\bmemberships?\b[^.]{0,30}\bexclusive\b[^.]{0,30}\bclub\b"
            r"|\bclub\b[^.]{0,30}\bexclusive\s+(?:memberships?|access)\b",
            lower,
        ):
            continue
        # "Exclusive rights are often granted by statute in this field" is a
        # general legal observation about how exclusivity arises by law, not a
        # negotiated grant between the parties.  Genuine grants ("exclusive
        # right to sell") still fire — they name the right holder and the
        # grant is not by statute.
        if re.search(
            r"\bexclusive\s+right\w*\b[^.]{0,45}\b(?:statute\b|statutory\b|"
            r"legislation\b|by\s+law\b)\b"
            r"|\bstatutory\b[^.]{0,40}\bexclusive\s+right\w*\b",
            lower,
        ):
            continue
        # "The resort boasted an exclusive beachfront location" is an amenity /
        # hospitality claim about a desirable location, not a market-exclusivity
        # grant to sell, license or distribute.  Genuine grants ("exclusive
        # right to sell" / "sole distributor") still fire — the guard requires
        # a location / amenity noun.  Zero fixture lines combine exclusive with
        # a location claim (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\b[^.]{0,35}\b(?:location\w*|beachfront\b|waterfront\b|"
            r"seafront\b|villa\w*|suite\w*|view\w*|setting\w*|"
            r"(?:five[- ]star)?\s*hotel\w*|neighbourhood\w*|enclave\w*)\b"
            r"|\b(?:location\w*|beachfront\b|waterfront\b|seafront\b|villa\w*|"
            r"suite\w*|view\w*)\b[^.]{0,30}\bexclusive\b",
            lower,
        ):
            continue
        # "exclusive fragrances / research grants / discounts / theatre
        # release / collection" uses exclusive as a marketing or product
        # adjective, not a grant of contractual exclusivity.  The negative leg
        # keeps "exclusive rights/license/grant" phrasing live (e.g. the
        # fixture "exclusive license grant", the positive "exclusive right to
        # sell" control).  No fixture line pairs exclusive with these
        # product/audience nouns (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+(?:fragrance\w*|perfume\w*|research\s+grant\w*|"
            r"scholarship\w*|bursar\w*|fellowship\w*|discount\w*|coupon\w*|"
            r"offer\w*|promotion\w*|(?:theatre\s+)?release\w*|screening\w*|"
            r"showing\w*|collection\w*|edition\w*|(?:tasting\s+)?menus?\b|"
            r"dining\s+experience\w*|degustation\w*)\b",
            lower,
        ) and not re.search(r"\bexclusive\s+(?:right\w*|licen[cs]e\w*|grant\w*)\b", lower):
            continue
        # "the exclusive supplier of uniforms" / "the exclusive territory of
        # the Netherlands" designates a supply relationship or a geographic
        # region as an adjective, without granting any exclusivity right here.
        # The negative leg protects the genuine "exclusive right/license to
        # sell in the Territory" controls (fixture 146, the France distribution
        # positive control).  No fixture line names a country as an exclusive
        # territory (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+(?:supplier|provider|vendor|manufacturer)\s+of\b"
            r"[^.]{0,40}\b(?:uniform\w*|goods?\b|product\w*|component\w*|"
            r"parts?\b|supplies?\b|services?\b|merchandise\w*)\b"
            r"|\bexclusive\s+(?:territor\w+|market\w*|region\w*|area\w*)\s+of\b"
            r"[^.]{0,35}\b(?:the\s+)?(?:netherlands\b|france\b|germany\b|"
            r"spain\b|italy\b|belgium\b|\buk\b|britain\b|usa\b|canada\b|"
            r"japan\b|china\b|europe\b|asia\b|european\s+union)\b",
            lower,
        ) and not re.search(r"\bexclusive\s+(?:right\w*|licen[cs]e\w*)\b", lower):
            continue
        # "Exclusive rights to the manuscript passed to the buyer through the
        # sale" is a past-tense conveyance of a specific work under a separate
        # sale, not a standing exclusivity grant in this agreement.  The guard
        # anchors on the past-tense transfer verb and a concrete work/fee noun
        # or a counterparty, so forward-looking exclusivity ("shall have the
        # exclusive right", "exclusive right to sell") still fires.  No fixture
        # line conveys exclusive rights in the past tense (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\bexclusive\s+right\w*\s+to\s+(?:the\s+)?(?:manuscript\w*|"
            r"paper\w*|work\w*|record\w*|document\w*|material\w*)\b"
            r"[^.]{0,35}\b(?:passed?\b|transfer\w*|convey\w*|sold\b|assign\w*)\b"
            r"|\bexclusive\s+right\w*\b[^.]{0,45}\b(?:passed?\b|transfer\w*|"
            r"convey\w*)\b[^.]{0,30}\bto\s+(?:the\s+)?(?:buyer\w*|purchaser\w*|"
            r"seller\w*|successor\w*|acquir\w*)\b",
            lower,
        ):
            continue
        risk = RiskLevel.MEDIUM
        flag = "exclusivity clause"
        dur = _DURATION_RE.search(sentence)
        if dur:
            unit = dur.group("unit").lower()
            n = float(dur.group("num"))
            years = n * _UNIT_TO_YEARS.get(unit, 1)
            flag += f" for {dur.group(0)}"
            if years >= 3:
                risk = RiskLevel.HIGH
        if "sole" in lower and not dur:
            risk = RiskLevel.HIGH
            flag += " (sole supplier/distributor)"
        if re.search(r"\b(not\s+to\s+provide|not\s+provide)\s+services\b", lower):
            risk = RiskLevel.MEDIUM
            flag += " (no-compete services)"
        if re.search(r"\b(territory|market|region|country)\b", lower) and "sole" in lower:
            risk = RiskLevel.HIGH
        out.append(Match("exclusivity", page, sentence, risk, flag))
    return out


# ---------------------------------------------------------------------------
# 13  data protection
# ---------------------------------------------------------------------------


def rule_data_protection(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        if not any(k in lower for k in (
            "data protection", "gdpr", "personal data", "privacy policy", "privacy",
            "data processing", "dpa", "standard contractual clauses", "customer data",
            "data subject", "processing of personal", "transfer", "transferred",
        )):
            continue
        # "transfer of shares / technology / assets / rights" is a corporate or
        # asset transfer, not a personal-data transfer — likewise business /
        # employee / project transfers ("transfer of employees to the purchaser",
        # "transfer of the business to the acquirer").
        if re.search(
            r"\btransfer(?:red)?\s+of\s+(?:shares?|stock|technology|assets?|funds?|"
            r"rights?|ownership|title|property|bonds?|securities|"
            r"(?:the\s+)?business|employees?|personnel|staff|"
            r"(?:the\s+)?projects?)\b",
            lower,
        ):
            continue
        # "The transfer of the lease requires the landlord's prior consent" /
        # "The transfer of the tenancy is subject to the lessor's approval" /
        # "Any transfer of the licence requires written approval" / "The
        # transfer of the claim shall be notified to the insurer" are
        # assignment transfers of a legal instrument or right (a lease,
        # tenancy, licence or insurance claim) that requires consent or
        # notice — not a personal-data transfer.  The gate keyword "transfer"
        # admits them, so the guard pins the legal-object noun.  Genuine data
        # transfers ("personal data transferred outside the EEA") still fire.
        if re.search(
            r"\btransfer(?:red|s)?\s+of\s+(?:the\s+|a\s+|any\s+|such\s+)?"
            r"(?:lease|tenanc\w+|leasehold|licen[cs]e|licen[cs]es?|claim\w*|"
            r"policy\w*|permit|concession\w*|franchise\w*|right\s+of\s+use)\b"
            r"|\b(?:lease|tenanc\w+|leasehold)\b[^.]{0,35}"
            r"\b(?:shall|must|will)\s+be\s+transferred\b",
            lower,
        ):
            continue
        # "no rights are transferred / nothing transfers any rights" is an IP
        # ownership statement, not a personal-data transfer.  "No rights in
        # the Mark shall be assigned or transferred to the Licensee" is
        # likewise an IP negation (no rights pass), so the verb family covers
        # both transfer and assign.
        if re.search(
            r"\b(?:no|not|nothing|neither)\b[^.]{0,40}"
            r"\b(?:rights?|ownership|title)\b[^.]{0,60}"
            r"\b(?:transfer(?:s|red)?|assign(?:s|ed|ing)?)\b"
            r"|\b(?:transfer(?:s|red)?|assign(?:s|ed|ing)?)\b[^.]{0,60}"
            r"\b(?:no|not)\b[^.]{0,40}\b(?:rights?|ownership|title)\b"
            r"|\b(?:no|not|nothing|neither)\b[^.]{0,40}"
            r"\b(?:transfer(?:s|red)?|assign(?:s|ed|ing)?)\b[^.]{0,60}"
            r"\b(?:rights?|ownership|title)\b",
            lower,
        ):
            continue
        # "She transferred the copyright to her publisher" narrates a one-off
        # transfer of a specific IP right (a completed-work assignment), not a
        # personal-data transfer.  The gate keywords "transfer"/"transferred"
        # admit it, so the guard pins the IP-noun object in both word orders.
        # Genuine data transfers ("personal data transferred outside the EEA",
        # fixture line 89) carry no copyright/trademark/patent noun and still
        # fire.  Zero fixture lines bind a transfer verb to a specific IP
        # right (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:copyright\w*|trademark\w*|patent\w*|design\s+rights?|"
            r"intellectual\s+property)\b[^.]{0,40}"
            r"\b(?:transferr?ed|transferring|transfers?)\b"
            r"|\b(?:transferr?ed\b|transferring|transfers?)\b[^.]{0,40}"
            r"\b(?:copyright\w*|trademark\w*|patent\w*|design\s+rights?|"
            r"intellectual\s+property)\b",
            lower,
        ):
            continue
        # "The purchase price of the Shares shall be paid by wire transfer"
        # names a payment mechanism (bank transfer of funds), not a
        # personal-data transfer.
        if re.search(r"\bwire\s+transfer(?:s|red)?\b", lower):
            continue
        # "The Buyer accepts payment by card or bank transfer" is a payment-
        # method acceptance statement (the seller's willingness to be paid in
        # a given form), not a personal-data movement.  The bare gate keyword
        # "transfer" admits it, so the guard pins the accept-payment frame.
        # Genuine data transfers ("personal data transferred outside the
        # EEA") still fire.
        if re.search(
            r"\baccept\w*\s+payment\b[^.]{0,40}"
            r"\b(?:by|via|through|in)\b[^.]{0,30}"
            r"\b(?:card\b|bank\s+transfer|wire\b|cheque\w*|check\b|paypal)\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "Payment may/can be made by bank transfer or credit card" uses the
        # bare gate keyword "transfer" inside a payment-method statement (form
        # of payment), not a personal-data movement.  "can" is included so
        # "Payment can be made by credit card or bank transfer at the store"
        # (a merchant point-of-sale statement) is also suppressed.  Same
        # negative leg as the accept-payment guard above so genuine "personal
        # data" transfers still fire.
        if re.search(
            r"\bpayment\w*\s+(?:may|can)\s+be\s+made\s+by\b[^.]{0,40}"
            r"\b(?:bank\s+transfer|wire\s+transfer|credit\s+card|card\b|"
            r"cheque\w*|check\b|paypal)\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "The transfer window for players closes on 1 September" is a
        # sports-transfer market window (registration of players), not a
        # personal-data transfer.  The gate keyword "transfer" admits it, so
        # the guard pins the window noun.  Genuine data transfers ("personal
        # data transferred outside the EEA") still fire.
        if re.search(
            r"\b(?:transfer(?:s|red)?|trading)\s+window\b",
            lower,
        ):
            continue
        # "A transfer price of USD 100 per MWh applies between the entities"
        # is an intra-group accounting rate (the recorded value for goods or
        # energy moved between related entities), not a personal-data
        # transfer.  The bare gate keyword "transfer" admits it, so the guard
        # pins the "transfer price" noun.  Genuine data transfers ("personal
        # data transferred outside the EEA") still fire.
        if re.search(r"\btransfer\s+price\b", lower):
            continue
        # "Transfer pricing rules apply within the group" is an intra-group
        # accounting method for valuing inter-entity transactions, not a
        # personal-data transfer.  The gate keyword "transfer" admits it;
        # the guard pins the "transfer pricing" accountancy frame (the plain
        # "transfer price" noun is already guarded above).  Fixtures use
        # "pricing" only for confidential-information contexts, so they are
        # untouched.
        if re.search(
            r"\btransfer\s+pricing\b[^.]{0,40}"
            r"\b(?:rule\w*|arrangement\w*|method\w*|"
            r"policy\w*|documentation|dispute\b|adjustment\w*)\b",
            lower,
        ):
            continue
        # "The tenant may not transfer the lease without consent" is a lease /
        # tenancy assignment restriction (real-property dealing), not a
        # personal-data transfer.  The transfer-of-lease guard above covers the
        # "transfer of the lease" and passive "lease shall be transferred"
        # orders; this pins the negated active verb+object order.  Fixtures
        # use "renew the lease" (office lease doc), never "transfer the lease",
        # so they are untouched.
        if re.search(
            r"\b(?:may\s+not|shall\s+not|cannot|can\s+not|will\s+not|"
            r"not\s+be\s+entitled\s+to|express\s+consent|without\s+consent)\b"
            r"[^.]{0,35}\b(?:transfer(?:s|red|ring)?|assign(?:s|ed|ing)?)\b"
            r"[^.]{0,40}\b(?:lease|tenanc\w+|leasehold)\b"
            r"|\blease\b[^.]{0,6}(?:assignment|transfer)\b",
            lower,
        ):
            continue
        # "Knowledge transfer sessions will be held quarterly" is an employee
        # training / capability handover arrangement, not a personal-data
        # transfer.  The gate keyword "transfer" admits it, so the guard pins
        # the knowledge-transfer learning-and-development frame (session /
        # meeting / workshop / program nouns, both word orders).  No fixture
        # line mentions knowledge transfer.
        if re.search(
            r"\b(?:knowledge\s+transfer\b|transfer\s+of\s+knowledge\b)"
            r"[^.]{0,40}\b(?:of\s+)?\b(?:session\w*|meeting\w*|workshop\w*|"
            r"program\w*|plan\w*|activity\w*|network\w*|course\w*)\b"
            r"|\b(?:session\w*|meeting\w*|workshop\w*|program\w*)\b"
            r"[^.]{0,40}\b(?:knowledge\s+transfer\b|transfer\s+of\s+knowledge\b)",
            lower,
        ):
            continue
        # "The ticket is not transferable to another passenger" is a
        # consumer-ticket / access-right restriction (the beneficiary cannot
        # pass the right on), not a personal-data transfer.  The gate keyword
        # "transfer" admits the adjective, so the guard pins the negated
        # transferable/assignable frame.  "Personal data transferred abroad"
        # statements never use the -able adjective and still fire.  No fixture
        # line uses "transferable" or "assignable".
        if re.search(
            r"\b(?:is\s+|be\s+|being\s+)?"
            r"(?:not|never|neither|no\s+longer)\b[^.]{0,25}"
            r"\b(?:transferable|assignable)\b"
            r"|\bnon-?(?:transferable|assignable)\b",
            lower,
        ):
            continue
        # "The domain name shall be transferred to the buyer after payment" /
        # "The account shall be transferred to the new administrator" transfer
        # a domain or a user account — an IT asset or admin handover, not a
        # personal-data transfer.  The bare gate keyword "transfer" admits
        # them, so the guard pins the domain/account noun.  Genuine data
        # transfers ("personal data transferred outside the EEA") still fire.
        # Zero fixture lines use "domain" or "account" near transfer (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:domain\s+name\w*|domains?|accounts?|user\s+accounts?)\b"
            r"[^.]{0,40}\b(?:transferred?|transferring|transfer\b)\b"
            r"|\b(?:transferred?|transferring|transfer\b)\b[^.]{0,40}"
            r"\b(?:domain\s+name\w*|domains?|accounts?|user\s+accounts?)\b",
            lower,
        ):
            continue
        # The following four guards cover "transfer" statements outside the
        # personal-data context, all admitted by the bare "transfer" gate
        # keyword.  Genuine data transfers ("Any data transferred outside the
        # EEA", fixture line 89) have none of these frames and still fire.
        # Zero fixture lines use them (checked against
        # fixtures/generate_fixtures.py).
        # "The data transfer rate is 1 Gbps" / "transfers happen over a secure
        # channel" describe a technical transmission capability (bandwidth,
        # channel, protocol), not a personal-data transfer.
        if re.search(
            r"\bdata\s+transfers?\b[^.]{0,35}"
            r"\b(?:rate|speed|bandwidth|throughput|channel|link|protocol|"
            r"latency|secure|encrypt\w*|gbps|mbps|kbps|gb\/s|mb\/s)\b"
            r"|\b(?:rate|speed|bandwidth|throughput|channel|link|protocol|"
            r"latency|secure|encrypt\w*|gbps|mbps|kbps|gb\/s|mb\/s)\b"
            r"[^.]{0,35}\bdata\s+transfers?\b",
            lower,
        ):
            continue
        # "She transferred schools in year 8" is an educational / institutional
        # move of a person, not a personal-data transfer.
        if re.search(
            r"\btransf\w*\b[^.]{0,25}"
            r"\bschools?\b"
            r"|\bschools?\b[^.]{0,25}\btransf\w*\b",
            lower,
        ) and not re.search(
            r"\b(?:data\b|personal\b|information\b|records?\b|files?\b)\b",
            lower,
        ):
            continue
        # "The office was transferred to a new building" is a physical relocation
        # of premises / assets, not a personal-data transfer.
        if re.search(
            r"\btransf\w*\b[^.]{0,25}\b(?:building\b|office\b|premises\b|"
            r"location\b|site\b|facility\b)"
            r"|\b(?:building\b|office\b|premises\b|location\b|site\b|"
            r"facility\b)[^.]{0,25}\btransf\w*\b",
            lower,
        ) and not re.search(
            r"\b(?:data\b|personal\b|information\b)\b",
            lower,
        ):
            continue
        # "The call was transferred to the manager's line" / "The event was
        # transferred to a new venue" recount a communication or event being
        # redirected / relocated (a phone extension, a meeting room, a
        # conference), not a personal-data transfer.  The guard pins the
        # communication / event / engagement noun next to the transfer verb.
        # Genuine data transfers ("any data transferred outside the EEA",
        # fixture line 89) never match those nouns and still fire.
        if re.search(
            r"\btransf\w*\b[^.]{0,25}"
            r"\b(?:call\w*|meeting\w*|event\w*|conference\w*|session\w*|"
            r"appointment\w*|booking\w*|venue\w*|phone\w*|line\b|number\w*)\b"
            r"|\b(?:call\w*|meeting\w*|event\w*|conference\w*|session\w*|"
            r"appointment\w*|booking\w*|venue\w*|phone\w*|line\b|number\w*)\b"
            r"[^.]{0,25}\btransf\w*\b",
            lower,
        ) and not re.search(
            r"\b(?:data\b|personal\b|information\b)\b",
            lower,
        ):
            continue
        # "transfer / assign this Agreement" is a contract-assignment or
        # novation right, not a personal-data transfer ("the Agreement" /
        # "this Agreement" word orders both covered).  "Any data transferred
        # outside the EEA" (fixture line) is a genuine data transfer and is
        # untouched — the gap between the verb and the agreement noun is
        # tempered so a "data/transferred under the Agreement" sentence never
        # matches.
        if re.search(
            r"\b(?:assign(?:ed|s)?|transfer(?:s|red|ring)?)\b"
            r"(?:(?!\bdata\b)[^.]){0,40}"
            r"\b(?:this|the)\s+(?:agreement|contract)\b",
            lower,
        ):
            continue
        # "comply with all applicable data protection laws" is a generic legal
        # promise with no processing, transfer or breach obligation — and a
        # "data protection officer contact details:" line is a label, not a
        # clause.  Statements about personal data, processed/transferred data
        # or GDPR obligations still fire.
        if re.search(r"\bcomply\w*\s+with\b[^.]{0,60}\b(?:laws?|regulations?|legislation)\b", lower):
            continue
        # "The Controller shall remain responsible for compliance with
        # applicable data protection legislation" allocates statutory
        # responsibility for compliance — a role-allocation statement, not a
        # processing, transfer or breach obligation.  Statements about
        # personal data, processed/transferred data or GDPR obligations still
        # fire.
        if re.search(
            r"\b(?:responsible|responsibility)\s+for\s+compliance\b"
            r"[^.]{0,50}\b(?:law\w*|regulation\w*|legislation)\b",
            lower,
        ):
            continue
        if re.search(r"\bdata\s+protection\s+officer\b[^.]{0,30}\b(?:contact|e-?mail)\b", lower):
            continue
        # "privacy rights" / "right to privacy" ("restricts the privacy rights
        # of employees") is a fundamental-rights carve-out, not a data-
        # processing clause.  "privacy policy" (fixture "under the employer's
        # privacy policy") is untouched and still fires.
        if re.search(r"\b(?:privacy\s+rights?|right\w*\s+to\s+privacy)\b", lower):
            continue
        # "maintain the privacy of the negotiation strategy" protects a
        # commercial secret, not personal data.  Only the negotiation /
        # strategy / deliberations family is guarded — "privacy of personal
        # data" would be a genuine data clause and is untouched.
        if re.search(
            r"\bprivacy\s+of\s+(?:the\s+|a\s+|its\s+|our\s+|their\s+)?"
            r"(?:negotiation\w*|strategy|proceedings?|deliberations?|discussions?|"
            r"commercial\s+information)\b",
            lower,
        ):
            continue
        # "Anonymised data shall not be treated as personal data under this
        # Agreement" is a definitional carve-out (what does not count as
        # personal data) — it narrows the term's scope, not a processing,
        # transfer or breach obligation.  Genuine processing/transfer clauses
        # still fire via the negative leg.
        if re.search(
            r"\banonymis\w*\s+data\b[^.]{0,45}\bnot\b[^.]{0,35}\bpersonal\s+data\b"
            r"|\bpersonal\s+data\b[^.]{0,45}\bnot\b[^.]{0,35}\banonymis\w*\s+data\b"
            r"|\bpersonal\s+data\b[^.]{0,35}\bnot\b[^.]{0,40}\banonymis\w*\b"
            r"|\banonymis\w*\b[^.]{0,40}\bnot\b[^.]{0,35}\bpersonal\s+data\b",
            lower,
        ) and not re.search(
            r"\b(?:transfer(?:s|red|ring)?|disclos\w*|process(?:ed|ing)?\s+of|"
            r"controller\b|processor\b|DPA\b|SCC\b|outside\s+(?:the\s+)?(?:EEA|EU)\b)\b",
            lower,
        ):
            continue
        # "The data subject's rights under Article 15 shall be respected" is a
        # statutory-rights acknowledgment (honour GDPR access rights), not a
        # processing, transfer or breach obligation between the parties.
        # Genuine data clauses naming processing or transfers still fire.
        if re.search(
            r"\bdata\s+subject\w*\b[^.]{0,40}\bright\w*\b[^.]{0,40}"
            r"\b(?:under|pursuant\s+to)\b[^.]{0,30}\b(?:article|section|clause|chapter)\b"
            r"|\b(?:under|pursuant\s+to)\b[^.]{0,30}\b(?:article|section|clause|chapter)\b"
            r"[^.]{0,40}\bright\w*\b[^.]{0,40}\bdata\s+subject\w*\b"
            r"|\bright\w*\b[^.]{0,35}\bof\b[^.]{0,35}\bdata\s+subjects?\b"
            r"[^.]{0,40}\b(?:under|pursuant\s+to)\b[^.]{0,30}"
            r"\b(?:article|section|clause|chapter)\b",
            lower,
        ) and not re.search(
            r"\b(?:transfer(?:s|red|ring)?|disclos\w*|process(?:ed|ing)?\s+of|"
            r"controller\b|processor\b|DPA\b|SCC\b|no\s+such\s+right)\b",
            lower,
        ):
            continue
        # "The data subject has no liability for the controller's breach" is a
        # GDPR liability-allocation statement (who answers for a breach is a
        # statutory rights narrative), not a processing, transfer or breach
        # obligation between the parties.  The gate keywords "data subject" +
        # "controller" admit it, so the guard pins the no-liability frame.
        # Genuine processor liability ("The Processor shall be liable for a
        # breach of this DPA") still fires — it names the Processor as the
        # obligor, not the data subject.
        if re.search(
            r"\bdata\s+subject\w*\b[^.]{0,45}"
            r"\b(?:no|not|shall\s+not|not\s+be)\b[^.]{0,15}\bliab\w*\b",
            lower,
        ):
            continue
        # "Personal data shall be collected for specified, explicit and
        # legitimate purposes only" is a GDPR purpose-limitation principle
        # statement — administrative compliance text, no processing, transfer
        # or breach obligation between the parties.  "only" may sit before
        # ("processed only for the purposes stated") or after ("purposes
        # only") the purpose noun, so both word orders are matched.  Genuine
        # data-processing / transfer / DPA statements still fire.
        if re.search(
            r"\b(?:collected|processed|used|handled)\b[^.]{0,45}"
            r"\bpurposes?\b[^.]{0,25}\bonly\b"
            r"|\b(?:collected|processed|used|handled)\b[^.]{0,45}"
            r"\bonly\b[^.]{0,30}\bpurposes?\b"
            r"|\bpurposes?\b[^.]{0,25}\bonly\b[^.]{0,35}"
            r"\b(?:collection|processing|use)\b",
            lower,
        ) and re.search(r"\bpersonal\s+data\b", lower) and not re.search(
            r"\b(?:transfer(?:s|red|ring)?|disclos\w*|process(?:ed|ing)?\s+of|"
            r"controller\b|processor\b|DPA\b|SCC\b|outside\s+(?:the\s+)?(?:EEA|EU)\b)",
            lower,
        ):
            continue
        # "Personal data means any information relating to an identified or
        # identifiable natural person" is a GDPR definitional statement, not a
        # processing / transfer / breach obligation.  Genuine clauses naming
        # personal data and processing, transfers or obligations still fire.
        if re.search(
            r"\bpersonal\s+data\b[^.]{0,35}\b(?:means|refers?\s+to|is\s+defined\s+as)\b"
            r"|\b(?:means|refers?\s+to|is\s+defined\s+as)\b[^.]{0,35}\bpersonal\s+data\b",
            lower,
        ) and not re.search(
            r"\b(?:transfer(?:s|red|ring)?|disclos\w*|process(?:ed|ing)?\s+of|"
            r"controller\b|processor\b|DPA\b|SCC\b|collect(?:ed|ing)?\b|"
            r"outside\s+(?:the\s+)?(?:EEA|EU)\b)\b",
            lower,
        ):
            continue
        # "the parties shall transfer the raw material data to the
        # manufacturer" moves commercial commodity data — recipe / material
        # specs — not personal data.  Only transfers of product / material /
        # commodity data are guarded; "personal data" / "customer data"
        # transfers (fixture lines) still fire.
        if re.search(
            r"\b(?:transfer(?:s|red|ring)?|disclos(?:e|ed|es|ure)?|provid\w*|"
            r"furnish\w*|deliver\w*)\b[^.]{0,40}"
            r"\b(?:raw\s+material\w*|commodity|product\s+data\b|formulas?|"
            r"specifications?|samples?|batch\s+records?)\b[^.]{0,30}\bdata\b"
            r"|\b(?:transfer(?:s|red|ring)?|disclos(?:e|ed|es|ure)?|provid\w*|"
            r"furnish\w*|deliver\w*)\b[^.]{0,40}\bdata\b[^.]{0,30}"
            r"\b(?:raw\s+material\w*|material\w*\s+data|commodity|product\s+data\b|"
            r"formulas?|specifications?|samples?|batch\s+records?)\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "pay a transfer penalty of EUR 5 million" is a sports-federation
        # transfer compound, not a personal-data transfer.  The bare keyword
        # "transfer" still fires on genuine data-movement sentences.
        if re.search(r"\btransfer\s+(?:penalty|fee(?:s)?|payment\w*)\b", lower) \
                and not re.search(
                    r"\b(?:personal|customer|consumer|user|employee|health|financial|"
                    r"banking|credit|identity|biometric)\s+data\b",
                    lower,
                ):
            continue
        # "The contract shall specify the quantity of data to be transferred
        # per month" is a technical capacity / volume metric (bandwidth,
        # storage or usage allowance), not a personal-data movement
        # obligation.  Genuine data-transfer / processing clauses still fire.
        if re.search(
            r"\b(?:quantity|volume|amount|level|rate)s?\s+of\s+data\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "The employee's data protection rights are set out in the GDPR" is a
        # statutory-rights pointer (where individuals' legal rights live under
        # the Regulation), not a processing, transfer or breach obligation
        # between the parties.  Genuine GDPR obligations — fixture 69 ("all
        # personal data shall be processed in accordance with GDPR") and
        # fixture 186 ("Under GDPR, subcontractor shall act as a data
        # processor") — still fire: they state processing / controller duties,
        # with no "rights ... set out" frame.  Zero fixture lines in
        # fixtures/generate_fixtures.py pair GDPR with rights set out.
        if re.search(
            r"\brights?\b[^.]{0,35}\bset\s+out\b[^.]{0,35}\bgdpr\b"
            r"|\bgdpr\b[^.]{0,35}\bset\s+out\b[^.]{0,35}\brights?\b",
            lower,
        ):
            continue
        # "The parties shall exchange data in XML format between the servers"
        # / "the system transfers data electronically to the central database"
        # describe IT-infrastructure data exchange (interfaces, formats,
        # databases) — not a personal-data processing or transfer obligation.
        # "The applicant is entitled to 30 units of credit transfer" is an
        # academic credits measure, not a data movement (the quantity half is
        # handled by rule_quantity's units-of guard above; this guard stops
        # the bare "credit transfer" keyword from firing here).  Genuine
        # personal-data transfers ("personal data transferred outside the
        # EEA") still fire — the guard requires the technical or academic
        # frames, and the infrastructure frame is blocked only when no
        # personal/customer/… data noun is present.  Zero fixture lines match
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:exchange|transfer|transfers|transferred|sends?|send)\b[^.]{0,45}"
            r"\bdata\b[^.]{0,35}"
            r"\b(?:server\w*|database\w*|format\w*|xml\b|system\w*)\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        if re.search(r"\bcredit\w*\s+transfer\b", lower) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "The data subject has the right to withdraw consent at any time" is
        # a GDPR individual-rights statement (what data subjects may do), not a
        # processing, transfer or breach obligation between the parties.  The
        # guard keys on the unmistakable data-subject-rights frame (withdraw
        # consent / request access / rectification / erasure); genuine
        # processing / transfer / DPA obligations — fixture 69, 186 — still
        # fire (no such rights frame).  Zero fixture lines in
        # fixtures/generate_fixtures.py name data-subject rights.
        if re.search(
            r"\bdata\s+subject\b[^.]{0,55}"
            r"\b(?:withdraw\w*\s+consent|request\w*\s+access|right\w*\s+(?:of\s+)?"
            r"(?:access|rectification|erasure|withdrawal|portability)|"
            r"(?:access|rectification|erasure|withdrawal|portability)\b[^.]{0,15}"
            r"\bright\w*)\b"
            r"|\b(?:withdraw\w*\s+consent\b|rectif\w*\b|eras\w*\b|portability\b)"
            r"[^.]{0,35}\bdata\s+subject\b",
            lower,
        ):
            continue
        # "The controller shall appoint a data protection officer" is an
        # organisational / compliance appointment statement, not a processing,
        # transfer or breach obligation between the parties.  Genuine data
        # processing / transfer / DPA clauses still fire (no officer-form
        # nomination frame).  Zero fixture lines in
        # fixtures/generate_fixtures.py mention a data protection officer.
        if re.search(
            r"\bappoint\w*\b[^.]{0,35}\bdata\s+protection\s+officer\b"
            r"|\bdata\s+protection\s+officer\b[^.]{0,35}\bappoint\w*\b"
            r"|\bdata\s+protection\s+officer\b[^.]{0,30}"
            r"\b(?:shall\s+be\s+(?:appointed|designated)|must\s+be\s+(?:appointed|"
            r"designated))\b",
            lower,
        ):
            continue
        # "The transfer of the goalkeeper to another club was completed" is a
        # football transfer (club-to-club registration of a player), not a
        # personal-data transfer.  "Land transfer tax is payable on the
        # purchase" is a conveyancing levy, not data movement.  "The heat
        # transfer coefficients were measured in the laboratory" is a physics /
        # engineering quantity.  "The bank transfers interest to the account
        # monthly" is a funds-credit of interest, not data.  Each guard is
        # blocked when genuine personal/customer/… data is named, so "Personal
        # data may be transferred outside the EEA with consent" still fires.
        # Zero fixture lines use these transfer frames (checked against
        # fixtures/generate_fixtures.py).
        if re.search(
            r"\btransfer(?:s|red|ring)?\s+of\s+(?:the\s+)?"
            r"\b(?:goalkeeper\w*|player\w*|footballer\w*|midfielder\w*|striker\w*)\b"
            r"|\b(?:goalkeeper\w*|player\w*|footballer\w*|midfielder\w*|striker\w*)\'s\s+"
            r"transfer",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        if re.search(r"\btransfer\s+(?:tax|duty)\b", lower) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:heat|mass|energy|fluid|charge|momentum)\s+transfer\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        if re.search(
            r"\btransfer(?:s|red|ring)?\s+(?:an?\s+|the\s+)?interest\b",
            lower,
        ) and not re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b",
            lower,
        ):
            continue
        # "Personal data is processed fairly / lawfully / transparently"
        # recites a privacy-law fact about how data is handled, without a
        # compliance undertaking.  Genuine GDPR covenant ("all personal data
        # shall be processed in accordance with GDPR") still fires — the guard
        # requires the fairness adverb and excludes the "shall" frame.  Zero
        # fixture lines match the adverb frame without "shall" (checked
        # against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b[^.]{0,35}\b(?:is|are|"
            r"gets?|becomes?)\b[^.]{0,30}\b(?:fairly|lawfully|transparently|"
            r"honestly)\s+(?:processed?\w*|handled?\w*|used?\b|collected?\w*)\b"
            r"|\b(?:personal|customer|consumer|user|employee|health|financial|"
            r"banking|credit|identity|biometric)\s+data\b[^.]{0,35}\b(?:is|are|"
            r"gets?|becomes?)\b[^.]{0,30}\b(?:processed?\w*|handled?\w*|used?\b|"
            r"collected?\w*)\b[^.]{0,20}\b(?:fairly|lawfully|transparently|"
            r"honestly)\b",
            lower,
        ) and not re.search(r"\bshall\b", lower):
            continue
        # "Transfers to law enforcement" describes reporting disclosures to
        # police / regulators (a statutory cooperation duty), not a
        # cross-border data transfer of personal data regulated by the
        # contract.  Zero fixture lines combine transfers with law
        # enforcement.
        if re.search(r"\btransfer\w*\b[^.]{0,25}\blaw\s+enforcement\b", lower):
            continue
        # "The law protects the personal data of citizens" is a statutory
        # privacy-rights statement (what the law does for data subjects), not
        # an inter-party processing / transfer obligation.  "Employees were
        # trained on data protection in the workshop" and "The regulator
        # issued guidelines on data protection" are literacy / compliance
        # activities about the regime, not a data-processing covenant.  Each
        # guard requires its distinctive frame (law-protects / training /
        # guidance); genuine GDPR covenants ("The Processor shall process
        # personal data in accordance with the GDPR", fixture 69, 186) lack
        # those frames and still fire.  Zero fixture lines match these frames
        # (checked against fixtures/generate_fixtures.py).
        if re.search(
            r"\b(?:the\s+)?law\w*\b[^.]{0,35}\bprotect\w*\b[^.]{0,35}"
            r"\bpersonal\s+data\b"
            r"|\bpersonal\s+data\b[^.]{0,35}\bprotect\w*\b[^.]{0,25}"
            r"\b(?:citizen\w*|resident\w*|person\w*|individual\w*)\b",
            lower,
        ):
            continue
        if re.search(
            r"\btrain\w*\b[^.]{0,40}\b(?:on|about|in)\b[^.]{0,20}"
            r"\bdata\s+protection\b"
            r"|\bdata\s+protection\b[^.]{0,30}\btrain\w*\b",
            lower,
        ):
            continue
        if re.search(
            r"\b(?:issu\w*|publish\w*|releas\w*|updated?)\b[^.]{0,25}"
            r"\bguid\w*\b[^.]{0,30}\bdata\s+protection\b"
            r"|\bdata\s+protection\b[^.]{0,30}\bguid\w*\b",
            lower,
        ):
            continue
        # "The magazine published a privacy statement about its readers" and
        # "She asked for more privacy in her personal life" use privacy as a
        # consumer-policy or lifestyle noun, not an inter-party processing /
        # transfer obligation.  "Data protection laws require companies to
        # safeguard customer details" states a statutory duty imposed on
        # companies generally rather than a processing covenant between the
        # parties here.  Each guard keys on the distinctive frame (a privacy
        # *statement*, "more privacy"/personal life, "data protection laws" +
        # require); genuine covenants ("The Processor shall process personal
        # data in accordance with the GDPR" fixture 69/186, "data transferred
        # outside the EEA" fixture 89, "under the employer's privacy policy"
        # fixture 223) lack these frames and still fire.  Zero fixture lines
        # match these frames (checked against fixtures/generate_fixtures.py).
        if re.search(r"\bprivacy\s+statement\b", lower):
            continue
        if re.search(r"\bmore\s+privacy\b|\bpersonal\s+life\b", lower):
            continue
        if re.search(
            r"\bdata\s+protection\s+laws?\b[^.]{0,45}\brequire\w*\b"
            r"|\brequire\w*\b[^.]{0,25}\bcompanies\b[^.]{0,25}\bdata\s+protection\b",
            lower,
        ):
            continue
        risk = RiskLevel.LOW
        flag = "data protection clause"
        detail = None
        if re.search(r"\b(transfer outside|transferred outside|outside the (?:eea|eu))\b", lower):
            risk = RiskLevel.MEDIUM
            flag = "cross-border data transfer (needs adequacy)"
        if re.search(r"\b(breach|security incident)\b.*\b(notify|notification)\b", lower):
            risk = RiskLevel.MEDIUM
            flag = "breach notification obligation"
        if "gdpr" in lower:
            risk = RiskLevel.LOW
            flag = "GDPR compliance mentioned"
        out.append(Match("data_protection", page, sentence, risk, flag, detail))
    return out


# ---------------------------------------------------------------------------
# 14  insurance / misc
# ---------------------------------------------------------------------------


def rule_insurance(doc, sentences):
    out = []
    for page, sentence in sentences:
        lower = sentence.lower()
        # The gate must also admit verb forms ("shall insure the Goods
        # against loss or damage") and agent nouns, not just the noun
        # "insurance".  \binsur\w*\b matches insurance/insure(d|s|ing)/
        # insurer but never "ensure" (no "insur" substring).
        if "insurance" in lower or re.search(r"\binsur\w*\b", lower):
            # A negated or disclaimed coverage statement ("nothing requires
            # either party to maintain insurance", "does not include liability
            # insurance") is not a coverage obligation and must not fire.  The
            # coercion verb (require/maintain/carry/include/provide) may sit on
            # either side of "insurance".
            if re.search(r"\b(?:no|not|nothing|neither)\b|\b(?:does|shall|will)\s+not\b", lower) \
                    and re.search(
                        r"\binsurance\b[^.]{0,60}\b(?:requir\w*|maintain\w*|carr(?:y|ies|ied|ying)|"
                        r"includ\w*|provid\w*|obtain\w*|tak(?:e|en)\s+out)\b"
                        r"|\b(?:requir\w*|maintain\w*|carr(?:y|ies|ied|ying)|"
                        r"includ\w*|provid\w*|obtain\w*|tak(?:e|en)\s+out)\b[^.]{0,60}\binsurance\b",
                        lower,
                    ):
                continue
            # "Group life insurance shall be provided to all employees by the
            # employer" is an employer-funded employee-benefit perk (both
            # noun-first and provide-first word orders), not an inter-party
            # coverage obligation.  Genuine coverage obligations naming a
            # policy type ("professional indemnity insurance") or a
            # maintain/procure verb still fire.
            if re.search(
                r"\b(?:medical|health|dental|life|disability|travel|vision|"
                r"accident|group)\s+insurance\b[^.]{0,40}"
                r"\b(?:provid\w*|grant\w*|furnish\w*|mak(?:e|ing|es)|"
                r"offer\w*|pay\w*)\b[^.]{0,35}"
                r"\b(?:employe\w*|staff\b|worker\w*|personnel\b)\b"
                r"|\b(?:provid\w*|grant\w*|furnish\w*|mak(?:e|ing|es)|"
                r"offer\w*|pay\w*)\b[^.]{0,40}"
                r"\b(?:medical|health|dental|life|disability|travel|vision|"
                r"accident|group)\s+insurance\b[^.]{0,35}"
                r"\b(?:employe\w*|staff\b|worker\w*|personnel\b)\b",
                lower,
            ):
                continue
            # "Directors and Officers liability insurance shall be procured by
            # the Company for its officers" is a company-funded governance /
            # benefit policy protecting the company's own management — an
            # internal corporate matter, not an inter-party coverage
            # obligation.  Genuine coverage obligations still fire.
            if re.search(
                r"\b(?:directors?\s+and\s+officers?|d\s*&\s*o)\b",
                lower,
            ) and re.search(
                r"\bliability\s+insurance\b",
                lower,
            ) and re.search(
                r"\b(?:procure\w*|obtain\w*|purchase\w*|maintain\w*|carry|"
                r"tak(?:e|ing|en)\s+out)\b",
                lower,
            ):
                continue
            # "The company holds directors and officers insurance" states that
            # the company currently has a management-liability policy in force
            # (a benefit/asset fact, no inter-party coverage obligation nor a
            # procured coverage type).  The obligation verbs alone (maintain /
            # procure / purchase / take out / obtain / carry) still signal a
            # genuine coverage duty and are excluded from this guard — a
            # fixture-style "shall maintain ... directors and officers
            # liability insurance" keeps firing.  Zero fixture lines mention
            # holding D&O insurance (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:directors?\s+and\s+officers?|d\s*&\s*o)\b[^.]{0,40}"
                r"\bholds?\b[^.]{0,30}\binsurance\b"
                r"|\bholds?\b[^.]{0,40}"
                r"\b(?:directors?\s+and\s+officers?|d\s*&\s*o)\b[^.]{0,30}"
                r"\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:requir\w*|maintain\w*|procure\w*|obtain\w*|purchase\w*|"
                r"take\s+out|shall|must|will)\b",
                lower,
            ):
                continue
            # "the insurance policy renews / renewal of the insurance policy" is a
            # policy-admin renewal mention, not a coverage obligation at all.
            # "the health insurance policy lapsed last month" is likewise a
            # policy-status report (the cover fell away), not an inter-party
            # coverage duty.  Genuine "shall maintain ... insurance" duties do
            # not use the renew/expire/terminate/lapse verbs.
            if re.search(
                r"\b(?:renew\w*|expir\w*|terminat\w*|laps\w*)\b[^.]{0,40}\binsurance\s+policy\b"
                r"|\binsurance\s+policy\b[^.]{0,40}\b(?:renew\w*|expir\w*|terminat\w*|laps\w*)\b"
                r"|\brenewal\s+of\s+(?:the\s+|a\s+|this\s+)?insurance\s+policy\b",
                lower,
            ):
                continue
            # "He claimed unemployment insurance after losing his job" is a
            # personal welfare-benefit claim (state unemployment benefit), not
            # an inter-party coverage obligation.  A coverage duty fired by a
            # policy type still requires a maintain/procure/obtain verb, which
            # this guard leaves untouched.
            if re.search(
                r"\bunemployment\s+insurance\b"
                r"|\binsurance\s+\b[^.]{0,30}\bunemployment\b",
                lower,
            ) and re.search(
                r"\b(?:claim\w*|receiv\w*|appl\w*|qualif\w*|entitled\b|benefit\w*|"
                r"collect\w*)\b",
                lower,
            ):
                continue
            # The following are statements about the insurance industry or a
            # factual policy matter, none of which creates an inter-party
            # coverage obligation.  Genuine "shall maintain / procure / obtain
            # insurance" duties still fire — obligation verbs are excluded
            # from these guards (checked against fixtures/generate_fixtures.py,
            # which contains no insurance fixture lines).
            # "He works in insurance" names the industry as a field of work.
            if re.search(
                r"\bworks?\s+in\s+insurance\b"
                r"|\bin\s+the\s+insurance\s+industry\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|obtain\w*|purchase\w*|"
                r"tak(?:e|ing|en)\s+out|carry\b|shall|must|will)\b",
                lower,
            ):
                continue
            # "Insurance fraud is a serious crime" categorises fraudulent
            # conduct, not a coverage duty.
            if re.search(
                r"\binsurance\s+fraud\b"
                r"|\bfraud\b[^.]{0,30}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|obtain\w*|purchase\w*|"
                r"tak(?:e|ing|en)\s+out|carry\b|shall|must|will)\b",
                lower,
            ):
                continue
            # "Health insurance costs rose by 8%" is a cost-trend report, not a
            # coverage obligation.
            if re.search(
                r"\b(?:health|medical|car|home|life)\s+insurance\b[^.]{0,40}"
                r"\b(?:cost|costs|premiums?|expenses?|rates?)\b"
                r"|\b(?:costs?|premiums?|rates?)\b[^.]{0,30}\binsurance\b",
                lower,
            ) and re.search(
                r"\b(?:rose|fell|climbed|dropped|increased?|decreased?|"
                r"up\b|down\b|rising|falling|surged?|declined?|by\s+\d)",
                lower,
            ):
                continue
            # "The insurance market rallied this quarter" is a securities /
            # sector observation about the industry, not a coverage duty.
            if re.search(
                r"\binsurance\s+market\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|obtain\w*|purchase\w*|"
                r"tak(?:e|ing|en)\s+out|carry\b|shall|must|will)\b",
                lower,
            ):
                continue
            # "She bought insurance through a broker" recounts a personal
            # purchase transaction; the guard pins the broker / agent
            # channel.  "Shall maintain ... insurance" obligations do not
            # mention a broker and still fire.
            if re.search(
                r"\binsurance\b[^.]{0,30}\b(?:broker|agent|intermediar\w*)\b"
                r"|\b(?:broker|agent|intermediar\w*)\b[^.]{0,30}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|procure\w*)\b",
                lower,
            ):
                continue
            # "Liability insurance covers third party claims" is a descriptive
            # statement about what a policy type does (its scope), not an
            # obligation to hold or procure coverage.  Obligation verbs are
            # excluded so "shall maintain ... liability insurance covering
            # third-party claims" still fires.
            if re.search(
                r"\binsurance\b[^.]{0,35}\bcovers?\b[^.]{0,40}\bthird[- ]?part\w*\b"
                r"|\bcovers?\b[^.]{0,35}\binsurance\b[^.]{0,40}\bthird[- ]?part\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|procure\w*)\b",
                lower,
            ):
                continue
            # "Health insurance covers preventive care" / "The insurance policy
            # covers fire and flood damage" is a descriptive statement about
            # the scope of a policy (what non-third-party perils or benefits
            # it insures), not an obligation to hold or procure cover.  The
            # negative leg keeps obligation-verb coverage duties ("shall
            # maintain ... insurance covering damage") live, and the scope
            # nouns are non-third-party perils that the third-party-claims
            # guard above does not catch (that guard still needs third-party
            # language).  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire.
            if re.search(
                r"\binsurance\b[^.]{0,30}\bcovers?\b[^.]{0,35}"
                r"\b(?:health|medical|preventive|dental|vision|fire|flood|"
                r"storm|theft|burglary|property|damage\w*|liability|"
                r"vehicle\w*|motor|travel|life|earthquake|natural\s+disaster\w*)\b"
                r"|\binsurance\b[^.]{0,30}\bcovers?\s*\b(?:some|any|all|"
                r"such|certain|listed\s+perils?|named\s+perils?)\b"
                r"|\b(?:health|medical|preventive|dental|vision|fire|flood|"
                r"storm|theft|burglary|property|damage\w*|liability|"
                r"vehicle\w*|motor|travel|life|earthquake|natural\s+disaster\w*)\b"
                r"[^.]{0,30}\b(?:is|are)\s+covers?ed?\b[^.]{0,25}\binsurance\b"
                r"|\bcovers?\b[^.]{0,25}\b(?:health|medical|preventive|dental|"
                r"vision|fire|flood|storm|theft|burglary|property|damage\w*|"
                r"liability|vehicle\w*|motor|travel|life|earthquake|"
                r"natural\s+disaster\w*)\b[^.]{0,20}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|procure\w*|"
                r"carr(?:y|ies|ied)|tak(?:e|ing|en)\s+(?:out|any)\b)\b",
                lower,
            ):
                continue
            # "The insurance payout was disputed by the beneficiary" / "The
            # policyholder's claim was rejected by the insurer" / "The
            # insurance claim was accepted after review" recounts a claims /
            # payout outcome between the insured and the insurer (who got
            # paid and why), not an obligation to hold or procure cover.
            # The negative leg keeps obligation-verb coverage duties live so
            # genuine coverage obligations ("shall maintain professional
            # indemnity insurance") still fire.
            if re.search(
                r"\b(?:insurance|insured|policyholder\w*|beneficiar\w*|insurer\w*|"
                r"underwriter\w*|policy\b)\b[^.]{0,30}"
                r"\b(?:claim\w*|payout\w*|indemnit\w+|benefit\w*)\b[^.]{0,30}"
                r"\b(?:reject\w*|accept\w*|disput\w*|denied?\b|grant\w*|"
                r"approv\w*|upheld?\b|overturn\w*|settled?\b|paid\b)\b"
                r"|\b(?:reject\w*|accept\w*|disput\w*|denied?\b|grant\w*|"
                r"approv\w*|upheld?\b|overturn\w*|settled?\b|paid\b)\b"
                r"[^.]{0,30}\b(?:claim\w*|payout\w*|indemnit\w+|benefit\w*)\b"
                r"[^.]{0,30}\b(?:insurance|insured|policyholder\w*|"
                r"beneficiar\w*|insurer\w*|underwriter\w*|policy\b)\b",
                lower,
            ) and not re.search(
                r"\b(?:shall|must|will|require\w*|maintain\w*|procure\w*|"
                r"oblig\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out)\b",
                lower,
            ):
                continue
            # "Payment under the policy was refused by the insurer" recounts a
            # claims / payout outcome between the insured and the insurer (a
            # policy payment that was refused), not an obligation to hold or
            # procure cover.  The claims guard above requires claim / payout /
            # benefit nouns and reject / accept / deny verdicts; here the
            # payment-refusal frame uses "payment" + "policy" + a refusal
            # verb.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") have no payment+policy+
            # refusal frame (checked against fixtures/generate_fixtures.py,
            # which contains no insurance sentences) and still fire.
            if re.search(
                r"\bpayment\w*\b[^.]{0,45}\bpolicy\b[^.]{0,45}"
                r"\b(?:refus\w*|denied?\b|reject\w*)\b"
                r"|\bpolicy\b[^.]{0,45}\bpayment\w*\b[^.]{0,45}"
                r"\b(?:refus\w*|denied?\b|reject\w*)\b",
                lower,
            ):
                continue
            # "insurance as required by applicable law" re-states a statutory
            # duty without a negotiated coverage type — boilerplate, not a
            # coverage obligation.  Obligations naming a coverage type
            # (professional indemnity, public liability, ...) still fire.
            if re.search(
                r"\brequir\w*\s+by\s+(?:the\s+|applicable\s+|relevant\s+|"
                r"statutory\s+)?law\b",
                lower,
            ) and not re.search(
                r"\b(?:professional\s+indemnity|errors?\s+and\s+omissions|"
                r"public\s+liability|general\s+liability|property\s+damage|"
                r"bodily\s+injury|vehicle\w*|work(?:er|ers)?['\u2019s]?s?\s+"
                r"comp(?:ensation)?)\b",
                lower,
            ):
                continue
            # "insurance contributions to the pension fund / social security"
            # is a payroll or statutory-deduction mention, not a negotiated
            # coverage obligation.
            if re.search(r"\binsurance\s+contributions?\b", lower):
                continue
            # "medical insurance to employees as part of their benefits
            # package" is an employee-benefit perk, not an inter-party
            # coverage obligation.  Genuine coverage obligations naming a
            # policy type ("professional indemnity insurance") still fire.
            if re.search(
                r"\b(?:medical|health|dental|life|disability|travel|vision)\s+"
                r"insurance\b[^.]{0,50}"
                r"\b(?:benefits?\s+package|benefits?|perks?|employees?\s+as\s+part)\b"
                r"|\b(?:benefits?\s+package|benefits?)\b[^.]{0,45}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "the insurance certificate shall be issued within 5 business
            # days" is a documentary timeline for issuing a certificate /
            # cover note, not a negotiated coverage obligation.  A coverage
            # obligation that mentions the certificate in passing ("maintain
            # professional indemnity insurance and provide a certificate of
            # insurance upon request") still fires — the negative leg keeps
            # maintain/procure/carry/obtain obligations untouched.  The third
            # and fourth branches cover certificate / cover-note issuance
            # without the noun "insurance" ("A cover note shall be delivered
            # upon request of the insured.") — the verb-form gate admits the
            # sentence via "insured".
            if re.search(
                r"\binsurance\s+(?:certificate|certificates|cover\s+(?:note|notes)|"
                r"policy\s+documents?|policies?\s+documents?)\b"
                r"[^.]{0,60}\b(?:issu(?:e|ed|ing)|deliver\w*|supply\w*|furnish\w*|"
                r"transmit\w*|submit\w*)\b"
                r"|\b(?:issu(?:e|ed|ing)|deliver\w*|supply\w*|furnish\w*|"
                r"transmit\w*|submit\w*)\b[^.]{0,60}"
                r"\binsurance\s+(?:certificate|certificates|cover\s+(?:note|notes)|"
                r"policy\s+documents?|policies?\s+documents?)\b"
                r"|\b(?:certificate|certificates|cover\s+(?:note|notes))\b"
                r"[^.]{0,60}\b(?:issu(?:e|ed|ing)|deliver\w*|supply\w*|furnish\w*|"
                r"transmit\w*|submit\w*)\b[^.]{0,40}\binsured\b"
                r"|\b(?:issu(?:e|ed|ing)|deliver\w*|supply\w*|furnish\w*|"
                r"transmit\w*|submit\w*)\b[^.]{0,60}"
                r"\b(?:certificate|certificates|cover\s+(?:note|notes))\b"
                r"[^.]{0,40}\binsured\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "the insurance policy shall contain a deductible of USD 1,000 per
            # claim" describes policy mechanics (the amount the insured bears
            # per claim), not an inter-party coverage obligation.  Genuine
            # coverage obligations ("shall maintain professional indemnity
            # insurance") still fire.
            if re.search(
                r"\binsurance\w*\b[^.]{0,45}\bdeductible\w*\b"
                r"|\bdeductible\w*\b[^.]{0,45}\binsurance\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Insurance premium adjustments shall be set out in the tariff
            # bulletin" is administrative bookkeeping on premium levels — no
            # inter-party coverage obligation is created.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire.
            if re.search(
                r"\binsurance\s+premium\w*\b[^.]{0,45}"
                r"\b(?:adjustment\w*|tariff|bulletin|rate\w*|schedule\w*|"
                r"premium\s+levels?|assessment\w*|notice\b)\b"
                r"|\b(?:adjustment\w*|tariff|bulletin|rate\w*|schedule\w*|"
                r"premium\s+levels?|assessment\w*|notice\b)\b[^.]{0,45}"
                r"\binsurance\s+premium\w*\b"
                r"|\bpremium\w*\b[^.]{0,20}\b(?:adjustment\w*|tariff|bulletin|"
                r"rate\w*|schedule\w*|premium\s+levels?|assessment\w*)\b[^.]{0,30}"
                r"\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The Goods shall be supplied on CIF terms, being cost,
            # insurance and freight to the port of destination" is an
            # INCOTERMS shipping term and "the cost of transportation and
            # insurance shall be borne by the Buyer" is a cost-allocation
            # statement — neither creates a coverage obligation to maintain a
            # policy.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire.
            if re.search(
                r"\bcost,\s+insurance\s+and\s+freight\b"  # CIF Incoterm
                r"|\b(?:cost|expense|expenses|charges?)\s+of\s+(?:the\s+)?"
                r"(?:transport\w*|shipment|shipping|freight)\b[^.]{0,30}\binsurance\b"
                r"|\bcif\s+(?:terms?|incoterm\w*)\b",
                lower,
            ):
                continue
            # "The insurance broker shall be notified of any claim within 30
            # days of the loss" is claim-administration mechanics (loss
            # reporting to the insurer / broker), not a coverage obligation to
            # maintain a policy.  Genuine coverage obligations ("shall
            # maintain professional indemnity insurance") still fire — the
            # negative leg keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\binsurance\s+broker\b"
                r"|\bclaim\w*\b[^.]{0,55}"
                r"\b(?:notif\w*|report\w*|submit\w*|file\b|filed?)\b"
                r"[^.]{0,40}\binsurance\b"
                r"|\binsurance\b[^.]{0,40}"
                r"\b(?:notif\w*|report\w*|submit\w*|file\b|filed?)\b"
                r"[^.]{0,55}\bclaim\w*\b"
                r"|\b(?:notif\w*|report\w*|submit\w*|file\b|filed?)\b"
                r"[^.]{0,40}\binsurance\b[^.]{0,40}\bclaim\w*\b"
                r"|\bclaim\w*\b[^.]{0,40}\binsurance\b[^.]{0,40}"
                r"\b(?:notif\w*|report\w*|submit\w*|file\b|filed?)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The insurance requirements of the Employer are set out in the
            # Contract Data" / "The insurance requirements for this project
            # are detailed in the specification" is a pointer to where
            # requirements live, not an obligation on a party to maintain a
            # policy.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire — the negative
            # leg keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\binsurance\s+(?:requirements?|obligation\w*|arrangements?)\b"
                r"[^.]{0,40}\b(?:set\s+(?:out|forth|down)|described?\b|"
                r"detailed?\b|specified?\b|defined?\b|listed?\b|"
                r"outlined?\b)\b"
                r"|\b(?:set\s+(?:out|forth|down)|described?\b|detailed?\b|"
                r"specified?\b|defined?\b|listed?\b|outlined?\b)\b"
                r"[^.]{0,40}\binsurance\s+(?:requirements?|obligation\w*|"
                r"arrangements?)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "To the extent required by the Buyer, the Supplier shall take
            # out insurance for the Goods" conditions coverage on a third
            # party's request via "to the extent" — a conditional
            # might-event, not a primary coverage duty on the Supplier.  A
            # primary duty ("the Supplier shall take out / maintain
            # insurance") still fires — the negative leg requires "to the
            # extent".
            if re.search(
                r"\bto\s+the\s+extent\b[^.]{0,50}"
                r"\b(?:requir\w*|request\w*|demand\w*|instruct\w*|"
                r"direct\w*)\s+by\b[^.]{0,60}\binsurance\b",
                lower,
            ):
                continue
            # "The transfer of the claim shall be notified to the insurer" /
            # "Assignment of the claim to the insurer shall be in writing" is
            # a claims-assignment / subrogation transfer notice to the insurer
            # (who will pay the claim and how it is assigned), not an
            # inter-party obligation to maintain cover.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire.
            if re.search(
                r"\b(?:transfer\w*|assign\w*|assignment\w*)\b[^.]{0,40}"
                r"\b(?:claim\w*|loss\b|losses?\b|benefit\w*)\b[^.]{0,40}"
                r"\b(?:to|upon|against)\b[^.]{0,25}"
                r"\b(?:insurer\b|insurance\s+compan\w*|underwriter\w*)\b"
                r"|\binsurer\b[^.]{0,45}\b(?:claim\w*|loss\b|losses?\b)\b"
                r"[^.]{0,35}\b(?:transfer\w*|assign\w*)\b",
                lower,
            ):
                continue
            # "The Contractor shall provide evidence of insurance coverage
            # upon request" is a documentary / evidentiary step (proof of a
            # policy that exists), not an inter-party obligation to take out
            # coverage.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire — the negative leg
            # keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\b(?:evidence?|proof|certificates?)\s+of\s+insurance\b"
                r"|\binsurance\b[^.]{0,35}"
                r"\b(?:evidence?|proof|certificates?)\s+of\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Insurance of the Goods shall be at the risk of the Buyer
            # during transit" allocates who bears the insurance risk / cost —
            # a risk-allocation statement, not an inter-party obligation to
            # maintain a policy.  Genuine coverage obligations ("shall
            # maintain professional indemnity insurance") still fire — the
            # negative leg keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\binsurance\b[^.]{0,45}\bat\s+the\s+risk\b"
                r"|\bat\s+the\s+risk\b[^.]{0,45}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The Vendor shall cause its insurers to waive rights of
            # subrogation against the Customer" is a subrogation-waiver duty
            # (an instruction about how a policy already in place will be
            # handled on a claim) — it does not oblige either party to take
            # out or maintain cover.  Genuine coverage obligations ("shall
            # maintain professional indemnity insurance") still fire — the
            # negative leg keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\bsubrogation\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|cover\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The Customer shall provide insurance information with each
            # purchase order" is a paperwork / information-supply step (data
            # about coverage to be furnished with an order), not an
            # inter-party obligation to take out or maintain a policy.  The
            # guard is pinned to the information/particulars/details noun, so
            # genuine coverage obligations ("shall maintain professional
            # indemnity insurance") still fire — the negative leg keeps
            # maintain/procure/carry/obtain untouched.
            if re.search(
                r"\binsurance\s+(?:information|particulars|details|data)\b"
                r"|\b(?:information|particulars|details|data)\b[^.]{0,25}\binsurance\b"
                r"[^.]{0,20}\b(?:with\s+each|upon\s+request|to\s+the|provide\w*|"
                r"furnish\w*|supply\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|take\s+out)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Liability shall have the meaning given to it in the Insurance
            # Policies" is a defined-term pointer (a definitional cross-
            # reference to policy documents), not an obligation to take out or
            # maintain cover.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire — the negative leg
            # keeps maintain/procure/carry/obtain untouched.
            if re.search(
                r"\binsurance\s+polic(?:y|ies)\b[^.]{0,40}"
                r"\b(?:meaning|definition\w*|interpreted|defined)\b"
                r"|\b(?:meaning|definition\w*|interpreted|defined)\b[^.]{0,40}"
                r"\binsurance\s+polic(?:y|ies)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|take\s+out)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The parties confirm that insurance cover is available at
            # competitive rates" is a market-availability statement about
            # coverage, not an inter-party obligation to maintain a policy.
            # Genuine coverage obligations ("shall maintain professional
            # indemnity insurance") still fire — the negative leg keeps
            # maintain/procure/carry/obtain untouched.
            if re.search(
                r"\b(?:insurance\s+cover(?:age)?|cover(?:age)?\s+insurance)\b"
                r"[^.]{0,45}\b(?:available|competitive|affordable)\b"
                r"|\b(?:available|competitive|affordable)\b[^.]{0,45}"
                r"\b(?:insurance\s+cover(?:age)?|cover(?:age)?\s+insurance)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*|take\s+out)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The Insurer shall make payment of the claim within 30 days of
            # the loss assessment" is a claims-settlement commitment by the
            # insurer (administering payouts under a policy already in
            # force), not an inter-party obligation for a party to take out
            # or maintain cover.  Genuine coverage obligations ("shall
            # maintain professional indemnity insurance") still fire.
            if re.search(
                r"\b(?:insurer\b|insurance\s+company\b|underwriter\w*)\b"
                r"[^.]{0,55}\b(?:pay\w*|settle\w*|indemnif\w*|reimburse\w*)\b"
                r"|\b(?:pay\w*|settle\w*|indemnif\w*|reimburse\w*)\b"
                r"[^.]{0,55}\b(?:insurer\b|insurance\s+company\b|underwriter\w*)\b",
                lower,
            ) and re.search(
                r"\b(?:claim\w*|loss(?:es)?\b|loss\s+assessment\b|insured\b)\b",
                lower,
            ):
                continue
            # "Each party shall take out such insurance as its professional
            # advisers recommend" defers the coverage decision to professional
            # advisers' recommendations — an elastic "obtain whatever cover
            # your advisers say is appropriate" statement, not a concrete
            # coverage obligation.  Genuine coverage obligations naming a
            # policy type or fixed requirement still fire.
            if re.search(
                r"\bsuch\s+insurance\b[^.]{0,50}"
                r"\b(?:advis\w*|recommend\w*|deem\w*|consider\w*)\b"
                r"|\b(?:advis\w*|recommend\w*|deem\w*|consider\w*)\b[^.]{0,50}"
                r"\bsuch\s+insurance\b",
                lower,
            ):
                continue
            # "The Organiser shall require attendees to hold their own
            # insurance for the event" passes the cost/effort of coverage to
            # individuals (attendees, participants, users, guests) rather than
            # obliging a contracting party — not an inter-party coverage
            # obligation.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire.
            if re.search(
                r"\b(?:attendees?|participants?|guests?|users?|customers?|"
                r"members?)\b[^.]{0,45}"
                r"\b(?:own|personal|individual)\s+insurance\b"
                r"|\b(?:own|personal|individual)\s+insurance\b[^.]{0,45}"
                r"\b(?:attendees?|participants?|guests?|users?|customers?|"
                r"members?)\b",
                lower,
            ):
                continue
            # "The tenant shall arrange home insurance for the contents" /
            # "Each household is encouraged to hold contents insurance" /
            # "Flood insurance is recommended in high-risk zones" is consumer
            # cover for the speaker's own residence or possessions — not an
            # inter-party coverage obligation in a commercial contract.  A
            # homeowner genuinely required to maintain cover still fires — the
            # negative leg keeps maintain/procure/carry/obtain/take-out
            # untouched.
            if re.search(
                r"\b(?:home|contents|flood|household|dwellings?|possessions?)\s+"
                r"insurance\b"
                r"|\binsurance\s+for\s+(?:the\s+|your\s+)?"
                r"(?:home\b|contents|flood\b|household\b|dwellings?|possessions?)\b"
                r"|\b(?:home|contents|flood|household|dwellings?|possessions?)\b"
                r"[^.]{0,35}\binsurance\b",
                lower,
            ) and re.search(
                r"\b(?:arrang\w*|encourag\w*|recommend\w*|hold\b|held\b|advis\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The insured value of the vehicle is USD 20,000" / "The named
            # insured is the policyholder listed in the schedule" is a policy
            # declaration (declared value / who is covered), not an
            # inter-party obligation to take out or maintain cover.
            if re.search(
                r"\binsured\s+value\b"
                r"|\bnamed\s+insured\b[^.]{0,40}\bpolicyholder\b",
                lower,
            ):
                continue
            # "The bank offers insurance products to its customers" is a
            # product-marketing statement by a seller of insurance, not an
            # inter-party obligation on a contracting party to maintain cover.
            if re.search(
                r"\b(?:offer\w*|sell\w*|sold\b|provid\w*|market\w*)\b[^.]{0,45}"
                r"\binsurance\s+products?\b"
                r"|\binsurance\s+products?\b[^.]{0,45}"
                r"\b(?:offer\w*|sell\w*|sold\b|provid\w*|market\w*)\b",
                lower,
            ):
                continue
            # "Insurance premiums for health plans rose 8% this year" is a
            # market / industry premium-level observation, not a coverage
            # obligation.  The premium-adjustment guard above covers
            # administrative bookkeeping; this one covers movement statements
            # about premium levels only.
            if re.search(
                r"\binsurance\s+premium\w*\b[^.]{0,45}"
                r"\b(?:rose\b|increase\w*|fell\b|decline\w*|up\s+\d+|by\s+\d+%?|"
                r"higher\b|lower\b)\b",
                lower,
            ):
                continue
            # "This policy covers personal accident insurance for members" is
            # a product-cover description for individuals who hold a
            # membership, not an inter-party obligation for a contracting
            # party to maintain cover.
            if re.search(
                r"\baccident\s+insurance\b[^.]{0,45}\b(?:member\w*|participant\w*)\b"
                r"|\b(?:member\w*|participant\w*)\b[^.]{0,45}"
                r"\baccident\s+insurance\b",
                lower,
            ):
                continue
            # "The policy pays a penalty for late renewal of the insurance"
            # is an insurance-continuation surcharge for not renewing a policy
            # on time, not a coverage obligation (the penalty-rule guard
            # already stops the penalty half).
            if re.search(
                r"\bpenalt\w*\b[^.]{0,45}\blate\s+renew\w*\b[^.]{0,30}\binsurance\b",
                lower,
            ):
                continue
            # "Cancellation insurance is offered to all travellers at check-out"
            # is a consumer add-on sold at booking (travel-industry upsell),
            # not an inter-party obligation for a contracting party to maintain
            # cover.  "The event was insured against adverse weather conditions"
            # is a public-event / film-production risk arrangement, not a
            # commercial contract coverage obligation.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire.  Zero fixture lines use these phrases (checked
            # against fixtures/generate_fixtures.py).
            if re.search(
                r"\bcancellation\s+insurance\b[^.]{0,50}"
                r"\b(?:travell?ers?|passengers?|customers?|guests?|booking\w*|"
                r"check[- ]out|flight\w*|hotel\w*|holiday\w*|vacation\w*|trip\w*)\b"
                r"|\b(?:travell?ers?|passengers?|customers?|guests?|booking\w*|"
                r"check[- ]out|flight\w*|hotel\w*|holiday\w*|vacation\w*|trip\w*)\b"
                r"[^.]{0,50}\bcancellation\s+insurance\b"
                r"|\binsured?\b[^.]{0,45}\bagainst\b[^.]{0,35}"
                r"\b(?:adverse\s+weather|rain\w*|storm\w*|flood\w*|weather\s+"
                r"conditions?)\b"
                r"|\badverse\s+weather\b[^.]{0,45}\binsured?\b",
                lower,
            ):
                continue
            # "Health insurance is mandatory for all employees" is a statutory
            # / HR benefits statement (the law or employer policy makes cover
            # compulsory for staff), not an inter-party obligation between the
            # parties on the contract's own risks.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire — the guard requires the mandatory/compulsory/required
            # frame bound to a benefit-type insurance for employees/staff.
            if re.search(
                r"\b(?:medical|health|dental|life|disability|pension)\s+"
                r"insurance\b[^.]{0,40}"
                r"\b(?:mandatory|compulsory|required?|obligatory|"
                r"required\s+by\s+law)\b"
                r"|\b(?:mandatory|compulsory|required?|obligatory|"
                r"required\s+by\s+law)\b[^.]{0,40}"
                r"\b(?:medical|health|dental|life|disability|pension)\s+"
                r"insurance\b",
                lower,
            ):
                continue
            # "The statutory insurance requirements are set out in the
            # applicable law" is a statutory-compliance pointer (the legal
            # regime determines the cover), not a negotiated coverage
            # obligation.  Genuine coverage obligations ("shall maintain
            # professional indemnity insurance") still fire — the guard
            # requires the statutory/regulatory adjective bound to insurance
            # requirements/coverage.
            if re.search(
                r"\b(?:statutory|regulatory|legal)\s+insurance\b[^.]{0,40}"
                r"\b(?:requirement\w*|obligation\w*|coverage?|cover\b)\b"
                r"|\b(?:requirement\w*|obligation\w*)\b[^.]{0,40}"
                r"\b(?:statutory|regulatory|legal)\s+insurance\b",
                lower,
            ):
                continue
            # "The car is insured for comprehensive coverage" is a personal /
            # asset-holder cover statement (who carries the policy on their own
            # asset), not a contractual arrangement binding the parties.
            # Genuine coverage obligations ("shall maintain professional
            # indemnity insurance") still fire — the guard requires the
            # comprehensive/coverage frame for a vehicle/personal asset.
            if re.search(
                r"\binsured?\b[^.]{0,40}\bcomprehensive\b[^.]{0,40}"
                r"\b(?:coverage?|cover\b)\b"
                r"|\bcomprehensive\b[^.]{0,40}\b(?:coverage?|cover\b)\b"
                r"[^.]{0,40}\binsured?\b",
                lower,
            ):
                continue
            # "The band insured their tour equipment against damage" is a
            # non-contractual asset-protection act (one party carrying cover on
            # its own property), not an inter-party obligation.  The guard
            # requires the insured/equipment/against frame without any of the
            # obligational verbs (shall maintain / shall procure / cover).
            if re.search(
                r"\binsured?\b[^.]{0,50}\b(?:equipment\w*|instruments?\b|"
                r"inventory\b|stock\b|property\b)\b[^.]{0,35}\bagainst\b"
                r"|\bagainst\b[^.]{0,35}\b(?:equipment\w*|instruments?\b|"
                r"inventory\b|stock\b|property\b)\b[^.]{0,50}\binsured?\b",
                lower,
            ):
                continue
            # "Travel insurance is included in the ticket price" is an
            # ancillary consumer product (a benefit bundled into a ticket /
            # price), not an insurance-clause obligation between contracting
            # parties.  Genuine coverage obligations still fire — the guard
            # requires the consumer-product/price binding frame.
            if re.search(
                r"\btravel\s+insurance\b[^.]{0,40}"
                r"\b(?:included?|offer\w*|availab\w+)\b"
                r"[^.]{0,40}\b(?:ticket\w*|price\b|fare\w*|package\w*)\b",
                lower,
            ):
                continue
            # "Cargo insurance is included in the freight cost" / "Shipping
            # insurance is included in the quoted price" are cost allocations
            # (which party's price already absorbs the premium), not an
            # insurance-clause obligation.  Genuine coverage obligations
            # ("shall maintain ... insurance") still fire — the guard requires
            # the included-in-cost binding frame.
            if re.search(
                r"\b(?:cargo|freight|shipping|marine|goods?)\s+insurance\b"
                r"[^.]{0,40}\b(?:included?|cover(?:ed)?\s+by)\b"
                r"[^.]{0,40}\b(?:cost\w*|price\b|quote\w*|packing\w*)\b",
                lower,
            ):
                continue
            # "The insured event must be reported within 30 days" is a
            # claims-reporting instruction (when to notify the insurer), not a
            # coverage obligation binding the parties.  The broker/claim frame
            # guard above requires "claim/broker" tokens; this catches the
            # "insured event" phrasing.
            if re.search(r"\binsured\s+event\b[^.]{0,40}\breport(?:ed|ing)?\b", lower):
                continue
            # "The policy covers the insured up to EUR 1,000,000" states a
            # policy's sum-insured limit, not an inter-party obligation to
            # hold cover.  The guard requires the "insured ... up to ...
            # currency" sum frame; a genuine coverage obligation ("This policy
            # covers personal accident insurance for members") has no "insured
            # ... up to ... <currency>" frame and still fires.
            if re.search(
                r"\binsured?\b[^.]{0,45}\bup\s+to\b[^.]{0,20}"
                r"\b(?:usd|eur|gbp|jpy|cny|myr|sgd)\b",
                lower,
            ):
                continue
            # "The company insured its directors against liability claims" and
            # "The bank insured the loan against the default risk" are
            # non-contractual cover arrangements taken by one party on its own
            # personnel / credit exposure, not an inter-party insurance
            # obligation.  The guard requires the insured+person/loan+against
            # frame.
            if re.search(
                r"\binsured?\b[^.]{0,40}"
                r"\b(?:directors?|officers?|employees?|personnel\b|staff\b|"
                r"loan\w*|mortgage\w*|credit\w*|receivable\w*)\b"
                r"[^.]{0,35}\bagainst\b",
                lower,
            ):
                continue
            # "The company insured the building for USD 5 million" is an
            # asset-holder cover with a sum insured, not a contractual
            # obligation to hold cover for the counterparty.  Genuine coverage
            # obligations ("shall maintain cover of not less than USD 5M")
            # still fire — the guard requires the plain insured+asset+sum
            # frame without shall-maintain verbs.
            if re.search(
                r"\binsured?\b[^.]{0,45}\b(?:building\w*|premises?\b|"
                r"plant\b|warehouse\w*|facilit\w+)\b[^.]{0,45}\bfor\b"
                r"[^.]{0,30}\b(?:usd|eur|gbp|jpy|cny|myr|sgd)\b",
                lower,
            ):
                continue
            # "The insurance premium is included in the annual fee" is a cost
            # allocation (which bundled price already absorbs the premium),
            # not a coverage obligation imposed on a party.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire — the guard requires the premium + included +
            # periodic-fee frame.  "Insurance is payable by the tenant under
            # the lease" allocates who pays the premium and still fires (no
            # premium + included + periodic-fee frame).  Zero fixture lines
            # pair insurance premium with an included periodic fee (checked
            # against fixtures/generate_fixtures.py).
            if re.search(
                r"\binsurance\s+premium\b[^.]{0,40}\binclud\w*\b[^.]{0,35}"
                r"\b(?:annual|monthly|quarterly|yearly|periodic)\s+fee\b"
                r"|\b(?:annual|monthly|quarterly|yearly|periodic)\s+fee\b"
                r"[^.]{0,35}\binclud\w*\b[^.]{0,40}\binsurance\s+premium\b",
                lower,
            ):
                continue
            # "Insurance coverage commences upon payment of the first premium"
            # states when a policy takes effect (an administrative effective-
            # date rule), not an inter-party obligation to maintain cover.
            # Genuine coverage obligations ("shall maintain professional
            # indemnity insurance") still fire — the guard requires the
            # coverage/cover + commence + premium frame.
            if re.search(
                r"\b(?:coverage|cover)\b[^.]{0,45}\bcommenc\w*\b[^.]{0,35}"
                r"\bpremium\b"
                r"|\bpremium\b[^.]{0,35}\bcommenc\w*\b[^.]{0,45}"
                r"\b(?:coverage|cover)\b",
                lower,
            ):
                continue
            # "Insurance coverage ends when the goods are delivered" states
            # when cover lapses (an administrative duration rule), not an
            # inter-party obligation to maintain cover.  Genuine coverage
            # obligations ("shall maintain professional indemnity insurance")
            # still fire — the negative leg keeps maintain/procure/carry/
            # obtain untouched.
            if re.search(
                r"\b(?:coverage|cover)\b[^.]{0,45}\b(?:end\w*|ceas\w*|terminat\w*|"
                r"laps\w*)\b[^.]{0,35}\b(?:when|upon|once)\b[^.]{0,30}\bdeliver\w*\b"
                r"|\bdeliver\w*\b[^.]{0,30}\b(?:when|upon|once)\b[^.]{0,35}"
                r"\b(?:end\w*|ceas\w*|terminat\w*|laps\w*)\b[^.]{0,45}"
                r"\b(?:coverage|cover)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Liability under the Policy is limited to the sum insured" is a
            # policy-terms statement of the policy's own liability limit (a
            # sum-insured frame), not an inter-party obligation to hold cover.
            # Genuine coverage obligations ("shall maintain liability
            # insurance limited to ...") still fire — the negative leg keeps
            # maintain/procure/carry/obtain untouched.  Zero fixture lines
            # combine liability + sum insured (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\bliab\w*\b[^.]{0,50}\b(?:limited|cap\w*)\s+to\b"
                r"[^.]{0,40}\b(?:the\s+)?sum\s+insured\b"
                r"|\bsum\s+insured\b[^.]{0,40}\b(?:limited|cap\w*)\s+to\b"
                r"[^.]{0,50}\bliab\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Insurance premiums are reviewed annually by the insurer" is an
            # administrative review of premium levels, not a coverage
            # obligation.  Premium-adjustment guards above cover the tariff /
            # rate / schedule frames; this covers the review/assessment frame.
            if re.search(
                r"\binsurance\s+premium\w*\b[^.]{0,40}\b(?:review\w*|assess\w*|"
                r"reassess\w*|revis\w*|calculat\w*)\b[^.]{0,30}(?:\bannually\b|"
                r"\bperiodic\w*\b|\bby\s+the\s+insurer\b)"
                r"|\b(?:review\w*|assess\w*|reassess\w*|revis\w*|calculat\w*)\b"
                r"[^.]{0,30}(?:\bannually\b|\bperiodic\w*\b|\bby\s+the\s+"
                r"insurer\b)[^.]{0,40}\binsurance\s+premium\w*\b"
                r"|\b(?:review\w*|assess\w*|reassess\w*|revis\w*|calculat\w*)\b"
                r"[^.]{0,30}\b(?:annually|periodically)\b[^.]{0,40}"
                r"\binsurance\s+premium\w*\b"
                r"|\binsurance\s+premium\w*\b[^.]{0,25}"
                r"\b(?:annually|periodically)\b",
                lower,
            ):
                continue
            # "The insured must notify the insurer of any claim promptly" is a
            # policy-administration notice rule (claims notification), not a
            # coverage obligation.  The broker/claim-notification guard above
            # needs the bare "insurance" noun; this one covers the
            # insured-notifies-insurer frame.
            if re.search(
                r"\binsured\b[^.]{0,35}\b(?:notif\w*|report\w*|inform\w*)\b"
                r"[^.]{0,30}\b(?:insurer|insurance\s+company|underwriter)\b"
                r"[^.]{0,30}\bclaim\w*\b"
                r"|\bclaim\w*\b[^.]{0,30}\b(?:insurer|insurance\s+company|"
                r"underwriter)\b[^.]{0,30}\b(?:notif\w*|report\w*|inform\w*)\b"
                r"[^.]{0,35}\binsured\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The policy holder must notify the insurer of any change of
            # address" is policy administration (updating personal details),
            # not a coverage obligation.  Fixtures checked: no fixture line
            # carries a policy-holder change-of-address notification frame.
            if re.search(
                r"\b(?:policy\s?holder|policyholder|insured)\b[^.]{0,35}"
                r"\b(?:notif\w*|inform\w*|report\w*|advise\b)\b[^.]{0,30}"
                r"\b(?:address|personal\s+details|circumstances|"
                r"change\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The insured shall submit a claim form within 30 days" is
            # claims-procedure narration (filing paperwork), not a coverage
            # obligation.  Fixtures checked: no fixture line uses a "claim
            # form" submission frame.
            if re.search(
                r"\b(?:insured\b|policy\s?holder|policyholder)\b[^.]{0,35}"
                r"\b(?:submit\w*|file\w*|deliver\b|furnish\b)\b[^.]{0,25}"
                r"\bclaim\s+form\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Insurance shall be effected by the party bearing the risk" is a
            # general risk-allocation statement identifying who arranges cover,
            # not a coverage obligation naming a policy type.  Genuine
            # "shall maintain [type] insurance" obligations still fire.
            if re.search(
                r"\binsurance\b[^.]{0,50}\beffect\w*\b[^.]{0,40}\brisk\b"
                r"|\brisk\b[^.]{0,40}\beffect\w*\b[^.]{0,50}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Health insurance is deducted from the salary each month" is a
            # payroll deduction, not a coverage obligation.  The
            # employee-benefit guard above requires the benefit frame; this
            # covers the deducted-from-pay frame.
            if re.search(
                r"\b(?:medical|health|dental|life|disability|pension|vision|"
                r"travel)\s+insurance\b[^.]{0,35}\bdeduct\w*\b[^.]{0,30}"
                r"\b(?:salary|wages|pay\b|payroll)\b"
                r"|\b(?:salary|wages|pay\b|payroll)\b[^.]{0,30}\bdeduct\w*\b"
                r"[^.]{0,35}\b(?:medical|health|dental|life|disability|pension|"
                r"vision|travel)\s+insurance\b"
                r"|\bdeduct\w*\b[^.]{0,40}"
                r"\b(?:medical|health|dental|life|disability|pension|vision|"
                r"travel)\s+insurance\b[^.]{0,30}\b(?:salary|wages|pay\b|payroll)\b",
                lower,
            ):
                continue
            # "The vessel must carry hull and machinery insurance" describes
            # what cover a ship is required to carry (marine term of art), not
            # an inter-party coverage obligation between commercial parties.
            # Bare "hull and machinery" insurance is unambiguous marine cover.
            if re.search(
                r"\b(?:vessel|ship\w*|boat\w*|marine\w*|carrier)\b[^.]{0,50}"
                r"\bhull\s+and\s+machinery\b"
                r"|\bhull\s+and\s+machinery\b",
                lower,
            ):
                continue
            # "The cafeteria plan administrator sends insurance summaries" /
            # "insurance enrollments are visible in the benefits portal" /
            # "the plan administrator lists insurance options for new hires"
            # are benefits-administration facts (an HR administrator
            # describing what the enrolment system does), not an inter-party
            # obligation to hold or procure coverage.  A genuine coverage
            # obligation naming a policy type and a maintaining verb still
            # fires — the negative leg keeps maintain / procure / carry /
            # obtain untouched.  Zero fixture lines combine the benefits
            # administration frame with insurance (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\binsurance\b[^.]{0,45}"
                r"\b(?:summar\w*|enroll(?:ed|s|ing|ment)?\w*|enrol(?:m|ed|s|ing)?\w*|"
                r"option\w*|portal\w*|administrator\w*)\b"
                r"|\b(?:summar\w*|enroll(?:ed|s|ing|ment)?\w*|enrol(?:m|ed|s|ing)?\w*|"
                r"option\w*|portal\w*|administrator\w*)\b[^.]{0,45}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "The schedule of coverages shows the sum insured per item" /
            # "the schedule lists the sums insured under the plan" state
            # declarative schedule contents (a coverage schedule or benefits
            # schedule listing sums insured), not an inter-party coverage
            # obligation.  A genuine "shall maintain ... insurance" duty tied
            # to a schedule ("the schedule shall show the required sum
            # insured") still fires — the negative leg keeps maintain /
            # procure / carry / obtain untouched.  Zero fixture lines combine
            # schedule with sum insured (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\bschedule\w*\b[^.]{0,45}"
                r"\b(?:sums?\s+insured|sum\s+assured)\b"
                r"|\b(?:sums?\s+insured|sum\s+assured)\b[^.]{0,45}"
                r"\bschedule\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:maintain\w*|procure\w*|carry|obtain\w*)\b[^.]{0,40}"
                r"\binsurance\b",
                lower,
            ):
                continue
            # "Insurance companies are regulated by the FCA" / "Insurance stocks
            # outperformed the index" / "Firms filed quarterly returns with the
            # insurance regulator" / "The sector employs over 100,000 people in
            # insurance services" are industry / regulatory observations about
            # the insurance business, not a coverage obligation on a party.
            # The reverse leg covers keyword-after-insurance word order
            # ("The sector employs over 100,000 people in insurance services").
            # Obligation verbs are excluded so a genuine duty naming a
            # regulator ("shall obtain insurance from an FCA-regulated
            # insurer") stays live.  Zero fixture lines pair insurance with
            # regulated / stocks / sector / returns (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\binsurance\b[^.]{0,45}"
                r"\b(?:regulat\w+|stocks?\b|sector\b|index\b|"
                r"outperform\w*|(?:quarterly|annual)\w*\s+return\w*\b)\b"
                r"|\b(?:regulat\w+|sector\b|stocks?\b|outperform\w*|index\b)\b"
                r"[^.]{0,45}\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "Title insurance protects buyers against title defects" /
            # "Insurance protects your family in times of need" / "Litigation
            # insurance covers legal costs" / "Term insurance provides a payout
            # to your beneficiaries" describe a product's benefit to a
            # beneficiary audience, not an obligation to hold or procure
            # coverage.  The benefit-object list (buyers / family / legal
            # costs / beneficiaries / ...) keeps it from swallowing generic
            # coverage language, and obligation verbs are excluded so genuine
            # duties still fire.  Zero fixture lines pair insurance with these
            # benefit objects (checked against fixtures/generate_fixtures.py).
            if re.search(
                r"\binsurance\b[^.]{0,35}"
                r"\b(?:protect\w+|provid\w+|offer\w*|cover\w*)\b[^.]{0,30}"
                r"\b(?:buyers?|famil\w*|beneficiar\w*|legal\s+cost\w*|"
                r"loved\s+ones\b|defect\w*|policyholder\w*|insured\b|"
                r"claimant\w*|estate\b)\b"
                r"|\b(?:buyers?|famil\w*|beneficiar\w*|legal\s+cost\w*|"
                r"loved\s+ones\b|defect\w*|policyholder\w*|insured\b|"
                r"claimant\w*|estate\b)\b[^.]{0,30}"
                r"\b(?:protect\w+|provid\w+|offer\w*|cover\w*)\b[^.]{0,30}"
                r"\binsurance\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The policy document states the sum insured for each item" /
            # "The sum insured under the policy is EUR 500,000" are descriptive
            # references to an insured value in a certificate or policy
            # schedule, not an inter-party obligation to procure coverage of
            # that amount.  Obligation verbs are excluded so genuine duties
            # ("shall obtain insurance with a sum insured of at least
            # USD 1,000,000") still fire.  Zero fixture lines reference a bare
            # sum insured / sum assured (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:sums?\s+insured|sum\s+assured)\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The insurer will review her claim next week" narrates the next
            # step in a claims-handling episode, not an obligation to hold or
            # procure coverage.  "will" is deliberately absent from the
            # negative leg (unlike the other guards) so this forward-looking
            # narration is caught, while genuine obligation verbs (shall /
            # require / maintain / ...) still keep a claims-handling duty live.
            # Round 51 already suppresses "the claims department will process
            # the application" and "the adjuster will assess the damage";
            # this guard extends the family to the insurer-as-processor
            # phrasing.  Zero fixture lines use insurer + review/process/assess
            # (checked against fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:insurer\w*|claims?\s+department|adjuster\w*|"
                r"underwriter\w*)\b[^.]{0,30}\bwill\b[^.]{0,25}"
                r"\b(?:review\w*|process\w*|assess\w*|evaluat\w*|"
                r"examin\w*|validat\w*|verif\w*)\b[^.]{0,25}\bclaim\w*\b"
                r"|\bclaim\w*\b[^.]{0,25}"
                r"\b(?:review\w*|process\w*|assess\w*|evaluat\w*|"
                r"examin\w*|validat\w*|verif\w*)\b[^.]{0,25}\bwill\b[^.]{0,30}"
                r"\b(?:insurer\w*|claims?\s+department|adjuster\w*|"
                r"underwriter\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The insurance company paid his medical bills promptly" narrates
            # a past claims payout by an insurer — an episode description, not
            # an inter-party obligation to hold or procure coverage.  The
            # pay/settle/reimburse verb and a cost noun (bills / medical /
            # expenses / costs / damages) pin the claims-payout frame; obligation
            # verbs are excluded so genuine duties ("the insurer shall pay valid
            # claims within 30 days") stay live.  Zero fixture lines pair an
            # insurer with a payout of bills (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|insurer\w*|"
                r"carrier\w*)\b[^.]{0,30}"
                r"\b(?:paid?|paid\s+out|reimburs\w*|settled?|refund\w*)\b"
                r"[^.]{0,35}\b(?:bills?\b|medical\b|hospital\b|treatment\w*|"
                r"expenses?\b|cost\w*|damages?\b)\b"
                r"|\b(?:bills?\b|medical\b|hospital\b|treatment\w*|expenses?\b|"
                r"cost\w*|damages?\b)\b[^.]{0,30}"
                r"\b(?:paid?|paid\s+out|reimburs\w*|settled?|refund\w*)\b"
                r"[^.]{0,30}\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The insurance company indemnified the policyholder after the
            # fire" narrates a past compensation event by an insurer (an
            # episode description), not an inter-party coverage obligation.
            # The guard requires an insurer subject bound to the past-tense
            # indemnify verb; obligation auxiliaries are excluded so a genuine
            # duty ("the insurer shall indemnify the policyholder") stays
            # live.  Zero fixture lines pair an insurer with the indemnified
            # verb (checked against fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b[^.]{0,40}\bindemnif\w*ed\b"
                r"|\bindemnif\w*ed\b[^.]{0,40}"
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The insurance company refunded the premium after cancellation"
            # narrates a cancelled-policy premium repayment (an episode), not
            # an inter-party coverage obligation.  The guard requires an
            # insurer subject, the refund verb and a premium noun bound within
            # the clause; obligation verbs are excluded so genuine duties
            # ("the insurer shall refund the premium") stay live.  Premium is
            # tested in several existing guards but never paired with a
            # refund verb by an insurer subject there (checked against
            # tests/test_rules.py and fixtures/generate_fixtures.py).
            if re.search(
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b[^.]{0,40}\brefund\w*\b[^.]{0,30}"
                r"\bpremium\w*\b"
                r"|\bpremium\w*\b[^.]{0,30}\brefund\w*\b[^.]{0,40}"
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "Any indemnified party shall cooperate with the insurer in the
            # defence" is a policy-compromise cooperation covenant (an
            # indemnified party must assist the insurer), not an inter-party
            # coverage obligation.  The guard requires the past-tense
            # indemnify verb AND a cooperate-to-insurer binding; the negative
            # leg omits shall/will/must/may because the cooperation frame is
            # typically phrased with "shall cooperate", while coverage-duty
            # verbs (maintain, procure, take out, obtain, purchase) still
            # cancel the guard.  Zero fixture lines pair indemnified with
            # cooperate (checked against fixtures/generate_fixtures.py).
            if re.search(r"\bindemnif\w*ed\b", lower) and re.search(
                r"\bcooperat\w*\b[^.]{0,45}"
                r"\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b"
                r"|\b(?:insurance\s+(?:company|provider|carrier|firm)|"
                r"insurer\w*|carrier\w*)\b[^.]{0,45}\bcooperat\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|require\w*|maintain\w*|procure\w*|"
                r"carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # "The travel agency recommends insurance for every booking" is a
            # sales / advisory recommendation (a vendor urging customers to buy
            # cover), not an inter-party coverage obligation.  The recommend
            # verb on either side of "insurance" pins the frame; obligation
            # verbs are excluded so genuine duties ("the Supplier shall
            # maintain insurance") stay live.  Zero fixture lines recommend
            # insurance (checked against fixtures/generate_fixtures.py).
            if re.search(
                r"\brecommend\w*\b[^.]{0,40}\binsurance\b"
                r"|\binsurance\b[^.]{0,40}\brecommend\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # Self-insurance availability ("Self-insurance is available to
            # large employers") describes an option open to a class of buyers,
            # not an inter-party coverage obligation (a party must fund risks
            # itself rather than buy cover).
            if re.search(r"\bself[\s-]?insur\w*\b", lower):
                continue
            # Valuation / appraisal narration ("The insurer valued the factory
            # at USD 2 million for replacement") reports a value opinion, not a
            # coverage obligation.
            if re.search(
                r"\binsur\w*\b[^.]{0,40}\b(?:valu\w*|apprais\w*|assess\w*)\b"
                r"|\b(?:valu\w*|apprais\w*|assess\w*)\b[^.]{0,40}\binsur\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # Membership / sports cover ("The club requires all players to be
            # insured against injury") is eligibility narration for
            # individuals, not an inter-party coverage duty.
            if re.search(
                r"\binsur\w*\b[^.]{0,45}\b(?:players?\b|athletes?\b|teams?\b|"
                r"members?\b)\b"
                r"|\b(?:players?\b|athletes?\b|teams?\b|members?\b)\b"
                r"[^.]{0,45}\binsur\w*\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # Premium-collection narration ("Insurance premiums for the vehicle
            # are collected with the registration fee", "contributions are
            # deducted on a monthly basis") describes how cover is paid for,
            # not a duty to procure coverage.
            if re.search(
                r"\binsurance\s+premiums?\b[^.]{0,45}\b(?:collect\w*|deduct\w*)\b"
                r"|\b(?:collect\w*|deduct\w*)\b[^.]{0,45}\binsurance\s+premiums?\b",
                lower,
            ) and not re.search(
                r"\b(?:oblig\w*|shall|must|will|require\w*|maintain\w*|"
                r"procure\w*|carr(?:y|ies|ied)|tak(?:e|ing|en)\s+out|"
                r"obtain\w*|purchase\w*)\b",
                lower,
            ):
                continue
            # Certificate-presentation rules ("The library's insurance
            # certificate must be presented at the venue") require showing a
            # document at a site, not maintaining cover.  Zero fixture lines
            # mention insurance certificates (checked against
            # fixtures/generate_fixtures.py).
            if re.search(
                r"\binsurance\s+certificate\b[^.]{0,45}"
                r"\b(?:present\w*|produce\w*)\b",
                lower,
            ):
                continue
            flag = "insurance requirement"
            risk = RiskLevel.LOW
            if "professional indemnity" in lower or "errors and omissions" in lower:
                flag += " professional liability"
                risk = RiskLevel.MEDIUM
            out.append(Match("insurance", page, sentence, risk, flag))
    return out


RULES = {
    "duration": rule_duration,
    "quantity": rule_quantity,
    "price": rule_price,
    "delivery": rule_delivery,
    "acceptance": rule_acceptance,
    "payment": rule_payment,
    "penalty": rule_penalty,
    "liability_cap": rule_liability,
    "confidentiality": rule_confidentiality,
    "ip": rule_ip,
    "renewal": rule_renewal,
    "termination": rule_termination,
    "exclusivity": rule_exclusivity,
    "data_protection": rule_data_protection,
    "insurance": rule_insurance,
}
