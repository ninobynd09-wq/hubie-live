import os, sys, json, base64, urllib.request
img=base64.b64encode(open(sys.argv[1],'rb').read()).decode()
prompt=open(sys.argv[2]).read().strip()
body={"contents":[{"type":"prompt","text":prompt},{"type":"first_frame","url":img},] + ([{"type":"last_frame","url":(base64.b64encode(open(os.environ["LASTIMG"],'rb').read()).decode() if os.environ.get("LASTIMG") else img)}] if os.environ.get("NOLAST")!="1" else []) + [],
 "settings":{"resolution":"720p","duration":int(sys.argv[3]) if len(sys.argv)>3 else 5,**({} if os.environ.get("KAUDIO")=="omit" else {"audio":os.environ.get("KAUDIO","off")}),**({} if os.environ.get("KMS")=="omit" else {"multi_shot":False})},
 "options":{"watermark_info":{"enabled":False}}}
r=urllib.request.Request("https://api-singapore.klingai.com/image-to-video/"+os.environ.get("KMODEL","kling-3.0")+"",data=json.dumps(body).encode(),
 headers={"Authorization":"Bearer "+os.environ["KLING_API_KEY"],"Content-Type":"application/json"})
try: print(urllib.request.urlopen(r,timeout=60).read().decode())
except urllib.error.HTTPError as e: print(e.code, e.read().decode())
