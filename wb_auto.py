#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_auto.py —— 云端 runner 执行的每日自动化：签到 + 派猫猫旅行，结果推送到群机器人。

设计要点：
  1. 一次运行把签到与派猫都做掉；
  2. 先领已到家的旅行积分，再判断能否派新一趟；今日名额已用则不派；
  3. 签到与猫猫状态分开汇报；
  4. 猫猫失败不污染签到结论 —— 签到成功即整体成功；
  5. 推送通道按需选配（飞书 / 企业微信），配了几条推几条，互不影响；
  6. WB_ACCESS_TOKEN 可放多条 token（换行 / 逗号分隔，可写「名字:token」），
     逐个账号签到后合并成一条消息推送。

凭证来自 GitHub Secret（由本机 refresh_token.py 注入），只读环境变量，不落盘、不打日志。
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
REQUEST_TIMEOUT = 20
MAX_RETRY = 3

CODE_NO_NETWORK = -1


def log(msg):
    print(msg, flush=True)


def masked(text):
    """把返回体里的 token 类字段脱敏后再落日志。"""
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + "***" + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + "..."
    return text


def dig(obj, *keys):
    """逐层取值，任一层缺失返回 None。"""
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request(url, headers, method="GET", payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"raw": raw[:300]}
            # 偶发 3xx（快速轮询时网关会短暂跳转）不是业务结论，退避后重试
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log("  · 收到 %d 跳转，%.1fs 后重试（%d/%d）"
                    % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                import time
                time.sleep(2.0 * attempt)
                continue
            return e.code, body
        except Exception as e:
            # 只对网络抖动与 5xx 重试；写操作靠接口幂等，最多重试一次
            if not retry or attempt >= MAX_RETRY:
                return CODE_NO_NETWORK, {"error": "%s: %s" % (type(e).__name__, e)}
            log("  · 请求异常，%.1fs 后重试（%d/%d）" % (2.0, attempt, MAX_RETRY))
            import time
            time.sleep(2.0)


def get(url, headers):
    return request(url, headers, method="GET")


def post(url, headers, payload=None):
    return request(url, headers, method="POST", payload=payload)


def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get("code", 0) == 0)


def unwrap(body):
    """真实返回统一是 {code,msg,data} 信封，业务字段都在 data 里。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- 签到

def run_checkin(headers, endpoint):
    """查状态 → 未签才领。返回 (ok, 汇报 dict)。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
    log("  [签到-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

    if scode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "网络不可达，签到未完成"}
    if scode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s），请重新运行 refresh_token.py 注入新 token" % scode}
    if not (200 <= scode < 300):
        return False, {"result": "ERROR", "report": "状态接口异常（HTTP %s）" % scode}

    status = unwrap(sbody)
    if dig(status, "active") is False:
        name = dig(status, "activity_name")
        return True, {"result": "INACTIVE", "report": "签到活动未开启" + ("（%s）" % name if name else "")}

    if dig(status, "today_checked_in") in (True, 1):
        return True, {"result": "ALREADY", "report": _fmt_already(status)}

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)
    log("  [签到-领取] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))

    if ccode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "领取请求未送达，签到未完成"}
    if ccode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % ccode}

    if _already_checked_in(ccode, cbody):
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        return True, {"result": "ALREADY",
                      "report": _fmt_already(unwrap(fresh), via="今日已签过（服务端判定已领取）")}

    credit = dig(unwrap(cbody), "credit")
    if credit is not None:
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        s = unwrap(fresh)
        bonus = "，且为连签奖励日" if dig(s, "is_streak_day") else ""
        streak = dig(s, "streak_days")
        cum = dig(s, "total_credits")
        tail = ""
        if streak is not None:
            tail = "（连续 %s 天%s）" % (streak, "，累计 %s 积分" % cum if cum is not None else "")
        return True, {"result": "CLAIMED", "credit": credit,
                      "report": "成功领取 %s 积分%s%s" % (credit, bonus, tail)}

    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        return False, {"result": "ERROR",
                       "report": "领取失败：%s（HTTP %s）" % (cbody.get("msg") or cbody.get("code"), ccode)}
    return False, {"result": "UNKNOWN", "report": "未识别的领取返回"}


