# -*- coding: utf-8 -*-
# ============================================================
# 舞萌/中二 机厅排卡 (AstrBot 插件版)
# 由 QFun 版 maimai_queue v8 移植而来。
# 排卡逻辑参考: https://github.com/Cola-Ace/koishi-plugin-maimai-player-queue
#   机厅别名/双机台: https://github.com/SalinX/ArcadeQueue
#
# 指令一览:
#   开启排卡 / 关闭排卡                  (仅管理员) 聊天级开关，各聊天数据隔离
#   排卡帮助                             查看帮助
#   j / 机厅几人                         排卡总览 (舞萌 + 中二 + 合计)
#   mai几 / 舞萌几   (几人/几卡皆可)      查看舞萌排卡
#   chu几 / 中二几   (几人/几卡皆可)      查看中二排卡
#   <机厅别名>几                          查看排卡总览
#   j+n / j-n / j<n> / j=n               机厅合计 加/减/设置
#   <机厅别名>+n / -n / =n / <n>         机厅合计 加/减/设置
#   mai+n / mai-n / mai<n> (舞萌同)      舞萌排卡 加/减/设置
#   chu+n / chu-n / chu<n> (中二同)      中二排卡 加/减/设置
#   mai<a>chu<b> / chu<b>mai<a>          一条消息同时更新两个游戏 (舞萌/中二、+/-/= 均可，
#                                        可用逗号/顿号等隔开，如 mai3，chu2)
#   机厅别名                             查看已设置的机厅别名
#
#   智能匹配: 消息中带 @ 或其他文字时，只要包含 mai+1 / chu-1
#   这类带操作符的指令即可识别 (裸数字如 mai3 仅在整条消息为指令时生效，
#   避免"舞萌30分钟后到"之类聊天被误判；超长消息不参与智能匹配)
#
#   以下仅限管理员 (AstrBot WebUI 中配置的管理员):
#   添加机厅别名 <别名> / 删除机厅别名 <别名>
#   设置机台 mai<舞萌机台数> chu<中二机台数>
#   设置排卡上限<n>                       (默认 30)
#
# 合计与分游戏数据的关系:
#   mai/chu 指令更新分游戏数据，并把本次增减的差值同步进合计;
#   j/别名 指令直接更新"机厅合计"，并按分游戏数据反向推算:
#     - 有效期(默认 2 小时)内更新过的分游戏数据视为可信并保留
#       (两个都可信时保留更新的那个)，另一游戏用 合计-可信值 推算;
#     - 没有任何可信分游戏数据时，合计优先全加在舞萌上
#       (超过单游戏上限时溢出到中二)，太久远的旧数据保持不变;
#     - 推算出的数据标注「预计」，并提示可能不准确。
#   舞萌 / 中二 / 合计 三组数据各自独立跨天清零。
#
# QQ 官方机器人 (qq_official) 平台下以 Markdown 排版回复，并附带
# <qqbot-cmd-input> 可点击指令按钮、<qqbot-at-user> 艾特标签;
# 其他平台保持纯文本。可在插件配置 markdown_enabled 中关闭。
# ============================================================

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # 低版本兜底

    def get_astrbot_data_path():
        return "data"


PLUGIN_NAME = "astrbot_plugin_maimai_queue"

DEFAULT_MAX_CARDS = 30
DEFAULT_MAI_M = 1  # 默认舞萌机台数
DEFAULT_CHU_M = 1  # 默认中二机台数
SMART_MATCH_MAX = 30  # 智能匹配的消息长度上限 (剥离@后)
DEFAULT_FRESH_HOURS = 2  # 合计推算时，分游戏数据的可信有效期 (小时)

GAME_ICON = {"mai": "🐻", "chu": "🐧"}
DIV = "━━━━━━━━━━━━"

# Markdown 排版下需替换为全角的字符 (防止昵称/别名中的特殊字符破坏排版)
MD_SAFE = str.maketrans(
    {"*": "＊", "_": "＿", "~": "～", "#": "＃", "`": "｀", ">": "＞", "<": "＜", "[": "［", "]": "］"}
)

