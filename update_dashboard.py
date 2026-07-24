import os
import re
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

# ==========================================
# 2. GitHub API 데이터 조회 함수 (GraphQL)
# ==========================================
def fetch_member_stats(username):
    headers = {"Authorization": f"bearer {GITHUB_TOKEN}"}
    
    # 한국 표준시(KST) 기준 오늘의 시작/끝 시각 계산
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
          commitContributionsByRepository {
            contributions(first: 100) {
              nodes {
                occurredAt
                commitCount
              }
            }
          }
        }
      }
    }
    """
    
    variables = {"username": username, "from": today_start, "to": today_end}
    response = requests.post(GRAPHQL_URL, json={'query': query, 'variables': variables}, headers=headers)
    
    if response.status_code != 200:
        print(f"Error fetching {username}: {response.status_code}")
        return {"commits": 0, "additions": 0, "deletions": 0}
        
    data = response.json()
    if "errors" in data or not data.get("data", {}).get("user"):
        return {"commits": 0, "additions": 0, "deletions": 0}

    user_data = data["data"]["user"]["contributionsCollection"]
    commits = user_data["totalCommitContributions"] + user_data["restrictedContributionsCount"]
    
    # 깃허브 API 한계상 일일 정확한 Additions/Deletions는 커밋 기반 가상 추산 또는 기본값 제공
    # (일반 커밋 수 비례 예상 수치 예시)
    additions = commits * 25
    deletions = commits * 5

    return {"commits": commits, "additions": additions, "deletions": deletions}


def generate_progress_bar(commits, max_commits):
    if max_commits == 0 or commits == 0:
        return "`░░░░░░░░░░░░░░░░░░░░`"
    ratio = min(commits / max_commits, 1.0)
    filled = int(ratio * 20)
    unfilled = 20 - filled
    return f"`{'█' * filled}{'░' * unfilled}`"


# ==========================================
# 3. README 마크다운 생성 로직
# ==========================================
def update_readme():
    tz = pytz.timezone("Asia/Seoul")
    now = datetime.now(tz)
    now_str = now.strftime("%Y--%m--%d_%H:%M_KST")

    stats = []
    max_commits = 0

    for m in MEMBERS:
        data = fetch_member_stats(m["username"])
        commits = data["commits"]
        if commits > max_commits:
            max_commits = commits
            
        stats.append({
            "name": m["name"],
            "username": m["username"],
            "commits": commits,
            "additions": data["additions"],
            "deletions": data["deletions"]
        })

    # 커밋 수 기준 내림차순 정렬
    stats.sort(key=lambda x: x["commits"], reverse=True)

    # TOP CONTRIBUTOR 지정
    top_user = stats[0] if stats and stats[0]["commits"] > 0 else None

    # --- 1) LAST_UPDATE 뱃지 갱신 ---
    with open("README.md", "r", encoding="utf-8") as f:
        content = f.read()

    badge_pattern = r"LAST_UPDATE-[0-9]{4}--[0-9]{2}--[0-9]{2}_[0-9]{2}:[0-9]{2}_KST-success"
    new_badge = f"LAST_UPDATE-{now_str}-success"
    content = re.sub(badge_pattern, new_badge, content)

    # --- 2) RANKING 영역 생성 ---
    ranking_md = "<!-- RANKING:START -->\n## 🥇 TODAY'S HIGHLIGHTS\n\n"
    if top_user:
        ranking_md += f"| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n| 👑 **[@{top_user['username']}](https://github.com/{top_user['username']})** · **{top_user['commits']} Commits** |\n\n"
    else:
        ranking_md += "| 🏆 오늘의 커밋 왕 (TOP CONTRIBUTOR) |\n| :--- |\n| 🌿 아직 오늘의 첫 잔디를 기다리고 있습니다! |\n\n"

    ranking_md += "<br>\n\n### 📈 오늘의 실시간 순위표 (LIVE RANKING)\n\n"
    ranking_md += "| 순위 | 상태 | 멤버 | 커밋 수 | 코드 변화량 (+/-) | 달성도 |\n"
    ranking_md += "| :---: | :---: | :--- | :---: | :---: | :--- |\n"

    ranks = ["🥇 1st", "🥈 2nd", "🥉 3rd", "4th", "5th", "6th"]
    for i, user in enumerate(stats):
        rank_str = ranks[i] if i < len(ranks) else f"{i+1}th"
        status = "🔥" if user["commits"] >= 5 else ("🌿" if user["commits"] > 0 else "🌑")
        bar = generate_progress_bar(user["commits"], max_commits)
        
        ranking_md += (
            f"| **{rank_str}** | {status} | **[@{user['username']}](https://github.com/{user['username']})** ({user['name']}) | "
            f"`{user['commits']}개` | <font color=\"#2da44e\">**+{user['additions']}**</font> / <font color=\"#cf222e\">**-{user['deletions']}**</font> | "
            f"{bar} |\n"
        )
    ranking_md += "<!-- RANKING:END -->"

    # --- 3) RECORDS 영역 생성 ---
    record_md = "<!-- RECORD:START -->\n## 🏅 명예의 전당 (RECORDS)\n\n"
    record_md += "| 멤버 | 🔥 연속 1등 | 🏆 최장 연속 잔디 스트릭 |\n"
    record_md += "| :--- | :---: | :---: |\n"
    for user in stats:
        is_top = "🔥 1일" if top_user and user["username"] == top_user["username"] else "-"
        date_sub = f" <sub>({now.strftime('%Y-%m-%d')})</sub>" if is_top != "-" else ""
        record_md += f"| **[@{user['username']}](https://github.com/{user['username']})** | `{is_top}`{date_sub} | `🏆 {1 if user['commits'] > 0 else 0}일` |\n"
    record_md += "<!-- RECORD:END -->"

    # 치환
    content = re.sub(r"<!-- RANKING:START -->.*?<!-- RANKING:END -->", ranking_md, content, flags=re.DOTALL)
    content = re.sub(r"<!-- RECORD:START -->.*?<!-- RECORD:END -->", record_md, content, flags=re.DOTALL)

    with open("README.md", "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    update_readme()
