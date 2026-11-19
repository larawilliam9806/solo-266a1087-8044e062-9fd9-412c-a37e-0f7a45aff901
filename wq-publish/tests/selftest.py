"""关键流程自测（不依赖 pytest，直接 python -I tests/selftest.py 运行）。

覆盖：
1. CSV 校验：中文表头、重复 (样本,项目)、负数/非数字/空值/缺列/缺批次号
2. 端到端：上传 → 预览(增/改/删/不变分类) → 发布 → 登记版本/行数 → 下游文件哈希一致
3. 并发：两个页面从同一版本发起，仅一个发布成功，另一个 409 须重新预览
4. 崩溃恢复：文件缺失、文件被改坏、遗留临时文件、游离文件、改名后未提交
5. 撤回语义：整批替换后旧行消失；会话取消/重复发布防护
6. 质控趋势：首次发布顺序、末版读数、同批修订只计一次、缺项中断、严格超限判定、
   旧批次重发重算、发布失败不改变判断、参数校验、读取失败不显示旧告警
"""
import hashlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wq_publish.app import create_app
from wq_publish.storage import LIVE_FILENAME
from wq_publish.validation import render_csv, validate_csv

PASS, FAIL = 0, 0
EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "examples")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def read_example(fname):
    with open(os.path.join(EXAMPLE_DIR, fname), "rb") as fh:
        return fh.read()


def upload(client, fname, batch_no="B1"):
    return client.post(
        "/preview",
        data={"batch_no": batch_no,
              "csv_file": (io.BytesIO(read_example(fname)), fname)},
        content_type="multipart/form-data",
    )


# ---------------- 1. 校验单元测试 ----------------
print("== 1. CSV 校验 ==")
rows, errs = validate_csv(read_example("batch_v1.csv"), "B1")
check("合法 CSV 通过并解析出 5 行", errs == [] and len(rows) == 5, str(errs))

rows, errs = validate_csv(read_example("batch_invalid.csv"), "B1")
check("重复(样本,项目)被拒", any("重复" in e for e in errs), str(errs))
check("负浓度被拒", any("负数" in e for e in errs), str(errs))
check("空浓度被拒", any("浓度为空" in e for e in errs), str(errs))
check("空项目被拒", any("检测项目为空" in e for e in errs), str(errs))

bad = "样本号,浓度\nS1,1.0\n".encode()
rows, errs = validate_csv(bad, "B1")
check("缺少检测项目列被拒", rows is None and any("表头" in e for e in errs), str(errs))

rows, errs = validate_csv(read_example("batch_v1.csv"), " ")
check("空批次号被拒", any("批次号" in e for e in errs), str(errs))

for text in ["S1,氨氮,nan", "S1,氨氮,inf", "S1,氨氮,abc"]:
    raw = f"样本号,检测项目,浓度\n{text}\n".encode()
    rows, errs = validate_csv(raw, "B1")
    check(f"非法浓度 {text!r} 被拒", rows is None and len(errs) > 0, str(errs))

raw_zero = "样本号,检测项目,浓度\nS1,氨氮,0\n".encode()
rows, errs = validate_csv(raw_zero, "B1")
check("零浓度（非负边界）通过", errs == [] and rows[0]["concentration"] == 0.0, str(errs))

# ---------------- 2-5. 端到端（隔离临时目录） ----------------
tmp = tempfile.mkdtemp(prefix="wq-selftest-")
data_dir = os.path.join(tmp, "data")
down_dir = os.path.join(tmp, "downstream")
app = create_app(data_dir=data_dir, downstream_dir=down_dir)
client = app.test_client()

print("== 2. 首次发布 v1 ==")
resp = upload(client, "batch_v1.csv", "BATCH-001")
check("预览页 200 且显示增删改", resp.status_code == 200
      and "新增".encode() in resp.data and "撤回".encode() in resp.data)
m = re.search(rb'/publish/([0-9a-f]+)', resp.data)
check("预览页含发布令牌", m is not None)
token1 = m.group(1).decode()

resp = client.post(f"/publish/{token1}")
check("确认发布后 302 回首页", resp.status_code == 302)
reg = app.storage.current_registry()
check("登记为 v1、5 行", reg["current_version"] == 1 and reg["row_count"] == 5,
      f"got {dict(reg) if reg else None}")
live = os.path.join(down_dir, LIVE_FILENAME)
check("下游 CSV 已生成", os.path.exists(live))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("文件哈希与登记一致", file_hash == reg["content_hash"])
check("下游目录无临时文件残留",
      not [n for n in os.listdir(down_dir) if n.startswith(".tmp-")])
