"""Versioned review metrics and the historical-validation gate.

This module deliberately does not read DuckDB or render HTML. It owns the
names, formulas and validation semantics of review metrics so daily, weekly
and monthly renderers cannot silently invent different composite-score rules.

The theme composite score is experimental until historical validation and
operator approval are both complete. A numeric score alone never proves
production readiness.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence


METRIC_CONTRACT_VERSION = "review-metrics-v1"
COMPOSITE_SCORE_STATUS = "experimental_unvalidated"


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    unit: str
    source: str
    formula: str
    status: str = "canonical"


METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "theme_breadth", "题材广度", "count",
        "same-date deduplicated limit-up pool + THS membership",
        "distinct limit-up stocks belonging to the concept",
    ),
    MetricDefinition(
        "theme_height", "题材高度", "board",
        "same-date canonical limit-up pool",
        "maximum verified consecutive board level among the concept's limit-up stocks",
    ),
    MetricDefinition(
        "theme_persistence", "题材持续性", "days",
        "versioned same-date concept snapshots",
        "number of valid trading days with concept limit-up breadth in the evaluation window",
    ),
    MetricDefinition(
        "theme_flow", "题材资金", "yuan",
        "canonical THS concept capital-flow snapshot",
        "same-date concept main-net inflow after source/unit normalization",
    ),
    MetricDefinition(
        "theme_composite_score", "题材综合分", "0-100",
        "the four metrics above",
        "35% breadth + 25% height + 20% persistence + 20% flow, each cross-sectional percentile",
        status=COMPOSITE_SCORE_STATUS,
    ),
)


_WEIGHTS = {"breadth": 0.35, "height": 0.25, "persistence": 0.20, "flow": 0.20}


def metric_contract() -> dict[str, Any]:
    """Return a JSON-serializable contract for reports and audit tools."""
    return {
        "version": METRIC_CONTRACT_VERSION,
        "composite_score": {
            "status": COMPOSITE_SCORE_STATUS,
            "formal_ready": False,
            "formal_gate": (
                "historical validation with sufficient dates and samples, "
                "leakage checks, stability review, and explicit operator approval"
            ),
            "weights": dict(_WEIGHTS),
        },
        "metrics": [asdict(item) for item in METRIC_DEFINITIONS],
    }


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _percentile(value: Any, values: Iterable[Any]) -> float | None:
    number = _number(value)
    population = sorted(
        item for item in (_number(candidate) for candidate in values)
        if item is not None
    )
    if number is None or not population:
        return None
    if len(population) == 1:
        return 100.0
    rank = sum(1 for item in population if item <= number)
    return max(0.0, min(100.0, rank * 100.0 / len(population)))


def _raw_value(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, "", "-"):
            return row[name]
    return None


def experimental_theme_score(
    row: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Score one theme using cross-sectional percentiles.

    ``peers`` must contain themes from the same trade date. Supplying no
    peers is allowed only when the row already contains ``*_pctile`` fields.
    Missing components produce no score instead of silently substituting zero.
    """
    peer_rows = list(peers) or [row]
    fields = {
        "breadth": ("breadth_pctile", ("limit_up_count", "breadth")),
        "height": ("height_pctile", ("max_board", "height", "highest_board")),
        "persistence": (
            "persistence_pctile",
            ("persistence_days", "duration_days", "persistence"),
        ),
        "flow": ("flow_pctile", ("main_net_inflow", "main_net", "flow")),
    }
    components: dict[str, float | None] = {}
    for component, (percentile_key, raw_keys) in fields.items():
        direct = _number(row.get(percentile_key))
        if direct is not None:
            components[component] = max(0.0, min(100.0, direct))
            continue
        value = _raw_value(row, raw_keys)
        components[component] = _percentile(
            value, (_raw_value(peer, raw_keys) for peer in peer_rows)
        )

    missing = [key for key, value in components.items() if value is None]
    result: dict[str, Any] = {
        "status": COMPOSITE_SCORE_STATUS,
        "formal_ready": False,
        "score": None,
        "components": components,
        "missing_components": missing,
        "contract_version": METRIC_CONTRACT_VERSION,
    }
    if missing:
        result["reason"] = "insufficient_components"
        return result
    result["score"] = round(
        sum(components[key] * _WEIGHTS[key] for key in _WEIGHTS), 4
    )
    result["reason"] = "experimental_only"
    return result