def _already_checked_in(code, body):
    if isinstance(body, dict):
        msg = body.get("msg") or ""
        if body.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _fmt_eta(arrive_at, server_now):
    """把秒级时间戳差格式化成人类可读文案；数据缺失时不编造。"""
    if arrive_at is None or server_now is None:
        return ""
    try:
        left = int(arrive_at) - int(server_now)
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "，已到家待领"
    if left < 3600:
        return "，约 %d 分钟后到家" % max(1, left // 60)
    return "，约 %.1f 小时后到家" % (left / 3600)


def _fmt_already(status, via="今日已签过"):
    parts = []
    if dig(status, "today_credit") is not None:
        parts.append("今日 +%s" % dig(status, "today_credit"))
    if dig(status, "streak_days") is not None:
        parts.append("连续 %s 天" % dig(status, "streak_days"))
    if dig(status, "total_credits") is not None:
        parts.append("累计 %s 积分" % dig(status, "total_credits"))
    return "%s（%s）" % (via, "，".join(parts)) if parts else via


# ---------------------------------------------------------------- 猫猫

def run_buddy(headers, endpoint):
    """先领已到家的旅行积分，再判断能否派新一趟。猫猫失败不影响签到结论。"""
    base = endpoint + "/v2/activity/growth"
    notes = []
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        log("  [猫猫-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

        if scode == CODE_NO_NETWORK:
            return {"result": "NETWORK", "report": "网络不可达，猫猫未处理", "notes": notes}
        if scode in (401, 403):
            return {"result": "AUTH", "report": "猫猫认证被拒（HTTP %s）" % scode, "notes": notes}
        if not (200 <= scode < 300):
            return {"result": "ERROR", "report": "旅行状态接口异常（HTTP %s）" % scode, "notes": notes}

        raw_state = dig(unwrap(sbody), "state")
        travel = raw_state
        daily_limit = bool(dig(unwrap(sbody), "daily_limit_reached"))
        travel_body = unwrap(sbody)

        # 1) 已到家 → 先领积分
        if travel == "arrived":
            ccode, cbody = post(base + "/buddy/travel/claim", headers,
                                {"record_id": dig(travel_body, "record_id")})
            log("  [猫猫-领礼物] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
            got = dig(unwrap(cbody), "reward_credit")
            if api_ok(ccode, cbody) and got is not None:
                notes.append("领旅行礼物 +%s 积分" % got)
                travel = "idle"
            else:
                return {"result": "ERROR",
                        "report": "领旅行礼物失败：%s（HTTP %s）" % (dig(cbody, "msg") or "未知", ccode),
                        "notes": notes}
        elif travel == "traveling":
            loc = dig(travel_body, "location", "name") or "?"
            notes.append("旅行中（%s%s）" % (loc, _fmt_eta(dig(travel_body, "arrive_at"),
                                                          dig(travel_body, "server_now"))))

        # 2) 判断能否派新一趟
        if travel == "traveling":
            return {"result": "IN_TRIP", "notes": notes,
                    "report": "；".join(notes) if notes else "猫猫在旅行中，无需派出"}

        if travel == "idle":
            if daily_limit:
                return {"result": "LIMIT_REACHED",
                        "report": "；".join(notes + ["今日旅行名额已用完，不再派出"])}
            kcode, kbody = get(base + "/buddy/travel/config", headers)
            log("  [猫猫-目的地] HTTP %s %s" % (kcode, json.dumps(masked(kbody), ensure_ascii=False)))
            locs = dig(unwrap(kbody), "locations") if 200 <= kcode < 300 else None
            if not locs or not isinstance(locs[0], dict):
                return {"result": "ERROR", "report": "未取到可派目的地", "notes": notes}
            loc = locs[0]
            dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                {"location_id": loc.get("id")})
            log("  [猫猫-派出] HTTP %s %s" % (dcode, json.dumps(masked(dbody), ensure_ascii=False)))
            if 200 <= dcode < 300:
                depart = unwrap(dbody)
                name = dig(depart, "location", "name") or loc.get("name") or "?"
                dur = dig(depart, "duration_hours") or dig(depart, "location", "duration_hours")
                notes.append("已派猫猫去%s%s" % (name, "（约 %s 小时后回）" % dur if dur else ""))
                return {"result": "DEPARTED", "notes": notes, "report": "；".join(notes)}
            msg = dig(unwrap(dbody), "msg") or dig(dbody, "msg") or ""
            # 名额已用完属于正常幂等结果，不算失败
            if "limit" in str(msg).lower() or dcode == 429:
                notes.append("今日旅行名额已用完，不再派出")
                return {"result": "LIMIT_REACHED", "notes": notes, "report": "；".join(notes)}
            return {"result": "ERROR",
                    "report": "派出失败：%s（HTTP %s）" % (msg or "未知", dcode), "notes": notes}

        # 兜底：state 不是已知取值。原样汇报、不做写操作，避免误派或误领。
        return {"result": "UNKNOWN_STATE",
                "report": "；".join(notes) if notes else "未识别的旅行状态：%s" % raw_state,
                "notes": notes}
    except Exception as e:
        return {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e), "notes": notes}


# ---------------------------------------------------------------- 推送通道

def _post_webhook(url, payload, tag, judge):
    """把 JSON POST 到群机器人地址，并交给 judge 判定成败。返回 (ok, reason)。

    重试策略与业务请求、WPS 版保持一致：
      - 网络异常 / HTTP 5xx → 自动重试（最多 MAX_RETRY 次，退避），瞬时故障自愈
      - 业务拒绝（飞书 19001、企业微信 93000 等）→ 不重试（重试无用），
        原因带改法返回，写入结果供 step summary 展示
    兜底：脚本幂等，当天 cron 有多个时段时，下一次运行会自动再推一遍当天结果。
    """
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15,
                                        context=ssl.create_default_context()) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw)
                except ValueError:
                    return False, "%s 返回非 JSON（%s）" % (tag, raw[:80])
                log("  [%s] HTTP %s %s" % (tag, resp.status, json.dumps(body, ensure_ascii=False)))
                return judge(body)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  [%s] HTTP %s，退避后重试（%d/%d）" % (tag, e.code, attempt, MAX_RETRY))
                time.sleep(2.0 * attempt)
                continue
            if e.code >= 500:
                return False, "重试 %d 次后仍 HTTP %s" % (MAX_RETRY, e.code)
            return False, "HTTP %s" % e.code
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  [%s] 请求异常，退避后重试（%d/%d）：%s: %s"
                    % (tag, attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(2.0 * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)


def _judge_feishu(body):
    """飞书自定义机器人：code == 0 才算成功。"""
    if body.get("code") == 0:
        return True, ""
    return False, ("飞书返回码 %s（19001 多为地址不对；机器人若开了签名校验也会失败。"
                   "这是配置问题，重试无用：检查 Secret FEISHU_WEBHOOK，或建机器人时"
                   "不要勾选签名校验）" % body.get("code", "未知"))


def _judge_wecom(body):
    """企业微信群机器人：errcode == 0 才算成功。"""
    code = body.get("errcode")
    if code == 0:
        return True, ""
    return False, ("企业微信返回码 %s（93000 多为地址不对或 key 复制不完整；45009 是超过"
                   "每分钟 20 条的调用限制；机器人若配置了签名校验也会失败。这是配置问题，"
                   "重试无用：检查 Secret WECOM_WEBHOOK）" % (code if code is not None else "未知"))


def build_head(report, overall_ok):
    """卡片标题：单账号写成败，多账号写「几分之几」。"""
    if len(report) == 1:
        return "Buddy 加油站 · %s" % ("成功" if overall_ok else "部分失败")
    ok_n = sum(1 for item in report if item["ok"])
    return "Buddy 加油站 · %d/%d 账号签到成功" % (ok_n, len(report))


def build_lines(title, report):
    """卡片正文：单账号按「签到 / 猫猫旅行」分节，多账号按账号分块。"""
    lines = ["**%s**" % title, ""]
    if len(report) == 1:
        lines += ["**签到**", report[0]["checkin_line"], "",
                  "**猫猫旅行**", report[0]["buddy_line"]]
        return lines
    for item in report:
        if lines[-1] != "":
            lines.append("")
        lines.append("**%s**" % item["label"])
        lines.append(item["checkin_line"])
        lines.append(item["buddy_line"])
    return lines


def push_feishu(webhook, title, report, overall_ok):
    """推送到飞书自定义机器人（交互卡片）。返回 (ok, reason)。

    report 形如 [{"label", "ok", "checkin_line", "buddy_line"}]，一个或多个账号。
    """
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green" if overall_ok else "red",
                "title": {"tag": "plain_text", "content": build_head(report, overall_ok)},
            },
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md",
                                   "content": "\n".join(build_lines(title, report))}}],
        },
    }
    return _post_webhook(webhook, payload, "飞书", _judge_feishu)


