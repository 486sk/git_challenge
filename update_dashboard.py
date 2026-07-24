"""
1일 1커밋 챌린지 대시보드 생성기 (개선판)

이전 버전 대비 변경 사항:
  1. 연속 1등 / 최장 연속 스트릭을 실제로 추적한다 (dashboard_state.json에 이력 저장).
  2. 코드 변화량(+/-)을 커밋 수 기반 가상 추산이 아니라 실제 REST API 커밋 stats로 계산한다.
     REST 조회가 실패하면 추정치로 폴백하되, README에 "추정치"임을 명시한다.
  3. API 실패(0커밧으로 조용히 덮어쓰기)와 "진짜 0커밋"을 구분한다. 실패한 멤버는 상태(⚠️)로 표시하고
     직전 성공 데이터가 있으면 그 값을 유지한다.
  4. 마커 치환이 실제로 일어났는지 확인(assert)하고, 실패 시 CI가 명확히 실패하도록 한다.
  5. GraphQL 응답에서 쓰지 않던 commitContributionsByRepository를 실제로 활용해
     레포별 커밋 stats를 REST로 조회하는 데 사용한다.
"""

import os
import re
import sys
import json
from datetime import datetime
import pytz
import requests

# ==========================================
# 1. 설정 (멤버 목록 설정)
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

REST_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
GRAPHQL_HEADERS = {"Authorization": f"bearer {GITHUB_TOKEN}"}


