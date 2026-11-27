"""Read-only Nagoya badminton availability monitor. No login or booking submission."""
import datetime as dt
import http.client
import http.cookiejar
import json
from browser_fallback import BrowserSession
import urllib.error
import os
import re
import time
import urllib.parse
import urllib.request
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
BASE = "https://www.net.city.nagoya.jp/cgi-bin/sp/"
STATE = Path("seen.json")
FAILED_STATE = Path("failed_dates.json")
CHECKED_STATE = Path("checked_dates_run.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Referer": BASE + "sps04001",
    "Origin": "https://www.net.city.nagoya.jp",
    "Accept-Language": "ja-JP,ja;q=0.9",
}
class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.select = None
        self.fields = {}
        self.text = []
        self.capture = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("name") == "user"
        if self.inside and tag == "input" and a.get("name"):
            if a.get("type") not in ("radio", "checkbox") or "checked" in a:
                self.fields[a["name"]] = a.get("value", "")
        if self.inside and tag == "select":
            self.select = a.get("name")
        if self.inside and tag == "option" and self.select and self.select not in self.fields:
            self.fields[self.select] = a.get("value", "")
        if tag in ("script", "style"):
            self.capture = True
    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        if tag == "select":
            self.select = None
        if tag in ("script", "style"):
            self.capture = False
    def handle_data(self, data):
        value = " ".join(data.split())
        if value and not self.capture:
            self.text.append(unicodedata.normalize("NFKC", value))

def fetch(opener, path, fields=None):
    data = urllib.parse.urlencode(fields, encoding="shift_jis").encode("ascii") if fields else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS)
    with opener.open(req, timeout=8) as response:
        return response.read().decode("shift_jis", errors="replace")

NETWORK_ERRORS = (TimeoutError, urllib.error.URLError, http.client.HTTPException, ConnectionError, OSError)

def fetch_initial_form(opener, jar, attempts=3):
    for attempt in range(attempts):
        try:
            page = Parser()
            page.feed(fetch(opener, "sps04001"))
            if "syumoku" not in page.fields:
                raise RuntimeError("Search form missing")
            return page
        except (*NETWORK_ERRORS, RuntimeError) as exc:
            print(f"Initial form attempt {attempt + 1}/{attempts} failed: {type(exc).__name__}", flush=True)
            if attempt == attempts - 1:
                raise
            jar.clear()
            time.sleep(min(2 ** attempt, 4))

def parse_results(lines, day):
    results = []
    for i in range(len(lines)):
        if lines[i] != "No.":
            continue
        section = lines[i:i+18]
        def after(label):
            try:
                return section[section.index(label) + 1]
            except (ValueError, IndexError):
                return ""
        venue, area, slot = after("施設"), after("地域"), after("供用区分")
        match = re.search(r"(\d{1,2}):(\d{2})\s*[-−ー~～]\s*(\d{1,2}):(\d{2})", slot)
        if venue and area and match:
            start = int(match.group(1))*60 + int(match.group(2))
            end = int(match.group(3))*60 + int(match.group(4))
            if day.weekday() >= 5 or (start <= 18*60 and end >= 21*60):
                results.append({"date": day.isoformat(), "area": area, "venue": venue,
                                "time": f"{match.group(1).zfill(2)}:{match.group(2)}-{match.group(3).zfill(2)}:{match.group(4)}"})
    return results

def valid_results(page):
    """Never treat an unknown response as an empty search result."""
    text = " ".join(page.text)
    if "空き検索結果" not in text or "サービス時間外エラー画面" in text:
        return False
    return "No." in page.text or "空き施設がないか、空き照会可能期間外です" in text or bool(re.search(r"(?:該当|検索結果|空き施設)[^。\\n]{0,35}0\\s*件|0\\s*件[^。\\n]{0,35}(?:該当|検索結果|空き施設)", text)) or any(t in text for t in (
        "該当する施設はありません", "該当データはありません", "検索結果はありません",
        "該当するデータはありません", "該当する空きはありません",
        "該当する施設がありません", "該当するデータがありません",
    ))

def log_unverified(page, day, source):
    # Record only short visible text to diagnose changing empty-result formats.
    excerpt = " ".join(page.text)[:350]
    print(f"Unverified {source} response for {day}", flush=True)

