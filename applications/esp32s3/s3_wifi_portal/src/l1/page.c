/*
 * page —— 三张页面（用户配网页 / 开发者调试台 / 口令页）+ captive 通配
 *
 * L1 接口层。页面内容是静态字符串，唯一的判断是"来的人是谁、要不要口令"。
 */
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/http/server.h>
#include <zephyr/net/http/service.h>
#include <zephyr/net/http/status.h>

#include "portal_cfg.h"
#include "http_core.h"
#include "pages.h"

LOG_MODULE_REGISTER(portal_page, LOG_LEVEL_INF);

/* ------------------------------------------------------------ 网页（HTML） */
static const char index_html[] =
"<!DOCTYPE html><html><head><meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>ESP32-S3 Wi-Fi Portal</title><style>"
"body{font-family:system-ui,sans-serif;background:#101418;color:#e8eef4;margin:0;padding:18px}"
"h1{font-size:19px;margin:0 0 4px}.sub{color:#8aa0b4;font-size:12px;margin-bottom:16px}"
".card{background:#182028;border:1px solid #24303a;border-radius:10px;padding:14px;margin-bottom:14px}"
".row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid #222c35}"
".row:last-child{border-bottom:0}.ssid{font-size:14px}.meta{color:#8aa0b4;font-size:12px}"
"input{width:100%;box-sizing:border-box;background:#0e1418;border:1px solid #2a3640;color:#e8eef4;"
"border-radius:8px;padding:10px;margin:6px 0;font-size:14px}"
"button{background:#2f6df6;border:0;color:#fff;border-radius:8px;padding:10px 16px;font-size:14px}"
"button.ghost{background:#24303a}button:disabled{opacity:.5}"
".pw{display:flex;gap:8px;align-items:center}.pw input{flex:1}.pw button{white-space:nowrap;padding:10px 12px}"
".badge{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px}"
".red{background:#3a1a1a;color:#ff7b7b}.blue{background:#152a4a;color:#6da8ff}"
".green{background:#153a22;color:#5ee089}#list{max-height:240px;overflow:auto}"
".fb{display:none;margin:10px 0 0;padding:10px;border-radius:8px;font-size:13px;line-height:1.5}"
".fb.wait{display:block;background:#152a4a;color:#8fbaff}"
".fb.ok{display:block;background:#153a22;color:#5ee089}"
".fb.err{display:block;background:#3a1a1a;color:#ff9b9b}"
"</style></head><body>"
"<h1>ESP32-S3 Wi-Fi Portal</h1>"
"<div class='sub'>AP 模式已开启：连 <b>S3-Portal</b>，密码 <b><REDACTED-PSK></b>，然后打开 "
"http://192.168.4.1 或 http://s3portal.local<br>连接成功会把 SSID/密码存入 NVS，下次上电自动重连（最多 3 次）</div>"
"<div class='card'><div class='row'><span>当前状态</span><span id='state' class='badge red'>idle</span></div>"
"<div class='row'><span>已连网络</span><span id='ssid' class='meta'>-</span></div>"
"<div class='row'><span>IP 地址</span><span id='ip' class='meta'>-</span></div>"
"<div class='row'><span>信号强度</span><span id='rssi' class='meta'>-</span></div>"
"<div class='row'><span>掉线重连</span><span id='rc' class='meta'>-</span></div>"
"<div class='row' style='display:block'><span>提示</span>"
"<span id='msg' class='meta' style='display:block;margin-top:4px;word-break:break-word'>-</span></div>"
"<div class='row'><span>连接控制</span><button class='ghost' id='discbtn'>断开</button></div></div>"
"<div class='card'><div class='row'><span>连到本机的设备</span><span id='apcnt' class='meta'>-</span></div>"
"<div id='aps'><div class='meta'>还没有设备连上本机 AP</div></div></div>"
"<div class='card'><div class='row'><span>信号强度趋势</span>"
"<span class='meta'>60 点 · 10 秒一个</span></div>"
"<div id='spark'><div class='meta'>连上路由器后开始记录</div></div></div>"
"<div class='card'><div class='row'><span>可用 Wi-Fi</span>"
"<button class='ghost' id='scanbtn'>扫描</button></div><div id='list'><div class='meta'>点“扫描”开始</div></div></div>"
"<div class='card'><div class='row'><span>已保存网络</span><span class='meta'>点“连接”免输密码</span></div>"
"<div id='saved'><div class='meta'>-</div></div></div>"
"<div class='card'><div class='meta'>手动连接（点上面的网络也会自动填入）</div>"
"<input id='in_ssid' placeholder='SSID'>"
"<div class='pw'><input id='in_psk' type='password' placeholder='密码（开放网络留空）'>"
"<button class='ghost' id='eyebtn' type='button'>显示</button></div>"
"<button id='connbtn' style='width:100%'>连接</button>"
"<div class='fb' id='fb'></div></div>"
"<div class='card'><div class='row'><span>固件升级（局域网 OTA）</span><span id='otabadge' class='badge'>-</span></div>"
"<div class='row'><span>板端版本</span><span id='otaver' class='meta'>-</span></div>"
"<div class='row'><span>传输进度</span><span id='otaprog' class='meta'>-</span></div>"
"<div class='row' style='display:block'><span>错误</span>"
"<span id='otaerr' class='meta' style='display:block;margin-top:4px;word-break:break-word'>-</span></div>"
"<div class='pw'><input id='fup' type='file' accept='.bin' style='padding:8px;border-radius:8px;border:1px solid #2a3550;background:#0e1424;color:#cbd5e6'>"
"<button class='ghost' id='upbtn'>上传</button></div>"
"<div class='fb' id='otafb'></div>"
"<div class='meta' style='margin-top:6px'>上传走 :8080，和配网页 :80 分开；写进 slot1 并回读校验通过后才会置升级标记。</div></div>"
"<div class='card'><div class='row'><span>联网自检</span><span id='netbadge' class='badge'>-</span></div>"
"<div class='row'><span>板端时钟 UTC+8</span><span id='clk' class='meta'>-</span></div>"
"<div class='row'><span>HTTP 出网</span><span id='netinfo' class='meta'>-</span></div></div>"
"<script>"
"const K=(new URLSearchParams(location.search)).get('k')||localStorage.getItem('s3k')||'';"
"if(K)localStorage.setItem('s3k',K);"
/* 所有 /api 请求自动带上 k：改一处就覆盖了页面里十几处 fetch，
 * 不用逐个去拼查询串（漏一个就是一个 401）。 */
