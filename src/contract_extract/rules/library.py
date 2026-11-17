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
                                        "to inspect", "to accept", "inspect and")):
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
            r"purchase\s+order(?:s)?|quotation(?:s)?)\b",
            lower,
        ):
            continue
        # "Acceptance of the risk under this policy" is an underwriting /
        # risk-appetite statement, not a deliverables acceptance.  No fixture
        # uses "acceptance of risk" or "underwriting".
        if re.search(
            r"\bacceptance\s+of\s+(?:the\s+|all\s+|any\s+)?risk\b"
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
            r"book\s+(?:s?\w*)?s?\s+(?:and|&)?|audit\b|documentation)\b"
            r"|\b(?:books?|records?|accounts?|audit\s+right\w*|"
            r"documentation)\b[^.]{0,45}\bto\s+inspect\b",
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
        # "the parties acknowledge that product liability for defective goods
        # follows the manufacturer" restates a statutory allocation — no cap,
        # disclaimer or exclusion is being agreed.  Genuine caps ("aggregate
        # liability shall not exceed USD 1M") and disclaimers still fire.
        if re.search(
            r"\bproduct\s+liability\b[^.]{0,45}"
            r"\b(?:defect\w*|manufacturer|consumer|statutory|"
            r"shall\s+follow|rests?\s+with|remains?\s+with)\b",
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
            r"\bdoes\s+not\s+(?:include|cover)\b",
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