resp = client.get("/published.csv")
check("已发布 CSV 可下载", resp.status_code == 200 and b"S001" in resp.data)
resp = client.post(f"/publish/{token1}")
check("同一会话不可重复发布（返回 409 过期页）", resp.status_code == 409
      and app.storage.get_session(token1)["status"] == "published")

print("== 3. 修订预览的增/改/删/不变分类 ==")
resp = upload(client, "batch_v2_revision.csv", "BATCH-002")
body = resp.data.decode()
# v2 相对 v1：S004氨氮新增；S001氨氮、S002COD 修改；S002氨氮 撤回；S001总磷/S003总磷 不变
check("新增 1 行", "+1" in body and "S004" in body)
check("修改 2 行", "~2" in body and "1.25" in body and "35.0" in body)
check("撤回 1 行（S002 氨氮）", "−1" in body and "S002" in body)
check("不变 2 行", "不变项 2" in body)
token2 = re.search(rb'/publish/([0-9a-f]+)', resp.data).group(1).decode()
client.post(f"/publish/{token2}")
reg = app.storage.current_registry()
check("发布后登记为 v2、5 行", reg["current_version"] == 2 and reg["row_count"] == 5)
current = app.storage.current_rows()
check("撤回生效：S002 氨氮已不存在",
      not any(r["sample_no"] == "S002" and r["analyte"] == "氨氮" for r in current))
check("修改生效：S001 氨氮=1.3",
      any(r["sample_no"] == "S001" and r["analyte"] == "氨氮"
          and r["concentration"] == 1.3 for r in current))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("v2 文件哈希与登记一致", file_hash == reg["content_hash"])

print("== 4. 同版本并发修订：只允许一个成功 ==")
r_a = upload(client, "batch_v1.csv", "CONCUR-A")
r_b = upload(client, "batch_v2_revision.csv", "CONCUR-B")
tok_a = re.search(rb'/publish/([0-9a-f]+)', r_a.data).group(1).decode()
tok_b = re.search(rb'/publish/([0-9a-f]+)', r_b.data).group(1).decode()
ok = client.post(f"/publish/{tok_a}")
check("A 发布成功 → v3", ok.status_code == 302
      and app.storage.current_registry()["current_version"] == 3)
conflict = client.post(f"/publish/{tok_b}")
check("B 发布被拒并返回 409 过期预览页", conflict.status_code == 409
      and "该预览已过期".encode() in conflict.data)
sess_b = app.storage.get_session(tok_b)
check("B 会话被标记 obsolete", sess_b["status"] == "obsolete")
reg_before = app.storage.current_registry()["current_version"]
retry = upload(client, "batch_v2_revision.csv", "CONCUR-B-RETRY")
tok_b2 = re.search(rb'/publish/([0-9a-f]+)', retry.data).group(1).decode()
client.post(f"/publish/{tok_b2}")
check("B 重新预览后可发布 → v4",
      app.storage.current_registry()["current_version"] == reg_before + 1)
check("A 的发布未被覆盖：v4 文件行数=5",
      app.storage.current_registry()["row_count"] == 5)

print("== 5. 崩溃后重启自动恢复 ==")
# 5a 下游文件被删除
os.unlink(live)
app2 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app2.storage.current_registry()
check("5a 文件缺失后重建且哈希与登记一致",
      os.path.exists(live)
      and hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5b 文件被外部改坏
with open(live, "ab") as fh:
    fh.write(b"CORRUPTED\n")
app3 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app3.storage.current_registry()
check("5b 损坏文件被按登记重写",
      hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5c 遗留半写临时文件
tmp_orphan = os.path.join(down_dir, ".tmp-publish-deadbeef.csv")
open(tmp_orphan, "wb").write(b"partial")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5c 遗留临时文件被清理", not os.path.exists(tmp_orphan))

# 5d 下游目录出现未登记游离文件
stranger = os.path.join(down_dir, "stranger.csv")
open(stranger, "wb").write(b"x")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5d 游离文件被移入 quarantine",
      not os.path.exists(stranger)
      and any(f.endswith("stranger.csv")
              for f in os.listdir(os.path.join(data_dir, "quarantine"))))

# 5e 模拟“文件已改名但事务未提交”：文件内容比登记新，重启应回滚到登记版本
with app3.storage._connect() as conn:
    old_csv = app3.storage._render_version(conn, reg["current_version"])
newer = render_csv([{"sample_no": "ZX", "analyte": "氨氮", "concentration": 9.9}])
app3.storage._atomic_write(newer)
check("预置：文件已被换成未登记的新版本",
      open(live, "rb").read().decode() != old_csv)
app4 = create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5e 改名后未提交 → 恢复为登记的完整旧版",
      open(live, "rb").read().decode() == old_csv)
events = app4.storage.recent_events(50)
check("恢复动作写入事件日志", any(e["kind"] == "recover" for e in events))

