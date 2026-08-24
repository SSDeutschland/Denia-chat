# -*- coding: utf-8 -*-
"""泊松插话实验室（假前端）——调参看触发节奏，看顺了再接进桥。

跑法：
  GUI/venv/Scripts/python.exe tools/qq-bridge/poisson_lab.py [--port 8792]
  浏览器开 http://127.0.0.1:8792

  --selftest   无头自测（各场景闸门断言）后退出

页面：滑块调参（λ基数/α/半衰期/冷却/日上限/静默时段）+ 五种流量场景，
点"跑模拟"用真实 poisson.PoissonClock 模拟 N 天逐分钟跑，canvas 画
消息密度/heat/λ/中签点 + 统计。同种子可复现，调参对比直接改种子重跑。

零依赖（stdlib http.server），模拟是纯 Python 瞬跑不等真实时间。
"""
import argparse
import json
import math
import random
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from poisson import PoissonClock, ConversationTracker  # noqa: E402

# ════════════════════ 流量场景生成 ════════════════════
# 每个生成器返回 {分钟序号: 该分钟消息数}，t 从 0 起，一天 1440 分钟。

def _scatter(rng, counts, t0, t1, n):
    for _ in range(n):
        t = rng.randrange(t0, t1)
        counts[t] = counts.get(t, 0) + 1


def gen_dead(rng, days):
    """死群：几天拢共几条，还都在白天。"""
    c = {}
    _scatter(rng, c, 9 * 60, 22 * 60, 5)
    return c


def gen_normal(rng, days):
    """熟人小群日常：早高峰一小撮+午后零星+晚高峰一堆，夜间基本没人。"""
    c = {}
    for d in range(days):
        base = d * 1440
        _scatter(rng, c, base + 8 * 60, base + 10 * 60, rng.randrange(3, 8))
        _scatter(rng, c, base + 13 * 60, base + 18 * 60, rng.randrange(2, 7))
        _scatter(rng, c, base + 20 * 60, base + 23 * 60, rng.randrange(8, 18))
        if rng.random() < 0.4:
            _scatter(rng, c, base + 23 * 60, base + 24 * 60, 1)
    return c


def gen_burst(rng, days):
    """爆发：平时安静，第二天晚上 30 分钟 40 条激烈讨论。"""
    c = {}
    for d in range(days):
        _scatter(rng, c, d * 1440 + 10 * 60, d * 1440 + 22 * 60,
                 rng.randrange(2, 6))
    if days >= 2:
        _scatter(rng, c, 1440 + 20 * 60, 1440 + 20 * 60 + 30, 40)
    else:
        _scatter(rng, c, 20 * 60, 20 * 60 + 30, 40)
    return c


def gen_heated(rng, days):
    """持续热聊：每晚 19~21 点约 5 条/分钟，其余零星。"""
    c = {}
    for d in range(days):
        base = d * 1440
        for t in range(base + 19 * 60, base + 21 * 60):
            c[t] = c.get(t, 0) + rng.randrange(3, 8)
        _scatter(rng, c, base + 9 * 60, base + 18 * 60, rng.randrange(3, 8))
    return c


def gen_night(rng, days):
    """夜间局：凌晨 2~3 点 15 条（静默时段应全挡），白天一丢丢。"""
    c = {}
    for d in range(days):
        base = d * 1440
        _scatter(rng, c, base + 2 * 60, base + 3 * 60, 15)
        _scatter(rng, c, base + 14 * 60, base + 16 * 60, 2)
    return c


SCENARIOS = {
    "dead": ("死群（几天没几条）", gen_dead),
    "normal": ("日常（熟人小群早晚高峰）", gen_normal),
    "burst": ("爆发（30分钟40条激烈讨论）", gen_burst),
    "heated": ("持续热聊（5条/分钟×2小时）", gen_heated),
    "night": ("夜间局（凌晨2点刷屏）", gen_night),
}

# ════════════════════ 模拟 ════════════════════

