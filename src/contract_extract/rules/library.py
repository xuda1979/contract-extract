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
        m = re.search(
            rf"|\b{_NUMTOKEN}\s+units?\b"
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
        is_penalty = re.search(r"\b(penalty|penalties|liquidated damages|delay damages|damages for delay|breach fee|late fee|late payment)\b", lower) is not None
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
        if re.search(r"\b(no|nothing|not)\s+confidential", lower) or "does not apply" in lower:
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
            "rights in its own", "vest",
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
            r"|\b(?:no|not)\b[^.]{0,40}\b(?:rights?|ownership|title)\b[^.]{0,30}"
            r"\btransfer(?:s|red)?\b",
            lower,
        ):
            continue
        # A "license agreement" heading should not produce an IP-risk clause by itself.
        if re.match(r"^(this\s+)?(software\s+|end[- ]user\s+)?license\w*\s+(agreement|terms)", lower):
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
        risk = RiskLevel.LOW
        flag = None
        detail = None
        if re.search(r"\b(work[- ]for[- ]hire|works made|shall vest in|vested in|are assigned to|is assigned to|assigns? to|shall be assigned|ownership.*passes|owned by)", lower):
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
        # A sub-provision sunset ("this clause shall terminate upon expiry of
        # the warranty") is a schedule detail, not a party's right to terminate
        # the agreement.  "This Agreement shall terminate…" still fires via the
        # agreement-specific context in _TERM_CONTEXT.
        if re.search(r"\bthis\s+(?:clause|section|provision|warranty|covenant|obligation|sublause|condition)\b[^.]{0,40}\bterminat", lower):
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
        # ownership statement, not a personal-data transfer.
        if re.search(
            r"\b(?:no|not|nothing|neither)\b[^.]{0,40}"
            r"\b(?:rights?|ownership|title)\b[^.]{0,30}\btransfer(?:s|red)?\b"
            r"|\btransfer(?:s|red)?\b[^.]{0,30}\b(?:no|not)\b[^.]{0,30}\b(?:rights?|ownership|title)\b"
            r"|\b(?:no|not|nothing|neither)\b[^.]{0,40}\btransfer(?:s|red)?\b[^.]{0,40}"
            r"\b(?:rights?|ownership|title)\b",
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
        if "insurance" in lower:
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
            # "the insurance policy renews / renewal of the insurance policy" is a
            # policy-admin renewal mention, not a coverage obligation at all.
            if re.search(
                r"\b(?:renew\w*|expir\w*|terminat\w*)\b[^.]{0,40}\binsurance\s+policy\b"
                r"|\binsurance\s+policy\b[^.]{0,40}\b(?:renew\w*|expir\w*|terminat\w*)\b"
                r"|\brenewal\s+of\s+(?:the\s+|a\s+|this\s+)?insurance\s+policy\b",
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
