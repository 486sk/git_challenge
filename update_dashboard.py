"""
1일 1커밋 챌린지 대시보드 생성기 (v3)

v2 대비 변경 사항:
  1. 스트릭 로직 재설계: '어제까지 확정된 기준값(base)' + '오늘의 라이브 값'을 분리한다.
     날짜가 바뀌면 지난 날짜의 전체 범위(00:00~23:59:59 KST)를 다시 조회해 최종값으로 확정하므로
     자정 직후 실행이 스트릭을 0으로 리셋하거나, 그날 마지막 실행 이후의 커밋이 누락되는 문제가 없다.
     워크플로가 며칠 멈췄던 경우에도 최대 BACKFILL_MAX_DAYS일까지 소급 확정한다.
  2. '최장 연속 잔디' 기록(max_grass)을 실제로 추적하고, 현재 스트릭과 별도로 표시한다.
  3. last_success에 날짜를 저장하고, 오늘 날짜의 값만 폴백에 사용한다.
  4. 1등 판정은 조회 실패 멤버를 제외한 멤버 중 최다 커밋 기준이다. 동점이면 공동 1등(모두 연속 1등 인정).
     순위표는 커밋 수가 같으면 같은 순위를 부여한다.
  5. REST 상세 조회 상한을 레포별/전체로 분리하고, 상한·권한 문제로 일부만 집계된 경우 (일부)로 표시한다.
  6. 조회 기준 시각을 한 번만 계산해 모든 멤버가 같은 날짜 범위로 조회된다.
  7. restrictedContributionsCount(비공개 기여 전체: 이슈/PR 포함)는 기본적으로 합산하지 않는다.
     비공개 레포 커밋을 포함하고 싶으면 INCLUDE_RESTRICTED = True 로 바꾼다.
  8. 상태 파일은 README 갱신이 성공한 뒤에만 저장한다 (중간 실패 시 다음 실행에서 재시도).

워크플로 참고:
  - dashboard_state.json도 README.md와 함께 커밋해야 스트릭이 유지된다.
  - 실행이 겹쳐 상태 파일이 꼬이지 않도록 workflow에 concurrency 그룹을 지정한다.
  - v2 이하의 dashboard_state.json은 구조가 달라 멤버별로 자동 초기화된다.
"""

import os
import re
import sys
import json
from datetime import datetime, date, time, timedelta
import pytz
import requests

# ==========================================
# 1. 설정
# ==========================================
MEMBERS = [
    {"name": "김효주", "username": "oojoyhh"},
    {"name": "권예리", "username": "Yelli915"},
    {"name": "인수연", "username": "1nyeonart"},
    {"name": "박유진", "username": "youjin09222"},
    {"name": "한석휘", "username": "Smorgg"},
    {"name": "배민혁", "username": "bmh7190"},
]

GITHUB_TOKEN = os.getenv("GH_TOKEN")
GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"
STATE_FILE = "dashboard_state.json"
README_FILE = "README.md"
TZ = pytz.timezone("Asia/Seoul")

INCLUDE_RESTRICTED = False   # 비공개 기여 수 합산 여부 (이슈/PR 등도 섞여 있음에 주의)
MAX_COMMITS_PER_REPO = 30    # 레포별 상세(stats) 조회 상한
MAX_COMMITS_TOTAL = 60       # 멤버 1인당 전체 상세 조회 상한
BACKFILL_MAX_DAYS = 7        # 워크플로가 멈췄을 때 소급 확정할 최대 일수
HOT_THRESHOLD = 5            # 🔥 표시 기준 커밋 수

REST_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
GRAPHQL_HEADERS = {"Authorization": f"bearer {GITHUB_TOKEN}"}


class FinalizeError(RuntimeError):
    """지난 날짜 확정에 필요한 데이터를 얻지 못함 — 상태를 저장하지 않고 다음 실행에서 재시도."""


def day_range(d):
    """KST 기준 하루의 시작/끝 ISO 문자열."""
    start = TZ.localize(datetime.combine(d, time.min))
    end = TZ.localize(datetime.combine(d, time.max))
    return start.isoformat(), end.isoformat()


