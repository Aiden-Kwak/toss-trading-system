#!/usr/bin/env python3
"""
discord-bot.py — Discord 명령어 봇

채널에서 !명령어를 입력하면 거래 정보를 조회하여 응답합니다.

사용법:
  .venv/bin/python3 scripts/discord-bot.py
  DISCORD_BOT_TOKEN=xxx .venv/bin/python3 scripts/discord-bot.py

명령어:
  !help       — 명령어 목록
  !positions  — 보유 포지션
  !trades [N] — 최근 N건 청산 내역 (기본 5)
  !today      — 오늘 거래 요약
  !status     — 데몬 상태
  !report     — 일일 보고서 전송
  !ping       — 봇 응답 테스트
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import discord

# ─── .env 로드 ───
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ─── 설정 ───
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

CONFIG_DIR = Path.home() / "Library/Application Support/tossctl"
LOG_FILE = CONFIG_DIR / "trade-log.json"
DAEMON_STATE_FILE = CONFIG_DIR / "daemon-state.json"
SIGNAL_CONFIG = CONFIG_DIR / "signal-config.json"
SCRIPTS_DIR = Path(__file__).parent

HOME = Path.home()
_default_bin = HOME / "Desktop/Auto-trader/tossinvest-cli/bin/tossctl"
_default_helper = HOME / "Desktop/Auto-trader/tossinvest-cli/auth-helper"
TOSS_BIN = Path(os.environ.get("TOSSCTL_BIN", str(_default_bin)))
TOSS_HELPER_DIR = os.environ.get("TOSSCTL_AUTH_HELPER_DIR", str(_default_helper))
TOSS_HELPER_PY = os.environ.get("TOSSCTL_AUTH_HELPER_PYTHON", str(Path(TOSS_HELPER_DIR) / ".venv/bin/python3"))
TOSS_ENV = {
    **os.environ,
    "PATH": f"{TOSS_BIN.parent}:{os.environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": TOSS_HELPER_DIR,
    "TOSSCTL_AUTH_HELPER_PYTHON": TOSS_HELPER_PY,
}

# 색상
GREEN = 0x22c55e
RED = 0xef4444
YELLOW = 0xeab308
BLUE = 0x3b82f6
PURPLE = 0xa855f7

# ─── 데이터 로드 ───

def load_trades() -> list:
    try:
        import sys as _sys; _sys.path.insert(0, str(Path(__file__).parent))
        from db import query_trades
        return query_trades(status="all", limit=5000)
    except Exception:
        pass
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def load_daemon_state() -> dict | None:
    if DAEMON_STATE_FILE.exists():
        return json.loads(DAEMON_STATE_FILE.read_text())
    return None


def get_mode_label() -> str:
    mode = os.environ.get("TOSS_TRADE_MODE", "live")
    return "DRY RUN" if mode == "dry-run" else "LIVE"


def footer() -> dict:
    return {"text": f"toss-trading-system | {get_mode_label()}"}


# ─── 명령 핸들러 ───

def cmd_help() -> discord.Embed:
    desc = "\n".join([
        "**조회 명령:**",
        "`!positions` — 현재 보유 포지션",
        "`!trades [N]` — 최근 N건 청산 내역 (기본 5)",
        "`!today` — 오늘 거래 요약",
        "`!status` — 데몬 상태",
        "`!report` — 일일 보고서 전송",
        "`!scan [watchlist|auto|all]` — 종목 스크리닝 (등급/점수)",
        "",
        "**제어 명령:**",
        "`!mode live|dry-run` — 거래 모드 전환",
        "`!daemon start|stop` — 데몬 시작/종료",
        "`!login` — 토스증권 세션 갱신",
        "",
        "**기타:**",
        "`!ping` — 봇 응답 테스트",
        "`!help` — 이 도움말",
    ])
    return discord.Embed(title="📖 명령어 목록", description=desc, color=PURPLE).set_footer(**footer())


def cmd_positions() -> discord.Embed:
    # tossctl로 실시간 포지션 조회
    positions = []
    try:
        r = subprocess.run(
            [str(TOSS_BIN), "portfolio", "positions", "--output", "json"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        if r.returncode == 0:
            positions = json.loads(r.stdout)
    except Exception:
        pass

    # 보호종목 제외 여부는 표시만 하고 포함
    protected_file = Path.home() / "Library/Application Support/tossctl/protected-stocks.json"
    protected = set()
    try:
        if protected_file.exists():
            pd = json.loads(protected_file.read_text())
            protected = {s["symbol"] for s in pd.get("stocks", [])}
    except Exception:
        pass

    # trade-log에서 진입 정보 보완 (grade, reason 등)
    trades = load_trades()
    log_map = {t["symbol"]: t for t in trades if t.get("status") == "open"}

    if not positions:
        return discord.Embed(
            title="📋 보유 포지션",
            description="현재 보유 중인 포지션이 없습니다.",
            color=PURPLE
        ).set_footer(**footer())

    embed = discord.Embed(title=f"📋 보유 포지션 ({len(positions)}건)", color=PURPLE)
    for p in positions[:25]:
        sym = p.get("symbol", p.get("product_code", "?"))
        qty = p.get("quantity", 0)
        avg = p.get("average_price", p.get("avg_price", 0))
        cur = p.get("current_price", 0)
        pnl_pct = p.get("profit_rate", 0)
        if isinstance(pnl_pct, float) and abs(pnl_pct) < 10:
            pnl_pct *= 100  # 0.xx → xx%로 변환

        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
        lock_icon = "🔒 " if sym in protected else ""
        log_entry = log_map.get(sym, {})
        grade = log_entry.get("entry_grade", "")
        grade_str = f" | {grade}등급" if grade else ""

        embed.add_field(
            name=f"{lock_icon}{sym}",
            value=f"{qty:.2f}주 @ {avg:,.0f}원\n현재: {cur:,.0f}원 {pnl_icon}{pnl_pct:+.2f}%{grade_str}",
            inline=True
        )
    return embed.set_footer(**footer())


def cmd_trades(count: int = 5) -> discord.Embed:
    trades = load_trades()
    closed = [t for t in trades if t.get("status") == "closed"]
    recent = closed[-count:][::-1]

    if not recent:
        return discord.Embed(
            title="📜 최근 거래",
            description="청산된 거래가 없습니다.",
            color=PURPLE
        ).set_footer(**footer())

    wins = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)
    total_pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    desc = f"전체: {len(closed)}건 | 승률: {win_rate:.0f}% | 누적: {total_pnl:+.2f}%\n\n"
    for t in recent:
        pnl = t.get("pnl_pct", 0) or 0
        icon = "🟢" if pnl >= 0 else "🔴"
        reason = t.get("exit_reason", "")
        mode_tag = " `DRY`" if t.get("mode") == "dry-run" else ""
        desc += f"{icon} **{t['symbol']}** {pnl:+.2f}%{mode_tag} — {reason} ({t.get('exit_date', '')})\n"

    return discord.Embed(
        title=f"📜 최근 거래 (최근 {len(recent)}건)",
        description=desc, color=PURPLE
    ).set_footer(**footer())


def cmd_today() -> discord.Embed:
    trades = load_trades()
    today = datetime.now().strftime("%Y-%m-%d")

    today_trades = [t for t in trades if t.get("entry_date") == today or t.get("exit_date") == today]
    opened = [t for t in today_trades if t.get("entry_date") == today]
    closed = [t for t in today_trades if t.get("exit_date") == today and t.get("status") == "closed"]

    pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)
    wins = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)

    desc = f"**매수**: {len(opened)}건 | **청산**: {len(closed)}건\n"
    if closed:
        desc += f"**수익**: {pnl:+.2f}% | **승률**: {wins}/{len(closed)}\n\n"
        for t in closed:
            p = t.get("pnl_pct", 0) or 0
            icon = "🟢" if p >= 0 else "🔴"
            desc += f"{icon} {t['symbol']} {p:+.2f}% ({t.get('exit_reason', '')})\n"
    else:
        desc += "청산 내역 없음\n"

    still_open = [t for t in today_trades if t.get("status") == "open"]
    if still_open:
        desc += f"\n**보유 중**: {', '.join(t['symbol'] for t in still_open)}"

    color = GREEN if pnl > 0 else (RED if pnl < 0 else PURPLE)
    return discord.Embed(
        title=f"📅 오늘 거래 요약 | {today}",
        description=desc, color=color
    ).set_footer(**footer())


def cmd_status() -> discord.Embed:
    d = load_daemon_state()
    if not d:
        return discord.Embed(
            title="🤖 데몬 상태",
            description="데몬 상태 파일이 없습니다.\n실행 중이 아닐 수 있습니다.",
            color=YELLOW
        ).set_footer(**footer())

    # 프로세스 실제 실행 여부
    try:
        ps = subprocess.run(["pgrep", "-f", "autotrade-daemon"], capture_output=True, text=True)
        process_alive = ps.returncode == 0
    except Exception:
        process_alive = False

    status = d.get("status", "unknown")
    icons = {"running": "🟢", "paused_session": "🔴", "paused_maintenance": "🔧",
             "paused_loss_limit": "🚫", "paused_cooldown": "❄️", "stopped": "⬛", "error": "⚠️"}

    proc_label = "프로세스 활성" if process_alive else "프로세스 없음 ⚠️"
    desc = f"**상태**: {icons.get(status, '❓')} {status}\n"
    desc += f"**프로세스**: {proc_label}\n"
    desc += f"**사이클**: {d.get('cycle_count', 0)}회\n"
    desc += f"**오늘 거래**: {d.get('today_trades', 0)}건\n"
    desc += f"**오늘 손익**: {d.get('today_pnl', 0):+.2f}%\n"
    desc += f"**연속 손절**: {d.get('consecutive_losses', 0)}회\n"
    desc += f"**마지막 사이클**: {d.get('last_cycle_at', '-')}\n"

    errors = d.get("errors", [])
    if errors:
        last = errors[-1]
        desc += f"\n**최근 에러**: {last.get('msg', '')[:100]}\n`{last.get('time', '')}`"

    color = GREEN if status == "running" and process_alive else (RED if status in ("error", "stopped") else YELLOW)
    return discord.Embed(title="🤖 데몬 상태", description=desc, color=color).set_footer(**footer())


def cmd_report() -> discord.Embed:
    """일일 보고서 생성"""
    try:
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "report-generator.py"), "daily"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return discord.Embed(title="📊 보고서 오류", description=f"생성 실패: {r.stderr[:200]}",
                                 color=RED).set_footer(**footer())

        report = json.loads(r.stdout)
        p = report.get("period", {})
        c = report.get("cumulative", {})
        pnl = p.get("total_pnl", 0)

        desc = f"거래: **{p.get('trades', 0)}건** | 승률: **{p.get('win_rate', 0)}%**\n"
        desc += f"수익: **{pnl:+.2f}%**\n"
        desc += f"누적: {c.get('total_trades', 0)}건 {c.get('total_pnl', 0):+.2f}%"

        by_sym = report.get("by_symbol", {})
        if by_sym:
            desc += "\n\n**종목별:**\n"
            for s, v in list(by_sym.items())[:5]:
                desc += f"  {s}: {v.get('pnl', 0):+.2f}% ({v.get('trades', 0)}건)\n"

        color = GREEN if pnl >= 0 else RED
        return discord.Embed(
            title=f"📊 {report.get('label', '일일')} 보고서 | {report.get('date', '')}",
            description=desc, color=color
        ).set_footer(**footer())
    except Exception as e:
        return discord.Embed(title="📊 보고서 오류", description=str(e)[:200],
                             color=RED).set_footer(**footer())


# ─── 스크리너 ───

def cmd_scan(source: str = "watchlist") -> list[discord.Embed]:
    """종목 스크리닝 실행 후 결과를 Discord에 전송"""
    try:
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "stock-screener.py"), "scan", "--source", source],
            capture_output=True, text=True, timeout=60, env=TOSS_ENV
        )
        if r.returncode != 0:
            return [discord.Embed(title="🔍 스크리너 오류", description=f"실행 실패:\n```{r.stderr[:300]}```",
                                   color=RED).set_footer(**footer())]

        data = json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return [discord.Embed(title="🔍 스크리너 오류", description="타임아웃 (60초 초과)",
                               color=RED).set_footer(**footer())]
    except Exception as e:
        return [discord.Embed(title="🔍 스크리너 오류", description=str(e)[:300],
                               color=RED).set_footer(**footer())]

    summary = data.get("summary", {})
    sources = ", ".join(data.get("sources", []))
    embeds = []

    # 요약 embed
    overview = discord.Embed(
        title=f"🔍 종목 스크리닝 결과",
        description=(
            f"**소스**: {sources}\n"
            f"**스캔**: {data.get('total_scanned', 0)}종목 → **채점**: {data.get('total_scored', 0)}종목\n\n"
            f"🟢 매수 후보 (A/B): **{summary.get('buy', 0)}**건\n"
            f"🟡 관찰 (C): **{summary.get('watch', 0)}**건\n"
            f"⚪ 스킵 (D): **{summary.get('skip', 0)}**건"
        ),
        color=GREEN if summary.get("buy", 0) > 0 else PURPLE
    ).set_footer(**footer())
    embeds.append(overview)

    # 매수 후보 상세
    buy_list = data.get("buy_candidates", [])
    if buy_list:
        desc = ""
        for c in buy_list[:10]:
            sym = c.get("symbol", "?")
            name = c.get("name", "")
            grade = c.get("grade", "?")
            score = c.get("score_pct", c.get("score", 0))
            price = c.get("current_price", 0)
            rec = c.get("recommendation", "")

            grade_icon = "🅰️" if grade == "A" else "🅱️"
            desc += f"{grade_icon} **{sym}**"
            if name:
                desc += f" ({name})"
            desc += f"\n  점수: **{score:.0f}** | 현재가: {price:,.2f}"
            if rec:
                desc += f"\n  추천: {rec}"
            desc += "\n\n"

        buy_embed = discord.Embed(
            title=f"🟢 매수 후보 ({len(buy_list)}건)",
            description=desc[:4000],
            color=GREEN
        ).set_footer(**footer())
        embeds.append(buy_embed)

    # 관찰 종목
    watch_list = data.get("watch_list", [])
    if watch_list:
        desc = ""
        for c in watch_list[:10]:
            sym = c.get("symbol", "?")
            name = c.get("name", "")
            score = c.get("score_pct", c.get("score", 0))
            desc += f"🟡 **{sym}**"
            if name:
                desc += f" ({name})"
            desc += f" — 점수: {score:.0f}\n"

        watch_embed = discord.Embed(
            title=f"🟡 관찰 종목 ({len(watch_list)}건)",
            description=desc[:4000],
            color=YELLOW
        ).set_footer(**footer())
        embeds.append(watch_embed)

    if not buy_list and not watch_list:
        embeds.append(discord.Embed(
            description="매수 후보 및 관찰 종목이 없습니다.",
            color=PURPLE
        ).set_footer(**footer()))

    return embeds


# ─── 제어 명령 ───

def cmd_mode(new_mode: str) -> discord.Embed:
    """live/dry-run 모드 전환 — signal-config.json에 기록, 데몬이 다음 사이클에 반영"""
    if new_mode not in ("live", "dry-run"):
        return discord.Embed(
            title="⚙️ 모드 전환",
            description="사용법: `!mode live` 또는 `!mode dry-run`",
            color=YELLOW
        ).set_footer(**footer())

    # signal-config.json 업데이트
    config = {}
    if SIGNAL_CONFIG.exists():
        try:
            config = json.loads(SIGNAL_CONFIG.read_text())
        except Exception:
            pass

    old_mode = config.get("trade_mode", "live")
    config["trade_mode"] = new_mode
    SIGNAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2))

    if old_mode == new_mode:
        desc = f"이미 **{new_mode.upper()}** 모드입니다."
        color = PURPLE
    else:
        desc = f"**{old_mode.upper()}** → **{new_mode.upper()}**\n다음 사이클부터 적용됩니다."
        color = GREEN if new_mode == "live" else BLUE

    return discord.Embed(title="⚙️ 모드 전환", description=desc, color=color).set_footer(**footer())


def _is_daemon_running() -> bool:
    try:
        ps = subprocess.run(["pgrep", "-f", "autotrade-daemon"], capture_output=True, text=True)
        return ps.returncode == 0
    except Exception:
        return False


async def cmd_daemon_control(action: str, args: list) -> discord.Embed:
    """데몬 시작/종료"""
    if action == "start":
        if _is_daemon_running():
            return discord.Embed(
                title="🤖 데몬 제어",
                description="데몬이 이미 실행 중입니다.\n중지 후 재시작하려면: `!daemon stop` → `!daemon start`",
                color=YELLOW
            ).set_footer(**footer())

        # 옵션 파싱
        market = "us"
        interval = "300"
        dry_run = False
        for i, a in enumerate(args):
            if a == "--market" and i + 1 < len(args):
                market = args[i + 1]
            elif a == "--interval" and i + 1 < len(args):
                interval = args[i + 1]
            elif a == "--dry-run":
                dry_run = True

        # config에서 모드 확인
        if SIGNAL_CONFIG.exists():
            try:
                cfg = json.loads(SIGNAL_CONFIG.read_text())
                if cfg.get("trade_mode") == "dry-run":
                    dry_run = True
            except Exception:
                pass

        cmd_args = ["python3", str(SCRIPTS_DIR / "autotrade-daemon.py"),
                     "--market", market, "--interval", interval]
        if dry_run:
            cmd_args.append("--dry-run")

        subprocess.Popen(cmd_args, stdout=open(CONFIG_DIR / "daemon.log", "a"),
                         stderr=subprocess.STDOUT, start_new_session=True)

        mode_label = "DRY RUN" if dry_run else "LIVE"
        return discord.Embed(
            title="🟢 데몬 시작",
            description=f"**모드**: {mode_label}\n**시장**: {market.upper()}\n**간격**: {interval}초",
            color=GREEN
        ).set_footer(**footer())

    elif action == "stop":
        if not _is_daemon_running():
            return discord.Embed(
                title="🤖 데몬 제어",
                description="실행 중인 데몬이 없습니다.",
                color=YELLOW
            ).set_footer(**footer())

        subprocess.run(["pkill", "-f", "autotrade-daemon"], capture_output=True)
        return discord.Embed(
            title="⬛ 데몬 종료",
            description="데몬 프로세스를 종료했습니다.",
            color=RED
        ).set_footer(**footer())

    else:
        return discord.Embed(
            title="🤖 데몬 제어",
            description="사용법:\n`!daemon start [--market us|kr] [--interval 300] [--dry-run]`\n`!daemon stop`",
            color=YELLOW
        ).set_footer(**footer())


def cmd_login() -> discord.Embed:
    """토스증권 세션 갱신 시도"""
    # 1) 현재 세션 상태 확인
    try:
        r = subprocess.run(
            [str(TOSS_BIN), "auth", "status", "--output", "json"],
            capture_output=True, text=True, timeout=10, env=TOSS_ENV
        )
        if r.returncode == 0:
            status = json.loads(r.stdout)
            if status.get("valid") or status.get("active") or "active" in str(status):
                return discord.Embed(
                    title="🟢 세션 상태",
                    description="세션이 이미 유효합니다. 재로그인이 필요 없습니다.",
                    color=GREEN
                ).set_footer(**footer())
    except Exception:
        pass

    # 2) 세션 갱신 시도 (auth refresh)
    try:
        r = subprocess.run(
            [str(TOSS_BIN), "auth", "refresh", "--output", "json"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        if r.returncode == 0:
            return discord.Embed(
                title="🟢 세션 갱신 성공",
                description="토스증권 세션이 갱신되었습니다.",
                color=GREEN
            ).set_footer(**footer())
    except Exception:
        pass

    # 3) 자동 갱신 실패 → 수동 안내
    return discord.Embed(
        title="🔴 세션 갱신 실패",
        description="자동 갱신에 실패했습니다.\n터미널에서 직접 로그인해주세요:\n```\ntossctl auth login\n```",
        color=RED
    ).set_footer(**footer())


# ─── Bot 실행 ───

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[Bot] {client.user} 로그인 완료")
    print(f"[Bot] 서버: {', '.join(g.name for g in client.guilds)}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("!"):
        return

    parts = content[1:].split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "help":
        await message.channel.send(embed=cmd_help())
    elif cmd == "positions" or cmd == "pos":
        await message.channel.send(embed=cmd_positions())
    elif cmd == "trades":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
        await message.channel.send(embed=cmd_trades(count))
    elif cmd == "today":
        await message.channel.send(embed=cmd_today())
    elif cmd == "status":
        await message.channel.send(embed=cmd_status())
    elif cmd == "report":
        await message.channel.send(embed=cmd_report())
    elif cmd == "scan":
        source = parts[1].lower() if len(parts) > 1 else "watchlist"
        embeds = cmd_scan(source)
        for embed in embeds:
            await message.channel.send(embed=embed)
    elif cmd == "mode":
        mode_arg = parts[1].lower() if len(parts) > 1 else ""
        await message.channel.send(embed=cmd_mode(mode_arg))
    elif cmd == "daemon":
        action = parts[1].lower() if len(parts) > 1 else ""
        await message.channel.send(embed=await cmd_daemon_control(action, parts[2:]))
    elif cmd == "login":
        await message.channel.send(embed=cmd_login())
    elif cmd == "ping":
        await message.channel.send(embed=discord.Embed(
            title="🏓 Pong!",
            description=f"응답 지연: {round(client.latency * 1000)}ms",
            color=GREEN
        ).set_footer(**footer()))


if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_BOT_TOKEN 환경변수를 설정하세요.")
        sys.exit(1)
    client.run(TOKEN)