def push_wecom(webhook, title, report, overall_ok):
    """推送到企业微信群机器人（markdown 消息）。返回 (ok, reason)。

    企业微信的 markdown 只支持 info（绿）/ comment（灰）/ warning（橙红）三种颜色，
    没有正红，因此失败用 warning 表示；颜色标签不与其他语法嵌套，避免客户端不渲染。
    """
    color = "info" if overall_ok else "warning"
    body = []
    for line in build_lines(title, report):
        body.append(line if (not line or line.startswith("**")) else "> " + line)
    content = "\n".join(["<font color=\"%s\">%s</font>" % (color, build_head(report, overall_ok))]
                        + body)
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    return _post_webhook(webhook, payload, "企业微信", _judge_wecom)


def summarize_push(results):
    """把各通道结果汇总为 (ok, reason)。results 形如 [(通道名, ok, reason)]。

    无通道配置时 ok 为 None —— 这是「未通知」，与「推送失败」区分开；
    任一通道失败则整体记失败，原因逐个拼接，便于一次看清该改哪个配置。
    """
    if not results:
        return None, "未配置推送通道（未通知）"
    ok = all(item[1] for item in results)
    reasons = ["%s：%s" % (name, reason) for name, item_ok, reason in results if not item_ok]
    return ok, "；".join(reasons)


