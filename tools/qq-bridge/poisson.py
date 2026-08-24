# -*- coding: utf-8 -*-
"""免@插话泊松时钟（一期：时间×热度）。

设计（2026-08-17 草案 + 08-18 用户拍板先实验）：
  λ = base_per_min × (1 + alpha × heat)
  heat = 群消息到达率的指数滑动平均——每来一条消息 heat+1，
         按半衰期 halflife_min 持续衰减（heat≈1 ≈ "每半衰期一条"的节奏）。
  每分钟 tick() 一次抽签，中了 = 投递【看看群里】（她可回[静默]，
  兴趣判断外包给她，一期不做 LLM 兴趣评估）。

闸门（任一不满足即不中签，λ 再高也没用）：
  - 静默时段 quiet_hours（前闭后开，本地小时）
  - 发言冷却 cooldown_min（上次中签后 N 分钟内不再中）
  - 日上限 daily_cap（按自然日重置）
  - 无热度不开口：heat < min_heat 时房间是死的，不硬聊

冷却/日上限都挂在"中签"（=投递）而不是"她说话"——她连续[静默]时
时钟不该越抽越频繁地追着她问。

时间轴约定：t 为模拟分钟序号（浮点也可），hour/day 由调用方给
（桥用真实时钟，实验室用模拟时钟，本类不碰 datetime/wall time）。
"""
import math
import random


class PoissonClock:
    def __init__(self, base_per_min=1 / 60, alpha=2.0, halflife_min=30,
                 cooldown_min=20, daily_cap=8, quiet_hours=(0, 8),
                 min_heat=0.5, rng=None):
        self.base = float(base_per_min)
        self.alpha = float(alpha)
        self.halflife = float(halflife_min)
        self.cooldown = float(cooldown_min)
        self.daily_cap = int(daily_cap)
        self.quiet = tuple(quiet_hours or ())      # (起, 止) 前闭后开
        self.min_heat = float(min_heat)
        self.rng = rng or random.Random()
        self.heat = 0.0
        self._decayed_to = None                    # heat 衰减结算到哪个 t
        self.last_fire = None
        self.fires_today = 0
        self._day = None

    # ---- 热度 EMA ----

    def _decay(self, t):
        """惰性衰减：heat 只在被观察时结算到 t。"""
        if self._decayed_to is None:
            self._decayed_to = t
            return
        dt = t - self._decayed_to
        if dt > 0:
            self.heat *= math.exp(-math.log(2) * dt / self.halflife)
            self._decayed_to = t

    def on_message(self, t):
        """群里来一条消息（自己的不算，调用方过滤）。"""
        self._decay(t)
        self.heat += 1.0

    def lam(self, t):
        """当前每分钟中签率（先结算衰减再读）。"""
        self._decay(t)
        return self.base * (1.0 + self.alpha * self.heat)

    # ---- 抽签 ----

    def _in_quiet(self, hour):
        if len(self.quiet) < 2:
            return False
        q0, q1 = int(self.quiet[0]), int(self.quiet[1])
        if q0 == q1:
            return False
        if q0 < q1:
            return q0 <= hour < q1
        return hour >= q0 or hour < q1              # 跨零点（如 22~6）

    def tick(self, t, hour, day):
        """每分钟调用一次。返回 True = 中签（该投递【看看群里】了）。"""
        if day != self._day:                        # 跨自然日重置日计数
            self._day = day
            self.fires_today = 0
        if self._in_quiet(hour):
            return False
        if self.last_fire is not None and t - self.last_fire < self.cooldown:
            return False
        if self.fires_today >= self.daily_cap:
            return False
        p = min(self.lam(t), 1.0)
        if self.heat < self.min_heat:               # 房间是死的，不硬聊
            return False
        if self.rng.random() < p:
            self.last_fire = t
            self.fires_today += 1
            return True
        return False


class ConversationTracker:
    """两态模型的第二态"会话中"：她发出群回复后进入盯手机模式，
    窗口期内群消息免@直投（她可[静默]，兴趣过滤继续外包给她）。

    退出取先到者：
      超时 —— 窗口 window_min 内群里没人再说话（每条消息滑动窗口）
      失趣 —— 连续 silent_quit 次[静默]（她说一句话清零）
      硬顶 —— 总会话时长 max_engaged_min（防热聊群把她钉死一整晚+控成本）

    与泊松钟的咬合：会话中调用方应跳过该群 tick（都在聊了还"看看群里"
    就精分了）；会话中的回复不占泊松的冷却/日上限——那是管自发起跳的，
    对话回应不该被罚。
    """

    def __init__(self, window_min=4, silent_quit=2, max_engaged_min=30):
        self.window = float(window_min)
        self.silent_quit = int(silent_quit)
        self.max_engaged = float(max_engaged_min)
        self.engaged_since = None
        self.last_activity = None
        self.silent_streak = 0
        self.exits = {"timeout": 0, "silent": 0, "cap": 0}
        self.conv_lengths = []

    @property
    def engaged(self):
        return self.engaged_since is not None

    def _exit(self, t, reason):
        self.exits[reason] += 1
        self.conv_lengths.append(t - self.engaged_since)
        self.engaged_since = None
        self.last_activity = None
        self.silent_streak = 0

    def on_her_reply(self, t):
        """她在群里说话 → 进入/续期会话态（清失趣计数）。"""
        if not self.engaged:
            self.engaged_since = t
        self.last_activity = t
        self.silent_streak = 0

    def on_message(self, t):
        """群消息（自己的不算）：会话中 → True=免@直投给她并滑动窗口。"""
        if not self.engaged:
            return False
        self.last_activity = t
        return True

    def on_silent(self, t):
        """她对一批直投消息回[静默]：连续 N 次 = 失趣退出。"""
        if not self.engaged:
            return
        self.silent_streak += 1
        if self.silent_streak >= self.silent_quit:
            self._exit(t, "silent")

    def check_timeout(self, t):
        """每分钟调用：硬顶优先于超时。"""
        if not self.engaged:
            return
        if t - self.engaged_since >= self.max_engaged:
            self._exit(t, "cap")
        elif t - self.last_activity >= self.window:
            self._exit(t, "timeout")


