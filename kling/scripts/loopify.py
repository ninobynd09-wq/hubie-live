# usage: loopify.py in.mp4 out.mp4  -> drops dup last frame, blends last N frames into frame 0, h264 yuv420p faststart, no audio
import sys,subprocess,json,numpy as np
i,o=sys.argv[1],sys.argv[2]; N=int(sys.argv[3]) if len(sys.argv)>3 else 6
p=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,r_frame_rate","-of","json",i],capture_output=True).stdout)["streams"][0]
w,h,fps=p["width"],p["height"],p["r_frame_rate"]
raw=subprocess.run(["ffmpeg","-v","error","-i",i,"-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True).stdout
f=np.frombuffer(raw,np.uint8).reshape(-1,h,w,3).astype(np.float32)
print("frames",len(f),"seam diff first/last",abs(f[0]-f[-1]).mean())
f=f[:-1].copy(); n=len(f)
for k in range(N):
    a=(k+1)/(N+1); j=n-N+k
    f[j]=f[j]*(1-a)+f[0]*a
enc=subprocess.Popen(["ffmpeg","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{w}x{h}","-r",fps,"-i","-","-c:v","libx264","-preset","slow","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",o],stdin=subprocess.PIPE)
enc.stdin.write(np.clip(f,0,255).astype(np.uint8).tobytes()); enc.stdin.close(); enc.wait()
print("wrote",o,n,"frames")
