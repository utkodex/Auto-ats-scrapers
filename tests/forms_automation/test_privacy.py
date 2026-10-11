"""No stored answer may reach Llama when creating heads or classifying questions.

The stores hold sentinel answers; every request sent to the (faked) Ollama is
captured and searched for them.
"""

from __future__ import annotations

import json

from forms_automation.ai import head_creator, jev
from forms_automation.db.head_store import HeadStore

URL = f"{head_creator.llm.OLLAMA}/api/chat"


def ollama_reply(request):
    import httpx
    body = json.loads(request.content)
    props = body["format"]["properties"]
    if "heads" in props:
        items = json.loads(body["messages"][1]["content"].split("\n\nYour previous")[0])
        content = {"heads": [{"id": f"topic_{q['id']}", "description": "d", "question_ids": [q["id"]]}
                             for q in items]}
    elif "same_answer" in props:
        content = {"reason": "r", "same_answer": "yes"}
    else:
        content = {"head": props["head"]["enum"][0], "confidence": 0.9, "reason": "r"}
    return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(content)}})


def sent_text(httpx_mock) -> str:
    return "\n".join(r.content.decode("utf-8") for r in httpx_mock.get_requests())


def test_head_creation_and_classification_send_no_answers(httpx_mock, qstore, tmp_path, sentinels):
    httpx_mock.add_callback(ollama_reply, url=URL, is_reusable=True)
    heads = HeadStore(tmp_path / "heads.json")
    qstore.add_generated("Why do you want this internship?", "https://x/jobs/1", "SENTINEL-ESSAY", "m")
    head_creator.build_initial_heads(qstore, heads)
    for h in heads.pending():
        heads.update(h["id"], status="approved")
    entry = qstore.get("What is your gender?")
    head_creator.propose_head(entry, heads)
    jev.clear_cache()
    jev.classify_head(entry["question"], entry["type"], entry["options"], heads.approved())

    requests = httpx_mock.get_requests()
    assert len(requests) == 4  # build, propose, classify, strict check
    text = sent_text(httpx_mock)
    for secret in [*sentinels, "SENTINEL-ESSAY"]:
        assert secret not in text
