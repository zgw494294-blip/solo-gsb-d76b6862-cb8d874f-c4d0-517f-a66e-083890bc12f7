"""End-to-end checks for the stateful drill sessions (requirements walkthrough)."""

import concurrent.futures

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def protocol(handshake="m"):
    # A: 0 -send m-> 1 ; B: 0 -recv m-> 1. Completion requires t1 then t2.
    return {
        "A": {"states": [0, 1], "initial": 0, "finals": [1]},
        "B": {"states": [0, 1], "initial": 0, "finals": [1]},
        "transitions": [
            {"id": "t1", "from": "A", "to": "B", "action": "send",
             "message": handshake, "source": 0, "target": 1},
            {"id": "t2", "from": "A", "to": "B", "action": "receive",
             "message": handshake, "source": 0, "target": 1},
        ],
    }


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok -", msg)


# 1. create session -> id + version 0 --------------------------------------
r = client.post("/v1/sessions", json=protocol())
check(r.status_code == 201, f"create returns 201, got {r.status_code} {r.text}")
body = r.json()
sid = body["session_id"]
check(isinstance(sid, str) and len(sid) >= 8, "session id returned")
check(body["version"] == 0, "initial version is 0")
check(body["completed"] is False, "not completed at start")
check(body["configuration"] == {
    "state": {"A": 0, "B": 0},
    "queues": {"A_to_B": None, "B_to_A": None},
}, "initial configuration")
check(body["trace"] == [], "empty trace")
check(body["enabled_transition_ids"] == ["t1"], "only t1 enabled, id ascending")
check(body["protocol"]["A"]["initial"] == 0, "protocol echoed back")

# 2. unknown session -> 404 ------------------------------------------------
r = client.get("/v1/sessions/deadbeef")
check(r.status_code == 404, f"unknown session 404, got {r.status_code}")
r = client.post("/v1/sessions/deadbeef/transitions/t1", json={"expected_version": 0})
check(r.status_code == 404, "execute unknown session 404")
r = client.put("/v1/sessions/deadbeef/protocol",
               json={"expected_version": 0, "protocol": protocol()})
check(r.status_code == 404, "replace unknown session 404")

# 3. illegal protocol reuses validation -> 422 ----------------------------
bad = protocol()
bad["transitions"][0]["id"] = "t2"  # duplicate id
r = client.post("/v1/sessions", json=bad)
check(r.status_code == 422, f"duplicate ids 422, got {r.status_code}")
bad = protocol()
bad["A"]["initial"] = 9
r = client.post("/v1/sessions", json=bad)
check(r.status_code == 422, "illegal initial state 422")
r = client.post("/v1/sessions", json={"A": {}, "B": {}, "transitions": []})
check(r.status_code == 422, "missing fields 422")

# 4. non-executable transition -> 409, state unchanged --------------------
r = client.post(f"/v1/sessions/{sid}/transitions/unknown",
                json={"expected_version": 0})
check(r.status_code == 409, f"unknown transition id 409, got {r.status_code}")
r = client.post(f"/v1/sessions/{sid}/transitions/t2", json={"expected_version": 0})
check(r.status_code == 409, f"t2 not enabled at start -> 409, got {r.status_code}")
check(r.json()["detail"]["code"] == "transition_not_enabled", "409 code")
g = client.get(f"/v1/sessions/{sid}").json()
check(g["version"] == 0 and g["trace"] == [], "failed execution keeps old session")

# 5. stale expected_version on execution -> 409 ---------------------------
r = client.post(f"/v1/sessions/{sid}/transitions/t1", json={"expected_version": 7})
check(r.status_code == 409, "stale version -> 409")
check(r.json()["detail"]["current_version"] == 0, "409 carries current version")

# 6. execute t1 -------------------------------------------------------------
r = client.post(f"/v1/sessions/{sid}/transitions/t1", json={"expected_version": 0})
check(r.status_code == 200, f"execute t1 200, got {r.status_code} {r.text}")
body = r.json()
check(body["version"] == 1, "version incremented to 1")
check(body["configuration"] == {
    "state": {"A": 1, "B": 0},
    "queues": {"A_to_B": "m", "B_to_A": None},
}, "configuration after send")
check(len(body["trace"]) == 1, "trace length 1")
step = body["trace"][0]
check(step["index"] == 1 and step["transition_id"] == "t1"
      and step["state"] == {"A": 1, "B": 0}
      and step["queues"] == {"A_to_B": "m", "B_to_A": None}
      and step["action"] == "send" and step["message"] == "m"
      and step["from"] == "A" and step["to"] == "B",
      "trace step carries id, state, queues, and transition details")