def average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for original, _ in ordered[index:end]:
            ranks[original] = rank
        index = end
    return ranks


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / (left_var * right_var) ** 0.5


def validate_experimental_theme_scores(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_samples: int = 60,
    min_dates: int = 20,
) -> dict[str, Any]:
    """Summarize out-of-sample evidence without promoting the score.

    Each sample must contain ``trade_date``, ``score`` and
    ``forward_return_pct``. The function deliberately returns
    ``formal_ready=False`` even when sample thresholds are met; formalization
    requires a separate explicit operator decision after reviewing leakage,
    stability and economic significance.
    """
    usable = []
    for sample in samples:
        date = sample.get("trade_date") or sample.get("date")
        score = _number(sample.get("score"))
        forward = _number(sample.get("forward_return_pct", sample.get("forward_return")))
        if date and score is not None and forward is not None:
            usable.append((str(date), score, forward))
    scores = [item[1] for item in usable]
    returns = [item[2] for item in usable]
    unique_dates = len({item[0] for item in usable})
    enough = len(usable) >= min_samples and unique_dates >= min_dates
    ic = correlation(average_ranks(scores), average_ranks(returns)) if enough else None
    result = {
        "contract_version": METRIC_CONTRACT_VERSION,
        "status": "validation_ready" if enough else "insufficient_sample",
        "formal_ready": False,
        "decision": "remain_experimental",
        "sample_count": len(usable),
        "date_count": unique_dates,
        "spearman_ic": round(ic, 6) if ic is not None else None,
        "positive_forward_rate": (
            round(sum(1 for value in returns if value > 0) / len(returns), 6)
            if returns else None
        ),
        "requirements": {
            "min_samples": min_samples,
            "min_dates": min_dates,
            "leakage_check": "required",
            "stability_check": "required",
            "operator_approval": "required",
        },
    }
    if not enough:
        result["reason"] = "not_enough_validated_history"
    return result