print("== 6. 会话取消 ==")
r = upload(client, "batch_v1.csv", "CANCEL-ME")
tok = re.search(rb'/publish/([0-9a-f]+)', r.data).group(1).decode()
v_before = app.storage.current_registry()["current_version"]
client.post(f"/cancel/{tok}")
check("取消后发布被拒绝（409 且会话 cancelled）",
      client.post(f"/publish/{tok}").status_code == 409
      and app.storage.get_session(tok)["status"] == "cancelled")
check("取消不影响当前版本",
      app.storage.current_registry()["current_version"] == v_before)

shutil.rmtree(tmp, ignore_errors=True)

# ---------------- 7. 质控趋势页 ----------------
print("== 7. 质控趋势：顺序、末版读数、缺项中断、严格判定 ==")
trend_tmp = tempfile.mkdtemp(prefix="wq-trend-")
tapp = create_app(data_dir=os.path.join(trend_tmp, "data"),
                  downstream_dir=os.path.join(trend_tmp, "downstream"))
tc = tapp.test_client()
TREND_QS = "/trend?sample_no=QC1&analyte=氨氮&target=1.0&stddev=0.05"


def preview_only(client_, batch_no, rows):
    csv_text = "样本号,检测项目,浓度(mg/L)\n" + "".join(
        f"{s},{a},{c}\n" for s, a, c in rows)
    resp = client_.post("/preview", data={
        "batch_no": batch_no,
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), f"{batch_no}.csv"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.data[:300]
    return re.search(rb"/publish/([0-9a-f]+)", resp.data).group(1).decode()


def publish_rows(client_, batch_no, rows):
    resp = client_.post(f"/publish/{preview_only(client_, batch_no, rows)}")
    assert resp.status_code == 302, resp.data[:300]


# 目标 1.0、σ 0.05 → 上限 1.10、下限 0.90
plan = [
    ("QA", [("QC1", "氨氮", 1.20), ("S1", "COD", 5.0)]),  # v1 高
    ("QB", [("QC1", "氨氮", 1.30)]),                      # v2 高 → 告警 QB
    ("QC", [("S2", "COD", 3.0)]),                         # v3 缺项 → 中断连续段
    ("QD", [("QC1", "氨氮", 1.40)]),                      # v4 高（前批缺项，无告警）
    ("QE", [("QC1", "氨氮", 1.50)]),                      # v5 高 → 告警 QE
    ("QF", [("QC1", "氨氮", 0.80)]),                      # v6 低
    ("QG", [("QC1", "氨氮", 0.70)]),                      # v7 低 → 告警 QG
    ("QH", [("QC1", "氨氮", 1.10)]),                      # v8 恰等于上限，不算超限
    ("QI", [("QC1", "氨氮", 1.25)]),                      # v9 高（前批未严格超限，无告警）
]
for _batch, _rows in plan:
    publish_rows(tc, _batch, _rows)

resp = tc.get(TREND_QS)
check("趋势查询页 200", resp.status_code == 200)
body = resp.data.decode()
check("页面同时展示偏高与偏低告警", "连续偏高" in body and "连续偏低" in body)
check("页面展示缺项与告警标记", "缺项" in body and body.count("告警批次") == 3,
      f"告警批次×{body.count('告警批次')}")

res = tapp.storage.qc_trend("QC1", "氨氮", 1.0, 0.05)
check("9 个批次按首次发布顺序各计一次",
      [p["batch_no"] for p in res["points"]]
      == ["QA", "QB", "QC", "QD", "QE", "QF", "QG", "QH", "QI"])
check("缺项批次读数为 None 且状态 missing",
      res["points"][2]["value"] is None and res["points"][2]["status"] == "missing")
check("恰等于上限不算超限（严格大于）", res["points"][7]["status"] == "ok")
alerts = {(a["later"]["batch_no"], a["direction"],
           a["earlier"]["last_version"], a["later"]["last_version"])
          for a in res["alerts"]}
check("告警为 QB/QE 偏高、QG 偏低，且两批依据版本正确（跨缺项无告警）",
      alerts == {("QB", "high", 1, 2), ("QE", "high", 4, 5), ("QG", "low", 6, 7)},
      str(alerts))

print("== 8. 旧批次重发后告警重算、同批修订只计一次 ==")
publish_rows(tc, "QA", [("QC1", "氨氮", 0.50)])  # v10：QA 末版读数变低
res = tapp.storage.qc_trend("QC1", "氨氮", 1.0, 0.05)
check("同批修订只计一次：仍 9 个批次点", len(res["points"]) == 9)
qa = res["points"][0]
check("QA 仍按首次发布顺序排第一，依据版本更新为 v10、读数 0.5",
      qa["batch_no"] == "QA" and qa["first_version"] == 1
      and qa["last_version"] == 10 and qa["value"] == 0.5)
alerts2 = {(a["later"]["batch_no"], a["direction"]) for a in res["alerts"]}
check("旧批次重发后告警重算：QB 告警消失，QE/QG 保留",
      alerts2 == {("QE", "high"), ("QG", "low")}, str(alerts2))

print("== 9. 发布失败不改变趋势判断 ==")
tok_j = preview_only(tc, "QJ", [("QC1", "氨氮", 1.60)])
tok_k = preview_only(tc, "QK", [("QC1", "氨氮", 1.70)])  # 与 QJ 同基线
resp = tc.post(f"/publish/{tok_j}")
check("QJ 发布成功 → v11", resp.status_code == 302
      and tapp.storage.current_registry()["current_version"] == 11)
before = tapp.storage.qc_trend("QC1", "氨氮", 1.0, 0.05)["alerts"]
check("QI→QJ 连续偏高，新告警出现",
      any(a["later"]["batch_no"] == "QJ" and a["direction"] == "high"
          for a in before))
resp = tc.post(f"/publish/{tok_k}")
check("QK 发布失败（409 过期）", resp.status_code == 409)
after = tapp.storage.qc_trend("QC1", "氨氮", 1.0, 0.05)["alerts"]
check("发布失败不改变趋势判断",
      [(a["direction"], a["earlier"]["batch_no"], a["later"]["batch_no"])
       for a in after]
      == [(a["direction"], a["earlier"]["batch_no"], a["later"]["batch_no"])
          for a in before])

print("== 10. 趋势参数校验 ==")
resp = tc.get("/trend")
check("未填参数时仅显示表单、不显示结果",
      resp.status_code == 200 and "控制上限（".encode() not in resp.data)
bad_cases = [
    ("target=-1&stddev=0.05", "非负"),
    ("target=abc&stddev=0.05", "不是合法数字"),
    ("target=nan&stddev=0.05", "有限数"),
    ("target=inf&stddev=0.05", "有限数"),
    ("target=1.0&stddev=0", "正有限数"),
    ("target=1.0&stddev=-0.5", "正有限数"),
    ("target=1.0&stddev=inf", "有限数"),
]
for qs, msg in bad_cases:
    resp = tc.get(f"/trend?sample_no=QC1&analyte=氨氮&{qs}")
    check(f"非法参数 {qs} 被拒（{msg}）",
          resp.status_code == 200 and msg.encode() in resp.data
          and "控制上限（".encode() not in resp.data)
resp = tc.get("/trend?sample_no=&analyte=氨氮&target=1.0&stddev=0.05")
check("空质控样本号被拒", "质控样本号不能为空".encode() in resp.data)
resp = tc.get("/trend?sample_no=QC1&analyte=&target=1.0&stddev=0.05")
check("空检测项目被拒", "检测项目不能为空".encode() in resp.data)
resp = tc.get("/trend?sample_no=QC1&analyte=氨氮&target=1.0&stddev=")
check("空标准差被拒", "标准差不能为空".encode() in resp.data)
resp = tc.get("/trend?sample_no=QC1&analyte=氨氮&target=0&stddev=0.05")
check("目标浓度 0（非负边界）通过", resp.status_code == 200
      and "控制上限（".encode() in resp.data)

print("== 11. 读取失败不显示旧告警 ==")
def _boom(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")

orig_qc = tapp.storage.qc_trend
tapp.storage.qc_trend = _boom
resp = tc.get(TREND_QS)
check("读取失败：提示错误且绝不渲染旧告警",
      resp.status_code == 200
      and "读取失败".encode() in resp.data
      and "告警批次".encode() not in resp.data
      and "控制上限（".encode() not in resp.data)
tapp.storage.qc_trend = orig_qc
resp = tc.get(TREND_QS)
check("恢复后重新实时计算并显示告警", "告警批次".encode() in resp.data)

print("== 12. 空库与无匹配读数 ==")
empty_tmp = tempfile.mkdtemp(prefix="wq-trend-empty-")
eapp = create_app(data_dir=os.path.join(empty_tmp, "data"),
                  downstream_dir=os.path.join(empty_tmp, "downstream"))
resp = eapp.test_client().get(TREND_QS)
check("无任何发布时正常显示空趋势",
      resp.status_code == 200 and "尚无已发布批次".encode() in resp.data)
res_none = tapp.storage.qc_trend("NO-SUCH", "氨氮", 1.0, 0.05)
check("无匹配读数时全部缺项且无告警",
      all(p["value"] is None for p in res_none["points"])
      and res_none["alerts"] == [])

shutil.rmtree(trend_tmp, ignore_errors=True)
shutil.rmtree(empty_tmp, ignore_errors=True)
print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
