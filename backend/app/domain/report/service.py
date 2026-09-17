"""报告中心（B12）：算写分离——平台先算结论集，叙述只消费结论。

红线（架构 1/2 延伸）：
- 报告内每个数字都来自 compute 单点出口（值/环比/同比）、B10 异动检测、
  B10-2 归因服务——本模块零自产业务数字；
- 规则化叙述（B12-1）从预置句式库拼装，数字全部来自结论集；
- 结论集带稳定 ref（B12-2 LLM 占位符 {{ref:...}} 的回填对账基础）。

周期语义（业务中立）：
- daily   = as_of 当天；
- weekly  = as_of 所在周「上一个完整自然周」（周一~周日）；
- monthly = as_of 的「上一个完整自然月」。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.response import BusinessError
from app.infra.models import Metric, ReportInstance, ReportTemplate, User
from app.infra.repository import Repository

PERIOD_TYPES = ("daily", "weekly", "monthly")
SECTION_KEYS = ("overview", "mom", "yoy", "anomaly", "attribution")
DEFAULT_SECTIONS = {k: True for k in SECTION_KEYS}
PERIOD_LABELS = {"daily": "日报", "weekly": "周报", "monthly": "月报"}


# ---------------------------------------------------------------- 模板 CRUD


def load_sections(raw: str | None) -> dict:
    stored = json.loads(raw or "{}")
    return {**DEFAULT_SECTIONS, **{k: bool(stored[k]) for k in SECTION_KEYS if k in stored}}


def template_to_dict(t: ReportTemplate) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "name": t.name,
        "period_type": t.period_type,
        "metric_ids": json.loads(t.metric_ids_json or "[]"),
        "sections": load_sections(t.sections_json),
        "created_by": t.created_by,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def list_templates(db: Session, project_id: int | None = None) -> list[ReportTemplate]:
    query = db.query(ReportTemplate)
    if project_id is not None:
        query = query.filter(ReportTemplate.project_id == project_id)
    return query.order_by(ReportTemplate.id).all()


def get_template(db: Session, template_id: int) -> ReportTemplate:
    t = db.get(ReportTemplate, template_id)
    if t is None:
        raise BusinessError(f"报告模板 {template_id} 不存在", 40400)
    return t


def validate_sections(body: dict | None) -> dict:
    if body is None:
        return dict(DEFAULT_SECTIONS)
    if not isinstance(body, dict):
        raise BusinessError("sections 必须是对象", 40000)
    unknown = set(body) - set(SECTION_KEYS)
    if unknown:
        raise BusinessError(f"未知章节开关 {sorted(unknown)}，可选 {list(SECTION_KEYS)}", 40000)
    return {**DEFAULT_SECTIONS, **{k: bool(body[k]) for k in SECTION_KEYS if k in body}}


def create_template(
    db: Session,
    *,
    project_id: int | None,
    name: str,
    period_type: str,
    metric_ids: list,
    sections: dict | None,
    created_by: str = "",
) -> ReportTemplate:
    if not isinstance(name, str) or not name.strip():
        raise BusinessError("报告模板名称必须是非空字符串", 40000)
    if period_type not in PERIOD_TYPES:
        raise BusinessError(f"period_type 只支持 {' / '.join(PERIOD_TYPES)}", 40000)
    if not isinstance(metric_ids, list) or not metric_ids:
        raise BusinessError("指标集不能为空（至少选择 1 个指标）", 40000)
    known = {m.id for m in db.query(Metric).filter(Metric.id.in_(metric_ids)).all()}
    missing = [mid for mid in metric_ids if mid not in known]
    if missing:
        raise BusinessError(f"指标不存在：{missing}", 40000)
    t = Repository(ReportTemplate, db).add(
        ReportTemplate(
            project_id=project_id,
            name=name.strip(),
            period_type=period_type,
            metric_ids_json=json.dumps([int(m) for m in metric_ids]),
            sections_json=json.dumps(validate_sections(sections)),
            created_by=created_by,
        )
    )
    return t


def update_template(db: Session, t: ReportTemplate, *, body: dict) -> ReportTemplate:
    if "name" in body:
        if not isinstance(body["name"], str) or not body["name"].strip():
            raise BusinessError("报告模板名称必须是非空字符串", 40000)
        t.name = body["name"].strip()
    if "period_type" in body:
        if body["period_type"] not in PERIOD_TYPES:
            raise BusinessError(f"period_type 只支持 {' / '.join(PERIOD_TYPES)}", 40000)
        t.period_type = body["period_type"]
    if "metric_ids" in body:
        ids = body["metric_ids"]
        if not isinstance(ids, list) or not ids:
            raise BusinessError("指标集不能为空（至少选择 1 个指标）", 40000)
        known = {m.id for m in db.query(Metric).filter(Metric.id.in_(ids)).all()}
        missing = [mid for mid in ids if mid not in known]
        if missing:
            raise BusinessError(f"指标不存在：{missing}", 40000)
        t.metric_ids_json = json.dumps([int(m) for m in ids])
    if "sections" in body:
        t.sections_json = json.dumps(validate_sections(body["sections"]))
    Repository(ReportTemplate, db).update(t)
    return t


def delete_template(db: Session, t: ReportTemplate) -> None:
    Repository(ReportTemplate, db).delete(t.id)


# ---------------------------------------------------------------- 周期解析


def resolve_period(period_type: str, as_of: date | None) -> tuple[date, date, str]:
    """日报=当天；周报=上一个完整自然周；月报=上一个完整自然月。"""
    d = as_of or date.today()
    if period_type == "daily":
        start = end = d
        return start, end, f"{d.isoformat()}"
    if period_type == "weekly":
        # 上一个完整自然周：as_of 所在周的上一周一 ~ 周日
        monday_this = d - timedelta(days=d.weekday())
        start = monday_this - timedelta(days=7)
        end = start + timedelta(days=6)
        return start, end, f"{start.isoformat()} ~ {end.isoformat()}"
    # monthly：上一个完整自然月
    first_this = d.replace(day=1)
    end = first_this - timedelta(days=1)
    start = end.replace(day=1)
    return start, end, f"{start.year}-{start.month:02d}"


# ---------------------------------------------------------------- 结论计算


def _fmt_pct(v) -> str:
    return f"{v:+.1f}%" if v is not None else "无基期数据"


def compute_conclusions(
    db: Session,
    user: User,
    *,
    metrics: list[Metric],
    start: date,
    end: date,
    sections: dict,
) -> dict:
    """结论集：值/环比/同比 + 异动 + 归因 TopN。全部经既有单点出口。

    refs 结构（B12-2 占位符回填对账基础）：
    {ref: {label, value, unit_none, mom_pct, yoy_pct, start, end}}
    """
    from app.domain.anomaly import attribution as attribution_svc
    from app.domain.anomaly import service as anomaly_svc
    from app.domain.query.service import (
        compute_metric_value,
        list_breakdown_dimensions,
    )

    s, e = start.isoformat(), end.isoformat()
    conclusions: list[dict] = []
    refs: dict[str, dict] = {}

    for m in metrics:
        mom = compute_metric_value(
            db, metric_ref=m.id, start=s, end=e, compare="mom", user=user
        )
        yoy = (
            compute_metric_value(
                db, metric_ref=m.id, start=s, end=e, compare="yoy", user=user
            )
            if sections.get("yoy")
            else None
        )
        ref = f"m{m.id}"
        cmp_mom = mom.get("compare") or {}
        cmp_yoy = (yoy or {}).get("compare") or {}
        item = {
            "ref": ref,
            "metric_id": m.id,
            "code": m.code,
            "name": m.name,
            "value": mom["value"],
            "data_through": mom.get("data_through"),
            "period_complete": mom.get("period_complete"),
            "constant": mom.get("constant"),
            "mom_value": cmp_mom.get("value"),
            "mom_pct": cmp_mom.get("change_pct"),
            "yoy_value": cmp_yoy.get("value"),
            "yoy_pct": cmp_yoy.get("change_pct"),
        }
        conclusions.append(item)
        refs[ref] = {
            "label": f"{m.name}（{m.code}）本期值",
            "value": mom["value"],
            "mom_pct": cmp_mom.get("change_pct"),
            "yoy_pct": cmp_yoy.get("change_pct"),
            "start": s,
            "end": e,
        }

    # ---- 异动章节（B10 引擎，复用同一出口；本期末日为检测锚点）----
    anomalies: list[dict] = []
    if sections.get("anomaly"):
        anchor = end  # 检测锚点 = 报告周期末日（日报 end==当天；周/月报=周期尾日）
        for m in metrics:
            try:
                r = anomaly_svc.detect_for_metric(db, user, m, detect_date=anchor)
            except BusinessError:
                continue  # 无时间字段/常数指标等：不参与异动章节
            if r["verdict"] == "abnormal" and r.get("material") is not False:
                # material=False = 反常但未过要紧度门槛，B10-2 契约：不构成异动结论
                anomalies.append(r)
                refs[f"a{m.id}"] = {
                    "label": f"{m.name} 异动当前值（{r['date']}）",
                    "value": r["current"],
                    "abnormality": r.get("abnormality"),
                }
        anomalies.sort(key=lambda r: -(r.get("abnormality") or 0))

    # ---- 归因章节（B10-2 服务：对异动且可加的指标拆 TopN）----
    attributions: list[dict] = []
    if sections.get("attribution") and anomalies:
        for r in anomalies[:3]:  # 报告只展开前 3 项，宁缺毋滥
            m = next((x for x in metrics if x.id == r["metric_id"]), None)
            if m is None:
                continue
            try:
                dims = list_breakdown_dimensions(db, metric_ref=m.id, user=user)
                candidates = dims.get("dimensions") or []
                if not candidates:
                    continue
                att = attribution_svc.attribute_delta(
                    db,
                    user,
                    metric_ref=m.id,
                    start=s,
                    end=e,
                    dimension=candidates[0]["column"],
                    compare="mom",
                    top_n=3,
                )
            except BusinessError:
                continue  # 比率类/无可用维度：报告如实跳过
            attributions.append(att)
            for i, td in enumerate(att.get("top_dimensions", [])[:3], 1):
                refs[f"att{m.id}d{i}"] = {
                    "label": f"{m.name} 归因 {att.get('dimension')}={td['value']} 贡献",
                    "value": td["contribution"],
                    "contribution_pct": td.get("contribution_pct"),
                }

    return {
        "conclusions": conclusions,
        "anomalies": anomalies,
        "attributions": attributions,
        "refs": refs,
    }


# ---------------------------------------------------------------- 规则化叙述


def _metric_sentence(item: dict, sections: dict) -> str:
    name = f"{item['name']}（{item['code']}）"
    if item.get("constant"):
        return f"{name}为全期常数指标，本期值 {_n(item['value'])}（不参与环比/同比）。"
    parts = [f"{name}本期值 {_n(item['value'])}"]
    if item.get("period_complete") is False and item.get("data_through"):
        parts.append(f"（数据截至 {item['data_through']}）")
    if sections.get("mom"):
        parts.append(f"环比 {_fmt_pct(item['mom_pct'])}")
    if sections.get("yoy"):
        parts.append(f"同比 {_fmt_pct(item['yoy_pct'])}")
    return "，".join(parts) + "。"


def _n(v) -> str:
    """数字叙述：整数不带小数、小数保留 4 位以内——数字只来自结论集。"""
    if v is None:
        return "无数据"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def build_narrative(result: dict, sections: dict, period: dict) -> list[dict]:
    """预置句式库拼装：每个数字都引用结论集字段，不引入任何新数值。"""
    narrative: list[dict] = []
    conclusions = result["conclusions"]
    if sections.get("overview"):
        narrative.append(
            {
                "section": "overview",
                "sentences": [
                    f"本{PERIOD_LABELS[period['type']]}覆盖周期 {period['start']} ~ {period['end']}，"
                    f"共纳入 {len(conclusions)} 个指标。"
                ],
            }
        )
    if sections.get("mom") or sections.get("yoy"):
        narrative.append(
            {
                "section": "metrics",
                "sentences": [_metric_sentence(c, sections) for c in conclusions],
            }
        )
    anomalies = result["anomalies"]
    if sections.get("anomaly"):
        if not anomalies:
            narrative.append(
                {"section": "anomaly", "sentences": ["本期检测范围内未发现反常波动。"]}
            )
        else:
            sentences = [
                f"检测到 {len(anomalies)} 项反常波动（先排除周期性后判定，超过要紧度门槛）："
            ]
            for r in anomalies:
                abnormality = (
                    f"{r['abnormality']:.2f} 倍标准差"
                    if r.get("abnormality") is not None
                    else "恒定基准偏离"
                )
                direction = "高于" if r["direction"] == "up" else "低于"
                sentences.append(
                    f"{r['name']}（{r['metric_code']}）在 {r['date']} {direction}正常水平："
                    f"当天 {_n(r['current'])}，基准均值 {_n(r['baseline']['mean'])}，"
                    f"偏离约 {abnormality}。"
                )
            narrative.append({"section": "anomaly", "sentences": sentences})
    if sections.get("attribution"):
        for att in result["attributions"]:
            tops = att.get("top_dimensions", [])[:3]
            if not tops:
                continue
            sentences = [
                f"{att['name']} 变化按「{att['dimension']}」拆解，主要来源："
            ]
            for td in tops:
                pct = td.get("contribution_pct")
                pct_txt = f"{pct:.1f}%" if pct is not None else "—"
                sentences.append(
                    f"{att['dimension']} = {td['value']} 贡献 {_n(td['contribution'])}（{pct_txt}）；"
                )
            if att.get("others_contribution"):
                sentences.append(
                    f"其余来源合计贡献 {_n(att['others_contribution'])}。"
                )
            narrative.append({"section": "attribution", "sentences": sentences})
    return narrative


# ---------------------------------------------------------------- 报告预览


def generate_report(
    db: Session,
    user: User,
    *,
    template_id: int | None = None,
    period_type: str | None = None,
    metric_ids: list | None = None,
    sections: dict | None = None,
    as_of: str | None = None,
    project_id: int | None = None,
) -> dict:
    """生成报告（无 LLM 完整可用版）。template_id 或临时参数二选一。"""
    if template_id is not None:
        t = get_template(db, template_id)
        period_type = t.period_type
        metric_ids = json.loads(t.metric_ids_json)
        sections = load_sections(t.sections_json)
        project_id = t.project_id
    if period_type not in PERIOD_TYPES:
        raise BusinessError(f"period_type 只支持 {' / '.join(PERIOD_TYPES)}", 40000)
    if not metric_ids:
        raise BusinessError("指标集不能为空", 40000)
    sections = validate_sections(sections)  # 合并默认开关 + 校验未知键

    metrics = db.query(Metric).filter(Metric.id.in_(metric_ids)).all()
    metrics = [m for m in metrics if m.status != "deleted"]
    if not metrics:
        raise BusinessError("模板内指标均已删除，无法生成报告", 40000)

    anchor = date.fromisoformat(as_of) if as_of else None
    start, end, label = resolve_period(period_type, anchor)
    result = compute_conclusions(
        db, user, metrics=metrics, start=start, end=end, sections=sections
    )
    period_info = {"type": period_type, "start": start.isoformat(), "end": end.isoformat(), "label": label}
    # B12-2：规则句先算好（既是无 LLM 时的正文，也是 LLM 章节级降级的兜底）
    rule = build_narrative(result, sections, period_info)
    result["rule_narrative"] = rule
    from app.domain.report.narrative import try_narrative

    narr = try_narrative(db, result, sections, period_info)
    return {
        "title": f"{PERIOD_LABELS[period_type]} · {period_info['label']}",
        "period": period_info,
        "sections": sections,
        "conclusions": result["conclusions"],
        "anomalies": result["anomalies"],
        "attributions": result["attributions"],
        "narrative": narr["narrative"],
        "narrative_source": narr["narrative_source"],
        "llm_degraded": narr["llm_degraded"],
        "refs": result["refs"],  # B12-2 占位符回填对账基础
    }


# ---------------------------------------------------------------- 存档与历史（B12-3）


def instance_meta(i: ReportInstance) -> dict:
    return {
        "id": i.id,
        "template_id": i.template_id,
        "template_name": i.template_name,
        "period_type": i.period_type,
        "period_start": i.period_start.isoformat(),
        "period_end": i.period_end.isoformat(),
        "version": i.version,
        "narrative_source": i.narrative_source,
        "created_by": i.created_by,
        "created_at": i.created_at,
    }


def list_instances(
    db: Session, *, template_id: int | None = None, project_id: int | None = None
) -> list[ReportInstance]:
    query = db.query(ReportInstance)
    if template_id is not None:
        query = query.filter(ReportInstance.template_id == template_id)
    if project_id is not None:
        query = query.filter(ReportInstance.project_id == project_id)
    return query.order_by(ReportInstance.id.desc()).limit(100).all()


def get_instance(db: Session, instance_id: int) -> ReportInstance:
    i = db.get(ReportInstance, instance_id)
    if i is None:
        raise BusinessError(f"报告存档 {instance_id} 不存在", 40400)
    return i


def _next_version(db: Session, template_id: int, start: date, end: date) -> int:
    last = (
        db.query(ReportInstance.version)
        .filter(
            ReportInstance.template_id == template_id,
            ReportInstance.period_start == start,
            ReportInstance.period_end == end,
        )
        .order_by(ReportInstance.version.desc())
        .first()
    )
    return (last[0] + 1) if last else 1


def archive_report(
    db: Session,
    *,
    user: User,
    template_id: int,
    report: dict,
    as_of: date | None,
) -> ReportInstance:
    """预览结果实例化存档：快照整体入 content_json，版本按模板×周期递增。"""
    t = get_template(db, template_id)
    period = report["period"]
    instance = ReportInstance(
        project_id=t.project_id,
        template_id=template_id,
        template_name=t.name,
        period_type=period["type"],
        period_start=date.fromisoformat(period["start"]),
        period_end=date.fromisoformat(period["end"]),
        as_of_date=as_of,
        version=_next_version(db, template_id, date.fromisoformat(period["start"]), date.fromisoformat(period["end"])),
        narrative_source=report.get("narrative_source", "rule"),
        content_json=json.dumps(report, ensure_ascii=False, default=str),
        created_by=user.username,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance
