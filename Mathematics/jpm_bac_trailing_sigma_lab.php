<?php
declare(strict_types=1);

/* Read-only daily data source.  The browser receives only the final common bar
   of each UTC date, then computes every trailing statistic locally. */
header('Content-Type: text/html; charset=utf-8');
$rows = [];
$loadError = null;
try {
    $db = new PDO(
        'mysql:host=localhost;dbname=quant_data;charset=utf8mb4',
        'quant',
        '1q2w3e',
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]
    );
    $query = $db->query("SELECT y.time_utc AS time_utc, y.price AS jpm, x.price AS bac
        FROM us_market_prices y
        INNER JOIN us_market_prices x ON x.ticker = 'BAC' AND x.time_utc = y.time_utc
        WHERE y.ticker = 'JPM' AND y.time_utc >= '2022-01-01'
          AND y.price IS NOT NULL AND x.price IS NOT NULL
        ORDER BY y.time_utc ASC");
    foreach ($query as $row) {
        $rows[] = [(string)$row['time_utc'], (float)$row['jpm'], (float)$row['bac']];
    }
    if (count($rows) < 21 * 50) {
        throw new RuntimeException('共同的 5 分钟 bar 不足以建立 20 日统计量。');
    }
} catch (Throwable $e) {
    $loadError = $e->getMessage();
}
?>
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JPM / BAC · ±2σ 手动交易实验</title>
<style>
:root{--bg:#07101b;--panel:#0d1928;--line:#24364d;--ink:#e8eef8;--muted:#aebdd0;--blue:#53b6ff;--white:#f7f9fc;--red:#ff7474;--green:#67dbac;--amber:#ffc86b}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 35% -20%,#153452,#07101b 45%);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif}
main{max-width:1380px;margin:auto;padding:28px 22px 50px}.eyebrow{color:var(--green);font-size:12px;letter-spacing:.13em;text-transform:uppercase}h1{margin:2px 0 6px;font-size:31px;line-height:1.2}p{color:var(--muted);margin:7px 0}.rule{color:#cbd8e9}.grid{display:grid;grid-template-columns:360px minmax(0,1fr);gap:18px;margin-top:20px}.card{background:rgba(13,25,40,.92);border:1px solid var(--line);border-radius:13px;padding:18px;box-shadow:0 12px 32px #02070c55}.control{margin:15px 0}.control label{display:block;font-weight:650;margin-bottom:6px}.control small{display:block;color:var(--muted);margin-top:5px}select,input{width:100%;background:#07121f;color:var(--ink);border:1px solid #36506e;border-radius:8px;padding:9px;font:inherit}.signal-nav{display:grid;grid-template-columns:44px 1fr 44px;gap:7px}.signal-nav button{padding:7px;background:#284766;color:var(--ink);font-size:21px}.signal-nav button:disabled{opacity:.35;cursor:not-allowed}button{width:100%;padding:10px;border:0;border-radius:8px;background:var(--green);color:#062019;font-weight:750;font:inherit;cursor:pointer}button:hover{filter:brightness(1.1)}.trade-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.secondary{background:#284766;color:var(--ink)}.info{border-top:1px solid var(--line);margin-top:17px;padding-top:14px}.info strong{color:var(--white)}.trade{min-height:164px}.tag{display:inline-block;border-radius:20px;padding:2px 8px;margin-right:6px;font-size:12px;font-weight:700}.long{color:#042018;background:var(--green)}.short{color:#2c0b0b;background:var(--red)}.open{color:#312205;background:var(--amber)}canvas{display:block;width:100%;height:510px;cursor:crosshair}.legend{display:flex;flex-wrap:wrap;gap:11px;color:var(--muted);font-size:13px;margin:0 0 7px}.dot{width:10px;height:3px;display:inline-block;vertical-align:middle;margin-right:4px}.note{font-size:13px;color:var(--muted);border-left:3px solid var(--amber);padding-left:10px;margin-top:14px}.error{color:#ff9797;background:#341820;padding:15px;border-radius:9px}@media(max-width:930px){.grid{grid-template-columns:1fr}main{padding:18px 12px}canvas{height:430px}}
main{max-width:1920px}.grid{display:block}.grid>aside{margin-top:18px}.headline-pnl{min-height:25px;margin:5px 0;color:var(--amber);font-weight:700}.trade-actions{max-width:300px}.trade{min-height:0}canvas{height:590px;cursor:default}.ledger-table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}.ledger-table th,.ledger-table td{padding:8px;text-align:left;border-bottom:1px solid var(--line)}.ledger-table th{color:var(--muted);font-weight:650}.pnl-positive{color:var(--green)}.pnl-negative{color:var(--red)}
textarea{width:100%;min-height:70px;background:#07121f;color:var(--ink);border:1px solid #36506e;border-radius:8px;padding:9px;font:inherit}.research-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.research-actions button{background:#284766;color:var(--ink)}
</style>
</head>
<body><main>
  <div class="eyebrow">hand-run pairs trading laboratory</div>
  <h1>JPM / BAC · 手动读取头回测器</h1>
  <p id="headlinePnl" class="headline-pnl">未持仓 · 按 Enter 在读取头建仓</p>
  <p class="rule">固定 <b>beta = 1.07693</b> · 20 日 trailing ±2σ · 39 根 5min bar 的价格均线回穿 · <b>spread = JPM<sub>norm</sub> − beta × BAC<sub>norm</sub></b></p>
  <p>不生成机器入场候选点。所有建仓与平仓均由你在读取头位置手动决定。</p>
<?php if ($loadError !== null): ?>
  <div class="error">无法载入 JPM/BAC 共同日线：<?= htmlspecialchars($loadError, ENT_QUOTES, 'UTF-8') ?></div>
<?php else: ?>
  <div class="grid"><section class="card"><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>5min spread</span><span><i class="dot" style="background:#b087ff"></i>39-bar price mean</span><span><i class="dot" style="background:white"></i>20d trailing mean</span><span><i class="dot" style="background:var(--red)"></i>±2σ</span><span><i class="dot" style="background:var(--green)"></i>盈利交易</span><span><i class="dot" style="background:var(--amber)"></i>亏损交易</span></div><canvas id="chart"></canvas><p id="caption"></p></section><aside class="card">
    <div class="control" hidden><div class="signal-nav"><button id="previous">←</button><select id="signal"></select><button id="next">→</button></div></div><input id="stopMultiple" type="hidden" value="1"><output id="stopOut" hidden>1.00</output>
    <div class="control"><label for="span">图上显示的附近 5 分钟 bar：<output id="spanOut">1300</output></label><input id="span" type="range" min="260" max="3120" step="260" value="1300"></div>
    <div class="control"><label for="speed">前进速度：每次 <output id="speedOut">5</output> 根 5 分钟 bar</label><input id="speed" type="range" min="1" max="60" step="1" value="5"><small>点击图表或按 → 时，读取头按此速度前进。</small></div>
    <div class="trade-actions"><button id="enter">按 Enter 建仓</button><button id="sideToggle" hidden></button><button id="resetTrade" hidden></button></div>
    <div class="info trade" id="trade"></div>
    <div class="info" id="ledger"></div>
    <div class="note">统计带没有未来函数：每个交易日盘中使用前 20 个已完成交易日的 spread 计算。读取头右侧的未来行情被遮挡；只可用 → 或点击图表向前读取。</div>
  </aside></div>
<?php endif; ?>
</main>
<?php if ($loadError === null): ?><script id="daily-version-code" type="text/plain">
const raw = <?= json_encode($rows, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>;
const beta = 1.07693, windowSize = 20;
const firstJ = raw[0][1], firstB = raw[0][2];
const points = raw.map(r => ({date:r[0], jpm:r[1]/firstJ, bac:r[2]/firstB, spread:r[1]/firstJ-beta*r[2]/firstB}));
for (let i=windowSize-1;i<points.length;i++) {
  const xs=points.slice(i-windowSize+1,i+1).map(p=>p.spread), mean=xs.reduce((a,b)=>a+b,0)/windowSize;
  const sigma=Math.sqrt(xs.reduce((a,x)=>a+(x-mean)**2,0)/(windowSize-1));
  Object.assign(points[i],{mean,sigma,lower2:mean-2*sigma,upper2:mean+2*sigma,lower1:mean-sigma,upper1:mean+sigma});
}
/* A pair return is one day's spread P/L divided by the initial gross exposure
   (1 + beta).  At day t, sigmaReturn uses only the last 20 such returns. */
for (let i=windowSize;i<points.length;i++) {
  const rs=[];
  for(let k=i-windowSize+1;k<=i;k++) rs.push((points[k].spread-points[k-1].spread)/(1+beta));
  const avg=rs.reduce((a,b)=>a+b,0)/windowSize;
  points[i].sigmaReturn=Math.sqrt(rs.reduce((a,r)=>a+(r-avg)**2,0)/(windowSize-1));
}
const signals=[];
for(let i=windowSize;i<points.length;i++) {
  const a=points[i-1],b=points[i];
  if(a.spread<a.lower2 && b.spread>=b.lower2) signals.push({i,side:'long'});
  if(a.spread>a.upper2 && b.spread<=b.upper2) signals.push({i,side:'short'});
}
const selector=document.querySelector('#signal'), span=document.querySelector('#span'), spanOut=document.querySelector('#spanOut'), previous=document.querySelector('#previous'), next=document.querySelector('#next');
signals.forEach((s,n)=>{const p=points[s.i],o=document.createElement('option');o.value=n;o.textContent=`${p.date} · ${s.side==='long'?'向上回穿下轨 → 做多':'向下回穿上轨 → 做空'}`;selector.append(o)});
let chosen=signals.length-1, entered=false;
function fmt(n){return n.toFixed(5)}
function simulate(s,multiple=s.stopMultiple){
  const entry=points[s.i], sign=s.side==='long'?1:-1; let peakReturn=0;
  for(let j=s.i+1;j<points.length;j++){
    const p=points[j], holdingReturn=sign*(p.spread-entry.spread)/(1+beta);
    peakReturn=Math.max(peakReturn,holdingReturn);
    if(s.side==='long' && p.spread>=p.mean)return {j,why:'止盈：收盘 spread 回到 / 越过 trailing mean'};
    if(s.side==='short' && p.spread<=p.mean)return {j,why:'止盈：收盘 spread 回到 / 越过 trailing mean'};
    if(holdingReturn<=peakReturn-p.sigmaReturn)return {j,why:'止损：当前 return 跌到 peak return − 1σ(return)'};
  } return null;
}
function stopPath(s){
  const entry=points[s.i], sign=s.side==='long'?1:-1, levels=[]; let peakReturn=0;
  for(let j=s.i+1;j<points.length;j++){
    const p=points[j], holdingReturn=sign*(p.spread-entry.spread)/(1+beta);
    peakReturn=Math.max(peakReturn,holdingReturn);
    levels.push({j,value:entry.spread+sign*(peakReturn-p.sigmaReturn)*(1+beta)});
  } return levels;
}
function tradeText(){
  const s=signals[chosen], p=points[s.i], exit=simulate(s), side=s.side==='long'?'做多 spread':'做空 spread';
  const sign=s.side==='long'?1:-1;
  let h=`<span class="tag ${s.side}">${side}</span><p><strong>建仓日：</strong>${p.date}<br><strong>建仓 spread：</strong>${fmt(p.spread)}<br><strong>当日 mean：</strong>${fmt(p.mean)} · level σ：${fmt(p.sigma)}<br><strong>当日 σ(return)：</strong>${(100*p.sigmaReturn).toFixed(3)}%<br><strong>规则：</strong>回到 mean 止盈；从持仓 peak return 回撤 1σ(return) 止损。</p>`;
  if(entered && exit){const q=points[exit.j], pnl=sign*(q.spread-p.spread);h+=`<p><strong>逐日回放结果：</strong>${q.date}<br>${exit.why}<br><strong>spread P/L：</strong>${pnl>=0?'+':''}${fmt(pnl)}</p>`}
  else if(entered)h+=`<p><span class="tag open">未平仓</span>数据末日以前未触发这两条收盘规则。</p>`;
  else h+=`<p>现在尚未建仓。先读图，再点击“建仓（演练）”。</p>`;
  document.querySelector('#trade').innerHTML=h;
}
const canvas=document.querySelector('#chart'),ctx=canvas.getContext('2d');
function chart(){
  const s=signals[chosen], half=Number(span.value)/2, lo=Math.max(windowSize-1,Math.floor(s.i-half)), hi=Math.min(points.length-1,Math.ceil(s.i+half)), view=points.slice(lo,hi+1), W=canvas.clientWidth,H=canvas.clientHeight,dpr=devicePixelRatio||1;
  canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);const pad={l:64,r:18,t:16,b:36};
  const nums=[];view.forEach(p=>nums.push(p.spread,p.mean,p.lower2,p.upper2));if(entered)stopPath(s).filter(z=>z.j>=lo&&z.j<=hi).forEach(z=>nums.push(z.value));let mn=Math.min(...nums),mx=Math.max(...nums),extra=(mx-mn)*.10||.01;mn-=extra;mx+=extra;
  const x=i=>pad.l+(i-lo)/(hi-lo)*(W-pad.l-pad.r), y=v=>pad.t+(mx-v)/(mx-mn)*(H-pad.t-pad.b);
  ctx.strokeStyle='#2a3a4e';ctx.lineWidth=1;ctx.font='12px system-ui';ctx.fillStyle='#aebdd0'; for(let n=0;n<5;n++){const v=mn+(mx-mn)*n/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(3),5,yy+4)}
  function line(key,color,width=1,dash=[]){ctx.beginPath();ctx.setLineDash(dash);view.forEach((p,k)=>{const xx=x(lo+k),yy=y(p[key]);k?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([])}
  line('lower2','#ff7474',1,[5,5]);line('upper2','#ff7474',1,[5,5]);line('mean','#f7f9fc',2);line('spread','#53b6ff',2);
  if(entered){const stops=stopPath(s).filter(z=>z.j>=lo&&z.j<=hi);if(stops.length){ctx.beginPath();ctx.setLineDash([3,4]);stops.forEach((z,k)=>k?ctx.lineTo(x(z.j),y(z.value)):ctx.moveTo(x(z.j),y(z.value)));ctx.strokeStyle='#ffc86b';ctx.lineWidth=1.5;ctx.stroke();ctx.setLineDash([])}}
  signals.filter(z=>z.i>=lo&&z.i<=hi).forEach(z=>{ctx.beginPath();ctx.arc(x(z.i),y(points[z.i].spread),3,0,Math.PI*2);ctx.fillStyle=z.side==='long'?'#67dbac':'#ff7474';ctx.fill()});
  const entry=points[s.i];ctx.strokeStyle=s.side==='long'?'#67dbac':'#ff7474';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(s.i),pad.t);ctx.lineTo(x(s.i),H-pad.b);ctx.stroke();ctx.beginPath();ctx.arc(x(s.i),y(entry.spread),6,0,Math.PI*2);ctx.fillStyle=ctx.strokeStyle;ctx.fill();
  if(entered){const e=simulate(s);if(e&&e.j>=lo&&e.j<=hi){const q=points[e.j];ctx.beginPath();ctx.arc(x(e.j),y(q.spread),6,0,Math.PI*2);ctx.fillStyle='#67dbac';ctx.fill()}}
  ctx.fillStyle='#aebdd0';ctx.fillText(points[lo].date,pad.l,H-12);ctx.fillText(points[hi].date,W-pad.r-78,H-12);document.querySelector('#caption').textContent=`视图：${points[lo].date} 至 ${points[hi].date}。竖线是所选回穿日；点击其他回穿点附近可切换。`;
  canvas.onclick=e=>{const rect=canvas.getBoundingClientRect(),i=Math.round(lo+(e.clientX-rect.left-pad.l)/(W-pad.l-pad.r)*(hi-lo));let nearest=-1,d=Infinity;signals.forEach((z,n)=>{const q=Math.abs(z.i-i);if(q<d){d=q;nearest=n}});if(nearest>=0&&d<8)choose(nearest)};
}
function choose(index){chosen=Math.max(0,Math.min(signals.length-1,index));selector.value=chosen;entered=false;previous.disabled=chosen===0;next.disabled=chosen===signals.length-1;tradeText();chart()}
selector.onchange=()=>choose(Number(selector.value));previous.onclick=()=>choose(chosen-1);next.onclick=()=>choose(chosen+1);span.oninput=()=>{spanOut.value=span.value;chart()};document.querySelector('#enter').onclick=()=>{entered=true;tradeText();chart()};
document.addEventListener('keydown',e=>{if(e.altKey||e.ctrlKey||e.metaKey||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key==='ArrowLeft'){e.preventDefault();choose(chosen-1)}if(e.key==='ArrowRight'){e.preventDefault();choose(chosen+1)}});
if(!signals.length){document.querySelector('#trade').innerHTML='<p>当前样本没有识别到回穿事件。</p>';previous.disabled=true;next.disabled=true}else{choose(chosen)} window.addEventListener('resize',chart);
</script>
<script>
/* The preceding, non-executed script carries the server-side JSON. */
const raw = JSON.parse(document.querySelector('#daily-version-code').textContent.match(/const raw = (.*);/)[1]);
const beta = 1.07693, dailyWindow = 20, maBars = 39, gross = 1 + beta;
const closeByDay=new Map();raw.forEach(r=>closeByDay.set(r[0].slice(0,10),r));
const closes=[...closeByDay.values()],baseJ=closes[0][1],baseB=closes[0][2];
const daily=closes.map(r=>({date:r[0].slice(0,10),spread:r[1]/baseJ-beta*r[2]/baseB})),statistics=new Map();
/* For trading day i, its band uses only the 20 completed daily closes before i. */
for(let i=dailyWindow+1;i<daily.length;i++){
  const levels=daily.slice(i-dailyWindow,i).map(d=>d.spread),mean=levels.reduce((a,b)=>a+b,0)/dailyWindow,returns=[];
  for(let k=i-dailyWindow;k<i;k++)returns.push((daily[k].spread-daily[k-1].spread)/gross);
  const rMean=returns.reduce((a,b)=>a+b,0)/dailyWindow,sigma=Math.sqrt(levels.reduce((a,x)=>a+(x-mean)**2,0)/(dailyWindow-1)),sigmaReturn=Math.sqrt(returns.reduce((a,x)=>a+(x-rMean)**2,0)/(dailyWindow-1));
  statistics.set(daily[i].date,{mean,sigma,sigmaReturn,lower2:mean-2*sigma,upper2:mean+2*sigma});
}
const rawBars=raw.map(r=>({time:r[0],date:r[0].slice(0,10),spread:r[1]/baseJ-beta*r[2]/baseB})),bars=[];
/* The 39-bar price MA includes this just-closed bar; the entry is the next bar. */
for(let i=maBars-1;i<rawBars.length;i++){const stat=statistics.get(rawBars[i].date);if(!stat)continue;const ma39=rawBars.slice(i-maBars+1,i+1).reduce((sum,b)=>sum+b.spread,0)/maBars;bars.push({...rawBars[i],ma39,...stat})}
/* This reader intentionally does not generate algorithmic entry candidates. */
const signals=[];
function simulate(s,multiple=s.stopMultiple){
  const entry=bars[s.entry], sign=s.side==='long'?1:-1; let peak=0;
  for(let j=s.entry+1;j<bars.length;j++){
    const p=bars[j], ret=sign*(p.spread-entry.spread)/gross; peak=Math.max(peak,ret);
    if((s.side==='long'&&p.spread>=p.mean)||(s.side==='short'&&p.spread<=p.mean))return {j,why:'止盈：5 分钟收盘 spread 回到 / 越过当日 20d mean',ret};
    if(ret<=peak-multiple*p.sigmaReturn)return {j,why:`止损：return 从 peak 回撤 ${multiple.toFixed(2)}σ(return)`,ret};
  } return null;
}
function stopPath(s,multiple=s.stopMultiple){const entry=bars[s.entry],sign=s.side==='long'?1:-1,result=[];let peak=0;for(let j=s.entry+1;j<bars.length;j++){const p=bars[j],ret=sign*(p.spread-entry.spread)/gross;peak=Math.max(peak,ret);result.push({j,value:entry.spread+sign*(peak-multiple*p.sigmaReturn)*gross})}return result}
const selector=document.querySelector('#signal'),span=document.querySelector('#span'),spanOut=document.querySelector('#spanOut'),previous=document.querySelector('#previous'),next=document.querySelector('#next'),stopInput=document.querySelector('#stopMultiple'),stopOut=document.querySelector('#stopOut'),resetTrade=document.querySelector('#resetTrade'),enterButton=document.querySelector('#enter');
let chosen=Math.max(0,signals.length-1),entered=false;
const confirmed=[];
function refreshSignal(s,n){s.exit=simulate(s);const last=s.exit?bars[s.exit.j]:bars[bars.length-1],sign=s.side==='long'?1:-1;s.pnl=sign*(last.spread-bars[s.entry].spread)/gross;s.good=s.pnl>=0;if(n!==undefined){const cross=bars[s.cross],entry=bars[s.entry];selector.options[n].textContent=`${cross.time} 39-bar MA 回穿 → ${entry.time} 入场 · ${s.side==='long'?'做多':'做空'} · ${s.good?'盈利':'亏损/未平'}`}}
function refreshResults(){signals.forEach((s,n)=>refreshSignal(s,n))}
signals.forEach((s,n)=>{s.stopMultiple=1;s.confirmed=false;const o=document.createElement('option');o.value=n;selector.append(o)});refreshResults();
const fmt=n=>n.toFixed(5),pct=n=>(100*n).toFixed(3)+'%';
function renderLedger(){const total=confirmed.reduce((sum,r)=>sum+r.pnl,0),body=confirmed.map(r=>{const s=signals[r.n],exit=s.exit?bars[s.exit.j].time:'未平仓';return `<li>${bars[s.entry].time} · ${s.side==='long'?'多':'空'} · ${r.multiple.toFixed(2)}σ · <strong style="color:${r.pnl>=0?'#67dbac':'#ff7474'}">${pct(r.pnl)}</strong> · ${exit}</li>`}).join('');document.querySelector('#ledger').innerHTML=confirmed.length?`<strong>正式建仓账本：</strong>${confirmed.length} 笔<br><strong>总 PnL：</strong><span style="color:${total>=0?'#67dbac':'#ff7474'}">${pct(total)}</span><small>（每笔按相同 1 单位 gross exposure，简单相加）</small><ul>${body}</ul>`:'<strong>正式建仓账本：</strong>尚未确认任何入场点。'}
function tradeText(){const s=signals[chosen],cross=bars[s.cross],p=bars[s.entry],exit=s.exit,side=s.side==='long'?'做多 spread':'做空 spread';let h=`<span class="tag ${s.side}">${side}</span><p><strong>39-bar MA 回穿确认：</strong>${cross.time}<br><strong>实际入场：</strong>下一根 5min bar，${p.time}<br><strong>入场 spread：</strong>${fmt(p.spread)}<br><strong>当日 20d mean：</strong>${fmt(p.mean)} · σ(return)：${pct(p.sigmaReturn)}<br><strong>规则：</strong>回到 20d mean 止盈；peak return 回撤 ${s.stopMultiple.toFixed(2)}σ(return) 止损。</p>`;if(exit)h+=`<p><strong>${entered?'正式持仓回放':'默认规则预览'}：</strong>${bars[exit.j].time}<br>${exit.why}<br><strong>pair return：</strong>${pct(exit.ret)}</p>`;else h+=`<p><span class="tag open">${entered?'正式持仓中':'默认规则预览'}</span>至样本末日的 mark-to-market return：${pct(s.pnl)}</p>`;document.querySelector('#trade').innerHTML=h;renderLedger()}
const canvas=document.querySelector('#chart'),ctx=canvas.getContext('2d');
function chart(){const s=signals[chosen],half=Number(span.value)/2,lo=Math.max(0,Math.floor(s.entry-half)),hi=Math.min(bars.length-1,Math.ceil(s.entry+half)),view=bars.slice(lo,hi+1),W=canvas.clientWidth,H=canvas.clientHeight,dpr=devicePixelRatio||1;canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);const pad={l:64,r:18,t:16,b:36},nums=[];view.forEach(p=>nums.push(p.spread,p.mean,p.lower2,p.upper2));stopPath(s).filter(z=>z.j>=lo&&z.j<=hi).forEach(z=>nums.push(z.value));let mn=Math.min(...nums),mx=Math.max(...nums),extra=(mx-mn)*.1||.01;mn-=extra;mx+=extra;const x=i=>pad.l+(i-lo)/(hi-lo)*(W-pad.l-pad.r),y=v=>pad.t+(mx-v)/(mx-mn)*(H-pad.t-pad.b);ctx.strokeStyle='#2a3a4e';ctx.font='12px system-ui';ctx.fillStyle='#aebdd0';for(let n=0;n<5;n++){const v=mn+(mx-mn)*n/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(3),5,yy+4)}function line(key,color,width=1,dash=[]){ctx.beginPath();ctx.setLineDash(dash);view.forEach((p,k)=>k?ctx.lineTo(x(lo+k),y(p[key])):ctx.moveTo(x(lo+k),y(p[key])));ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([])}line('lower2','#ff7474',1,[5,5]);line('upper2','#ff7474',1,[5,5]);line('mean','#f7f9fc',2);line('spread','#53b6ff',2);const z=stopPath(s).filter(q=>q.j>=lo&&q.j<=hi);ctx.beginPath();ctx.setLineDash([3,4]);z.forEach((q,k)=>k?ctx.lineTo(x(q.j),y(q.value)):ctx.moveTo(x(q.j),y(q.value)));ctx.strokeStyle='#ffc86b';ctx.stroke();ctx.setLineDash([]);const holdingEnd=s.exit?s.exit.j:bars.length-1;signals.filter(z=>z.entry>=lo&&z.entry<=hi&&(!entered||z===s||z.entry<s.entry||z.entry>holdingEnd)).forEach(z=>{ctx.beginPath();ctx.arc(x(z.entry),y(bars[z.entry].spread),3,0,Math.PI*2);ctx.fillStyle=z.good?'#67dbac':'#ff7474';ctx.fill()});ctx.strokeStyle=s.good?'#67dbac':'#ff7474';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(s.entry),pad.t);ctx.lineTo(x(s.entry),H-pad.b);ctx.stroke();ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.arc(x(s.entry),y(bars[s.entry].spread),6,0,Math.PI*2);ctx.fill();if(s.exit&&s.exit.j>=lo&&s.exit.j<=hi){ctx.beginPath();ctx.arc(x(s.exit.j),y(bars[s.exit.j].spread),6,0,Math.PI*2);ctx.fillStyle='#ffc86b';ctx.fill()}ctx.fillStyle='#aebdd0';ctx.fillText(bars[lo].time,pad.l,H-12);ctx.fillText(bars[hi].time,W-pad.r-130,H-12);document.querySelector('#caption').textContent=`视图：${bars[lo].time} 至 ${bars[hi].time}。橙线和出口圆点是所选点在当前规则下的持仓预览；绿/红是回放结果。`;canvas.onclick=e=>{const rect=canvas.getBoundingClientRect(),i=Math.round(lo+(e.clientX-rect.left-pad.l)/(W-pad.l-pad.r)*(hi-lo));let near=-1,d=Infinity;signals.forEach((z,n)=>{const q=Math.abs(z.entry-i);if(q<d){d=q;near=n}});if(d<8)choose(near)}}
/* Redraw with the 39-bar MA explicitly visible (purple). */
function chart(){
  const s=signals[chosen],half=Number(span.value)/2,lo=Math.max(0,Math.floor(s.entry-half)),hi=Math.min(bars.length-1,Math.ceil(s.entry+half)),view=bars.slice(lo,hi+1),W=canvas.clientWidth,H=canvas.clientHeight,dpr=devicePixelRatio||1;
  canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const pad={l:64,r:18,t:16,b:36},stops=stopPath(s).filter(q=>q.j>=lo&&q.j<=hi),nums=[];
  view.forEach(p=>nums.push(p.spread,p.ma39,p.mean,p.lower2,p.upper2));stops.forEach(q=>nums.push(q.value));
  let mn=Math.min(...nums),mx=Math.max(...nums),extra=(mx-mn)*.1||.01;mn-=extra;mx+=extra;
  const x=i=>pad.l+(i-lo)/(hi-lo)*(W-pad.l-pad.r),y=v=>pad.t+(mx-v)/(mx-mn)*(H-pad.t-pad.b);
  ctx.strokeStyle='#2a3a4e';ctx.font='12px system-ui';ctx.fillStyle='#aebdd0';
  for(let n=0;n<5;n++){const v=mn+(mx-mn)*n/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(3),5,yy+4)}
  const line=(key,color,width=1,dash=[])=>{ctx.beginPath();ctx.setLineDash(dash);view.forEach((p,k)=>k?ctx.lineTo(x(lo+k),y(p[key])):ctx.moveTo(x(lo+k),y(p[key])));ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([])};
  line('lower2','#ff7474',1,[5,5]);line('upper2','#ff7474',1,[5,5]);line('mean','#f7f9fc',2);line('ma39','#b087ff',2);line('spread','#53b6ff',2);
  ctx.beginPath();ctx.setLineDash([3,4]);stops.forEach((q,k)=>k?ctx.lineTo(x(q.j),y(q.value)):ctx.moveTo(x(q.j),y(q.value)));ctx.strokeStyle='#ffc86b';ctx.stroke();ctx.setLineDash([]);
  const holdingEnd=s.exit?s.exit.j:bars.length-1;
  signals.filter(z=>z.entry>=lo&&z.entry<=hi&&(!entered||z===s||z.entry<s.entry||z.entry>holdingEnd)).forEach(z=>{ctx.beginPath();ctx.arc(x(z.entry),y(bars[z.entry].spread),3,0,Math.PI*2);ctx.fillStyle=z.good?'#67dbac':'#ff7474';ctx.fill()});
  ctx.strokeStyle=s.good?'#67dbac':'#ff7474';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(s.entry),pad.t);ctx.lineTo(x(s.entry),H-pad.b);ctx.stroke();ctx.beginPath();ctx.arc(x(s.entry),y(bars[s.entry].spread),6,0,Math.PI*2);ctx.fillStyle=ctx.strokeStyle;ctx.fill();
  if(s.exit&&s.exit.j>=lo&&s.exit.j<=hi){ctx.beginPath();ctx.arc(x(s.exit.j),y(bars[s.exit.j].spread),6,0,Math.PI*2);ctx.fillStyle='#ffc86b';ctx.fill()}
  ctx.fillStyle='#aebdd0';ctx.fillText(bars[lo].time,pad.l,H-12);ctx.fillText(bars[hi].time,W-pad.r-130,H-12);
  document.querySelector('#caption').textContent=`视图：${bars[lo].time} 至 ${bars[hi].time}。紫线是 39-bar price MA；只有它回穿 20d ±2σ 才生成候选入场。`;
  canvas.onclick=e=>{const rect=canvas.getBoundingClientRect(),i=Math.round(lo+(e.clientX-rect.left-pad.l)/(W-pad.l-pad.r)*(hi-lo));let near=-1,d=Infinity;signals.forEach((z,n)=>{const q=Math.abs(z.entry-i);if(q<d){d=q;near=n}});if(d<8)choose(near)};
}
function updateControls(){const s=signals[chosen];previous.disabled=chosen===0;next.disabled=chosen===signals.length-1;enterButton.hidden=entered;resetTrade.hidden=!entered;stopInput.disabled=entered;stopInput.value=s.stopMultiple;stopOut.value=s.stopMultiple.toFixed(2)}
function choose(index){chosen=Math.max(0,Math.min(signals.length-1,index));entered=signals[chosen].confirmed;selector.value=chosen;updateControls();tradeText();chart()}
selector.onchange=()=>choose(Number(selector.value));previous.onclick=()=>choose(chosen-1);next.onclick=()=>choose(chosen+1);span.oninput=()=>{spanOut.value=span.value;chart()};enterButton.onclick=()=>{const s=signals[chosen];s.confirmed=true;confirmed.push({n:chosen,multiple:s.stopMultiple,pnl:s.pnl});entered=true;updateControls();tradeText();chart()};resetTrade.onclick=()=>{const s=signals[chosen];s.confirmed=false;const record=confirmed.findIndex(r=>r.n===chosen);if(record>=0)confirmed.splice(record,1);entered=false;updateControls();tradeText();chart()};stopInput.oninput=()=>{const s=signals[chosen];if(s.confirmed)return;s.stopMultiple=Number(stopInput.value);stopOut.value=s.stopMultiple.toFixed(2);refreshSignal(s,chosen);tradeText();chart()};document.addEventListener('keydown',e=>{if(e.altKey||e.ctrlKey||e.metaKey||['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key==='ArrowLeft'){e.preventDefault();choose(chosen-1)}if(e.key==='ArrowRight'){e.preventDefault();choose(chosen+1)}});if(!signals.length){document.querySelector('#trade').innerHTML='<p>当前样本没有识别到可用的 5 分钟回穿事件。</p>';previous.disabled=true;next.disabled=true;enterButton.disabled=true}else choose(chosen);window.addEventListener('resize',chart);
</script><script type="text/plain">
/* Legacy manual-decision layer retained as documentation; the reader below owns interaction. */
selector.closest('.control').style.display='none';
const manualRecords=[];let manualIndex=0,viewCenter=Math.min(650,bars.length-1),hoverIndex=null;
const timeTip=document.createElement('div');timeTip.style.cssText='position:fixed;display:none;pointer-events:none;z-index:9;padding:6px 9px;border:1px solid #49627d;border-radius:6px;background:#07101b;color:#f7f9fc;font:13px system-ui';document.body.append(timeTip);
previous.textContent='←';next.textContent='→';previous.title='向左平移窗口';next.title='向右平移窗口';
function manualSim(i,m){const e=bars[i];let peak=0,sign=1;for(let j=i+1;j<bars.length;j++){const p=bars[j],r=sign*(p.spread-e.spread)/gross;peak=Math.max(peak,r);if(p.spread>=p.mean)return{j,r,why:'回到 20d mean'};if(r<=peak-m*p.sigmaReturn)return{j,r,why:`peak 回撤 ${m}σ`}}return{j:bars.length-1,r:(bars.at(-1).spread-e.spread)/gross,why:'样本末日 MTM'}}
function paint(){const h=Number(span.value)/2,lo=Math.max(0,Math.floor(viewCenter-h)),hi=Math.min(bars.length-1,Math.ceil(viewCenter+h)),v=bars.slice(lo,hi+1),W=canvas.clientWidth,H=canvas.clientHeight,d=devicePixelRatio||1;canvas.width=W*d;canvas.height=H*d;ctx.setTransform(d,0,0,d,0,0);const q=[];v.forEach(p=>q.push(p.spread,p.ma39,p.mean,p.lower2,p.upper2));let mn=Math.min(...q),mx=Math.max(...q),z=(mx-mn)*.1||.01;mn-=z;mx+=z;const x=i=>64+(i-lo)/(hi-lo)*(W-82),y=a=>16+(mx-a)/(mx-mn)*(H-52);ctx.clearRect(0,0,W,H);const L=(k,c,w=1)=>{ctx.beginPath();v.forEach((p,n)=>n?ctx.lineTo(x(lo+n),y(p[k])):ctx.moveTo(x(lo+n),y(p[k])));ctx.strokeStyle=c;ctx.lineWidth=w;ctx.stroke()};L('lower2','#ff7474');L('upper2','#ff7474');L('mean','#fff',2);L('ma39','#b087ff',2);L('spread','#53b6ff',2);ctx.strokeStyle='#67dbac';ctx.beginPath();ctx.moveTo(x(manualIndex),16);ctx.lineTo(x(manualIndex),H-36);ctx.stroke();if(hoverIndex!==null&&hoverIndex>=lo&&hoverIndex<=hi){ctx.strokeStyle='#ffd166';ctx.beginPath();ctx.moveTo(x(hoverIndex),16);ctx.lineTo(x(hoverIndex),H-36);ctx.stroke()}document.querySelector('#caption').textContent=`UTC ${bars[manualIndex].time} ｜ 北京 ${new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'medium',timeStyle:'medium'}).format(new Date(bars[manualIndex].time+'Z'))}。点击 spread 选择入场；← → 平移窗口。`}
const basePaint=paint;paint=()=>{basePaint();const h=Number(span.value)/2,lo=Math.max(0,Math.floor(viewCenter-h)),hi=Math.min(bars.length-1,Math.ceil(viewCenter+h)),W=canvas.clientWidth,H=canvas.clientHeight,q=[];bars.slice(lo,hi+1).forEach(p=>q.push(p.spread,p.ma39,p.mean,p.lower2,p.upper2));let mn=Math.min(...q),mx=Math.max(...q),z=(mx-mn)*.1||.01;mn-=z;mx+=z;const x=i=>64+(i-lo)/(hi-lo)*(W-82),y=a=>16+(mx-a)/(mx-mn)*(H-52);manualRecords.forEach(r=>{if(r.entry>=lo&&r.entry<=hi){ctx.fillStyle=r.side==='long'?'#67dbac':'#ffb86b';ctx.beginPath();ctx.arc(x(r.entry),y(bars[r.entry].spread),6,0,Math.PI*2);ctx.fill();ctx.fillText(r.exit===null?`${r.side==='long'?'多':'空'}·持仓中`:`${r.side==='long'?'多':'空'}入`,x(r.entry)+7,y(bars[r.entry].spread)-7)}if(r.exit!==null&&r.exit>=lo&&r.exit<=hi){ctx.fillStyle='#ff7474';ctx.beginPath();ctx.arc(x(r.exit),y(bars[r.exit].spread),6,0,Math.PI*2);ctx.fill();ctx.fillText('出',x(r.exit)+7,y(bars[r.exit].spread)-7);if(r.entry>=lo&&r.entry<=hi){ctx.strokeStyle=r.side==='long'?'#67dbac':'#ffb86b';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x(r.entry),y(bars[r.entry].spread));ctx.lineTo(x(r.exit),y(bars[r.exit].spread));ctx.stroke();ctx.setLineDash([])}}})};
function slide(dir){const target=Math.max(0,Math.min(bars.length-1,viewCenter+dir*Math.floor(Number(span.value)/3)));const start=viewCenter,t0=performance.now();(function f(t){viewCenter=Math.round(start+(target-start)*Math.min(1,(t-t0)/180));paint();if(t-t0<180)requestAnimationFrame(f)})(t0)}
previous.onclick=()=>slide(-1);next.onclick=()=>slide(1);span.oninput=paint;
canvas.onclick=e=>{const r=canvas.getBoundingClientRect(),h=Number(span.value)/2,lo=Math.max(0,Math.floor(viewCenter-h)),hi=Math.min(bars.length-1,Math.ceil(viewCenter+h));manualIndex=Math.max(lo,Math.min(hi,Math.round(lo+(e.clientX-r.left-64)/(r.width-82)*(hi-lo))));paint()};
canvas.onmousemove=e=>{const r=canvas.getBoundingClientRect(),h=Number(span.value)/2,lo=Math.max(0,Math.floor(viewCenter-h)),hi=Math.min(bars.length-1,Math.ceil(viewCenter+h));hoverIndex=Math.max(lo,Math.min(hi,Math.round(lo+(e.clientX-r.left-64)/(r.width-82)*(hi-lo))));timeTip.textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'medium',timeStyle:'medium'}).format(new Date(bars[hoverIndex].time+'Z'));timeTip.style.display='block';timeTip.style.left=(e.clientX+14)+'px';timeTip.style.top=(e.clientY+14)+'px';paint()};canvas.onmouseleave=()=>timeTip.style.display='none';
enterButton.hidden=false;enterButton.textContent='确认入场';resetTrade.hidden=false;resetTrade.textContent='确认出场';enterButton.onclick=()=>{const side=bars[manualIndex].spread>bars[manualIndex].mean?'short':'long';manualRecords.push({entry:manualIndex,exit:null,side});document.querySelector('#trade').innerHTML=`已手动确认${side==='long'?'做多':'做空'} spread：${bars[manualIndex].time}。请移动到你的出场 bar 后确认出场。`;paint();};resetTrade.onclick=()=>{const r=[...manualRecords].reverse().find(x=>x.exit===null);if(!r)return;r.exit=manualIndex;r.pnl=(r.side==='long'?1:-1)*(bars[r.exit].spread-bars[r.entry].spread)/gross;const closed=manualRecords.filter(x=>x.exit!==null),compound=closed.reduce((a,x)=>a*(1+x.pnl),1)-1;document.querySelector('#ledger').innerHTML=`<strong>正式账本：</strong>${closed.length} 笔已平仓 · 复利总 PnL <strong>${(100*compound).toFixed(3)}%</strong>`;document.querySelector('#trade').innerHTML=`已手动确认出场：${bars[manualIndex].time}；该笔 ${r.side==='long'?'做多':'做空'} spread PnL ${(100*r.pnl).toFixed(3)}%`;paint();};
document.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.stopImmediatePropagation();e.preventDefault();slide(e.key==='ArrowLeft'?-1:1)}},true);paint();
</script><script>
/* One-way tape reader: the right edge is the only actionable market time. */
selector.closest('.control').style.display='none';
previous.closest('.signal-nav').style.display='none';
resetTrade.hidden=true;
const records=[]; let readIndex=Math.min(Math.max(0,Number(span.value)-1),bars.length-1),activeTrade=null;
const chartCard=canvas.closest('.card'),ledger=document.querySelector('#ledger');chartCard.append(ledger);
const stamp=i=>bars[i].time, sideName=s=>s==='long'?'做多 spread':'做空 spread', currentSide=()=>bars[readIndex].spread>bars[readIndex].mean?'short':'long', percent=v=>`${v>=0?'+':''}${(v*100).toFixed(3)}%`;
const currentPnl=()=>activeTrade?(activeTrade.side==='long'?1:-1)*(bars[readIndex].spread-bars[activeTrade.entry].spread)/gross:0;
function renderTrade(){const pnl=currentPnl();document.querySelector('#trade').innerHTML=activeTrade?`<span class="tag ${activeTrade.side}">${sideName(activeTrade.side)}</span><p><strong>建仓：</strong>${stamp(activeTrade.entry)}<br><strong>读取头：</strong>${stamp(readIndex)}<br><strong>读取头浮盈：</strong><span class="${pnl>=0?'pnl-positive':'pnl-negative'}">${percent(pnl)}</span></p><p>按 <strong>Enter</strong> 在当前读取头平仓。</p>`:`<p><strong>读取头：</strong>${stamp(readIndex)}<br><strong>当前建议：</strong>${sideName(currentSide())}</p><p>按 <strong>Enter</strong> 在读取头建仓；之后时间只可向前推进。</p>`;enterButton.textContent=activeTrade?'按 Enter 平仓':'按 Enter 建仓'}
function renderLedger(){const ordered=[...records].sort((a,b)=>a.exit-b.exit),total=ordered.reduce((value,r)=>value*(1+r.pnl),1)-1;ledger.innerHTML=ordered.length?`<strong>已完成交易</strong>　${ordered.length} 笔　累计复利 <span class="${total>=0?'pnl-positive':'pnl-negative'}">${percent(total)}</span><table class="ledger-table"><thead><tr><th>方向</th><th>建仓时间</th><th>平仓时间</th><th>收益</th></tr></thead><tbody>${ordered.map(r=>`<tr><td>${r.side==='long'?'多':'空'}</td><td>${stamp(r.entry)}</td><td>${stamp(r.exit)}</td><td class="${r.pnl>=0?'pnl-positive':'pnl-negative'}">${percent(r.pnl)}</td></tr>`).join('')}</tbody></table>`:'<strong>已完成交易</strong><p>尚无已平仓交易。</p>'}
function paint(){const count=Number(span.value),hi=readIndex,lo=Math.max(0,hi-count+1),view=bars.slice(lo,hi+1),W=canvas.clientWidth,H=canvas.clientHeight,dpr=devicePixelRatio||1;canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);const pad={l:64,r:88,t:52,b:38},values=[];view.forEach(p=>values.push(p.spread,p.ma39,p.mean,p.lower2,p.upper2));let min=Math.min(...values),max=Math.max(...values),extra=(max-min)*.1||.01;min-=extra;max+=extra;const x=i=>pad.l+(i-lo)/Math.max(1,hi-lo)*(W-pad.l-pad.r),y=v=>pad.t+(max-v)/(max-min)*(H-pad.t-pad.b);ctx.strokeStyle='#2a3a4e';ctx.fillStyle='#aebdd0';ctx.font='12px system-ui';for(let n=0;n<5;n++){const v=min+(max-min)*n/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(3),5,yy+4)}const line=(key,color,width=1,dash=[])=>{ctx.beginPath();ctx.setLineDash(dash);view.forEach((p,n)=>n?ctx.lineTo(x(lo+n),y(p[key])):ctx.moveTo(x(lo+n),y(p[key])));ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([])};line('lower2','#ff7474',1,[5,5]);line('upper2','#ff7474',1,[5,5]);line('mean','#f7f9fc',2);line('ma39','#b087ff',2);line('spread','#53b6ff',2);records.forEach(r=>{ctx.fillStyle='#ff7474';ctx.beginPath();ctx.arc(x(r.entry),y(bars[r.entry].spread),5,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.arc(x(r.exit),y(bars[r.exit].spread),5,0,Math.PI*2);ctx.fill()});if(activeTrade){ctx.fillStyle='#67dbac';ctx.beginPath();ctx.arc(x(activeTrade.entry),y(bars[activeTrade.entry].spread),6,0,Math.PI*2);ctx.fill()}const headX=x(readIndex);ctx.strokeStyle='#ffd166';ctx.lineWidth=4;ctx.beginPath();ctx.moveTo(headX,pad.t-10);ctx.lineTo(headX,H-pad.b);ctx.stroke();ctx.fillStyle='#ffd166';ctx.fillRect(headX-8,pad.t-29,16,16);const pnl=activeTrade?`浮盈 ${percent(currentPnl())}`:'等待建仓';ctx.fillStyle=activeTrade?(currentPnl()>=0?'#67dbac':'#ff7474'):'#ffd166';ctx.font='bold 14px system-ui';ctx.fillText(`读取头 · ${pnl}`,Math.max(pad.l,headX-160),25);ctx.fillStyle='#aebdd0';ctx.font='12px system-ui';ctx.fillText(stamp(lo),pad.l,H-12);ctx.fillText(stamp(hi),W-pad.r-125,H-12);document.querySelector('#caption').textContent=`读取头固定在图表右边界：${stamp(readIndex)}。使用 → 前进一根 5 分钟 bar；不支持回退。`}
function advance(){if(readIndex<bars.length-1){readIndex++;paint();renderTrade()}else document.querySelector('#caption').textContent='已到样本最后一根 bar。'}function toggleTrade(){if(activeTrade){records.push({...activeTrade,exit:readIndex,pnl:currentPnl()});activeTrade=null}else activeTrade={entry:readIndex,side:currentSide()};renderTrade();renderLedger();paint()}enterButton.onclick=toggleTrade;document.addEventListener('keydown',e=>{if(e.altKey||e.ctrlKey||e.metaKey)return;if(['Enter','ArrowRight','ArrowLeft'].includes(e.key)){e.preventDefault();e.stopImmediatePropagation()}if(e.key==='Enter')toggleTrade();if(e.key==='ArrowRight')advance()},true);next.onclick=advance;previous.onclick=e=>e.preventDefault();span.oninput=()=>{spanOut.value=span.value;paint()};canvas.onclick=advance;renderTrade();renderLedger();paint();window.addEventListener('resize',paint);
</script><script>
/* Final manual reader and persistent research ledger. */
(()=>{
  const storageKey='jpm_bac_manual_records_v1', panel=document.querySelector('.grid > aside.card'), headline=document.querySelector('#headlinePnl'), speed=document.querySelector('#speed'), speedOut=document.querySelector('#speedOut');
  let reviewing=false, selectedRecord=-1;
  try { const saved=JSON.parse(localStorage.getItem(storageKey)||'null'); if(saved){
    (Array.isArray(saved.records)?saved.records:[]).forEach(r=>{if(Number.isInteger(r.entry)&&Number.isInteger(r.exit)&&r.entry>=0&&r.exit>=r.entry&&r.exit<bars.length&&['long','short'].includes(r.side)) records.push({...r,pnl:Number.isFinite(r.pnl)?r.pnl:(r.side==='long'?1:-1)*(bars[r.exit].spread-bars[r.entry].spread)/gross,review:r.review||'unreviewed',note:r.note||''})});
    if(saved.activeTrade&&Number.isInteger(saved.activeTrade.entry)&&saved.activeTrade.entry>=0&&saved.activeTrade.entry<bars.length&&['long','short'].includes(saved.activeTrade.side)) activeTrade=saved.activeTrade;
    if(Number.isInteger(saved.readIndex)) readIndex=Math.max(0,Math.min(bars.length-1,saved.readIndex));
  }} catch(_) {}
  panel.insertAdjacentHTML('beforeend',`<div class="info" id="research"><strong>逐笔复盘与研究标签</strong><div class="control"><label for="reviewRecord">已完成交易</label><select id="reviewRecord"></select></div><div class="control"><label for="reviewLabel">入场判断</label><select id="reviewLabel"><option value="unreviewed">未复核</option><option value="clean">合规入场</option><option value="late">晚入场 / 已接近回归</option><option value="skip">事后应跳过</option><option value="other">其他</option></select></div><div class="control"><label for="reviewNote">当时可见的理由 / 备注</label><textarea id="reviewNote" placeholder="例如：z 已从 2.1 回到 0.6，疲劳时仍入场。不要填写后来才知道的信息。"></textarea></div><div class="research-actions"><button id="continueLabel">继续新标注</button><button id="saveResearch">保存到浏览器</button><button id="exportResearch">导出研究 CSV</button><button id="clearReview">退出复盘</button></div><small id="researchStatus"></small></div>`);
  const reviewRecord=document.querySelector('#reviewRecord'),reviewLabel=document.querySelector('#reviewLabel'),reviewNote=document.querySelector('#reviewNote'),researchStatus=document.querySelector('#researchStatus');
  panel.append(document.querySelector('#ledger'));
  const save=()=>{localStorage.setItem(storageKey,JSON.stringify({version:2,records,activeTrade,readIndex,savedAt:new Date().toISOString()}));researchStatus.textContent=`已保存：${records.length} 笔，${new Date().toLocaleString('zh-CN')}`};
  const feature=r=>{const p=bars[r.entry],z=p.sigma?(p.spread-p.mean)/p.sigma:NaN;return {entry_time:stamp(r.entry),exit_time:stamp(r.exit),side:r.side,pnl:r.pnl,review:r.review||'unreviewed',note:r.note||'',entry_spread:p.spread,entry_mean:p.mean,entry_sigma:p.sigma,entry_z:z,entry_ma39:p.ma39,entry_ma39_minus_spread:p.ma39-p.spread,entry_sigma_return:p.sigmaReturn,entry_lower2:p.lower2,entry_upper2:p.upper2};};
  function refreshReviewList(){const keep=selectedRecord;reviewRecord.innerHTML='';[...records].sort((a,b)=>a.entry-b.entry).forEach(r=>{const i=records.indexOf(r),o=document.createElement('option');o.value=i;o.textContent=`#${i+1} · ${stamp(r.entry)} · ${r.side==='long'?'多':'空'} · ${percent(r.pnl)} · ${r.review||'未复核'}`;reviewRecord.append(o)});if(records.length){selectedRecord=Math.max(0,Math.min(records.length-1,keep<0?0:keep));reviewRecord.value=selectedRecord;loadReview()}else{selectedRecord=-1;reviewLabel.value='unreviewed';reviewNote.value=''}}
  function loadReview(){const r=records[selectedRecord];if(!r)return;reviewLabel.value=r.review||'unreviewed';reviewNote.value=r.note||''}
  function saveReview(){const r=records[selectedRecord];if(!r)return;r.review=reviewLabel.value;r.note=reviewNote.value.trim();save();refreshReviewList()}
  function tradePanel(){const pnl=currentPnl();headline.innerHTML=reviewing?'复盘模式：仅查看历史入场前可见信息':activeTrade?`持仓中 · 实时盈亏 <span class="${pnl>=0?'pnl-positive':'pnl-negative'}">${percent(pnl)}</span> · ${sideName(activeTrade.side)}`:'未持仓 · 按 Enter 在读取头建仓';document.querySelector('#trade').innerHTML=reviewing?`<p><strong>复盘读取头：</strong>${stamp(readIndex)}<br>右侧仍遮挡，先检查当时可见的 spread、39-bar MA 与 20 日统计带。标注会自动保存。</p>`:activeTrade?`<span class="tag ${activeTrade.side}">${sideName(activeTrade.side)}</span><p><strong>建仓：</strong>${stamp(activeTrade.entry)}<br><strong>读取头：</strong>${stamp(readIndex)}</p><p>按 <strong>Enter</strong> 在当前读取头平仓。</p>`:`<p><strong>读取头：</strong>${stamp(readIndex)}<br><strong>自动方向：</strong>${sideName(currentSide())}</p><p>按 <strong>Enter</strong> 在读取头建仓。</p>`;enterButton.textContent=activeTrade?'按 Enter 平仓':'按 Enter 建仓';enterButton.disabled=reviewing}
  function draw(){const count=Number(span.value),past=Math.floor(count*.72),lo=Math.max(0,readIndex-past),hi=Math.min(bars.length-1,lo+count-1),view=bars.slice(lo,hi+1),W=canvas.clientWidth,H=canvas.clientHeight,dpr=devicePixelRatio||1;canvas.width=W*dpr;canvas.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);const pad={l:64,r:32,t:38,b:38},values=[];view.forEach(p=>values.push(p.spread,p.ma39,p.mean,p.lower2,p.upper2));let min=Math.min(...values),max=Math.max(...values),extra=(max-min)*.1||.01;min-=extra;max+=extra;const x=i=>pad.l+(i-lo)/Math.max(1,hi-lo)*(W-pad.l-pad.r),y=v=>pad.t+(max-v)/(max-min)*(H-pad.t-pad.b),head=x(readIndex);ctx.strokeStyle='#2a3a4e';ctx.fillStyle='#aebdd0';ctx.font='12px system-ui';for(let n=0;n<5;n++){const v=min+(max-min)*n/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(3),5,yy+4)}const line=(key,color,width=1,dash=[])=>{ctx.beginPath();ctx.setLineDash(dash);view.forEach((p,n)=>n?ctx.lineTo(x(lo+n),y(p[key])):ctx.moveTo(x(lo+n),y(p[key])));ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([])};line('lower2','#ff7474',1,[5,5]);line('upper2','#ff7474',1,[5,5]);line('mean','#f7f9fc',2);line('ma39','#b087ff',2);line('spread','#53b6ff',2);ctx.fillStyle='#07101b';ctx.fillRect(head+3,pad.t,W-pad.r-head-3,H-pad.t-pad.b);ctx.strokeStyle='#ffd166';ctx.lineWidth=5;ctx.beginPath();ctx.moveTo(head,pad.t);ctx.lineTo(head,H-pad.b);ctx.stroke();ctx.fillStyle='#ffd166';ctx.fillRect(head-9,8,18,18);records.forEach(r=>{ctx.fillStyle=r.pnl>=0?'#67dbac':'#ffc86b';ctx.beginPath();ctx.arc(x(r.entry),y(bars[r.entry].spread),5,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.arc(x(r.exit),y(bars[r.exit].spread),5,0,Math.PI*2);ctx.fill()});if(activeTrade){ctx.fillStyle='#67dbac';ctx.beginPath();ctx.arc(x(activeTrade.entry),y(bars[activeTrade.entry].spread),6,0,Math.PI*2);ctx.fill()}ctx.fillStyle='#aebdd0';ctx.fillText(stamp(lo),pad.l,H-12);ctx.fillText(stamp(readIndex),Math.max(pad.l,head-125),H-12);document.querySelector('#caption').textContent=`读取头：${stamp(readIndex)}。右侧未来行情已遮挡；复盘时不允许在历史位置新增交易。`}
  function forward(){if(readIndex<bars.length-1){readIndex=Math.min(bars.length-1,readIndex+Number(speed.value));draw();tradePanel();if(!reviewing)save()}}
  function decide(){if(reviewing)return;if(activeTrade){records.push({...activeTrade,exit:readIndex,pnl:currentPnl(),review:'unreviewed',note:''});activeTrade=null}else activeTrade={entry:readIndex,side:currentSide()};save();refreshReviewList();tradePanel();renderLedger();draw()}
  function exportCsv(){const rows=records.map(feature),columns=['entry_time','exit_time','side','pnl','review','note','entry_spread','entry_mean','entry_sigma','entry_z','entry_ma39','entry_ma39_minus_spread','entry_sigma_return','entry_lower2','entry_upper2'],quote=v=>`"${String(v??'').replaceAll('"','""')}"`,csv=[columns.join(','),...rows.map(r=>columns.map(c=>quote(r[c])).join(','))].join('\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download=`jpm_bac_manual_research_${new Date().toISOString().slice(0,10)}.csv`;a.click();URL.revokeObjectURL(a.href)}
  reviewRecord.onchange=()=>{selectedRecord=Number(reviewRecord.value);reviewing=true;readIndex=records[selectedRecord].entry;loadReview();tradePanel();draw()};reviewLabel.onchange=saveReview;reviewNote.onchange=saveReview;document.querySelector('#saveResearch').onclick=save;document.querySelector('#exportResearch').onclick=exportCsv;document.querySelector('#clearReview').onclick=()=>{reviewing=false;tradePanel();draw()};document.querySelector('#continueLabel').onclick=()=>{reviewing=false;readIndex=Math.max(readIndex,...records.map(r=>r.exit));tradePanel();draw();save()};
  enterButton.onclick=decide;next.onclick=forward;canvas.onclick=forward;span.oninput=()=>{spanOut.value=span.value;draw()};speed.oninput=()=>{speedOut.value=speed.value};window.addEventListener('keydown',e=>{if(['Enter','ArrowRight','ArrowLeft'].includes(e.key)){e.preventDefault();e.stopImmediatePropagation();if(e.key==='Enter')decide();if(e.key==='ArrowRight')forward()}},true);window.addEventListener('beforeunload',save);refreshReviewList();tradePanel();renderLedger();draw();save();window.addEventListener('resize',draw);
})();
</script><?php endif; ?>
</body></html>
