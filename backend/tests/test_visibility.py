"""B4 可见性双出口测试：受限指标在 query 数值出口与 metric 元数据接口同时隐藏。

不变式 2：看板（query）、目录（metrics）、导出（export）三端行为一致；
受限指标的名称与口径不得对无权限用户泄露。
"""

from __future__ import annotations

import io

import pytest

from tests.conftest import create_test_user

METRIC_CODE = "vis_secret_metric"
DATASET = "vis_ds"


def _in_codes(client, headers, code):
    items = client.get("/api/metrics", headers=headers).json()["data"]
    return any(m["code"] == code for m in items)


@pytest.fixture(scope="module")
def vis_env(client):
    """上传数据集 + 建指标 + 三类用户；返回 {metric_id, headers_by_user}。"""
    csv = b"order_date,amount,region\n2026-01-01,100,north\n2026-01-02,250,south\n"
    resp = client.post(
        "/api/datasets/upload",
        files={"file": ("vis.csv", io.BytesIO(csv), "text/csv")},
        data={"name": DATASET},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        "/api/metrics",
        json={
            "code": METRIC_CODE,
            "name": "可见性测试指标",
            "calc_rule": {"base_aggregation": "sum", "source": {"table": DATASET, "column": "amount"}},
        },
    )
    assert resp.status_code == 200, resp.text
    metric_id = resp.json()["data"]["id"]

    return {
        "metric_id": metric_id,
        "viewer_sales": create_test_user(client, "vis_viewer_sales", role="viewer", department="销售部"),
        "viewer_other": create_test_user(client, "vis_viewer_other", role="viewer", department="后勤部"),
        "analyst": create_test_user(client, "vis_analyst", role="analyst"),
        "admin": {"Authorization": client.headers["Authorization"]},
    }


def _restrict(client, vis_env, items):
    resp = client.put(
        f"/api/auth/metrics/{vis_env['metric_id']}/restrictions", json={"items": items}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestVisibilityDualEgress:
    def test_default_visible_to_all(self, client, vis_env):
        assert _in_codes(client, vis_env["viewer_sales"], METRIC_CODE)
        assert _in_codes(client, vis_env["analyst"], METRIC_CODE)

    def test_role_restriction_hides_on_both_egress(self, client, vis_env):
        _restrict(client, vis_env, [{"subject_type": "role", "subject_value": "viewer"}])
        v = vis_env["viewer_sales"]

        # 元数据出口：目录、搜索、详情、SQL、历史
        assert not _in_codes(client, v, METRIC_CODE)
        assert not _in_codes(client, v, "可见性")  # 按名称搜索也不泄露
        resp = client.get(f"/api/metrics/{vis_env['metric_id']}", headers=v)
        assert resp.status_code == 403 and resp.json()["code"] == 40300
        resp = client.get(f"/api/metrics/{vis_env['metric_id']}/sql", headers=v)
        assert resp.status_code == 403
        resp = client.get(f"/api/metrics/{vis_env['metric_id']}/changes", headers=v)
        assert resp.status_code == 403

        # 数值出口：单值与导出
        resp = client.post(
            "/api/query/metric-value",
            json={"metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-02"},
            headers=v,
        )
        assert resp.status_code == 403 and resp.json()["code"] == 40300
        resp = client.post(
            "/api/query/export",
            json={"metric": vis_env["metric_id"], "start": "2026-01-01", "end": "2026-01-02"},
            headers=v,
        )
        assert resp.status_code == 403

        # analyst / admin 不受 role=viewer 登记 影响
        assert _in_codes(client, vis_env["analyst"], METRIC_CODE)
        assert _in_codes(client, vis_env["admin"], METRIC_CODE)

    def test_department_restriction_targets_exact_department(self, client, vis_env):
        _restrict(client, vis_env, [{"subject_type": "department", "subject_value": "销售部"}])
        assert not _in_codes(client, vis_env["viewer_sales"], METRIC_CODE)   # 销售部 viewer 隐藏
        assert _in_codes(client, vis_env["viewer_other"], METRIC_CODE)       # 其他部门 viewer 可见
        resp = client.post(
            "/api/query/metric-value",
            json={"metric": METRIC_CODE, "start": "2026-01-01", "end": "2026-01-02"},
            headers=vis_env["viewer_other"],
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["value"] == 350.0  # 可见用户数值正常

    def test_clear_restrictions_restores_visibility(self, client, vis_env):
        _restrict(client, vis_env, [])
        assert _in_codes(client, vis_env["viewer_sales"], METRIC_CODE)

    def test_restriction_validation(self, client, vis_env):
        resp = client.put(
            f"/api/auth/metrics/{vis_env['metric_id']}/restrictions",
            json={"items": [{"subject_type": "role", "subject_value": "boss"}]},
        )
        assert resp.status_code == 400
        resp = client.put(
            f"/api/auth/metrics/{vis_env['metric_id']}/restrictions",
            json={"items": [{"subject_type": "planet", "subject_value": "earth"}]},
        )
        assert resp.status_code == 400

    def test_restrictions_endpoint_admin_only(self, client, vis_env):
        resp = client.get(
            f"/api/auth/metrics/{vis_env['metric_id']}/restrictions",
            headers=vis_env["analyst"],
        )
        assert resp.status_code == 403

    def test_idempotent_replace(self, client, vis_env):
        first = _restrict(
            client, vis_env, [{"subject_type": "role", "subject_value": "viewer"},
                              {"subject_type": "role", "subject_value": "viewer"}]
        )
        second = _restrict(client, vis_env, [{"subject_type": "role", "subject_value": "viewer"}])
        assert len(first) == len(second) == 1  # 去重 + PUT 幂等
        _restrict(client, vis_env, [])
