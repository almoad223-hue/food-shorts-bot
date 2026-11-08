import os, time, requests

cid = os.environ.get("CLIENT_ID") or input("CLIENT_ID: ").strip()
secret = os.environ.get("CLIENT_SECRET") or input("CLIENT_SECRET: ").strip()

r = requests.post("https://oauth2.googleapis.com/device/code",
                  data={"client_id": cid, "scope": "https://www.googleapis.com/auth/youtube"}).json()
if "device_code" not in r:
    raise SystemExit(r)

print("\n1) Open:", r["verification_url"])
print("2) Enter code:", r["user_code"])
print("Waiting for you to approve...\n")

interval = r.get("interval", 5)
while True:
    time.sleep(interval)
    t = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret,
        "device_code": r["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}).json()
    if "refresh_token" in t:
        print("YT_REFRESH_TOKEN =", t["refresh_token"])
        break
    err = t.get("error")
    if err == "slow_down":
        interval += 5
    elif err != "authorization_pending":
        raise SystemExit(t)
