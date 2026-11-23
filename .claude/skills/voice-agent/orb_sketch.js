// Orb visuals, copied from voice-mode/orb.html. Reads outAnalyser and outDataArray from voice.js.
  const audioState = { bass: 0, mid: 0, high: 0, amp: 0 };

  function computeBands(arr) {
    const len = arr.length;
    let bS=0, mS=0, hS=0;
    for (let i=0; i<len; i++) {
      if      (i < len*0.15) bS += arr[i];
      else if (i < len*0.50) mS += arr[i];
      else                   hS += arr[i];
    }
    return { bass:bS/(len*0.15)/255, mid:mS/(len*0.35)/255, high:hS/(len*0.50)/255, amp:(bS+mS+hS)/len/255 };
  }

  function updateAudioState() {
    let out={bass:0,mid:0,high:0,amp:0};
    if (outAnalyser&&outDataArray) { outAnalyser.getByteFrequencyData(outDataArray); out=computeBands(outDataArray); }
    audioState.bass = out.bass; audioState.mid = out.mid;
    audioState.high = out.high; audioState.amp = out.amp;
  }
  // ─── PURE HELPERS ────────────────────────────────────────────────────────────
  const tune = { brightness:1.0, size:1.1, speed:0.8, spread:1.1, smoke:220, core:1.0 };
  let setParticleCount = null;

  function hsbToRgb(h,s,b) {
    h/=360;s/=100;b/=100;
    let r,g,bv,i=Math.floor(h*6),f=h*6-i;
    let pp=b*(1-s),q=b*(1-f*s),tv=b*(1-(1-f)*s);
    switch(i%6){case 0:r=b;g=tv;bv=pp;break;case 1:r=q;g=b;bv=pp;break;case 2:r=pp;g=b;bv=tv;break;case 3:r=pp;g=q;bv=b;break;case 4:r=tv;g=pp;bv=b;break;case 5:r=b;g=pp;bv=q;break;}
    return [Math.round(r*255),Math.round(g*255),Math.round(bv*255)];
  }

  function buildSprites() {
    // Blue/cyan palette
    const cfgs=[{h:210,s:44},{h:215,s:40},{h:220,s:35},{h:210,s:23},{h:200,s:19},{h:195,s:15},{h:205,s:13},{h:200,s:10},{h:210,s:7}];
    return cfgs.map(({h,s})=>{
      const sz=128,cx=64,r=61, c=document.createElement('canvas');
      c.width=c.height=sz;
      const ctx=c.getContext('2d'), [rv,gv,bv]=hsbToRgb(h,s,90);
      const grad=ctx.createRadialGradient(cx,cx,0,cx,cx,r);
      grad.addColorStop(0,`rgba(${rv},${gv},${bv},0.94)`); grad.addColorStop(0.32,`rgba(${rv},${gv},${bv},0.52)`);
      grad.addColorStop(0.62,`rgba(${rv},${gv},${bv},0.16)`); grad.addColorStop(0.86,`rgba(${rv},${gv},${bv},0.03)`);
      grad.addColorStop(1,`rgba(${rv},${gv},${bv},0)`);
      ctx.fillStyle=grad; ctx.beginPath(); ctx.arc(cx,cx,r,0,Math.PI*2); ctx.fill();
      return c;
    });
  }

  // ─── P5 SKETCH ───────────────────────────────────────────────────────────────
  const sketch = (p) => {
    const particles = [];
    const sprites   = buildSprites();
    let coreSize=45, targetCoreSize=45;
    let sBass=0, sMid=0, sHigh=0, sAmp=0;

    function curl(x, y, t, ox, oy) {
      const eps=1.5, sc=0.0035;
      const dy = p.noise(x*sc+ox,(y+eps)*sc+oy,t) - p.noise(x*sc+ox,(y-eps)*sc+oy,t);
      const dx = p.noise((x+eps)*sc+ox,y*sc+oy,t) - p.noise((x-eps)*sc+ox,y*sc+oy,t);
      return { x: dy/(eps*sc*2), y: -dx/(eps*sc*2) };
    }

    class Smoke {
      constructor(idx) {
        this.type      = idx % 3;
        this.spriteIdx = this.type*3 + Math.floor(Math.random()*3);
        this.nox = p.random(100); this.noy = p.random(100); this.nosh = p.random(2000);
        this.rot = p.random(p.TWO_PI); this.rotSpd = p.random(-0.012,0.012);
        this.vx = p.random(-0.4,0.4); this.vy = p.random(-0.4,0.4);
        this.reset(true);
      }
      reset(init=false) {
        const a=p.random(p.TWO_PI);
        let r;
        if      (this.type===0) r=init?p.random(18,145):p.random(15,46);
        else if (this.type===1) r=init?p.random(44,235):p.random(38,76);
        else                    r=init?p.random(78,315):p.random(68,112);
        this.x=p.cos(a)*r; this.y=p.sin(a)*r;
        this.life=init?p.random(0.3,1.0):1.0;
        if      (this.type===0){this.baseSize=p.random(32,70); this.aspect=p.random(0.72,1.30);this.decay=p.random(0.0015,0.0040);}
        else if (this.type===1){this.baseSize=p.random(13,43); this.aspect=p.random(0.26,0.62);this.decay=p.random(0.0013,0.0034);}
        else                   {this.baseSize=p.random(4,15);  this.aspect=p.random(0.16,0.50);this.decay=p.random(0.001,0.0026);}
        this.sz=this.baseSize; this.ia=init?1.0:0.0;
      }
      update(energy,bv,mv,hv) {
        const te=this.type===0?bv:(this.type===1?mv:hv);
        const t=p.frameCount*0.0022;
        const c=curl(this.x,this.y,t,this.nox,this.noy);
        const cs=5+energy*12+te*8;
        const dist=p.max(p.sqrt(this.x*this.x+this.y*this.y),1);
        const push=1.2+energy*3.5+te*2.5;
        this.vx=p.lerp(this.vx,c.x*cs+(this.x/dist)*push,0.06);
        this.vy=p.lerp(this.vy,c.y*cs+(this.y/dist)*push,0.06);
        this.x+=this.vx*0.5; this.y+=this.vy*0.5;
        const ss=this.type===2?3.5:(this.type===1?1.8:0.55);
        this.rotSpd+=(p.noise(this.nosh+p.frameCount*0.004)-0.5)*0.0007;
        this.rotSpd=p.constrain(this.rotSpd,-0.042,0.042);
        this.rot+=this.rotSpd*ss*(1+te*2.2);
        const spd=p.sqrt(this.vx*this.vx+this.vy*this.vy);
        this.sz=this.baseSize*p.max(0.18,1.2/(1+spd*0.38))*(0.5+te*0.42+energy*0.2);
        this.ia=p.min(1.0,this.ia+0.045);
        this.life-=this.decay;
        if (this.life<=0||p.sqrt(this.x*this.x+this.y*this.y)>445) this.reset();
      }
      display() {
        const sp=tune.spread,sz=tune.size,br=tune.brightness;
        const dist=p.sqrt(this.x*this.x+this.y*this.y);
        const fs=this.type===0?95:(this.type===1?135:160);
        const fe=this.type===0?195:(this.type===1?250:285);
        const tF=Math.max(0,Math.min(1,(dist-fs)/(fe-fs)));
        const alpha=this.life*this.ia*(1-tF*tF*(3-2*tF))*(this.type===2?0.22:0.15)*br;
        if (alpha<=0.001) return;
        const w=this.sz*sz*(0.42+this.life*0.58);
        const ctx=p.drawingContext;
        ctx.save();
        ctx.translate(this.x*sp,this.y*sp); ctx.rotate(this.rot); ctx.scale(1,this.aspect);
        ctx.globalAlpha=alpha;
        ctx.drawImage(sprites[this.spriteIdx],-w,-w,w*2,w*2);
        ctx.restore();
      }
    }

    function drawCore(size) {
      if (tune.core===0) return;
      const c=tune.core, sz=tune.size, ctx=p.drawingContext, R=size*sz;
      ctx.save();
      const glow=ctx.createRadialGradient(0,0,R*0.15,0,0,R*5.8);
      glow.addColorStop(0,`rgba(45,115,215,${0.13*c})`); glow.addColorStop(0.28,`rgba(35,95,190,${0.06*c})`);
      glow.addColorStop(0.6,`rgba(25,75,160,${0.02*c})`); glow.addColorStop(1,`rgba(18,55,130,0)`);
      ctx.fillStyle=glow; ctx.beginPath(); ctx.arc(0,0,R*5.8,0,Math.PI*2); ctx.fill();
      const t=p.frameCount*0.012*tune.speed;
      const wA=p.noise(t)*0.08+0.96, wB=p.noise(t+50)*0.06+0.97, tilt=p.noise(t*0.4)*Math.PI*0.35;
      const sph=ctx.createRadialGradient(-R*0.26,-R*0.30,0,0,0,R*1.55);
      sph.addColorStop(0,`rgba(215,235,255,${0.94*c})`); sph.addColorStop(0.10,`rgba(165,205,255,${0.88*c})`);
      sph.addColorStop(0.26,`rgba(95,165,248,${0.76*c})`); sph.addColorStop(0.42,`rgba(52,120,215,${0.54*c})`);
      sph.addColorStop(0.58,`rgba(30,85,175,${0.28*c})`); sph.addColorStop(0.72,`rgba(22,65,150,${0.10*c})`);
      sph.addColorStop(0.88,`rgba(18,50,130,${0.03*c})`); sph.addColorStop(1,`rgba(14,38,110,0)`);
      ctx.fillStyle=sph; ctx.beginPath(); ctx.ellipse(0,0,R*wA*1.55,R*wB*1.55,tilt,0,Math.PI*2); ctx.fill();
      const rim=ctx.createRadialGradient(R*0.32,R*0.28,0,R*0.32,R*0.28,R*0.72);
      rim.addColorStop(0,`rgba(72,138,185,${0.20*c})`); rim.addColorStop(0.5,`rgba(55,112,160,${0.07*c})`); rim.addColorStop(1,`rgba(38,85,130,0)`);
      ctx.fillStyle=rim; ctx.beginPath(); ctx.arc(0,0,R*1.08,0,Math.PI*2); ctx.fill();
      for (let i=0;i<4;i++) {
        const sa=p.noise(p.frameCount*0.006+i*73.1)*Math.PI*2;
        const sr=R*(0.1+p.noise(p.frameCount*0.004+i*41.7)*0.45);
        const sx=Math.cos(sa)*sr-R*0.15, sy=Math.sin(sa)*sr-R*0.18;
        const sA=p.noise(p.frameCount*0.009+i*29.3)*0.18*c;
        const sh=ctx.createRadialGradient(sx,sy,0,sx,sy,R*0.18);
        sh.addColorStop(0,`rgba(230,245,255,${sA})`); sh.addColorStop(1,`rgba(200,230,255,0)`);
        ctx.fillStyle=sh; ctx.beginPath(); ctx.arc(sx,sy,R*0.18,0,Math.PI*2); ctx.fill();
      }
      ctx.restore();
    }

    p.setup = () => {
      const cnv = p.createCanvas(p.windowWidth, p.windowHeight);
      console.log('[orb] p5 setup OK:', cnv.width, 'x', cnv.height);
      p.colorMode(p.HSB, 360, 100, 100, 100);
      for (let i=0; i<220; i++) particles.push(new Smoke(i));
      setParticleCount = (n) => {
        while (particles.length < n) particles.push(new Smoke(particles.length));
        while (particles.length > n) particles.pop();
      };
    };

    p.draw = () => {
      updateAudioState();
      p.background(0, 0, 2, 18);
      {  // same place as the face (face.js layout): framed by Flight Deck the chat is on the left and narrower
        const emb = window.top !== window.self, wide = p.width > (emb ? 560 : 800);
        const pw = !emb ? 440 : p.width > 800 ? 380 : 300;
        p.translate(!wide ? p.width / 2 : emb ? pw + (p.width - pw) / 2 : (p.width - pw) / 2, wide ? p.height * 0.42 : p.height * 0.17);
      }

      const fr = audioState;
      sBass = p.lerp(sBass, fr.bass, fr.bass>sBass?0.18:0.13);
      sMid  = p.lerp(sMid,  fr.mid,  fr.mid >sMid ?0.18:0.10);
      sHigh = p.lerp(sHigh, fr.high, fr.high>sHigh?0.20:0.10);
      sAmp  = p.lerp(sAmp,  fr.amp,  fr.amp >sAmp ?0.18:0.13);

      const time    = p.frameCount*0.007*tune.speed;
      const breathe = (p.sin(time)+1)/2*0.16;
      const aBass = p.max(sBass, breathe*0.9);
      const aMid  = p.max(sMid,  breathe*0.5);
      const aHigh = p.max(sHigh, breathe*0.25);
      const aAmp  = p.max(sAmp,  breathe*0.55);

      for (const s of particles) s.update(aAmp,aBass,aMid,aHigh);
      p.blendMode(p.BLEND);
      for (const s of particles) { if (s.type>0) s.display(); }
      p.blendMode(p.ADD);
      for (const s of particles) { if (s.type===0) s.display(); }
      p.blendMode(p.BLEND);

      const rawBass=p.max(fr.bass,breathe*0.9), rawAmp=p.max(fr.amp,breathe*0.55);
      targetCoreSize = 33+(rawBass*0.55+aBass*0.45)*36+(rawAmp*0.55+aAmp*0.45)*54;
      coreSize = p.lerp(coreSize, targetCoreSize, targetCoreSize>coreSize?0.15:0.09);
      drawCore(coreSize);
    };

    p.windowResized = () => { p.resizeCanvas(p.windowWidth, p.windowHeight); };
  };

  new p5(sketch);
