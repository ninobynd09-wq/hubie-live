import os, sys, json, time, urllib.request
tid=sys.argv[1]; out=sys.argv[2]
H={"Authorization":"Bearer "+os.environ["KLING_API_KEY"],"Content-Type":"application/json"}
while True:
    d=json.loads(urllib.request.urlopen(urllib.request.Request("https://api-singapore.klingai.com/tasks?task_ids="+tid,headers=H),timeout=30).read())
    t=d["data"][0]; print(t["status"], t.get("message"), flush=True)
    if t["status"]=="succeeded":
        print(json.dumps(t.get("billing")))
        u=[o for o in t["outputs"] if o["type"]=="video"][0]["url"]
        urllib.request.urlretrieve(u,out); print("saved",out); break
    if t["status"]=="failed": print(json.dumps(t)); break
    time.sleep(15)
