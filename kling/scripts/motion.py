import sys,subprocess,numpy as np
for i in sys.argv[1:]:
    raw=subprocess.run(["ffmpeg","-v","error","-i",i,"-vf","scale=388:-1,format=gray","-f","rawvideo","-"],capture_output=True).stdout
    f=np.frombuffer(raw,np.uint8).reshape(-1,259,388).astype(float)
    print(i,"avg change vs first frame %.1f, max %.1f"%(np.mean([abs(x-f[0]).mean() for x in f]),max(abs(x-f[0]).mean() for x in f)))
