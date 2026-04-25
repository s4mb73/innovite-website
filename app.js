// ── AI Qualifier ─────────────────────────────────────
const qa={};let qc=1;const qTotal=4;let qAutoTimer=null;

function oQ(){
  document.getElementById('qo').classList.add('on');
  document.body.style.overflow='hidden';
  setTimeout(()=>document.querySelector('#qo .qx').focus(),50);
  if(window.plausible) plausible('Qualifier Open');
}
function cQ(){
  document.getElementById('qo').classList.remove('on');
  document.body.style.overflow='';
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&document.getElementById('qo').classList.contains('on'))cQ();
});

function qSel(el,key,val){
  if(qAutoTimer)clearTimeout(qAutoTimer);
  el.parentElement.querySelectorAll('.qopt').forEach(o=>o.classList.remove('sel'));
  el.classList.add('sel');
  qa[key]=val;
  document.getElementById('qnext').disabled=false;
  // auto-advance after 450ms — feels conversational, still lets user see their pick
  qAutoTimer=setTimeout(()=>qNext(),450);
}

function qProg(n,total){
  document.getElementById('qpfill').style.width=((n-1)/total*100)+'%';
}

function qShow(n){
  document.querySelectorAll('.qstep').forEach(s=>s.classList.remove('active'));
  document.getElementById('qq'+n).classList.add('active');
  document.getElementById('qback').style.display=n>1?'inline-block':'none';
  document.getElementById('qnext').disabled=!qa['q'+n];
  document.getElementById('qnext').textContent=n===qTotal?'See my result →':'Next';
  document.getElementById('qstep-label').textContent='Question '+n+' of '+qTotal;
  qProg(n,qTotal);
  qc=n;
}

function qNext(){
  if(qAutoTimer)clearTimeout(qAutoTimer);
  // B2C short-circuit — no point asking 3 more questions
  if(qc===1&&qa.q1==='b2c'){qAnalyse();return;}
  if(qc<qTotal){qShow(qc+1);}
  else{qAnalyse();}
}
function qPrev(){
  if(qAutoTimer)clearTimeout(qAutoTimer);
  if(qc>1)qShow(qc-1);
}

function qAnalyse(){
  document.getElementById('qmain').style.display='none';
  document.getElementById('qanalyse').style.display='block';
  setTimeout(qShowResult,1600);
}

// Weighted scoring: business model 30 | deal value 30 | authority 20 | timeline 20 = 100
function qScore(){
  let score=0,flags=[],hardNo=false;

  // Business model
  if(qa.q1==='b2c'){
    hardNo=true;
    flags.push({bad:true,t:'You sell to consumers — our system is built for B2B'});
  } else if(qa.q1==='mix'){
    score+=15;
    flags.push({bad:false,t:'Mostly B2B — we\'d focus the system there'});
  } else {
    score+=30;
    flags.push({bad:false,t:'Pure B2B service firm — exactly who we\'re built for'});
  }

  // Deal value
  if(qa.q2==='sub2k'){
    hardNo=true;
    flags.push({bad:true,t:'Deal value under £2k — the ROI maths doesn\'t work at this level'});
  } else if(qa.q2==='2to5k'){
    score+=8;
    flags.push({bad:false,t:'£2–5k deal value — workable if volume is there'});
  } else if(qa.q2==='5to15k'){
    score+=22;
    flags.push({bad:false,t:'£5–15k contracts — solid economics for outbound'});
  } else if(qa.q2==='15kplus'){
    score+=30;
    flags.push({bad:false,t:'£15k+ deals — this is where we do our best work'});
  }

  // Decision authority
  if(qa.q3==='yes'){
    score+=20;
    flags.push({bad:false,t:'You\'re the decision-maker — we can move quickly'});
  } else if(qa.q3==='partner'){
    score+=14;
    flags.push({bad:false,t:'Shared decision — straightforward to get aligned'});
  } else if(qa.q3==='no'){
    score+=5;
    flags.push({bad:false,t:'Sign-off needed — we\'d want the right people in the room early'});
  }

  // Timeline
  if(qa.q4==='now'){
    score+=20;
    flags.push({bad:false,t:'Ready now — we can start building immediately'});
  } else if(qa.q4==='soon'){
    score+=14;
    flags.push({bad:false,t:'Starting in 1–3 months — good window to plan ahead'});
  } else if(qa.q4==='exploring'){
    score+=5;
    flags.push({bad:false,t:'Just exploring — no pressure, reach out when the time\'s right'});
  }

  return{score,flags,hardNo};
}