def push_text(ok, reason):
    """单个通道在汇总表里的一句话。"""
    return "✅ 已推送" if ok else "❌ 推送失败：%s" % reason


def not_configured_text(env_name):
    """未配置时的文案：说明没推送，但不记成失败。"""
    return "未通知（未配置 %s）" % env_name


# ---------------------------------------------------------------- 主流程

def parse_tokens(raw):
    """解析 WB_ACCESS_TOKEN，支持一条或多条。返回 [(label, token)]。

    多条用换行 / 逗号 / 分号分隔，每条可写「名字:token」给账号起个名；
    token 的字符集是 A-Za-z0-9._~+/- 加尾部 =，不含这些分隔符与冒号，故可安全切分。
    没写名字的用 token 尾 4 位做指纹当标签；重复 token 只保留一条。
    """
    accounts, seen = [], set()
    for chunk in re.split(r"[\n,;]+", raw or ""):
        item = chunk.strip()
        if not item:
            continue
        label, sep, token = item.partition(":")
        if not sep:
            label, token = "", item
        token = token.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        accounts.append((label.strip() or "账号(%s)" % token[-4:], token))
    return accounts


def run_account(label, token, endpoint):
    """跑一个账号的签到与猫猫。返回 {label, ok, checkin, buddy, checkin_line, buddy_line}。"""
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }
    try:
        checkin_ok, checkin = run_checkin(headers, endpoint)
    except Exception as e:
        checkin_ok, checkin = False, {"result": "ERROR",
                                      "report": "签到模块异常（%s: %s）" % (type(e).__name__, e)}
    log("    签到：%s" % checkin["report"])

    # 猫猫独立 try：任何异常都不影响签到结论
    try:
        buddy = run_buddy(headers, endpoint)
    except Exception as e:
        buddy = {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e)}
    log("    猫猫：%s" % buddy.get("report", ""))

    return {
        "label": label,
        "ok": checkin_ok,
        "checkin": checkin,
        "buddy": buddy,
        "checkin_line": ("✅ " if checkin_ok else "❌ ") + checkin["report"],
        "buddy_line": ("🐱 " + buddy.get("report", "")) if buddy.get("report") else "🐱 无结论",
    }