def simulate(p):
    days = p["days"]
    rng = random.Random(p["seed"])
    _, gen = SCENARIOS[p["scenario"]]
    counts = gen(rng, days)
    clk = PoissonClock(
        base_per_min=p["base_per_hour"] / 60.0,
        alpha=p["alpha"], halflife_min=p["halflife"],
        cooldown_min=p["cooldown"], daily_cap=p["daily_cap"],
        quiet_hours=(p["quiet_start"], p["quiet_end"]),
        min_heat=p["min_heat"], rng=random.Random(p["seed"] + 1))
    trk = ConversationTracker(
        window_min=p["engage_window"], silent_quit=p["silent_quit"],
        max_engaged_min=p["max_engaged"])
    her = random.Random(p["seed"] + 2)              # 她的接话/静默抽签
    q = p["talk_prob"]                              # 实验室没有 LLM，用概率代她的兴趣
    total = days * 1440
    lam_s, heat_s, msg_s, fires = [], [], [], []
    talks, engaged_s = [], []
    batches = 0                                     # 会话中免@直投的批次数
    for t in range(total):
        hour, day = (t // 60) % 24, t // 1440
        trk.check_timeout(t)
        n = counts.get(t, 0)
        for _ in range(n):
            clk.on_message(t)
        if n and trk.on_message(t):                 # 会话中：免@直投一批
            batches += 1
            if her.random() < q:
                trk.on_her_reply(t)
                talks.append(t)
            else:
                trk.on_silent(t)
        if not trk.engaged and clk.tick(t, hour, day):   # 会话中压制泊松
            fires.append(t)
            if her.random() < q:                    # 她接话 → 进入会话态
                trk.on_her_reply(t)
                talks.append(t)
        lam_s.append(round(clk.lam(t) * 60, 4))     # 换算成 次/小时 好读
        heat_s.append(round(clk.heat, 3))
        msg_s.append(n)
        engaged_s.append(1 if trk.engaged else 0)
    intervals = [b - a for a, b in zip(fires, fires[1:])]
    per_day = [sum(1 for f in fires if d * 1440 <= f < (d + 1) * 1440)
               for d in range(days)]
    convs = trk.conv_lengths
    stats = {
        "total_fires": len(fires),
        "per_day": per_day,
        "avg_per_day": round(len(fires) / days, 2),
        "mean_interval": (round(sum(intervals) / len(intervals), 1)
                          if intervals else None),
        "min_interval": min(intervals) if intervals else None,
        "total_msgs": sum(msg_s),
        "convs": len(convs),
        "avg_conv": round(sum(convs) / len(convs), 1) if convs else None,
        "batches": batches,
        "talks": len(talks),
        "exits": dict(trk.exits),
    }
    return {"fires": fires, "lam": lam_s, "heat": heat_s, "msgs": msg_s,
            "engaged": engaged_s, "talks": talks,
            "stats": stats, "days": days,
            "quiet": [p["quiet_start"], p["quiet_end"]]}


# ════════════════════ 页面 ════════════════════

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>泊松插话实验室</title>
<style>
 body{font:14px/1.6 sans-serif;max-width:1080px;margin:20px auto;padding:0 16px;
      background:#1e1f24;color:#dde}
 h1{font-size:20px} h2{font-size:15px;margin:14px 0 6px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
       gap:8px 20px}
 label{display:flex;justify-content:space-between;align-items:center;gap:8px}
 input[type=range]{flex:1}
 .val{min-width:52px;text-align:right;color:#8cf}
 select,input[type=number]{background:#2a2c33;color:#dde;border:1px solid #444;
       border-radius:4px;padding:2px 6px}
 button{background:#3d7eff;color:#fff;border:0;border-radius:6px;
       padding:8px 22px;font-size:15px;cursor:pointer}
 button:hover{background:#5a90ff}
 canvas{width:100%;background:#14151a;border-radius:8px;margin-top:10px}
 #stats{white-space:pre-wrap;background:#14151a;border-radius:8px;
       padding:10px 14px;margin-top:10px;font-family:Consolas,monospace}
 .tip{color:#99a;font-size:12px}
</style></head><body>
<h1>🧪 泊松插话实验室 <span class="tip">λ = 基数 × (1 + α × 热度EMA)，每分钟抽签；红三角=投递【看看群里】</span></h1>

<h2>场景</h2>
<label>群流量 <select id="scenario"></select></label>
<div class="grid">
 <label>模拟天数 <input type="number" id="days" value="3" min="1" max="7" style="width:60px"></label>
 <label>随机种子 <input type="number" id="seed" value="42" style="width:80px"></label>
</div>

<h2>参数</h2>
<div class="grid">
 <label>λ基数(次/时) <input type="range" id="base_per_hour" min="0.1" max="6" step="0.1" value="1"><span class="val"></span></label>
 <label>热度系数 α <input type="range" id="alpha" min="0" max="6" step="0.2" value="2"><span class="val"></span></label>
 <label>半衰期(分) <input type="range" id="halflife" min="5" max="120" step="5" value="30"><span class="val"></span></label>
 <label>冷却(分) <input type="range" id="cooldown" min="0" max="120" step="5" value="20"><span class="val"></span></label>
 <label>日上限 <input type="range" id="daily_cap" min="1" max="30" step="1" value="8"><span class="val"></span></label>
 <label>最低热度 <input type="range" id="min_heat" min="0" max="2" step="0.1" value="0.5"><span class="val"></span></label>
 <label>静默起(时) <input type="range" id="quiet_start" min="0" max="23" step="1" value="0"><span class="val"></span></label>
 <label>静默止(时) <input type="range" id="quiet_end" min="0" max="24" step="1" value="8"><span class="val"></span></label>
</div>

<h2>会话态（盯手机）<span class="tip">她说话后进入：窗口内群消息免@直投，她可接话或[静默]</span></h2>
<div class="grid">
 <label>会话窗口(分) <input type="range" id="engage_window" min="1" max="15" step="1" value="4"><span class="val"></span></label>
 <label>失趣退出(次) <input type="range" id="silent_quit" min="1" max="5" step="1" value="2"><span class="val"></span></label>
 <label>会话硬顶(分) <input type="range" id="max_engaged" min="10" max="60" step="5" value="30"><span class="val"></span></label>
 <label>她接话概率 <input type="range" id="talk_prob" min="0" max="1" step="0.05" value="0.6"><span class="val"></span></label>
</div>
<p><button id="run">跑模拟</button>
<span class="tip"> 灰柱=群消息(条/15分) · 橙线=热度heat · 蓝线=λ(次/时) · 灰底=静默时段 · 绿底=会话中 · ▲=中签 · ▼=她说话</span></p>
<canvas id="cv" width="1040" height="380"></canvas>
<div id="stats">（还没跑）</div>

<script>
const $ = id => document.getElementById(id);
const SEL = Object.entries(%SCEN%);
for (const [k,v] of SEL) $('scenario').append(new Option(v,k));
$('scenario').value = 'normal';
for (const el of document.querySelectorAll('input[type=range]')) {
  const show = () => el.parentElement.querySelector('.val').textContent = el.value;
  el.oninput = show; show();
}
function params() {
  const o = {};
  for (const id of ['scenario','days','seed','base_per_hour','alpha','halflife',
      'cooldown','daily_cap','min_heat','quiet_start','quiet_end',
      'engage_window','silent_quit','max_engaged','talk_prob'])
    o[id] = $(id).value;
  return new URLSearchParams(o).toString();
}
function fmtT(t){const d=Math.floor(t/1440)+1,h=String(Math.floor(t/60)%24).padStart(2,'0'),
  m=String(t%60).padStart(2,'0');return `第${d}天 ${h}:${m}`;}
async function run(){
  $('stats').textContent = '模拟中…';
  const r = await (await fetch('/simulate?'+params())).json();
  draw(r);
  const s = r.stats;
  $('stats').textContent =
    `总消息 ${s.total_msgs} 条 · 中签 ${s.total_fires} 次（日均 ${s.avg_per_day}，逐日 ${JSON.stringify(s.per_day)}）\n` +
    (s.mean_interval!=null
      ? `间隔：均值 ${s.mean_interval} 分 · 最短 ${s.min_interval} 分\n`
      : `（不足两次，无间隔统计）\n`) +
    `会话 ${s.convs} 次（平均 ${s.avg_conv==null?'-':s.avg_conv} 分）· ` +
    `她说话 ${s.talks} 次 · 免@直投 ${s.batches} 批 · ` +
    `退出[超时${s.exits.timeout}/失趣${s.exits.silent}/硬顶${s.exits.cap}]\n` +
    '中签时刻：' + (r.fires.length ? r.fires.map(fmtT).join('　') : '无');
}
function draw(r){
  const cv=$('cv'), ctx=cv.getContext('2d'), W=cv.width, H=cv.height, T=r.msgs.length;
  ctx.clearRect(0,0,W,H);
  const x = t => t/T*W;
  // 静默时段底纹
  ctx.fillStyle='#23242c';
  const [q0,q1]=r.quiet;
  for(let d=0;d<r.days;d++){
    if(q0<q1){ctx.fillRect(x(d*1440+q0*60),0,x(d*1440+q1*60)-x(d*1440+q0*60),H);}
    else if(q0>q1){ctx.fillRect(x(d*1440+q0*60),0,x((d+1)*1440)-x(d*1440+q0*60),H);
                   ctx.fillRect(x(d*1440),0,x(d*1440+q1*60)-x(d*1440),H);}
  }
  // 会话中绿底
  if(r.engaged){
    ctx.fillStyle='rgba(60,180,90,0.13)';
    let st=-1;
    for(let t=0;t<=T;t++){
      const on = t<T && r.engaged[t];
      if(on && st<0) st=t;
      if(!on && st>=0){ctx.fillRect(x(st),0,x(t)-x(st),H);st=-1;}
    }
  }
  // 消息柱（15分桶）
  const B=15, nb=Math.ceil(T/B); let mmax=1; const buck=[];
  for(let i=0;i<nb;i++){let s=0;for(let j=0;j<B;j++)s+=r.msgs[i*B+j]||0;buck.push(s);if(s>mmax)mmax=s;}
  ctx.fillStyle='#5a5d68';
  for(let i=0;i<nb;i++){const h=buck[i]/mmax*(H*0.45);ctx.fillRect(x(i*B),H-h,Math.max(1,x(B)-1),h);}
  // 中线参考
  ctx.strokeStyle='#333';ctx.beginPath();ctx.moveTo(0,H*0.55);ctx.lineTo(W,H*0.55);ctx.stroke();
  // heat 橙线（上半）
  const hmax=Math.max(1,...r.heat);
  ctx.strokeStyle='#f90';ctx.lineWidth=1.2;ctx.beginPath();
  r.heat.forEach((v,t)=>{const y=H*0.55-v/hmax*(H*0.5);t?ctx.lineTo(x(t),y):ctx.moveTo(x(t),y);});
  ctx.stroke();
  // λ 蓝线（上半）
  const lmax=Math.max(0.5,...r.lam);
  ctx.strokeStyle='#4af';ctx.lineWidth=1.2;ctx.beginPath();
  r.lam.forEach((v,t)=>{const y=H*0.55-v/lmax*(H*0.5);t?ctx.lineTo(x(t),y):ctx.moveTo(x(t),y);});
  ctx.stroke();
  // 中签红三角
  ctx.fillStyle='#f44';
  for(const f of r.fires){ctx.beginPath();ctx.moveTo(x(f),H-14);
    ctx.lineTo(x(f)-5,H-2);ctx.lineTo(x(f)+5,H-2);ctx.fill();}
  // 她说话绿三角（顶部朝下）
  if(r.talks){ctx.fillStyle='#3cb45a';
    for(const t of r.talks){ctx.beginPath();ctx.moveTo(x(t),16);
      ctx.lineTo(x(t)-5,4);ctx.lineTo(x(t)+5,4);ctx.fill();}}
  // 刻度
  ctx.fillStyle='#778';ctx.font='11px sans-serif';
  for(let d=0;d<=r.days;d++)ctx.fillText('第'+(d+1)+'天',x(d*1440)+3,12);
}
$('run').onclick=run; run();
</script></body></html>"""

# ════════════════════ 服务 ════════════════════

PARAM_DEFAULTS = {
    "scenario": "normal", "days": 3, "seed": 42,
    # B 参数组（2026-08-18 A/B 同种子对比拍板：热度主导 + 会话粘性，
    # 桥侧同名配置三处同步以此为准）
    "base_per_hour": 0.3, "alpha": 4.0, "halflife": 45,
    "cooldown": 25, "daily_cap": 6, "min_heat": 1.0,
    "quiet_start": 0, "quiet_end": 8,
    "engage_window": 8, "silent_quit": 3, "max_engaged": 45,
    "talk_prob": 0.6,
}


def parse_params(qs):
    q = parse_qs(qs)
    p = dict(PARAM_DEFAULTS)
    for k in p:
        if k in q:
            v = q[k][0]
            if k == "scenario":
                p[k] = v if v in SCENARIOS else p[k]
            elif k in ("base_per_hour", "alpha", "min_heat", "talk_prob"):
                p[k] = float(v)
            else:
                p[k] = int(float(v))
    p["days"] = max(1, min(7, p["days"]))
    return p


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = PAGE.replace("%SCEN%",
                json.dumps([[k, v[0]] for k, v in SCENARIOS.items()],
                           ensure_ascii=False)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif u.path == "/simulate":
            body = json.dumps(simulate(parse_params(u.query)),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            self.send_response(404)
            body = b"404"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ════════════════════ 自测 ════════════════════

def selftest():
    fails = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    base = dict(PARAM_DEFAULTS)

    # 页面 JS 健全性：Python 端 \n 逃逸曾把真换行泄进 JS 单引号串，
    # 整页 SyntaxError 停在"还没跑"（2026-08-18 实坑）。正则测不准
    # （闭引号误报），有 node 就直接 --check 提取出的脚本。
    import shutil, subprocess, tempfile, os
    node = shutil.which("node")
    if node:
        rendered = PAGE.replace("%SCEN%", "[]")
        js = re.search(r"<script>(.*)</script>", rendered, re.S).group(1)
        fd, jspath = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        rc = subprocess.run([node, "--check", jspath],
                            capture_output=True).returncode
        os.unlink(jspath)
        check("页面 JS 语法（node --check）", rc == 0)
    else:
        print("SKIP 页面 JS 语法（本机无 node）")
    r = simulate({**base, "scenario": "night"})
    in_quiet = [f for f in r["fires"] if 0 <= (f // 60) % 24 < 8]
    check("夜间局：中签全在静默时段外", not in_quiet
          and r["stats"]["total_fires"] >= 0)

    r_dead = simulate({**base, "scenario": "dead"})
    r1 = simulate({**base, "scenario": "normal"})
    check("死群远少于日常（有动静才投，非全挡）",
          r_dead["stats"]["total_fires"] < r1["stats"]["total_fires"])

    r = simulate({**base, "scenario": "burst"})
    fires = r["fires"]
    near = [f for f in fires if 1440 + 20 * 60 - 60 <= f <= 1440 + 21 * 60 + 60]
    check(f"爆发场景中签 {len(fires)} 次且贴着爆发段 {len(near)} 次",
          1 <= len(fires) and len(near) >= 1)

    r = simulate({**base, "scenario": "heated"})
    check("热聊场景逐日都不超日上限",
          all(n <= base["daily_cap"] for n in r["stats"]["per_day"]))
    check("热聊场景每天都有中签",
          all(n >= 1 for n in r["stats"]["per_day"]))

    r2 = simulate({**base, "scenario": "normal"})
    check("同参数同种子可复现", r1["fires"] == r2["fires"])
    check("日常场景中签数不离谱（0~8/天）",
          all(0 <= n <= 8 for n in r1["stats"]["per_day"]))

    r1_hi = simulate({**base, "scenario": "normal", "daily_cap": 100})
    r3 = simulate({**base, "scenario": "normal", "base_per_hour": 6,
                   "daily_cap": 100})
    check("λ基数拉高中签显著变多（cap 放开对比）",
          r3["stats"]["total_fires"] > r1_hi["stats"]["total_fires"])

    r4 = simulate({**base, "scenario": "normal", "cooldown": 120})
    same_day = [(a, b) for a, b in zip(r4["fires"], r4["fires"][1:])
                if a // 1440 == b // 1440]
    check("冷却120分：同日间隔都≥120",
          all(b - a >= 120 for a, b in same_day))

    # —— 两态（会话中）——
    r_q0 = simulate({**base, "scenario": "heated", "talk_prob": 0,
                     "daily_cap": 100})
    check("她永不接话：零会话零直投",
          r_q0["stats"]["convs"] == 0 and r_q0["stats"]["batches"] == 0
          and not any(r_q0["engaged"]))

    r_q1 = simulate({**base, "scenario": "heated", "talk_prob": 1,
                     "daily_cap": 100})
    check("她必接话：有会话且时长都不超硬顶",
          r_q1["stats"]["convs"] >= 1
          and r_q1["stats"]["avg_conv"] <= base["max_engaged"])
    check("会话中压制泊松：必接话时中签更少（cap 放开对比）",
          r_q1["stats"]["total_fires"] < r_q0["stats"]["total_fires"])
    check("热聊必接话：直投批次>0",
          r_q1["stats"]["batches"] > 0)

    r5 = simulate({**base, "scenario": "normal", "talk_prob": 0.6})
    r6 = simulate({**base, "scenario": "normal", "talk_prob": 0.6})
    check("两态同种子可复现", r5["fires"] == r6["fires"]
          and r5["talks"] == r6["talks"])

    print(f"\n{'全过' if not fails else '有 FAIL：' + str(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    PAGE = PAGE  # noqa: F841
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"泊松插话实验室 → http://127.0.0.1:{args.port}  （Ctrl+C 关）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
