// Stockwise — single bundle: nav, reveal, FAQ, waitlist form.

// ── Nav toggle ─────────────────────────────────────────
function toggleNav(){const n=document.querySelector('nav');const o=n.classList.toggle('open');n.querySelector('.nav-toggle').setAttribute('aria-expanded',o)}
function closeNav(){const n=document.querySelector('nav');n.classList.remove('open');n.querySelector('.nav-toggle').setAttribute('aria-expanded','false')}

// ── Reveal on scroll ───────────────────────────────────
const io=new IntersectionObserver((entries)=>{
  entries.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}});
},{rootMargin:'0px 0px -40px 0px',threshold:.05});
document.querySelectorAll('.rv').forEach(el=>io.observe(el));

// ── FAQ accordion ──────────────────────────────────────
document.querySelectorAll('.fq').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const fi=btn.parentElement;
    const open=fi.classList.toggle('open');
    btn.setAttribute('aria-expanded',open);
  });
});

// ── Waitlist form ──────────────────────────────────────
const FORM_STEPS=4;             // 4 question steps then email step
const ans={s1:null,s2:null,s3:null};
let cur=1;

function oF(){
  document.getElementById('fo').classList.add('on');
  document.body.style.overflow='hidden';
  // Focus first option for keyboard users
  setTimeout(()=>{
    const first=document.querySelector('#f'+cur+' .fop, #f'+cur+' .fin');
    if(first) first.focus?.();
  },50);
}
function cF(){
  document.getElementById('fo').classList.remove('on');
  document.body.style.overflow='';
}

// Select an option in current step
function sO(el){
  const step=el.parentElement.dataset.s;
  el.parentElement.querySelectorAll('.fop').forEach(o=>o.classList.remove('sel'));
  el.classList.add('sel');
  ans['s'+step]=el.textContent.trim();
  document.getElementById('fnx').disabled=false;
}

function renderDots(){
  for(let i=1;i<=FORM_STEPS;i++){
    const d=document.getElementById('dd'+i);
    if(!d) continue;
    d.classList.remove('ac','dn');
    if(i<cur) d.classList.add('dn');
    else if(i===cur) d.classList.add('ac');
  }
  document.getElementById('fbk').style.display=cur>1?'block':'none';
}

// Steps: f1-f3 are MCQs, f4 is name/company/email inputs, f5 is the success screen.
function showStep(n){
  for(let i=1;i<=5;i++){
    const el=document.getElementById('f'+i);
    if(el) el.style.display=i===n?'block':'none';
  }
  document.getElementById('fnv').style.display=n===5?'none':'flex';
  cur=n;
  renderDots();
}

let emailWired=false;
function wireEmailStep(){
  if(emailWired) return; emailWired=true;
  const en=()=>{
    const name=document.getElementById('fN').value.trim();
    const email=document.getElementById('fE').value.trim();
    const ok=name.length>=2 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    document.getElementById('fnx').disabled=!ok;
  };
  ['fN','fE'].forEach(id=>document.getElementById(id).addEventListener('input',en));
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

// Close on Escape
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(document.getElementById('fo').classList.contains('on')) cF();
    if(document.querySelector('nav').classList.contains('open')) closeNav();
  }
});

// Init dots
renderDots();