function qShowResult(){
  document.getElementById('qanalyse').style.display='none';
  const r=document.getElementById('qresult');
  r.style.display='block';
  const{score,flags,hardNo}=qScore();
  let icon,heading,body,cta,resultBucket;

  if(hardNo){
    resultBucket='hard-no';
    icon='<div class="qr-icon nope">&#10005;</div>';
    heading='We\'re not the right fit — and we\'d rather tell you now.';
    body='We only take on clients where we\'re confident the numbers work. Based on your answers, that\'s not the case here. Better to be honest than waste each other\'s time.';
    cta='<p style="font-size:12.5px;color:var(--t3);margin-top:4px">If things change — deal size grows, you shift to B2B — we\'d love to talk. <a href="mailto:sammy@innoviteai.com" style="color:var(--accent)">Drop us a line.</a></p>';
  } else if(score>=70){
    resultBucket='strong-fit';
    icon='<div class="qr-icon fit">&#10003;</div>';
    heading='You look like a strong fit.';
    body='Based on what you\'ve told us, we\'d be confident working together. The strategy call is free — we\'ll walk you through exactly what we\'d build and what you\'d expect to get back.';
    cta='<a href="#" onclick="cQ();oF();return false" class="btn btn-a" style="width:100%;justify-content:center;padding:13px;font-size:14px">Book a free strategy call →</a>';
  } else if(score>=45){
    resultBucket='maybe';
    icon='<div class="qr-icon maybe">&#8764;</div>';
    heading='There\'s probably something here.';
    body='It\'s not a clear-cut yes, but the fundamentals are there. A 20-minute call is the right next step — we can dig into whether the numbers make sense for your specific situation.';
    cta='<a href="#" onclick="cQ();oF();return false" class="btn btn-a" style="width:100%;justify-content:center;padding:13px;font-size:14px">Book a call — no obligation</a>';
  } else {
    resultBucket='not-yet';
    icon='<div class="qr-icon maybe">&#8764;</div>';
    heading='The timing might not be right yet.';
    body='The fundamentals look okay, but a few things suggest now isn\'t the moment. Come back when you\'re ready to move — the first conversation is always free.';
    cta='<p style="font-size:12.5px;color:var(--t3);margin-top:4px">No rush. <a href="mailto:sammy@innoviteai.com" style="color:var(--accent)">Email us when the time is right.</a></p>';
  }
  if(window.plausible) plausible('Qualifier Result',{props:{result:resultBucket,score:score}});

  document.getElementById('qr-icon').outerHTML=icon;
  document.getElementById('qr-h').textContent=heading;
  document.getElementById('qr-p').textContent=body;
  const why=document.getElementById('qr-why');
  why.innerHTML='<div class="qr-why-label">What we looked at</div>'+flags.map(f=>`<div class="qr-why-item${f.bad?' bad':''}">${f.t}</div>`).join('');
  document.getElementById('qr-cta').innerHTML=cta;
}

// ── Showcase carousel (transform-based, no scroll API) ───
var scIdx=0,scGrid,scTrack,scDots,scCards,scTouchX=0;

function scVis(){return window.innerWidth>640?2:1;}
function scPages(){return Math.ceil(scCards.length/scVis());}
function scCardW(){return scCards[0].offsetWidth+16;}

