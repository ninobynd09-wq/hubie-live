# usage: frames_cut_stream.py in.mp4 out.mp4 N anchor.mp4 : frames_cut.py with spec "1-N", same output, one frame in memory
import sys, subprocess, numpy as np, cv2
ACC=["-sws_flags","accurate_rnd+full_chroma_int"]; W,H=1164,776; FS=W*H*3
i,o,N,anc=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]
a0=np.frombuffer(subprocess.run(["ffmpeg","-v","error","-i",anc,"-frames:v","1",*ACC,"-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True).stdout,np.uint8).reshape(H,W,3).astype(np.float32)
dec=subprocess.Popen(["ffmpeg","-v","error","-i",i,*ACC,"-f","rawvideo","-pix_fmt","rgb24","-"],stdout=subprocess.PIPE)
enc=subprocess.Popen(["ffmpeg","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r","24","-i","-",*ACC,"-c:v","libx264","-preset","slow","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",o],stdin=subprocess.PIPE)
rd=lambda: np.frombuffer(dec.stdout.read(FS),np.uint8).reshape(H,W,3).astype(np.float32)
f0=rd(); off=cv2.GaussianBlur(a0-f0,(0,0),8)
bg=np.zeros((H,W),bool); bg[:int(H*.5),int(W*.86):]=True; bg[:int(H*.5),:int(W*.1)]=True; ref=a0[bg].mean(0)
enc.stdin.write(a0.astype(np.uint8).tobytes()); n=N+1; Nb=8
for k in range(1,N+1):
    x=rd()+off; x+=ref-x[bg].mean(0)
    if k<4: x=a0*(1-k/4)+x*(k/4)
    j=k-(n-Nb)
    if j>=0: a=(j+1)/(Nb+1); x=x*(1-a)+a0*a
    enc.stdin.write(np.clip(x,0,255).astype(np.uint8).tobytes())
dec.kill(); enc.stdin.close(); enc.wait(); print("wrote",o,n)
