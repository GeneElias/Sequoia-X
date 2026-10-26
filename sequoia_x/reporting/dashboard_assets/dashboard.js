"use strict";
(() => {
  const DATA = JSON.parse(document.getElementById("dashboard-data").textContent);
  let market = "A", sortKey = "rps", descending = true;
  const $ = id => document.getElementById(id);
  const num = x => x === null || x === undefined || x === "" || !Number.isFinite(Number(x)) ? null : Number(x);
  const money = x => num(x) === null ? "—" : Number(x).toLocaleString("zh-CN", {minimumFractionDigits:2, maximumFractionDigits:2});
  const percent = x => num(x) === null ? "—" : (Number(x)*100).toFixed(2) + "%";
  const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const set = (id,text) => {$(id).textContent=text;};
  function tone(node, value) {node.classList.remove("positive","negative");if(num(value)!==null)node.classList.add(Number(value)<0?"negative":"positive");}
  function cell(row,text,cls) {const n=el("td",text,cls);row.appendChild(n);return n;}
  function stockCell(row, item) {const n=cell(row,undefined);n.append(el("span",item.name||item.symbol,"stock-name"),el("span",item.symbol,"stock-code"));}
  function empty(id, isEmpty) {$(id).hidden=!isEmpty;}
  function renderSignals(m) {
    const term=$("search").value.trim().toLowerCase(), filter=$("status-filter").value;
    const rows=m.observations.filter(x=>{
      const match=(x.symbol+" "+(x.name||"")).toLowerCase().includes(term);
      const yes=filter==="all" || (filter==="candidate" && x.status.startsWith("买入研究")) ||
        (filter==="pending" && x.valuation.status==="待核验") ||
        (filter==="watch" && (x.status.includes("观察")||x.status.includes("等待价格")));
      return match&&yes;
    });
    rows.sort((a,b)=>{const aa=num(a[sortKey]),bb=num(b[sortKey]);if(aa===null)return bb===null?0:1;if(bb===null)return -1;return descending?bb-aa:aa-bb;});
    $("signals-body").replaceChildren();
    for(const item of rows){
      const row=el("tr");stockCell(row,item);cell(row,money(item.raw_price));cell(row,num(item.rps)===null?"待数据":Number(item.rps).toFixed(1));
      tone(cell(row,percent(item.return120)),item.return120);cell(row,num(item.volume_ratio)===null?"—":Number(item.volume_ratio).toFixed(2)+"×");
      const sig=cell(row,undefined);sig.append(el("span",(item.signals||[]).join(" / ")||"等待确认","tag neutral"));
      const status=cell(row,undefined),pending=item.valuation.status==="待核验";
      status.append(el("span",item.valuation.status||"待核验","tag "+(pending?"pending":item.status.startsWith("买入研究")?"good":"neutral")),el("div",item.status,"status-sub"));
      $("signals-body").appendChild(row);
    }
    set("signal-count",`${rows.length} / ${m.observations.length}`);empty("signals-empty",rows.length===0);
  }
  function renderPositions(m){
    $("positions-body").replaceChildren();
    for(const p of m.positions){const row=el("tr");cell(row,p.symbol);cell(row,num(p.quantity)===null?"待核验":Number(p.quantity).toLocaleString());cell(row,money(p.price));cell(row,money(p.cost));cell(row,money(p.value));tone(cell(row,p.status||money(p.pnl)),p.pnl);$("positions-body").appendChild(row);}
    empty("positions-empty",m.positions.length===0);
  }
  function renderWeekly(m){
    $("weekly-body").replaceChildren();
    for(const w of [...m.weekly].reverse()){const row=el("tr");cell(row,w.week_start+" — "+w.week_end);cell(row,w.complete?String(w.fill_count??0):"待核验");cell(row,w.complete?money(w.new_capital):"—");cell(row,w.complete?money(w.equity):"—");tone(cell(row,w.complete?money(w.weekly_pnl):"待核验"),w.complete?w.weekly_pnl:null);tone(cell(row,w.complete?percent(w.weekly_return):"待核验"),w.complete?w.weekly_return:null);$("weekly-body").appendChild(row);}
    empty("weekly-empty",m.weekly.length===0);
  }
  function renderOrders(m){
    $("orders-body").replaceChildren();const names={pending:"待成交",filled:"已模拟成交",expired:"错过成交日，已过期",unfilled:"未成交"};
    for(const order of m.orders){const row=el("tr");cell(row,order.symbol);cell(row,order.signal_date);cell(row,order.execution_date);cell(row,"100 股");const td=cell(row,undefined);td.append(el("span",names[order.state]||order.state,"tag "+(order.state==="filled"?"good":"neutral")));$("orders-body").appendChild(row);}
    set("order-count",`${m.orders.length} 笔（最近500笔以内）`);empty("orders-empty",m.orders.length===0);
  }
  const NS="http://www.w3.org/2000/svg";
  function svgEl(tag,attrs,text){const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
  function renderChart(m){
    const metric=$("chart-metric").value, range=$("chart-range").value;
    let rows=m.history;
    if(range!=="all" && rows.length){const end=Date.parse(rows[rows.length-1].day+"T00:00:00Z");rows=rows.filter(x=>end-Date.parse(x.day+"T00:00:00Z")<Number(range)*86400000);}
    const points=rows.filter(x=>num(x[metric])!==null), container=$("chart");container.replaceChildren();
    set("chart-tip",metric==="equity"?"实线：持仓市值　虚线：累计虚拟投入":"悬停查看实际快照；缺失价格不补零");
    if(!points.length){const n=el("div",undefined,"chart-empty");n.append(el("strong","—"),el("span",metric==="return"?"尚无投入，不生成收益率曲线":"尚无完整估值快照"));container.appendChild(n);return;}
    const W=760,H=230,L=62,R=12,T=15,B=34;
    const values=points.map(x=>Number(x[metric]));if(metric==="equity")for(const r of rows)if(num(r.contributed)!==null)values.push(Number(r.contributed));
    const min=Math.min(0,...values),max=Math.max(0,...values),pad=(max-min)*.15||1;
    const low=min-pad,high=max+pad;
    const y=v=>T+(high-v)/(high-low)*(H-T-B),x=i=>L+(rows.length===1?.5:i/(rows.length-1))*(W-L-R);
    const svg=svgEl("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":market+"模拟组合快照曲线"});
    for(let i=0;i<5;i++){const value=low+(high-low)*i/4,yy=y(value);svg.append(svgEl("line",{x1:L,y1:yy,x2:W-R,y2:yy,stroke:"#eaf0e9","stroke-dasharray":"3 5"}));svg.append(svgEl("text",{x:L-10,y:yy+3,"text-anchor":"end",fill:"#8ca08f","font-size":9},metric==="return"?percent(value):Math.abs(value)>=10000?(value/10000).toFixed(1)+"万":Number(value).toFixed(high-low<10?1:0)));}
    const paths=metric==="equity"?[[metric,"#387f57",false],["contributed","#a0b896",true]]:[[metric,"#387f57",false]];
    for(const [field,color,dashed] of paths){let segment=[];const flush=()=>{if(segment.length)svg.append(svgEl("polyline",{points:segment.join(" "),fill:"none",stroke:color,"stroke-width":2,"stroke-dasharray":dashed?"5 4":"none"}));segment=[];};rows.forEach((r,i)=>{if(num(r[field])===null)flush();else segment.push(`${x(i)},${y(Number(r[field]))}`);});flush();}
    rows.forEach((r,i)=>{if(num(r[metric])===null)return;const circle=svgEl("circle",{cx:x(i),cy:y(Number(r[metric])),r:rows.length===1?4:3,fill:"#387f57"});const tip=`${r.day} · ${metric==="return"?percent(r[metric]):money(r[metric])+" "+m.currency}`;circle.append(svgEl("title",{},tip));circle.addEventListener("pointerenter",()=>set("chart-tip",tip));svg.appendChild(circle);});
    const labels=[0,rows.length-1];for(const i of new Set(labels))svg.append(svgEl("text",{x:x(i),y:H-10,"text-anchor":i===0&&rows.length>1?"start":"end",fill:"#8ca08f","font-size":9},rows[i].day));
    container.appendChild(svg);
  }
  function render(){
    const m=DATA.markets[market], last=m.history[m.history.length-1], invested=last&&num(last.contributed)>0;
    set("asof",m.as_of||"等待首次报告");set("currency",m.currency);set("coverage",`有效观察项 ${m.valid} / ${m.pool_size}`);
    set("equity",last?money(last.equity):"—");set("capital",`累计虚拟投入 ${last?money(last.contributed):"—"} ${m.currency}`);
    set("pnl",last?money(last.pnl):"—");tone($("pnl"),last?.pnl);set("return",last?percent(last.return):"—");tone($("return"),last?.return);
    set("return-note",invested?"价格盈亏 / 累计虚拟投入，非年化":"无投入，不计算收益率");
    set("counts",`${m.candidate_count} / ${m.fill_count??m.fills.length}`);
    set("notice",DATA.demo?"演示模式：所有股票、指标、交易和收益均为虚构示例，不是实际投资结果。":!m.complete?"数据待补齐：观察池未完整取数，暂停RPS和买入推荐。":m.candidate_count===0?"保持纪律：当前没有完成量价与估值双重核验的推荐。观察项不自动转为模拟买单。":"合格推荐仅供研究；模拟订单在下一交易日开盘结算，不使用未来价格补单。");
    const delivery={blocked_missing_webhook:"飞书待配置 · 日报已保存在本地",preview_only:"本地预览 · 未发送到飞书",attempted:"飞书已尝试发送 · 以发送账本为准",demo:"公开演示 · 未发送消息"};set("delivery",delivery[m.delivery]||"飞书发送状态待核验");
    document.querySelectorAll("[data-market]").forEach(b=>{b.classList.toggle("selected",b.dataset.market===market);b.setAttribute("aria-pressed",String(b.dataset.market===market));});
    renderSignals(m);renderPositions(m);renderWeekly(m);renderOrders(m);renderChart(m);
  }
  document.querySelectorAll("[data-market]").forEach(b=>b.addEventListener("click",()=>{market=b.dataset.market;render();}));
  for(const id of ["search","status-filter"])$(id).addEventListener(id==="search"?"input":"change",()=>renderSignals(DATA.markets[market]));
  for(const id of ["chart-metric","chart-range"])$(id).addEventListener("change",()=>renderChart(DATA.markets[market]));
  document.querySelectorAll("[data-sort]").forEach(b=>b.addEventListener("click",()=>{descending=b.dataset.sort===sortKey?!descending:true;sortKey=b.dataset.sort;renderSignals(DATA.markets[market]);}));
  document.querySelectorAll("nav a").forEach(a=>a.addEventListener("click",()=>{document.querySelectorAll("nav a").forEach(x=>x.classList.toggle("active",x===a));}));
  $("download").addEventListener("click",()=>{const blob=new Blob([JSON.stringify({generated_at:DATA.generated_at,demo:DATA.demo,market,...DATA.markets[market]},null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const a=el("a");a.href=url;a.download=`sequoia-${market}-${DATA.markets[market].as_of||"snapshot"}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  set("mode",DATA.demo?"虚构演示":"本地数据 · 未公开");
  set("updated","生成于 "+new Date(DATA.generated_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}));
  set("basis",DATA.basis);
  const hb=DATA.scheduler.heartbeat;
  set("heartbeat",hb?"心跳快照 "+new Date(hb).toLocaleTimeString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false}):"未记录定时进程心跳");
  $("heartbeat").title="静态快照，不代表进程此刻在线。"+(DATA.scheduler.lifetime||"");
  if(!hb)$("heartbeat-dot").classList.add("offline");
  render();
  document.documentElement.dataset.ready="true";
})();