"const _fetch=window.fetch.bind(window);"
"window.fetch=function(u,o){"
"if(typeof u==='string'&&u.charAt(0)==='/'){u+=(u.indexOf('?')>=0?'&':'?')+'k='+encodeURIComponent(K);}"
"return _fetch(u,o);};"
"const $=id=>document.getElementById(id);"
"let SAVED={},SAVED_LIST=[],SCAN_APS=[],LAST_LOCAL_VER=0;"
"function esc(s){return String(s).split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;')}"
"function fb(t,c){const e=$('fb');e.textContent=t;e.className='fb '+c}"
"function badge(s){const e=$('state');"
"if(s==='connected'){e.className='badge green';e.textContent='已连接'} "
"else if(s==='connecting'){e.className='badge blue';e.textContent='连接中'} "
"else{e.className='badge red';e.textContent='未连接'}}"
"async function status(){try{const r=await fetch('/api/status');const j=await r.json();"
"badge(j.state);$('ssid').textContent=j.ssid||'-';$('ip').textContent=j.ip||'-';$('msg').textContent=j.msg||'-';"
"$('rssi').textContent=j.rssi?j.rssi+' dBm':'-';"
"$('rc').textContent=(j.reconnects||0)+' 次 · 运行 '+Math.floor((j.uptime||0)/60)+' 分钟';"
"const o=j.ota||{};LAST_LOCAL_VER=o.local_ver||0;"
"$('otaver').textContent='板端 '+(o.local_ver||'-')+(o.ver?(' · 待装 '+o.ver):'');"
"$('otaprog').textContent=(o.stage||'idle')+(o.total?(o.pct+'% ('+o.written+'/'+o.total+' B)'):'');"
"$('otaerr').textContent=o.err||'-';"
"const ob=$('otabadge');"
"if(o.stage==='ready'){ob.className='badge green';ob.textContent='待重启'}"
"else if(o.stage==='error'){ob.className='badge red';ob.textContent='失败'}"
"else if(o.stage==='download'||o.stage==='verify'){ob.className='badge blue';ob.textContent=o.stage}"
"else{ob.className='badge';ob.textContent='空闲'}"
"$('clk').textContent=j.clock||'(未对时)';"
"const nc=j.netcheck|0;"
"$('netinfo').textContent=nc===1?'在线（已拿到 HTTP 响应）':'离线（没连上外网）';"
"const nb=$('netbadge');nb.className='badge '+(nc===1?'green':'red');nb.textContent=nc===1?'OK':'OFF'"
"}catch(e){}}"
"async function scan(){const b=$('scanbtn');b.disabled=true;$('list').innerHTML=\"<div class='meta'>扫描中…（约 10 秒）</div>\";"
"try{await fetch('/api/scan/start');let j={aps:[],scanning:true};"
"for(let i=0;i<14;i++){await new Promise(k=>setTimeout(k,1500));"
"const r=await fetch('/api/scan');j=await r.json();"
"if(j.aps.length||!j.scanning){break}}"
"SCAN_APS=j.aps||[];let h='';"
"for(let i=0;i<SCAN_APS.length;i++){const a=SCAN_APS[i];"
"h+=\"<div class='row' data-p='\"+i+\"'><span class='ssid'>\"+esc(a.ssid)+"
"\"</span><span class='meta'>\"+a.rssi+' dBm · ch'+a.ch+(SAVED[a.ssid]?' · 已保存':'')+'</span></div>'}"
"$('list').innerHTML=h||\"<div class='meta'>没扫到，换个位置再试</div>\"}"
"catch(e){$('list').innerHTML=\"<div class='meta'>扫描失败</div>\"}"
"b.disabled=false}"
"async function doConnect(ssid,psk){const b=$('connbtn');const old=b.textContent;"
"b.disabled=true;b.textContent='连接中…';fb('正在连接 '+ssid+' …（若手机 wifi 短暂断开属正常，会自动重连）','wait');"
"let q='/api/connect?ssid='+encodeURIComponent(ssid);"
"if(psk)q+='&psk='+encodeURIComponent(psk);"
"try{"
"const r=await fetch(q,{method:'GET'});"
"const j=await r.json();"
"if(!j.ok){fb('连接请求被拒绝：'+(j.err||'未知原因'),'err');b.disabled=false;b.textContent=old;return}"
"if(j.msg&&j.msg.indexOf('already')>=0){fb('已经连着 '+ssid+'，不用重复连接','ok');"
"b.disabled=false;b.textContent=old;await status();return}"
"let last='',got=false;"
"for(let i=0;i<14;i++){await new Promise(k=>setTimeout(k,1500));"
"let st=null;try{st=await (await fetch('/api/status')).json()}"
"catch(e){last='Wi-Fi 正在切换信道，等手机重连';continue}"
"if(st.state==='connected'&&st.ssid===ssid){got=true;break}"
"if(st.last_err){last=st.last_err;break}"
"if(st.state==='idle'&&String(st.msg||'').indexOf('failed')>=0){last=st.msg;break}"
"last=st.msg||''}"
"if(got){fb('已连接 '+ssid+'，可以关掉配网页了','ok')}"
"else{fb('连接失败：'+(last||'等待超时'),'err')}"
"}catch(e){fb('请求失败：'+e,'err')}"
"b.disabled=false;b.textContent=old;await status();await loadSaved()}"
"async function connect(){const s=$('in_ssid').value.trim();"
"if(!s){fb('请先点一个网络，或手动输入 SSID','err');return}"
"await doConnect(s,$('in_psk').value)}"
"async function quickConnect(ssid){$('in_ssid').value=ssid;$('in_psk').value='';await doConnect(ssid,'')}"
"function renderSaved(){let h='';for(let i=0;i<SAVED_LIST.length;i++){const s=SAVED_LIST[i];SAVED[s.ssid]=s;"
"h+=\"<div class='row'><span class='ssid'>\"+esc(s.ssid)+"
"(s.current?\" <span class='badge green'>当前</span>\":'')+'</span>';"
"h+=\"<span><button class='ghost' data-c='\"+i+\"'>连接</button> \"+"
"\"<button class='ghost' data-f='\"+i+\"'>忘记</button></span></div>\";}"
"$('saved').innerHTML=h||\"<div class='meta'>还没有保存过网络，连上一个就会记住</div>\"}"
"async function loadSaved(){$('saved').innerHTML=\"<div class='meta'>读取中…</div>\";"
"try{const r=await fetch('/api/saved');const j=await r.json();SAVED={};SAVED_LIST=j.saved||[];renderSaved()}"
"catch(e){$('saved').innerHTML=\"<div class='meta'>读取失败</div>\"}}"
"async function forget(ssid){try{const r=await fetch('/api/forget?ssid='+encodeURIComponent(ssid),"
" {method:'GET'});"
"const j=await r.json();"
"if(j.ok){SAVED_LIST=SAVED_LIST.filter(x=>x.ssid!==ssid);delete SAVED[ssid];renderSaved()}"
"fb(j.ok?('已忘记 '+ssid):('没有找到 '+ssid+' 的保存记录'),j.ok?'ok':'err');"
"await loadSaved()}catch(e){fb('忘记失败：'+e,'err')}}"
"async function disconnect(){const b=$('discbtn');b.disabled=true;"
"try{await fetch('/api/disconnect',{method:'GET'});fb('已断开，不会再自动连回去','ok');"
"await status()}catch(e){fb('断开失败：'+e,'err')}"
"b.disabled=false}"
"async function loadApClients(){try{const j=await (await fetch('/api/apclients')).json();"
"const c=j.clients||[];$('apcnt').textContent=c.length+' / '+(j.max||8);"
"if(!c.length){$('aps').innerHTML=\"<div class='meta'>还没有设备连上本机 AP</div>\";return}"
"let h='';for(let i=0;i<c.length;i++){const x=c[i];"
"const t=x.sec<60?x.sec+' 秒':(x.sec<3600?Math.floor(x.sec/60)+' 分钟':Math.floor(x.sec/3600)+' 小时');"
"h+=\"<div class='row'><span class='meta'>\"+esc(x.mac)+\"</span><span class='meta'>在线 \"+t+'</span></div>'}"
"$('aps').innerHTML=h}catch(e){}}"
"async function loadRssi(){try{const j=await (await fetch('/api/rssi_hist')).json();"
"drawSpark(j.points||[])}catch(e){}}"
"function drawSpark(p){const box=$('spark');"
"if(p.length<2){box.innerHTML=\"<div class='meta'>连上路由器后开始记录（10 秒 1 个点）</div>\";return}"
"const w=300,h=64;let mn=Math.min.apply(null,p),mx=Math.max.apply(null,p);if(mx-mn<8){mx=mn+8}"
"const pts=p.map(function(v,i){return (i/(p.length-1)*w).toFixed(1)+','+(h-((v-mn)/(mx-mn))*(h-8)-4).toFixed(1)}).join(' ');"
"box.innerHTML=\"<svg viewBox='0 0 \"+w+' '+h+\"' preserveAspectRatio='none' style='width:100%;height:72px;display:block'>\""
"+\"<polyline fill='none' stroke='#5ee089' stroke-width='2' vector-effect='non-scaling-stroke' points='\"+pts+\"'/></svg>\""
"+\"<div class='meta' style='margin-top:6px'>当前 \"+p[p.length-1]+' dBm · 区间 '+mn+' ~ '+mx+' dBm</div>'}"
"$('scanbtn').onclick=scan;$('connbtn').onclick=connect;$('discbtn').onclick=disconnect;"
"function sha256Hex(buf){"
"/* 纯 JS 的 SHA-256：为什么不用 crypto.subtle —— 页面是 http://10.x.x.x，"
"   浏览器只把 https/localhost 当安全上下文，非安全上下文里 crypto.subtle 是 undefined。"
"   整包 850 KB 在这里约 0.2~0.4 s（手机慢一点也能接受），换来的是\"期望摘要\"真的由发起端算。 */"
"const K=["
"0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,"
"0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,"
"0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,"
"0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,"
"0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,"
"0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,"
"0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,"
"0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];"
"let H=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];"
"const len=buf.byteLength;"
"const total=(((len+9)>>6)+1)*16;"
"const w=new Uint32Array(total);"
"for(let i=0;i<len;i++) w[i>>2]|=buf[i]<<(24-((i&3)<<3));"
"w[len>>2]|=0x80<<(24-((len&3)<<3));"
"w[total-2]=Math.floor(len/0x20000000);"
"w[total-1]=(len<<3)>>>0;"
"const rr=(x,n)=>(x>>>n)|(x<<(32-n));"
"for(let b=0;b<total;b+=16){"
" const W=new Uint32Array(64);"
" for(let i=0;i<16;i++) W[i]=w[b+i];"
" for(let i=16;i<64;i++){"
"  const s0=rr(W[i-15],7)^rr(W[i-15],18)^(W[i-15]>>>3);"
"  const s1=rr(W[i-2],17)^rr(W[i-2],19)^(W[i-2]>>>10);"
"  W[i]=(W[i-16]+s0+W[i-7]+s1)>>>0;}"
" let a=H[0],bb=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7];"
" for(let i=0;i<64;i++){"
"  const S1=rr(e,6)^rr(e,11)^rr(e,25);"
"  const ch=(e&f)^((~e)&g);"
"  const t1=(h+S1+ch+K[i]+W[i])>>>0;"
"  const S0=rr(a,2)^rr(a,13)^rr(a,22);"
"  const mj=(a&bb)^(a&c)^(bb&c);"
"  const t2=(S0+mj)>>>0;"
"  h=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=bb;bb=a;a=(t1+t2)>>>0;}"
" H[0]=(H[0]+a)>>>0;H[1]=(H[1]+bb)>>>0;H[2]=(H[2]+c)>>>0;H[3]=(H[3]+d)>>>0;"
" H[4]=(H[4]+e)>>>0;H[5]=(H[5]+f)>>>0;H[6]=(H[6]+g)>>>0;H[7]=(H[7]+h)>>>0;}"
"let hex='';"
"for(let i=0;i<8;i++){const v=H[i];hex+=('00000000'+v.toString(16)).slice(-8);}"
"return hex;}"
"$('upbtn').onclick=async()=>{"
"const f=$('fup').files[0];"
"if(!f){$('otafb').className='fb bad';$('otafb').textContent='先选一个 zephyr.signed.bin（签名过的，不是裸 bin）';return}"
"const v=prompt('新固件版本号（必须大于板端 '+LAST_LOCAL_VER+'）',String(LAST_LOCAL_VER+1));"
"if(!v)return;"
"const b=$('upbtn');b.disabled=true;$('otafb').className='fb';"
"const t0=Date.now();"
"try{"
"$('otafb').textContent='正在算整包 SHA-256（'+Math.round(f.size/1024)+' KB）…';"
"const sha=await sha256Hex(new Uint8Array(await f.arrayBuffer()));"
"$('otafb').textContent='上传中 '+Math.round(f.size/1024)+' KB（首次要擦整槽，约 10 秒）…';"
"const r=await fetch('http://'+location.hostname+':8080/ota',{method:'POST',"
"headers:{'Content-Type':'application/octet-stream','X-Version':v,'X-Nonce':String(Math.floor(t0/1000)),"
"'X-Force':'0','X-Sha256':sha,'X-Key':K},body:f});"
"const j=await r.json();const sec=((Date.now()-t0)/1000).toFixed(1);"
"if(j.ok){$('otafb').className='fb ok';"
"$('otafb').textContent='slot1 已写入：期望摘要 + Flash 回读都通过（'+sec+' s，sha='+sha.slice(0,12)+'…）';"
"if(confirm('现在重启切到新固件？')){"
"await fetch('http://'+location.hostname+':8080/api/ota/reboot',{method:'POST',"
"headers:{'X-Nonce':String(Math.floor(Date.now()/1000)),'X-Key':K}});"
"$('otafb').textContent='已发重启请求，等 20 秒后刷新本页看新版本号';}}"
"else{$('otafb').className='fb bad';"
"$('otafb').textContent='被拒绝 HTTP '+r.status+'：'+(j.err||'')+'（401/400 先查口令和镜像文件）';}"
"}catch(e){$('otafb').className='fb bad';$('otafb').textContent='上传失败：'+e}"
"finally{b.disabled=false}};"
"$('list').onclick=e=>{const r=e.target.closest('[data-p]');if(!r)return;"
"const a=SCAN_APS[+r.dataset.p];if(!a)return;"
"$('in_ssid').value=a.ssid;$('in_psk').value='';$('in_psk').focus();"
"fb('已填入 '+a.ssid+'：'+(SAVED[a.ssid]?'已存过密码，直接点「连接」':'输入密码后点「连接」'),'wait')};"
"$('saved').onclick=e=>{const b=e.target.closest('button');if(!b)return;"
"if(b.dataset.c!==undefined){const s=SAVED_LIST[+b.dataset.c];if(s)quickConnect(s.ssid)}"
"else if(b.dataset.f!==undefined){const s=SAVED_LIST[+b.dataset.f];if(s)forget(s.ssid)}};"
"$('eyebtn').onclick=()=>{const e=$('in_psk');const toText=e.type==='password';"
"e.type=toText?'text':'password';$('eyebtn').textContent=toText?'隐藏':'显示'};"
"$('in_psk').addEventListener('keydown',e=>{if(e.key==='Enter')connect()});"
"status();loadSaved();loadApClients();loadRssi();"
"setInterval(status,3000);setInterval(loadSaved,8000);"
"setInterval(loadApClients,5000);setInterval(loadRssi,10000);"
"/* --- 远程命令卡片：走 /api/shell，和串口/Telnet 是同一条 shell --- */\nconst shBox=document.createElement('div');\nshBox.className='card';\nshBox.innerHTML=\"<h2>远程命令</h2>\"\n +\"<div class='meta'>和串口 / Telnet 共用一条 shell；输出会剥掉颜色控制码</div>\"\n +\"<div style='display:flex;gap:6px;margin-top:8px'>\"\n +\"<input id='shin' autocomplete='off' spellcheck='false' style='flex:1;min-width:0' placeholder='kernel uptime'>\"\n +\"<button id='shgo' class='ghost'>执行</button></div>\"\n +\"<pre id='shout' class='meta' style='white-space:pre-wrap;margin-top:8px;max-height:220px;overflow:auto'></pre>\";\ndocument.body.appendChild(shBox);\nfunction stripAnsi(s){return String(s).replace(/\\x1b\\[[0-9;]*[A-Za-z]/g,'').replace(/\\[[0-9;]*m/g,'')}\nasync function runcmd(){\n const c=$('shin').value.trim();if(!c)return;\n const out=$('shout');out.textContent='执行中…';\n try{const r=await fetch('/api/shell?cmd='+encodeURIComponent(c));\n  const j=await r.json();\n  out.textContent=j.ok?stripAnsi(j.out||'(无输出)'):('失败：'+(j.err||('HTTP '+r.status)));\n }catch(e){out.textContent='请求失败：'+e}}\n$('shgo').onclick=runcmd;\n$('shin').addEventListener('keydown',e=>{if(e.key==='Enter')runcmd()});\n/* 常用命令一键填入，省得手机上敲 */\nconst shQuick=['portal status','kernel uptime','kernel thread list','net iface','portal saved','portal drop'];\nconst shRow=document.createElement('div');\nshRow.style.marginTop='6px';\nfor(const q of shQuick){const b=document.createElement('button');b.className='ghost';b.textContent=q;\n b.style.marginRight='6px';b.style.marginTop='4px';\n b.onclick=()=>{$('shin').value=q;runcmd()};shRow.appendChild(b)}\nshBox.appendChild(shRow);"
"</script></body></html>";

