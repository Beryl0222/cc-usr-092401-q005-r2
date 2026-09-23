"""移动广告合规实验室的领域模型与规则引擎。

设计要点（与专项整治业务约定一一对应）：

* 证据只增不改：设备、构建、事件一经登记只能追加，新版构建与旧版并存，
  复核员不能修改或删除原始事件，只能对规则发现作确认/驳回。
* 采集幂等：上报事件带客户端事件号，重传去重；迟到事件允许补录，重新判定
  只会追加新发现，不会抹掉已有结论。
* 版本固化：任务创建时固化当时生效的规范版本与测试脚本版本，脚本升级只影响
  之后创建的任务。
* 规则与复核分离：自动规则只能产生「涉嫌」发现，复核员确认后才能生成告知材料。
* 复测与回潮：复测通过只关闭当前整改周期，此前问题时段完整保留；再次出现问题
  开启新的周期并累计回潮次数。
"""

import hashlib
import json
from dataclasses import dataclass, field
from itertools import count
from time import time

# 三条标准操作轨迹
TRACK_NORMAL = "normal"            # 正常用户
TRACK_SCREEN_READER = "screen_reader"  # 读屏用户（TalkBack 等）
TRACK_ELDERLY = "elderly"          # 老人模式
TRACKS = (TRACK_NORMAL, TRACK_SCREEN_READER, TRACK_ELDERLY)
TRACK_LABELS = {
    TRACK_NORMAL: "正常用户",
    TRACK_SCREEN_READER: "读屏用户",
    TRACK_ELDERLY: "老人模式",
}

EVENT_AD_SHOWN = "ad_shown"                # 广告展示（锁屏画报/开屏弹窗/插屏等）
EVENT_CLOSE_AFFORDANCE = "close_affordance"  # 关闭入口状态
EVENT_SENSOR_READING = "sensor_reading"    # 摇一摇传感器读数
EVENT_JUMP = "jump"                        # 跳转行为
EVENT_NETWORK_RESPONSE = "network_response"  # 网络响应
EVENT_GESTURE = "gesture"                  # 用户真实操作（用于证明有无交互）
EVENT_TYPES = (
    EVENT_AD_SHOWN,
    EVENT_CLOSE_AFFORDANCE,
    EVENT_SENSOR_READING,
    EVENT_JUMP,
    EVENT_NETWORK_RESPONSE,
    EVENT_GESTURE,
)

SUBJECT_APP = "app"
SUBJECT_ADVERTISER = "advertiser"
SUBJECT_SDK = "sdk"
SUBJECT_TYPES = (SUBJECT_APP, SUBJECT_ADVERTISER, SUBJECT_SDK)
SUBJECT_LABELS = {
    SUBJECT_APP: "应用运营者",
    SUBJECT_ADVERTISER: "广告主",
    SUBJECT_SDK: "嵌入SDK",
}

FINDING_SUSPECTED = "suspected"
FINDING_CONFIRMED = "confirmed"
FINDING_DISMISSED = "dismissed"

CASE_OPEN = "open"
CASE_RECTIFIED = "rectified"

# 规则编号（对承办人员与告知材料保持稳定）
RULE_NO_CLOSE_PATH = "R-CLOSE-001"       # 不存在可操作的关闭路径
RULE_SHAKE_THRESHOLD = "R-SHAKE-001"     # 摇一摇阈值/读数时间低于规范
RULE_AUTO_JUMP = "R-JUMP-001"            # 无用户操作自动跳转

RULE_LABELS = {
    RULE_NO_CLOSE_PATH: "广告缺少可操作关闭路径",
    RULE_SHAKE_THRESHOLD: "摇一摇广告传感器阈值低于规范",
    RULE_AUTO_JUMP: "无用户操作自动跳转或误导跳转",
}

# 缺省规范参数，可由 register_regulation 按版本覆盖
DEFAULT_REGULATION_PARAMS = {
    "close_max_delay_seconds": 3.0,       # 开屏后关闭入口最迟出现时间
    "close_min_touch_target_dp": 44.0,    # 普通模式关闭入口最小可点尺寸
    "close_elderly_min_touch_target_dp": 56.0,  # 老人模式放大后的要求
    "shake_min_acceleration": 15.0,       # 触发摇一摇的最小加速度 m/s^2
    "shake_min_rotation_deg": 35.0,       # 最小旋转角度
    "shake_min_reading_seconds": 3.0,     # 最短持续读数时间
    "rectification_days": 15,             # 整改期限（自然日）
}


class DomainError(ValueError):
    """请求不符合领域约束（字段缺失、状态不允许等）。"""


class NotFoundError(LookupError):
    """引用的实体不存在。"""


# 内容冲突在批次结果中的固定字段名，服务层据此返回 HTTP 409
CONFLICTS_KEY = "conflicts"