def main():
    now = dt.datetime.now(JST)
    if not (8 <= now.hour < 24):
        print("Outside monitoring hours (JST).")
        return
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    first = fetch_initial_form(opener, jar)
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    found = {}
    browser_session = None
    consecutive_http_failures = 0
    http_cooldown = 0
    invalid_result_dates = []
    verified_dates = set()
    # Check today and the next 29 days (30 dates total).
    horizon = 30
    checked = 0
    failed = 0
    # Keep one browser-like session and one form state for the entire scan.
    # Only change the day; update month only when the scan crosses a month boundary.
    fields = dict(first.fields)
    fields.update(syumoku="024", joken="1", kyoyo1="07", kyoyo2="00",
                  chiiki="20", button="照会", syumokunm="バドミントン",
                  chiikinm="全地域", kyoyo1nm="全供用", kyoyo2nm="−−")
    current_month = None
    window = [now.date() + dt.timedelta(days=delta) for delta in range(horizon)]
    try:
        old_failed = json.loads(FAILED_STATE.read_text()) if FAILED_STATE.exists() else []
    except (ValueError, OSError):
        old_failed = []
    priority = set(old_failed) if isinstance(old_failed, list) else set()
    dates = sorted(window, key=lambda d: (d.isoformat() not in priority, d))
    failed_dates = []
    for day in dates:
        if current_month != (day.year, day.month):
            fields["month"] = day.strftime("%m")
            current_month = (day.year, day.month)
        fields["day"] = day.strftime("%d")
        fields["kyoyo1"] = "05" if day.weekday() < 5 else "07"
        fields["kyoyo1nm"] = "夜間" if day.weekday() < 5 else "全供用"
        page = None
        for attempt in range(1 if http_cooldown else 2):
            if http_cooldown:
                break
            try:
                candidate = Parser()
                candidate.feed(fetch(opener, "sps04002", fields))
                if "サービス時間外エラー画面" in candidate.text or "spsConnectionErr.html" in candidate.text:
                    raise RuntimeError("Site service error")
                if not valid_results(candidate):
                    log_unverified(candidate, day, "HTTP")
                    raise RuntimeError("Unverified results page")
                page = candidate
                consecutive_http_failures = 0
                http_cooldown = 0
                break
            except (*NETWORK_ERRORS, RuntimeError) as exc:
                print(f"HTTP query attempt {attempt + 1}/2 failed for {day}: {type(exc).__name__}", flush=True)
                consecutive_http_failures += 1
                if consecutive_http_failures >= 3:
                    http_cooldown = 3
                if attempt == 0 and not http_cooldown:
                    time.sleep(1)
                    jar.clear()
                    try:
                        refreshed = Parser()
                        refreshed.feed(fetch(opener, "sps04001"))
                        if "syumoku" not in refreshed.fields:
                            raise RuntimeError("Search form missing on retry")
                        # Preserve the requested date and filters after refreshing form tokens.
                        fields.update(refreshed.fields)
                        fields.update(month=day.strftime("%m"), day=day.strftime("%d"),
                                      syumoku="024", joken="1", chiiki="20",
                                      kyoyo1="05" if day.weekday() < 5 else "07",
                                      kyoyo2="00", button="照会",
                                      syumokunm="バドミントン", chiikinm="全地域",
                                      kyoyo1nm="夜間" if day.weekday() < 5 else "全供用",
                                      kyoyo2nm="−−")
                    except Exception as refresh_error:
                        print(f"HTTP session refresh failed: {type(refresh_error).__name__}", flush=True)
        if page is None:
            print(f"Starting browser recovery for {day}", flush=True)
            try:
                if browser_session is None:
                    browser_session = BrowserSession()
                html = browser_session.query(day, fields)
                candidate = Parser()
                candidate.feed(html)
                if not valid_results(candidate):
                    log_unverified(candidate, day, "browser")
                    raise RuntimeError("Browser returned unverified results")
                page = candidate
                print(f"Browser recovered query for {day}", flush=True)
                # Browser session is separate from urllib session. Restart the
                # HTTP form session before querying subsequent dates.
                jar.clear()
                refreshed = fetch_initial_form(opener, jar)
                if "syumoku" in refreshed.fields:
                    fields = dict(refreshed.fields)
                    fields.update(syumoku="024", joken="1", kyoyo1="07",
                                  kyoyo2="00", chiiki="20", button="照会",
                                  syumokunm="バドミントン", chiikinm="全地域",
                                  kyoyo1nm="全供用", kyoyo2nm="−−",
                                  month=day.strftime("%m"), day=day.strftime("%d"))
                    current_month = (day.year, day.month)
            except Exception as recovery_error:
                print(f"Browser recovery failed for {day}: {type(recovery_error).__name__}: {str(recovery_error)[:160]}", flush=True)
                if browser_session is not None:
                    browser_session.close()
                    browser_session = None
        if http_cooldown:
            http_cooldown -= 1
        if page is None:
            failed += 1
            failed_dates.append(day.isoformat())
            print(f"Skipped {day}; not marked empty", flush=True)
            continue
        if not valid_results(page):
            print(f"Warning: unverified empty results format for {day}; treating as failed", flush=True)
            failed += 1
            failed_dates.append(day.isoformat())
            invalid_result_dates.append(day.isoformat())
            continue
        rows = parse_results(page.text, day)
        for row in rows:
            key = "|".join(row.values())
            found[key] = row
        checked += 1
        verified_dates.add(day.isoformat())
        print(day, "matching slots:", len(rows), flush=True)
        time.sleep(0.2)
    # Give dates that failed during the main scan one final attempt after all dates.
    # Do not count a failed date as checked unless its results page is validated.
    if failed_dates:
        print(f"Final retry for {len(failed_dates)} failed dates", flush=True)
        remaining = []
        for date_text in failed_dates:
            day = dt.date.fromisoformat(date_text)
            retry_fields = dict(fields)
            retry_fields.update(
                month=day.strftime("%m"), day=day.strftime("%d"),
                kyoyo1="05" if day.weekday() < 5 else "07",
                kyoyo1nm="夜間" if day.weekday() < 5 else "全供用"
            )
            page = None
            try:
                jar.clear()
                fresh = Parser()
                fresh.feed(fetch(opener, "sps04001"))
                if "syumoku" not in fresh.fields:
                    raise RuntimeError("Search form missing on final retry")
                retry_fields.update(fresh.fields)
                retry_fields.update(
                    syumoku="024", joken="1", kyoyo1="05" if day.weekday() < 5 else "07",
                    kyoyo2="00", chiiki="20", button="照会",
                    syumokunm="バドミントン", chiikinm="全地域",
                    kyoyo1nm="夜間" if day.weekday() < 5 else "全供用",
                    kyoyo2nm="−−", month=day.strftime("%m"), day=day.strftime("%d")
                )
                candidate = Parser()
                candidate.feed(fetch(opener, "sps04002", retry_fields))
                if not valid_results(candidate):
                    log_unverified(candidate, day, "final HTTP")
                    raise RuntimeError("Unverified results page on final retry")
                page = candidate
                print(f"Final HTTP retry succeeded for {day}", flush=True)
            except (*NETWORK_ERRORS, RuntimeError) as exc:
                print(f"Final HTTP retry failed for {day}: {type(exc).__name__}", flush=True)
                try:
                    candidate = Parser()
                    if browser_session is None:
                        browser_session = BrowserSession()
                    candidate.feed(browser_session.query(day, retry_fields))
                    if not valid_results(candidate):
                        log_unverified(candidate, day, "final browser")
                        raise RuntimeError("Unverified browser results on final retry")
                    page = candidate
                    print(f"Final browser retry succeeded for {day}", flush=True)
                except Exception as recovery_error:
                    print(f"Final browser retry failed for {day}: {type(recovery_error).__name__}", flush=True)
                    if browser_session is not None:
                        browser_session.close()
                        browser_session = None
            if page is None:
                remaining.append(date_text)
                continue
            rows = parse_results(page.text, day)
            for row in rows:
                found["|".join(row.values())] = row
            checked += 1
            verified_dates.add(day.isoformat())
            failed -= 1
            print(f"Final retry {day} matching slots: {len(rows)}", flush=True)
        failed_dates = remaining
    if browser_session is not None:
        browser_session.close()
    FAILED_STATE.write_text(json.dumps(failed_dates, ensure_ascii=False, indent=2) + "\n")
    CHECKED_STATE.write_text(json.dumps(sorted(verified_dates), ensure_ascii=False, indent=2) + "\n")
    print(f"Priority backlog for next run: {len(failed_dates)} dates", flush=True)
    new = {k:v for k,v in found.items() if k not in previous}
    removed = {k:v for k,v in previous.items() if v.get("date") in verified_dates and k not in found}
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    discord_user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    mention = f"<@{discord_user_id}>" if discord_user_id.isdigit() else ""
    def send_discord(message, label):
        if not webhook:
            print(f"Discord webhook missing; {label} not sent", flush=True)
            return False
        payload_data = message if isinstance(message, dict) else {"content": message}
        payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook.strip(), data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                print(f"Discord {label} status: {response.status}", flush=True)
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            detail = exc.read(250).decode("utf-8", errors="replace")
            print(f"Discord {label} rejected: HTTP {exc.code}; response: {detail}", flush=True)
        except NETWORK_ERRORS as exc:
            print(f"Discord {label} network error: {type(exc).__name__}", flush=True)
        return False

    booking_url = "https://www.net.city.nagoya.jp/cgi-bin/sp/sps04001"
    def compact_venue(v):
        venue = v["venue"].replace("第1競技場", "①").replace("第2競技場", "②").replace("第１競技場", "①").replace("第２競技場", "②").replace("プール体育室", "").replace("競技場", "").replace("SC", "").strip()
        return venue

    current_slots = sorted(found.values(), key=lambda v: (v["date"], v["time"], v["area"], v["venue"]))
    groups = {}
    for v in current_slots:
        hours = re.sub(r":00\b", "", v["time"]).replace("～", "-").replace("〜", "-")
        groups.setdefault(v["date"], {}).setdefault(hours, []).append(("🆕 " if "|".join(v.values()) in new else "") + compact_venue(v))
    batches = []
    for date_text, times in groups.items():
        day = dt.date.fromisoformat(date_text)
        heading = f"**📅 {day:%m/%d}（{'一二三四五六日'[day.weekday()]}）**"
        for hours, venues in times.items():
            for offset in range(0, len(venues), 8):
                line = f"• {hours}　" + "、".join(venues[offset:offset+8])
                if not batches or len(batches[-1]) + len(heading) + len(line) + 3 > 1500:
                    batches.append("")
                if not batches[-1] or not batches[-1].endswith("\n") and (hours == next(iter(times)) and offset == 0):
                    batches[-1] += ("\n\n" if batches[-1] else "") + heading
                batches[-1] += "\n" + line
    batches = batches or [""]
    color = 0xE6A23C if failed else (0xF59E0B if found else 0x2ECC71)
    summary = f"成功 **{checked}/{horizon}** ｜失败 **{failed}** ｜空场 **{len(found)}** ｜新增 **{len(new)}** ｜消失 **{len(removed)}**"
    if removed:
        gone = sorted(removed.values(), key=lambda v: (v["date"], v["time"], v["venue"]))
        summary += "\n➖ 已不显示：" + "；".join(f"{v['date'][5:]} {v['time']} {compact_venue(v)}" for v in gone[:5])
        if len(gone) > 5:
            summary += f" 等{len(gone)}个"
    if failed_dates:
        summary += "\n⚠️ 待补查：" + "、".join(d[5:].replace("-", "/") for d in failed_dates)
    report_sent = True
    for index, batch in enumerate(batches):
        details = "\n\n" + batch if batch else "\n暂无已确认空场"
        report = {
            "content": f"{mention} 🏸 监控完成" if index == 0 else "",
            "embeds": [{
                "title": "🏸 名古屋羽毛球监控" + (f" ({index+1}/{len(batches)})" if len(batches)>1 else ""),
                "description": (summary if index == 0 else "") + details,
                "color": color,
                "url": booking_url,
                "footer": {"text": f"{now.strftime('%m/%d %H:%M')} JST · ①第一 / ②第二竞技场 · 请自行确认预约"}
            }],
            "allowed_mentions": {"users": [discord_user_id]} if index == 0 and discord_user_id.isdigit() else {"parse": []}
        }
        if not send_discord(report, "monitoring report"):
            report_sent = False
    notified = dict(new) if report_sent else {}
    if not report_sent:
        print("WARNING: Discord monitoring report was not delivered", flush=True)
    # Only mark notified slots as seen; retain history for 65 days.
    if webhook and report_sent:
        previous = {k:v for k,v in previous.items() if v.get("date") not in verified_dates}
        previous.update(found)
        cutoff = (now.date() - dt.timedelta(days=1)).isoformat()
        previous = {k:v for k,v in previous.items() if v.get("date", "") >= cutoff}
        STATE.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
        Path("snapshot_ready_run.json").write_text("true\n")
    print(f"Date coverage: {checked}/{horizon} checked, {failed} failed; new matching slots: {len(new)}; booking disabled", flush=True)
    if webhook and not report_sent:
        raise RuntimeError("Discord monitoring report delivery failed")
    if checked == 0:
        raise RuntimeError("All monitored dates failed; marking workflow unsuccessful")

if __name__ == "__main__":
    main()
