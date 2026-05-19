// Stockwise — nav, reveal, count-up, cursor glow, waitlist form.

// ── Nav toggle ─────────────────────────────────────────
function toggleNav(){const n=document.querySelector('nav');const o=n.classList.toggle('open');n.querySelector('.nav-toggle').setAttribute('aria-expanded',o)}
function closeNav(){const n=document.querySelector('nav');n.classList.remove('open');n.querySelector('.nav-toggle').setAttribute('aria-expanded','false')}

// ── Reveal on scroll ───────────────────────────────────
const io=new IntersectionObserver((entries)=>{
  entries.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}});
},{rootMargin:'0px 0px -60px 0px',threshold:.05});
document.querySelectorAll('.rv').forEach(el=>io.observe(el));

// ── Count-up on dashboard numbers ──────────────────────
const countIo=new IntersectionObserver((entries)=>{
  entries.forEach(e=>{
    if(!e.isIntersecting) return;
    const el=e.target;
    const target=parseFloat(el.dataset.count);
    const suffix=el.dataset.suffix||'';
    const dur=1400;
    const start=performance.now();
    const initial=0;
    const step=(now)=>{
      const t=Math.min(1,(now-start)/dur);
      const eased=1-Math.pow(1-t,3);
      const val=Math.round(initial+(target-initial)*eased);
      el.textContent=val.toLocaleString('en-US')+suffix;
      if(t<1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    countIo.unobserve(el);
  });
},{threshold:.3});
document.querySelectorAll('[data-count]').forEach(el=>countIo.observe(el));

// ── Cursor glow on feature cards ───────────────────────
document.querySelectorAll('[data-glow]').forEach(card=>{
  card.addEventListener('pointermove',e=>{
    const r=card.getBoundingClientRect();
    card.style.setProperty('--mx',((e.clientX-r.left)/r.width*100)+'%');
    card.style.setProperty('--my',((e.clientY-r.top)/r.height*100)+'%');
  });
});

// ── Waitlist form ──────────────────────────────────────
const ans={s1:null,s2:null,s3:null};
let cur=1;

function oF(){
  document.getElementById('fo').classList.add('on');
  document.body.style.overflow='hidden';
  setTimeout(()=>{
    const first=document.querySelector('#f'+cur+' .fop, #f'+cur+' .fin');
    if(first) first.focus?.();
  },50);
}
function cF(){
  document.getElementById('fo').classList.remove('on');
  document.body.style.overflow='';
}

function sO(el){
  const step=el.parentElement.dataset.s;
  el.parentElement.querySelectorAll('.fop').forEach(o=>o.classList.remove('sel'));
  el.classList.add('sel');
  ans['s'+step]=el.textContent.trim();
  document.getElementById('fnx').disabled=false;
}

function renderDots(){
  for(let i=1;i<=4;i++){
    const d=document.getElementById('dd'+i);
    if(!d) continue;
    d.classList.remove('ac','dn');
    if(i<cur) d.classList.add('dn');
    else if(i===cur) d.classList.add('ac');
  }
  document.getElementById('fbk').style.display=cur>1?'block':'none';
}

function showStep(n){
  for(let i=1;i<=5;i++){
    const el=document.getElementById('f'+i);
    if(el) el.style.display=i===n?'block':'none';
  }
  document.getElementById('fnv').style.display=n===5?'none':'flex';
  cur=n;
  renderDots();
}

function syncNextBtn(){
  const btn=document.getElementById('fnx');
  if(cur===4){
    btn.textContent='Join waitlist';
    const name=document.getElementById('fN').value.trim();
    const email=document.getElementById('fE').value.trim();
    btn.disabled=!(name.length>=2 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email));
  }else{
    btn.textContent='Next';
    btn.disabled=!ans['s'+cur];
  }
}

let emailWired=false;
function wireEmailStep(){
  if(emailWired) return; emailWired=true;
  const en=()=>syncNextBtn();
  ['fN','fE'].forEach(id=>document.getElementById(id).addEventListener('input',en));
}

function nx(){
  if(cur===4) return submitForm();
  showStep(cur+1);
  if(cur===4) wireEmailStep();
  syncNextBtn();
}

function pv(){
  if(cur>1) showStep(cur-1);
  syncNextBtn();
}

async function submitForm(){
  const name=document.getElementById('fN').value.trim();
  const company=document.getElementById('fC').value.trim();
  const email=document.getElementById('fE').value.trim();
  const err=document.getElementById('fErr');
  err.style.display='none';

  if(name.length<2){err.textContent='Please enter your name.';err.style.display='block';return}
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)){err.textContent='Please enter a valid email.';err.style.display='block';return}

  const btn=document.getElementById('fnx');
  btn.disabled=true;btn.textContent='Sending…';

  try{
    const r=await fetch('/api/waitlist',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,company,email,s1:ans.s1,s2:ans.s2,s3:ans.s3})
    });
    if(!r.ok){
      const j=await r.json().catch(()=>({}));
      throw new Error(j.error||'Could not submit');
    }
    showStep(5);
  }catch(e){
    err.textContent=e.message;err.style.display='block';
    btn.disabled=false;btn.textContent='Join waitlist';
  }
}

document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(document.getElementById('fo').classList.contains('on')) cF();
    if(document.querySelector('nav').classList.contains('open')) closeNav();
  }
});

renderDots();