def _canonical_event(raw):
    """提取入库留存的白名单业务字段（与快照中可重建的内容完全一致）。"""
    return {
        "event_id": raw["event_id"],
        "seq": raw["seq"],
        "type": raw["type"],
        "occurred_at": raw["occurred_at"],
        "payload": raw.get("payload") or {},
    }


def event_content_hash(raw):
    """对事件业务内容计算规范化摘要（SHA-256）。

    只覆盖实际留存的白名单字段（event_id/seq/type/occurred_at/payload），
    以紧凑、排序键、无多余空白的 UTF-8 JSON 序列化；因此同一次上传入库后
    再从快照重建，摘要必然一致。重传时内容逐字段一致才算完全重传，任何
    差异都会暴露为证据冲突。
    """
    canonical = json.dumps(
        _canonical_event(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require(payload, key, entity):
    if key not in payload or payload[key] in (None, ""):
        raise DomainError(f"{entity}缺少必填字段：{key}")
    return payload[key]


def _subject_key(subject):
    return (subject["type"], subject["id"])


def _display_subject(subject):
    return {"type": subject["type"], "id": subject["id"], "name": subject.get("name", "")}


@dataclass
class Lab:
    """合规实验室的内存仓储与全部业务操作（可整体序列化为 JSON 快照）。"""

    clock: callable = time
    _seq: count = field(default_factory=lambda: count(1), repr=False)
    regulations: dict = field(default_factory=dict)       # version -> regulation
    regulation_order: list = field(default_factory=list)
    scripts: dict = field(default_factory=dict)           # (script_id, version) -> script
    devices: dict = field(default_factory=dict)
    builds: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)            # event_id -> event（全局幂等）
    findings: dict = field(default_factory=dict)
    cases: dict = field(default_factory=dict)             # case 键为责任主体
    notices: dict = field(default_factory=dict)

    def _new_id(self, prefix):
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------------ #
    # 基础档案：规范版本、脚本版本、设备、应用构建
    # ------------------------------------------------------------------ #

    def register_regulation(self, payload):
        """登记一版规范；参数缺省继承 DEFAULT_REGULATION_PARAMS。"""
        version = _require(payload, "version", "规范")
        if version in self.regulations:
            raise DomainError(f"规范版本已存在且不可覆盖：{version}")
        params = dict(DEFAULT_REGULATION_PARAMS)
        params.update(payload.get("params") or {})
        regulation = {
            "version": version,
            "title": payload.get("title", f"移动广告合规规范 {version}"),
            "effective_at": _require(payload, "effective_at", "规范"),
            "params": params,
            "registered_at": self.clock(),
        }
        self.regulations[version] = regulation
        self.regulation_order.append(version)
        return regulation

    def register_script(self, payload):
        """登记一版测试脚本；已创建的任务不会因新版本而改变。"""
        script_id = _require(payload, "script_id", "测试脚本")
        version = _require(payload, "version", "测试脚本")
        key = (script_id, version)
        if key in self.scripts:
            raise DomainError(f"测试脚本版本已存在：{script_id}@{version}")
        script = {
            "script_id": script_id,
            "version": version,
            "created_at": payload.get("created_at", self.clock()),
            "notes": payload.get("notes", ""),
        }
        self.scripts[key] = script
        return script

    def register_device(self, payload):
        device_id = _require(payload, "device_id", "设备")
        if device_id in self.devices:
            raise DomainError(f"设备已登记：{device_id}")
        device = {
            "device_id": device_id,
            "model": _require(payload, "model", "设备"),
            "os_version": _require(payload, "os_version", "设备"),
            "registered_at": self.clock(),
        }
        self.devices[device_id] = device
        return device

    def register_build(self, payload):
        """登记应用构建。开发者提交新版得到新 build_id，旧构建证据原样保留。"""
        app_id = _require(payload, "app_id", "应用")
        version_code = _require(payload, "version_code", "应用构建")
        build_id = payload.get("build_id") or f"{app_id}:{version_code}"
        if build_id in self.builds:
            raise DomainError(f"应用构建已存在，禁止覆盖旧证据：{build_id}")
        build = {
            "build_id": build_id,
            "app_id": app_id,
            "app_name": _require(payload, "app_name", "应用"),
            "developer": _require(payload, "developer", "应用"),
            "version_code": version_code,
            "version_name": payload.get("version_name", str(version_code)),
            "submitted_at": payload.get("submitted_at", self.clock()),
        }
        self.builds[build_id] = build
        return build

    def _regulation_at(self, at, explicit_version=None):
        if explicit_version is not None:
            if explicit_version not in self.regulations:
                raise NotFoundError(f"规范版本不存在：{explicit_version}")
            return self.regulations[explicit_version]
        effective = [
            r for r in self.regulations.values() if r["effective_at"] <= at
        ]
        if not effective:
            raise DomainError("尚无在该时点生效的规范版本，请显式指定 regulation_version")
        return max(effective, key=lambda r: r["effective_at"])

    def _latest_script(self, at):
        available = [s for s in self.scripts.values() if s["created_at"] <= at]
        if not available:
            return None
        return max(available, key=lambda s: (s["created_at"], s["version"]))

    # ------------------------------------------------------------------ #
    # 测试任务：同一构建在不同设备/轨迹上并存
    # ------------------------------------------------------------------ #

    def create_task(self, payload):
        build_id = _require(payload, "build_id", "任务")
        device_id = _require(payload, "device_id", "任务")
        track = payload.get("track", TRACK_NORMAL)
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        if device_id not in self.devices:
            raise NotFoundError(f"设备不存在：{device_id}")
        if track not in TRACKS:
            raise DomainError(f"未知操作轨迹：{track}，允许值：{', '.join(TRACKS)}")
        now = self.clock()
        regulation = self._regulation_at(now, payload.get("regulation_version"))
        script = self._latest_script(now)
        task = {
            "task_id": self._new_id("task"),
            "build_id": build_id,
            "device_id": device_id,
            "track": track,
            "accessibility": {
                "screen_reader_enabled": track == TRACK_SCREEN_READER
                or bool(payload.get("screen_reader_enabled", False)),
                "elderly_mode_enabled": track == TRACK_ELDERLY
                or bool(payload.get("elderly_mode_enabled", False)),
            },
            "regulation_version": regulation["version"],  # 固化
            "script": None if script is None else {"script_id": script["script_id"], "version": script["version"]},
            "status": "collecting",
            "created_at": now,
            "completed_at": None,
            "assessment_count": 0,
            "event_ids": [],
        }
        self.tasks[task["task_id"]] = task
        return task

    def ingest_events(self, task_id, events):
        """幂等接收一批事件。

        接收流程严格分两阶段：

        1. 整批先做字段、序号、时间、事件类型校验。该阶段只读仓储，任何
           一条不合规都会整批失败、不留下任何痕迹（任务、发现、案件、磁盘
           快照都不变）。
        2. 结构全部合规后，再按 ``(task_id, event_id)`` 与规范化内容摘要
           对每条定性：新事件原子提交；摘要一致的计为完全重传（duplicates）；
           同键异内容是证据冲突——冲突项绝不覆盖原证据，而本批中与其不相
           交的合法新事件照常落库（相交并发批次的合法部分不能丢），冲突清
           单放入结果 ``conflicts``（HTTP 层映射为 409，可按 event_id/seq/
           index 定位）。

        任务完成后到达的迟到事件标记 ``late=true`` 并触发一次追加判定；
        乱序（``occurred_at`` 早于既有事件，或序号跳号后补）允许到达，只
        触发规则重算，绝不回改任务固化的规范/脚本版本；判定按稳定指纹去
        重，不重复生成发现，也不重复出具整改通知。
        """
        task = self._get_task(task_id)
        if not isinstance(events, list):
            raise DomainError("事件批次必须是数组：{\"events\": [...]}")

        # ---- 阶段一：整批校验（只读，不触碰任何仓储状态）---- #
        prepared = []
        seen_ids, seen_seqs = set(), set()
        for index, raw in enumerate(events):
            where = f"批次第 {index + 1} 条"
            if not isinstance(raw, dict):
                raise DomainError(f"{where}不是合法事件对象")
            event_id = _require(raw, "event_id", "事件")
            event_type = _require(raw, "type", "事件")
            if event_type not in EVENT_TYPES:
                raise DomainError(
                    f"{where}（event_id={event_id}）未知事件类型：{event_type}"
                )
            seq = _require(raw, "seq", "事件")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                raise DomainError(
                    f"{where}（event_id={event_id}）序号 seq 必须是正整数，实际：{seq!r}"
                )
            occurred_at = _require(raw, "occurred_at", "事件")
            if not isinstance(occurred_at, (int, float)) or isinstance(occurred_at, bool):
                raise DomainError(
                    f"{where}（event_id={event_id}）occurred_at 必须是 UNIX 时间戳（数字）"
                )
            if occurred_at > self.clock() + 300:
                raise DomainError(
                    f"{where}（event_id={event_id}）occurred_at={occurred_at} 晚于当前"
                    "时间（容忍 300s 时钟偏差），不接受来自未来的事件"
                )
            if "payload" in raw and not isinstance(raw["payload"], dict):
                raise DomainError(f"{where}（event_id={event_id}）payload 必须是对象")
            if event_id in seen_ids:
                raise DomainError(f"{where}批次内事件号重复：{event_id}")
            if seq in seen_seqs:
                raise DomainError(f"{where}（event_id={event_id}）批次内序号重复：seq={seq}")
            seen_ids.add(event_id)
            seen_seqs.add(seq)
            prepared.append((index, event_id, seq, event_type, occurred_at, raw))

        # 序号只要求正整数且任务内唯一、与事件号一一对应；允许跳号与乱序
        # 到达（缺口代表事件尚未到齐，之后可以补），但同一序号不能被不同
        # 事件号占用。
        existing = [self.events[(task_id, eid)] for eid in task["event_ids"]]
        seq_owner = {e["seq"]: e["event_id"] for e in existing}
        for _, event_id, seq, _, _, _ in prepared:
            owner = seq_owner.get(seq)
            if owner is not None and owner != event_id:
                raise DomainError(
                    f"任务 {task_id} 序号 seq={seq} 已属于事件 {owner}，"
                    f"本次事件 {event_id} 不能占用同一序号"
                )

        accepted, duplicates, to_commit = [], [], []
        conflicts = []
        for index, event_id, seq, event_type, occurred_at, raw in prepared:
            stored = self.events.get((task_id, event_id))
            if stored is not None:
                incoming_hash = event_content_hash(raw)
                if stored["content_hash"] != incoming_hash:
                    conflicts.append({
                        "event_id": event_id,
                        "seq": seq,
                        "index": index,
                        "stored_hash": stored["content_hash"],
                        "incoming_hash": incoming_hash,
                    })
                else:
                    duplicates.append(event_id)
                continue
            to_commit.append((event_id, seq, event_type, occurred_at, raw))

        # ---- 阶段二：结构全部合规后原子提交合法的不相交事件 ----
        # 同键异内容只拒绝冲突项本身（原证据原样保留），与本批相交批次无
        # 关的合法事件照常落库，冲突通过结果清单可定位（HTTP 409）。
        is_late = task["status"] == "completed"
        received_at = self.clock()
        for event_id, seq, event_type, occurred_at, raw in to_commit:
            event = {
                "event_id": event_id,
                "task_id": task_id,
                "seq": seq,
                "type": event_type,
                "occurred_at": occurred_at,
                "received_at": received_at,
                "late": is_late,
                "payload": raw.get("payload", {}),
                "content_hash": event_content_hash(raw),
            }
            self.events[(task_id, event_id)] = event
            task["event_ids"].append(event_id)
            accepted.append(event_id)
        result = {"accepted": accepted, "duplicates": duplicates}
        if conflicts:
            result[CONFLICTS_KEY] = conflicts
        if accepted and is_late:
            result["assessment"] = self._evaluate(task)
            result["late_arrivals"] = True
        return result

    def complete_task(self, task_id):
        task = self._get_task(task_id)
        if not task["event_ids"]:
            raise DomainError("任务尚无任何事件，不能完成")
        task["status"] = "completed"
        task["completed_at"] = self.clock()
        return self._evaluate(task)

    def _get_task(self, task_id):
        try:
            return self.tasks[task_id]
        except KeyError:
            raise NotFoundError(f"任务不存在：{task_id}")

    def _task_events(self, task):
        rows = [self.events[(task["task_id"], eid)] for eid in task["event_ids"]]
        return sorted(rows, key=lambda e: (e["occurred_at"], e["seq"]))

    # ------------------------------------------------------------------ #
    # 规则引擎：只产出「涉嫌」发现
    # ------------------------------------------------------------------ #

    def _evaluate(self, task):
        """对任务的全部事件运行规则，按指纹去重追加发现。"""
        regulation = self.regulations[task["regulation_version"]]
        params = regulation["params"]
        events = self._task_events(task)
        build = self.builds[task["build_id"]]
        app_subject = {"type": SUBJECT_APP, "id": build["app_id"], "name": build["developer"]}

        ads = [e for e in events if e["type"] == EVENT_AD_SHOWN]
        new_findings = []

        def first_existing(fingerprint):
            for f in self.findings.values():
                if f["task_id"] == task["task_id"] and f["fingerprint"] == fingerprint:
                    return f
            return None

        def add_finding(rule_id, ad_info, subject, chain, evidence, detail, occurred_at):
            # 指纹只取「规则 + 广告位 + 责任主体」这一稳定身份：乱序/迟到补来
            # 的证据事件不得改变指纹，否则重算会把同一违规重复生成成多条发现。
            fingerprint = "|".join([
                rule_id, str(ad_info.get("ad_id", "")),
                str(ad_info.get("placement", "")),
                _subject_key(subject)[0], _subject_key(subject)[1],
            ])
            existing = first_existing(fingerprint)
            if existing:
                # 证据只增不改：补来的事件并入既有发现，结论/详情/时间均不回改
                merged = set(existing["evidence_event_ids"]) | set(evidence)
                existing["evidence_event_ids"] = sorted(merged)
                return existing
            finding = {
                "finding_id": self._new_id("finding"),
                "task_id": task["task_id"],
                "rule_id": rule_id,
                "rule_title": RULE_LABELS[rule_id],
                "status": FINDING_SUSPECTED,
                "responsible_subject": _display_subject(subject),
                "responsibility_chain": chain,
                "ad": ad_info,
                "evidence_event_ids": sorted(evidence),
                "detail": detail,
                "observed_at": occurred_at,
                "regulation_version": task["regulation_version"],
                "created_at": self.clock(),
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "fingerprint": fingerprint,
            }
            self.findings[finding["finding_id"]] = finding
            new_findings.append(finding)
            return finding

        for ad_event in ads:
            ad_payload = ad_event["payload"]
            ad_info = {
                "ad_id": ad_payload.get("ad_id", ad_event["event_id"]),
                "placement": ad_payload.get("placement", "unknown"),
                "creative_id": ad_payload.get("creative_id"),
            }
            refs = ad_info["ad_id"]
            related = [
                e for e in events
                if e["occurred_at"] >= ad_event["occurred_at"]
                and e["payload"].get("ad_id") in (refs, None)
            ]
            affs = [e for e in related if e["type"] == EVENT_CLOSE_AFFORDANCE and e["payload"].get("ad_id") == refs]
            jumps = [e for e in related if e["type"] == EVENT_JUMP and e["payload"].get("ad_id") == refs]

            chain = {"app": _display_subject(app_subject), "advertiser": None, "sdk": None}

            # 规则一：关闭路径是否存在且对当前轨迹可操作
            reasons, close_detail = self._assess_close_path(task, affs, params)
            if reasons:
                detail = {"close_path": close_detail, "violations": reasons}
                add_finding(
                    RULE_NO_CLOSE_PATH, ad_info, app_subject, chain,
                    [ad_event["event_id"]] + [e["event_id"] for e in affs],
                    detail, ad_event["occurred_at"],
                )

            # 跳转类规则：自动跳转 / 摇一摇阈值
            gestures = [e for e in related if e["type"] == EVENT_GESTURE]
            for jump in jumps:
                jp = jump["payload"]
                sdk = self._subject_from_payload(jp.get("sdk"), SUBJECT_SDK)
                advertiser = self._subject_from_payload(
                    jp.get("advertiser") or ad_payload.get("advertiser"), SUBJECT_ADVERTISER
                )
                jump_chain = {
                    "app": _display_subject(app_subject),
                    "advertiser": None if advertiser is None else _display_subject(advertiser),
                    "sdk": None if sdk is None else _display_subject(sdk),
                }
                trigger = jp.get("trigger", "auto")
                evidence = [ad_event["event_id"], jump["event_id"]]

                if trigger == "shake":
                    sensors = [
                        e for e in related
                        if e["type"] == EVENT_SENSOR_READING
                        and e["payload"].get("ad_id") == refs
                        and e["occurred_at"] <= jump["occurred_at"]
                    ]
                    violations, sensor_detail = self._assess_shake(sensors, jp, params)
                    if violations:
                        subject = sdk or advertiser or app_subject
                        detail = {
                            "jump": self._jump_summary(jump, gestures),
                            "sensor": sensor_detail,
                            "violations": violations,
                        }
                        evidence += [e["event_id"] for e in sensors]
                        add_finding(RULE_SHAKE_THRESHOLD, ad_info, subject, jump_chain,
                                    evidence, detail, jump["occurred_at"])
                elif trigger == "auto":
                    subject = sdk or advertiser or app_subject
                    detail = {"jump": self._jump_summary(jump, gestures),
                              "violations": ["跳转发生前无任何用户操作记录"]}
                    add_finding(RULE_AUTO_JUMP, ad_info, subject, jump_chain,
                                evidence, detail, jump["occurred_at"])

        task["assessment_count"] += 1
        return {
            "task_id": task["task_id"],
            "regulation_version": task["regulation_version"],
            "new_suspected_findings": [f["finding_id"] for f in new_findings],
            "suspected_total": len([
                f for f in self.findings.values()
                if f["task_id"] == task["task_id"] and f["status"] == FINDING_SUSPECTED
            ]),
        }

    @staticmethod
    def _subject_from_payload(raw, subject_type):
        if not raw or not raw.get("id"):
            return None
        return {"type": subject_type, "id": raw["id"], "name": raw.get("name", "")}

    def _assess_close_path(self, task, aff_events, params):
        """返回（违规原因列表，关闭路径快照）。"""
        detail = {
            "exists": bool(aff_events),
            "operable": False,
            "visible_after_seconds": None,
            "touch_target_dp": None,
            "screen_reader_actionable": None,
            "track": task["track"],
        }
        if not aff_events:
            return ["广告展示期间未上报任何关闭入口"], detail
        aff = min(aff_events, key=lambda e: e["occurred_at"])
        p = aff["payload"]
        detail["visible_after_seconds"] = p.get("visible_after_seconds")
        detail["touch_target_dp"] = p.get("touch_target_dp")
        detail["screen_reader_actionable"] = p.get("screen_reader_actionable")
        detail["dismiss_label"] = p.get("label")

        reasons = []
        if p.get("present") is False:
            reasons.append("关闭入口不存在")
        if p.get("visible_after_seconds") is not None \
                and p["visible_after_seconds"] > params["close_max_delay_seconds"]:
            reasons.append(
                f"关闭入口迟至 {p['visible_after_seconds']}s 出现，"
                f"超过 {params['close_max_delay_seconds']}s"
            )
        size = p.get("touch_target_dp")
        if size is not None:
            floor = (params["close_elderly_min_touch_target_dp"] if task["track"] == TRACK_ELDERLY
                     else params["close_min_touch_target_dp"])
            if size < floor:
                reasons.append(f"可点区域 {size}dp 小于 {TRACK_LABELS[task['track']]}要求的 {floor}dp")
        if task["track"] == TRACK_SCREEN_READER and not p.get("screen_reader_actionable", False):
            reasons.append("读屏模式下关闭入口不可聚焦或无操作标签")
        detail["operable"] = not reasons
        return reasons, detail

    @staticmethod
    def _assess_shake(sensor_events, jump_payload, params):
        peak_accel = jump_payload.get("peak_acceleration")
        peak_rotation = jump_payload.get("peak_rotation_deg")
        reading_seconds = jump_payload.get("reading_seconds")
        for e in sensor_events:
            p = e["payload"]
            peak_accel = p.get("peak_acceleration", peak_accel)
            peak_rotation = p.get("peak_rotation_deg", peak_rotation)
            reading_seconds = p.get("reading_seconds", reading_seconds)
        detail = {
            "peak_acceleration": peak_accel,
            "peak_rotation_deg": peak_rotation,
            "reading_seconds": reading_seconds,
            "thresholds": {
                "min_acceleration": params["shake_min_acceleration"],
                "min_rotation_deg": params["shake_min_rotation_deg"],
                "min_reading_seconds": params["shake_min_reading_seconds"],
            },
        }
        violations = []
        if peak_accel is not None and peak_accel < params["shake_min_acceleration"]:
            violations.append(f"峰值加速度 {peak_accel} m/s² 低于下限 {params['shake_min_acceleration']}")
        if peak_rotation is not None and peak_rotation < params["shake_min_rotation_deg"]:
            violations.append(f"旋转角度 {peak_rotation}° 低于下限 {params['shake_min_rotation_deg']}")
        if reading_seconds is not None and reading_seconds < params["shake_min_reading_seconds"]:
            violations.append(f"读数持续 {reading_seconds}s 短于下限 {params['shake_min_reading_seconds']}s")
        return violations, detail

    @staticmethod
    def _jump_summary(jump_event, gesture_events):
        p = jump_event["payload"]
        prior_gesture = any(g["occurred_at"] <= jump_event["occurred_at"] for g in gesture_events)
        return {
            "event_id": jump_event["event_id"],
            "at": jump_event["occurred_at"],
            "trigger": p.get("trigger"),
            "target_url": p.get("target_url"),
            "user_gesture_before_jump": prior_gesture,
            "sdk": p.get("sdk"),
            "advertiser": p.get("advertiser"),
        }

    # ------------------------------------------------------------------ #
    # 复核与整改案件
    # ------------------------------------------------------------------ #

    def review_finding(self, finding_id, payload):
        try:
            finding = self.findings[finding_id]
        except KeyError:
            raise NotFoundError(f"发现不存在：{finding_id}")
        decision = _require(payload, "decision", "复核结论")
        if decision not in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("复核结论只能是 confirmed 或 dismissed")
        reviewer = _require(payload, "reviewer", "复核结论")
        finding["status"] = decision
        finding["reviewed_by"] = reviewer
        finding["reviewed_at"] = self.clock()
        finding["review_comment"] = payload.get("comment", "")
        if decision == FINDING_CONFIRMED:
            self._attach_to_case(finding)
        return finding

    def _attach_to_case(self, finding):
        subject = finding["responsible_subject"]
        key = _subject_key(subject)
        case = self.cases.get(key)
        if case is None:
            case = {
                "case_id": self._new_id("case"),
                "responsible_subject": _display_subject(subject),
                "status": CASE_OPEN,
                "relapse_count": 0,
                "cycles": [],
                "created_at": self.clock(),
            }
            self.cases[key] = case
        if case["cycles"] and case["cycles"][-1]["status"] == CASE_RECTIFIED:
            # 已整改后再次出现问题：开启新周期，旧周期作为回潮历史保留
            case["status"] = CASE_OPEN
            case["relapse_count"] += 1
        if not case["cycles"] or case["cycles"][-1]["status"] == CASE_RECTIFIED:
            case["cycles"].append({
                "seq": len(case["cycles"]) + 1,
                "status": CASE_OPEN,
                "opened_at": self.clock(),
                "rectified_at": None,
                "finding_ids": [],
                "notice_ids": [],
                "retests": [],
            })
        cycle = case["cycles"][-1]
        if finding["finding_id"] not in cycle["finding_ids"]:
            cycle["finding_ids"].append(finding["finding_id"])
        finding["case_id"] = case["case_id"]
        finding["case_cycle_seq"] = cycle["seq"]
        return case

    def generate_notice(self, subject_type, subject_id, payload):
        """对已确认发现生成告知材料；涉嫌发现一律不得进入材料。"""
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前整改周期已关闭，不能重复出具告知材料")
        reviewer = _require(payload, "issued_by", "告知材料")
        pending = [
            f for f in (self.findings[fid] for fid in cycle["finding_ids"])
            if f["status"] == FINDING_CONFIRMED and not f.get("notice_id")
        ]
        if not pending:
            raise DomainError("没有尚未告知的已确认发现")
        # 整改期限取各发现所依据规范中最严格（最短）的一个
        deadline_days = min(
            self.regulations[f["regulation_version"]]["params"]["rectification_days"]
            for f in pending
        )
        issued_at = self.clock()
        notice = {
            "notice_id": self._new_id("notice"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "issued_by": reviewer,
            "issued_at": issued_at,
            "rectification_deadline": issued_at + deadline_days * 86400,
            "rectification_days": deadline_days,
            "regulation_versions": sorted({f["regulation_version"] for f in pending}),
            # 快照：后续任何操作都不得改动材料内容
            "findings": [self._finding_snapshot(f) for f in pending],
        }
        self.notices[notice["notice_id"]] = notice
        cycle["notice_ids"].append(notice["notice_id"])
        for f in pending:
            f["notice_id"] = notice["notice_id"]
        return notice

    def record_retest(self, subject_type, subject_id, payload):
        """登记一次复测。复测通过关闭当前周期，但此前问题时段原样保留。"""
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前周期不在整改中")
        task_id = _require(payload, "task_id", "复测")
        task = self._get_task(task_id)
        if task["status"] != "completed":
            raise DomainError("复测任务必须先完成采集与判定")
        subject = {"type": subject_type, "id": subject_id}
        new_findings = [
            f for f in self.findings.values()
            if f["task_id"] == task_id
            and _subject_key(f["responsible_subject"]) == key
        ]
        confirmed_new = [f for f in new_findings if f["status"] == FINDING_CONFIRMED]
        retest = {
            "task_id": task_id,
            "at": self.clock(),
            "result": "passed" if not confirmed_new else "failed",
            "suspected_count": len([f for f in new_findings if f["status"] == FINDING_SUSPECTED]),
            "confirmed_count": len(confirmed_new),
            "by": payload.get("by", ""),
            "note": "复测通过仅关闭当前整改周期，历史问题时段保留在既有周期中"
                    if not confirmed_new else "复测仍发现已确认问题，整改周期保持开启",
        }
        cycle["retests"].append(retest)
        if retest["result"] == "passed":
            cycle["status"] = CASE_RECTIFIED
            cycle["rectified_at"] = retest["at"]
            case["status"] = CASE_RECTIFIED
        return retest

    # ------------------------------------------------------------------ #
    # 查询与报告
    # ------------------------------------------------------------------ #

    def task_report(self, task_id):
        task = self._get_task(task_id)
        build = self.builds[task["build_id"]]
        device = self.devices[task["device_id"]]
        findings = [f for f in self.findings.values() if f["task_id"] == task_id]
        return {
            "task": {
                "task_id": task_id,
                "track": task["track"],
                "track_label": TRACK_LABELS[task["track"]],
                "accessibility": task["accessibility"],
                "status": task["status"],
                "created_at": task["created_at"],
                "regulation_version": task["regulation_version"],
                "script": task["script"],
            },
            "build": {k: build[k] for k in ("build_id", "app_id", "app_name", "developer",
                                            "version_code", "version_name")},
            "device": device,
            "event_count": len(task["event_ids"]),
            "findings": [self._finding_snapshot(f) for f in findings],
        }

    def build_report(self, build_id):
        """同一构建跨设备、跨轨迹的汇总，三轨迹结果并存不合并。"""
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        tasks = [t for t in self.tasks.values() if t["build_id"] == build_id]
        tracks = []
        for t in sorted(tasks, key=lambda x: (x["device_id"], x["track"])):
            report = self.task_report(t["task_id"])
            tracks.append({
                "device_id": t["device_id"],
                "track": t["track"],
                "track_label": TRACK_LABELS[t["track"]],
                "status": t["status"],
                "confirmed": len([f for f in report["findings"] if f["status"] == FINDING_CONFIRMED]),
                "suspected": len([f for f in report["findings"] if f["status"] == FINDING_SUSPECTED]),
                "dismissed": len([f for f in report["findings"] if f["status"] == FINDING_DISMISSED]),
                "findings": report["findings"],
            })
        return {"build": self.builds[build_id], "tracks": tracks}

    def subject_view(self, subject_type, subject_id):
        """承办人员视图：整改期限、各周期与回潮记录。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        case = self.cases.get((subject_type, subject_id))
        if case is None:
            raise NotFoundError("该责任主体尚无案件")
        cycles = []
        for cycle in case["cycles"]:
            notices = [self.notices[nid] for nid in cycle["notice_ids"]]
            cycles.append({
                "seq": cycle["seq"],
                "status": cycle["status"],
                "opened_at": cycle["opened_at"],
                "rectified_at": cycle["rectified_at"],
                "rectification_deadline": min((n["rectification_deadline"] for n in notices), default=None),
                "notices": [{
                    "notice_id": n["notice_id"],
                    "issued_at": n["issued_at"],
                    "rectification_deadline": n["rectification_deadline"],
                    "regulation_versions": n["regulation_versions"],
                    "finding_count": len(n["findings"]),
                } for n in notices],
                "retests": cycle["retests"],
                "problem_period": self._problem_period(cycle),
                "findings": [self._finding_snapshot(self.findings[fid]) for fid in cycle["finding_ids"]],
            })
        return {
            "case_id": case["case_id"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "status": case["status"],
            "relapse_count": case["relapse_count"],
            "current_cycle_seq": case["cycles"][-1]["seq"],
            "cycles": cycles,
        }

    def _problem_period(self, cycle):
        """问题时段：周期内发现的实际观测时间范围（复测通过后仍保留）。"""
        findings = [self.findings[fid] for fid in cycle["finding_ids"]]
        if not findings:
            return None
        return {
            "first_observed_at": min(f["observed_at"] for f in findings),
            "last_observed_at": max(f["observed_at"] for f in findings),
        }

    def _finding_snapshot(self, finding):
        events = []
        for eid in finding["evidence_event_ids"]:
            e = self.events.get((finding["task_id"], eid))
            if e is None:
                continue
            events.append({"event_id": e["event_id"], "type": e["type"],
                           "occurred_at": e["occurred_at"], "late": e["late"],
                           "payload": e["payload"]})
        return {
            "finding_id": finding["finding_id"],
            "task_id": finding["task_id"],
            "rule_id": finding["rule_id"],
            "rule_title": finding["rule_title"],
            "status": finding["status"],
            "responsible_subject": finding["responsible_subject"],
            "responsibility_chain": finding["responsibility_chain"],
            "ad": finding["ad"],
            "detail": finding["detail"],
            "observed_at": finding["observed_at"],
            "regulation_version": finding["regulation_version"],
            "reviewed_by": finding["reviewed_by"],
            "review_comment": finding["review_comment"],
            "evidence": events,
        }

    # ------------------------------------------------------------------ #
    # 持久化快照：键含元组的仓储与自增序列
    # ------------------------------------------------------------------ #

    _TUPLE_DICTS = ("scripts", "events", "cases")

    def to_snapshot(self):
        data = {"next_seq": next(self._seq)}
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            data[name] = getattr(self, name)
        for name in self._TUPLE_DICTS:
            mapping = getattr(self, name)
            data[name] = [{"key": list(key), "value": value} for key, value in mapping.items()]
        return data

    @classmethod
    def from_snapshot(cls, data, clock=time):
        lab = cls(clock=clock)
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            setattr(lab, name, data.get(name, {} if name != "regulation_order" else []))
        for name in cls._TUPLE_DICTS:
            setattr(lab, name, {tuple(item["key"]): item["value"] for item in data.get(name, [])})
        lab._seq = count(data.get("next_seq", 1))
        lab._verify_snapshot()
        return lab

    def _verify_snapshot(self):
        """重启恢复时核对快照：事件内容摘要、任务-事件引用、发现证据引用。

        * 旧快照缺少 ``content_hash`` 时按留存字段重建补齐；
        * 已记录摘要与重建摘要不一致，说明磁盘证据被篡改，直接拒绝启动，
          避免在被污染的证据上继续判定与出告知材料；
        * 任务 ``event_ids`` 与事件仓储、发现证据引用必须双向对得上。
        """
        for key, event in self.events.items():
            task_id, event_id = key
            if event.get("task_id") != task_id or event.get("event_id") != event_id:
                raise DomainError(f"快照事件键与内容不一致：{key}")
            digest = event_content_hash(_canonical_event(event))
            stored = event.get("content_hash")
            if stored is None:
                event["content_hash"] = digest  # 旧版快照补齐
            elif stored != digest:
                raise DomainError(
                    f"快照证据被篡改：任务 {task_id} 事件 {event_id} 的内容摘要不一致"
                    f"（已记录 {stored}，实际 {digest}），拒绝恢复"
                )
        for task_id, task in self.tasks.items():
            for eid in task["event_ids"]:
                if (task_id, eid) not in self.events:
                    raise DomainError(f"快照任务 {task_id} 引用了不存在的事件：{eid}")
        for finding in self.findings.values():
            for eid in finding.get("evidence_event_ids", []):
                if (finding["task_id"], eid) not in self.events:
                    raise DomainError(
                        f"快照发现 {finding['finding_id']} 引用了不存在的证据事件：{eid}"
                    )
