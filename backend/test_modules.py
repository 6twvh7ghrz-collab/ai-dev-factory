import requests, json

r = requests.get('http://localhost:8000/api/projects', timeout=5)
projects = r.json()['data']
for p in projects:
    pid = p['id']
    print(f'Project {pid}: {p["name"]} | stage={p["current_stage"]}')
    
    r2 = requests.get(f'http://localhost:8000/api/projects/{pid}/modules', timeout=5)
    mods = r2.json().get('data', [])
    print(f'  Modules: {len(mods)} items')
    for m in mods:
        print(f'    - {m["name"]} | features={len(m.get("features",[]))} | stage={m.get("version_stage","-")}')