# ==========================================
# 2. 상태(이력) 로드/저장 — 스트릭 추적용
# ==========================================
def load_state():
    """
    상태 파일 구조:
    {
      "last_run_date": "2026-07-24",
      "members": {
        "Yelli915": {
          "top_streak": 2,          # 연속으로 1등한 일수
          "grass_streak": 5,        # 연속으로 커밋(>0) 한 일수
          "last_success": {...}     # API 실패 시 폴백용 마지막 성공 데이터
        },
        ...
      }
    }
    """
    if not os.path.exists(STATE_FILE):
        return {"last_run_date": None, "members": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] 상태 파일 로드 실패, 새로 시작합니다: {e}")
        return {"last_run_date": None, "members": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==========================================
# 3. GitHub GraphQL: 오늘의 커밋/레포 목록 조회
# ==========================================
def fetch_member_contributions(username):
    """오늘의 커밋 수와, 어느 레포에 커밋했는지(레포명 목록)를 함께 반환."""
    tz = pytz.timezone("Asia/Seoul")
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

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
    variables = {"username": username, "from": today_start, "to": today_end}

    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=GRAPHQL_HEADERS,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[ERROR] {username} GraphQL 요청 실패: {e}")
        return None  # None = 조회 실패 (0커밋과 명확히 구분)

    if resp.status_code != 200:
        print(f"[ERROR] {username} GraphQL 상태코드 {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json()
    if "errors" in data:
        print(f"[ERROR] {username} GraphQL 에러: {data['errors']}")
        return None

    user_node = data.get("data", {}).get("user")
    if not user_node:
        print(f"[ERROR] {username} 유저 정보를 찾을 수 없음")
        return None

    cc = user_node["contributionsCollection"]
    commits = cc["totalCommitContributions"] + cc["restrictedContributionsCount"]

    repos = [
        {"owner": r["repository"]["owner"]["login"], "repo": r["repository"]["name"]}
        for r in cc["commitContributionsByRepository"]
        if r["contributions"]["totalCount"] > 0
    ]

    return {
        "commits": commits,
        "repos": repos,
        "today_start": today_start,
        "today_end": today_end,
    }


# ==========================================
# 4. GitHub REST: 실제 additions/deletions 조회
# ==========================================
def fetch_real_stats(username, repos, since_iso, until_iso, max_commits_to_inspect=30):
    """
    오늘 커밋한 레포들을 REST API로 조회해 실제 additions/deletions 합산.
    비공개 레포(권한 없음) 등으로 실패하면 그 레포는 건너뛴다.
    호출량 제한을 위해 레포별 최대 max_commits_to_inspect개 커밋까지만 상세 조회.
    """
    total_add, total_del = 0, 0
    inspected = 0

    for r in repos:
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
            continue
        if resp.status_code != 200:
            # 비공개 레포 접근 불가 등 — 이 레포만 건너뜀 (전체 실패 아님)
            continue

        for commit in resp.json():
            if inspected >= max_commits_to_inspect:
                break
            sha = commit.get("sha")
            if not sha:
                continue
            detail_url = f"{REST_BASE}/repos/{owner}/{repo}/commits/{sha}"
            try:
                detail_resp = requests.get(detail_url, headers=REST_HEADERS, timeout=15)
            except requests.RequestException:
                continue
            if detail_resp.status_code != 200:
                continue
            stats = detail_resp.json().get("stats", {})
            total_add += stats.get("additions", 0)
            total_del += stats.get("deletions", 0)
            inspected += 1

    return {"additions": total_add, "deletions": total_del, "inspected": inspected}


def fetch_member_stats(username):
    """
    반환값:
      {"commits", "additions", "deletions", "is_estimate", "fetch_failed"}
    fetch_failed=True 면 GraphQL 조회 자체가 실패한 것 (0커밋과 구분됨).
    """
    contrib = fetch_member_contributions(username)
    if contrib is None:
        return {"commits": 0, "additions": 0, "deletions": 0, "is_estimate": False, "fetch_failed": True}

    commits = contrib["commits"]
    if commits == 0:
        return {"commits": 0, "additions": 0, "deletions": 0, "is_estimate": False, "fetch_failed": False}

    real = fetch_real_stats(username, contrib["repos"], contrib["today_start"], contrib["today_end"])

    if real["inspected"] > 0:
        return {
            "commits": commits,
            "additions": real["additions"],
            "deletions": real["deletions"],
            "is_estimate": False,
            "fetch_failed": False,
        }

    # REST 조회가 아무것도 못 가져왔으면(권한 문제 등) 추정치로 폴백 + 명시
    print(f"[WARN] {username} 실제 stats 조회 실패, 추정치로 대체")
    return {
        "commits": commits,
        "additions": commits * 25,
        "deletions": commits * 5,
        "is_estimate": True,
        "fetch_failed": False,
    }


def generate_progress_bar(commits, max_commits):
    if max_commits == 0 or commits == 0:
        return "`░░░░░░░░░░░░░░░░░░░░`"
    ratio = min(commits / max_commits, 1.0)
    filled = int(ratio * 20)
    unfilled = 20 - filled
    return f"`{'█' * filled}{'░' * unfilled}`"


# ==========================================
# 5. 스트릭 계산
# ==========================================
def update_streaks(state, stats, today_str, top_user):
    """
    상태 파일을 오늘 결과로 갱신하고, 각 멤버의 (top_streak, grass_streak)를 반환.
    같은 날 재실행(cron이 여러 번 도는 경우) 시 스트릭이 중복 증가하지 않도록
    last_run_date로 '이미 오늘 처리했는지' 확인한다.
    """
    already_ran_today = state.get("last_run_date") == today_str
    members_state = state.setdefault("members", {})

    result = {}
    for user in stats:
        uname = user["username"]
        m = members_state.setdefault(uname, {"top_streak": 0, "grass_streak": 0, "last_success": None})

        # API 조회 실패면 직전 성공 데이터로 폴백, 스트릭은 변경하지 않음
        if user.get("fetch_failed"):
            fallback = m.get("last_success")
            if fallback:
                user["commits"] = fallback["commits"]
                user["additions"] = fallback["additions"]
                user["deletions"] = fallback["deletions"]
                user["is_estimate"] = fallback.get("is_estimate", True)
            result[uname] = {"top_streak": m["top_streak"], "grass_streak": m["grass_streak"]}
            continue

        if not already_ran_today:
            is_top = top_user is not None and uname == top_user["username"]
            has_grass = user["commits"] > 0

            m["grass_streak"] = m["grass_streak"] + 1 if has_grass else 0
            m["top_streak"] = m["top_streak"] + 1 if is_top else 0
            m["last_success"] = {
                "commits": user["commits"],
                "additions": user["additions"],
                "deletions": user["deletions"],
                "is_estimate": user.get("is_estimate", False),
            }

        result[uname] = {"top_streak": m["top_streak"], "grass_streak": m["grass_streak"]}

    state["last_run_date"] = today_str
    return result


# ==========================================
# 6. README 마크다운 생성 로직
# ==========================================
def update_readme():
    if not GITHUB_TOKEN:
        print("[FATAL] GH_TOKEN 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    tz = pytz.timezone("Asia/Seoul")
    now = datetime.now(tz)
    now_str = now.strftime("%Y--%m--%d_%H:%M_KST")
    today_str = now.strftime("%Y-%m-%d")

    state = load_state()

    stats = []
    max_commits = 0
    any_failed = False

    for m in MEMBERS:
        data = fetch_member_stats(m["username"])
        if data["fetch_failed"]:
            any_failed = True
        if data["commits"] > max_commits:
            max_commits = data["commits"]

        stats.append({
            "name": m["name"],
            "username": m["username"],
            "commits": data["commits"],
            "additions": data["additions"],
            "deletions": data["deletions"],
            "is_estimate": data["is_estimate"],
            "fetch_failed": data["fetch_failed"],
        })

    # 커밋 수 기준 내림차순 정렬 (조회 실패자는 정렬에 영향 없도록 그대로 두되 순위 하단 배치는 안 함 - 값 그대로 사용)
    stats.sort(key=lambda x: x["commits"], reverse=True)

    top_user = stats[0] if stats and stats[0]["commits"] > 0 and not stats[0]["fetch_failed"] else None

    streaks = update_streaks(state, stats, today_str, top_user)
    save_state(state)

    with open(README_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # --- 1) LAST_UPDATE 뱃지 갱신 ---
    badge_pattern = r"LAST_UPDATE-[0-9]{4}--[0-9]{2}--[0-9]{2}_[0-9]{2}:[0-9]{2}_KST-success"
    new_badge = f"LAST_UPDATE-{now_str}-success"
    content, n = re.subn(badge_pattern, new_badge, content)
    if n == 0:
        print("[WARN] LAST_UPDATE 뱃지 패턴을 찾지 못해 갱신하지 못했습니다.")

    # --- 2) RANKING 영역 생성 ---
    ranking_md = "<!-- RANKING:START -->\n## 🥇 TODAY'S HIGHLIGHTS\n\n"
    if top_user:
        ranking_md += (
            f"| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n"
            f"| 👑 **[@{top_user['username']}](https://github.com/{top_user['username']})** "
            f"· **{top_user['commits']} Commits** |\n\n"
        )
    else:
        ranking_md += "| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n| 🌿 아직 오늘의 첫 잔디를 기다리고 있습니다! |\n\n"

    if any_failed:
        ranking_md += "> ⚠️ 일부 멤버의 데이터를 가져오지 못해 마지막으로 성공한 값을 표시하고 있습니다.\n\n"

    ranking_md += "<br>\n\n### 📈 오늘의 순위표 (매시간 갱신)\n\n"
    ranking_md += "| 순위 | 상태 | 멤버 | 커밋 수 | 코드 변화량 (+/-) | 달성도 |\n"
    ranking_md += "| :---: | :---: | :--- | :---: | :---: | :--- |\n"

    ranks = ["🥇 1st", "🥈 2nd", "🥉 3rd", "4th", "5th", "6th"]
    for i, user in enumerate(stats):
        rank_str = ranks[i] if i < len(ranks) else f"{i + 1}th"

        if user["fetch_failed"]:
            status = "⚠️"
        elif user["commits"] >= 5:
            status = "🔥"
        elif user["commits"] > 0:
            status = "🌿"
        else:
            status = "🌑"

        bar = generate_progress_bar(user["commits"], max_commits)
        est_mark = " <sup>(추정)</sup>" if user["is_estimate"] else ""

        ranking_md += (
            f"| **{rank_str}** | {status} | **[@{user['username']}](https://github.com/{user['username']})** "
            f"({user['name']}) | `{user['commits']}개` | "
            f"`+{user['additions']}` / `-{user['deletions']}`{est_mark} | {bar} |\n"
        )
    ranking_md += "<!-- RANKING:END -->"

    # --- 3) RECORDS 영역 생성 (실제 스트릭 반영) ---
    record_md = "<!-- RECORD:START -->\n## 🏅 명예의 전당 (RECORDS)\n\n"
    record_md += "| 멤버 | 🔥 연속 1등 | 🏆 최장 연속 잔디 스트릭 |\n"
    record_md += "| :--- | :---: | :---: |\n"
    for user in stats:
        s = streaks[user["username"]]
        top_streak_str = f"🔥 {s['top_streak']}일" if s["top_streak"] > 0 else "-"
        record_md += (
            f"| **[@{user['username']}](https://github.com/{user['username']})** | "
            f"`{top_streak_str}` | `🏆 {s['grass_streak']}일` |\n"
        )
    record_md += "<!-- RECORD:END -->"

    # --- 4) 치환 (실패 시 명확히 알림) ---
    content, n_rank = re.subn(
        r"<!-- RANKING:START -->.*?<!-- RANKING:END -->", ranking_md, content, flags=re.DOTALL
    )
    content, n_record = re.subn(
        r"<!-- RECORD:START -->.*?<!-- RECORD:END -->", record_md, content, flags=re.DOTALL
    )

    if n_rank == 0 or n_record == 0:
        print("[FATAL] README의 RANKING 또는 RECORD 마커를 찾지 못해 치환하지 못했습니다. "
              "마커가 삭제/변형되었는지 확인하세요.")
        sys.exit(1)

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[OK] README 갱신 완료 (실패 멤버: {'있음' if any_failed else '없음'})")


if __name__ == "__main__":
    update_readme()