function scBuildDots(){
  scDots.innerHTML='';
  for(var i=0;i<scPages();i++){
    var b=document.createElement('button');
    b.className='sc-dot'+(i===0?' on':'');
    b.setAttribute('aria-label','Slide '+(i+1));
    (function(n){b.onclick=function(){scGoTo(n);};})(i);
    scDots.appendChild(b);
  }
}

function scGoTo(i){
  scIdx=Math.max(0,Math.min(scPages()-1,i));
  var tx=scIdx*scVis()*scCardW();
  scTrack.style.transform='translateX(-'+tx+'px)';
  document.getElementById('scPrev').disabled=scIdx===0;
  document.getElementById('scNext').disabled=scIdx>=scPages()-1;
  var dots=scDots.querySelectorAll('.sc-dot');
  for(var j=0;j<dots.length;j++) dots[j].classList.toggle('on',j===scIdx);
}

function scNav(dir){scGoTo(scIdx+dir);}

scGrid=document.getElementById('scGrid');
scTrack=document.getElementById('scTrack');
scDots=document.getElementById('scDots');
if(scGrid&&scTrack){
  scCards=scTrack.querySelectorAll('.showcase-card');
  scBuildDots();scGoTo(0);
  window.addEventListener('resize',function(){scBuildDots();scGoTo(0);});
  // Touch swipe
  scGrid.addEventListener('touchstart',function(e){scTouchX=e.touches[0].clientX;},{passive:true});
  scGrid.addEventListener('touchend',function(e){
    var dx=scTouchX-e.changedTouches[0].clientX;
    if(Math.abs(dx)>40)scNav(dx>0?1:-1);
  },{passive:true});
}

// ── Wistia lazy-load ────────────────────────────────
// Defer the Wistia player + per-video embed scripts until a card enters the viewport.
// Saves ~600KB of JS on initial load for visitors who never scroll to the work section.
(function(){
  let playerLoaded=false;
  const loadedEmbeds=new Set();
  function loadScript(src,asModule){
    const s=document.createElement('script');
    s.src=src;s.async=true;
    if(asModule) s.type='module';
    document.head.appendChild(s);
  }
  function ensurePlayer(){
    if(playerLoaded) return;
    playerLoaded=true;
    loadScript('https://fast.wistia.com/player.js',false);
  }
  function ensureEmbed(mediaId){
    if(loadedEmbeds.has(mediaId)||!mediaId) return;
    loadedEmbeds.add(mediaId);
    loadScript('https://fast.wistia.com/embed/'+mediaId+'.js',true);
  }
  const wistiaObs=new IntersectionObserver(function(entries){
    entries.forEach(function(en){
      if(!en.isIntersecting) return;
      const p=en.target.querySelector('wistia-player');
      if(p){ensurePlayer();ensureEmbed(p.getAttribute('media-id'));}
      wistiaObs.unobserve(en.target);
    });
  },{rootMargin:'200px'});
  document.querySelectorAll('.showcase-video').forEach(function(el){wistiaObs.observe(el);});
})();

// ── Reveal on scroll
const ob=new IntersectionObserver(e=>{e.forEach(x=>{if(x.isIntersecting)x.target.classList.add('in')})},{threshold:.06});
document.querySelectorAll('.rv').forEach(e=>ob.observe(e));

// FAQ accordion — keeps aria-expanded in sync
document.querySelectorAll('.fq').forEach(b=>{
  b.addEventListener('click',()=>{
    const i=b.parentElement;
    const o=i.classList.contains('open');
    document.querySelectorAll('.fi.open').forEach(x=>{
      x.classList.remove('open');
      x.querySelector('.fq').setAttribute('aria-expanded','false');
    });
    if(!o){i.classList.add('open');b.setAttribute('aria-expanded','true');}
  });
});

