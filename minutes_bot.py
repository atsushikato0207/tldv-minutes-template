# -*- coding: utf-8 -*-
"""tldv → 議事録生成(GPT) → Chatwork 自動連携ボット(複数メンバー対応)。

GitHub Actions で1時間ごとに実行される想定(ローカル実行も可)。
- メンバーごとに本人のtldv APIキーで直近 LOOKBACK_HOURS 時間の会議を取得し、
  「本人が参加したもの」だけを処理する
- 参加判定: 主催者/招待者に本人のメールがある、または文字起こしの話者に本人の名前がある
- 文字起こしを GPT で議事録に要約し、本人のChatworkトークンで届け先ルームへ投稿
  (届け先未指定ならマイチャットを自動判別)。同じ会議の要約は1回だけ生成して使い回す
- 処理済みは「メンバー:会議ID」単位で state.json に記録して二重投稿を防ぐ

メンバー設定(環境変数 MEMBERS_JSON、JSON配列):
  [{"label": "kato",                     # state記録用の一意な英数字ラベル
    "emails": ["a@stock-sun.com", ...],  # 会議で使うメール(参加判定)
    "names": ["加藤敦", "加藤 敦"],       # tldv話者名の表記ゆれ(参加判定)
    "tldv_api_key": "...",               # 本人のtldvキー(Pro以上で発行)
    "chatwork_token": "...",             # 本人のChatworkトークン
    "room_id": "442074010",              # 省略時は本人のマイチャットを自動判別
    "to_id": "4206729"}]                 # 省略可。グループルーム宛てのときのみTo付与

MEMBERS_JSON が無い場合は従来のシングル運用にフォールバック:
  TLDV_API_KEY / CHATWORK_TOKEN / CHATWORK_ROOM_ID(既定442074010) / MY_EMAILS / MY_NAMES

共通: OPENAI_API_KEY(必須) / OPENAI_MODEL(既定 gpt-5-mini)

使い方:
  python minutes_bot.py            # 通常実行(投稿+state更新)
  python minutes_bot.py --dry-run  # 投稿もstate更新もせず、生成した議事録を表示
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
JST = timezone(timedelta(hours=9))

LOOKBACK_HOURS = 26          # これより古い会議は対象外(初回の大量投稿も防ぐ)
SETTLE_MINUTES = 15          # 会議終了からこの時間は文字起こし生成待ちとして保留
MAX_STATE_ENTRIES = 2000
MAX_ATTEMPTS = 3             # 恒常的に失敗する会議(例: 権限都合の403)を諦めるまでの試行回数

# tldv手前のCloudflareがボット風UA/データセンターIPを弾くことがあるため、ブラウザ同等のUAを使う
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

HOME = os.path.expanduser("~")


# ---------- 認証情報 ----------

def _from_json_file(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get(key)
    except OSError:
        return None


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or ""
if not OPENAI_API_KEY:
    sys.exit("認証情報がありません: OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")


def load_members():
    """メンバー一覧を返す。MEMBERS_JSON が無ければ従来のシングル運用(加藤)にフォールバック。"""
    raw = os.environ.get("MEMBERS_JSON")
    if raw:
        # PowerShell経由の登録でBOMが混入することがあるため除去してから解釈する
        members = json.loads(raw.lstrip("﻿"))
    else:
        # 各自デプロイ(シングル運用): 本人のSecretsだけで動く汎用モード。
        # CHATWORK_ROOM_ID 未設定なら本人のマイチャットへ届く
        tldv_key = os.environ.get("TLDV_API_KEY") or _from_json_file(
            os.path.join(HOME, ".claude", "tldv-config.json"), "api_key")
        cw_token = os.environ.get("CHATWORK_TOKEN") or _from_json_file(
            os.path.join(HOME, ".claude", "chatwork-config.json"), "api_token")
        if not (os.environ.get("MY_EMAILS") and os.environ.get("MY_NAMES")):
            sys.exit("MY_EMAILS(会議で使うメール、カンマ区切り)と MY_NAMES(tldv話者名表記、"
                     "カンマ区切り)を Secrets/環境変数に設定してください")
        members = [{
            "label": os.environ.get("MEMBER_LABEL", "me"),
            "emails": os.environ["MY_EMAILS"].split(","),
            "names": os.environ["MY_NAMES"].split(","),
            "tldv_api_key": tldv_key,
            "chatwork_token": cw_token,
            "room_id": os.environ.get("CHATWORK_ROOM_ID", ""),
            "to_id": os.environ.get("MY_CHATWORK_ID", ""),
        }]
    for m in members:
        m["emails"] = [e.strip().lower() for e in m.get("emails", []) if e.strip()]
        m["names"] = [n.strip() for n in m.get("names", []) if n.strip()]
        if not (m.get("label") and m.get("tldv_api_key") and m.get("chatwork_token")):
            sys.exit("メンバー設定が不足しています(label/tldv_api_key/chatwork_token必須): %s"
                     % m.get("label", "?"))
    return members


# ---------- HTTP ----------

def http_json(url, headers, payload=None, form=False, timeout=120, method=None):
    # requests採用の理由: urllibのTLSフィンガープリントがtldv手前のCloudflareに
    # 遮断される(2026-07-10確認)
    headers = dict(headers, **{"User-Agent": UA})
    if method == "PUT":
        resp = requests.put(url, headers=headers, data=payload, timeout=timeout)
    elif payload is None:
        resp = requests.get(url, headers=headers, timeout=timeout)
    elif form:
        resp = requests.post(url, headers=headers, data=payload, timeout=timeout)
    else:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def tldv_get(api_key, path, params=None, retries=4):
    """tldv API GET。Cloudflareの遮断(403)・レート制限(429)・5xxはバックオフつきで再試行。"""
    url = "https://pasta.tldv.io/v1alpha1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(1, retries + 1):
        try:
            return http_json(url, {"x-api-key": api_key, "Accept": "application/json"})
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 404:
                return None
            if code in (403, 429) or code >= 500:
                if attempt < retries:
                    wait = 30 * attempt
                    print(f"tldv API {code}: retry {attempt}/{retries - 1} ({wait}s待機)")
                    time.sleep(wait)
                    continue
            raise


# ---------- Chatwork ----------

_mychat_cache = {}


def resolve_room(member):
    """届け先ルームID。room_id 未指定なら本人のマイチャットを自動判別してキャッシュ。"""
    if member.get("room_id"):
        return str(member["room_id"]), False
    token = member["chatwork_token"]
    if token not in _mychat_cache:
        rooms = http_json("https://api.chatwork.com/v2/rooms", {"X-ChatWorkToken": token})
        my = [r for r in rooms if r.get("type") == "my"]
        if not my:
            raise RuntimeError("マイチャットが見つかりません: %s" % member["label"])
        _mychat_cache[token] = str(my[0]["room_id"])
    return _mychat_cache[token], True


def chatwork_post(member, message):
    """本人トークンで届け先へ投稿し、未読化する。グループルーム宛てのみToを付ける。"""
    room, is_mychat = resolve_room(member)
    body = message
    if member.get("to_id") and not is_mychat:
        body = "[To:%s]\n%s" % (member["to_id"], message)
    result = http_json(
        "https://api.chatwork.com/v2/rooms/%s/messages" % room,
        {"X-ChatWorkToken": member["chatwork_token"]},
        payload={"body": body},
        form=True,
    )
    # 自分のトークンでの投稿は既読扱いで通知に気付けないため未読へ戻す。
    # To付き投稿は最初から未読のため400(既に未読)は正常扱い
    try:
        if result.get("message_id"):
            resp = requests.put(
                "https://api.chatwork.com/v2/rooms/%s/messages/unread" % room,
                headers={"X-ChatWorkToken": member["chatwork_token"], "User-Agent": UA},
                data={"message_id": result["message_id"]},
                timeout=30,
            )
            if resp.status_code != 400:
                resp.raise_for_status()
    except Exception as e:
        print("未読設定に失敗(投稿自体は成功): %s" % e)
    return result


# ---------- 会議の抽出 ----------

def parse_happened_at(s):
    """'Wed Jul 08 2026 09:00:00 GMT+0000 (...)' または ISO 形式を UTC datetime に。"""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    m = re.match(r"\w{3} (\w{3}) (\d{1,2}) (\d{4}) (\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        raise ValueError("happenedAt を解釈できません: %r" % s)
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    mon, day, year, hh, mm, ss = m.groups()
    return datetime(int(year), months[mon], int(day), int(hh), int(mm), int(ss),
                    tzinfo=timezone.utc)


def attended_by_metadata(meeting, member):
    people = list(meeting.get("invitees") or [])
    if meeting.get("organizer"):
        people.append(meeting["organizer"])
    emails = {(p.get("email") or "").lower() for p in people}
    return any(e in emails for e in member["emails"])


def transcript_text_and_speakers(api_key, meeting_id):
    data = tldv_get(api_key, "/meetings/%s/transcript" % meeting_id)
    if not data:
        return "", set()
    segments = data.get("data", data if isinstance(data, list) else [])
    lines, speakers = [], set()
    for seg in segments:
        text = (seg.get("text") or "").strip()
        speaker = (seg.get("speaker") or "?").strip()
        if text:
            lines.append("%s: %s" % (speaker, text))
            speakers.add(speaker)
    return "\n".join(lines), speakers


def attended_by_speakers(speakers, member):
    normalized = {s.replace(" ", "").replace("　", "") for s in speakers}
    return any(n.replace(" ", "").replace("　", "") in normalized for n in member["names"])


# ---------- 議事録生成 ----------

SYSTEM_PROMPT = """あなたはYouTubeコンサルティング会社(弊社)のアシスタントです。
会議の文字起こしから、社内外で共有できる簡潔な議事録本文を日本語で作成してください。
読み手が短時間で「①何が決まったのか ②弊社が何を対応するのか ③貴社に何を対応いただくのか」を
把握できる状態にします。

