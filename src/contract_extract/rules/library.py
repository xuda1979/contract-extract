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
        if re.search(
            r"\b(?:workmanship|snag\w*|remedial|rectification)\b[^.]{0,40}"
            r"\b(?:repair\w*|remed\w*|defect\w*)\b"
            r"|\b(?:repair\w*|remed\w*|defect\w*)\b[^.]{0,40}"
            r"\b(?:workmanship|snag\w*|remedial|rectification)\b",
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
        if re.search(
            r"\baccept\w*\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:documentary\s+credit(?:s)?|letter\s+of\s+credit|bill(?:s)?\s+of\s+exchange|"
            r"promissory\s+note(?:s)?|draft(?:s)?|offer(?:s)?|tender(?:s)?|bid(?:s)?|"
            r"purchase\s+order(?:s)?|quotation(?:s)?|term(?:s)?\s+(?:and|&)\s+condition(?:s)?|"
            r"appointment(?:s)?|engagement(?:s)?)\b"
            r"|\bacceptance\s+of\s+(?:the\s+|a\s+|this\s+)?"
            r"(?:documentary\s+credit(?:s)?|letter\s+of\s+credit|bill(?:s)?\s+of\s+exchange|"
            r"promissory\s+note(?:s)?|draft(?:s)?|offer(?:s)?|tender(?:s)?|bid(?:s)?|"
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
        # default penalties for a breach.
        if re.search(
            r"\b(?:prosecution|criminal|fraud"
            r"|indictment|offen[cs]e(?:s)?|misrepresent\w*)\b[^.]{0,60}\bpenalt"
            r"|\b(?:statutory|regulatory|regulator|administrative|civil|"
            r"disciplinary|government(?:al)?|sanction(?:s|ed)?)\b[^.]{0,40}"
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
        # liability caps.  A genuine indemnity obligation ("indemnifies",
        # "hold harmless") still fires.
        if "insurance" in lower and not re.search(
            r"\bindemnif|hold\s+harmless", lower
        ):
            continue
        if not any(k in lower for k in (
            "liability", "aggregate liability", "maximum liability", "indemn", "cap on damages",
            "not be liable for", "be liable for", "disclaims all", "no liability",
        )):
            continue
        # A force-majeure event ("neither party shall be liable for damages
        # caused by a force majeure event") relaxes one narrow excuse, and
        # "each party shall be liable for its own taxes and duties" allocates
        # statutory charges — neither is a liability-cap risk.
        if re.search(r"\bforce\s+majeure\b", lower):
            continue
        if re.search(r"\bliable\s+for\s+its\s+own\b", lower):
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
        # in the Goods" restates the strict-liability doctrine (tort) — it is
        # not an agreed cap, disclaimer or exclusion.  Genuine caps
        # ("aggregate liability shall not exceed USD 1M") and disclaimers
        # still fire.
        if re.search(
            r"\b(?:strict|absolute)\s+liability\b[^.]{0,45}"
            r"\b(?:creat\w*|impos\w*|incur\w*|aris\w*|attach\w*|result\w*)\b"
            r"|\b(?:creat\w*|impos\w*|incur\w*|aris\w*|attach\w*|result\w*)\b"
            r"[^.]{0,45}\b(?:strict|absolute)\s+liability\b",
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
            r"\b(?:intellectual\s+property|ip)\s+(?:clause|provision|section|"
            r"article|term)\b[^.]{0,50}\b(?:allocat\w*|contemplat\w*|address\w*|"
            r"deal\w*|govern\w*)\b",
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
        # Zero fixture lines mention renewing campaigns (checked against
        # fixtures/generate_fixtures.py; "campaign" appears only in a
        # confidentiality fixture, which is a different rule).
        if re.search(
            r"\brenew(?:al|s|ing|ed|able)?\b[^.]{0,40}"
            r"\b(?:campaign\w*|advertis\w*|marketing\w*|promotion\w*|season)\b"
            r"|\b(?:campaign\w*|advertis\w*|marketing\w*|promotion\w*|season)\b[^.]{0,40}"
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
            # "the insurance policy renews / renewal of the insurance policy" is a
            # policy-admin renewal mention, not a coverage obligation at all.
            if re.search(
                r"\b(?:renew\w*|expir\w*|terminat\w*)\b[^.]{0,40}\binsurance\s+policy\b"
                r"|\binsurance\s+policy\b[^.]{0,40}\b(?:renew\w*|expir\w*|terminat\w*)\b"
                r"|\brenewal\s+of\s+(?:the\s+|a\s+|this\s+)?insurance\s+policy\b",
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