/* ------------------------------------------------------------ HTTP 资源 */
static const struct http_header html_header[] = {
	{.name = "Content-Type", .value = "text/html; charset=utf-8"},
	/* 首页必须每次都重新拿：静态资源会被 http_server 加 ETag，
	 * 手机浏览器拿缓存里的旧页面，就会看到"点连接没反应"的老版本。 */
	{.name = "Cache-Control", .value = "no-store"},
};

/* 把"这一侧拿到的是哪一页"直接标在响应头上。
 * ① 上电自检 / curl 一行就能判定，不必去猜 HTML 内容里有没有某个字符串；
 * ② 排障时一眼分清 AP 侧（用户页）和局域网侧（开发者页），
 *    避免把"用户页连不上网"的问题当成开发者页的 bug 去查。 */
static const struct http_header page_user_header[] = {
	{.name = "Content-Type", .value = "text/html; charset=utf-8"},
	{.name = "Cache-Control", .value = "no-store"},
	{.name = "X-Portal-Scope", .value = "user"},
};

static const struct http_header page_dev_header[] = {
	{.name = "Content-Type", .value = "text/html; charset=utf-8"},
	{.name = "Cache-Control", .value = "no-store"},
	{.name = "X-Portal-Scope", .value = "dev"},
};

/* 首页用动态资源回，而不是 HTTP_RESOURCE_TYPE_STATIC：
 * 静态资源的响应头由 http_server 自己拼，加不了 Cache-Control。 */