def main():
    accounts = parse_tokens(os.environ.get("WB_ACCESS_TOKEN"))
    feishu_webhook = os.environ.get("FEISHU_WEBHOOK")
    wecom_webhook = os.environ.get("WECOM_WEBHOOK")
    endpoint = os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT

    if not accounts:
        log("缺少环境变量 WB_ACCESS_TOKEN（请在本机运行 refresh_token.py 注入 GitHub Secret）")
        return 2
    if not feishu_webhook and not wecom_webhook:
        log("未配置 FEISHU_WEBHOOK / WECOM_WEBHOOK，本次只打印结果、不推送")

    # 逐个账号跑：某个账号异常不影响其它账号，各自独立记结论
    results = []
    for idx, (label, token) in enumerate(accounts, 1):
        log("=== 账号 %d/%d：%s ===" % (idx, len(accounts), label))
        results.append(run_account(label, token, endpoint))

    # 整体成败只看签到；多账号时要求每个账号的签到都达成
    overall_ok = all(item["ok"] for item in results)
    report = [{"label": item["label"], "ok": item["ok"],
               "checkin_line": item["checkin_line"], "buddy_line": item["buddy_line"]}
              for item in results]

    # 推送状态与业务结论分开呈现：失败原因可诊断，但不污染签到结论。
    # 每个通道独立推送、独立汇报：一个失败不影响另一个；没配的写「未通知」，不记成失败。
    channels = [
        ("飞书", "FEISHU_WEBHOOK", feishu_webhook, push_feishu),
        ("企业微信", "WECOM_WEBHOOK", wecom_webhook, push_wecom),
    ]
    push_results, push_rows, push_records = [], [], []
    for name, env_name, url, send in channels:
        if not url:
            log("  %s未配置（缺 %s），本次不推送" % (name, env_name))
            push_records.append({"name": name, "ok": None,
                                 "reason": "未配置 %s（未通知）" % env_name})
            push_rows.append(("%s推送" % name, not_configured_text(env_name)))
            continue
        ok, reason = send(url, "每日自动化", report, overall_ok)
        push_results.append((name, ok, reason))
        push_records.append({"name": name, "ok": ok, "reason": reason})
        push_rows.append(("%s推送" % name, push_text(ok, reason)))
    push_ok, push_reason = summarize_push(push_results)

    result = {
        "accounts": [{"label": item["label"], "ok": item["ok"],
                      "checkin": {"ok": item["ok"], **item["checkin"]},
                      "buddy": {k: v for k, v in item["buddy"].items() if k != "notes"}}
                     for item in results],
        "push": {"ok": push_ok, "reason": push_reason, "channels": push_records},
        "overall_ok": overall_ok,
    }
    print("RESULT_JSON " + json.dumps(result, ensure_ascii=False))

    # 结果文件只含结论文案，不含 token，供 Actions 写入 step summary
    ok_n = sum(1 for item in results if item["ok"])
    result_file = os.environ.get("WB_RESULT_FILE")
    if result_file:
        try:
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("| 项目 | 结论 |\n| --- | --- |\n")
                if len(results) == 1:
                    f.write("| 签到 | %s |\n" % results[0]["checkin_line"])
                    f.write("| 猫猫旅行 | %s |\n" % results[0]["buddy_line"])
                else:
                    for item in results:
                        f.write("| %s | %s<br>%s |\n"
                                % (item["label"], item["checkin_line"], item["buddy_line"]))
                for label, text in push_rows:
                    f.write("| %s | %s |\n" % (label, text))
                f.write("| 整体 | %s |\n" % ("成功（签到已达成）" if overall_ok
                                             else "失败（签到未达成）" if len(results) == 1
                                             else "%d/%d 账号签到达成" % (ok_n, len(results))))
        except OSError as e:
            log("结果文件写入失败：%s" % e)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())