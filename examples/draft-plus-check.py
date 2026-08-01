#!/usr/bin/env python3
"""draft-plus-check: a cheap, honest use of small models served over IICP.

A small DRAFTER model labels public text; an independent VERIFIER model labels
it too. Agreement is accepted; disagreement is escalated to a bigger model or a
human. This is a good first-pass + disagreement detector, NOT a verifier for
anything with stakes: two weak, correlated models can agree on a WRONG answer
(the demo below deliberately contains a case where they do).

Topology
--------
This example runs BOTH models on your local node (direct POST to the node's
wire port), because that always works. To make the drafter a real remote offload,
replace `classify(h, DRAFTER)` with an `iicp-node query` subprocess call:

    iicp-node query "<prompt>" --allow-remote-executor \
        --routing-profile debug-override --model <drafter-model> \
        --max-tokens 10 --timeout-ms 60000

Only send PUBLIC prompts to remote nodes: the remote operator can read every
prompt it executes (use --routing-profile sensitive for fail-closed local-only).

Requires: a running local IICP node (wire port 9484) with the two models pulled.
"""
import json, os, time, urllib.request

CATS = ["Tech", "Business", "Sports", "Politics", "Science", "Entertainment"]
DRAFTER = "phi3:mini"       # ~3.8B
VERIFIER = "qwen2.5:1.5b"   # 1.5B, independent
NODE = "http://127.0.0.1:9484/v1/task"
TOK = json.load(open(os.path.expanduser("~/.iicp/nodes/default.json")))["node_token"]

HEADLINES = [
    "New space telescope captures the first image of a distant galaxy cluster",  # Science
    "Tech giant's quarterly earnings beat Wall Street expectations",             # Tech/Business (split)
    "Home team wins the championship in an overtime thriller",                   # Sports
    "Streaming platform's new sci-fi series breaks viewership records",          # Entertainment (both may miss)
]
PROMPT = ("Classify this news headline into exactly one category from this list: "
          + ", ".join(CATS) + ". Reply with ONLY the single category word.\nHeadline: ")


def extract(text):
    low = (text or "").lower()
    return next((c for c in CATS if c.lower() in low), "?")


def classify(headline, model):
    body = {"task_id": "cls", "intent": "urn:iicp:intent:llm:chat:v1",
            "payload": {"messages": [{"role": "user", "content": PROMPT + headline}],
                        "model": model, "max_tokens": 10}}
    req = urllib.request.Request(NODE, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    t = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=90))
    content = r["result"]["choices"][0]["message"]["content"]
    return extract(content), (time.time() - t) * 1000


classify("warm up", DRAFTER)  # absorb the cold-load hit before timing
verified = flagged = 0
for h in HEADLINES:
    d, d_ms = classify(h, DRAFTER)
    v, v_ms = classify(h, VERIFIER)
    verdict = "VERIFIED" if d == v else "FLAG -> escalate"
    verified += d == v
    flagged += d != v
    print(f"\n- {h}")
    print(f"    drafter  {DRAFTER:14}: {d:14} {d_ms:6.0f}ms")
    print(f"    verifier {VERIFIER:14}: {v:14} {v_ms:6.0f}ms")
    print(f"    verdict: {verdict}")
print(f"\nVERIFIED {verified}  FLAGGED {flagged}  of {len(HEADLINES)}")
print("Reminder: agreement between two small models is not correctness. Use a")
print("genuinely stronger, independent verifier for anything that matters.")