static const char unlock_html[] =
	"<!doctype html><meta charset=utf-8>"
	"<meta name=viewport content=\"width=device-width,initial-scale=1\">"
	"<title>需要口令</title>"
	"<body style=\"font-family:system-ui,sans-serif;padding:24px;max-width:560px;margin:auto\">"
	"<h3>ESP32-S3 Wi-Fi Portal</h3>"
	"<p>这个配网页已经加了访问口令（默认 <b><REDACTED-PSK></b>）。</p>"
	"<input id=k placeholder=\"输入口令\" "
	"style=\"width:100%;padding:10px;font-size:16px;box-sizing:border-box\">"
	"<button style=\"margin-top:12px;width:100%;padding:12px;font-size:16px\" "
	"onclick=\"location.href='/?k='+encodeURIComponent(document.getElementById('k').value)\">"
	"进入</button>"
	"<p style=\"color:#888;font-size:13px;margin-top:18px\">"
	"口令可在页面里改（/api/pw?pw=新口令&amp;k=旧口令）。</p>"
	"</body>";

/* 用户配网页（只在 AP 侧出现）由 tools/gen_pages.py 从 src/user_page.html 生成，
 * 不要在 C 里手写大段 HTML：三层引号叠加时 C 的 \" 转义会把 JS 字符串截断成半句，
 * 而且编译不会报错。改页面就改 src/user_page.html，然后重新生成 pages.h。 */