# ==========================================
# 2. 상태(이력) 로드/저장
# ==========================================
def new_member_state():
    """
    grass_base  : 어제까지 확정된 연속 잔디 일수
    top_base    : 어제까지 확정된 연속 1등 일수
    max_grass   : 확정된 최장 연속 잔디 일수
    day         : day_commits/day_top이 가리키는 날짜 (오늘)
    day_commits : 오늘 마지막으로 성공한 조회의 커밋 수
    day_top     : 오늘 마지막으로 성공한 조회에서 1등(공동 포함)이었는지
    last_success: API 실패 시 폴백용 마지막 성공 데이터 (date 포함)
    """
    return {
        "grass_base": 0,
        "top_base": 0,
        "max_grass": 0,
        "day": None,
        "day_commits": 0,
        "day_top": False,
        "last_success": None,
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_run_date": None, "members": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] 상태 파일 로드 실패, 새로 시작합니다: {e}")
        return {"last_run_date": None, "members": {}}
    state.setdefault("last_run_date", None)
    state.setdefault("members", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_member_state(state, username):
    members = state.setdefault("members", {})
    m = members.get(username)
    if not isinstance(m, dict) or "grass_base" not in m:
        # v2 이하 구조이거나 신규 멤버 → 초기화
        m = new_member_state()
        members[username] = m
    else:
        for k, v in new_member_state().items():
            m.setdefault(k, v)
    return m


# ==========================================
# 3. GitHub GraphQL: 커밋 수 / 레포 목록 조회
# ==========================================
def fetch_member_contributions(username, start_iso, end_iso):
    """
    주어진 범위의 커밋 수와 커밋한 레포 목록을 반환.
    조회 실패 시 None (0커밋과 명확히 구분).
    """
    query = """
    query($username: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $username) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
          commitContributionsByRepository(maxRepositories: 20) {
            repository { name owner { login } }
            contributions(first: 1) { totalCount }
          }
        }
      }
    }
    """
    variables = {"username": username, "from": start_iso, "to": end_iso}

    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=GRAPHQL_HEADERS,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[ERROR] {username} GraphQL 요청 실패: {e}")
        return None

    if resp.status_code != 200:
        print(f"[ERROR] {username} GraphQL 상태코드 {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[ERROR] {username} GraphQL 응답 파싱 실패")
        return None

    if "errors" in data:
        print(f"[ERROR] {username} GraphQL 에러: {data['errors']}")
        return None

    user_node = (data.get("data") or {}).get("user")
    if not user_node:
        print(f"[ERROR] {username} 유저 정보를 찾을 수 없음")
        return None

    cc = user_node["contributionsCollection"]
    commits = cc["totalCommitContributions"]
    if INCLUDE_RESTRICTED:
        commits += cc["restrictedContributionsCount"]

    repos = [
        {"owner": r["repository"]["owner"]["login"], "repo": r["repository"]["name"]}
        for r in cc["commitContributionsByRepository"]
        if r["contributions"]["totalCount"] > 0
    ]

    return {"commits": commits, "repos": repos}


# ==========================================
# 4. GitHub REST: 실제 additions/deletions 조회
# ==========================================
def fetch_real_stats(username, repos, since_iso, until_iso):
    """
    오늘 커밋한 레포들을 REST API로 조회해 실제 additions/deletions를 합산.
    레포별 MAX_COMMITS_PER_REPO, 전체 MAX_COMMITS_TOTAL개까지만 상세 조회한다.
    상한 도달, 권한 없음, 요청 실패 등으로 빠진 커밋이 있으면 complete=False.
    """
    total_add, total_del = 0, 0
    inspected = 0
    complete = True

    for r in repos:
        if inspected >= MAX_COMMITS_TOTAL:
            complete = False
            break

        owner, repo = r["owner"], r["repo"]
        list_url = f"{REST_BASE}/repos/{owner}/{repo}/commits"
        params = {
            "author": username,
            "since": since_iso,
            "until": until_iso,
            "per_page": 100,
        }
        try:
            resp = requests.get(list_url, headers=REST_HEADERS, params=params, timeout=15)
        except requests.RequestException:
            complete = False
            continue
        if resp.status_code != 200:
            # 비공개 레포 접근 불가 등 — 이 레포만 건너뜀
            complete = False
            continue

        commits = resp.json()
        if len(commits) >= 100:
            complete = False  # 페이지네이션 미처리분 존재 가능

        repo_inspected = 0
        for commit in commits:
            if repo_inspected >= MAX_COMMITS_PER_REPO or inspected >= MAX_COMMITS_TOTAL:
                complete = False
                break
            sha = commit.get("sha")
            if not sha:
                continue
            detail_url = f"{REST_BASE}/repos/{owner}/{repo}/commits/{sha}"
            try:
                detail_resp = requests.get(detail_url, headers=REST_HEADERS, timeout=15)
            except requests.RequestException:
                complete = False
                continue
            if detail_resp.status_code != 200:
                complete = False
                continue
            stats = detail_resp.json().get("stats", {})
            total_add += stats.get("additions", 0)
            total_del += stats.get("deletions", 0)
            inspected += 1
            repo_inspected += 1

    return {
        "additions": total_add,
        "deletions": total_del,
        "inspected": inspected,
        "complete": complete,
    }


def fetch_member_stats(username, start_iso, end_iso):
    """
    반환값:
      {"commits", "additions", "deletions", "is_estimate", "is_partial", "fetch_failed"}
    fetch_failed=True 면 GraphQL 조회 자체가 실패한 것 (0커밋과 구분됨).
    """
    empty = {
        "commits": 0, "additions": 0, "deletions": 0,
        "is_estimate": False, "is_partial": False, "fetch_failed": False,
    }

    contrib = fetch_member_contributions(username, start_iso, end_iso)
    if contrib is None:
        return {**empty, "fetch_failed": True}

    commits = contrib["commits"]
    if commits == 0:
        return empty

    real = fetch_real_stats(username, contrib["repos"], start_iso, end_iso)

    if real["inspected"] > 0:
        # 이메일 미연결 커밋 등으로 GraphQL 커밋 수보다 적게 잡힌 경우도 '일부'로 본다
        partial = (not real["complete"]) or real["inspected"] < commits
        return {
            **empty,
            "commits": commits,
            "additions": real["additions"],
            "deletions": real["deletions"],
            "is_partial": partial,
        }

    print(f"[WARN] {username} 실제 stats 조회 실패, 추정치로 대체")
    return {
        **empty,
        "commits": commits,
        "additions": commits * 25,
        "deletions": commits * 5,
        "is_estimate": True,
    }


def generate_progress_bar(commits, max_commits):
    if max_commits == 0 or commits == 0:
        return "`░░░░░░░░░░░░░░░░░░░░`"
    ratio = min(commits / max_commits, 1.0)
    filled = int(ratio * 20)
    return f"`{'█' * filled}{'░' * (20 - filled)}`"


# ==========================================
# 5. 스트릭: 지난 날짜 확정
# ==========================================
def finalize_day(state, d):
    """
    날짜 d의 전체 범위를 다시 조회해 최종 커밋 수로 base 스트릭을 확정한다.
    재조회 실패 시 그날 마지막으로 기록된 값으로 대체하고, 그것도 없으면 FinalizeError.
    """
    d_str = d.isoformat()
    start_iso, end_iso = day_range(d)
    counts = {}

    for mem in MEMBERS:
        uname = mem["username"]
        ms = get_member_state(state, uname)
        contrib = fetch_member_contributions(uname, start_iso, end_iso)
        if contrib is not None:
            counts[uname] = contrib["commits"]
        elif ms["day"] == d_str:
            print(f"[WARN] {uname} {d_str} 재조회 실패 → 그날 마지막 기록값({ms['day_commits']}) 사용")
            counts[uname] = ms["day_commits"]
        else:
            raise FinalizeError(
                f"{uname}의 {d_str} 데이터를 조회하지 못했고 기록값도 없습니다. "
                f"(계정명 변경 여부를 확인하세요)"
            )

    best = max(counts.values(), default=0)
    for uname, c in counts.items():
        ms = get_member_state(state, uname)
        ms["grass_base"] = ms["grass_base"] + 1 if c > 0 else 0
        ms["top_base"] = ms["top_base"] + 1 if (best > 0 and c == best) else 0
        ms["max_grass"] = max(ms["max_grass"], ms["grass_base"])
        ms["day"] = None
        ms["day_commits"] = 0
        ms["day_top"] = False

    summary = ", ".join(f"{u}={c}" for u, c in counts.items())
    print(f"[INFO] {d_str} 확정: {summary}")


def finalize_past_days(state, today):
    """마지막 실행일부터 어제까지의 날짜를 순서대로 확정한다."""
    last = state.get("last_run_date")
    if not last:
        return
    last_day = date.fromisoformat(last)
    if last_day >= today:
        return

    start_day = last_day
    if (today - last_day).days > BACKFILL_MAX_DAYS:
        print(f"[WARN] 마지막 실행({last})이 {BACKFILL_MAX_DAYS}일 이상 전입니다. "
              f"스트릭을 초기화하고 최근 {BACKFILL_MAX_DAYS}일만 소급 확정합니다.")
        for mem in MEMBERS:
            ms = get_member_state(state, mem["username"])
            ms["grass_base"] = 0
            ms["top_base"] = 0
            ms["day"] = None
            ms["day_commits"] = 0
            ms["day_top"] = False
        start_day = today - timedelta(days=BACKFILL_MAX_DAYS)

    d = start_day
    while d < today:
        finalize_day(state, d)
        d += timedelta(days=1)


# ==========================================
# 6. 스트릭: 오늘 라이브 값 반영
# ==========================================
def update_today(state, stats, today_str, top_usernames):
    """
    오늘의 라이브 값을 상태에 기록하고, 표시용 스트릭을 반환한다.
    표시값 = 확정 base + (오늘 조건 충족 시 1)
    """
    result = {}
    for user in stats:
        uname = user["username"]
        ms = get_member_state(state, uname)

        if ms["day"] != today_str:
            ms["day"] = today_str
            ms["day_commits"] = 0
            ms["day_top"] = False

        if not user["fetch_failed"]:
            ms["day_commits"] = user["commits"]
            ms["day_top"] = uname in top_usernames
            ms["last_success"] = {
                "date": today_str,
                "commits": user["commits"],
                "additions": user["additions"],
                "deletions": user["deletions"],
                "is_estimate": user["is_estimate"],
                "is_partial": user["is_partial"],
            }

        grass = ms["grass_base"] + (1 if ms["day_commits"] > 0 else 0)
        top = ms["top_base"] + (1 if ms["day_top"] else 0)
        result[uname] = {
            "top_streak": top,
            "grass_streak": grass,
            "max_grass": max(ms["max_grass"], grass),
        }

    state["last_run_date"] = today_str
    return result


# ==========================================
# 7. 순위 유틸
# ==========================================
def rank_label(rank):
    medals = {1: "🥇 1st", 2: "🥈 2nd", 3: "🥉 3rd"}
    if rank in medals:
        return medals[rank]
    if 10 <= rank % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return f"{rank}{suffix}"


def assign_ranks(stats):
    """커밋 수가 같으면 같은 순위 (1, 2, 2, 4 ...)."""
    prev = None
    rank = 0
    for i, u in enumerate(stats):
        if u["commits"] != prev:
            rank = i + 1
            prev = u["commits"]
        u["rank"] = rank


def user_link(username):
    return f"[@{username}](https://github.com/{username})"


# ==========================================
# 8. README 생성
# ==========================================
def update_readme():
    if not GITHUB_TOKEN:
        print("[FATAL] GH_TOKEN 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    now = datetime.now(TZ)
    today = now.date()
    today_str = today.isoformat()
    now_str = now.strftime("%Y--%m--%d_%H:%M_KST")
    start_iso, end_iso = day_range(today)

    state = load_state()

    # --- 지난 날짜 확정 (실패 시 상태 저장 없이 종료 → 다음 실행에서 재시도) ---
    try:
        finalize_past_days(state, today)
    except FinalizeError as e:
        print(f"[FATAL] 지난 날짜 확정 실패: {e}")
        sys.exit(1)

    # --- 오늘 데이터 조회 ---
    stats = []
    any_failed = False
    for m in MEMBERS:
        uname = m["username"]
        data = fetch_member_stats(uname, start_iso, end_iso)

        if data["fetch_failed"]:
            any_failed = True
            # 정렬/순위 산정 전에 폴백 적용. 오늘 날짜의 성공값만 사용한다.
            fallback = get_member_state(state, uname).get("last_success")
            if fallback and fallback.get("date") == today_str:
                data["commits"] = fallback["commits"]
                data["additions"] = fallback["additions"]
                data["deletions"] = fallback["deletions"]
                data["is_estimate"] = fallback.get("is_estimate", False)
                data["is_partial"] = fallback.get("is_partial", False)

        stats.append({"name": m["name"], "username": uname, **data})

    # --- 정렬 / 순위 / 1등 ---
    stats.sort(key=lambda u: (-u["commits"], -(u["additions"] + u["deletions"]), u["username"].lower()))
    assign_ranks(stats)
    max_commits = max((u["commits"] for u in stats), default=0)

    valid = [u for u in stats if not u["fetch_failed"]]
    best = max((u["commits"] for u in valid), default=0)
    top_users = [u for u in valid if best > 0 and u["commits"] == best]
    top_usernames = {u["username"] for u in top_users}

    streaks = update_today(state, stats, today_str, top_usernames)

    # --- README 읽기 ---
    with open(README_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # 1) LAST_UPDATE 뱃지
    badge_pattern = r"LAST_UPDATE-[0-9]{4}--[0-9]{2}--[0-9]{2}_[0-9]{2}:[0-9]{2}_KST-success"
    content, n_badge = re.subn(badge_pattern, f"LAST_UPDATE-{now_str}-success", content)
    if n_badge == 0:
        print("[WARN] LAST_UPDATE 뱃지 패턴을 찾지 못해 갱신하지 못했습니다.")

    # 2) RANKING 영역
    ranking_md = "<!-- RANKING:START -->\n## 🥇 TODAY'S HIGHLIGHTS\n\n"
    if len(top_users) > 1:
        names = " · ".join(f"👑 **{user_link(u['username'])}**" for u in top_users)
        ranking_md += (
            f"| 🏆 오늘의 공동 커밋 왕 (TOP CONTRIBUTORS) |\n| :--- |\n"
            f"| {names} · **{best} Commits** |\n\n"
        )
    elif top_users:
        u = top_users[0]
        ranking_md += (
            f"| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n"
            f"| 👑 **{user_link(u['username'])}** · **{u['commits']} Commits** |\n\n"
        )
    else:
        ranking_md += (
            "| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n"
            "| 🌿 아직 오늘의 첫 잔디를 기다리고 있습니다! |\n\n"
        )

    if any_failed:
        ranking_md += (
            "> ⚠️ 일부 멤버의 데이터를 가져오지 못했습니다. "
            "오늘 성공한 기록이 있으면 그 값을, 없으면 0을 표시합니다.\n\n"
        )

    ranking_md += "<br>\n\n### 📈 오늘의 순위표 (매시간 갱신)\n\n"
    ranking_md += "| 순위 | 상태 | 멤버 | 커밋 수 | 코드 변화량 (+/-) | 달성도 |\n"
    ranking_md += "| :---: | :---: | :--- | :---: | :---: | :--- |\n"

    for user in stats:
        if user["fetch_failed"]:
            status = "⚠️"
        elif user["commits"] >= HOT_THRESHOLD:
            status = "🔥"
        elif user["commits"] > 0:
            status = "🌿"
        else:
            status = "🌑"

        if user["is_estimate"]:
            mark = " <sup>(추정)</sup>"
        elif user["is_partial"]:
            mark = " <sup>(일부)</sup>"
        else:
            mark = ""

        ranking_md += (
            f"| **{rank_label(user['rank'])}** | {status} | **{user_link(user['username'])}** "
            f"({user['name']}) | `{user['commits']}개` | "
            f"`+{user['additions']}` / `-{user['deletions']}`{mark} | "
            f"{generate_progress_bar(user['commits'], max_commits)} |\n"
        )
    ranking_md += "<!-- RANKING:END -->"

    # 3) RECORD 영역
    record_md = "<!-- RECORD:START -->\n## 🏅 명예의 전당 (RECORDS)\n\n"
    record_md += "| 멤버 | 🔥 연속 1등 | 🌿 현재 연속 잔디 | 🏆 최장 연속 잔디 |\n"
    record_md += "| :--- | :---: | :---: | :---: |\n"
    for user in stats:
        s = streaks[user["username"]]
        top_str = f"🔥 {s['top_streak']}일" if s["top_streak"] > 0 else "-"
        grass_str = f"🌿 {s['grass_streak']}일" if s["grass_streak"] > 0 else "-"
        max_str = f"🏆 {s['max_grass']}일" if s["max_grass"] > 0 else "-"
        record_md += (
            f"| **{user_link(user['username'])}** | "
            f"`{top_str}` | `{grass_str}` | `{max_str}` |\n"
        )
    record_md += "<!-- RECORD:END -->"

    # 4) 치환 (실패 시 상태도 저장하지 않고 종료)
    content, n_rank = re.subn(
        r"<!-- RANKING:START -->.*?<!-- RANKING:END -->",
        lambda _: ranking_md, content, flags=re.DOTALL,
    )
    content, n_record = re.subn(
        r"<!-- RECORD:START -->.*?<!-- RECORD:END -->",
        lambda _: record_md, content, flags=re.DOTALL,
    )

    if n_rank == 0 or n_record == 0:
        print("[FATAL] README의 RANKING 또는 RECORD 마커를 찾지 못해 치환하지 못했습니다. "
              "마커가 삭제/변형되었는지 확인하세요.")
        sys.exit(1)

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    save_state(state)

    print(f"[OK] README 갱신 완료 (실패 멤버: {'있음' if any_failed else '없음'})")


if __name__ == "__main__":
    update_readme()
