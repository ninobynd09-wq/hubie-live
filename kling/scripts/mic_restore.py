# usage: mic_restore.py in.mp4 out.mp4 [check.png]   (react_banana v3b: the bite hides the headset mic boom ~frames 44-72)
# NOTE: loads the whole 5 s clip as float32 (~3 GB). Run under a memory cap, e.g. prlimit --as=4500000000, never beside other heavy jobs.
import sys, subprocess, numpy as np, cv2
ACC=["-sws_flags","accurate_rnd+full_chroma_int"]; W,H=1164,776
i,o=sys.argv[1],sys.argv[2]
f=np.frombuffer(subprocess.run(["ffmpeg","-v","error","-i",i,*ACC,"-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True).stdout,np.uint8).reshape(-1,H,W,3).astype(np.float32)
g=[cv2.cvtColor(x.astype(np.uint8),cv2.COLOR_RGB2GRAY) for x in f]
reg=np.zeros((H,W),np.uint8)
pts=np.array([[420,256],[426,276],[434,292],[450,306],[470,316]],np.int32)
cv2.polylines(reg,[pts],False,255,14); cv2.ellipse(reg,(484,320),(24,18),15,0,360,255,-1)
m=((g[0]<70)&(reg>0)).astype(np.uint8)*255
m=cv2.morphologyEx(m,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
m=cv2.GaussianBlur(cv2.dilate(m,np.ones((3,3),np.uint8)).astype(np.float32)/255,(0,0),1.2)
def track(c,r,k,prev):
    x,y=c; t=g[0][y-r:y+r,x-r:x+r]; px,py=prev; s=40
    win=g[k][max(0,py-r-s):py+r+s,max(0,px-r-s):px+r+s]
    res=cv2.matchTemplate(win,t,cv2.TM_CCOEFF_NORMED); _,sc,_,ml=cv2.minMaxLoc(res)
    return (max(0,px-r-s)+ml[0]+r, max(0,py-r-s)+ml[1]+r), sc
A=[(596,236),22]; B=[(420,212),18]
pa,pb=A[0],B[0]; T=[]; vis=[]; SC=[]; PTS=[]
for k in range(len(f)):
    pa,sa=track(A[0],A[1],k,pa); nb,sb=track(B[0],B[1],k,pb)
    if sb>0.78: pb=nb
    src=np.float32([A[0],B[0]]); dst=np.float32([pa,pb])
    M,_=cv2.estimateAffinePartial2D(src,dst); T.append(M); SC.append(min(sa,sb)); PTS.append((pa,round(sa,2),pb,round(sb,2)))
P=np.array([[np.hypot(M[0,0],M[1,0]),np.arctan2(M[1,0],M[0,0]),M[0,2],M[1,2]] for M in T])
SC=np.array(SC); good=SC>0.78
for k in range(1,len(PTS)):
    if np.hypot(PTS[k][2][0]-PTS[k-1][2][0],PTS[k][2][1]-PTS[k-1][2][1])>12: good[k:k+1]=False
# a big jump in angle against both neighbours is a lost track too
for k in range(1,len(P)-1):
    if abs(P[k,1]-P[k-1,1])>0.03 and abs(P[k,1]-P[k+1,1])>0.03: good[k]=False
idx=np.arange(len(P))
for c in range(2): P[:,c]=np.interp(idx,idx[good],P[good,c])
for k in idx[~good]:   # lost earcup: interpolated scale/angle, translation from the nose (always tracked)
    sc,t=P[k,0],P[k,1]; R=sc*np.array([[np.cos(t),-np.sin(t)],[np.sin(t),np.cos(t)]])
    P[k,2:]=np.array(PTS[k][0])-R@np.array(A[0])
P=np.array([np.convolve(np.pad(P[:,c],2,mode='edge'),np.ones(5)/5,'valid') for c in range(4)]).T
T=[np.float32([[s*np.cos(t),-s*np.sin(t),x],[s*np.sin(t),s*np.cos(t),y]]) for s,t,x,y in P]
print("bad tracks", list(idx[~good]))
for k in range(len(f)):
    M=T[k]; bm=cv2.warpAffine(m,M,(W,H)); b0=cv2.warpAffine(g[0],M,(W,H)).astype(np.float32)
    core=bm>0.6; vis.append(float(abs(g[k].astype(np.float32)[core]-b0[core]).mean()))
vis=np.array(vis); a=np.clip((vis-25)/20,0,1)
a=np.convolve(np.pad(a,4,mode='edge'),np.ones(9)/9,'valid')
out=f.copy()
for k in range(len(f)):
    if a[k]<0.01: continue
    bm=cv2.warpAffine(m,T[k],(W,H))[...,None]*a[k]; b0=cv2.warpAffine(f[0],T[k],(W,H))
    out[k]=f[k]*(1-bm)+b0*bm
print("vis", " ".join(f"{k}:{v:.0f}" for k,v in enumerate(vis) if k%3==0))
print("alpha", " ".join(f"{k}:{a[k]:.2f}" for k in range(len(a)) if a[k]>0.01))
if len(sys.argv)>3:
    tl=[]
    for k in range(0,len(f),6):
        x=out[k].astype(np.uint8).copy(); bm=(cv2.warpAffine(m,T[k],(W,H))>0.5).astype(np.uint8)
        c,_=cv2.findContours(bm,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE); y=f[k].astype(np.uint8).copy(); cv2.drawContours(y,c,-1,(255,0,0),1)
        tl.append(np.vstack([cv2.resize(y[180:380,340:640],(300,200)),cv2.resize(x[180:380,340:640],(300,200))]))
        cv2.putText(tl[-1],str(k),(5,20),0,0.6,(255,255,0),2)
    rows=[np.hstack(tl[r*7:(r+1)*7]) for r in range(len(tl)//7)]
    cv2.imwrite(sys.argv[3],cv2.cvtColor(np.vstack(rows),cv2.COLOR_RGB2BGR))
enc=subprocess.Popen(["ffmpeg","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r","24","-i","-",*ACC,"-c:v","libx264","-preset","slow","-crf","14","-pix_fmt","yuv420p",o],stdin=subprocess.PIPE)
enc.stdin.write(np.clip(out,0,255).astype(np.uint8).tobytes()); enc.stdin.close(); enc.wait()