# ---- 自测（python poisson.py）：闸门与单调性 ----

if __name__ == "__main__":
    fails = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    # 1. 无热度不开口：base 拉到 1.0（必中）也不中
    c = PoissonClock(base_per_min=1.0, quiet_hours=(), rng=random.Random(1))
    check("无热度必不中", not any(c.tick(t, 12, 0) for t in range(60)))

    # 2. 静默时段挡：heat 拉满也不中；跨零点时段两端都挡
    c = PoissonClock(base_per_min=1.0, quiet_hours=(22, 6), rng=random.Random(1))
    for t in range(10):
        c.on_message(t)
    check("静默 23 点挡", not any(c.tick(t, 23, 0) for t in range(10, 70)))
    check("静默 3 点挡", not any(c.tick(t, 3, 0) for t in range(10, 70)))
    check("静默外 12 点中", any(c.tick(t, 12, 0) for t in range(10, 70)))

    # 3. 冷却：base=1 必中场景，中后冷却期内不中
    c = PoissonClock(base_per_min=1.0, cooldown_min=20, quiet_hours=(),
                     min_heat=0.0, rng=random.Random(1))
    c.on_message(0)
    check("首次即中", c.tick(1, 12, 0))
    check("冷却期内不中", not any(c.tick(t, 12, 0) for t in range(2, 21)))
    check("冷却后再中", c.tick(21, 12, 0))

    # 4. 日上限：cap=2，第三天仍 2 次（跨日重置）
    c = PoissonClock(base_per_min=1.0, cooldown_min=1, daily_cap=2,
                     quiet_hours=(), min_heat=0.0, rng=random.Random(1))
    c.on_message(0)
    n0 = sum(c.tick(t, 12, 0) for t in range(1, 100))
    n1 = sum(c.tick(t, 12, 1) for t in range(100, 200))
    check(f"日上限 day0={n0} day1={n1} 都=2", n0 == 2 and n1 == 2)

    # 5. 爆发抬 λ：10 连击后 λ 显著高于底噪
    c = PoissonClock(base_per_min=1 / 60, alpha=2.0, halflife_min=30)
    lam_cold = c.lam(0)
    for t in range(10):
        c.on_message(t)
    check("爆发后 λ 抬升 >10x", c.lam(10) > 10 * lam_cold)

    # 6. 衰减：半衰期后 heat 减半
    c = PoissonClock(halflife_min=30)
    c.on_message(0)
    c._decay(30)
    check("半衰期 heat 减半", abs(c.heat - 0.5) < 1e-9)

    # 7. 种子确定性：同种子同序列
    seq = []
    for _ in range(2):
        c = PoissonClock(base_per_min=0.05, alpha=2.0, cooldown_min=5,
                         rng=random.Random(42))
        for t in range(0, 200, 7):
            c.on_message(t)
        seq.append([t for t in range(600) if c.tick(t, 12, 0)])
    check(f"同种子同结果 fires={seq[0]}", seq[0] == seq[1])

    # 8. 会话态：说话进入，窗口内免@直投，群里没人说话超时退出
    tr = ConversationTracker(window_min=4, silent_quit=2, max_engaged_min=30)
    check("未进会话不免@", not tr.on_message(0))
    tr.on_her_reply(1)
    check("说话进入会话", tr.engaged)
    check("窗口内直投", tr.on_message(3))
    for t in range(4, 10):
        tr.check_timeout(t)
    check("超时退出（4分无人说话）", not tr.engaged
          and tr.exits["timeout"] == 1)

    # 9. 失趣：连续2次[静默]退出；中间说话清零
    tr = ConversationTracker(window_min=100, silent_quit=2)
    tr.on_her_reply(0)
    tr.on_silent(1)
    check("一次静默不退", tr.engaged)
    tr.on_her_reply(2)                                # 说话清零
    tr.on_silent(3)
    check("说话清失趣计数", tr.engaged)
    tr.on_silent(4)
    check("连续两次静默失趣退出", not tr.engaged
          and tr.exits["silent"] == 1)

    # 10. 硬顶优先于超时（群里一直在聊也被顶出去）
    tr = ConversationTracker(window_min=100, max_engaged_min=30)
    tr.on_her_reply(0)
    tr.on_message(29)
    tr.check_timeout(29)
    alive = tr.engaged
    tr.on_message(30)
    tr.check_timeout(30)
    check("硬顶30分退出", alive and not tr.engaged
          and tr.exits["cap"] == 1)

    print(f"\n{'全过' if not fails else '有 FAIL：' + str(fails)}")
    raise SystemExit(0 if not fails else 1)