# ---------- 预编译正则 ----------
NUM = r"(0|[1-9]\d*)"
SEP = r"[,;.、。]*"  # 组合指令两段间允许的分隔符 (全角标点已在 normalize 转半角)
RE_CODES = re.compile(r"\[[a-zA-Z]+=[^\]]*\]")  # [CQ:at=..] 之类残留消息码
RE_AT_TEXT = re.compile(r"@\S+")  # 文本形式的 @昵称
RE_MACHINES = re.compile(r"^mai(\d+)chu(\d+)$")
RE_QUERY = re.compile(r"^(mai|舞萌|chu|中二)(几|几人|几卡)$")
RE_J_UPD = re.compile(r"^j([+\-=]?)" + NUM + r"$")
RE_ALIAS_OP = re.compile(r"^(.+?)(加|减|设置|设定|\+|-|=)" + NUM + r"$")
RE_ALIAS_NUM = re.compile(r"^(.+?)" + NUM + r"$")
RE_COMBO_MAI = re.compile(
    r"^(mai|舞萌)([+\-=]?)" + NUM + SEP + r"((chu|中二)([+\-=]?)" + NUM + r")?" + SEP + r"$"
)
RE_COMBO_CHU = re.compile(
    r"^(chu|中二)([+\-=]?)" + NUM + SEP + r"((mai|舞萌)([+\-=]?)" + NUM + r")?" + SEP + r"$"
)
# 消息内智能匹配: 必须带 +/-/= 操作符; (?<![a-z]) 防 email+1、(?!\d) 防截断数字
RE_INLINE_MAI = re.compile(r"(?<![a-z])(mai|舞萌)\s*([+\-=])\s*" + NUM + r"(?!\d)")
RE_INLINE_CHU = re.compile(r"(?<![a-z])(chu|中二)\s*([+\-=])\s*" + NUM + r"(?!\d)")


# ==================== 基础工具 ====================


def normalize(s: str) -> str:
    """全角转半角、去空白、统一小写 (用于精确指令匹配)"""
    if not s:
        return ""
    out = []
    for c in s:
        if c == "　":
            continue
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E:
            c = chr(o - 0xFEE0)
        if c.isspace():
            continue
        out.append(c.lower())
    return "".join(out)


def normalize_keep(s: str) -> str:
    """全角转半角、统一小写但保留空白 (用于消息内智能匹配，空白作为词边界)"""
    if not s:
        return ""
    out = []
    for c in s:
        if c == "　":
            c = " "
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E:
            c = chr(o - 0xFEE0)
        out.append(c.lower())
    return "".join(out)


def split_args(raw: str) -> list:
    """保留空白的切词 (用于读取别名等参数)，全角转半角但保留大小写"""
    out = []
    if not raw:
        return out
    buf = []
    for c in raw.strip():
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E:
            c = chr(o - 0xFEE0)
        if c.isspace() or c == "　":
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(c)
    if buf:
        out.append("".join(buf))
    return out


def strip_codes(s: str) -> str:
    """去除消息码及文本 @昵称，避免 @ 等内容干扰指令识别"""
    if not s:
        return ""
    return RE_AT_TEXT.sub(" ", RE_CODES.sub(" ", s))


def fmt_time(sec: int) -> str:
    return datetime.fromtimestamp(sec).strftime("%H:%M:%S")


def time_diff(sec: int) -> str:
    diff = int(time.time()) - int(sec)
    if diff < 0:
        diff = 0
    minutes = (diff + 59) // 60
    hours, rem = divmod(minutes, 60)
    if hours > 0:
        return f"{hours} 小时 {rem} 分钟"
    return f"{minutes} 分钟"