check(body["enabled_transition_ids"] == ["t2"], "t2 now enabled")

# query returns same-version snapshot
g = client.get(f"/v1/sessions/{sid}").json()
check(g == body, "GET returns the same version snapshot")

# 7. replace protocol: valid change (rename message), replay t1 ------------
p2 = protocol(handshake="n")
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 1, "protocol": p2})
check(r.status_code == 200, f"valid replacement 200, got {r.status_code} {r.text}")
body = r.json()
check(body["version"] == 2, "version incremented to 2 after replace")
check(body["protocol"]["transitions"][0]["message"] == "n", "protocol replaced")
check(body["trace"][0]["transition_id"] == "t1"
      and body["trace"][0]["message"] == "n"
      and body["trace"][0]["state"] == {"A": 1, "B": 0}
      and body["trace"][0]["queues"] == {"A_to_B": "n", "B_to_A": None},
      "trace replayed from new initial with new transition definition")
check(body["configuration"]["queues"]["A_to_B"] == "n",
      "current configuration recomputed under new protocol")
check(body["enabled_transition_ids"] == ["t2"], "t2 enabled after replay")

# 8. replacement with stale version -> 409, old session kept ---------------
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 1, "protocol": p2})
check(r.status_code == 409, "stale replace -> 409")
g = client.get(f"/v1/sessions/{sid}").json()
check(g["version"] == 2 and g["protocol"]["transitions"][0]["message"] == "n",
      "failed replace keeps old session/version")

# 9. replacement that breaks replay -> 422 with first failed step ----------
# History is [t1(send)]. New protocol: t1 sends "z", t2 receives "q": t1 still
# fireable at the initial config, so replay of step 1 succeeds... Instead make
# t1 fireable only from state 1 (A starts at 0): step 1 fails.
p3 = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send",
         "message": "n", "source": 1, "target": 1},
        {"id": "t2", "from": "A", "to": "B", "action": "receive",
         "message": "n", "source": 0, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 2, "protocol": p3})
check(r.status_code == 422, f"unfireable replay step -> 422, got {r.status_code}")
detail = r.json()["detail"]
check(detail["code"] == "replay_failed" and detail["failed_step"] == 1
      and detail["transition_id"] == "t1",
      "422 names first failing step (1-based) and transition id")
g = client.get(f"/v1/sessions/{sid}").json()
check(g["version"] == 2 and g["protocol"]["transitions"][0]["source"] == 0,
      "failed replay leaves protocol and trace untouched")

# removal of an executed id also fails replay
p4 = protocol(handshake="n")
p4["transitions"] = [t for t in p4["transitions"] if t["id"] != "t1"]
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 2, "protocol": p4})
check(r.status_code == 422, "removing executed id -> 422 replay failure")
check(r.json()["detail"]["failed_step"] == 1, "first failed step is 1")

# structurally illegal replacement -> 422 via reused validation
badp = protocol(handshake="n")
badp["transitions"][1]["id"] = "t1"
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 2, "protocol": badp})
check(r.status_code == 422, "illegal replacement protocol 422")

# 10. trace must not reach completion before its end -----------------------
# Drive another session: completion needs two steps t1,t2. Then offer a new
# protocol whose first historical step (t1) by itself lands in completion
# (B initial=final=1 while t1 targets A final with empty queues): step 2 (t2)
# would be executed after completion -> 422 failed_step=2.
s2 = client.post("/v1/sessions", json=protocol()).json()["session_id"]
client.post(f"/v1/sessions/{s2}/transitions/t1", json={"expected_version": 0})
client.post(f"/v1/sessions/{s2}/transitions/t2", json={"expected_version": 1})
g2 = client.get(f"/v1/sessions/{s2}").json()
check(g2["version"] == 2 and g2["completed"] is True and len(g2["trace"]) == 2
      and g2["enabled_transition_ids"] == [], "completed session snapshot")
r = client.post(f"/v1/sessions/{s2}/transitions/t1", json={"expected_version": 2})
check(r.status_code == 409, "no further execution once completed -> 409")