static int index_handler(struct http_client_ctx *client, enum http_transaction_status status,
			 const struct http_request_ctx *request_ctx,
			 struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	if (status == HTTP_SERVER_TRANSACTION_ABORTED) {
		return 0;
	}

	if (status == HTTP_SERVER_REQUEST_DATA_MORE) {
		response_ctx->final_chunk = false;
		return 0;
	}

	if (status != HTTP_SERVER_REQUEST_DATA_FINAL) {
		return 0;
	}

	/* 两个页面，两个受众：
	 *   AP 侧  → user_html：只管 WiFi（扫码/保存/连接/断开），没有 OTA、没有命令、不要口令；
	 *   局域网侧 → index_html：开发者调试台（OTA、远程 shell、槽位、复位原因），要 k。 */
	if (client_side_ap(client)) {
		response_ctx->status = HTTP_200_OK;
		response_ctx->headers = page_user_header;
		response_ctx->header_count = ARRAY_SIZE(page_user_header);
		response_ctx->body = (const uint8_t *)user_html;
		response_ctx->body_len = sizeof(user_html) - 1;
		response_ctx->final_chunk = true;
		return 0;
	}

	/* 局域网侧的开发者页面仍然要口令，没带对的 k 就给"入口令"的小页面，而不是 401：
	 * 浏览器弹的 Basic 框既难看又常被当成页面坏了。 */
	if (!auth_pass(client, request_ctx, response_ctx)) {
		response_ctx->status = HTTP_200_OK;
		response_ctx->headers = page_dev_header;
		response_ctx->header_count = ARRAY_SIZE(page_dev_header);
		response_ctx->body = (const uint8_t *)unlock_html;
		response_ctx->body_len = sizeof(unlock_html) - 1;
		response_ctx->final_chunk = true;
		return 0;
	}

	response_ctx->status = HTTP_200_OK;
	response_ctx->headers = page_dev_header;
	response_ctx->header_count = ARRAY_SIZE(page_dev_header);
	response_ctx->body = (const uint8_t *)index_html;
	response_ctx->body_len = sizeof(index_html) - 1;
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic index_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET),
	},
	.cb = index_handler,
};

