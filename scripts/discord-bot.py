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
SCRIPTS_DIR = Path(__file__).parent

# 색상
GREEN = 0x22c55e
RED = 0xef4444
YELLOW = 0xeab308
BLUE = 0x3b82f6
PURPLE = 0xa855f7

# ─── 데이터 로드 ───

def load_trades() -> list:
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
        "",
        "**기타:**",
        "`!ping` — 봇 응답 테스트",
        "`!help` — 이 도움말",
    ])
    return discord.Embed(title="📖 명령어 목록", description=desc, color=PURPLE).set_footer(**footer())


def cmd_positions() -> discord.Embed:
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]

    if not open_trades:
        return discord.Embed(
            title="📋 보유 포지션",
            description="현재 보유 중인 포지션이 없습니다.",
            color=PURPLE
        ).set_footer(**footer())

    embed = discord.Embed(title=f"📋 보유 포지션 ({len(open_trades)}건)", color=PURPLE)
    for t in open_trades[:25]:
        entry = t.get("entry_price", 0)
        grade = t.get("entry_grade", "?")
        mode_icon = "🤖" if t.get("mode") == "autotrade" else "🔧" if t.get("mode") == "dry-run" else "👤"
        embed.add_field(
            name=f"{mode_icon} {t['symbol']}",
            value=f"{t.get('quantity', 0):.0f}주 @ {entry:,.2f}\n등급: {grade} | {t.get('entry_date', '')}",
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