出力は次の3セクションのみ(この順):

■ 決定事項
- 会議内で合意・確定した内容のみを記載する
- 1項目につき1文でまとめる。背景や議論の経緯は記載しない
- 未確定の内容は記載しない
- 最大10項目程度に絞る

■ 弊社対応
- 弊社が対応するタスクのみ。1タスク1行で「・〇〇を対応する【期限：〇月〇日】」の形式

■ 貴社対応
- 貴社に対応いただくタスクのみ。1タスク1行で「・〇〇をご対応いただく【期限：〇月〇日】」の形式

担当の分類ルール:
- 担当者名は記載せず、必ず「弊社」または「貴社」のどちらかに分類する
- 会議内で、制作・編集・企画・進行管理を行う側を「弊社」とする
- 素材提供・確認・承認・社内調整を行う側を「貴社」とする
- 個人名が出ていても、所属する側に置き換える
- 同じタスクを弊社対応と貴社対応の両方に記載しない
- 担当を判断できない場合は、発言の依頼方向や実際の実行主体から分類する

期限の記載ルール:
- 明確な期限がある場合は、具体的な日付を記載する
- 「次回まで」「撮影前」などの場合は、そのまま記載する
- 期限が会議内で決まっていない場合は「未定」と記載する

整理ルール:
- 「企画・制作方針」「アクションアイテム」「未確定事項」など、項目を細かく分けすぎない
- 未確定事項でも確認や対応が必要なものは、弊社対応または貴社対応に変換する
- 対応内容が似ているタスクは統合する
- 完了条件、背景説明、発言者名は記載しない
- 単なる意見、雑談、検討段階のアイデアは除外する
- 重要度の低い細かな作業は省略する
- 決定事項と対応事項の重複を避ける
- 全体をA4用紙1枚程度の分量に収める
- 簡潔で分かりやすいビジネス文にする