def avg_cards(queues: int, machines: int) -> str:
    """机均卡数: 整除显示具体数值，否则 'x+'"""
    if machines < 1:
        machines = 1
    if queues % machines == 0:
        return str(queues // machines)
    return f"{queues // machines}+"


def apply_op(cur: int, op: str, n: int) -> int:
    """按操作符计算新卡数: '+' 加、'-' 减、其余 (=/空) 直接设置"""
    if op == "+":
        return cur + n
    if op == "-":
        return cur - n
    return n


def game_label(g: str) -> str:
    return "舞萌DX" if g == "mai" else "中二节奏"


def md_escape(s) -> str:
    """昵称/别名等用户内容中的 Markdown 字符替换为全角，防止破坏排版"""
    return str(s).translate(MD_SAFE)


def cmd_btn(text: str, show: str = "") -> str:
    """QQ 官方 Markdown 可点击指令标签: 点击后把指令填入用户输入框 (text/show 需 urlencode)"""
    return f'<qqbot-cmd-input text="{quote(text, safe="")}" show="{quote(show or text, safe="")}" />'


def btn_row(*cmds: str) -> str:
    """一行可点击指令按钮"""
    return "💡 " + " ".join(cmd_btn(c) for c in cmds)


class MaimaiQueue(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = config or {}
        self.def_max = int(cfg.get("default_max_cards", DEFAULT_MAX_CARDS) or DEFAULT_MAX_CARDS)
        self.def_mai = int(cfg.get("default_mai_machines", DEFAULT_MAI_M))
        self.def_chu = int(cfg.get("default_chu_machines", DEFAULT_CHU_M))
        self.smart_max = int(cfg.get("smart_match_max", SMART_MATCH_MAX) or SMART_MATCH_MAX)
        self.fresh_hours = int(cfg.get("fresh_hours", DEFAULT_FRESH_HOURS) or DEFAULT_FRESH_HOURS)
        self.md_enabled = bool(cfg.get("markdown_enabled", True))

        self.note = (
            "📋 到达机厅后发 j+1（机厅合计）或 mai+1 / chu+1（分游戏）加卡，"
            "退勤时对应 -1 减卡，可修改数字一次增减多张\n"
            "j[数字] / mai[数字] / chu[数字] 快速设置，mai[数字]chu[数字] 一次更新两个游戏\n"
            f"直接更新合计时会按 {self.fresh_hours} 小时内的分游戏数据自动推算另一游戏（推算值标注「预计」）\n"
            "j 查看总览，mai几 / chu几 查看对应游戏，排卡帮助 查看全部指令"
        )

        self.help_text = (
            "📖 排卡指令帮助\n"
            f"{DIV}\n"
            "▎查询\n"
            "j / 机厅几人 …排卡总览\n"
            "mai几 / 舞萌几 …舞萌排卡\n"
            "chu几 / 中二几 …中二排卡\n"
            "<机厅别名>几 …排卡总览\n"
            "机厅别名 …查看已设置的别名\n"
            "▎更新\n"
            "j+n / j-n / j<n> …机厅合计 加/减/设置\n"
            "<机厅别名>+n / -n / <n> …机厅合计 加/减/设置\n"
            "mai+n / mai-n / mai<n> …舞萌（舞萌 前缀同效）\n"
            "chu+n / chu-n / chu<n> …中二（中二 前缀同效）\n"
            "mai<a>chu<b> …同时更新两个游戏（顺序可换、可用逗号分隔）\n"
            f"直接更新合计时，会按 {self.fresh_hours} 小时内的分游戏数据自动推算另一游戏，"
            "推算值标注「预计」\n"
            f"消息中含 mai+1 / chu-1 这类带符号指令也能识别（超{self.smart_max}字长消息除外）\n"
            "▎管理员\n"
            "开启排卡 / 关闭排卡 …本聊天排卡开关\n"
            "添加机厅别名 <别名> / 删除机厅别名 <别名>\n"
            "设置机台 mai<n> chu<n> …设置机台数\n"
            f"设置排卡上限<n> …默认{self.def_max}，舞萌/中二各自独立生效"
        )

        # ---- QQ 官方机器人 Markdown 版文案 (角括号会被识别为标签，改用示例写法) ----
        self.note_md = "\n".join(
            [
                "📋 到达机厅后发 j+1（机厅合计）或 mai+1 / chu+1（分游戏）加卡，"
                "退勤时对应 -1 减卡，可修改数字一次增减多张",
                "",
                "- jn / main / chun 快速设置，mai3chu2 一次更新两个游戏",
                f"- 直接更新合计时会按 {self.fresh_hours} 小时内的分游戏数据自动推算另一游戏"
                "（推算值标注「预计」）",
                "- j 查看总览，mai几 / chu几 查看对应游戏，排卡帮助 查看全部指令",
                "",
                btn_row("j", "j+1", "mai+1", "chu+1", "排卡帮助"),
            ]
        )

        self.help_md = "\n".join(
            [
                "## 📖 排卡指令帮助",
                "### ▎查询",
                "- **j** / 机厅几人 … 排卡总览",
                "- **mai几** / 舞萌几 … 舞萌排卡",
                "- **chu几** / 中二几 … 中二排卡",
                "- **机厅别名几** … 排卡总览",
                "- **机厅别名** … 查看已设置的别名",
                "### ▎更新",
                "- **j+n / j-n / jn** … 机厅合计 加/减/设置",
                "- **别名+n / 别名-n / 别名n** … 机厅合计 加/减/设置",
                "- **mai+n / mai-n / main** … 舞萌（舞萌 前缀同效）",
                "- **chu+n / chu-n / chun** … 中二（中二 前缀同效）",
                "- **mai3chu2** … 同时更新两个游戏（顺序可换、可用逗号分隔）",
                "",
                f"> 直接更新合计时，按 {self.fresh_hours} 小时内的分游戏数据自动推算另一游戏，"
                "推算值标注「预计」",
                f"> 消息中含 mai+1 / chu-1 这类带符号指令也能识别（超 {self.smart_max} 字长消息除外）",
                "### ▎管理员",
                "- 开启排卡 / 关闭排卡 … 本聊天排卡开关",
                "- 添加机厅别名 xx / 删除机厅别名 xx",
                "- 设置机台 mai2 chu1 … 设置机台数",
                f"- 设置排卡上限{self.def_max} … 舞萌/中二各自独立生效",
                "",
                btn_row("j", "j+1", "j-1", "mai+1", "chu+1"),
            ]
        )

        # 持久化数据存于 data/plugin_data/<plugin_name>/，防止更新插件时被覆盖
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = data_dir / "queue.json"
        self.data = self._load()

    # ==================== 存储 ====================

    def _load(self) -> dict:
        try:
            if self.data_file.exists():
                with open(self.data_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if isinstance(d, dict):
                        return d
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 读取数据文件失败: {e}")
        return {}

    def _save(self):
        try:
            tmp = str(self.data_file) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_file)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存数据文件失败: {e}")

    def _chat(self, key: str) -> dict:
        """获取(或创建)某聊天的数据，key 为 unified_msg_origin，各聊天隔离"""
        return self.data.setdefault(key, {})

    @staticmethod
    def _grp(chat: dict, g: str) -> dict:
        """某组排卡数据 (g 取 mai / chu / tot)"""
        return chat.setdefault(
            g, {"p": 0, "u": False, "t": 0, "uid": "", "name": "", "est": False}
        )

    # ==================== 机厅别名 ====================

    @staticmethod
    def _aliases(chat: dict) -> list:
        return chat.setdefault("aliases", [])

    def _match_alias(self, chat: dict, name: str) -> bool:
        return any(normalize(a) == name for a in self._aliases(chat))

    def _alias_line(self, chat: dict, md: bool = False) -> str:
        al = self._aliases(chat)
        if not al:
            return ""
        return "🏷 机厅别名：" + " / ".join(md_escape(a) if md else a for a in al)

    # ==================== 排卡数据 (mai/chu 分游戏 + tot 合计) ====================

    def _machines(self, chat: dict, g: str) -> int:
        if g == "mai":
            return int(chat.get("maim", self.def_mai))
        return int(chat.get("chum", self.def_chu))

    def _tot_machines(self, chat: dict) -> int:
        m = self._machines(chat, "mai") + self._machines(chat, "chu")
        return m if m >= 1 else 1

    def _tot_max_cards(self, chat: dict, max_cards: int) -> int:
        """合计上限 = 单组上限 × 有机台的游戏数"""
        games = (1 if self._machines(chat, "mai") > 0 else 0) + (
            1 if self._machines(chat, "chu") > 0 else 0
        )
        return max_cards * max(games, 1)

    def _max_cards(self, chat: dict) -> int:
        return int(chat.get("max", self.def_max))

    def _reset_daily(self, chat: dict) -> bool:
        """舞萌 / 中二 / 合计 各自独立跨天清零，任何消息到达都会检查"""
        today = datetime.now().strftime("%Y-%m-%d")
        changed = False
        for g in ("mai", "chu", "tot"):
            d = self._grp(chat, g)
            t = int(d.get("t", 0))
            if t == 0:
                continue
            if datetime.fromtimestamp(t).strftime("%Y-%m-%d") != today:
                d["p"] = 0
                d["u"] = False
                d["est"] = False
                changed = True
        return changed

    def _write_cards(
        self, chat: dict, g: str, cards: int, now: int, uid: str, name: str, est: bool = False
    ):
        d = self._grp(chat, g)
        d["p"] = cards
        d["u"] = True
        d["t"] = now
        d["uid"] = uid
        d["name"] = name
        d["est"] = est

    def _cur_total(self, chat: dict) -> int:
        """当前合计卡数: 未记录过合计时以 舞萌+中二 之和为基准"""
        tot = self._grp(chat, "tot")
        if tot["u"]:
            return int(tot["p"])
        return int(self._grp(chat, "mai")["p"]) + int(self._grp(chat, "chu")["p"])

    def _distribute_total(
        self, chat: dict, total: int, now: int, uid: str, name: str, max_cards: int
    ) -> list:
        """直接更新合计后，反向推算分游戏数据。返回用于回复的说明行:
        - 有效期内更新过的分游戏数据视为可信并保留 (两个都可信时保留更新的那个)，
          另一游戏用 合计-可信值 推算并标注「预计」;
        - 没有任何可信分游戏数据时，合计优先全加在舞萌上 (超上限溢出到中二)，
          太久远的旧数据保持不变。"""
        fresh_secs = self.fresh_hours * 3600
        eligible = [g for g in ("mai", "chu") if self._machines(chat, g) > 0]
        lines = []
        if not eligible:
            return lines

        def is_fresh(g: str) -> bool:
            d = self._grp(chat, g)
            return bool(d["u"]) and now - int(d["t"]) <= fresh_secs

        def infer(g: str, v: int):
            v = min(max(v, 0), max_cards)
            self._write_cards(chat, g, v, now, uid, name, est=True)
            lines.append(f"{GAME_ICON[g]} {game_label(g)}：预计 {v} 卡（该数据可能不准确）")

        if len(eligible) == 1:
            # 只有一个游戏有机台: 它的可信数据优先，否则整体推算给它
            g = eligible[0]
            if not is_fresh(g):
                infer(g, total)
            return lines

        fresh_games = [g for g in eligible if is_fresh(g)]
        if fresh_games:
            if len(fresh_games) == 2:
                # 最新的数据优先保留，较早的一个吸收差值
                t_mai = int(self._grp(chat, "mai")["t"])
                t_chu = int(self._grp(chat, "chu")["t"])
                anchor = "mai" if t_mai > t_chu else "chu"
            else:
                anchor = fresh_games[0]
            other = "chu" if anchor == "mai" else "mai"
            kept = self._grp(chat, anchor)
            lines.append(
                f"{GAME_ICON[anchor]} {game_label(anchor)}："
                f"沿用 {time_diff(kept['t'])}前数据（{int(kept['p'])} 卡）"
            )
            infer(other, total - int(kept["p"]))
            return lines

        # 没有可信的分游戏数据: 优先全加在舞萌上，溢出给中二；太久远的旧数据不变
        mai_v = min(max(total, 0), max_cards)
        infer("mai", mai_v)
        overflow = total - mai_v
        if overflow > 0:
            infer("chu", overflow)
        return lines

    # ==================== 展示 ====================

    def _game_line(self, chat: dict, g: str, md: bool = False) -> str:
        m = self._machines(chat, g)
        if m <= 0:
            return ""  # 无机台不显示
        label, icon = game_label(g), GAME_ICON[g]
        d = self._grp(chat, g)
        if not d["u"]:
            return f"{icon} {label}：暂无排卡数据"
        cards = int(d["p"])
        est = bool(d.get("est"))
        if md:
            head = (
                f"**{icon} {label}：{'预计 ' if est else ''}{cards} 卡**"
                f"（{m}台 · 机均 {avg_cards(cards, m)}）"
            )
            if est:
                sub = f"> ⚠ 推算数据，可能不准确（{time_diff(d['t'])}前）"
            else:
                sub = f"> ⏱ {time_diff(d['t'])}前 由 {md_escape(d['name'])} 更新"
            return head + "\n" + sub
        head = (
            f"{icon} {label}：{'预计 ' if est else ''}{cards} 卡"
            f"（{m}台 · 机均 {avg_cards(cards, m)}）"
        )
        if est:
            sub = f"　⚠ 推算数据，可能不准确（{time_diff(d['t'])}前）"
        else:
            sub = f"　⏱ {time_diff(d['t'])}前 由 {d['name']} 更新"
        return head + "\n" + sub

    def _total_line(self, chat: dict, md: bool = False) -> str:
        tot = self._grp(chat, "tot")
        if tot["u"]:
            cards = int(tot["p"])
            m = self._tot_machines(chat)
            if md:
                return (
                    f"**🧮 合计：{cards} 卡**（{m}台 · 机均 {avg_cards(cards, m)}）\n"
                    f"> ⏱ {time_diff(tot['t'])}前 由 {md_escape(tot['name'])} 更新"
                )
            return (
                f"🧮 合计：{cards} 卡（{m}台 · 机均 {avg_cards(cards, m)}）\n"
                f"　⏱ {time_diff(tot['t'])}前 由 {tot['name']} 更新"
            )
        total = int(self._grp(chat, "mai")["p"]) + int(self._grp(chat, "chu")["p"])
        return f"**🧮 合计：{total} 卡**" if md else f"🧮 合计：{total} 卡"

    def _overview(self, chat: dict, md: bool = False) -> str:
        mai_u = self._grp(chat, "mai")["u"]
        chu_u = self._grp(chat, "chu")["u"]
        tot_u = self._grp(chat, "tot")["u"]
        if md:
            # 引用块后需空行隔断，防止后续内容被并入引用
            lines = ["## 🎪 机厅数据如下"]
            if not mai_u and not chu_u and not tot_u:
                lines.append("当前还没有人更新过排卡数据")
            else:
                for g in ("mai", "chu"):
                    gl = self._game_line(chat, g, md=True)
                    if gl:
                        lines.append(gl)
                        lines.append("")
                lines.append("***")
                lines.append(self._total_line(chat, md=True))
                lines.append("")
            al = self._alias_line(chat, md=True)
            if al:
                lines.append(al)
            lines.append("***")
            lines.append(btn_row("j+1", "j-1", "mai+1", "chu+1", "排卡帮助"))
            return "\n".join(lines)
        lines = ["🎪 机厅数据如下", DIV]
        if not mai_u and not chu_u and not tot_u:
            lines.append("当前还没有人更新过排卡数据")
        else:
            mai_line = self._game_line(chat, "mai")
            chu_line = self._game_line(chat, "chu")
            if mai_line:
                lines.append(mai_line)
            if chu_line:
                lines.append(chu_line)
            lines.append(DIV)
            lines.append(self._total_line(chat))
        al = self._alias_line(chat)
        if al:
            lines.append(al)
        lines.append(DIV)
        lines.append("💡 j+1 加卡 · j-1 减卡 · mai+1 / chu+1 分游戏")
        lines.append("📖 发送「排卡帮助」查看全部指令")
        return "\n".join(lines)

    # ==================== 消息处理 ====================

    def _use_md(self, event: AstrMessageEvent) -> bool:
        """QQ 官方机器人平台 (qq_official / qq_official_webhook) 下启用 Markdown 排版"""
        if not self.md_enabled:
            return False
        try:
            return str(event.get_platform_name() or "").startswith("qq_official")
        except Exception:
            return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        md = self._use_md(event)
        try:
            res = self._handle(event, md)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 处理消息出错: {e}", exc_info=True)
            return
        if res is None:
            return
        text, with_at = res
        chain = []
        if with_at and event.get_group_id():
            # 群聊回复艾特操作人，私聊不带
            if md:
                # 官方适配器发送时会丢弃 At 组件，Markdown 下改用官方艾特标签
                chain.append(
                    Comp.Plain(f'<qqbot-at-user id="{event.get_sender_id()}" />\n{text}')
                )
            else:
                chain.append(Comp.At(qq=event.get_sender_id()))
                chain.append(Comp.Plain(" " + text))
        else:
            chain.append(Comp.Plain(text))
        yield event.chain_result(chain)
        # 已作为排卡指令处理，阻止事件继续传播 (不再触发 LLM / 其他插件)
        event.stop_event()

    def _handle(self, event: AstrMessageEvent, md: bool = False):
        """解析并执行排卡指令。返回 (回复文本, 是否艾特操作人)，非指令返回 None。
        md=True 时输出 QQ 官方 Markdown 排版"""
        raw = strip_codes(event.message_str or "")
        cmd = normalize(raw)  # 无空白，精确指令
        if not cmd:
            return None

        # ---------- 自身/回流防护 ----------
        # 机器人自己的消息不当指令 (帮助/总览文本含 mai+1 等示例)
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return None
        except Exception:
            pass
        # 转发/引用本插件回复也不当指令
        if "机厅数据如下" in cmd or "排卡指令帮助" in cmd or "排卡功能已开启" in cmd:
            return None

        key = event.unified_msg_origin
        is_admin = event.is_admin()

        # ---------- 开关: 仅管理员 (非管理员静默忽略) ----------
        if cmd == "开启排卡":
            if not is_admin:
                return None
            chat = self._chat(key)
            if chat.get("enabled"):
                return ("本聊天已开启排卡功能", False)
            chat["enabled"] = True
            self._save()
            if md:
                head = "**✅ 排卡功能已开启**，开始统计本聊天的排卡数据\n"
                return (head + self.note_md, False)
            return ("✅ 排卡功能已开启，开始统计本聊天的排卡数据\n" + self.note, False)

        if cmd == "关闭排卡":
            if not is_admin:
                return None
            chat = self.data.get(key)
            if chat and chat.get("enabled"):
                chat["enabled"] = False
                self._save()
                return ("排卡功能已关闭", False)
            return ("本聊天未开启排卡功能", False)

        # ---------- 聊天隔离 ----------
        chat = self.data.get(key)
        if not chat or not chat.get("enabled"):
            return None

        if self._reset_daily(chat):
            self._save()

        # ---------- 帮助 ----------
        if cmd in ("排卡帮助", "帮助排卡"):
            return (self.help_md if md else self.help_text, True)

        # ---------- 机厅别名查询 ----------
        if cmd in ("机厅别名", "别名列表"):
            line = self._alias_line(chat, md)
            if line:
                return (line, True)
            # Markdown 下角括号会被识别为标签，示例写法改用 xx
            tip = "添加机厅别名 xx" if md else "添加机厅别名 <别名>"
            return (f"暂无机厅别名，管理员可使用 {tip} 添加", True)

        # ---------- 以下管理指令仅管理员可用 ----------
        if cmd.startswith("添加机厅别名") or cmd.startswith("删除机厅别名"):
            if not is_admin:
                return None
            is_add = cmd.startswith("添加机厅别名")
            args = split_args(raw)
            if len(args) < 2:
                fmt = f"{'添加' if is_add else '删除'}机厅别名"
                example = f"{fmt} xx" if md else f"{fmt} <别名>"
                return (f"指令错误，格式: {example}", True)
            alias = args[1].replace(",", "").strip()
            if not alias:
                return ("别名不能为空", True)
            al = self._aliases(chat)
            if is_add:
                if self._match_alias(chat, normalize(alias)):
                    return ("已存在该别名", True)
                al.append(alias)
                self._save()
                return ("✅ 别名已添加\n" + self._alias_line(chat, md), True)
            na = normalize(alias)
            for i, a in enumerate(al):
                if normalize(a) == na:
                    al.pop(i)
                    self._save()
                    return ("✅ 别名已移除", True)
            return ("没有该别名", True)

        if cmd.startswith("设置机台"):
            if not is_admin:
                return None
            rest = cmd[4:]  # 去掉 "设置机台"
            if rest.startswith("数"):
                rest = rest[1:]
            mm = RE_MACHINES.fullmatch(rest)
            if not mm:
                return ("格式错误，示例: 设置机台 mai2 chu1", True)
            mai_n, chu_n = int(mm.group(1)), int(mm.group(2))
            if mai_n < 0 or chu_n < 0 or mai_n + chu_n < 1:
                return ("两种机台至少保留 1 台", True)
            chat["maim"] = mai_n
            chat["chum"] = chu_n
            self._save()
            return (f"✅ 机台已设置：舞萌DX {mai_n} 台 · 中二节奏 {chu_n} 台", True)

        if cmd.startswith("设置排卡上限"):
            if not is_admin:
                return None
            rest = cmd[6:]
            if not re.fullmatch(r"\d+", rest):
                return ("格式错误，示例: 设置排卡上限30", True)
            n = int(rest)
            if n < 1:
                return ("排卡上限至少为 1", True)
            chat["max"] = n
            self._save()
            return (f"✅ 排卡上限已设置为 {n}", True)

        max_cards = self._max_cards(chat)

        # ---------- j: 排卡总览 ----------
        if cmd in ("j", "机厅几人"):
            return (self._overview(chat, md), True)

        # ---------- mai几 / chu几: 单游戏查询 ----------
        qm = RE_QUERY.fullmatch(cmd)
        if qm:
            g = "chu" if qm.group(1) in ("chu", "中二") else "mai"
            label, icon = game_label(g), GAME_ICON[g]
            m = self._machines(chat, g)
            if m <= 0:
                return (f"本机厅没有{label}机台", True)
            d = self._grp(chat, g)
            if not d["u"]:
                return (f"{label} 当前还没有排卡数据", True)
            cards = int(d["p"])
            t = int(d["t"])
            est = bool(d.get("est"))
            if md:
                lines = [
                    f"**{icon} {label}：{'预计 ' if est else ''}{cards} 卡**"
                    f"（{m}台 · 机均 {avg_cards(cards, m)}）"
                ]
                if est:
                    lines.append("> ⚠ 该数据由合计推算，可能不准确")
                lines.append(
                    f"> ⏱ 由 {md_escape(d['name'])} ({d['uid']}) 更新于 "
                    f"{fmt_time(t)}（{time_diff(t)}前）"
                )
                lines.append("")
                lines.append(btn_row(f"{g}+1", f"{g}-1", "j"))
                return ("\n".join(lines), True)
            lines = [
                f"{icon} {label}：{'预计 ' if est else ''}{cards} 卡"
                f"（{m}台 · 机均 {avg_cards(cards, m)}）"
            ]
            if est:
                lines.append("⚠ 该数据由合计推算，可能不准确")
            lines.append(f"⏱ 由 {d['name']} ({d['uid']}) 更新于 {fmt_time(t)}（{time_diff(t)}前）")
            return ("\n".join(lines), True)

        # ---------- <别名>几 / 机厅几: 排卡总览 ----------
        for suf in ("几人", "几卡", "几"):
            if cmd.endswith(suf) and len(cmd) > len(suf):
                name = cmd[: -len(suf)]
                if name == "机厅" or self._match_alias(chat, name):
                    return (self._overview(chat, md), True)
                break  # 非别名继续走后面的智能匹配

        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or uid

        # ---------- j±n / <别名>±n: 机厅合计更新 (反向推算分游戏数据) ----------
        tot_op = tot_num = None
        t = RE_J_UPD.fullmatch(cmd)
        if t:
            tot_op, tot_num = t.group(1), t.group(2)
        if tot_num is None:
            t = RE_ALIAS_OP.fullmatch(cmd)
            if t and self._match_alias(chat, t.group(1)):
                w = t.group(2)
                if w in ("加", "+"):
                    tot_op = "+"
                elif w in ("减", "-"):
                    tot_op = "-"
                else:
                    tot_op = "="
                tot_num = t.group(3)
        if tot_num is None:
            # <别名><数字> 直接设置
            t = RE_ALIAS_NUM.fullmatch(cmd)
            if t and self._match_alias(chat, t.group(1)):
                tot_op, tot_num = "=", t.group(2)
        if tot_num is not None:
            nxt = apply_op(self._cur_total(chat), tot_op, int(tot_num))
            if nxt < 0 or nxt > self._tot_max_cards(chat, max_cards):
                return ("干什么！", True)
            now = int(time.time())
            self._write_cards(chat, "tot", nxt, now, uid, uname)
            infer_lines = self._distribute_total(chat, nxt, now, uid, uname, max_cards)
            self._save()
            m = self._tot_machines(chat)
            if md:
                lines = [
                    f"**✅ {fmt_time(now)} 更新成功**",
                    f"> 🧮 机厅合计：{nxt} 卡（{m}台 · 机均 {avg_cards(nxt, m)}）",
                ]
                lines.extend(f"> {ln}" for ln in infer_lines)
                lines.append("")
                lines.append(btn_row("j", "j+1", "j-1"))
                return ("\n".join(lines), True)
            lines = [
                f"✅ {fmt_time(now)} 更新成功",
                f"🧮 机厅合计：{nxt} 卡（{m}台 · 机均 {avg_cards(nxt, m)}）",
            ]
            lines.extend(infer_lines)
            return ("\n".join(lines), True)

        # ---------- mai/chu 更新: 支持单条消息同时更新两个游戏 ----------
        mai_op = mai_num = chu_op = chu_num = None

        # 1) 整条消息即指令: mai3 / mai+2 / mai3chu2 / chu-1mai+2 / 舞萌3，中二2 等
        #    (裸数字仅此形式生效，两段间可用逗号/顿号等分隔，顺序可换)
        t = RE_COMBO_MAI.fullmatch(cmd)
        if t:
            mai_op, mai_num = t.group(2), t.group(3)
            if t.group(4) is not None:
                chu_op, chu_num = t.group(6), t.group(7)
        else:
            t = RE_COMBO_CHU.fullmatch(cmd)
            if t:
                chu_op, chu_num = t.group(2), t.group(3)
                if t.group(4) is not None:
                    mai_op, mai_num = t.group(6), t.group(7)

        # 2) 智能匹配: 消息中任意位置的 mai+1 / chu-2 (必须带 +/-/= 操作符;
        #    超长消息不匹配，防止回复/转发/长聊天误触发)
        if mai_num is None and chu_num is None:
            loose = normalize_keep(raw).strip()  # 保留空白作词边界
            if len(loose) <= self.smart_max:
                im = RE_INLINE_MAI.search(loose)
                if im:
                    mai_op, mai_num = im.group(2), im.group(3)
                ic = RE_INLINE_CHU.search(loose)
                if ic:
                    chu_op, chu_num = ic.group(2), ic.group(3)

        if mai_num is None and chu_num is None:
            return None

        mai_m = self._machines(chat, "mai")
        chu_m = self._machines(chat, "chu")
        if mai_num is not None and mai_m <= 0:
            return ("本机厅没有舞萌DX机台，可使用 设置机台 配置", True)
        if chu_num is not None and chu_m <= 0:
            return ("本机厅没有中二节奏机台，可使用 设置机台 配置", True)

        # 两项全部校验通过才写入 (原子性)
        old_mai = int(self._grp(chat, "mai")["p"])
        old_chu = int(self._grp(chat, "chu")["p"])
        next_mai, next_chu = old_mai, old_chu
        if mai_num is not None:
            next_mai = apply_op(old_mai, mai_op, int(mai_num))
            if next_mai < 0 or next_mai > max_cards:
                return ("干什么！", True)
        if chu_num is not None:
            next_chu = apply_op(old_chu, chu_op, int(chu_num))
            if next_chu < 0 or next_chu > max_cards:
                return ("干什么！", True)

        now = int(time.time())
        if mai_num is not None:
            self._write_cards(chat, "mai", next_mai, now, uid, uname)
        if chu_num is not None:
            self._write_cards(chat, "chu", next_chu, now, uid, uname)

        # 把本次增减的差值同步进机厅合计 (未记录过合计时以更新前两游戏之和为基准)
        tot = self._grp(chat, "tot")
        tot_base = int(tot["p"]) if tot["u"] else old_mai + old_chu
        new_tot = tot_base + (next_mai - old_mai) + (next_chu - old_chu)
        new_tot = max(new_tot, 0)
        new_tot = min(new_tot, self._tot_max_cards(chat, max_cards))
        self._write_cards(chat, "tot", new_tot, now, uid, uname)
        self._save()

        if md:
            lines = [f"**✅ {fmt_time(now)} 更新成功**"]
            if mai_num is not None:
                lines.append(
                    f"> {GAME_ICON['mai']} 舞萌DX：{next_mai} 卡（机均 {avg_cards(next_mai, mai_m)}）"
                )
            if chu_num is not None:
                lines.append(
                    f"> {GAME_ICON['chu']} 中二节奏：{next_chu} 卡（机均 {avg_cards(next_chu, chu_m)}）"
                )
            lines.append(f"> 🧮 机厅合计：{new_tot} 卡")
            btns = ["j"]
            if mai_num is not None:
                btns += ["mai+1", "mai-1"]
            if chu_num is not None:
                btns += ["chu+1", "chu-1"]
            lines.append("")
            lines.append(btn_row(*btns))
            return ("\n".join(lines), True)

        lines = [f"✅ {fmt_time(now)} 更新成功"]
        if mai_num is not None:
            lines.append(
                f"{GAME_ICON['mai']} 舞萌DX：{next_mai} 卡（机均 {avg_cards(next_mai, mai_m)}）"
            )
        if chu_num is not None:
            lines.append(
                f"{GAME_ICON['chu']} 中二节奏：{next_chu} 卡（机均 {avg_cards(next_chu, chu_m)}）"
            )
        lines.append(f"🧮 机厅合计：{new_tot} 卡")
        return ("\n".join(lines), True)

    async def terminate(self):
        """插件被卸载/停用时保存数据"""
        self._save()