static const struct http_header captive_header[] = {
	/* 必须指向真实存在的资源（"/(根)"），不能指向 /index.html：
	 * 后者自身若没注册就会再落进通配资源，302 到自己 → 无限重定向，
	 * 浏览器报 ERR_TOO_MANY_REDIRECTS，并在环里把 TCP 上下文耗尽 */
	{.name = "Location", .value = "http://192.168.4.1/"},
	{.name = "Cache-Control", .value = "no-store"},
};

/* 任意路径 → 302 回配网页。手机的 captive 探测（iOS 的
 * captive.apple.com/hotspot-detect.html、Android 的 generate_204、
 * Windows 的 connecttest.txt）都会因此判定「这是个登录门户」。 */
static int captive_handler(struct http_client_ctx *client, enum http_transaction_status status,
			   const struct http_request_ctx *request_ctx,
			   struct http_response_ctx *response_ctx, void *user_data)
{
	ARG_UNUSED(client);
	/* 这里刻意不加 AUTH_GUARD：手机连上 AP 后会去探测
	 * /generate_204、/hotspot-detect.html 等路径，这些必须是 302 而不是 401，
	 * 否则系统判定"该网络需要登录"的逻辑会走歪。它只把请求 302 到 /，
	 * 而 / 自己有鉴权，所以并没有绕开。 */
	ARG_UNUSED(request_ctx);
	ARG_UNUSED(user_data);

	if (status == HTTP_SERVER_TRANSACTION_ABORTED) {
		return 0;
	}

	response_ctx->status = HTTP_302_FOUND;
	response_ctx->headers = captive_header;
	response_ctx->header_count = ARRAY_SIZE(captive_header);
	response_ctx->body = (const uint8_t *)"";
	response_ctx->body_len = 0;
	response_ctx->final_chunk = true;

	return 0;
}

struct http_resource_detail_dynamic captive_detail = {
	.common = {
		.type = HTTP_RESOURCE_TYPE_DYNAMIC,
		.bitmask_of_supported_http_methods = BIT(HTTP_GET) | BIT(HTTP_HEAD),
	},
	.cb = captive_handler,
};

/* --------------------------------------- GET /api/wlog：WiFi 事件账本 ---- */
/* 16 条 × 约 130 B ≈ 2.1 KB：原来给 920 B，多一个字段就会把数组截断成半截 JSON
 * （实测末尾停在 `{"t":12,"k":1` 处），前端 JSON.parse 直接失败。 */