def period_bounds(day: str, period: str) -> tuple[str, str]:
    """Natural periods; a week is Monday through Sunday, not five sessions."""
    from datetime import date, timedelta
    end = date.fromisoformat(day)
    if period == 'day':
        return day, day
    if period == 'week':
        start = end - timedelta(days=end.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if period not in ('month', 'quarter'):
        raise ValueError('unknown natural period')
    month = end.month if period == 'month' else (end.month-1)//3*3+1
    start = end.replace(month=month, day=1)
    following = month + (1 if period == 'month' else 3)
    stop = date(end.year + (following>12), (following-1)%12+1, 1) - timedelta(days=1)
    return start.isoformat(), stop.isoformat()


def market_period_summary(day, period, daily, calendar):
    """Aggregate stored security-session observations, never account returns.

    calendar maps dates to the exact SSE/SZSE flags. Missing calendar dates and
    missing price sessions remain explicit; a source-sized market denominator
    is not inferred from the rows that happened to arrive.
    """
    from datetime import date, timedelta
    start, end = period_bounds(day, period)
    days=[]; cursor=date.fromisoformat(start)
    while cursor <= date.fromisoformat(day):
        days.append(cursor.isoformat());cursor += timedelta(days=1)
    unknown=[d for d in days if calendar.get(d) not in (
        [('SSE',0),('SZSE',0)], [('SSE',1),('SZSE',1)])]
    sessions=[d for d in days if calendar.get(d)==[('SSE',1),('SZSE',1)]]
    rows=[dict(date=d, **daily[d]) for d in sessions if daily.get(d,{}).get('samples',0)>0]
    missing=[d for d in sessions if not daily.get(d,{}).get('samples',0)]
    for row in rows:
        if any(type(row.get(k)) is not int or row[k]<0 for k in ('rise','fall','flat','samples')):
            raise ValueError('nonnegative observed counts required')
        if row['samples'] != row['rise']+row['fall']+row['flat']:
            raise ValueError('breadth counts must reconcile')
    total=sum(r['samples'] for r in rows)
    return {'contract':'available-security-session-breadth-v1','period':period,
        'start':start,'end':end,'through':day,'unfinished_period':day<end,
        'status':'calendar_unverified' if unknown else 'missing_sessions' if missing else 'observed_sessions' if sessions else 'closed_period',
        'calendar_missing':unknown,'expected_sessions':sessions,'missing_sessions':missing,
        'rows':rows,'security_session_samples':total,
        'rising_observation_ratio':sum(r['rise'] for r in rows)/total if total else None,
        'scope':'available_observations_not_exchange_coverage_or_strategy_win_rate',
        'point_in_time_qualified':False,'execution_ready':False}


def judgement_period_summary(day, period, notes, reviews):
    """Receipt-time cohort; retain revisions/rejections and unknown outcomes."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    start, end = period_bounds(day, period)
    selected = []
    for note in notes:
        received = datetime.fromisoformat(note['received_at'])
        if received.tzinfo is None:
            raise ValueError('judgement receipt time must be timezone aware')
        if start <= received.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() <= day:
            selected.append(note)
    attached = {}
    for review in reviews:
        received = datetime.fromisoformat(review['received_at'])
        if received.tzinfo is None:
            raise ValueError('review receipt time must be timezone aware')
        if received.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() <= day:
            attached.setdefault(review['note_id'], []).append(review)
    rows = []
    for note in selected:
        history = sorted(attached.get(note['note_id'], []), key=lambda r: r['received_at'])
        rows.append({'note_id':note['note_id'], 'instrument':note['instrument'],
            'intent':note['intent'], 'supersedes':note.get('supersedes'),
            'hypothesis':note['hypothesis'], 'invalidation':note['invalidation'],
            'received_at':note['received_at'], 'reviews':history,
            'outcome':history[-1].get('conclusion','unknown') if history else 'unreviewed',
            'realized_return':None})
    return {'start':start,'end':end,'through':day,'rows':rows,
        'record_count':len(rows),'revision_count':sum(bool(r['supersedes']) for r in rows),
        'rejected_count':sum(r['intent']=='reject' for r in rows),
        'unreviewed_count':sum(r['outcome']=='unreviewed' for r in rows),
        'scope':'human_receipt_cohort_not_trades_or_strategy_win_rate',
        'execution_ready':False}


def theme_evolution(sessions, expected_sessions):
    """Compare certified daily members; a missing day is never a disappearance."""
    by_date = {s['date']:s for s in sessions}
    codes = sorted({r['concept_code'] for s in sessions for r in s.get('rows',[])})
    rows = []
    for code in codes:
        observations = []
        prior = None
        changes = 0
        for day in expected_sessions:
            session = by_date.get(day,{})
            row = next((r for r in session.get('rows',[]) if r['concept_code']==code),None)
            if row is None:
                observations.append({'date':day,'status':'missing','member_version':None})
                prior = None
                continue
            leaders = sorted(r['stock_code'] for r in row.get('leaders',[]))
            membership=row.get('member_set_id',row['member_version'])
            comparable = prior is not None and prior['member_version']==membership
            if comparable and leaders!=prior['leaders']:changes+=1
            observations.append({'date':day,'status':'observed','member_version':row['member_version'],
                'snapshot_id':session.get('snapshot_id'),
                'leaders':leaders,'leader_changed':leaders!=prior['leaders'] if comparable else None,
                'priced_members':row['priced_members'],'member_count':row['member_count'],
                'flow_status':row['flow_status'],'mean_change_pct':row['mean_change_pct']})
            prior = {'member_version':membership,'leaders':leaders}
        name=next((r.get('concept_name',code) for s in sessions for r in s.get('rows',[]) if r['concept_code']==code),code)
        rows.append({'concept_code':code,'concept_name':name,'observed_sessions':sum(r['status']=='observed' for r in observations),
            'comparable_leader_changes':changes,'observations':observations})
    return {'rows':rows,'missing_sessions':[d for d in expected_sessions if d not in by_date],
        'scope':'retained_membership_and_limit_leaders_not_inferred_theme_stage',
        'point_in_time_qualified':False}