// Form modal — ESC to close + focus trap
let c=1;const d={};
function oF(){
  const m=document.getElementById('fo');
  m.classList.add('on');
  document.body.style.overflow='hidden';
  // Move focus to close button
  setTimeout(()=>m.querySelector('.fx').focus(),50);
  if(window.plausible) plausible('Form Open');
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){cF();}
  if(e.key==='Tab'&&document.getElementById('fo').classList.contains('on')){
    const sel='button:not([disabled]),input,[tabindex]:not([tabindex="-1"])';
    const els=[...document.getElementById('fo').querySelectorAll(sel)].filter(el=>el.offsetParent!==null);
    if(!els.length)return;
    const fi=els[0],la=els[els.length-1];
    if(e.shiftKey&&document.activeElement===fi){e.preventDefault();la.focus();}
    else if(!e.shiftKey&&document.activeElement===la){e.preventDefault();fi.focus();}
  }
});
function cF(){document.getElementById('fo').classList.remove('on');document.body.style.overflow=''}
function sO(e){
  e.parentElement.querySelectorAll('.fop').forEach(o=>o.classList.remove('sel'));
  e.classList.add('sel');
  // Strip the leading .foi icon span so we save just the label text,
  // not "◆Professional services" or "££5k — £15k".
  const clone=e.cloneNode(true);
  const icon=clone.querySelector('.foi');
  if(icon) icon.remove();
  d['s'+c]=clone.textContent.trim();
  document.getElementById('fnx').disabled=false;
}
function sh(n){
  for(let i=1;i<=5;i++)document.getElementById('f'+i).style.display=i===n?'block':'none';
  document.getElementById('f6').style.display='none';
  document.getElementById('fbk').style.display=n>1?'inline-block':'none';
  document.getElementById('fnx').textContent=n===5?'Submit':'Next';
  document.getElementById('fnx').disabled=n<5?!d['s'+n]:false;
  for(let i=1;i<=5;i++){
    const dd=document.getElementById('dd'+i);
    dd.className='fd'+(i===n?' ac':i<n?' dn':'');
  }
  c=n;
  if(window.plausible) plausible('Form Step',{props:{step:n}});
}
function nx(){if(c===5){sb();return}sh(c+1)}
function pv(){if(c>1)sh(c-1)}
// Posts to the Vercel serverless function at /api/submit, which writes the lead
// to Supabase. See SETUP.md for the env vars Vercel needs to make this work.
const SUBMIT_ENDPOINT='/api/submit';

async function sb(){
  d.name=document.getElementById('fN').value.trim();
  d.company=document.getElementById('fC').value.trim();
  d.email=document.getElementById('fE').value.trim();
  d.phone=document.getElementById('fP').value.trim();

  const emailOk=/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(d.email);
  const err=document.getElementById('fErr');
  if(!d.name||!emailOk){
    document.getElementById('fN').style.borderColor=d.name?'':'#f87171';
    document.getElementById('fE').style.borderColor=emailOk?'':'#f87171';
    err.textContent=!d.name?'Please enter your name.':'Please enter a valid email address.';
    err.style.display='block';
    return;
  }
  err.style.display='none';

  const btn=document.getElementById('fnx');
  btn.disabled=true;
  btn.textContent='Sending…';

  try{
    // Send the form payload + any qualifier answers the visitor already gave.
    // qa is the AI Qualifier's answer object; empty if they skipped it.
    const payload={...d, qualifier: Object.keys(qa).length ? qa : null};
    const res=await fetch(SUBMIT_ENDPOINT,{
      method:'POST',
      headers:{'Accept':'application/json','Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
    if(!res.ok){
      const payload=await res.json().catch(()=>({}));
      throw new Error(payload.error||('Submit failed: '+res.status));
    }
    for(let i=1;i<=5;i++)document.getElementById('f'+i).style.display='none';
    document.getElementById('f6').style.display='block';
    document.getElementById('fnv').style.display='none';
    if(window.plausible) plausible('Form Submit');
  }catch(e){
    btn.disabled=false;
    btn.textContent='Submit';
    err.innerHTML='Something went wrong. Please email <a href="mailto:sammy@innoviteai.com" style="color:var(--accent)">sammy@innoviteai.com</a> directly.';
    err.style.display='block';
  }
}