出力形式:
- Markdownは使わない(Chatworkでは装飾されないため)。見出しは「■ 」、箇条書きは「・」を使う
- 文字起こしから確認できた事実だけを書く。推測で数字・日付を補わない
- 社内のみの会議(相手企業がいない場合)は貴社対応を「・なし」とし、社内タスクは弊社対応にまとめる
- 該当する内容がないセクションは「・なし」と書く
- 挨拶や前置きは書かず、いきなり「■ 決定事項」から始める"""


def generate_minutes(meeting_name, transcript):
    body = http_json(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": "Bearer " + OPENAI_API_KEY},
        payload={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": "会議名: %s\n\n文字起こし:\n%s" % (meeting_name, transcript[:80000])},
            ],
        },
    )
    return body["choices"][0]["message"]["content"].strip()


def build_message(meeting, minutes_body, happened_jst):
    return (
        "[info][title]【議事録】%s(%s)[/title]"
        "%s\n\n"
        "▼ 録画・文字起こし\n%s"
        "\n\n※ tldv文字起こしからの自動生成です。クライアント共有前に内容をご確認ください。"
        "[/info]"
    ) % (meeting.get("name", "無題"), happened_jst.strftime("%Y/%m/%d %H:%M"),
         minutes_body, meeting.get("url", ""))


# ---------- state ----------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except OSError:
        state = {"processed": {}}
    state.setdefault("processed", {})
    state.setdefault("failures", {})
    # 旧シングル運用のキー(会議IDのみ)を「kato:<会議ID>」へ移行
    for key in list(state["processed"]):
        if ":" not in key:
            state["processed"]["kato:" + key] = state["processed"].pop(key)
    for key in list(state["failures"]):
        if ":" not in key:
            state["failures"]["kato:" + key] = state["failures"].pop(key)
    return state


def save_state(state):
    processed = state["processed"]
    if len(processed) > MAX_STATE_ENTRIES:
        for key in list(processed)[: len(processed) - MAX_STATE_ENTRIES]:
            del processed[key]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- main ----------

def process_member(member, state, minutes_cache, now, dry_run):
    """1メンバー分の処理。戻り値: (投稿数, スキップ数, 保留数, エラー数)"""
    label = member["label"]
    processed, failures = state["processed"], state["failures"]
    posted = skipped = pending = errors = 0

    meetings = (tldv_get(member["tldv_api_key"], "/meetings", {"limit": 50}) or {}).get(
        "results", [])

    for meeting in meetings:
        mid = meeting["id"]
        key = "%s:%s" % (label, mid)
        name = meeting.get("name", "無題")
        if key in processed:
            continue
        happened = parse_happened_at(meeting["happenedAt"])
        if happened < now - timedelta(hours=LOOKBACK_HOURS):
            continue  # 古い会議は state に載せず単に無視(初回の大量投稿防止)
        ended = happened + timedelta(seconds=meeting.get("duration") or 0)
        if now < ended + timedelta(minutes=SETTLE_MINUTES):
            print("[%s] 保留(終了直後/進行中): %s" % (label, name))
            pending += 1
            continue

        # 1件の失敗(tldv 403等)で残りの会議やstate保存が道連れにならないよう会議単位で隔離
        try:
            time.sleep(3)  # tldv手前のCloudflareレート制限(403)対策: 連発を避ける
            transcript, speakers = transcript_text_and_speakers(member["tldv_api_key"], mid)
            attended = attended_by_metadata(meeting, member) or attended_by_speakers(
                speakers, member)
            if not attended:
                print("[%s] スキップ(不参加): %s" % (label, name))
                processed[key] = {"name": name, "status": "not_attended",
                                  "at": now.isoformat()}
                if not dry_run:
                    save_state(state)
                skipped += 1
                continue
            if not transcript:
                print("[%s] 保留(文字起こし未生成): %s" % (label, name))
                pending += 1
                continue

            print("[%s] 議事録生成中: %s (%d文字)" % (label, name, len(transcript)))
            if mid not in minutes_cache:
                minutes_cache[mid] = generate_minutes(name, transcript)
            message = build_message(meeting, minutes_cache[mid], happened.astimezone(JST))
        except Exception as e:
            failures[key] = failures.get(key, 0) + 1
            if failures[key] >= MAX_ATTEMPTS:
                # 恒常エラー(権限403等)は諦めて記録し、届け先に1回だけ知らせる
                print("[%s] 断念(%d回失敗): %s — %s" % (label, failures[key], name, e))
                processed[key] = {"name": name, "status": "error_gave_up",
                                  "at": now.isoformat(), "error": str(e)[:200]}
                failures.pop(key, None)
                if not dry_run:
                    notice = (
                        "[info][title]⚠ 議事録未作成: %s(%s)[/title]"
                        "tldvからデータを取得できず%d回失敗したため、自動作成を断念しました。\n"
                        "エラー: %s\n\n"
                        "▼ tldv上で直接ご確認ください\n%s[/info]"
                    ) % (name, happened.astimezone(JST).strftime("%Y/%m/%d %H:%M"),
                         MAX_ATTEMPTS, e, meeting.get("url", ""))
                    try:
                        chatwork_post(member, notice)
                    except Exception as e2:
                        print("[%s] 断念通知の投稿にも失敗: %s" % (label, e2))
            else:
                print("[%s] エラー(次回再試行 %d/%d): %s — %s"
                      % (label, failures[key], MAX_ATTEMPTS, name, e))
            if not dry_run:
                save_state(state)
            errors += 1
            continue

        if dry_run:
            print("--- dry-run [%s]: 投稿内容 ---" % label)
            print(message)
            print("---")
        else:
            result = chatwork_post(member, message)
            room, _ = resolve_room(member)
            print("[%s] Chatwork投稿完了: %s → room %s (message_id=%s)"
                  % (label, name, room, result.get("message_id")))
            # 直後にクラッシュしても二重投稿しないよう、1件ごとに即保存する
            processed[key] = {"name": name, "status": "posted", "at": now.isoformat()}
            failures.pop(key, None)
            save_state(state)
        posted += 1

    return posted, skipped, pending, errors


def main():
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc)
    state = load_state()
    members = load_members()
    minutes_cache = {}  # 会議ID→生成済み議事録(複数メンバー参加の会議でGPTを1回に)

    totals = [0, 0, 0, 0]
    for member in members:
        # メンバー単位でも隔離: 1人のキー不備で全員が止まらないようにする
        try:
            result = process_member(member, state, minutes_cache, now, dry_run)
            totals = [a + b for a, b in zip(totals, result)]
        except Exception as e:
            print("[%s] メンバー処理全体が失敗(次回再試行): %s" % (member["label"], e))
            totals[3] += 1

    print("完了: 投稿%d件 / 不参加スキップ%d件 / 保留%d件 / エラー%d件" % tuple(totals))
    # 会議単位のエラーはリトライ/断念通知で自己完結するため、ジョブ自体は失敗にしない


if __name__ == "__main__":
    main()