p5 = {
    # Historical trace is [t1 (send), t2 (receive)] with A owning both.
    # Under the replacement B owns them; step 1 (B send n) is fireable, but
    # t2 expects "q" while the B_to_A slot holds "n": step 2 is not fireable.
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "B", "to": "A", "action": "send",
         "message": "n", "source": 0, "target": 1},
        {"id": "t2", "from": "B", "to": "A", "action": "receive",
         "message": "q", "source": 0, "target": 1},
    ],
}
# Under p5 step 1 fires (B 0->1, B_to_A=n), A=0 is not final; step 2 is a
# B-side receive expecting "q" from the B_to_A slot that holds "n" -> fails.
r = client.put(f"/v1/sessions/{s2}/protocol",
               json={"expected_version": 2, "protocol": p5})
check(r.status_code == 422, f"replay fails at step 2 -> 422, got {r.status_code}")
check(r.json()["detail"]["failed_step"] == 2, "first failed step reported as 2 (1-based)")
g2b = client.get(f"/v1/sessions/{s2}").json()
check(g2b["version"] == 2 and g2b["completed"] is True, "old completed session kept")

# 10b. completion before the end of the trace -> 422 ------------------------
# New session with history [t1, t2] (2 steps). Replacement makes the initial
# configuration itself a completion (both endpoints start final, empty queues):
# step 1 can no longer be executed because completion configurations never
# expand, so replay fails at step 1 even though t1 is structurally enabled.
p6 = {
    "A": {"states": [1], "initial": 1, "finals": [1]},
    "B": {"states": [1], "initial": 1, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send",
         "message": "n", "source": 1, "target": 1},
        {"id": "t2", "from": "A", "to": "B", "action": "receive",
         "message": "n", "source": 1, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{s2}/protocol",
               json={"expected_version": 2, "protocol": p6})
check(r.status_code == 422, f"completed before trace end -> 422, got {r.status_code}")
check(r.json()["detail"]["failed_step"] == 1, "first blocked step is 1")

# 11. concurrent same-version mutations: only one succeeds -----------------
s3 = client.post("/v1/sessions", json=protocol()).json()["session_id"]


def mutate(_):
    return client.post(f"/v1/sessions/{s3}/transitions/t1",
                       json={"expected_version": 0}).status_code


with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    codes = list(pool.map(mutate, range(8)))
check(codes.count(200) == 1 and codes.count(409) == 7,
      f"exactly one concurrent same-version execution succeeds: {codes}")
check(client.get(f"/v1/sessions/{s3}").json()["version"] == 1,
      "version advanced exactly once")

# same guarantee for concurrent protocol replacement
s4 = client.post("/v1/sessions", json=protocol()).json()["session_id"]
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    codes = list(pool.map(
        lambda _: client.put(f"/v1/sessions/{s4}/protocol",
                             json={"expected_version": 0, "protocol": protocol("n")}).status_code,
        range(8)))
check(codes.count(200) == 1 and codes.count(409) == 7,
      f"exactly one concurrent same-version replacement succeeds: {codes}")

# 12. continue executing after replacement ---------------------------------
# s1 at version 2: history [t1] under protocol with message "n"; finish it.
r = client.post(f"/v1/sessions/{sid}/transitions/t2", json={"expected_version": 2})
check(r.status_code == 200, f"execute after replace 200, got {r.status_code} {r.text}")
body = r.json()
check(body["version"] == 3 and body["completed"] is True
      and [s["transition_id"] for s in body["trace"]] == ["t1", "t2"],
      "execution continues on the replayed trace and reaches completion")
check(body["configuration"] == {
    "state": {"A": 1, "B": 1},
    "queues": {"A_to_B": None, "B_to_A": None},
}, "completion configuration")

# 13. validation quirks on new endpoints -----------------------------------
r = client.post(f"/v1/sessions/{sid}/transitions/t1", json={})
check(r.status_code == 422, "missing expected_version -> 422")
r = client.post(f"/v1/sessions/{sid}/transitions/t1",
                json={"expected_version": 0, "extra": 1})
check(r.status_code == 422, "unknown body field -> 422")
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 3})
check(r.status_code == 422, "missing protocol in replace -> 422")

print("\nALL CHECKS PASSED")
